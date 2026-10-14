"""Answers questions over an index: the test context for a symbol, search, neighbours.

The graph is loaded once into adjacency maps (by edge kind, both directions), so an answer is
mostly dict lookups. Chunk texts (sources, outlines, doc sections) come from LanceDB in one
query per answer, and the embedding model is loaded only when vector search needs it (`search`
and the related docs of a test context). Answers are JSON-ready dicts, bounded so they fit an
agent's context: long sources are cut and lists capped, and every cut says what was left out.
"""

from __future__ import annotations

import difflib
import re
import shlex
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .embeddings import Embedder, FastEmbedEmbedder, HashEmbedder, default_embedder, tokenize
from .graph import match_url
from .models import (
    CALLS,
    CONTAINS,
    DOC,
    ENDPOINT,
    EXTENDS,
    FILE,
    GENERATED_FROM,
    IMPLEMENTS,
    INJECTS,
    MENTIONS,
    METHOD,
    MOCKS,
    OVERRIDES,
    REFERENCES,
    SCHEMA,
    SERVES,
    TESTS,
    TYPE,
    USES,
    Chunk,
    Edge,
    Node,
    to_dict,
)
from .store import IndexStore

CHUNK_KINDS = (METHOD, TYPE, ENDPOINT, SCHEMA, DOC)  # the kinds `search` can return
EDGE_KINDS = (CONTAINS, EXTENDS, IMPLEMENTS, INJECTS, CALLS, OVERRIDES, USES, SERVES, TESTS, MOCKS,
              GENERATED_FROM, REFERENCES, MENTIONS)  # fmt: skip

# ---- bounds: what one answer may hold --------------------------------------------------------

MAX_SOURCE_CHARS = 4000  # the target's source, an outline
MAX_TYPE_SOURCES_CHARS = 6000  # method sources shown for a class target, all together
MAX_HELPER_CHARS = 1500  # a private helper the target calls
MAX_SETUP_CHARS = 2000  # a test class's @BeforeEach method
MAX_TEST_CHARS = 2500  # one example test method
MAX_DOC_CHARS = 1200  # one doc section
MAX_SHOW_CHARS = 12000  # get_node's text
MAX_OUTLINE_METHODS = 8  # a bigger enclosing class shows only what the target needs
MAX_DEPENDENCIES = 12
MAX_CALLS = 10  # methods listed per dependency
MAX_COLLABORATORS = 15
MAX_HELPERS = 3  # same-class helpers shown with their source
MAX_DATA_TYPES = 10
MAX_FIELDS = 25
MAX_ENDPOINTS = 10
MAX_TESTED_BY = 8
MAX_SUBCLASSES = 8
MAX_IMPLEMENTATIONS = 8
MAX_CANDIDATES = 10
MAX_SCOPE_METHODS = 150  # test methods scanned for conventions
MAX_FIND_TESTS = 20  # test classes (and methods per class) listed by find_tests
MAX_SIMILAR_TESTS = 5  # unlinked test methods find_tests lists for their names alone

# How strongly a TESTS edge into each kind of node ties a test class to the target.
W_DIRECT = 5  # the target method itself, or an endpoint it serves
W_OVERRIDDEN = 4  # a method the target overrides or implements
W_TYPE = 3  # the target's class
W_SUPER = 2  # a supertype or interface of the target's class
W_SIBLING = 1  # another method of the target's class

_ID_PREFIXES = ("type:", "method:", "endpoint:", "schema:", "doc:")  # a file path means its types
_FILE_SUFFIXES = (".java", ".yml", ".yaml", ".json", ".md", ".markdown")
_PART = re.compile(r"#part(\d+)$")
_HTTP = re.compile(r"(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|TRACE)\s+(\S+)", re.IGNORECASE)
_CODE_NAME = re.compile(r"[\w$.#<>]+")
_GENERIC = re.compile(r"<[^<>]*>")
_ANNOTATION = re.compile(r"@[\w.$]+(?:\([^)]*\))?")
_ROLE_RANK = {
    role: rank
    for rank, role in enumerate(
        ("controller", "service", "repository", "mapper", "controller_advice", "config", "other",
         "entity", "dto", "api_interface", "test_support", "test")  # fmt: skip
    )
}
_VERB_RANK = {verb: rank for rank, verb in enumerate(("GET", "POST", "PUT", "PATCH", "DELETE"))}
_BEAN_ROLES = frozenset(
    {"controller", "controller_advice", "service", "repository", "mapper", "config", "test",
     "test_support", "api_interface"}  # fmt: skip
)
_TEST_ROLES = frozenset({"test", "test_support"})
_SETUP_ANNOTATIONS = frozenset({"BeforeEach", "Before", "BeforeAll", "BeforeClass"})
_NAME_STOP_TOKENS = frozenset(
    {"test", "should", "it", "when", "then", "given", "and", "the", "by", "with", "for", "of",
     "in", "to", "from", "on", "at", "an"}  # fmt: skip
)
_TEST_SUFFIXES = ("Tests", "IT", "Test")  # endswith order: "Tests" before "Test"
_ROLE_WORDS = re.compile(r"(Rest)?(Controller|Service|Repository|Mapper|Impl|Resource)(V\d+)?")


class ContextError(Exception):
    """A problem to report as is, without a traceback: no index, no such symbol, bad input."""


# ---- frameworks a test class uses: what to copy ---------------------------------------------

# JUnit is told apart by the annotations only one version has; @Test is in both.
_JUNIT5 = frozenset({"BeforeEach", "AfterEach", "BeforeAll", "AfterAll", "ParameterizedTest",
                     "RepeatedTest", "Nested", "DisplayName", "ExtendWith", "TestFactory",
                     "TempDir"})  # fmt: skip
_JUNIT4 = frozenset({"Before", "After", "BeforeClass", "AfterClass", "RunWith", "Rule",
                     "ClassRule"})  # fmt: skip
_JUNIT_ANY = frozenset({"Test"})


@dataclass(frozen=True)
class _Framework:
    name: str
    annotations: frozenset[str] = frozenset()
    field_types: frozenset[str] = frozenset()
    pattern: re.Pattern[str] | None = None


_FRAMEWORKS = (
    _Framework("@SpringBootTest", frozenset({"SpringBootTest"})),
    _Framework(
        "Spring test slice (@WebMvcTest, @DataJpaTest...)",
        frozenset({"WebMvcTest", "WebFluxTest", "DataJpaTest", "DataJdbcTest", "JdbcTest",
                   "JsonTest", "RestClientTest"}),  # fmt: skip
    ),
    _Framework(
        "Spring test context (@ContextConfiguration, @SpringJUnitConfig...)",
        frozenset({"ContextConfiguration", "SpringJUnitConfig", "WebAppConfiguration",
                   "TestPropertySource", "AutoConfigureMockMvc"}),  # fmt: skip
    ),
    _Framework("@ActiveProfiles", frozenset({"ActiveProfiles"})),
    _Framework(
        "@MockitoBean mocks in the Spring context", frozenset({"MockitoBean", "MockitoSpyBean"})
    ),
    _Framework("@MockBean mocks in the Spring context", frozenset({"MockBean", "SpyBean"})),
    _Framework(
        "Mockito annotations (@Mock, @InjectMocks)",
        frozenset({"Mock", "InjectMocks", "Spy", "Captor"}),
    ),
    _Framework("BDDMockito given(...).willReturn(...)", pattern=re.compile(r"\bgiven\s*\(")),
    _Framework(
        "Mockito when(...).thenReturn(...)",
        pattern=re.compile(r"\bwhen\s*\([^;]*?\)\s*\.\s*then(?:Return|Throw|Answer)"),
    ),
    _Framework("Mockito verify(...)", pattern=re.compile(r"\bverify\s*\(")),
    _Framework(
        "MockMvc",
        field_types=frozenset({"MockMvc"}),
        pattern=re.compile(r"\bMockMvc\b|\.perform\s*\("),
    ),
    _Framework("WebTestClient", field_types=frozenset({"WebTestClient"})),
    _Framework("TestRestTemplate", field_types=frozenset({"TestRestTemplate"})),
    _Framework(
        "AssertJ assertThat(...).isEqualTo(...)",
        pattern=re.compile(r"\bassertThat\s*\([^;]*?\)\s*\.\s*(?:is|has|contains|doesNot|"
                           r"extracting|startsWith|endsWith|matches|as)\w*\s*\("),  # fmt: skip
    ),
    _Framework(
        "Hamcrest assertThat(x, matcher)",
        pattern=re.compile(r"\bassertThat\s*\([^;]*?,\s*(?:is|equalTo|hasSize|notNullValue|"
                           r"nullValue|containsString|hasItems?|hasProperty|not)\s*\("),
    ),
    _Framework(
        "JUnit assertions (assertEquals...)",
        pattern=re.compile(r"\bassert(?:Equals|NotEquals|True|False|Null|NotNull|Throws|Same|"
                           r"ArrayEquals|All)\s*\("),  # fmt: skip
    ),
    _Framework(
        "Spring Security test (@WithMockUser)",
        frozenset({"WithMockUser", "WithUserDetails", "WithAnonymousUser"}),
    ),
    _Framework("@Transactional tests (rolled back)", frozenset({"Transactional"})),
    _Framework(
        "Jackson ObjectMapper for JSON bodies",
        field_types=frozenset({"ObjectMapper"}),
        pattern=re.compile(r"\bObjectMapper\b"),
    ),
)

# ---- the engine ------------------------------------------------------------------------------


def embedder_for_model(model_name: str | None) -> Embedder:
    """The embedder an index was built with, so query vectors match the stored ones."""
    if not model_name:
        return default_embedder()
    if model_name.startswith("hash-") and model_name[5:].isdigit():
        return HashEmbedder(int(model_name[5:]))
    return FastEmbedEmbedder(model_name)


class ContextEngine:
    """One index, loaded once. Answers are graph walks, plus chunk texts and vector search
    when an answer needs them."""

    def __init__(self, index_dir: str | Path = ".tcg", embedder: Embedder | None = None) -> None:
        self.index_dir = Path(index_dir)
        self.store = IndexStore(self.index_dir)
        try:
            graph = self.store.read_graph()
        except (OSError, ValueError) as exc:  # missing, unreadable or from another tcg version
            raise ContextError(str(exc)) from exc
        self.manifest = self.store.read_manifest()
        self.nodes: dict[str, Node] = {node.id: node for node in graph.nodes}
        self._out: dict[str, dict[str, list[Edge]]] = {}  # kind -> source -> edges
        self._in: dict[str, dict[str, list[Edge]]] = {}  # kind -> target -> edges
        for edge in graph.edges:
            self._out.setdefault(edge.kind, {}).setdefault(edge.source, []).append(edge)
            self._in.setdefault(edge.kind, {}).setdefault(edge.target, []).append(edge)
        self._embedder = embedder
        self._texts: dict[str, str | None] = {}  # node id -> its chunk text, all parts joined
        self._parts = _chunk_parts(self.manifest.chunks if self.manifest else {}, self.nodes)
        self._index_names()

    # ---- graph primitives --------------------------------------------------------------------

    def edges_out(self, node_id: str, kind: str) -> list[Edge]:
        return self._out.get(kind, {}).get(node_id, [])

    def edges_in(self, node_id: str, kind: str) -> list[Edge]:
        return self._in.get(kind, {}).get(node_id, [])

    def contained(self, node_id: str, kind: str) -> list[str]:
        """The nodes of a kind that node_id CONTAINS, in source order."""
        ids = [e.target for e in self.edges_out(node_id, CONTAINS) if e.target in self.nodes]
        ids = [i for i in ids if self.nodes[i].kind == kind]
        return sorted(ids, key=lambda i: (self.nodes[i].start_line or 0, i))

    def ancestors(self, type_id: str) -> list[str]:
        """Supertypes and interfaces, transitively, nearest first."""
        return self._walk(type_id, (EXTENDS, IMPLEMENTS), self.edges_out)

    def superclasses(self, type_id: str) -> list[str]:
        """The EXTENDS chain: where inherited fields and injections come from."""
        return self._walk(type_id, (EXTENDS,), self.edges_out)

    def descendants(self, type_id: str) -> list[str]:
        """Subtypes and implementations, transitively, nearest first."""
        return self._walk(type_id, (EXTENDS, IMPLEMENTS), self.edges_in)

    def overridden(self, method_id: str) -> list[str]:
        """The methods this one overrides or implements, transitively."""
        return self._walk(method_id, (OVERRIDES,), self.edges_out)

    def overriders(self, method_id: str) -> list[str]:
        """The methods that override or implement this one, transitively."""
        return self._walk(method_id, (OVERRIDES,), self.edges_in)

    def _walk(
        self, start: str, kinds: tuple[str, ...], step: Callable[[str, str], list[Edge]]
    ) -> list[str]:
        seen = [start]
        for current in seen:  # grows while iterating: breadth first
            for kind in kinds:
                for edge in step(current, kind):
                    other = edge.target if edge.source == current else edge.source
                    if other not in seen and other in self.nodes:
                        seen.append(other)
        return seen[1:]

    def role(self, node_id: str) -> str | None:
        node = self.nodes.get(node_id)
        return node.attrs.get("role") if node else None

    def label(self, node_id: str) -> str:
        """A short readable name: OwnerRestControllerV1#getOwner(Integer), GET /owners/{id}."""
        node = self.nodes.get(node_id)
        if node is None:
            return node_id
        if node.kind == METHOD:
            owner = self.nodes.get(_owner_id(node_id))
            owner_name = owner.name if owner else _owner_id(node_id).rsplit(".", 1)[-1]
            return f"{owner_name}#{node.name}({','.join(_param_types(node_id))})"
        if node.kind == DOC:
            crumbs = node.attrs.get("breadcrumbs") or []
            return " > ".join([*crumbs, node.name])
        if node.kind == FILE:
            return node.path or node.name
        return node.name

    # ---- texts and vectors -------------------------------------------------------------------

    def load_texts(self, node_ids: Iterable[str]) -> None:
        """Fetch the chunk texts of these nodes in one query; a long node's parts are joined."""
        missing = [i for i in dict.fromkeys(node_ids) if i not in self._texts]
        if not missing:
            return
        wanted = {i: self._parts.get(i, []) if self.manifest else [i] for i in missing}
        chunks = self.store.get_chunks(c for ids in wanted.values() for c in ids)
        found = {chunk.id: chunk.text for chunk in chunks}
        for node_id, chunk_ids in wanted.items():
            parts = [found[c] for c in chunk_ids if c in found]
            self._texts[node_id] = "\n".join(parts) if parts else None

    def text(self, node_id: str) -> str | None:
        self.load_texts([node_id])
        return self._texts.get(node_id)

    @property
    def embedder(self) -> Embedder:
        """Created on first use: loading a real model takes a second or so."""
        if self._embedder is None:
            model = self.manifest.embedding_model if self.manifest else None
            self._embedder = embedder_for_model(model)
        return self._embedder

    def vector_search(
        self, query: str, limit: int, kinds: list[str] | None = None
    ) -> list[tuple[Chunk, float]]:
        """(chunk, similarity), best first, one per node (the best of its parts)."""
        vector = self.embedder.embed_query(query)
        try:
            results = self.store.search(vector, limit=limit * 2, kinds=kinds)
        except ValueError as exc:  # the stored vectors have another dimension
            raise ContextError(str(exc)) from exc
        best: dict[str, tuple[Chunk, float]] = {}
        for chunk, score in results:
            best.setdefault(chunk.node_id, (chunk, score))
        return list(best.values())[:limit]

    # ---- resolving symbols -------------------------------------------------------------------

    def _index_names(self) -> None:
        self._types_by_name: dict[str, list[str]] = {}  # "Owner", "Outer.Inner", "Inner"
        self._types_by_lower: dict[str, list[str]] = {}
        self._methods_by_name: dict[str, list[str]] = {}
        self._schemas_by_name: dict[str, list[str]] = {}
        self._type_ids: list[str] = []
        self._endpoints: list[Node] = []
        self._files: list[str] = []
        for node in self.nodes.values():
            if node.kind == TYPE:
                self._type_ids.append(node.id)
                for name in dict.fromkeys((node.name, node.name.rsplit(".", 1)[-1])):
                    self._types_by_name.setdefault(name, []).append(node.id)
                    self._types_by_lower.setdefault(name.lower(), []).append(node.id)
            elif node.kind == METHOD:
                self._methods_by_name.setdefault(node.name, []).append(node.id)
            elif node.kind == SCHEMA:
                self._schemas_by_name.setdefault(node.name, []).append(node.id)
            elif node.kind == ENDPOINT:
                self._endpoints.append(node)
            elif node.kind == FILE:
                self._files.append(node.id)

    def resolve(self, symbol: str) -> list[Node]:
        """The nodes a symbol names, best first; several when it's ambiguous, [] when none.
        Accepts a node id (with or without its kind prefix); a qualified, simple or nested type
        name; Type#method, Type.method, Type#method(String, int); a bare method name; a file path
        (-> its types); an endpoint "GET /owners/{id}", or a served or client URL path."""
        text = " ".join(symbol.split())
        if not text:
            return []
        for ids, ranked in self._match_tiers(text):
            found = [self.nodes[i] for i in dict.fromkeys(ids) if i in self.nodes]
            if found:
                return found if ranked else sorted(found, key=self._preference)
        return []

    def _match_tiers(self, text: str) -> Iterator[tuple[list[str], bool]]:
        """Candidate ids, tier by tier: the first tier that finds a node wins. The flag says the
        tier is already in order."""
        yield [text, *(prefix + text for prefix in _ID_PREFIXES)], False
        part = _PART.search(text)
        if part:  # a chunk id: "<node id>#part2"
            yield [text[: part.start()]], False
        verb = _HTTP.fullmatch(text)
        if verb or text.startswith("/"):
            method, path = (verb.group(1).upper(), verb.group(2)) if verb else (None, text)
            yield self._endpoint_ids(method, path), False
        if "/" in text or text.endswith(_FILE_SUFFIXES):
            yield self._file_ids(text), True
        for ids in self._code_ids(text):
            yield ids, False
        yield self._schemas_by_name.get(text, []), False

    def _endpoint_ids(self, method: str | None, path: str) -> list[str]:
        if not path.startswith("/"):
            return []
        exact = [
            ep.id
            for ep in self._endpoints
            if method in (None, ep.attrs.get("http_method"))
            and path in (ep.attrs.get(k) for k in ("spec_path", "served_path", "url_path"))
        ]
        return exact or match_url(path, method, self._endpoints)

    def _file_ids(self, text: str) -> list[str]:
        path = text[2:] if text.startswith("./") else text
        if f"file:{path}" in self.nodes:
            files = [f"file:{path}"]
        else:
            files = [f for f in self._files if (self.nodes[f].path or "").endswith("/" + path)]
        found: list[str] = []
        for fid in sorted(files):
            stem = Path(self.nodes[fid].path or "").stem
            types = self.contained(fid, TYPE)
            found += sorted(types, key=lambda t: (self.nodes[t].name != stem, t)) or [fid]
        return found

    def _code_ids(self, text: str) -> Iterator[list[str]]:
        name, params = _split_params(text)
        if not _CODE_NAME.fullmatch(name):
            return
        if "#" in name:
            type_part, method = name.split("#", 1)
            yield self._methods_in(self._find_types(type_part), method, params)
            return
        if params is None:
            yield self._find_types(name)
        if "." in name:
            type_part, method = name.rsplit(".", 1)
            yield self._methods_in(self._find_types(type_part), method, params)
        yield _by_params(self._methods_by_name.get(name, []), params)

    def _find_types(self, name: str) -> list[str]:
        """Qualified name, else simple or nested name, else a dotted suffix ("model.Owner"),
        else the simple name in any case."""
        if not name:
            return []
        if f"type:{name}" in self.nodes:
            return [f"type:{name}"]
        if name in self._types_by_name:
            return list(self._types_by_name[name])
        if "." in name:
            found = [i for i in self._type_ids if i.endswith("." + name)]
            if found:
                return found
        return list(self._types_by_lower.get(name.lower(), []))

    def _methods_in(self, type_ids: list[str], method: str, params: list[str] | None) -> list[str]:
        """A method of these types; if a type doesn't declare it, the nearest supertype that
        does, else its implementations (a generated *Api interface has no method nodes)."""
        found: list[str] = []
        for type_id in type_ids:
            for scope in ([type_id], self.ancestors(type_id), self.descendants(type_id)):
                methods = [m for t in scope for m in self.contained(t, METHOD)]
                named = [m for m in methods if self.nodes[m].name == method]
                if named:
                    found += named
                    break
        return _by_params(found, params)

    def _preference(self, node: Node) -> tuple[Any, ...]:
        """Among equally good matches: main code before tests, a body before an abstract
        declaration, controllers and services before entities and DTOs."""
        if node.kind == METHOD:
            owner = self.nodes.get(_owner_id(node.id))
            attrs = owner.attrs if owner else {}
            has_body = node.attrs.get("has_body", True)
        elif node.kind == TYPE:
            attrs, has_body = node.attrs, not node.attrs.get("external")
        else:
            rank = _VERB_RANK.get(node.attrs.get("http_method"), 9) if node.kind == ENDPOINT else 0
            return (node.kind != ENDPOINT, False, False, rank, node.id)
        is_test = attrs.get("source_set") == "test"
        return (False, is_test, not has_body, _ROLE_RANK.get(attrs.get("role"), 6), node.id)

    def suggest(self, symbol: str, limit: int = 5) -> list[str]:
        """Names close to a symbol that resolves to nothing: a typo, a wrong case."""
        core = re.split(r"[(\s]", symbol.strip(), maxsplit=1)[0].split("#")[-1].rsplit(".", 1)[-1]
        if not core:
            return []
        names = [*self._types_by_name, *self._methods_by_name]
        labels: list[str] = []
        for name in difflib.get_close_matches(core, names, n=limit, cutoff=0.6):
            ids = self._types_by_name.get(name) or self._methods_by_name.get(name) or []
            labels += [self.label(i) for i in ids[:2]]
        return list(dict.fromkeys(labels))[:limit]

    def pick(self, symbol: str) -> tuple[Node, list[Node]]:
        """The best match and the others, or a ContextError that suggests close names."""
        candidates = self.resolve(symbol)
        if not candidates:
            message = f"no symbol matches {symbol!r}"
            close = self.suggest(symbol)
            if close:
                message += f"; did you mean {', '.join(close)}?"
            hint = f"search by meaning: `tcg search {shlex.quote(symbol)}`, or the search_code tool"
            raise ContextError(f"{message} ({hint})")
        return candidates[0], candidates[1:]

    # ---- answers -----------------------------------------------------------------------------

    def test_context(self, symbol: str, max_tests: int = 3, max_docs: int = 3) -> dict[str, Any]:
        """Everything needed to write a test for a method (or a whole class): its code, what to
        mock, the data types, the endpoints it serves, existing tests to copy, conventions and
        docs. JSON-ready and bounded."""
        target, others, notes = self._test_target(symbol)
        return _TestContext(self, target, notes).build(symbol, others, max_tests, max_docs)

    def find_tests(self, symbol: str) -> dict[str, Any]:
        """The existing tests of a symbol, strongest link first, with the test methods involved
        and why each is related. Graph only: no sources, so it's fast."""
        target, others, notes = self._test_target(symbol)
        builder = _TestContext(self, target, notes)
        tests = []
        for hit in builder.test_hits()[:MAX_FIND_TESTS]:
            related_only = target.kind == METHOD
            methods = builder.rank_test_methods(hit, MAX_FIND_TESTS, related_only=related_only)
            tests.append({
                **builder.test_type_summary(hit),
                "methods": [{"id": m, "label": self.label(m), "lines": _lines(self.nodes[m]),
                             "why": why} for m, why in methods],
            })  # fmt: skip
        if not tests:
            notes.append(f"no existing tests found for {self.label(target.id)}")
        return {
            "symbol": symbol,
            "target": {"id": target.id, "kind": target.kind, "label": self.label(target.id),
                       "path": target.path, "lines": _lines(target)},  # fmt: skip
            "candidates": self._candidates(others),
            "tests": tests,
            "notes": notes,
        }

    def _test_target(self, symbol: str) -> tuple[Node, list[Node], list[str]]:
        """The method or type to test: an endpoint stands for the method serving it."""
        target, others = self.pick(symbol)
        notes: list[str] = []
        if others:
            shown = ", ".join(c["label"] for c in self._candidates(others))
            notes.append(f"{symbol!r} matches {len(others) + 1} symbols; showing "
                         f"{self.label(target.id)}. Others: {shown}. Pass a node id or "
                         "Type#method(params) to choose.")  # fmt: skip
        if target.kind == ENDPOINT:
            servers = sorted(x.source for x in self.edges_in(target.id, SERVES))
            if not servers:
                raise ContextError(f"{target.name} is in the spec, but no method in the source "
                                   "serves it")  # fmt: skip
            notes.append(f"{target.name} is served by {', '.join(self.label(s) for s in servers)}")
            target = self.nodes[servers[0]]
        if target.kind not in (METHOD, TYPE):
            raise ContextError(f"{self.label(target.id)} is a {target.kind}: a test context needs "
                               "a class, a method or an endpoint; `tcg show` describes any "
                               "node")  # fmt: skip
        return target, others, notes

    def _candidates(self, others: list[Node]) -> list[dict[str, str]]:
        return [{"id": n.id, "label": self.label(n.id)} for n in others[:MAX_CANDIDATES]]

    def search(
        self, query: str, kinds: list[str] | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Chunks by meaning (vector search), one hit per node, best first."""
        if not query.strip():
            raise ContextError("the search query is empty")
        kinds = _check_kinds(kinds, CHUNK_KINDS, "kind")
        return [
            {
                "id": chunk.node_id,
                "kind": chunk.kind,
                "name": self.label(chunk.node_id),
                "path": chunk.path,
                "lines": [chunk.start_line, chunk.end_line],
                "score": round(score, 4),
                "snippet": _snippet(chunk.text),
            }
            for chunk, score in self.vector_search(query, max(1, min(limit, 100)), kinds)
        ]

    def get_node(self, symbol: str) -> dict[str, Any]:
        """A node with its chunk text and how many edges of each kind it has."""
        node, others = self.pick(symbol)
        text, truncated = _cut(self.text(node.id), MAX_SHOW_CHARS)
        return {
            "node": to_dict(node),
            "label": self.label(node.id),
            "text": text,
            "truncated": truncated,
            "edges": {"out": self._edge_counts(node.id, self._out),
                      "in": self._edge_counts(node.id, self._in)},  # fmt: skip
            "candidates": self._candidates(others),
        }

    def neighbors(
        self,
        node_id: str,
        edge_kinds: list[str] | None = None,
        direction: str = "both",
        limit: int = 50,
    ) -> dict[str, Any]:
        """The nodes one edge away, filtered by edge kind and direction ("out", "in", "both").
        node_id may also be any symbol `resolve` accepts."""
        if direction not in ("out", "in", "both"):
            raise ContextError(f"direction must be 'out', 'in' or 'both', not {direction!r}")
        wanted = [k.upper() for k in edge_kinds] if edge_kinds else None
        kinds = _check_kinds(wanted, EDGE_KINDS, "edge kind") or EDGE_KINDS
        node = self.nodes.get(node_id) or self.pick(node_id)[0]
        items = []
        for side, table in (("out", self._out), ("in", self._in)):
            if direction not in (side, "both"):
                continue
            for kind in (k for k in EDGE_KINDS if k in kinds):
                for edge in table.get(kind, {}).get(node.id, []):
                    other = edge.target if side == "out" else edge.source
                    other_node = self.nodes.get(other)
                    items.append({
                        "direction": side, "edge": kind, "id": other, "label": self.label(other),
                        "kind": other_node.kind if other_node else None,
                        "path": other_node.path if other_node else None, "attrs": edge.attrs,
                    })  # fmt: skip
        limit = max(0, limit)
        return {"id": node.id, "label": self.label(node.id), "total": len(items),
                "truncated": len(items) > limit, "neighbors": items[:limit]}  # fmt: skip

    def stats(self) -> dict[str, Any]:
        """What's indexed, when and how: repo, commit, model, counts, age."""
        m = self.manifest
        meta = {} if m else self.store.read_meta()
        generated = m.generated_at if m else meta.get("generated_at", "")
        age = _age_seconds(generated)
        return {
            "index_dir": str(self.index_dir),
            "repo_name": m.repo_name if m else meta.get("repo_name"),
            "repo_commit": m.repo_commit if m else meta.get("repo_commit"),
            "generated_at": generated,
            "age_seconds": age,
            "age": _age_text(age),
            "embedding_model": m.embedding_model if m else None,
            "embedding_dim": m.embedding_dim if m else None,
            "tool_version": m.tool_version if m else None,
            "counts": dict(m.counts) if m else {"nodes": len(self.nodes)},
        }

    def _edge_counts(self, node_id: str, table: dict[str, dict[str, list[Edge]]]) -> dict[str, int]:
        return {k: len(table[k][node_id]) for k in EDGE_KINDS if node_id in table.get(k, {})}

    # ---- facts shared by answers -------------------------------------------------------------

    def fields(self, type_id: str) -> tuple[list[dict[str, Any]], int]:
        """Declared and inherited fields (the superclass chain), and how many were left out."""
        fields = [
            _field(f, self.nodes[owner].name if owner != type_id else None)
            for owner in [type_id, *self.superclasses(type_id)]
            for f in self.nodes[owner].attrs.get("fields") or []
        ]
        return fields[:MAX_FIELDS], max(0, len(fields) - MAX_FIELDS)

    def schema_fields(self, schema_id: str) -> dict[str, Any]:
        """A schema's properties, with those of the schemas it composes (allOf) first: the fields
        of the DTO generated from it."""
        node = self.nodes[schema_id]
        properties: dict[str, str] = {}
        required: list[str] = []
        for part in self._all_of(schema_id, [schema_id]):
            attrs = self.nodes[part].attrs
            properties.update(attrs.get("properties") or {})
            required += [r for r in attrs.get("required") or [] if r not in required]
        return {"id": schema_id, "name": node.name, "path": node.path, "line": node.start_line,
                "all_of": list(node.attrs.get("all_of") or []), "properties": properties,
                "required": required}  # fmt: skip

    def _all_of(self, schema_id: str, seen: list[str]) -> list[str]:
        """The schemas composed into this one, depth first, then itself."""
        parts = []
        for edge in self.edges_out(schema_id, REFERENCES):
            part = edge.target
            if edge.attrs.get("as") == "all_of" and part in self.nodes and part not in seen:
                seen.append(part)
                parts += self._all_of(part, seen)
        return [*parts, schema_id]


# ---- one test context ------------------------------------------------------------------------


@dataclass
class _Link:
    weight: int
    relation: str  # how the tested node relates to the target: "", "which the target implements"
    sources: set[str] = field(default_factory=set)  # the test methods or classes behind it


@dataclass
class _TestHit:
    """A test class linked to the target: how strongly, why, and through which test methods."""

    type_id: str
    weight: int = 0
    links: dict[tuple[str, str], _Link] = field(default_factory=dict)  # (tested, reason) -> link
    methods: dict[str, tuple[int, str]] = field(default_factory=dict)  # test method -> weight, why

    @property
    def count(self) -> int:
        return sum(len(link.sources) for link in self.links.values())


class _TestContext:
    """Builds one answer: graph walks first, then one chunk query for every text it shows,
    then the docs search."""

    def __init__(self, engine: ContextEngine, target: Node, notes: list[str]) -> None:
        self.e = engine
        self.target = target
        self.notes = notes
        self.is_method = target.kind == METHOD
        type_node = engine.nodes.get(_owner_id(target.id) if self.is_method else target.id)
        if type_node is None:
            raise ContextError(f"the index has no type for {target.id}: rebuild it with "
                               "`tcg index <repo> --full`")  # fmt: skip
        self.type = type_node
        self.methods = [target.id] if self.is_method else engine.contained(target.id, METHOD)
        self.calls = self._calls()
        name = target.name if self.is_method else _strip_role_words(target.name)
        self.name_tokens = _name_tokens(name)

    def _calls(self) -> list[tuple[str, Edge]]:
        """Resolved CALLS from the target's methods, one per callee, in source order."""
        callees: dict[str, Edge] = {}
        for method in self.methods:
            edges = self.e.edges_out(method, CALLS)
            for edge in sorted(edges, key=lambda x: (x.attrs.get("line") or 0, x.target)):
                if edge.target in self.e.nodes:
                    callees.setdefault(edge.target, edge)
        return list(callees.items())

    def build(
        self, symbol: str, others: list[Node], max_tests: int, max_docs: int
    ) -> dict[str, Any]:
        e = self.e
        deps = self.dependencies()
        collaborators = self.collaborators(deps)
        data_types = self.data_types(deps)
        endpoints = self.endpoints()
        all_hits = self.test_hits()
        hits = self.select_tests(all_hits, max(0, max_tests))
        implementations = self.implementations(deps)
        scope = self.convention_scope(hits)

        # One query for every text shown below.
        helpers = [c["id"] for c in collaborators if c["same_type"]][:MAX_HELPERS]
        examples = {  # two example methods from the strongest test class, one from the others
            hit.type_id: self.rank_test_methods(hit, 1 if i else 2) for i, hit in enumerate(hits)
        }
        setups = {hit.type_id: self.setup_method(hit.type_id) for hit in hits}
        e.load_texts([
            self.target.id, self.type.id, *self.methods, *helpers, *scope["methods"],
            *(m for chosen in examples.values() for m, _ in chosen), *filter(None, setups.values()),
        ])  # fmt: skip

        for c in collaborators:
            if c["id"] in helpers:
                c["source"], c["truncated"] = _cut(e.text(c["id"]), MAX_HELPER_CHARS)
        target = self.target_info()
        enclosing = self.enclosing_type(helpers) if self.is_method else None
        tests = [self.test_type(hit, examples[hit.type_id], setups[hit.type_id]) for hit in hits]
        if not all_hits:
            self.notes.append(f"no existing tests found for {e.label(self.target.id)} or its class")
        elif len(all_hits) > len(hits):
            more = _count(len(all_hits) - len(hits), "more related test class")
            self.notes.append(f"{more}: find_tests or `tcg tests {self.target.id}`")
        conventions = self.conventions(hits, scope)
        docs = self.related_docs(max_docs, deps, endpoints)
        return {
            "symbol": symbol,
            "target": target,
            "candidates": e._candidates(others),
            "enclosing_type": enclosing,
            "dependencies": deps,
            "collaborators": collaborators,
            "data_types": data_types,
            "endpoints": endpoints,
            "existing_tests": tests,
            "implementations": implementations,
            "conventions": conventions,
            "related_docs": docs,
            "notes": list(dict.fromkeys(self.notes)),
        }

    # ---- the target --------------------------------------------------------------------------

    def target_info(self) -> dict[str, Any]:
        e, node = self.e, self.target
        source, truncated = _cut(e.text(node.id), MAX_SOURCE_CHARS)
        info: dict[str, Any] = {
            "id": node.id,
            "kind": node.kind,
            "label": e.label(node.id),
            "role": self.type.attrs.get("role"),
            "path": node.path,
            "lines": _lines(node),
            "annotations": node.attrs.get("annotations") or [],
            "javadoc": node.attrs.get("javadoc"),
            "source": source,
            "truncated": truncated,
        }
        if not self.is_method:
            info.update({
                "qualified_name": node.attrs.get("qualified_name"),
                "type_kind": node.attrs.get("type_kind"),
                "profiles": node.attrs.get("profiles") or [],
                "methods": self._type_methods(),
            })  # fmt: skip
            return info
        has_body = node.attrs.get("has_body", True)
        info.update({
            "signature": node.attrs.get("signature"),
            "parameters": node.attrs.get("parameters") or [],
            "return_type": node.attrs.get("return_type"),
            "has_body": has_body,
        })  # fmt: skip
        if source is None:
            self.notes.append(f"{e.label(node.id)} has no indexed source (a trivial accessor?): "
                              "its signature is all there is")  # fmt: skip
        if not has_body:
            impls = ", ".join(e.label(m) for m in e.overriders(node.id)[:MAX_IMPLEMENTATIONS])
            self.notes.append(f"{e.label(node.id)} is abstract: test it through its "
                              f"implementations{f' ({impls})' if impls else ''}")  # fmt: skip
        return info

    def _type_methods(self) -> list[dict[str, Any]]:
        """A class target's methods; their sources while the budget lasts."""
        budget = MAX_TYPE_SOURCES_CHARS
        items = []
        for method in self.methods:
            node = self.e.nodes[method]
            item: dict[str, Any] = {"id": method, "label": self.e.label(method),
                                    "signature": node.attrs.get("signature"),
                                    "lines": _lines(node)}  # fmt: skip
            text = self.e.text(method)
            if text and len(text) <= budget:
                item["source"] = text
                budget -= len(text)
            items.append(item)
        if any("source" not in item for item in items):
            self.notes.append("some method sources of the class are left out (size): the "
                              "test context of Type#method shows one")  # fmt: skip
        return items

    def enclosing_type(self, helpers: list[str]) -> dict[str, Any]:
        """The class around the target. A big class's outline keeps its header, fields,
        constructors, the target and its helpers: the rest is noise for this test."""
        e, node = self.e, self.type
        outline = e.text(node.id)
        methods = e.contained(node.id, METHOD)
        if outline and len(methods) > MAX_OUTLINE_METHODS:
            keep = {self.target.id, *helpers}
            dropped = [
                e.nodes[m].attrs.get("signature") or ""
                for m in methods
                if m not in keep and e.nodes[m].attrs.get("method_kind") != "constructor"
            ]
            outline = _trim_outline(outline, dropped, f"get_node or `tcg show {node.id}`")
        outline, truncated = _cut(outline, MAX_SOURCE_CHARS)
        return {
            "id": node.id,
            "name": node.name,
            "type_kind": node.attrs.get("type_kind"),
            "role": node.attrs.get("role"),
            "annotations": node.attrs.get("annotations") or [],
            "profiles": node.attrs.get("profiles") or [],
            "implements": node.attrs.get("implements") or [],
            "extends": node.attrs.get("extends") or [],
            "path": node.path,
            "lines": _lines(node),
            "outline": outline,
            "truncated": truncated,
        }

    # ---- what the code touches ---------------------------------------------------------------

    def dependencies(self) -> list[dict[str, Any]]:
        """Types the class INJECTS (its own and inherited), with the methods the target calls on
        each, those it calls first: what a unit test mocks or provides."""
        e = self.e
        entries: list[tuple[int, int, dict[str, Any]]] = []  # (first call, found, dependency)
        seen: set[str] = set()
        for owner in [self.type.id, *e.superclasses(self.type.id)]:
            for edge in e.edges_out(owner, INJECTS):
                dep = e.nodes.get(edge.target)
                if dep is None or dep.id in seen or dep.id == self.type.id:
                    continue
                seen.add(dep.id)
                family = {dep.id, *e.ancestors(dep.id)}
                called = [i for i, (c, _) in enumerate(self.calls) if _owner_id(c) in family]
                calls = [self._call_info(self.calls[i][0]) for i in called]
                entries.append((called[0] if called else len(self.calls), len(entries), {
                    "id": dep.id,
                    "name": dep.name,
                    "type_kind": dep.attrs.get("type_kind"),
                    "interface": dep.attrs.get("type_kind") == "interface",
                    "role": dep.attrs.get("role"),
                    "generated": bool(dep.attrs.get("generated")),
                    "field": edge.attrs.get("name"),
                    "via": edge.attrs.get("via"),
                    "inherited_from": e.nodes[owner].name if owner != self.type.id else None,
                    "calls": calls[:MAX_CALLS],
                    "more_calls": max(0, len(calls) - MAX_CALLS),
                    "in_tests": self._dependency_in_tests(dep.id),
                }))  # fmt: skip
        ranked = [dep for *_, dep in sorted(entries, key=lambda entry: entry[:2])]
        if len(ranked) > MAX_DEPENDENCIES:
            left_out = _count(len(ranked) - MAX_DEPENDENCIES, "more injected dependency")
            self.notes.append(f"{left_out} left out")
        return ranked[:MAX_DEPENDENCIES]

    def _call_info(self, method_id: str) -> dict[str, Any]:
        signature = self.e.nodes[method_id].attrs.get("signature")
        return {"id": method_id, "label": self.e.label(method_id), "signature": signature}

    def _dependency_in_tests(self, dep_id: str) -> dict[str, Any]:
        """How the repo's tests treat a dependency: mocked (and how) or injected for real."""
        e = self.e
        mocked = sorted({
            (e.nodes[x.source].name, x.attrs.get("via") or "")
            for x in e.edges_in(dep_id, MOCKS) if x.source in e.nodes
        })  # fmt: skip
        injected = sorted({
            e.nodes[x.source].name
            for x in e.edges_in(dep_id, INJECTS) if e.role(x.source) in _TEST_ROLES
        })  # fmt: skip
        return {
            "mocked_in": [f"{name} ({_via(via)})" for name, via in mocked[:5]],
            "mocked_count": len(mocked),
            "injected_in": injected[:5],
            "injected_count": len(injected),
        }

    def collaborators(self, deps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Other resolved calls: helpers of the same class first, then other project code."""
        e = self.e
        on_deps = {t for d in deps for t in (d["id"], *e.ancestors(d["id"]))}
        own = {self.type.id, *e.ancestors(self.type.id)}
        items = []
        for callee, edge in self.calls:
            owner = _owner_id(callee)
            if owner in on_deps or (owner in own and not self.is_method):
                continue
            items.append({
                **self._call_info(callee),
                "owner": e.label(owner),
                "owner_role": e.role(owner),
                "resolution": edge.attrs.get("resolution"),
                "same_type": owner in own,
            })  # fmt: skip
            if edge.attrs.get("resolution") == "name_only":
                self.notes.append(f"the call to {e.label(callee)} was matched by name only: "
                                  "check it")  # fmt: skip
        items.sort(key=lambda item: not item["same_type"])  # stable: source order otherwise
        return items[:MAX_COLLABORATORS]

    def data_types(self, deps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Project types in the target's signatures and `new`s, then those the mocked calls take
        and return: what test data is made of. Generated DTOs come with their schema fields."""
        e = self.e
        excluded = {self.type.id, *e.ancestors(self.type.id), *(d["id"] for d in deps)}
        uses: dict[str, list[str]] = {}

        def add(type_id: str, how: str) -> None:
            node = e.nodes.get(type_id)
            if node is not None and type_id not in excluded and self._is_data(node):
                hows = uses.setdefault(type_id, [])
                if how not in hows:
                    hows.append(how)

        for method in self.methods:
            for edge in e.edges_out(method, USES):
                add(edge.target, f"{edge.attrs.get('as') or 'used'} of {e.nodes[method].name}")
        for dep in deps:
            for call in dep["calls"]:
                for edge in e.edges_out(call["id"], USES):
                    if edge.attrs.get("as") in ("parameter", "return"):
                        add(edge.target, f"{edge.attrs['as']} of {call['label']}")
        if len(uses) > MAX_DATA_TYPES:
            self.notes.append(f"{_count(len(uses) - MAX_DATA_TYPES, 'more data type')} left out")
        return [self._data_type(t, hows) for t, hows in list(uses.items())[:MAX_DATA_TYPES]]

    def _is_data(self, node: Node) -> bool:
        """Not a bean: beans are mocked or injected, data is built."""
        return (node.kind == TYPE and node.attrs.get("role") not in _BEAN_ROLES
                and not self.e.edges_in(node.id, INJECTS))  # fmt: skip

    def _data_type(self, type_id: str, hows: list[str]) -> dict[str, Any]:
        e = self.e
        node = e.nodes[type_id]
        item: dict[str, Any] = {
            "id": type_id,
            "name": node.name,
            "role": node.attrs.get("role"),
            "type_kind": node.attrs.get("type_kind"),
            "path": node.path,
            "used_as": hows[:4],
            "generated": bool(node.attrs.get("generated")),
        }
        if not node.attrs.get("external"):
            item["fields"], item["more_fields"] = e.fields(type_id)
            return item
        schemas = [x.target for x in e.edges_out(type_id, GENERATED_FROM) if x.target in e.nodes]
        if schemas:
            item["schema"] = e.schema_fields(schemas[0])
            schema = e.nodes[schemas[0]]
            self.notes.append(f"{node.name} is generated at build time from schema "
                              f"{schema.name} ({schema.path}): its fields are the schema's "
                              "properties")  # fmt: skip
        else:
            self.notes.append(f"{node.name} is generated at build time (not in the source tree); "
                              "no schema was found for it")  # fmt: skip
        return item

    def endpoints(self) -> list[dict[str, Any]]:
        """Endpoints served by the target, with the test methods already calling each."""
        e = self.e
        items = []
        for method in self.methods:
            for edge in e.edges_out(method, SERVES):
                ep = e.nodes.get(edge.target)
                if ep is None:
                    continue
                a = ep.attrs
                tested = sorted(
                    {x.source for x in e.edges_in(ep.id, TESTS)
                     if x.attrs.get("reason") == "http" and x.source in e.nodes},
                    key=e.label,
                )  # fmt: skip
                items.append({
                    "id": ep.id, "name": ep.name, "http_method": a.get("http_method"),
                    "spec_path": a.get("spec_path"), "served_path": a.get("served_path"),
                    "url_path": a.get("url_path"), "operation_id": a.get("operation_id"),
                    "summary": a.get("summary"), "description": _cut(a.get("description"), 300)[0],
                    "parameters": a.get("parameters") or [], "request_body": a.get("request_body"),
                    "responses": a.get("responses") or {}, "served_by": e.label(method),
                    "via": edge.attrs.get("via"), "spec": ep.path if a.get("spec_path") else None,
                    "tested_by": [e.label(t) for t in tested[:MAX_TESTED_BY]],
                    "tested_by_count": len(tested),
                })  # fmt: skip
        return items[:MAX_ENDPOINTS]

    # ---- existing tests ----------------------------------------------------------------------

    def _test_targets(self) -> list[tuple[str, int, str]]:
        """What a test can be linked through, with a weight and how it relates to the target."""
        e = self.e
        name = self.type.name
        targets: list[tuple[str, int, str]] = []
        for method in self.methods:
            targets.append((method, W_DIRECT, ""))
            implements = "which the target implements"
            targets += [(o, W_OVERRIDDEN, implements) for o in e.overridden(method)]
            served = [x.target for x in e.edges_out(method, SERVES)]
            targets += [(ep, W_DIRECT, "served by the target") for ep in served]
        targets.append((self.type.id, W_TYPE, ""))
        targets += [(t, W_SUPER, f"a supertype of {name}") for t in e.ancestors(self.type.id)]
        if self.is_method:
            siblings = [m for m in e.contained(self.type.id, METHOD) if m != self.target.id]
            targets += [(m, W_SIBLING, f"another method of {name}") for m in siblings]
        return targets

    def test_hits(self) -> list[_TestHit]:
        """Test classes linked by TESTS edges to the target, strongest first."""
        e = self.e
        hits: dict[str, _TestHit] = {}
        for tested, weight, relation in self._test_targets():
            for edge in e.edges_in(tested, TESTS):
                source = e.nodes.get(edge.source)
                if source is None:
                    continue
                type_id = source.id if source.kind == TYPE else _owner_id(source.id)
                if type_id not in e.nodes or type_id == self.type.id:
                    continue
                hit = hits.setdefault(type_id, _TestHit(type_id))
                hit.weight = max(hit.weight, weight)
                reason = edge.attrs.get("reason") or "?"
                link = hit.links.setdefault((tested, reason), _Link(weight, relation))
                link.sources.add(source.id)
                if source.kind == METHOD and weight > hit.methods.get(source.id, (0, ""))[0]:
                    hit.methods[source.id] = (weight, f"{_verb(reason)} {e.label(tested)}")
        return sorted(hits.values(), key=lambda h: (-h.weight, -h.count, e.nodes[h.type_id].name))

    def select_tests(self, hits: list[_TestHit], max_tests: int) -> list[_TestHit]:
        """The strongest test classes; a subclass of one already chosen is listed under it."""
        chosen: list[_TestHit] = []
        for hit in hits:
            if len(chosen) >= max_tests:
                break
            if not any(c.type_id in self.e.superclasses(hit.type_id) for c in chosen):
                chosen.append(hit)
        return chosen

    def _test_chain(self, type_id: str) -> list[str]:
        """A test class and the test base classes it extends: where its methods and fields live."""
        bases = [t for t in self.e.superclasses(type_id) if self.e.role(t) in _TEST_ROLES]
        return [type_id, *bases]

    def rank_test_methods(
        self, hit: _TestHit, count: int, related_only: bool = False
    ) -> list[tuple[str, str]]:
        """The test methods most related to the target: linked by TESTS edges first, then by the
        words their names share with the target's (testGetOwnerSuccess for getOwner), then in
        source order. Any test of the class serves as a style example after those."""
        e = self.e
        tests = [
            m
            for owner in self._test_chain(hit.type_id)
            for m in e.contained(owner, METHOD)
            if e.nodes[m].attrs.get("is_test") or m in hit.methods
        ]
        shared = {
            m: len(self.name_tokens & _name_tokens(e.nodes[m].name))
            for m in dict.fromkeys([*tests, *hit.methods])
        }
        ranked = dict(hit.methods)
        similar = [m for m in shared if m not in ranked and shared[m]]
        if related_only:  # linked test methods say it better than names
            similar = [] if hit.methods else sorted(similar, key=lambda m: -shared[m])
            similar = similar[:MAX_SIMILAR_TESTS]
        ranked.update((m, (0, "similar name")) for m in similar)
        if not related_only:
            for method in shared:
                ranked.setdefault(method, (0, "a test of this class"))

        def key(item: tuple[str, tuple[int, str]]) -> tuple[Any, ...]:
            method, (weight, _) = item
            node = e.nodes[method]
            return (-weight, -shared[method], node.path or "", node.start_line or 0, method)

        return [(method, why) for method, (_, why) in sorted(ranked.items(), key=key)[:count]]

    def setup_method(self, type_id: str) -> str | None:
        """The @BeforeEach (or JUnit 4 @Before) method: how the test class builds its subject."""
        for owner in self._test_chain(type_id):
            for method in self.e.contained(owner, METHOD):
                if _SETUP_ANNOTATIONS & set(self.e.nodes[method].attrs.get("annotations") or []):
                    return method
        return None

    def test_type_summary(self, hit: _TestHit) -> dict[str, Any]:
        e = self.e
        node = e.nodes[hit.type_id]
        a = node.attrs
        return {
            "id": node.id,
            "name": node.name,
            "path": node.path,
            "lines": _lines(node),
            "annotations": a.get("annotations") or [],
            "active_profiles": a.get("active_profiles") or [],
            "is_abstract": bool(a.get("is_abstract")),
            "extends": [e.nodes[t].name for t in self._test_chain(hit.type_id)[1:]],
            "why": self._why(hit),
            "subclasses": self._subclasses(hit.type_id),
        }

    def _why(self, hit: _TestHit) -> list[str]:
        e = self.e
        reasons = []
        links = sorted(hit.links.items(), key=lambda kv: (-kv[1].weight, kv[0]))
        for (tested, reason), link in links:
            methods = [s for s in link.sources if e.nodes[s].kind == METHOD]
            if methods:
                many = "s" if len(methods) > 1 else ""
                text = f"{len(methods)} test method{many} {_verb(reason)} {e.label(tested)}"
            else:
                text = f"tests {e.label(tested)} (by {reason})"
            reasons.append(f"{text}, {link.relation}" if link.relation else text)
        return reasons[:5]

    def _subclasses(self, type_id: str) -> list[dict[str, Any]]:
        """Test classes extending this one: each runs its tests, often under other profiles."""
        e = self.e
        subclasses = [t for t in e.descendants(type_id) if e.role(t) in _TEST_ROLES]
        return [
            {"id": t, "name": e.nodes[t].name, "path": e.nodes[t].path,
             "annotations": e.nodes[t].attrs.get("annotations") or [],
             "active_profiles": e.nodes[t].attrs.get("active_profiles") or []}
            for t in subclasses[:MAX_SUBCLASSES]
        ]  # fmt: skip

    def test_type(
        self, hit: _TestHit, examples: list[tuple[str, str]], setup: str | None
    ) -> dict[str, Any]:
        """A test class to copy: how it's set up, what it mocks, and related test methods."""
        e = self.e
        chain = self._test_chain(hit.type_id)
        fields = [
            _field(f, e.nodes[owner].name if owner != hit.type_id else None)
            for owner in chain
            for f in e.nodes[owner].attrs.get("fields") or []
        ]
        mocks = [
            {"id": x.target, "name": e.label(x.target), "via": x.attrs.get("via")}
            for owner in chain
            for x in e.edges_out(owner, MOCKS)
        ]
        return {
            **self.test_type_summary(hit),
            "setup_fields": fields[:MAX_FIELDS],
            "mocks": mocks,
            "setup": self._source(setup, MAX_SETUP_CHARS) if setup else None,
            "methods": [{**self._source(m, MAX_TEST_CHARS), "why": why} for m, why in examples],
        }

    def _source(self, method_id: str, limit: int) -> dict[str, Any]:
        node = self.e.nodes[method_id]
        source, truncated = _cut(self.e.text(method_id), limit)
        return {"id": method_id, "label": self.e.label(method_id), "path": node.path,
                "lines": _lines(node), "source": source, "truncated": truncated}  # fmt: skip

    # ---- implementations, conventions, docs --------------------------------------------------

    def implementations(self, deps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """For an interface (or abstract class) target or called dependency: the classes behind
        it, with their profiles; which one runs depends on the active profile."""
        e = self.e
        subjects = []
        if self.type.attrs.get("type_kind") == "interface" or self.type.attrs.get("is_abstract"):
            subjects.append(self.type.id)
        subjects += [d["id"] for d in deps if d["interface"] and (d["calls"] or not self.is_method)]
        items = []
        for subject in dict.fromkeys(subjects):
            impls = [t for t in e.descendants(subject) if e.role(t) not in _TEST_ROLES]
            if not impls:
                continue
            overriders = e.overriders(self.target.id) if subject == self.type.id else []
            entries = []
            for impl in impls[:MAX_IMPLEMENTATIONS]:
                node = e.nodes[impl]
                entry = {"id": impl, "name": node.name, "role": node.attrs.get("role"),
                         "type_kind": node.attrs.get("type_kind"),
                         "profiles": node.attrs.get("profiles") or [],
                         "generated": bool(node.attrs.get("generated"))}  # fmt: skip
                method = next((m for m in overriders if _owner_id(m) == impl), None)
                if method:
                    entry["method"] = e.label(method)
                entries.append(entry)
            items.append({"id": subject, "name": e.nodes[subject].name,
                          "implementations": entries, "count": len(impls)})  # fmt: skip
        return items

    def convention_scope(self, hits: list[_TestHit]) -> dict[str, Any]:
        """The test classes whose style to describe: the related ones, else the nearest by
        package. Their methods' texts are scanned for frameworks."""
        e = self.e
        types: list[str] = []
        for hit in hits:
            subclasses = [s["id"] for s in self._subclasses(hit.type_id)]
            types += [t for t in (*self._test_chain(hit.type_id), *subclasses) if t not in types]
        if not types:
            package = self.type.attrs.get("package") or ""
            tests = [n for n in e.nodes.values() if e.role(n.id) == "test"]
            tests.sort(key=lambda n: (-_common_prefix(package, n.attrs.get("package") or ""), n.id))
            types = [n.id for n in tests[:3]]
        methods = [
            m
            for t in types
            for m in e.contained(t, METHOD)
            if e.nodes[m].attrs.get("is_test")
            or _SETUP_ANNOTATIONS & set(e.nodes[m].attrs.get("annotations") or [])
        ]
        return {"types": types, "methods": methods[:MAX_SCOPE_METHODS]}

    def conventions(self, hits: list[_TestHit], scope: dict[str, Any]) -> dict[str, Any]:
        """Frameworks seen in the related tests (by annotations, field types and code), the
        repo's test class naming, and where the new test goes."""
        e = self.e
        units: list[tuple[set[str], set[str], str]] = []  # (annotations, field types, code)
        for type_id in scope["types"]:
            fields = e.nodes[type_id].attrs.get("fields") or []
            annotations = set(e.nodes[type_id].attrs.get("annotations") or [])
            annotations |= {a for f in fields for a in f.get("annotations") or []}
            units.append((annotations, {_simple_type(f.get("type") or "") for f in fields}, ""))
        for method in scope["methods"]:
            annotations = set(e.nodes[method].attrs.get("annotations") or [])
            units.append((annotations, set(), e.text(method) or ""))
        counts = {fw.name: sum(_uses(fw, *unit) for unit in units) for fw in _FRAMEWORKS}
        junit = self._junit(units)
        if junit:
            counts = {junit[0]: junit[1], **counts}
        frameworks = [{"name": name, "count": n} for name, n in counts.items() if n]
        frameworks.sort(key=lambda f: -f["count"])  # stable: the table's order breaks ties
        suffix, suffix_counts = self._test_suffix()
        path, advice = self._suggested_file(hits, suffix)
        return {
            "frameworks": frameworks,
            "scope": [e.nodes[t].name for t in scope["types"]],
            "test_class_suffix": suffix,
            "suffix_counts": suffix_counts,
            "suggested_test_file": path,
            "advice": advice,
        }

    def _junit(self, units: list[tuple[set[str], set[str], str]]) -> tuple[str, int] | None:
        """JUnit's version, from the annotations only one version has; when the test classes
        shown use only @Test, from the rest of the repo's tests."""
        found = [u for u in units if u[0] & (_JUNIT5 | _JUNIT4 | _JUNIT_ANY)]
        if not found:
            return None
        seen = set().union(*(u[0] for u in found)) & (_JUNIT5 | _JUNIT4)
        if not seen:  # @Test alone: either version. Test code only: AspectJ has a @Before too.
            seen = {
                a
                for n in self.e.nodes.values()
                if n.kind == METHOD and self.e.role(_owner_id(n.id)) in _TEST_ROLES
                for a in n.attrs.get("annotations") or []
                if a in _JUNIT5 | _JUNIT4
            }
        five, four = bool(seen & _JUNIT5), bool(seen & _JUNIT4)
        if five != four:
            return ("JUnit 5 (Jupiter)" if five else "JUnit 4"), len(found)
        return "JUnit (@Test)", len(found)

    def _test_suffix(self) -> tuple[str, dict[str, int]]:
        """The usual test class name ending in this repo: Tests, Test or IT."""
        counts = dict.fromkeys(_TEST_SUFFIXES, 0)
        for node in self.e.nodes.values():
            if node.attrs.get("role") == "test" and not node.attrs.get("outer"):  # a top-level test
                suffix = next((s for s in _TEST_SUFFIXES if node.name.endswith(s)), None)
                if suffix:
                    counts[suffix] += 1
        best = max(_TEST_SUFFIXES, key=lambda s: (counts[s], -_TEST_SUFFIXES.index(s)))
        return (best if counts[best] else "Tests"), {s: n for s, n in counts.items() if n}

    def _suggested_file(self, hits: list[_TestHit], suffix: str) -> tuple[str, str]:
        """Where the new test goes: the class that already tests this one by name, else a new
        file mirroring the package under src/test/java."""
        e = self.e
        top = self.type
        while top.attrs.get("outer") and f"type:{top.attrs['outer']}" in e.nodes:
            top = e.nodes[f"type:{top.attrs['outer']}"]
        owners = sorted(
            {x.source for x in e.edges_in(top.id, TESTS)
             if x.attrs.get("reason") in ("name", "annotation") and x.source in e.nodes},
            key=lambda t: (bool(e.nodes[t].attrs.get("is_abstract")), t),
        )  # fmt: skip
        if owners:
            node = e.nodes[owners[0]]
            return node.path or "", f"{node.name} already tests {top.name}: add the new tests to it"
        path = _mirror_test_path(top, suffix)
        advice = f"no test class for {top.name} yet: create {Path(path).name}"
        for hit in hits:
            base = e.nodes[hit.type_id]
            if hit.weight >= W_SUPER and base.attrs.get("is_abstract"):
                runs = len(self._subclasses(hit.type_id))
                under = f" and runs under {_count(runs, 'subclass')} (profiles)" if runs else ""
                advice += (f"; {base.name} tests what {top.name} implements{under}: add "
                           "integration tests there to cover every implementation")  # fmt: skip
                break
        return path, advice

    def related_docs(
        self, max_docs: int, deps: list[dict[str, Any]], endpoints: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Doc sections nearest to the target's name, signature and javadoc; a section that
        mentions the class, a dependency or an endpoint ranks a little higher."""
        if max_docs <= 0:
            return []
        e, node = self.e, self.target
        query = " ".join(filter(None, [
            self.type.name, node.name if self.is_method else None, node.attrs.get("signature"),
            (node.attrs.get("javadoc") or "")[:500], *(ep.get("summary") for ep in endpoints),
        ]))  # fmt: skip
        mentioned = {self.type.id, *(d["id"] for d in deps), *(ep["id"] for ep in endpoints)}
        try:
            results = e.vector_search(query, max_docs * 3, [DOC])
        except Exception as exc:  # e.g. the model can't be loaded offline: the rest still helps
            self.notes.append(f"related docs skipped: the embedding model failed "
                              f"({type(exc).__name__}: {exc})")  # fmt: skip
            return []
        scored = []
        for chunk, score in results:
            mentions = any(x.target in mentioned for x in e.edges_out(chunk.node_id, MENTIONS))
            scored.append((score + (0.05 if mentions else 0.0), chunk))
        scored.sort(key=lambda pair: (-pair[0], pair[1].node_id))
        e.load_texts(chunk.node_id for _, chunk in scored[:max_docs])
        docs = []
        for score, chunk in scored[:max_docs]:
            text, truncated = _cut(e.text(chunk.node_id) or chunk.text, MAX_DOC_CHARS)
            docs.append({"id": chunk.node_id, "label": e.label(chunk.node_id), "path": chunk.path,
                         "lines": [chunk.start_line, chunk.end_line], "score": round(score, 4),
                         "text": text, "truncated": truncated})  # fmt: skip
        return docs


def _uses(fw: _Framework, annotations: set[str], field_types: set[str], code: str) -> bool:
    return bool(annotations & fw.annotations or field_types & fw.field_types
                or (fw.pattern and code and fw.pattern.search(code)))  # fmt: skip


# ---- Markdown --------------------------------------------------------------------------------


def render_markdown(context: dict[str, Any]) -> str:
    """A test context as compact Markdown, for an LLM prompt or a terminal."""
    sections = (_md_target, _md_enclosing, _md_dependencies, _md_collaborators, _md_data_types,
                _md_endpoints, _md_tests, _md_implementations, _md_conventions, _md_docs,
                _md_notes)  # fmt: skip
    out = [line for section in sections for line in section(context)]
    return "\n".join(out).rstrip() + "\n"


def _md_target(context: dict[str, Any]) -> list[str]:
    t = context["target"]
    out = [f"# Test context: {t['label']}", ""]
    role = t.get("role") or "?"
    out.append(f"{t['kind']} `{t['id']}` · {role} · {_where(t['path'], t['lines'])}")
    if context.get("candidates"):
        out.append("Also matches: " + ", ".join(f"`{c['label']}`" for c in context["candidates"]))
    out += ["", "## Target", ""]
    if t["kind"] == METHOD:
        out.append(f"`{t.get('signature')}`")
    else:
        out.append(f"{t.get('type_kind')} {t.get('qualified_name')}{_profiles(t.get('profiles'))}")
    out += _code(t.get("source"), "java")
    for method in t.get("methods") or []:
        if method.get("source"):
            out += [f"`{method['label']}` ({_where(None, method['lines'])})"]
            out += _code(method["source"], "java")
    for method in t.get("methods") or []:
        if not method.get("source"):
            out.append(f"- `{method['signature']}` ({_where(None, method['lines'])})")
    return out


def _md_enclosing(context: dict[str, Any]) -> list[str]:
    c = context.get("enclosing_type")
    if not c:
        return []
    out = ["", f"## Enclosing class: {c['name']} ({c.get('role')})", ""]
    out.append(f"{_annotations(c['annotations'])}{_profiles(c['profiles'])} · "
               f"{_where(c['path'], c['lines'])}")  # fmt: skip
    return out + _code(c.get("outline"), "java")


def _md_dependencies(context: dict[str, Any]) -> list[str]:
    if not context["dependencies"]:
        return []
    out = ["", "## Dependencies to mock", ""]
    for d in context["dependencies"]:
        kind = "interface" if d["interface"] else d.get("type_kind") or "type"
        kind += f", {d['role']}" if d.get("role") not in (None, "other") else ""
        inherited = f", inherited from {d['inherited_from']}" if d.get("inherited_from") else ""
        line = f"- **{d['name']}** ({kind}; field `{d.get('field')}` via {d.get('via')}{inherited})"
        unused = " — not called by the target; needed to build the class"
        out.append(line if d["calls"] else line + unused)
        out += [f"  - calls `{call['signature']}`" for call in d["calls"]]
        if d.get("more_calls"):
            out.append(f"  - ... {d['more_calls']} more calls")
        usage, parts = d["in_tests"], []
        if usage["mocked_count"]:
            parts.append(f"mocked in {_count(usage['mocked_count'], 'test class')}, "
                         f"e.g. {', '.join(usage['mocked_in'][:2])}")  # fmt: skip
        if usage["injected_count"]:
            parts.append(f"injected for real in {_count(usage['injected_count'], 'test class')}, "
                         f"e.g. {', '.join(usage['injected_in'][:2])}")  # fmt: skip
        if parts:
            out.append("  - in tests: " + "; ".join(parts))
    return out


def _md_collaborators(context: dict[str, Any]) -> list[str]:
    if not context["collaborators"]:
        return []
    out = ["", "## Other calls", ""]
    for c in context["collaborators"]:
        where = "same class" if c["same_type"] else f"{c['owner']} ({c.get('owner_role')})"
        out.append(f"- `{c['label']}` — {where}, resolved by {c.get('resolution')}: "
                   f"`{c.get('signature')}`")  # fmt: skip
        if c.get("source"):
            out += _code(c["source"], "java", indent="  ")
    return out


def _md_data_types(context: dict[str, Any]) -> list[str]:
    if not context["data_types"]:
        return []
    out = ["", "## Data types", ""]
    for d in context["data_types"]:
        used = "; ".join(d["used_as"])
        schema = d.get("schema")
        if schema:
            out.append(f"- **{d['name']}** — generated from OpenAPI schema `{schema['name']}` "
                       f"({schema['path']}); {used}")  # fmt: skip
            required = set(schema["required"])
            props = ", ".join(f"{name}: {kind}{'*' if name in required else ''}"
                              for name, kind in schema["properties"].items())  # fmt: skip
            out.append(f"  - properties (* required): {props or '—'}")
            continue
        kind = f"{d.get('role')} {d.get('type_kind') or ''}".rstrip()
        out.append(f"- **{d['name']}** ({kind}); {used}")
        if d.get("generated"):
            out.append("  - generated at build time; no schema found")
        if d.get("fields"):
            more = f", ... {d['more_fields']} more" if d.get("more_fields") else ""
            out.append(f"  - fields: {', '.join(_field_text(f) for f in d['fields'])}{more}")
    return out


def _md_endpoints(context: dict[str, Any]) -> list[str]:
    if not context["endpoints"]:
        return []
    out = ["", "## Endpoints", ""]
    for ep in context["endpoints"]:
        operation = f" — operationId `{ep['operation_id']}`" if ep.get("operation_id") else ""
        summary = f": {ep['summary']}" if ep.get("summary") else ""
        out.append(f"- **{ep['name']}**{operation}{summary}")
        paths = []
        if ep.get("served_path"):
            paths.append(f"served at `{ep['served_path']}` (what MockMvc calls)")
        if ep.get("url_path"):
            paths.append(f"clients call `{ep['url_path']}`")
        if paths:
            out.append("  - " + "; ".join(paths))
        if ep["parameters"]:
            out.append("  - parameters: " + ", ".join(_parameter_text(p) for p in ep["parameters"]))
        if ep.get("request_body"):
            out.append(f"  - request body: {ep['request_body']}")
        if ep["responses"]:
            responses = " · ".join(f"{status} {what}" for status, what in ep["responses"].items())
            out.append(f"  - responses: {responses}")
        if ep["tested_by"]:
            more = ep["tested_by_count"] - len(ep["tested_by"])
            out.append("  - tested by: " + ", ".join(f"`{t}`" for t in ep["tested_by"])
                       + (f", ... {more} more" if more > 0 else ""))  # fmt: skip
    return out


def _md_tests(context: dict[str, Any]) -> list[str]:
    out = ["", "## Existing tests"]
    if not context["existing_tests"]:
        out += ["", "None found."]
    for test in context["existing_tests"]:
        abstract = " (abstract)" if test["is_abstract"] else ""
        out += ["", f"### {test['name']}{abstract} — {test['path']}", ""]
        out.append(f"- why: {'; '.join(test['why'])}")
        if test["annotations"]:
            profiles = _profiles(test["active_profiles"], "active profiles")
            out.append(f"- annotations: {_annotations(test['annotations'])}{profiles}")
        if test["extends"]:
            out.append(f"- extends: {', '.join(test['extends'])}")
        if test["setup_fields"]:
            fields = "; ".join(_field_text(f, annotations_first=True) for f in test["setup_fields"])
            out.append(f"- fields: {fields}")
        if test["mocks"]:
            mocks = ", ".join(f"{m['name']} ({_via(m.get('via') or '')})" for m in test["mocks"])
            out.append(f"- mocks: {mocks}")
        for sub in test["subclasses"]:
            profiles = _profiles(sub["active_profiles"], "active profiles")
            out.append(f"- subclass **{sub['name']}**{profiles} — {sub['path']}")
        if test.get("setup"):
            out += [f"- setup `{test['setup']['label']}`:"]
            out += _code(test["setup"]["source"], "java", indent="  ")
        for m in test["methods"]:
            out += [f"- `{m['label']}` ({m['why']}; {_where(m['path'], m['lines'])}):"]
            out += _code(m["source"], "java", indent="  ")
    return out


def _md_implementations(context: dict[str, Any]) -> list[str]:
    if not context["implementations"]:
        return []
    out = ["", "## Implementations", ""]
    for item in context["implementations"]:
        impls = ", ".join(
            i["name"] + (f" (`{i['method']}`)" if i.get("method") else "")
            + _profiles(i["profiles"])
            for i in item["implementations"]
        )
        more = item["count"] - len(item["implementations"])
        out.append(f"- {item['name']}: {impls}" + (f", ... {more} more" if more > 0 else ""))
    return out


def _md_conventions(context: dict[str, Any]) -> list[str]:
    conv = context["conventions"]
    out = ["", "## Conventions", ""]
    if conv["frameworks"]:
        seen = ", ".join(f"{f['name']} ({f['count']})" for f in conv["frameworks"])
        out.append(f"- seen in {', '.join(conv['scope'])}: {seen}")
    counts = ", ".join(f"*{s}: {n}" for s, n in conv["suffix_counts"].items())
    counts = f" ({counts})" if counts else ""
    out.append(f"- test classes are named *{conv['test_class_suffix']}{counts}")
    out.append(f"- suggested test file: `{conv['suggested_test_file']}` — {conv['advice']}")
    return out


def _md_docs(context: dict[str, Any]) -> list[str]:
    if not context["related_docs"]:
        return []
    out = ["", "## Related docs"]
    for doc in context["related_docs"]:
        where = _where(doc["path"], doc["lines"])
        out += ["", f"### {doc['label']} — {where} (score {doc['score']:.2f})", ""]
        out += _code(doc["text"], "markdown")
    return out


def _md_notes(context: dict[str, Any]) -> list[str]:
    if not context["notes"]:
        return []
    return ["", "## Notes", "", *(f"- {note}" for note in context["notes"])]


def _code(text: str | None, lang: str, indent: str = "") -> list[str]:
    """A fenced block, its fence longer than any backtick run inside (docs hold fences too)."""
    if not text:
        return [f"{indent}(no source indexed)"]
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    lines = [f"{fence}{lang}", *text.split("\n"), fence]
    return [f"{indent}{line}" if line else line for line in lines]


def _where(path: str | None, lines: list[int | None] | None) -> str:
    start, end = (lines or [None, None])[:2]
    span = "" if start is None else f":{start}" + (f"-{end}" if end and end != start else "")
    return f"{path or ''}{span}" or "?"


def _annotations(names: list[str]) -> str:
    return " ".join(f"@{n}" for n in names) or "no annotations"


def _profiles(profiles: list[str] | None, label: str = "profiles") -> str:
    return f" [{label}: {', '.join(profiles)}]" if profiles else ""


def _field_text(f: dict[str, Any], annotations_first: bool = False) -> str:
    annotations = " ".join(f"@{a}" for a in f.get("annotations") or [])
    inherited = f" (from {f['declared_in']})" if f.get("declared_in") else ""
    if annotations_first:
        return f"{annotations} {f.get('type')} {f.get('name')}{inherited}".lstrip()
    return f"{f.get('name')}: {f.get('type')} {annotations}".rstrip() + inherited


def _parameter_text(p: dict[str, Any]) -> str:
    required = ", required" if p.get("required") else ""
    return f"{p.get('name')} ({p.get('location')}{required}): {p.get('schema') or '?'}"


def _count(n: int, noun: str) -> str:
    """3 test classes, 1 subclass, 2 more dependencies."""
    if n == 1:
        return f"{n} {noun}"
    if noun.endswith("y") and noun[-2:-1] not in ("a", "e", "o", "u"):
        return f"{n} {noun[:-1]}ies"
    return f"{n} {noun}{'es' if noun.endswith('s') else 's'}"


def _via(via: str) -> str:
    """How a test mocks: "MockitoBean" -> "@MockitoBean"; "mock()" stays as is."""
    return via if via.endswith(")") or not via else f"@{via}"


# ---- helpers ---------------------------------------------------------------------------------


def _owner_id(method_id: str) -> str:
    """method:org.example.Owner#getPet(String) -> type:org.example.Owner"""
    return "type:" + method_id[len("method:") :].split("#", 1)[0]


def _param_types(method_id: str) -> list[str]:
    """The erased parameter types in a method id."""
    name_part = method_id.split("#", 1)[-1]
    inner = name_part[name_part.find("(") + 1 : name_part.rfind(")")] if "(" in name_part else ""
    return [p for p in inner.split(",") if p]


def _split_params(text: str) -> tuple[str, list[str] | None]:
    """"Type#m(int, List<String> xs)" -> ("Type#m", ["int", "List"]); no parentheses -> None."""
    if not (text.endswith(")") and "(" in text):
        return text.strip(), None
    inner = text[text.index("(") + 1 : -1]
    while _GENERIC.search(inner):
        inner = _GENERIC.sub("", inner)
    params = [_erase_param(p) for p in inner.split(",") if p.strip()]
    return text[: text.index("(")].strip(), params


def _erase_param(text: str) -> str:
    """"final java.lang.String... names" -> "String[]", as in method ids."""
    text = re.sub(r"\s+\[", "[", _ANNOTATION.sub(" ", text).replace("...", "[] "))
    words = [w for w in text.split() if w != "final"]
    if not words:
        return ""
    return words[0].replace("[]", "").rsplit(".", 1)[-1] + "[]" * words[0].count("[]")


def _by_params(ids: list[str], params: list[str] | None) -> list[str]:
    """The overloads with exactly these parameter types, else all of them."""
    if params is None:
        return ids
    return [i for i in ids if _param_types(i) == params] or ids


def _chunk_parts(chunk_ids: Iterable[str], nodes: dict[str, Node]) -> dict[str, list[str]]:
    """node id -> its chunk ids in order: a long node is split into "<id>#part2"..."""
    parts: dict[str, list[tuple[int, str]]] = {}
    for chunk_id in chunk_ids:
        node_id, number = chunk_id, 1
        match = _PART.search(chunk_id)
        if match and chunk_id not in nodes and chunk_id[: match.start()] in nodes:
            node_id, number = chunk_id[: match.start()], int(match.group(1))
        parts.setdefault(node_id, []).append((number, chunk_id))
    return {node_id: [c for _, c in sorted(items)] for node_id, items in parts.items()}


def _field(f: dict[str, Any], declared_in: str | None) -> dict[str, Any]:
    item = {"name": f.get("name"), "type": f.get("type"), "annotations": f.get("annotations") or []}
    return {**item, "declared_in": declared_in} if declared_in else item


def _trim_outline(outline: str, signatures: list[str], hint: str) -> str:
    """An outline without the member lines of these signatures, and a line saying how many
    went (member lines are "    @A @B <signature>;")."""
    endings = {f"{sig};" for sig in signatures if sig}
    lines: list[str] = []
    dropped, marker = 0, None
    for line in outline.split("\n"):
        member = line.startswith("    ")
        if member and any(line.endswith(" " + end) or line.strip() == end for end in endings):
            dropped += 1
            if marker is None:
                marker = len(lines)
                lines.append("")
            continue
        lines.append(line)
    if marker is None:
        return outline
    lines[marker] = f"    // ... {_count(dropped, 'more method')}: {hint}"
    return "\n".join(lines)


def _cut(text: str | None, limit: int) -> tuple[str | None, bool]:
    """At most `limit` characters, cut at a line end, saying how much was left out."""
    if text is None or len(text) <= limit:
        return text, False
    end = text.rfind("\n", 0, limit)
    if end < limit // 2:
        end = limit
    left = len(text) - end
    return f"{text[:end]}\n... ({left} more characters: get_node or `tcg show` has them)", True


def _snippet(text: str, lines: int = 3, chars: int = 300) -> str:
    kept = [line.strip() for line in text.split("\n") if line.strip()][:lines]
    snippet = " ⏎ ".join(kept)
    return snippet if len(snippet) <= chars else snippet[: chars - 3] + "..."


def _lines(node: Node) -> list[int | None]:
    return [node.start_line, node.end_line]


def _name_tokens(name: str) -> set[str]:
    """Word parts of an identifier, singular, minus test words and prepositions:
    testGetOwnersSuccess -> get, owner, success."""
    tokens = {_singular(t) for t in tokenize(name)}
    return {t for t in tokens if t not in _NAME_STOP_TOKENS and len(t) > 1}


def _singular(token: str) -> str:
    plural = len(token) > 3 and token.endswith("s") and not token.endswith("ss")
    return token[:-1] if plural else token


def _strip_role_words(name: str) -> str:
    """OwnerRestControllerV1 -> Owner: the words that tell one class's tests from another's."""
    return _ROLE_WORDS.sub("", name) or name


def _verb(reason: str) -> str:
    return {"http": "call", "call": "call", "new": "create"}.get(reason, reason)


def _simple_type(text: str) -> str:
    return _GENERIC.sub("", text).strip().rsplit(".", 1)[-1]


def _common_prefix(a: str, b: str) -> int:
    count = 0
    for x, y in zip(a.split("."), b.split(".")):
        if x != y:
            break
        count += 1
    return count


def _mirror_test_path(type_node: Node, suffix: str) -> str:
    """src/main/java/<pkg>/Owner.java -> src/test/java/<pkg>/Owner<suffix>.java (a module
    prefix such as api/ is kept)."""
    name = f"{type_node.name.split('.')[0]}{suffix}.java"
    package_dir = (type_node.attrs.get("package") or "").replace(".", "/")
    path = type_node.path or ""
    marker = "src/main/java/"
    prefix = path[: path.index(marker)] if marker in path else ""
    return f"{prefix}src/test/java/{package_dir}/{name}".replace("//", "/")


def _check_kinds(kinds: list[str] | None, allowed: tuple[str, ...], what: str) -> list[str] | None:
    if not kinds:
        return None
    unknown = [k for k in kinds if k not in allowed]
    if unknown:
        names = ", ".join(map(repr, unknown))
        raise ContextError(f"unknown {what} {names}: use {', '.join(allowed)}")
    return list(dict.fromkeys(kinds))


def _age_seconds(generated_at: str | None) -> int | None:
    if not generated_at:
        return None
    try:
        then = datetime.fromisoformat(generated_at)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - then).total_seconds()))


def _age_text(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count > 1 else ''} ago"
    return "just now"
