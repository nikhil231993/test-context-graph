"""Graph + parser facts -> Chunks to embed. Pure: no file, network or clock access.

One chunk per method, type, endpoint, schema and doc-section node, with the node id as the chunk
id. A chunk's text is what an agent is shown: a method's source, a type's outline (not the whole
file), an endpoint's contract, a schema's YAML, a doc section. Its embed_text puts a short header
in front (where it is, what it is), so a search can match names the text doesn't repeat. A chunk
longer than max_chars is split at line boundaries into "<node id>#part2", "#part3"..., each with
the header again. Trivial accessors of entities, DTOs and plain classes get no chunk: they would
crowd out the code worth finding.
"""

from __future__ import annotations

import hashlib
import re
import textwrap
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .models import (
    DOC,
    ENDPOINT,
    METHOD,
    SCHEMA,
    SERVES,
    TYPE,
    Annotation,
    ApiSchema,
    ApiSpec,
    Chunk,
    DocSection,
    FieldDecl,
    Graph,
    JavaFile,
    MethodDecl,
    Node,
    TypeDecl,
    doc_id,
    method_id,
    schema_id,
    type_id,
)

_TRIVIAL_ROLES = frozenset({"entity", "dto", "other"})
_ACCESSOR = re.compile(r"(?:get|set|is)[A-Z0-9_]")
_MAX_ACCESSOR_LINES = 3  # code lines, annotations aside: "int getId() {", "return id;", "}"
_MAX_INITIALIZER = 80  # a field initializer in a type outline is cut after this many characters
_INDENT = "    "
_SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)\s*")
# An annotation argument that is code, not a string: Owner.class, RequestMethod.GET, MAX, 42.
_CODE_VALUE = re.compile(
    r"[\w$.]+\.class|[A-Z][\w$]*(?:\.[\w$]+)+|[A-Z_][A-Z0-9_]*|-?\d[\w.]*|true|false|null"
    r"|@.*|\{.*\}",
    re.DOTALL,
)


def make_chunks(
    graph: Graph,
    java_files: Sequence[JavaFile],
    sources: dict[str, str],
    specs: Sequence[ApiSpec] = (),
    docs: Sequence[DocSection] = (),
    max_chars: int = 4000,
) -> list[Chunk]:
    """The chunks to embed for a graph, in node id order. `sources` maps a Java file's path to
    its text; a method whose file is missing gets its signature as text."""
    facts = _Facts(graph, java_files, sources, specs, docs)
    chunks: list[Chunk] = []
    for node in sorted(graph.nodes, key=lambda n: n.id):
        builder = _BUILDERS.get(node.kind)
        draft = builder(node, facts) if builder else None
        if draft is not None:
            chunks.extend(_split(draft, max_chars))
    return chunks


@dataclass
class _Draft:
    """A chunk before splitting."""

    node: Node
    header: str
    text: str
    first_line: int | None = None  # the source line of the text's first line, if lines map 1:1


class _Facts:
    """The parser facts behind each node, by node id, for the chunk builders."""

    def __init__(
        self,
        graph: Graph,
        java_files: Sequence[JavaFile],
        sources: dict[str, str],
        specs: Sequence[ApiSpec],
        docs: Sequence[DocSection],
    ) -> None:
        self.nodes = {n.id: n for n in graph.nodes}
        self.sources = sources
        self.types: dict[str, tuple[TypeDecl, JavaFile]] = {}
        self.nested: dict[str, list[TypeDecl]] = defaultdict(list)
        self.methods: dict[str, tuple[MethodDecl, JavaFile]] = {}
        for java_file in sorted(java_files, key=lambda f: f.path):  # first one wins, as in graph
            for decl in java_file.types:
                self._add_type(decl, java_file)
        self.schemas: dict[str, ApiSchema] = {}
        for spec in sorted(specs, key=lambda s: s.path):
            for schema in spec.schemas:
                self.schemas.setdefault(schema_id(spec.path, schema.name), schema)
        self.sections: dict[str, DocSection] = {}
        for section in sorted(docs, key=lambda d: (d.path, d.start_line, d.anchor)):
            self.sections.setdefault(doc_id(section.path, section.anchor), section)
        self.servers: dict[str, list[str]] = defaultdict(list)  # endpoint id -> method ids
        for edge in graph.edges:
            if edge.kind == SERVES:
                self.servers[edge.target].append(edge.source)
        self._lines: dict[str, list[str] | None] = {}

    def _add_type(self, decl: TypeDecl, java_file: JavaFile) -> None:
        if decl.qualified_name in self.types:
            return
        self.types[decl.qualified_name] = (decl, java_file)
        if decl.outer:
            self.nested[decl.outer].append(decl)
        for m in decl.methods:
            mid = method_id(decl.qualified_name, m.name, [p.type for p in m.parameters])
            self.methods.setdefault(mid, (m, java_file))

    def lines(self, path: str) -> list[str] | None:
        """A source file's lines, split on "\\n" only, as the parser counts them."""
        if path not in self._lines:
            text = self.sources.get(path)
            self._lines[path] = None if text is None else [s.rstrip("\r") for s in text.split("\n")]
        return self._lines[path]


# ---- drafts, by node kind ------------------------------------------------------------------


def _method_draft(node: Node, facts: _Facts) -> _Draft | None:
    found = facts.methods.get(node.id)
    if found is None:
        return None
    m, java_file = found
    owner = facts.nodes.get(type_id(_owner(node.id)))
    lines = facts.lines(java_file.path)
    body = lines[m.start_line - 1 : m.end_line] if lines else []
    if owner and owner.attrs.get("role") in _TRIVIAL_ROLES and _is_trivial_accessor(m, body):
        return None
    javadoc = _javadoc(m.javadoc)
    code = textwrap.dedent("\n".join(body)) if body else m.signature
    header = _header(java_file.path, _type_label(owner), m.signature)
    first_line = m.start_line - len(javadoc) if body else None
    return _Draft(node, header, "\n".join([*javadoc, code]), first_line)


def _type_draft(node: Node, facts: _Facts) -> _Draft | None:
    if node.attrs.get("external"):
        return None
    found = facts.types.get(node.attrs.get("qualified_name", ""))
    if found is None:
        return None
    decl, java_file = found
    text = _outline(decl, facts.nested.get(decl.qualified_name, []))
    return _Draft(node, _header(java_file.path, _type_label(node)), text)


def _endpoint_draft(node: Node, facts: _Facts) -> _Draft:
    servers = sorted(facts.servers.get(node.id, []))
    return _Draft(node, _header(node.path, f"endpoint {node.name}"), _endpoint_text(node, servers))


def _schema_draft(node: Node, facts: _Facts) -> _Draft:
    schema = facts.schemas.get(node.id)
    text = schema.text.rstrip() if schema and schema.text.strip() else _schema_outline(node)
    return _Draft(node, _header(node.path, f"schema {node.name}"), text)


def _doc_draft(node: Node, facts: _Facts) -> _Draft | None:
    section = facts.sections.get(node.id)
    if section is None or not _has_body(section):
        return None
    title = " > ".join([*section.breadcrumbs, section.heading]) if section.heading else ""
    return _Draft(node, _header(section.path, title), section.text.rstrip(), section.start_line)


_BUILDERS: dict[str, Callable[[Node, _Facts], _Draft | None]] = {
    METHOD: _method_draft,
    TYPE: _type_draft,
    ENDPOINT: _endpoint_draft,
    SCHEMA: _schema_draft,
    DOC: _doc_draft,
}


# ---- texts ---------------------------------------------------------------------------------


def _outline(decl: TypeDecl, nested: list[TypeDecl]) -> str:
    """A type without its method bodies: annotations, declaration, fields, method signatures
    and nested type names; what an agent needs to use the type."""
    groups = [
        [", ".join(decl.enum_constants) + ";"] if decl.enum_constants else [],
        [_field_line(f) for f in decl.fields],
        [_member_line(m.annotations, m.signature) for m in decl.methods],
        [f"{_keyword(n.kind)} {_simple(n.name)} {{ ... }}" for n in nested],
    ]
    body: list[str] = []
    for group in (g for g in groups if g):
        body += ([""] if body else []) + [_INDENT + line for line in group]
    head = [*_javadoc(decl.javadoc), *map(_annotation_text, decl.annotations)]
    return "\n".join([*head, _declaration(decl) + " {", *body, "}"])


def _declaration(decl: TypeDecl) -> str:
    name = _simple(decl.name)
    if decl.type_parameters:
        name += "<" + ", ".join(decl.type_parameters) + ">"
    if decl.kind == "record":
        name += "(" + ", ".join(f"{p.type.text} {p.name}" for p in decl.record_components) + ")"
    parts = [*decl.modifiers, _keyword(decl.kind), name]
    if decl.extends:
        parts.append("extends " + ", ".join(t.text for t in decl.extends))
    if decl.implements:
        parts.append("implements " + ", ".join(t.text for t in decl.implements))
    return " ".join(parts)


def _field_line(f: FieldDecl) -> str:
    initializer = f" = {_shorten(f.initializer, _MAX_INITIALIZER)}" if f.initializer else ""
    return _member_line(f.annotations, " ".join([*f.modifiers, f.type.text, f.name]) + initializer)


def _member_line(annotations: list[Annotation], declaration: str) -> str:
    return " ".join([*map(_annotation_text, annotations), declaration]) + ";"


def _annotation_text(a: Annotation) -> str:
    """An annotation as source: @RequestMapping("api"), @Mapping(target = "id", ignore = true)."""
    if not a.arguments:
        return "@" + a.name
    if list(a.arguments) == ["value"]:
        return f"@{a.name}({_value_text(a.arguments['value'])})"
    args = ", ".join(f"{key} = {_value_text(value)}" for key, value in a.arguments.items())
    return f"@{a.name}({args})"


def _value_text(value: Any) -> str:
    """An annotation argument as source: strings quoted again, code (Owner.class, 42) as is."""
    if isinstance(value, list):
        return "{" + ", ".join(_value_text(v) for v in value) + "}"
    text = str(value)
    if _CODE_VALUE.fullmatch(text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _endpoint_text(node: Node, servers: list[str]) -> str:
    a = node.attrs
    lines = [node.name]
    for label, key in (("operationId", "operation_id"), ("summary", "summary")):
        if a.get(key):
            lines.append(f"{label}: {a[key]}")
    if a.get("description"):
        lines.append(f"description: {a['description']}")
    if a.get("tags"):
        lines.append("tags: " + ", ".join(a["tags"]))
    if a.get("parameters"):
        lines += ["parameters:", *(_parameter_line(p) for p in a["parameters"])]
    if a.get("request_body"):
        lines.append(f"request body: {a['request_body']}")
    if a.get("responses"):
        lines += ["responses:", *(f"  {status}: {v}" for status, v in a["responses"].items())]
    for label, key in (("served at", "served_path"), ("url", "url_path")):
        if a.get(key):
            lines.append(f"{label}: {a[key]}")
    if servers:
        lines += ["served by:", *(f"  {mid}" for mid in servers)]
    return "\n".join(lines)


def _parameter_line(p: dict[str, Any]) -> str:
    required = ", required" if p.get("required") else ""
    return f"  {p.get('name')} ({p.get('location')}{required}): {p.get('schema') or 'any'}"


def _schema_outline(node: Node) -> str:
    """A schema without its YAML text: name, allOf, required, properties."""
    a = node.attrs
    lines = [f"{node.name}:"]
    if a.get("all_of"):
        lines.append("  allOf: " + ", ".join(a["all_of"]))
    if a.get("required"):
        lines.append("  required: " + ", ".join(a["required"]))
    if a.get("properties"):
        lines += ["  properties:", *(f"    {k}: {v}" for k, v in a["properties"].items())]
    return "\n".join(lines)


def _javadoc(text: str | None) -> list[str]:
    """A javadoc's text (markers stripped by the parser) as a /** */ block again."""
    if not text:
        return []
    return ["/**", *((" * " + line).rstrip() for line in text.split("\n")), " */"]


def _is_trivial_accessor(m: MethodDecl, body: list[str]) -> bool:
    """get*/set*/is* with at most 3 lines of code, annotations and blank lines aside."""
    if m.kind != "method" or not _ACCESSOR.match(m.name):
        return False
    if not body:
        return m.end_line - m.start_line + 1 <= _MAX_ACCESSOR_LINES
    code = [line for line in body if line.strip() and not line.strip().startswith("@")]
    return len(code) <= _MAX_ACCESSOR_LINES


def _has_body(section: DocSection) -> bool:
    """Anything beyond the heading line (or the two lines of a setext heading)."""
    lines = section.text.split("\n")
    if section.level > 0:
        setext = len(lines) > 1 and _SETEXT_UNDERLINE.fullmatch(lines[1])
        lines = lines[2 if setext else 1 :]
    return any(line.strip() for line in lines)


def _type_label(node: Node | None) -> str:
    """ "class org.example.OwnerController (controller)" """
    if node is None:
        return ""
    a = node.attrs
    return f"{a.get('type_kind', 'type')} {a.get('qualified_name', node.name)} ({a.get('role')})"


def _header(*parts: str | None) -> str:
    return "\n".join(p for p in parts if p)


def _owner(method_node_id: str) -> str:
    """The qualified type name of a method id: method:org.example.Owner#getPet(String)."""
    return method_node_id.removeprefix("method:").split("#", 1)[0]


def _simple(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _keyword(kind: str) -> str:
    return "@interface" if kind == "annotation" else kind


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---- splitting -----------------------------------------------------------------------------


def _split(draft: _Draft, max_chars: int) -> list[Chunk]:
    """One chunk, or parts of at most max_chars (header included) cut at line boundaries."""
    node = draft.node
    if len(draft.header) + 1 + len(draft.text) <= max_chars:
        return [_chunk(draft, node.id, draft.text, node.start_line, node.end_line)]
    budget = max(max_chars - len(draft.header) - 1, max(1, max_chars // 4))
    chunks = []
    for number, (text, first, last) in enumerate(_pack(draft.text.split("\n"), budget), 1):
        chunk_id = node.id if number == 1 else f"{node.id}#part{number}"
        start, end = _part_lines(draft, first, last)
        chunks.append(_chunk(draft, chunk_id, text, start, end))
    return chunks


def _pack(lines: list[str], budget: int) -> list[tuple[str, int, int]]:
    """Lines grouped into parts of at most `budget` characters: (text, first line index, last
    line index). A line longer than the budget is cut into pieces."""
    parts: list[tuple[str, int, int]] = []
    current: list[str] = []
    size = first = last = 0
    for index, line in enumerate(lines):
        for piece in [line[i : i + budget] for i in range(0, len(line), budget)] or [""]:
            added = len(piece) + (1 if current else 0)
            if current and size + added > budget:
                parts.append(("\n".join(current), first, last))
                current, size, added = [], 0, len(piece)
            if not current:
                first = index
            current.append(piece)
            size, last = size + added, index
    if current:
        parts.append(("\n".join(current), first, last))
    return parts


def _part_lines(draft: _Draft, first: int, last: int) -> tuple[int | None, int | None]:
    """The source lines of a part, when the text's lines map to the source; else the node's."""
    node = draft.node
    if draft.first_line is None or node.start_line is None:
        return node.start_line, node.end_line
    low, high = node.start_line, node.end_line or node.start_line

    def clamp(line: int) -> int:
        return min(max(line, low), high)

    return clamp(draft.first_line + first), clamp(draft.first_line + last)


def _chunk(draft: _Draft, chunk_id: str, text: str, start: int | None, end: int | None) -> Chunk:
    embed_text = f"{draft.header}\n{text}"
    return Chunk(
        id=chunk_id,
        node_id=draft.node.id,
        kind=draft.node.kind,
        text=text,
        embed_text=embed_text,
        content_hash=hashlib.sha256(embed_text.encode("utf-8")).hexdigest(),
        path=draft.node.path,
        start_line=start,
        end_line=end,
    )
