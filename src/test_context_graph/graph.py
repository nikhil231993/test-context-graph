"""JavaFiles + ApiSpecs + DocSections -> Graph. Pure: no file, network or clock access, and the
same inputs in any order give the same graph (nodes sorted by id, edges by kind, source, target).

There is no compiler here, so most links are heuristics. Each heuristic edge records how it was
found (`resolution` on CALLS, `reason` on TESTS, `via` on INJECTS, SERVES and MOCKS), so a reader
can tell a sure link from a guess.

- Type names resolve the way javac would, as far as one file shows it (TypeIndex.resolve).
- Types generated at build time (OpenAPI `*Api` interfaces and `*Dto` classes, MapStruct
  `*MapperImpl`) are not in the source tree. A single-type import of one from the project's own
  packages gives it a stub node (external, generated), so signatures and DTO schemas still link.
- Library types get no nodes, and calls into libraries no edges.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from .http_calls import HttpCallFact, client_env, find_http_calls, module_of
from .manifest import source_set
from .models import (
    CALLS,
    CALLS_HTTP,
    CONFIG,
    CONFIGURED_BY,
    CONTAINS,
    DOC,
    ENDPOINT,
    EXTENDS,
    FILE,
    GENERATED_FROM,
    HTTP_CALL,
    IMPLEMENTS,
    INJECTS,
    MENTIONS,
    METHOD,
    MOCKS,
    OVERRIDES,
    REFERENCES,
    SCHEMA,
    SERVES,
    TARGETS,
    TESTS,
    TYPE,
    USES,
    Annotation,
    ApiOperation,
    ApiSpec,
    CallSite,
    ConfigProperty,
    DocSection,
    Edge,
    FieldDecl,
    Graph,
    Import,
    JavaFile,
    MethodDecl,
    Node,
    Parameter,
    TypeDecl,
    TypeRef,
    config_id,
    doc_id,
    endpoint_id,
    erase,
    file_id,
    http_call_id,
    join_paths,
    method_id,
    schema_id,
    to_dict,
    type_id,
)

# ---- vocabulary ----------------------------------------------------------------------------

PRIMITIVES = frozenset(
    {"boolean", "byte", "char", "short", "int", "long", "float", "double", "void", "var"}
)
# java.lang names, visible without an import. As in Java, an enclosing type's member, a
# single-type import or a type of the same package still wins over them; wildcard imports don't.
JAVA_LANG = frozenset(
    {
        "Object", "String", "CharSequence", "StringBuilder", "StringBuffer", "Boolean", "Byte",
        "Character", "Short", "Integer", "Long", "Float", "Double", "Number", "Void", "Math",
        "System", "Thread", "Runnable", "Iterable", "Comparable", "AutoCloseable", "Cloneable",
        "Class", "Enum", "Record", "Throwable", "Exception", "Error", "RuntimeException",
        "IllegalArgumentException", "IllegalStateException", "NullPointerException",
        "UnsupportedOperationException", "IndexOutOfBoundsException", "ClassCastException",
        "NumberFormatException", "InterruptedException", "Override", "Deprecated",
        "SuppressWarnings", "FunctionalInterface", "SafeVarargs", "Process", "Runtime",
        "ThreadLocal", "Module", "Package", "AssertionError", "SecurityException",
    }
)  # fmt: skip

TEST_ANNOTATIONS = frozenset(
    {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"}
)
_CONTROLLER = frozenset({"RestController", "Controller"})
_CONTROLLER_ADVICE = frozenset({"ControllerAdvice", "RestControllerAdvice"})
_ENTITY = frozenset({"Entity", "MappedSuperclass", "Embeddable"})
_CONFIG = frozenset({"Configuration", "TestConfiguration", "SpringBootApplication"})
_REPOSITORY_BASES = frozenset(
    {
        "Repository", "CrudRepository", "JpaRepository", "PagingAndSortingRepository",
        "ListCrudRepository", "ListPagingAndSortingRepository", "ReactiveCrudRepository",
    }
)  # fmt: skip

_INJECTED_FIELD = frozenset({"Autowired", "Inject", "Resource"})
_INJECTED_SETTER = frozenset({"Autowired", "Inject"})
# Injected containers whose element type is the dependency: List<Validator>, Optional<Clock>.
_CONTAINERS = frozenset(
    {"List", "Set", "Collection", "Iterable", "Map", "Optional", "ObjectProvider", "Provider"}
)

_TESTED_FIELD = frozenset({"Autowired", "Inject", "InjectMocks"})
_MOCK_FIELD = frozenset({"Mock", "MockBean", "MockitoBean", "Spy", "SpyBean", "MockitoSpyBean"})
_SUBJECT_ANNOTATIONS = frozenset({"WebMvcTest", "WebFluxTest"})
_TEST_SUFFIXES = ("IntegrationTests", "IntegrationTest", "Tests", "Test", "IT")
_TEST_PREFIXES = ("Abstract", "Test")
# MockMvc request builders, usually static imports: name -> HTTP method (None: any).
_REQUEST_BUILDERS: dict[str, str | None] = {
    "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE",
    "head": "HEAD", "options": "OPTIONS", "request": None,
}  # fmt: skip

# Mapping annotations: name -> HTTP method (None: given by `method`, as on @RequestMapping).
_MAPPINGS: dict[str, str | None] = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH", "RequestMapping": None,
}  # fmt: skip
ANY_METHOD = "ANY"  # a @RequestMapping without `method` serves every HTTP method

# JDK method names too common to link by name alone: an unknown receiver of `.stream()`,
# `.get(0)` or `.orElseThrow()` is far more likely a JDK type than the one project method.
_NAME_ONLY_SKIP = frozenset(
    {
        "equals", "hashCode", "toString", "getClass", "compareTo", "clone", "get", "set", "add",
        "addAll", "put", "putAll", "remove", "contains", "containsKey", "size", "isEmpty",
        "clear", "stream", "map", "flatMap", "filter", "collect", "forEach", "reduce", "sorted",
        "findFirst", "findAny", "anyMatch", "allMatch", "noneMatch", "count", "toList",
        "toArray", "of", "valueOf", "build", "builder", "apply", "accept", "test", "run",
        "call", "iterator", "next", "hasNext", "length", "close", "orElse", "orElseGet",
        "orElseThrow", "isPresent", "ifPresent",
    }
)  # fmt: skip

MAX_MENTIONS = 30  # per doc section, so an index page doesn't link to everything

_HTTP_METHOD = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|TRACE)\b")
_CLASS_LITERAL = re.compile(r"([\w$.]+)\.class\b")
_MOCK_CALL = re.compile(r"^(?:Mockito\s*\.\s*)?mock\s*\(\s*([\w$.]+?)\s*\.\s*class\b")
_NEW_CALL = re.compile(r"^new\s+([\w$.]+)")
_PLACEHOLDER = re.compile(r"\$\{[^}:]*(?::([^}]*))?\}")  # ${name:default}
_BARE_PLACEHOLDER = re.compile(r"\$\{[^}:]*\}")  # ${name}: no default, so no known value
_CONSTANT_REF = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+|[A-Z_][A-Z0-9_]*")
_PATH_PARAM = re.compile(r"\{[^{}/\s]*\}")
_METHOD_TYPE_PARAMS = re.compile(r"^(?:[a-z]+\s+)*<(.*?)>\s")  # "public <T> T f(...)" -> "T"
_SHORT_FORM = re.compile(r"[\w.$-]+(?:<[\w.$<>, -]*>)?")  # "Owner", "array<Owner>"
_SHORT_FORM_NAME = re.compile(r"[\w.$-]+")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_TYPE_NAME = re.compile(r"[A-Za-z_$][\w$.]*")
_PROVIDER_DIR = re.compile(
    r"/(?:contracts?|providers?|downstream|dependencies|consumes|clients?|external|specmatic)/"
)
_PATH_TOKEN = re.compile(r"(?<![\w/{}.-])/[\w/{}.-]*")  # "/api/owners/{}" in prose


# ---- public API ----------------------------------------------------------------------------


def build_graph(
    java_files: Sequence[JavaFile],
    specs: Sequence[ApiSpec] = (),
    docs: Sequence[DocSection] = (),
    config: Sequence[ConfigProperty] = (),
) -> Graph:
    """The context graph of a codebase: its files, types, methods, endpoints, schemas, doc
    sections, config properties and outbound HTTP calls, and the edges between them (see
    models.py for the kinds)."""
    return _Builder(java_files, specs, docs, config).build()


def project_prefix(packages: Iterable[str]) -> str:
    """The longest dotted prefix the packages share; "" when it has fewer than two segments,
    as a lone "com" or "org" would make every vendor's package part of the project."""
    common: list[str] | None = None
    for package in packages:
        if not package:
            continue
        parts = package.split(".")
        if common is None:
            common = parts
            continue
        size = 0
        while size < min(len(common), len(parts)) and common[size] == parts[size]:
            size += 1
        common = common[:size]
    return ".".join(common) if common and len(common) >= 2 else ""


def type_role(decl: TypeDecl, source_set_name: str) -> str:
    """What a type is for; the first rule that matches wins. A type with test methods is a test
    wherever it lives; a Test/Tests/IT name only counts outside the main source set."""
    annotations = _names(decl.annotations)
    simple = _simple(decl.name)
    if _has_tests(decl) or (
        source_set_name != "main" and simple.endswith(("Test", "Tests", "IT"))
    ):
        return "test"
    if source_set_name == "test":
        return "test_support"
    if annotations & _CONTROLLER:
        return "controller"
    if annotations & _CONTROLLER_ADVICE:
        return "controller_advice"
    if "Service" in annotations:
        return "service"
    if "Repository" in annotations or simple.endswith("Repository") or any(
        _simple(ref.name) in _REPOSITORY_BASES for ref in (*decl.extends, *decl.implements)
    ):
        return "repository"
    if annotations & _ENTITY:
        return "entity"
    if "Mapper" in annotations or simple.endswith("MapperImpl"):
        return "mapper"
    if annotations & _CONFIG:
        return "config"
    return "other"


def stub_role(simple_name: str) -> str:
    """The role of a generated type, from its name alone (same order as type_role)."""
    if simple_name.endswith("Repository"):
        return "repository"
    if simple_name.endswith("MapperImpl"):
        return "mapper"
    if simple_name.endswith("Dto"):
        return "dto"
    if simple_name.endswith("Api"):
        return "api_interface"
    return "other"


class TypeIndex:
    """The project's types by qualified name (from the source, plus stubs for generated types
    the source imports), and javac-like name resolution against them."""

    def __init__(self, java_files: Iterable[JavaFile]) -> None:
        files = sorted(java_files, key=lambda f: f.path)
        self.decls: dict[str, tuple[TypeDecl, JavaFile]] = {}
        for java_file in files:
            for decl in java_file.types:
                self.decls.setdefault(decl.qualified_name, (decl, java_file))
        self.prefix = project_prefix(f.package for f in files if source_set(f.path) == "main")
        stubs = {imp.name for f in files for imp in f.imports if self._generated(imp)}
        self.stubs: dict[str, str] = {q: stub_role(_simple(q)) for q in sorted(stubs)}

    def known(self, qualified_name: str) -> bool:
        return qualified_name in self.decls or qualified_name in self.stubs

    def resolve(self, name: str, file: JavaFile, enclosing: str | None = None) -> str | None:
        """The qualified name of the project type (in the source, or a stub) that `name` means
        in `file`, inside type `enclosing`; None for primitives, library and unknown types."""
        name = name.strip()
        if not name or name in PRIMITIVES or not (name[0].isalpha() or name[0] in "_$"):
            return None
        if "." not in name:
            return self._resolve_simple(name, file, enclosing)
        if self.known(name):
            return name
        head, rest = name.split(".", 1)
        base = self._resolve_simple(head, file, enclosing)  # Outer.Inner
        return f"{base}.{rest}" if base and self.known(f"{base}.{rest}") else None

    def enclosing_chain(self, qualified_name: str | None) -> Iterator[str]:
        """A source type, then the types around it, innermost first."""
        seen: set[str] = set()
        while qualified_name in self.decls and qualified_name not in seen:
            seen.add(qualified_name)
            yield qualified_name
            qualified_name = self.decls[qualified_name][0].outer

    def _resolve_simple(self, name: str, file: JavaFile, enclosing: str | None) -> str | None:
        for scope in self.enclosing_chain(enclosing):
            if _simple(scope) == name:
                return scope
            if f"{scope}.{name}" in self.decls:
                return f"{scope}.{name}"
        imported = self._single_import(file, name)
        if imported is not None:
            return imported or None
        same_package = _qualify(file.package, name)
        if self.known(same_package):
            return same_package
        if name in JAVA_LANG:
            return None
        for imp in file.imports:
            if imp.wildcard and self.known(f"{imp.name}.{name}"):
                return f"{imp.name}.{name}"
        return None

    def _single_import(self, file: JavaFile, name: str) -> str | None:
        """The project type a single-type import of `name` names; "" for a library type (which
        hides any other type of that name); None when no import names it."""
        for imp in file.imports:
            if imp.wildcard or _simple(imp.name) != name:
                continue
            if self.known(imp.name):
                return imp.name
            if not imp.static:  # a static import of that name may be a method; keep looking
                return ""
        return None

    def _generated(self, imp: Import) -> bool:
        """A single-type import of a type in the project's packages that isn't in the source."""
        if imp.static or imp.wildcard or not self.prefix or imp.name in self.decls:
            return False
        parts = imp.name.split(".")
        if not imp.name.startswith(self.prefix + ".") or not parts[-1][:1].isupper():
            return False
        # A nested type of a source type that the parser missed is not generated code.
        return not any(".".join(parts[:size]) in self.decls for size in range(1, len(parts)))


# ---- the builder ---------------------------------------------------------------------------


@dataclass
class _Type:
    """A type from the source, with what the builder works out about it."""

    decl: TypeDecl
    file: JavaFile
    source_set: str
    role: str = "other"
    supers: list[tuple[str, str]] = field(default_factory=list)  # (EXTENDS|IMPLEMENTS, qname)
    methods: list[tuple[str, MethodDecl]] = field(default_factory=list)  # (method id, decl)

    @property
    def qname(self) -> str:
        return self.decl.qualified_name

    @property
    def id(self) -> str:
        return type_id(self.decl.qualified_name)


@dataclass
class _Call:
    """A call site and what it resolved to."""

    site: CallSite
    targets: list[str] = field(default_factory=list)  # method ids
    resolution: str | None = None
    receiver: str | None = None  # the type the method was looked up in
    created: str | None = None  # for "new" and X::new: the created type


class _Builder:
    def __init__(
        self,
        java_files: Sequence[JavaFile],
        specs: Sequence[ApiSpec],
        docs: Sequence[DocSection],
        config: Sequence[ConfigProperty] = (),
    ) -> None:
        self.index = TypeIndex(java_files)
        self.config = sorted(config, key=lambda c: (c.path, c.line, c.key))
        self.config_by_key: dict[str, list[str]] = defaultdict(list)  # relaxed key -> node ids
        self.files = sorted(java_files, key=lambda f: f.path)
        self.specs = sorted(specs, key=lambda s: s.path)
        self.docs = sorted(docs, key=lambda d: (d.path, d.start_line, d.anchor))
        self.types = {
            q: _Type(decl, jf, source_set(jf.path)) for q, (decl, jf) in self.index.decls.items()
        }
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}
        self.by_name: dict[str, list[tuple[_Type, str, MethodDecl]]] = defaultdict(list)
        self.overridden: dict[str, list[str]] = defaultdict(list)  # method id -> its supers
        self.calls: dict[str, list[_Call]] = {}  # method id -> its resolved call sites
        self.mocked: dict[str, set[str]] = defaultdict(set)  # test type -> mocked types
        self.spec_endpoints: list[Node] = []  # endpoints of own specs, sorted; set by _serves
        self.provider_endpoints: list[Node] = []  # endpoints of provider specs; set by _serves
        self._ancestor_cache: dict[str, list[str]] = {}
        self._name_only_cache: dict[tuple[str, str, int, bool], list[str]] = {}

    def build(self) -> Graph:
        self._java_nodes()
        self._spec_nodes()
        self._doc_nodes()
        self._injections()
        self._signature_uses()
        self._overrides()
        self._calls()
        self._serves()
        self._config_nodes()
        self._http_calls()
        self._tests()
        self._generated_from()
        self._references()
        self._mentions()
        nodes = sorted(self.nodes.values(), key=lambda n: n.id)
        edges = [e for e in self.edges.values() if {e.source, e.target} <= self.nodes.keys()]
        edges.sort(key=lambda e: (e.kind, e.source, e.target))
        return Graph(nodes=nodes, edges=edges)

    # -- primitives

    def _add_node(
        self,
        node_id: str,
        kind: str,
        name: str,
        path: str | None = None,
        start: int | None = None,
        end: int | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> Node:
        node = Node(node_id, kind, name, path, start, end, attrs or {})
        return self.nodes.setdefault(node_id, node)

    def _add_edge(
        self, kind: str, source: str, target: str, attrs: dict[str, Any] | None = None
    ) -> None:
        """First one wins: a later edge with the same kind, source and target is dropped."""
        self.edges.setdefault((kind, source, target), Edge(source, target, kind, attrs or {}))

    def _resolve(self, t: _Type, name: str) -> str | None:
        return self.index.resolve(name, t.file, t.qname)

    def _is_main_source(self, qname: str | None) -> bool:
        return qname in self.types and self.types[qname].source_set == "main"

    def _ancestors(self, qname: str) -> list[str]:
        """Supertypes, transitively, nearest first: source types and stubs."""
        if qname not in self._ancestor_cache:
            seen = [qname]
            for current in seen:  # grows while iterating: breadth first
                t = self.types.get(current)
                for _, sup in t.supers if t else ():
                    if sup not in seen:
                        seen.append(sup)
            self._ancestor_cache[qname] = seen[1:]
        return self._ancestor_cache[qname]

    # -- nodes, CONTAINS, EXTENDS, IMPLEMENTS

    def _java_nodes(self) -> None:
        for jf in self.files:
            attrs = {"language": "java", "source_set": source_set(jf.path), "package": jf.package}
            self._add_node(file_id(jf.path), FILE, _basename(jf.path), jf.path, attrs=attrs)
        for t in self.types.values():
            t.supers = self._supers(t)
            t.role = type_role(t.decl, t.source_set)
        for qname, role in self.index.stubs.items():
            self._add_node(type_id(qname), TYPE, _simple(qname), attrs=_stub_attrs(qname, role))
        for t in self.types.values():
            self._add_type(t)

    def _supers(self, t: _Type) -> list[tuple[str, str]]:
        found = []
        for kind, refs in ((EXTENDS, t.decl.extends), (IMPLEMENTS, t.decl.implements)):
            for ref in refs:  # resolved where the type is declared: its outer type's scope
                qname = self.index.resolve(ref.name, t.file, t.decl.outer)
                if qname and qname != t.qname:
                    found.append((kind, qname))
        return found

    def _add_type(self, t: _Type) -> None:
        decl = t.decl
        path, attrs = t.file.path, _type_attrs(t)
        self._add_node(t.id, TYPE, decl.name, path, decl.start_line, decl.end_line, attrs)
        parent = type_id(decl.outer) if decl.outer in self.types else file_id(t.file.path)
        self._add_edge(CONTAINS, parent, t.id)
        for kind, sup in t.supers:
            self._add_edge(kind, t.id, type_id(sup))
        for m in decl.methods:
            mid = method_id(t.qname, m.name, [p.type for p in m.parameters])
            if mid in self.nodes:  # two declarations with the same erasure: keep the first
                continue
            self._add_node(mid, METHOD, m.name, path, m.start_line, m.end_line, _method_attrs(m))
            self._add_edge(CONTAINS, t.id, mid)
            t.methods.append((mid, m))
            self.by_name[m.name].append((t, mid, m))

    def _spec_nodes(self) -> None:
        for spec in self.specs:
            fid = file_id(spec.path)
            attrs = {
                "language": "openapi", "source_set": source_set(spec.path), "title": spec.title,
                "version": spec.version, "base_path": spec.base_path,
                "spec_role": _role(spec),
            }  # fmt: skip
            self._add_node(fid, FILE, _basename(spec.path), spec.path, attrs=attrs)
            for op in spec.operations:
                eid = _spec_endpoint_id(spec, op)
                name = f"{op.method.upper()} {op.path}"
                attrs = _endpoint_attrs(spec, op)
                self._add_node(eid, ENDPOINT, name, spec.path, op.line, attrs=attrs)
                self._add_edge(CONTAINS, fid, eid)
            for schema in spec.schemas:
                sid = schema_id(spec.path, schema.name)
                attrs = {
                    "properties": dict(schema.properties), "required": list(schema.required),
                    "all_of": list(schema.all_of),
                }  # fmt: skip
                self._add_node(sid, SCHEMA, schema.name, spec.path, schema.line, attrs=attrs)
                self._add_edge(CONTAINS, fid, sid)

    def _config_nodes(self) -> None:
        for path in dict.fromkeys(c.path for c in self.config):
            attrs = {"language": "config", "source_set": source_set(path)}
            self._add_node(file_id(path), FILE, _basename(path), path, attrs=attrs)
        for c in self.config:
            cid = config_id(c.path, c.key)
            if cid in self.nodes:  # the same key in another YAML document (another profile)
                cid = f"{cid}@{c.profile or c.line}"
            attrs = {"key": c.key, "value": c.value, "profile": c.profile,
                     "source_set": source_set(c.path)}  # fmt: skip
            self._add_node(cid, CONFIG, c.key, c.path, c.line, c.line, attrs)
            self._add_edge(CONTAINS, file_id(c.path), cid)
            self.config_by_key[_relaxed(c.key)].append(cid)

    # -- HTTP_CALL, CALLS_HTTP, CONFIGURED_BY, TARGETS

    def _http_calls(self) -> None:
        main = [t for t in self.types.values() if t.source_set == "main"]
        by_module: dict[str, list[_Type]] = defaultdict(list)
        for t in main:  # each module is its own application: its own beans and config
            by_module[module_of(t.file.path)].append(t)
        envs = {
            module: client_env({t.file.path: t.file for t in ts}.values())
            for module, ts in by_module.items()
        }
        # Provider specs first; an own spec's endpoint this code doesn't serve may be another
        # system's spec kept outside a contracts/ folder. A served one is never a target.
        served = {target for kind, _, target in self.edges if kind == SERVES}
        unserved = [ep for ep in self.spec_endpoints if ep.id not in served]
        for t in main:
            for fact in find_http_calls(t.decl, envs[module_of(t.file.path)]):
                self._add_http_call(t, fact, self.provider_endpoints, unserved)

    def _add_http_call(
        self, t: _Type, fact: HttpCallFact, providers: list[Node], unserved: list[Node]
    ) -> None:
        m = fact.method
        mid = method_id(t.qname, m.name, [p.type for p in m.parameters])
        hid = http_call_id(mid, fact.line)
        attrs = _http_call_attrs(fact)
        for role, text in (("request", fact.request_body), ("response", fact.response)):
            found = self._types_in_text(t, text)
            attrs[f"{role}_type"] = type_id(found[0]) if found else None
            for qname in found:
                self._add_edge(USES, hid, type_id(qname), {"as": role})
        name = f"{fact.http_method or '?'} {fact.path or '?'}"
        self._add_node(hid, HTTP_CALL, name, t.file.path, fact.line, fact.line, attrs)
        self._add_edge(CALLS_HTTP, mid, hid, {"line": fact.line})
        keys = fact.property_keys
        if not keys and fact.client == "feign" and fact.service and not fact.base_url:
            keys = [f"spring.cloud.openfeign.client.config.{fact.service}.url",
                    f"feign.client.config.{fact.service}.url"]  # fmt: skip
        module = module_of(t.file.path)
        for key in keys:
            for cid in self.config_by_key.get(_relaxed(key), []):
                if module_of(self.nodes[cid].path or "") == module:
                    self._add_edge(CONFIGURED_BY, hid, cid, {"as": "base_url"})
        if fact.path:
            for candidates, match in ((providers, "provider_spec"), (unserved, "unserved_spec")):
                found = match_url(fact.path, fact.http_method, candidates)
                for eid in found:
                    self._add_edge(TARGETS, hid, eid, {"match": match})
                if found:
                    break

    def _types_in_text(self, t: _Type, text: str | None) -> list[str]:
        """Project types named in a type as written: "List<Order>" -> [the Order type]."""
        found = []
        for name in _TYPE_NAME.findall(text or ""):
            qname = self._resolve(t, name)
            if qname and qname not in found:
                found.append(qname)
        return found

    def _doc_nodes(self) -> None:
        for path in sorted({s.path for s in self.docs}):
            attrs = {"language": "markdown", "source_set": source_set(path)}
            self._add_node(file_id(path), FILE, _basename(path), path, attrs=attrs)
        for s in self.docs:
            did = doc_id(s.path, s.anchor)
            attrs = {"level": s.level, "breadcrumbs": list(s.breadcrumbs)}
            name = s.heading or _basename(s.path)
            self._add_node(did, DOC, name, s.path, s.start_line, s.end_line, attrs)
            self._add_edge(CONTAINS, file_id(s.path), did)

    # -- INJECTS

    def _injections(self) -> None:
        for t in self.types.values():
            values = t.role == "entity" or t.decl.kind in ("record", "enum")
            for m in t.decl.methods:
                if m.kind == "constructor" and not values:  # an entity's arguments are data
                    self._inject(t, m.parameters, "constructor")
                elif m.kind == "method" and _names(m.annotations) & _INJECTED_SETTER:
                    self._inject(t, m.parameters, "setter")
            for f in t.decl.fields:
                if _names(f.annotations) & _INJECTED_FIELD:
                    self._inject_type(t, f.type, "field", f.name)

    def _inject(self, t: _Type, params: list[Parameter], via: str) -> None:
        for p in params:
            self._inject_type(t, p.type, via, p.name)

    def _inject_type(self, t: _Type, ref: TypeRef, via: str, name: str) -> None:
        for qname in self._dependency_types(t, ref):
            if qname != t.qname:
                self._add_edge(INJECTS, t.id, type_id(qname), {"via": via, "name": name})

    def _dependency_types(self, t: _Type, ref: TypeRef) -> list[str]:
        qname = None if ref.name == "?" else self._resolve(t, ref.name)
        if qname:
            return [qname]
        if ref.name != "?" and _simple(ref.name) not in _CONTAINERS:
            return []
        args = ref.args[-1:] if _simple(ref.name) == "Map" else ref.args
        return [q for arg in args for q in self._dependency_types(t, arg)]

    # -- USES (signature), OVERRIDES

    def _signature_uses(self) -> None:
        for t in self.types.values():
            for mid, m in t.methods:
                for p in m.parameters:
                    for qname in self._types_in(t, p.type):
                        self._add_edge(USES, mid, type_id(qname), {"as": "parameter"})
                if m.return_type is not None:
                    for qname in self._types_in(t, m.return_type):
                        self._add_edge(USES, mid, type_id(qname), {"as": "return"})

    def _types_in(self, t: _Type, ref: TypeRef) -> list[str]:
        """The project types a type reference names, generic arguments included."""
        own = None if ref.name == "?" else self._resolve(t, ref.name)
        return ([own] if own else []) + [q for arg in ref.args for q in self._types_in(t, arg)]

    def _overrides(self) -> None:
        for t in self.types.values():
            for mid, m in t.methods:
                if _overridable(m):
                    for sup in self._ancestors(t.qname):
                        self._override(t, mid, m, self.types.get(sup))

    def _override(self, t: _Type, mid: str, m: MethodDecl, sup: _Type | None) -> None:
        for sup_mid, sup_m in sup.methods if sup else ():
            if sup_m.name == m.name and _overridable(sup_m) and self._same_params(t, m, sup, sup_m):
                self._add_edge(OVERRIDES, mid, sup_mid)
                self.overridden[mid].append(sup_mid)

    def _same_params(self, t: _Type, m: MethodDecl, sup: _Type, sup_m: MethodDecl) -> bool:
        """Same arity and erased parameter types; a type variable matches any type."""
        if len(m.parameters) != len(sup_m.parameters):
            return False
        mine, theirs = self._type_vars(t, m), self._type_vars(sup, sup_m)
        return all(
            a.type.name in mine or b.type.name in theirs or erase(a.type) == erase(b.type)
            for a, b in zip(m.parameters, sup_m.parameters)
        )

    def _type_vars(self, t: _Type, m: MethodDecl) -> set[str]:
        names = {
            p.split()[0]
            for q in self.index.enclosing_chain(t.qname)
            for p in self.types[q].decl.type_parameters
            if p.split()
        }
        match = _METHOD_TYPE_PARAMS.match(m.signature)
        if match:
            names |= {p.split()[0] for p in match.group(1).split(",") if p.split()}
        return names

    # -- CALLS, USES (new)

    def _calls(self) -> None:
        for t in self.types.values():
            for mid, m in t.methods:
                self.calls[mid] = [self._resolve_call(t, c) for c in m.calls]
                for call in self.calls[mid]:
                    if call.created:
                        self._add_edge(USES, mid, type_id(call.created), {"as": "new"})
                    for target in call.targets:
                        attrs = {"line": call.site.line, "resolution": call.resolution}
                        self._add_edge(CALLS, mid, target, attrs)

    def _resolve_call(self, t: _Type, c: CallSite) -> _Call:
        if c.kind == "new" or (c.kind == "method_ref" and c.name == "<init>"):
            created = self._resolve(t, c.receiver_type or c.name)
            return _Call(c, self._constructors(created, c), "receiver_type", created, created)
        if c.name == "<init>":  # this(...) or super(...) in a constructor
            owner = t.qname if c.receiver == "this" else self._superclass(t)
            return _Call(c, self._constructors(owner, c), "same_type", owner)
        if c.receiver in (None, "this"):
            return self._unqualified(t, c)
        if c.receiver == "super":
            owner = self._superclass(t)
            return _Call(c, self._lookup(owner, c), "same_type", owner)
        if c.receiver_type:
            owner = self._resolve(t, c.receiver_type)
            return _Call(c, self._lookup(owner, c), "receiver_type", owner)
        return _Call(c, self._name_only(t, c), "name_only")

    def _unqualified(self, t: _Type, c: CallSite) -> _Call:
        """bar() or this.bar(): the enclosing type and its supertypes, then the outer types;
        then static imports."""
        for scope in self.index.enclosing_chain(t.qname):
            found = self._lookup(scope, c)
            if found:
                return _Call(c, found, "same_type", scope)
        for owner in self._static_import_owners(t, c.name) if c.receiver is None else ():
            found = self._lookup(owner, c)
            if found:
                return _Call(c, found, "receiver_type", owner)
        return _Call(c)

    def _static_import_owners(self, t: _Type, name: str) -> Iterator[str]:
        for imp in t.file.imports:
            if not imp.static:
                continue
            owner = imp.name if imp.wildcard else imp.name.rpartition(".")[0]
            if (imp.wildcard or _simple(imp.name) == name) and owner in self.types:
                yield owner

    def _superclass(self, t: _Type) -> str | None:
        if t.decl.kind != "class":
            return None
        return next((qname for kind, qname in t.supers if kind == EXTENDS), None)

    def _lookup(self, owner: str | None, c: CallSite) -> list[str]:
        """The methods that fit the call in `owner`, else in its nearest supertype with some."""
        for qname in (owner, *self._ancestors(owner)) if owner else ():
            t = self.types.get(qname)
            found = [
                mid
                for mid, m in (t.methods if t else ())
                if m.name == c.name and m.kind == "method" and _fits(m, c)
            ]
            if found:
                return found
        return []

    def _constructors(self, owner: str | None, c: CallSite) -> list[str]:
        t = self.types.get(owner) if owner else None
        methods = t.methods if t else []
        return [mid for mid, m in methods if m.kind == "constructor" and _fits(m, c)]

    def _name_only(self, t: _Type, c: CallSite) -> list[str]:
        """The one project method with this name and arity (main code only sees main methods).
        Overrides of one method count once, as that method: an interface and its implementations
        are one candidate."""
        if c.name in _NAME_ONLY_SKIP:
            return []
        key = (c.name, c.kind, c.arg_count, t.source_set == "main")
        if key not in self._name_only_cache:
            found = [
                mid
                for owner, mid, m in self.by_name.get(c.name, ())
                if m.kind == "method" and _fits(m, c)
                and (t.source_set != "main" or owner.source_set == "main")
            ]
            candidates = set(found)
            roots = [mid for mid in found if candidates.isdisjoint(self.overridden.get(mid, ()))]
            self._name_only_cache[key] = roots if len(roots) == 1 else []
        return self._name_only_cache[key]

    # -- SERVES

    def _serves(self) -> None:
        operations: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
        for spec in self.specs:
            if _role(spec) != "own":
                continue  # another system's operations: this code calls them, never serves them
            for op in spec.operations:
                if op.operation_id:  # openapi-generator puts untagged operations in DefaultApi
                    eid = endpoint_id(op.method, op.path)
                    operations[op.operation_id].append((eid, op.tags or ["default"]))
        all_tags = {tag for ops in operations.values() for _, tags in ops for tag in tags}
        tag_apis = {_api_name(tag) for tag in all_tags}
        spec_endpoints = [n for n in self._endpoints() if "openapi" in n.attrs["sources"]]
        self.spec_endpoints = [n for n in spec_endpoints if n.attrs["spec_role"] == "own"]
        self.provider_endpoints = [n for n in spec_endpoints if n.attrs["spec_role"] == "provider"]
        # A Feign client's mapping annotations describe the calls it makes, not routes it serves.
        serving = [
            t for t in self.types.values()
            if t.source_set != "test" and not _annotation(t.decl.annotations, "FeignClient")
        ]  # fmt: skip
        for t in serving:
            if t.role == "controller" or self._api_stubs(t):
                self._serve_by_operation_id(t, operations, tag_apis)
        for t in serving:
            for mid, m in t.methods:
                for verb, path in self._routes(t, m):
                    self._serve_by_annotation(t, mid, m, verb, path)

    def _endpoints(self) -> list[Node]:
        return sorted((n for n in self.nodes.values() if n.kind == ENDPOINT), key=lambda n: n.id)

    def _api_stubs(self, t: _Type) -> list[str]:
        return [q for q in self._ancestors(t.qname) if q in self.index.stubs and q.endswith("Api")]

    def _serve_by_operation_id(
        self, t: _Type, operations: dict[str, list[tuple[str, list[str]]]], tag_apis: set[str]
    ) -> None:
        """Methods named like an operationId. When the controller implements interfaces that
        openapi-generator named after tags (OwnersApi for tag "owners"), only the operations of
        those tags count, so a helper that happens to share another tag's operationId doesn't."""
        prefixes = self._class_paths(t)
        apis = {_simple(q) for q in self._api_stubs(t)} & tag_apis
        for mid, m in t.methods:
            if m.kind != "method":
                continue
            for eid in _pick_operations(operations.get(m.name, []), apis):
                self._add_edge(SERVES, mid, eid, {"via": "operation_id"})
                attrs = self.nodes[eid].attrs
                if attrs["served_path"] is None and prefixes:
                    attrs["served_path"] = join_paths(prefixes[0], attrs["spec_path"])

    def _class_paths(self, t: _Type) -> list[str]:
        """The paths of the class-level @RequestMapping of the type or its nearest supertype
        with one: [""] if none, [] if they aren't literals."""
        for qname in (t.qname, *self._ancestors(t.qname)):
            sup = self.types.get(qname)
            mapping = _annotation(sup.decl.annotations, "RequestMapping") if sup else None
            if mapping is not None:
                return _mapping_paths(mapping)
        return [""]

    def _routes(self, t: _Type, m: MethodDecl) -> list[tuple[str, str]]:
        """(HTTP method, full path) for each mapping annotation of a method."""
        routes = []
        for a in m.annotations:
            if a.name not in _MAPPINGS:
                continue
            verb = _MAPPINGS[a.name]
            verbs = [verb] if verb else _request_methods(a)
            for prefix in self._class_paths(t):
                for path in _mapping_paths(a):
                    routes += [(v, join_paths(prefix, path)) for v in verbs]
        return list(dict.fromkeys(routes))

    def _serve_by_annotation(self, t: _Type, mid: str, m: MethodDecl, verb: str, path: str) -> None:
        matches = self._spec_endpoints_for(verb, path)
        for eid, served in matches:
            self._add_edge(SERVES, mid, eid, {"via": "annotation"})
            attrs = self.nodes[eid].attrs
            if "code" not in attrs["sources"]:
                attrs["sources"].append("code")
            if attrs["served_path"] is None:
                attrs["served_path"] = served
        if not matches:
            eid = endpoint_id(verb, path)
            name, attrs = f"{verb} {path}", _code_endpoint_attrs(verb, path)
            self._add_node(eid, ENDPOINT, name, t.file.path, m.start_line, m.end_line, attrs)
            self._add_edge(SERVES, mid, eid, {"via": "annotation"})

    def _spec_endpoints_for(self, verb: str, path: str) -> list[tuple[str, str]]:
        """The spec endpoints a mapped route serves, each with the path it's served at: the
        best match by served_path, else by the spec path after a literal prefix ("/api")."""
        segments = _segments(path)
        scored = []
        for ep in self.spec_endpoints:
            attrs = ep.attrs
            if not _methods_match(verb, attrs["http_method"]):
                continue
            served = attrs["served_path"]
            score = _route_match(segments, _segments(served)) if served else None
            if score is not None:
                scored.append(((2, score), ep.id, served))
                continue
            spec = _segments(attrs["spec_path"])
            score = _suffix_match(segments, spec, _route_match)
            if score is not None:
                prefix = segments[: len(segments) - len(spec)]
                scored.append(((1, score), ep.id, join_paths(*prefix, attrs["spec_path"])))
        best = max((s for s, _, _ in scored), default=None)
        return [(eid, served) for s, eid, served in scored if s == best]

    # -- TESTS, MOCKS

    def _tests(self) -> None:
        for t in self.types.values():
            if t.role in ("test", "test_support"):
                for f in t.decl.fields:
                    mock = self._mock_target(t, f)
                    if mock:
                        self._add_edge(MOCKS, t.id, type_id(mock[0]), {"via": mock[1]})
                        self.mocked[t.qname].add(mock[0])
        main_names: dict[str, list[str]] = defaultdict(list)
        for t in self.types.values():
            if t.source_set == "main":
                main_names[_simple(t.qname)].append(t.qname)
        endpoints = self._endpoints()
        for t in self.types.values():
            if t.role == "test":
                self._test_type_links(t, main_names)
                self._test_method_links(t, endpoints)

    def _test_type_links(self, t: _Type, main_names: dict[str, list[str]]) -> None:
        subject = self._subject_by_name(t, main_names)
        if subject:
            self._add_edge(TESTS, t.id, type_id(subject), {"reason": "name"})
        for qname in self._annotated_subjects(t):
            self._add_edge(TESTS, t.id, type_id(qname), {"reason": "annotation"})
        for f in t.decl.fields:
            qname = self._tested_field(t, f)
            if qname:
                self._add_edge(TESTS, t.id, type_id(qname), {"reason": "field"})

    def _test_method_links(self, t: _Type, endpoints: list[Node]) -> None:
        mocked = set().union(*(self.mocked[q] for q in (t.qname, *self._ancestors(t.qname))))
        for mid, m in t.methods:
            if _is_test(m):
                for call in self.calls.get(mid, []):
                    self._test_call(mid, call, mocked, endpoints)

    def _test_call(self, mid: str, call: _Call, mocked: set[str], endpoints: list[Node]) -> None:
        c = call.site
        if call.created:
            if self._is_main_source(call.created):
                self._add_edge(TESTS, mid, type_id(call.created), {"reason": "new", "line": c.line})
            return
        if c.kind == "call" and c.name in _REQUEST_BUILDERS and c.string_args:
            for eid in match_url(c.string_args[0], _REQUEST_BUILDERS[c.name], endpoints):
                self._add_edge(TESTS, mid, eid, {"reason": "http", "line": c.line})
        if (
            call.resolution == "receiver_type"
            and self._is_main_source(call.receiver)
            and call.receiver not in mocked
        ):
            for target in call.targets:
                self._add_edge(TESTS, mid, target, {"reason": "call", "line": c.line})

    def _subject_by_name(self, t: _Type, main_names: dict[str, list[str]]) -> str | None:
        """OwnerServiceTests -> OwnerService in the same package, else the one main type of
        that name in any package. A nested (@Nested) test class goes by its outer class."""
        name = _strip_test_affixes(t.decl.name.split(".")[0])
        same_package = _qualify(t.file.package, name)
        if self._is_main_source(same_package):
            return same_package
        found = main_names.get(name, [])
        return found[0] if len(found) == 1 else None

    def _annotated_subjects(self, t: _Type) -> list[str]:
        """@WebMvcTest(X.class), @WebMvcTest(controllers = {X.class, Y.class})."""
        found = []
        for a in t.decl.annotations:
            if a.name not in _SUBJECT_ANNOTATIONS:
                continue
            texts = _strings(a.arguments.get("value")) + _strings(a.arguments.get("controllers"))
            for name in (n for text in texts for n in _CLASS_LITERAL.findall(text)):
                qname = self._resolve(t, name)
                if self._is_main_source(qname):
                    found.append(qname)
        return found

    def _mock_target(self, t: _Type, f: FieldDecl) -> tuple[str, str] | None:
        """The type a field mocks, and how: @Mock, @MockitoBean... or `= mock(X.class)`."""
        via = next((a.name for a in f.annotations if a.name in _MOCK_FIELD), None)
        name = f.type.name
        if via is None:
            match = _MOCK_CALL.match(f.initializer or "")
            if not match:
                return None
            via, name = "mock()", match.group(1)
        qname = self._resolve(t, name)
        return (qname, via) if qname else None

    def _tested_field(self, t: _Type, f: FieldDecl) -> str | None:
        """The main type an injected (@Autowired, @InjectMocks) or `new`-initialised field holds."""
        names = _names(f.annotations)
        if names & _MOCK_FIELD or _MOCK_CALL.match(f.initializer or ""):
            return None
        if names & _TESTED_FIELD:
            name = f.type.name
        else:
            match = _NEW_CALL.match(f.initializer or "")
            if not match:
                return None
            name = match.group(1)
        qname = self._resolve(t, name)
        return qname if self._is_main_source(qname) else None

    # -- GENERATED_FROM, REFERENCES

    def _generated_from(self) -> None:
        schemas: dict[str, list[str]] = defaultdict(list)
        for spec in self.specs:
            for schema in spec.schemas:
                schemas[schema.name].append(schema_id(spec.path, schema.name))
        for qname, role in self.index.stubs.items():
            if role == "dto":
                simple = _simple(qname)
                for sid in schemas.get(simple[: -len("Dto")]) or schemas.get(simple, []):
                    self._add_edge(GENERATED_FROM, type_id(qname), sid)

    def _references(self) -> None:
        for spec in self.specs:
            names = {s.name for s in spec.schemas}
            for op in spec.operations:
                eid = _spec_endpoint_id(spec, op)
                for name in _schema_refs(op.request_body, names):
                    self._add_edge(REFERENCES, eid, schema_id(spec.path, name), {"as": "request"})
                for status, value in op.responses.items():
                    for name in _schema_refs(value, names):
                        attrs = {"as": "response", "status": status}
                        self._add_edge(REFERENCES, eid, schema_id(spec.path, name), attrs)
            for schema in spec.schemas:
                sid = schema_id(spec.path, schema.name)
                for name in (n for n in schema.all_of if n in names):
                    self._add_edge(REFERENCES, sid, schema_id(spec.path, name), {"as": "all_of"})
                for prop, short in schema.properties.items():
                    for name in _schema_refs(short, names):
                        attrs = {"as": "property", "property": prop}
                        self._add_edge(REFERENCES, sid, schema_id(spec.path, name), attrs)

    # -- MENTIONS

    def _mentions(self) -> None:
        type_names: dict[str, list[str]] = defaultdict(list)
        for t in self.types.values():
            if t.source_set == "main":
                type_names[_simple(t.qname)].append(t.id)
        for qname in self.index.stubs:
            type_names[_simple(qname)].append(type_id(qname))
        names = {n: ids for n, ids in type_names.items() if _mentionable(n)}
        paths: dict[str, list[str]] = defaultdict(list)  # normalised path -> endpoint ids
        for ep in self._endpoints():
            for value in _endpoint_paths(ep):
                paths[_PATH_PARAM.sub("{}", value)].append(ep.id)
        for section in self.docs:
            found: dict[str, int] = {}  # target -> first position in the text
            for match in _IDENTIFIER.finditer(section.text):  # whole words only
                for target in names.get(match.group(0), ()):
                    found.setdefault(target, match.start())
            text = _PATH_PARAM.sub("{}", section.text)
            for match in _PATH_TOKEN.finditer(text):
                targets = paths.get(match.group(0).rstrip("./"), ())
                verbs = _line_methods(text, match.start()) if targets else set()  # | GET | /x |
                for target in targets:
                    method = self.nodes[target].attrs["http_method"]
                    if not verbs or any(_methods_match(v, method) for v in verbs):
                        found.setdefault(target, match.start())
            ranked = sorted(found.items(), key=lambda item: (item[1], item[0]))[:MAX_MENTIONS]
            for target, _ in ranked:
                self._add_edge(MENTIONS, doc_id(section.path, section.anchor), target)


# ---- attrs ---------------------------------------------------------------------------------


def _type_attrs(t: _Type) -> dict[str, Any]:
    d = t.decl
    return {
        "type_kind": d.kind,
        "qualified_name": d.qualified_name,
        "package": t.file.package,
        "source_set": t.source_set,
        "role": t.role,
        "annotations": [a.name for a in d.annotations],
        "profiles": _annotation_strings(d.annotations, "Profile", ("value",)),
        "active_profiles": _annotation_strings(
            d.annotations, "ActiveProfiles", ("value", "profiles")
        ),
        "modifiers": list(d.modifiers),
        "type_parameters": list(d.type_parameters),
        "extends": [r.text for r in d.extends],
        "implements": [r.text for r in d.implements],
        "fields": [_field_attrs(f) for f in d.fields],
        "record_components": [{"name": p.name, "type": p.type.text} for p in d.record_components],
        "is_abstract": "abstract" in d.modifiers,
        "external": False,
        "generated": False,
        "javadoc": d.javadoc,
        "outer": d.outer,
    }


def _stub_attrs(qname: str, role: str) -> dict[str, Any]:
    """A generated type: only its name is known. Same keys as a source type."""
    parts = qname.split(".")
    package = ".".join(p for p in parts[: next(i for i, p in enumerate(parts) if p[:1].isupper())])
    return {
        "type_kind": "interface" if role == "api_interface" else "class",
        "qualified_name": qname,
        "package": package,
        "source_set": "main",
        "role": role,
        "annotations": [],
        "profiles": [],
        "active_profiles": [],
        "modifiers": [],
        "type_parameters": [],
        "extends": [],
        "implements": [],
        "fields": [],
        "record_components": [],
        "is_abstract": False,
        "external": True,
        "generated": True,
        "javadoc": None,
        "outer": None,
    }


def _field_attrs(f: FieldDecl) -> dict[str, Any]:
    return {"name": f.name, "type": f.type.text, "annotations": [a.name for a in f.annotations]}


def _method_attrs(m: MethodDecl) -> dict[str, Any]:
    return {
        "signature": m.signature,
        "method_kind": m.kind,
        "modifiers": list(m.modifiers),
        "annotations": [a.name for a in m.annotations],
        "return_type": m.return_type.text if m.return_type else None,
        "parameters": [
            {"name": p.name, "type": p.type.text, "annotations": [a.name for a in p.annotations]}
            for p in m.parameters
        ],
        "is_test": _is_test(m),
        "has_body": m.has_body,
        "javadoc": m.javadoc,
    }


def _endpoint_attrs(spec: ApiSpec, op: ApiOperation) -> dict[str, Any]:
    return {
        "http_method": op.method.upper(),
        "spec_path": op.path,
        "operation_id": op.operation_id,
        "summary": op.summary,
        "description": op.description,
        "tags": list(op.tags),
        "parameters": to_dict(list(op.parameters)),
        "request_body": op.request_body,
        "responses": dict(op.responses),
        "url_path": join_paths(spec.base_path, op.path),
        "served_path": None,  # set by SERVES: controller prefix + spec path
        "spec_role": _role(spec),
        "sources": ["openapi"],
    }


def spec_role(path: str) -> str:
    """"provider" for another system's spec checked into this repo (a contracts/ or clients/
    folder...), else "own": the API this system serves."""
    return "provider" if _PROVIDER_DIR.search("/" + path.lower()) else "own"


def _role(spec: ApiSpec) -> str:
    return spec.role or spec_role(spec.path)


def _spec_endpoint_id(spec: ApiSpec, op: ApiOperation) -> str:
    """A provider spec's endpoints carry the spec path, so a BFF serving POST /orders and
    calling its provider's POST /orders gets two nodes."""
    eid = endpoint_id(op.method, op.path)
    return f"{eid}@{spec.path}" if _role(spec) == "provider" else eid


def _http_call_attrs(fact: HttpCallFact) -> dict[str, Any]:
    return {
        "client": fact.client,
        "http_method": fact.http_method,
        "path": fact.path,
        "query": list(fact.query),
        "headers": list(fact.headers),
        "request_body": fact.request_body,
        "response": fact.response,
        "base_url": fact.base_url,
        "base_url_via": fact.base_url_via,
        "base_url_properties": fact.property_keys,
        "service": fact.service,
        "resolution": fact.resolution,
        "notes": list(fact.notes),
    }


def _relaxed(key: str) -> str:
    """Spring's relaxed binding: base-url, baseUrl and base_url are the same property."""
    return re.sub(r"[-_]", "", key).lower()


def _code_endpoint_attrs(verb: str, path: str) -> dict[str, Any]:
    """An endpoint found only in code: its mapped path is the only one known."""
    return {
        "http_method": verb,
        "spec_path": None,
        "operation_id": None,
        "summary": None,
        "description": None,
        "tags": [],
        "parameters": [],
        "request_body": None,
        "responses": {},
        "url_path": None,
        "served_path": join_paths(path),
        "sources": ["code"],
    }


# ---- helpers -------------------------------------------------------------------------------


def _simple(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _qualify(package: str, name: str) -> str:
    return f"{package}.{name}" if package else name


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _names(annotations: list[Annotation]) -> set[str]:
    return {a.name for a in annotations}


def _annotation(annotations: list[Annotation], name: str) -> Annotation | None:
    return next((a for a in annotations if a.name == name), None)


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _annotation_strings(
    annotations: list[Annotation], name: str, keys: tuple[str, ...]
) -> list[str]:
    """The string values of an annotation's arguments: @Profile({"jdbc", "h2"}) -> jdbc, h2."""
    return [
        s for a in annotations if a.name == name for k in keys for s in _strings(a.arguments.get(k))
    ]


def _is_test(m: MethodDecl) -> bool:
    return bool(_names(m.annotations) & TEST_ANNOTATIONS)


def _has_tests(decl: TypeDecl) -> bool:
    return any(_is_test(m) for m in decl.methods)


def _overridable(m: MethodDecl) -> bool:
    return m.kind == "method" and not {"static", "private"} & set(m.modifiers)


def _fits(m: MethodDecl, c: CallSite) -> bool:
    """A method reference fits by name; a call by arity, varargs included."""
    if c.kind == "method_ref":
        return True
    params = m.parameters
    varargs = bool(params) and params[-1].type.text.endswith("...")
    return c.arg_count == len(params) or (varargs and c.arg_count >= len(params) - 1)


def _strip_test_affixes(name: str) -> str:
    """AbstractClinicServiceTests -> ClinicService, OwnerControllerIT -> OwnerController."""
    for suffix in _TEST_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    for prefix in _TEST_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix) and name[len(prefix)].isupper():
            name = name[len(prefix) :]
    return name


def _pick_operations(candidates: list[tuple[str, list[str]]], apis: set[str]) -> list[str]:
    """The endpoints of an operationId; only those of the given tag interfaces, if any."""
    if apis:
        return [eid for eid, tags in candidates if any(_api_name(tag) in apis for tag in tags)]
    return [eid for eid, _ in candidates]


def _api_name(tag: str) -> str:
    """The interface openapi-generator makes for a tag: "owner-v2" -> "OwnerV2Api"."""
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^0-9A-Za-z]+", tag) if p) + "Api"


def _mapping_paths(a: Annotation) -> list[str]:
    """The literal paths of a mapping annotation (value or path), "${name:default}" read as its
    default; [""] when it has none. Constants, expressions and placeholders without a default
    (ApiPaths.OWNERS, "${base}") have no known value, so they give no path at all."""
    values = _strings(a.arguments.get("value")) + _strings(a.arguments.get("path"))
    if not values:
        return [""]
    known = [
        v for v in values
        if not ('"' in v or "+" in v or _CONSTANT_REF.fullmatch(v) or _BARE_PLACEHOLDER.search(v))
    ]  # fmt: skip
    return [_PLACEHOLDER.sub(lambda m: m.group(1) or "", v) for v in known]


def _request_methods(a: Annotation) -> list[str]:
    found = [m for text in _strings(a.arguments.get("method")) for m in _HTTP_METHOD.findall(text)]
    return list(dict.fromkeys(found)) or [ANY_METHOD]


def _methods_match(a: str | None, b: str | None) -> bool:
    return a is None or b is None or ANY_METHOD in (a, b) or a == b


def _segments(path: str | None) -> list[str]:
    return [s for s in (path or "").split("/") if s]


def _is_variable(segment: str) -> bool:
    return "{" in segment  # {id}, {id:\d+}, or {} for a concatenated value


def _route_match(route: list[str], other: list[str]) -> int | None:
    """Two route templates: variables align with variables, literals are equal. Returns the
    number of literal segments, or None when they differ."""
    if len(route) != len(other):
        return None
    score = 0
    for a, b in zip(route, other):
        if _is_variable(a) != _is_variable(b) or (not _is_variable(a) and a != b):
            return None
        score += not _is_variable(a)
    return score


def _url_match(url: list[str], route: list[str]) -> int | None:
    """A URL a test calls against a route template: {params} on either side match any segment.
    Returns the number of literal segments in common, or None; at least one must match."""
    if len(url) != len(route):
        return None
    score = 0
    for a, b in zip(url, route):
        if _is_variable(a) or _is_variable(b):
            continue
        if a != b:
            return None
        score += 1
    return score if score or not route else None


def _suffix_match(
    path: list[str], tail: list[str], match: Callable[[list[str], list[str]], int | None]
) -> int | None:
    """`path` ends with `tail` after a literal prefix (a base path such as /api)."""
    if not tail or len(tail) > len(path):
        return None
    prefix = path[: len(path) - len(tail)]
    if any(_is_variable(s) for s in prefix):
        return None
    return match(path[len(prefix) :], tail)


def match_url(url: str, http_method: str | None, endpoints: Sequence[Node]) -> list[str]:
    """The endpoints a URL in a test calls: matched against served_path, then url_path, then
    the spec path as a suffix; the best by literal segments in common (ties: all). The query
    string is ignored; {} (a concatenated value) and {param} match any segment."""
    path = _url_path(url)
    if path is None:
        return []
    url_segments = _segments(path)
    for attr, suffix in (("served_path", False), ("url_path", False), ("spec_path", True)):
        scored = []
        for ep in endpoints:
            value = ep.attrs.get(attr)
            if not value or not _methods_match(http_method, ep.attrs.get("http_method")):
                continue
            route = _segments(value)
            if suffix:
                score = _suffix_match(url_segments, route, _url_match)
            else:
                score = _url_match(url_segments, route)
            if score is not None:
                scored.append((score, ep.id))
        if scored:
            best = max(score for score, _ in scored)
            return [eid for score, eid in scored if score == best]
    return []


def _url_path(url: str) -> str | None:
    """The path of a URL as a test writes it: no scheme and host, no query string or fragment."""
    url = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", url.strip())
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    return path if path.startswith("/") else None


def _schema_refs(short: str | None, names: set[str]) -> list[str]:
    """Schema names in an OpenAPI short form ("Owner", "array<Owner>"); a response
    description ("Not modified.") names none."""
    if not short or not _SHORT_FORM.fullmatch(short.strip()):
        return []
    return [n for n in dict.fromkeys(_SHORT_FORM_NAME.findall(short)) if n in names]


def _mentionable(name: str) -> bool:
    """Distinctive enough to find in prose: ClinicService yes, Owner no."""
    return len(name) >= 4 and any(ch.isupper() for ch in name[1:])


def _endpoint_paths(ep: Node) -> list[str]:
    values = {ep.attrs.get(k) for k in ("served_path", "url_path", "spec_path")}
    return sorted(v for v in values if v and _segments(v))


def _line_methods(text: str, position: int) -> set[str]:
    """The HTTP methods named on the line of the text at `position`."""
    end = text.find("\n", position)
    line = text[text.rfind("\n", 0, position) + 1 : end if end != -1 else len(text)]
    return set(_HTTP_METHOD.findall(line))


