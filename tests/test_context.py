"""context.py on an index of the made-up bookshop app (tests/fixtures/bookshop), built once per
module with HashEmbedder: resolving symbols, the test context and its parts, search, nodes,
neighbours, tests and stats."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from test_context_graph import context as ctx_module
from test_context_graph.context import (
    ContextEngine,
    ContextError,
    _chunk_parts,
    _code,
    _count,
    _cut,
    _name_tokens,
    _split_params,
    _trim_outline,
    embedder_for_model,
    render_markdown,
)
from test_context_graph.embeddings import FastEmbedEmbedder, HashEmbedder
from test_context_graph.indexer import index_repo
from test_context_graph.models import Node

BOOKSHOP = Path(__file__).parent / "fixtures" / "bookshop"
P = "com.example.bookshop"
GET_BOOK = f"method:{P}.rest.controller.BookRestController#getBook(Integer)"
FIND_BOOK_IMPL = f"method:{P}.service.BookServiceImpl#findBook(int)"
FIND_BOOK_API = f"method:{P}.service.BookService#findBook(int)"
CONTROLLER_TESTS = "src/test/java/com/example/bookshop/rest/controller/BookRestControllerTests.java"


@pytest.fixture(scope="module")
def index_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("context")
    repo = Path(shutil.copytree(BOOKSHOP, root / "bookshop"))
    index_repo(repo, root / "index", HashEmbedder())
    return root / "index"


@pytest.fixture(scope="module")
def engine(index_dir: Path) -> ContextEngine:
    return ContextEngine(index_dir)


@pytest.fixture(scope="module")
def get_book(engine: ContextEngine) -> dict:
    return engine.test_context("BookRestController#getBook", max_docs=2)


@pytest.fixture(scope="module")
def find_book(engine: ContextEngine) -> dict:
    return engine.test_context("BookServiceImpl#findBook", max_docs=0)


def ids(nodes: list[Node]) -> list[str]:
    return [node.id for node in nodes]


# ---- loading ---------------------------------------------------------------------------------


def test_a_missing_index_says_to_run_tcg_index(tmp_path):
    with pytest.raises(ContextError, match=r"run `tcg index <repo>`"):
        ContextEngine(tmp_path / "nothing-here")


def test_the_embedder_matches_the_index(index_dir):
    engine = ContextEngine(index_dir)
    assert engine._embedder is None  # not created until vector search needs it
    assert isinstance(engine.embedder, HashEmbedder) and engine.embedder.dim == 256


def test_embedder_for_model_names():
    assert embedder_for_model("hash-64").dim == 64
    fastembed = embedder_for_model("BAAI/bge-small-en-v1.5")
    assert isinstance(fastembed, FastEmbedEmbedder) and fastembed._model is None  # not loaded


def test_a_given_embedder_is_used(index_dir):
    mine = HashEmbedder(256)
    assert ContextEngine(index_dir, embedder=mine).embedder is mine


# ---- resolve -------------------------------------------------------------------------------


def test_resolve_a_node_id(engine):
    assert ids(engine.resolve(GET_BOOK)) == [GET_BOOK]
    assert ids(engine.resolve("endpoint:GET /books")) == ["endpoint:GET /books"]


def test_resolve_an_id_without_its_kind_prefix(engine):
    assert ids(engine.resolve(f"{P}.model.Book")) == [f"type:{P}.model.Book"]
    schema = "src/main/resources/openapi.yml#Book"
    assert ids(engine.resolve(schema)) == [f"schema:{schema}"]


def test_resolve_a_chunk_part_id_to_its_node(engine):
    assert ids(engine.resolve(f"{GET_BOOK}#part2")) == [GET_BOOK]


def test_resolve_type_names(engine):
    book_service = [f"type:{P}.service.BookService"]
    assert ids(engine.resolve("BookService")) == book_service
    assert ids(engine.resolve("service.BookService")) == book_service  # a dotted suffix
    assert ids(engine.resolve("bookservice")) == book_service  # any case, as a last resort


@pytest.mark.parametrize(
    "symbol",
    ["BookServiceImpl#findBook", "BookServiceImpl.findBook", "BookServiceImpl#findBook(int)",
     f"{P}.service.BookServiceImpl#findBook", f"{P}.service.BookServiceImpl.findBook(int bookId)"],
)  # fmt: skip
def test_resolve_type_and_method(engine, symbol):
    assert ids(engine.resolve(symbol)) == [FIND_BOOK_IMPL]


def test_resolve_a_bare_method_name_ranks_the_implementation_first(engine):
    assert ids(engine.resolve("findBook")) == [FIND_BOOK_IMPL, FIND_BOOK_API]


def test_resolve_wrong_parameters_fall_back_to_the_overloads(engine):
    assert ids(engine.resolve("BookServiceImpl#findBook(String)")) == [FIND_BOOK_IMPL]


def test_resolve_an_inherited_method(engine):
    found = engine.resolve("BookServiceJdbcTests#findBooksIsNeverNull")
    assert ids(found) == [f"method:{P}.service.AbstractBookServiceTests#findBooksIsNeverNull()"]


def test_resolve_a_generated_api_method_through_its_implementation(engine):
    assert ids(engine.resolve("BooksApi#getBook")) == [GET_BOOK]


def test_resolve_a_file_path_to_its_types(engine):
    impl = [f"type:{P}.service.BookServiceImpl"]
    assert ids(engine.resolve("src/main/java/com/example/bookshop/service/BookServiceImpl.java")) == impl
    assert ids(engine.resolve("BookServiceImpl.java")) == impl
    spec = "file:src/main/resources/openapi.yml"
    assert ids(engine.resolve(spec)) == [spec]  # a file node id is the file itself


@pytest.mark.parametrize(
    "symbol", ["GET /books/{bookId}", "GET /books/{id}", "get /api/books/{bookId}",
               "/bookshop/api/books/7", "GET /api/books/42?full=true"],
)  # fmt: skip
def test_resolve_an_endpoint(engine, symbol):
    assert ids(engine.resolve(symbol)) == ["endpoint:GET /books/{bookId}"]


def test_resolve_nothing(engine):
    assert engine.resolve("NoSuchThing") == []
    assert engine.resolve("   ") == []


def test_pick_suggests_close_names(engine):
    with pytest.raises(ContextError, match=r"did you mean .*getBook"):
        engine.pick("getBok")


def test_split_params():
    assert _split_params("T#m(final java.util.List<String> xs, int... ns, @Valid Book b)") == (
        "T#m", ["List", "int[]", "Book"])  # fmt: skip
    assert _split_params("T#m()") == ("T#m", [])
    assert _split_params("T#m") == ("T#m", None)


# ---- the test context of a controller method ------------------------------------------------


def test_context_target(get_book):
    target = get_book["target"]
    assert target["id"] == GET_BOOK and target["kind"] == "method" and target["role"] == "controller"
    assert target["signature"] == "public ResponseEntity<BookDto> getBook(Integer bookId)"
    assert target["path"].endswith("BookRestController.java") and target["lines"] == [29, 36]
    assert "bookService.findBook(bookId)" in target["source"] and not target["truncated"]


def test_context_enclosing_type(get_book):
    enclosing = get_book["enclosing_type"]
    assert enclosing["name"] == "BookRestController" and enclosing["role"] == "controller"
    assert enclosing["annotations"] == ["RestController", "RequestMapping"]
    assert '@RequestMapping("api")' in enclosing["outline"]


def test_context_dependencies_with_the_methods_called_on_them(get_book):
    deps = {d["name"]: d for d in get_book["dependencies"]}
    assert list(deps) == ["BookService", "BookMapper"]  # in the order the target calls them
    service = deps["BookService"]
    assert service["interface"] and service["field"] == "bookService" and service["via"] == "constructor"
    assert [c["id"] for c in service["calls"]] == [FIND_BOOK_API]
    assert service["calls"][0]["signature"] == "Book findBook(int bookId)"
    assert service["in_tests"]["mocked_in"] == ["BookRestControllerTests (@MockitoBean)"]
    assert [c["label"] for c in deps["BookMapper"]["calls"]] == ["BookMapper#toBookDto(Book)"]


def test_context_generated_dto_comes_with_its_schema_fields(get_book):
    dto = next(d for d in get_book["data_types"] if d["name"] == "BookDto")
    assert dto["generated"] and dto["role"] == "dto"
    assert dto["schema"]["name"] == "Book"
    assert dto["schema"]["properties"] == {"id": "integer", "title": "string", "author": "string"}
    assert dto["schema"]["required"] == ["title"]
    assert "return of getBook" in dto["used_as"]
    assert any("BookDto is generated at build time from schema Book" in n for n in get_book["notes"])


def test_context_data_types_include_what_mocked_calls_return(get_book):
    book = next(d for d in get_book["data_types"] if d["name"] == "Book")
    assert "return of BookService#findBook(int)" in book["used_as"]
    fields = [(f["name"], f["type"]) for f in book["fields"]]
    assert fields == [("id", "Integer"), ("title", "String"), ("author", "String")]


def test_context_endpoints(get_book):
    (endpoint,) = get_book["endpoints"]
    assert endpoint["id"] == "endpoint:GET /books/{bookId}"
    assert endpoint["served_path"] == "/api/books/{bookId}"
    assert endpoint["url_path"] == "/bookshop/api/books/{bookId}"
    assert endpoint["operation_id"] == "getBook" and endpoint["responses"]["404"] == "No such book"
    assert endpoint["parameters"][0]["name"] == "bookId"
    assert endpoint["tested_by"] == ["BookRestControllerTests#getBookReturnsNotFound()",
                                     "BookRestControllerTests#getBookReturnsTheBook()"]  # fmt: skip


def test_context_existing_tests(get_book):
    test = get_book["existing_tests"][0]
    assert test["name"] == "BookRestControllerTests" and test["path"] == CONTROLLER_TESTS
    assert any("GET /books/{bookId}" in why for why in test["why"])
    fields = {f["name"]: f for f in test["setup_fields"]}
    assert fields["bookService"]["annotations"] == ["MockitoBean"]
    assert fields["bookRestController"]["annotations"] == ["Autowired"]
    mock = {"id": f"type:{P}.service.BookService", "name": "BookService", "via": "MockitoBean"}
    assert test["mocks"] == [mock]
    assert "standaloneSetup(bookRestController)" in test["setup"]["source"]
    methods = [m["label"] for m in test["methods"]]
    assert methods == ["BookRestControllerTests#getBookReturnsTheBook()",
                       "BookRestControllerTests#getBookReturnsNotFound()"]  # fmt: skip
    assert "willReturn(book)" in test["methods"][0]["source"]


def test_context_conventions(get_book):
    conventions = get_book["conventions"]
    names = [f["name"] for f in conventions["frameworks"]]
    for expected in ("JUnit 5 (Jupiter)", "MockMvc", "BDDMockito given(...).willReturn(...)",
                     "@MockitoBean mocks in the Spring context", "@SpringBootTest"):  # fmt: skip
        assert expected in names
    assert conventions["test_class_suffix"] == "Tests"
    assert conventions["suggested_test_file"] == CONTROLLER_TESTS
    assert "add the new tests to it" in conventions["advice"]


def test_context_related_docs(get_book):
    docs = get_book["related_docs"]
    assert len(docs) == 2
    assert docs[0]["id"] == "doc:README.md#finding-books"
    assert docs[0]["label"] == "Bookshop > Finding books" and "BookRepository" in docs[0]["text"]
    assert docs[0]["score"] >= docs[1]["score"]


def test_no_docs_means_no_embedder(index_dir):
    engine = ContextEngine(index_dir)
    assert engine.test_context("BookRestController#getBook", max_docs=0)["related_docs"] == []
    assert engine._embedder is None


# ---- the test context of a service method ---------------------------------------------------


def test_service_context_dependency_and_its_implementations(find_book):
    (dep,) = find_book["dependencies"]
    assert dep["name"] == "BookRepository"
    assert [c["label"] for c in dep["calls"]] == ["BookRepository#findById(int)"]
    (impls,) = find_book["implementations"]
    assert impls["name"] == "BookRepository"
    found = [(i["name"], i["profiles"]) for i in impls["implementations"]]
    assert found == [("JdbcBookRepository", ["jdbc"])]


def test_service_context_same_class_helper_with_its_source(find_book):
    (helper,) = find_book["collaborators"]
    assert helper["label"] == "BookServiceImpl#find(Supplier)" and helper["same_type"]
    assert helper["resolution"] == "same_type" and "lookup.get()" in helper["source"]


def test_service_context_finds_the_abstract_test_of_its_interface(find_book):
    (test,) = find_book["existing_tests"]
    assert test["name"] == "AbstractBookServiceTests" and test["is_abstract"]
    assert test["why"] == ["tests BookService (by name), a supertype of BookServiceImpl"]
    subclasses = [(s["name"], s["active_profiles"]) for s in test["subclasses"]]
    assert subclasses == [("BookServiceJdbcTests", ["jdbc"])]
    assert [m["label"] for m in test["methods"]] == ["AbstractBookServiceTests#findBooksIsNeverNull()"]
    frameworks = [f["name"] for f in find_book["conventions"]["frameworks"]]
    assert "AssertJ assertThat(...).isEqualTo(...)" in frameworks
    # The abstract test only has @Test; the repo's other tests (@BeforeEach) tell the version.
    assert "JUnit 5 (Jupiter)" in frameworks


def test_service_context_suggests_a_new_test_file(find_book):
    conventions = find_book["conventions"]
    expected = "src/test/java/com/example/bookshop/service/BookServiceImplTests.java"
    assert conventions["suggested_test_file"] == expected
    assert "AbstractBookServiceTests" in conventions["advice"] and "1 subclass " in conventions["advice"]


def test_an_abstract_method_points_to_its_implementations(engine):
    context = engine.test_context("BookService#findBook", max_docs=0)
    assert any("is abstract: test it through its implementations (BookServiceImpl#findBook(int))" in n
               for n in context["notes"])  # fmt: skip
    (impls,) = [i for i in context["implementations"] if i["name"] == "BookService"]
    assert impls["implementations"][0]["method"] == "BookServiceImpl#findBook(int)"


# ---- other targets -------------------------------------------------------------------------


def test_a_class_target_covers_all_its_methods(engine):
    context = engine.test_context("BookRestController", max_docs=0)
    target = context["target"]
    assert target["kind"] == "type" and target["type_kind"] == "class"
    assert [m["label"] for m in target["methods"]] == [
        "BookRestController#<init>(BookService,BookMapper)", "BookRestController#getBook(Integer)",
        "BookRestController#listBooks()"]  # fmt: skip
    assert all(m.get("source") for m in target["methods"])
    assert context["enclosing_type"] is None
    assert {e["name"] for e in context["endpoints"]} == {"GET /books", "GET /books/{bookId}"}
    service = next(d for d in context["dependencies"] if d["name"] == "BookService")
    calls = {c["label"] for c in service["calls"]}
    assert calls == {"BookService#findBook(int)", "BookService#findBooks()"}


def test_an_endpoint_target_is_its_serving_method(engine):
    context = engine.test_context("GET /api/books/{bookId}", max_docs=0)
    assert context["target"]["id"] == GET_BOOK
    assert "GET /books/{bookId} is served by BookRestController#getBook(Integer)" in context["notes"]


def test_an_ambiguous_symbol_is_reported(engine):
    context = engine.test_context("findBook", max_docs=0)
    assert context["target"]["id"] == FIND_BOOK_IMPL
    assert context["candidates"] == [{"id": FIND_BOOK_API, "label": "BookService#findBook(int)"}]
    assert any("'findBook' matches 2 symbols" in note for note in context["notes"])


def test_a_schema_has_no_test_context(engine):
    with pytest.raises(ContextError, match="is a schema"):
        engine.test_context("schema:src/main/resources/openapi.yml#Book")


def test_unknown_symbol(engine):
    with pytest.raises(ContextError, match="no symbol matches 'Nope'"):
        engine.test_context("Nope")


def test_untested_code_says_so(engine):
    context = engine.test_context("JdbcBookRepository#findAll", max_docs=0)
    assert context["existing_tests"] == []
    assert any("no existing tests found" in note for note in context["notes"])


# ---- bounds and rendering ------------------------------------------------------------------


def test_max_tests_bounds_the_test_classes(engine):
    context = engine.test_context("BookRestController#getBook", max_tests=0, max_docs=0)
    assert context["existing_tests"] == []
    assert any("1 more related test class: find_tests or `tcg tests" in n for n in context["notes"])


def test_long_sources_are_cut(index_dir, monkeypatch):
    monkeypatch.setattr(ctx_module, "MAX_SOURCE_CHARS", 60)
    context = ContextEngine(index_dir).test_context("BookRestController#getBook", max_docs=0)
    target = context["target"]
    assert target["truncated"] and "more characters" in target["source"]
    assert len(target["source"].split("\n... (")[0]) <= 60


def test_count():
    assert [_count(1, "subclass"), _count(4, "subclass"), _count(2, "more dependency")] == [
        "1 subclass", "4 subclasses", "2 more dependencies"]  # fmt: skip
    assert _count(3, "test class") == "3 test classes" and _count(2, "day") == "2 days"


def test_cut():
    assert _cut("short", 10) == ("short", False)
    text, truncated = _cut("line one\nline two\nline three", 20)
    assert truncated and text.startswith("line one\nline two\n...")


def test_the_enclosing_outline_of_a_big_class_is_trimmed(index_dir, monkeypatch):
    monkeypatch.setattr(ctx_module, "MAX_OUTLINE_METHODS", 1)
    context = ContextEngine(index_dir).test_context("BookRestController#getBook", max_docs=0)
    outline = context["enclosing_type"]["outline"]
    assert "public BookRestController(BookService bookService, BookMapper bookMapper);" in outline
    assert "getBook(Integer bookId);" in outline and "listBooks()" not in outline
    hint = f"get_node or `tcg show type:{P}.rest.controller.BookRestController`"
    assert f"// ... 1 more method: {hint}" in outline


def test_docs_are_skipped_when_the_model_fails(index_dir, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no network to download the model")

    engine = ContextEngine(index_dir)
    monkeypatch.setattr(engine, "vector_search", broken)
    context = engine.test_context("BookRestController#getBook")
    assert context["related_docs"] == [] and context["existing_tests"]
    assert any(n.startswith("related docs skipped: the embedding model failed (OSError")
               for n in context["notes"])  # fmt: skip


def test_a_big_class_outline_keeps_what_the_target_needs():
    head = "class A {\n    int x;\n\n    A();\n    void a();\n"
    outline = head + "    void b();\n    @Override void c();\n}"
    trimmed = _trim_outline(outline, ["void b()", "void c()"], "see A")
    assert trimmed == head + "    // ... 2 more methods: see A\n}"


def test_render_markdown(get_book):
    md = render_markdown(get_book)
    assert md.startswith("# Test context: BookRestController#getBook(Integer)\n")
    for heading in ("## Target", "## Enclosing class: BookRestController", "## Dependencies to mock",
                    "## Data types", "## Endpoints", "## Existing tests", "## Conventions",
                    "## Related docs", "## Notes"):  # fmt: skip
        assert f"\n{heading}" in md
    assert "```java\n@Override\npublic ResponseEntity<BookDto> getBook(Integer bookId) {" in md
    assert "served at `/api/books/{bookId}`" in md
    assert "@MockitoBean BookService bookService" in md
    assert "title: string*" in md  # required schema properties are starred


def test_code_fences_outgrow_the_text():
    assert _code("a ```b``` c", "markdown") == ["````markdown", "a ```b``` c", "````"]
    assert _code(None, "java") == ["(no source indexed)"]


def test_name_tokens():
    assert {"get", "owner", "success"} <= _name_tokens("testGetOwnersSuccess")
    assert not {"test", "should", "by"} & _name_tokens("shouldFindOwnerByIdTest")


def test_chunk_parts_join_split_chunks():
    nodes = {i: Node(i, "doc", "x") for i in ("method:A#f()", "doc:x.md#part2")}
    chunk_ids = ["method:A#f()#part3", "method:A#f()", "method:A#f()#part2", "doc:x.md#part2"]
    parts = _chunk_parts(chunk_ids, nodes)
    assert parts == {"method:A#f()": ["method:A#f()", "method:A#f()#part2", "method:A#f()#part3"],
                     "doc:x.md#part2": ["doc:x.md#part2"]}  # fmt: skip


# ---- search, nodes, neighbours, tests, stats --------------------------------------------------


def test_search(engine):
    hits = engine.search("find a book by its id")
    assert hits and len({h["id"] for h in hits}) == len(hits)  # one hit per node
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    assert set(hits[0]) == {"id", "kind", "name", "path", "lines", "score", "snippet"}


def test_search_by_kind_and_limit(engine):
    hits = engine.search("book by id", kinds=["endpoint"], limit=1)
    assert len(hits) == 1 and hits[0]["kind"] == "endpoint" and hits[0]["name"] == "GET /books/{bookId}"


def test_search_rejects_bad_input(engine):
    with pytest.raises(ContextError, match="unknown kind 'class'"):
        engine.search("book", kinds=["class"])
    with pytest.raises(ContextError, match="empty"):
        engine.search("  ")


def test_get_node(engine):
    info = engine.get_node("BookRestController#getBook")
    assert info["node"]["id"] == GET_BOOK and info["label"] == "BookRestController#getBook(Integer)"
    assert "bookMapper.toBookDto(book)" in info["text"] and not info["truncated"]
    assert info["edges"]["out"] == {"CALLS": 2, "USES": 1, "SERVES": 1}
    assert info["edges"]["in"] == {"CONTAINS": 1}
    assert info["candidates"] == []


def test_neighbors(engine):
    result = engine.neighbors(f"type:{P}.service.BookService", ["implements"], "in")
    assert [(n["id"], n["edge"], n["direction"]) for n in result["neighbors"]] == [
        (f"type:{P}.service.BookServiceImpl", "IMPLEMENTS", "in")]  # fmt: skip
    capped = engine.neighbors("BookRestController", limit=2)  # a symbol works too
    assert capped["total"] > 2 and capped["truncated"] and len(capped["neighbors"]) == 2


def test_neighbors_rejects_bad_input(engine):
    with pytest.raises(ContextError, match="direction"):
        engine.neighbors(GET_BOOK, direction="up")
    with pytest.raises(ContextError, match="unknown edge kind"):
        engine.neighbors(GET_BOOK, edge_kinds=["LIKES"])


def test_find_tests_of_an_interface(engine):
    result = engine.find_tests("BookService")
    test = result["tests"][0]
    assert test["name"] == "AbstractBookServiceTests" and test["is_abstract"]
    assert "tests BookService (by name)" in test["why"]
    assert [s["name"] for s in test["subclasses"]] == ["BookServiceJdbcTests"]


def test_find_tests_of_a_method_lists_the_linked_test_methods(engine):
    result = engine.find_tests(GET_BOOK)
    (test,) = result["tests"]
    assert test["name"] == "BookRestControllerTests"
    assert [(m["label"], m["why"]) for m in test["methods"]] == [
        ("BookRestControllerTests#getBookReturnsTheBook()", "call GET /books/{bookId}"),
        ("BookRestControllerTests#getBookReturnsNotFound()", "call GET /books/{bookId}")]  # fmt: skip


def test_find_tests_of_untested_code(engine):
    result = engine.find_tests("JdbcBookRepository#findAll")
    assert result["tests"] == [] and "no existing tests found" in result["notes"][-1]


def test_stats(engine):
    stats = engine.stats()
    assert stats["repo_name"] == "bookshop" and stats["embedding_model"] == "hash-256"
    assert stats["embedding_dim"] == 256 and stats["counts"]["nodes"] == 56
    assert 0 <= stats["age_seconds"] < 3600
    assert stats["age"] == "just now" or stats["age"].endswith("ago")
