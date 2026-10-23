"""java_parser.parse_java on small made-up Spring-style fixtures (tests/fixtures/java)."""

from __future__ import annotations

from pathlib import Path

import pytest

from test_context_graph.java_parser import parse_java
from test_context_graph.models import (
    CallSite,
    JavaFile,
    MethodDecl,
    TypeDecl,
    TypeRef,
    from_dict,
    to_dict,
)

FIXTURES = Path(__file__).parent / "fixtures" / "java"


def parse_fixture(name: str) -> JavaFile:
    return parse_java((FIXTURES / name).read_text(encoding="utf-8"), f"src/main/java/{name}")


def line_of(name: str, text: str) -> int:
    """The 1-based line of the fixture's first line containing `text`."""
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return next(i for i, line in enumerate(lines, start=1) if text in line)


def type_named(java_file: JavaFile, name: str) -> TypeDecl:
    return next(t for t in java_file.types if t.name == name)


def method_named(type_decl: TypeDecl, name: str) -> MethodDecl:
    return next(m for m in type_decl.methods if m.name == name)


def call_named(method: MethodDecl, name: str) -> CallSite:
    return next(c for c in method.calls if c.name == name)


def snippet_method(body: str, members: str = "") -> MethodDecl:
    """The method `m` of a made-up class with `members`, whose body is `body`."""
    source = f"package org.example;\nclass Snippet {{\n{members}\nvoid m() {{\n{body}\n}}\n}}\n"
    java_file = parse_java(source, "Snippet.java")
    assert java_file.errors == []
    return method_named(java_file.types[0], "m")


# ---- file level --------------------------------------------------------------------------


def test_package_and_imports_including_static_and_wildcard():
    tests = parse_fixture("AbstractLoanControllerTests.java")
    controller = parse_fixture("LoanController.java")

    assert tests.package == "org.example.library.web"
    imports = {i.name: i for i in tests.imports}
    assert imports["org.mockito.BDDMockito.given"].static
    assert not imports["org.mockito.BDDMockito.given"].wildcard
    builders = imports["org.springframework.test.web.servlet.request.MockMvcRequestBuilders"]
    assert builders.static and builders.wildcard
    web = next(i for i in controller.imports if i.name == "org.springframework.web.bind.annotation")
    assert web.wildcard and not web.static
    assert any(i.name == "tools.jackson.databind.json.JsonMapper" for i in controller.imports)


def test_package_info_has_a_package_and_no_types():
    java_file = parse_fixture("package-info.java")

    assert java_file.package == "org.example.library.model"
    assert java_file.types == [] and java_file.imports == [] and java_file.errors == []


def test_str_and_bytes_with_a_bom_give_the_same_result():
    source = (FIXTURES / "LoanMapper.java").read_text(encoding="utf-8")

    from_bytes = parse_java(b"\xef\xbb\xbf" + source.encode("utf-8"), "LoanMapper.java")

    assert from_bytes == parse_java(source, "LoanMapper.java")
    assert from_bytes.path == "LoanMapper.java"


# ---- types -------------------------------------------------------------------------------


def test_controller_type_with_annotations_javadoc_and_lines():
    controller = type_named(parse_fixture("LoanController.java"), "LoanController")

    assert controller.kind == "class"
    assert controller.qualified_name == "org.example.library.web.LoanController"
    assert controller.modifiers == ["public"]
    assert [(a.name, a.arguments) for a in controller.annotations] == [
        ("RestController", {}),
        ("CrossOrigin", {"exposedHeaders": "errors, content-type"}),
        ("RequestMapping", {"value": "api"}),
    ]
    assert controller.javadoc == "REST endpoints for loans.\n\n@author Someone"
    # The annotations are part of the declaration; the javadoc is not.
    assert controller.start_line == line_of("LoanController.java", "@RestController")
    assert controller.end_line == len((FIXTURES / "LoanController.java").read_text().splitlines())
    assert controller.outer is None


def test_extends_and_implements():
    service = type_named(parse_fixture("LoanServiceImpl.java"), "LoanServiceImpl")
    isbn = type_named(parse_fixture("Isbn.java"), "Isbn")

    assert [t.name for t in service.extends] == ["AbstractAuditedService"]
    assert [t.name for t in service.implements] == ["LoanService"]
    assert isbn.implements == [
        TypeRef("Comparable<Isbn>", "Comparable", [TypeRef("Isbn", "Isbn")])
    ]


def test_interface_with_constant_abstract_and_default_methods():
    mapper = type_named(parse_fixture("LoanMapper.java"), "LoanMapper")

    assert mapper.kind == "interface"
    assert [(f.name, f.type.name, f.initializer) for f in mapper.fields] == [
        ("MAX_BATCH", "int", "50")
    ]
    to_dto = method_named(mapper, "toDto")
    assert not to_dto.has_body and to_dto.calls == []
    assert to_dto.signature == "LoanDto toDto(Loan loan)"
    sorted_dtos = method_named(mapper, "toDtosSorted")
    assert sorted_dtos.has_body and sorted_dtos.modifiers == ["default"]
    assert sorted_dtos.signature == "default List<LoanDto> toDtosSorted(Collection<Loan> loans)"
    assert [c.name for c in sorted_dtos.calls] == ["toDtos", "stream", "sorted", "toList"]


def test_repeated_annotations_are_all_kept():
    to_dto = method_named(type_named(parse_fixture("LoanMapper.java"), "LoanMapper"), "toDto")

    assert [(a.name, a.arguments) for a in to_dto.annotations] == [
        ("Mapping", {"source": "book.id", "target": "bookId"}),
        ("Mapping", {"target": "notes", "ignore": "true"}),
    ]


def test_nested_types_are_flattened_with_dotted_names():
    java_file = parse_fixture("LibraryCard.java")

    assert [(t.name, t.kind, t.outer) for t in java_file.types] == [
        ("LibraryCard", "class", None),
        ("LibraryCard.Builder", "class", "org.example.library.model.LibraryCard"),
        ("LibraryCard.Stamp", "class", "org.example.library.model.LibraryCard"),
        ("LibraryCard.Stamp.Color", "enum", "org.example.library.model.LibraryCard.Stamp"),
    ]
    builder = type_named(java_file, "LibraryCard.Builder")
    assert builder.qualified_name == "org.example.library.model.LibraryCard.Builder"
    assert builder.modifiers == ["public", "static"]
    assert builder.javadoc == "Builds cards."
    assert [m.name for m in builder.methods] == ["number", "build", "validate"]
    assert type_named(java_file, "LibraryCard.Stamp.Color").enum_constants == ["RED", "BLUE"]


def test_record_components_and_compact_constructor():
    isbn = type_named(parse_fixture("Isbn.java"), "Isbn")

    assert isbn.kind == "record"
    assert [(c.name, c.type.text, [a.name for a in c.annotations]) for c in isbn.record_components] == [
        ("value", "String", []),
        ("prefix", "String", ["Nullable"]),
    ]
    constructor = isbn.methods[0]
    assert (constructor.name, constructor.kind, constructor.return_type) == ("<init>", "constructor", None)
    assert [p.name for p in constructor.parameters] == ["value", "prefix"]
    assert constructor.signature == "public Isbn(String value, String prefix)"
    assert call_named(constructor, "requireNonNull").string_args == ["value"]
    # Components are visible as fields in the record's methods.
    compare_to = method_named(isbn, "compareTo")
    assert [(c.name, c.receiver_type) for c in compare_to.calls] == [
        ("compareTo", "String"),
        ("value", "Isbn"),
    ]


def test_enum_constants_constructor_and_methods():
    status = type_named(parse_fixture("LoanStatus.java"), "LoanStatus")

    assert status.kind == "enum"
    assert status.enum_constants == ["OPEN", "RETURNED", "LOST"]
    assert [t.name for t in status.implements] == ["Labelled"]
    assert [f.name for f in status.fields] == ["label"]
    # The body of RETURNED is an anonymous class: its isClosed() is not a method of the enum.
    assert [m.name for m in status.methods] == ["<init>", "isClosed", "fromLabel", "next"]
    assert method_named(status, "<init>").signature == "LoanStatus(String label)"
    # An enum constant is a field of the enum's own type.
    assert call_named(method_named(status, "fromLabel"), "next").receiver_type == "LoanStatus"


def test_annotation_type_elements():
    valid_isbn = type_named(parse_fixture("ValidIsbn.java"), "ValidIsbn")

    assert valid_isbn.kind == "annotation"
    assert [(a.name, a.arguments) for a in valid_isbn.annotations] == [
        ("Target", {"value": ["ElementType.FIELD", "ElementType.PARAMETER"]}),
        ("Retention", {"value": "RetentionPolicy.RUNTIME"}),
        ("Constraint", {"validatedBy": "IsbnValidator.class"}),
        ("Documented", {}),
    ]
    message, groups, payload = valid_isbn.methods
    assert (message.signature, message.has_body) == ("String message()", False)
    assert groups.return_type == TypeRef("Class<?>[]", "Class", [TypeRef("?", "?")], 1)
    assert payload.return_type == TypeRef(
        "Class<? extends Payload>[]",
        "Class",
        [TypeRef("? extends Payload", "?", [TypeRef("Payload", "Payload")])],
        1,
    )


# ---- members -----------------------------------------------------------------------------


def test_constructor_injection():
    controller = type_named(parse_fixture("LoanController.java"), "LoanController")

    constructor = controller.methods[0]
    assert (constructor.name, constructor.kind, constructor.return_type) == ("<init>", "constructor", None)
    assert [(p.name, p.type.name) for p in constructor.parameters] == [
        ("loanService", "LoanService"),
        ("loanMapper", "LoanMapper"),
    ]
    # Parameters on two lines still give a one-line signature.
    assert constructor.signature == "public LoanController(LoanService loanService, LoanMapper loanMapper)"
    assert [(f.name, f.modifiers) for f in controller.fields] == [
        ("loanService", ["private", "final"]),
        ("loanMapper", ["private", "final"]),
    ]


def test_method_signature_parameters_annotations_and_lines():
    controller = type_named(parse_fixture("LoanController.java"), "LoanController")

    get_loan = method_named(controller, "getLoan")
    assert get_loan.signature == "public ResponseEntity<LoanDto> getLoan(Integer loanId)"
    assert get_loan.return_type == TypeRef(
        "ResponseEntity<LoanDto>", "ResponseEntity", [TypeRef("LoanDto", "LoanDto")]
    )
    assert get_loan.modifiers == ["public"]
    assert [(a.name, a.arguments) for a in get_loan.annotations] == [
        ("PreAuthorize", {"value": "hasRole(@roles.LIBRARIAN)"}),
        ("GetMapping", {"value": "/loans/{loanId}", "produces": ["application/json", "application/xml"]}),
    ]
    param = get_loan.parameters[0]
    assert (param.name, param.type.name) == ("loanId", "Integer")
    assert [(a.name, a.arguments) for a in param.annotations] == [("PathVariable", {"value": "loanId"})]
    assert get_loan.javadoc == "One loan by id."
    assert get_loan.start_line == line_of("LoanController.java", "@PreAuthorize")
    assert get_loan.end_line == line_of("LoanController.java", "HttpStatus.OK);") + 1
    list_loans = method_named(controller, "listLoans")
    assert list_loans.parameters[0].annotations[0].arguments == {"name": "member", "required": "false"}


def test_generic_static_method_signature_throws_and_javadoc():
    lookup = type_named(parse_fixture("EntityLookup.java"), "EntityLookup")

    get_by_id = method_named(lookup, "getById")
    assert get_by_id.signature == (
        "public static <T extends BaseItem> T getById(Collection<T> items, Class<T> itemClass, "
        "int itemId) throws ItemNotFoundException"
    )
    assert get_by_id.throws == ["ItemNotFoundException"]
    assert get_by_id.return_type == TypeRef("T", "T")
    assert get_by_id.javadoc == "The item with the given id.\n\n@throws ItemNotFoundException if there is none"
    assert lookup.modifiers == ["public", "abstract"]


def test_varargs_parameter():
    describe = method_named(type_named(parse_fixture("EntityLookup.java"), "EntityLookup"), "describe")

    assert describe.parameters[0].type == TypeRef("Object...", "Object", [], 1)
    assert describe.signature == "public static String describe(Object... parts)"


def test_entity_annotations_with_nested_annotations_as_raw_text():
    book = type_named(parse_fixture("Book.java"), "Book")

    table = book.annotations[1]
    # Written over two lines, kept on one.
    assert table.arguments == {
        "name": "books",
        "uniqueConstraints": '@UniqueConstraint(columnNames = {"isbn", "edition"})',
    }
    authors = next(f for f in book.fields if f.name == "authors")
    assert [(a.name, a.arguments) for a in authors.annotations] == [
        ("ManyToMany", {"fetch": "FetchType.EAGER"}),
        (
            "JoinTable",
            {
                "name": "book_authors",
                "joinColumns": '@JoinColumn(name = "book_id")',
                "inverseJoinColumns": '@JoinColumn(name = "author_id")',
            },
        ),
    ]
    version = next(f for f in book.fields if f.name == "version")
    assert version.annotations[0].name == "Version"  # written @jakarta.persistence.Version


def test_one_field_per_declarator_with_initializers():
    book = type_named(parse_fixture("Book.java"), "Book")
    fields = {f.name: f for f in book.fields}

    assert list(fields) == ["title", "authors", "copies", "reserved", "FORMATS", "version"]
    assert (fields["copies"].initializer, fields["reserved"].initializer) == ("1", None)
    assert fields["copies"].line == fields["reserved"].line == line_of("Book.java", "copies")
    assert fields["authors"].initializer == "new HashSet<>()"
    assert fields["authors"].type == TypeRef("Set<Author>", "Set", [TypeRef("Author", "Author")])
    formats = fields["FORMATS"]
    assert formats.modifiers == ["private", "static", "final"]
    assert formats.type == TypeRef("String[]", "String", [], 1)
    assert formats.initializer == '{"paper", "ebook"}'
    assert [a.name for a in fields["title"].annotations] == ["Column", "NotEmpty"]


def test_type_refs():
    java_file = parse_java(
        "class S { Map<String, List<Book>> byAuthor; java.util.List<String> names; int[][] grid;"
        " Map.Entry<String, Integer> entry; List<? super Book> sink; String legacy[]; boolean flag; }",
        "S.java",
    )
    types = {f.name: f.type for f in java_file.types[0].fields}

    assert types["byAuthor"] == TypeRef(
        "Map<String, List<Book>>",
        "Map",
        [TypeRef("String", "String"), TypeRef("List<Book>", "List", [TypeRef("Book", "Book")])],
    )
    assert types["names"] == TypeRef(
        "java.util.List<String>", "java.util.List", [TypeRef("String", "String")]
    )
    assert types["grid"] == TypeRef("int[][]", "int", [], 2)
    assert types["entry"].name == "Map.Entry" and len(types["entry"].args) == 2
    assert types["sink"].args == [TypeRef("? super Book", "?", [TypeRef("Book", "Book")])]
    assert types["legacy"] == TypeRef("String[]", "String", [], 1)
    assert types["flag"] == TypeRef("boolean", "boolean")


def test_javadoc_markers_are_stripped_and_plain_comments_ignored():
    java_file = parse_java(
        "class S {\n"
        "  /**\n   * First line.\n   *   indented\n   */\n"
        "  // a line comment between the javadoc and the method\n"
        "  void documented() {}\n"
        "  /* not javadoc */\n"
        "  void plain() {}\n"
        "  @Deprecated\n  /** After the annotations. */\n  void late() {}\n"
        "}\n",
        "S.java",
    )
    docs = {m.name: m.javadoc for m in java_file.types[0].methods}

    assert docs == {"documented": "First line.\n  indented", "plain": None, "late": "After the annotations."}


# ---- call sites --------------------------------------------------------------------------


def test_calls_inside_lambdas_count_for_the_enclosing_method():
    service = type_named(parse_fixture("LoanServiceImpl.java"), "LoanServiceImpl")

    find_loan = method_named(service, "findLoan")
    assert [(c.name, c.receiver, c.receiver_type) for c in find_loan.calls] == [
        ("findOrNull", None, None),
        ("findById", "loanRepository", "LoanRepository"),
    ]
    assert find_loan.calls[1].line == line_of("LoanServiceImpl.java", "loanRepository.findById")


def test_receiver_types_from_parameters_locals_fields_and_static_calls():
    return_all = method_named(
        type_named(parse_fixture("LoanServiceImpl.java"), "LoanServiceImpl"), "returnAll"
    )
    types = {(c.receiver, c.name): c.receiver_type for c in return_all.calls}

    assert types[("loan", "markReturned")] == "Loan"  # enhanced-for variable
    assert types[("this.clock", "now")] == "Clock"  # this.field
    assert types[("loanRepository", "newBatch")] == "LoanRepository"  # field
    assert types[("loans", "forEach")] == "List"  # parameter
    assert types[("l", "isLost")] == "Loan"  # typed lambda parameter
    assert types[("member", "notify")] == "Member"  # parameter
    assert types[("receipt", "summary")] == "Receipt"  # var receipt = new Receipt(...)
    assert types[("e", "printStackTrace")] == "IllegalStateException"  # catch parameter
    assert types[("book", "touch")] == "Book"  # local variable
    assert types[("LoanPolicy", "check")] == "LoanPolicy"  # static call
    assert types[("super", "audit")] == "AbstractAuditedService"
    assert types[("this", "restock")] == "LoanServiceImpl"  # this::restock


def test_receiver_type_is_none_when_the_file_does_not_tell():
    service = type_named(parse_fixture("LoanServiceImpl.java"), "LoanServiceImpl")
    return_all = method_named(service, "returnAll")
    types = {(c.receiver, c.name): c.receiver_type for c in return_all.calls}

    assert types[("batch", "flush")] is None  # var batch = loanRepository.newBatch()
    assert types[("each", "archive")] is None  # untyped lambda parameter
    assert types[("LoanPolicy.check(member)", "apply")] is None  # chained call
    # A multi-catch parameter's type is a union.
    assert call_named(method_named(service, "findOrNull"), "getMessage").receiver_type is None


def test_chained_calls_get_the_receiver_text_and_no_type():
    tests = type_named(parse_fixture("AbstractLoanControllerTests.java"), "AbstractLoanControllerTests")

    set_up = method_named(tests, "setUp")
    assert [(c.name, c.receiver, c.receiver_type) for c in set_up.calls] == [
        ("standaloneSetup", "MockMvcBuilders", "MockMvcBuilders"),
        ("build", "MockMvcBuilders.standaloneSetup(loanController)", None),
    ]


def test_call_kinds_and_source_order():
    get_loan = method_named(type_named(parse_fixture("LoanController.java"), "LoanController"), "getLoan")

    assert [(c.kind, c.name) for c in get_loan.calls] == [
        ("call", "findLoan"),
        ("new", "ResponseEntity"),
        ("new", "ResponseEntity"),
        ("call", "toDto"),
    ]
    new_entity = get_loan.calls[1]
    assert (new_entity.receiver, new_entity.receiver_type, new_entity.arg_count) == (
        None,
        "ResponseEntity",
        1,
    )


def test_method_references():
    service = type_named(parse_fixture("LoanServiceImpl.java"), "LoanServiceImpl")
    refs = [c for c in method_named(service, "returnAll").calls if c.kind == "method_ref"]

    assert [(c.name, c.receiver, c.receiver_type, c.arg_count) for c in refs] == [
        ("getBook", "Loan", "Loan", 0),
        ("restock", "this", "LoanServiceImpl", 0),
    ]
    to_list = method_named(type_named(parse_fixture("EntityLookup.java"), "EntityLookup"), "describe")
    assert call_named(to_list, "valueOf").kind == "method_ref"
    constructor_ref = snippet_method("names.stream().map(Book::new);").calls[-1]
    assert (constructor_ref.kind, constructor_ref.name, constructor_ref.receiver_type) == (
        "method_ref",
        "<init>",
        "Book",
    )


def test_mockmvc_calls_keep_their_url_string_arguments():
    tests = type_named(parse_fixture("AbstractLoanControllerTests.java"), "AbstractLoanControllerTests")

    get = call_named(method_named(tests, "shouldGetLoan"), "get")
    assert (get.receiver, get.receiver_type, get.arg_count, get.string_args) == (
        None,
        None,
        1,
        ["/api/loans/1"],
    )
    assert call_named(method_named(tests, "shouldPageLoans"), "get").string_args == [
        "/api/v2/loans?page=0&size=2"
    ]
    assert call_named(method_named(tests, "shouldRenewLoan"), "put").string_args == [
        "/api/members/{}/loans/{}/renewal"
    ]


def test_multiline_chains_are_put_on_one_line():
    should_get_loan = method_named(
        type_named(parse_fixture("AbstractLoanControllerTests.java"), "AbstractLoanControllerTests"),
        "shouldGetLoan",
    )

    and_expect = call_named(should_get_loan, "andExpect")
    assert and_expect.receiver == 'this.mockMvc.perform(get("/api/loans/1").accept(MediaType.APPLICATION_JSON))'
    assert and_expect.line == line_of("AbstractLoanControllerTests.java", ".andExpect(status().isOk());")
    assert call_named(should_get_loan, "perform").receiver_type == "MockMvc"


def test_tabs_mixed_with_spaces():
    tests = type_named(parse_fixture("AbstractLoanControllerTests.java"), "AbstractLoanControllerTests")

    fields = {f.name: f for f in tests.fields}
    assert [a.name for a in fields["loanController"].annotations] == ["Autowired"]
    assert [a.name for a in fields["loanService"].annotations] == ["MockitoBean"]
    assert fields["loanController"].modifiers == ["protected"]
    page = method_named(tests, "shouldPageLoans")  # indented with tabs and spaces
    assert call_named(page, "andExpect").receiver == 'mockMvc.perform(get("/api/v2/loans?page=0&size=2"))'


def test_string_arguments_concatenations_text_blocks_and_escapes():
    method = snippet_method(
        'log("a\\"b\\tc", 42, name);\n'
        'log("/x/" + (id + 1) + "/y", "total: " + count);\n'
        "log(a + b);\n"
        'query("""\n    SELECT id\n      FROM loans\n    """);\n'
    )
    first, second, third, fourth = method.calls

    assert (first.string_args, first.arg_count) == (['a"b\tc'], 3)
    assert second.string_args == ["/x/{}/y", "total: {}"]
    assert third.string_args == []
    assert fourth.string_args == ["SELECT id\n  FROM loans\n"]


def test_unicode_and_octal_escapes_decode_as_java_does():
    method = parse_java(
        "class S {\n"
        '  @DisplayName("\\uD83D\\uDE00") void m() {\n'
        '    log("\\uD83D\\uDE00 ok", "\\uD83D alone", "\\477", "\\101\\0");\n'
        "  }\n"
        "}\n",
        "S.java",
    ).types[0].methods[0]

    # A Java string is UTF-16: the surrogate pair is one character, and a lone surrogate (which
    # can't be written as UTF-8) becomes U+FFFD. A 3-digit octal escape starts with 0-3: \477 is \47 + 7.
    assert method.annotations[0].arguments == {"value": "\U0001F600"}
    assert method.calls[0].string_args == ["\U0001F600 ok", "� alone", "'7", "A\0"]


def test_anonymous_class_is_not_a_type_and_its_calls_belong_to_the_enclosing_method():
    java_file = parse_fixture("JdbcBookRepository.java")

    assert [t.name for t in java_file.types] == ["JdbcBookRepository"]
    repository = java_file.types[0]
    assert [m.name for m in repository.methods] == ["<init>", "findByAuthor"]
    find = method_named(repository, "findByAuthor")
    assert [(c.kind, c.name, c.receiver_type) for c in find.calls] == [
        ("call", "query", "JdbcTemplate"),
        ("new", "RowMapper", "RowMapper"),
        ("new", "Book", "Book"),
        ("call", "setTitle", "Book"),
        ("call", "getString", "ResultSet"),  # rs: a parameter of the anonymous class's method
    ]
    query = find.calls[0]
    assert (query.arg_count, query.string_args) == (3, ["SELECT id, title FROM books WHERE author_id = ?"])


def test_local_classes_are_not_types_either():
    java_file = parse_java(
        "class S { void m() { class Helper { void run(Clock clock) { clock.tick(); } } } }", "S.java"
    )

    assert [t.name for t in java_file.types] == ["S"]
    tick = java_file.types[0].methods[0].calls[0]
    assert (tick.name, tick.receiver_type) == ("tick", "Clock")


def test_fields_of_outer_types_and_this_in_nested_types():
    java_file = parse_fixture("LibraryCard.java")

    build = method_named(type_named(java_file, "LibraryCard.Builder"), "build")
    assert [(c.name, c.receiver_type) for c in build.calls] == [
        ("validate", "LibraryCard.Builder"),
        ("register", "Issuer"),  # the Builder's own field
        ("LibraryCard", "LibraryCard"),
    ]
    stamp = method_named(type_named(java_file, "LibraryCard.Stamp"), "stamp")
    assert [(c.name, c.receiver_type) for c in stamp.calls] == [
        ("sign", "Issuer"),  # a field of the outer type
        ("instant", "Clock"),
    ]


def test_type_variables_resolve_to_their_bound():
    get_by_id = method_named(type_named(parse_fixture("EntityLookup.java"), "EntityLookup"), "getById")

    assert call_named(get_by_id, "getId").receiver_type == "BaseItem"  # T extends BaseItem
    assert call_named(get_by_id, "isInstance").receiver_type == "Class"
    unbounded = method_named(type_named(parse_fixture("LoanServiceImpl.java"), "LoanServiceImpl"), "findOrNull")
    assert call_named(unbounded, "get").receiver_type == "Supplier"


def test_qualified_type_names_as_receivers():
    describe = method_named(type_named(parse_fixture("EntityLookup.java"), "EntityLookup"), "describe")

    assert call_named(describe, "stream").receiver_type == "java.util.Arrays"
    assert call_named(describe, "join").string_args == [", "]
    status = snippet_method("HttpStatus.OK.value(); Outer.Inner.create();").calls
    assert [(c.name, c.receiver_type) for c in status] == [("value", None), ("create", "Outer.Inner")]


def test_lexical_scoping_of_local_variables():
    method = snippet_method(
        "owner.save();\n"
        "if (ready) { Pet owner = next(); owner.feed(); }\n"
        "owner.close();\n"
        "items.forEach(owner -> owner.ping());\n"
        "for (Visit v : visits) { v.check(); }\n"
        "for (Vet v : vets) { v.check(); }\n"
        "if (thing instanceof Book b) { b.open(); }\n",
        members="private Owner owner;",
    )
    receiver_types = [(c.receiver, c.name, c.receiver_type) for c in method.calls if c.receiver]

    assert receiver_types == [
        ("owner", "save", "Owner"),  # the field
        ("owner", "feed", "Pet"),  # the local variable shadows it in its block
        ("owner", "close", "Owner"),  # and is gone after the block
        ("items", "forEach", None),
        ("owner", "ping", None),  # an untyped lambda parameter shadows the field
        ("v", "check", "Visit"),
        ("v", "check", "Vet"),
        ("b", "open", "Book"),  # a pattern variable
    ]


def test_explicit_constructor_calls_and_qualified_this():
    java_file = parse_java(
        "class Base {}\n"
        "class S extends Base {\n"
        "  Repo repo;\n"
        "  S() { this(1); }\n"
        "  S(int n) { super(n); }\n"
        "  class Inner { void m() { S.this.repo.find(); } }\n"
        "}\n",
        "S.java",
    )
    s = type_named(java_file, "S")
    this_call, super_call = (m.calls[0] for m in s.methods)

    assert (this_call.kind, this_call.name, this_call.receiver, this_call.receiver_type) == (
        "call",
        "<init>",
        "this",
        "S",
    )
    assert (super_call.name, super_call.receiver, super_call.receiver_type) == ("<init>", "super", "Base")
    find = type_named(java_file, "S.Inner").methods[0].calls[0]
    assert (find.receiver, find.receiver_type) == ("S.this.repo", "Repo")


def test_field_initializers_have_no_calls_recorded():
    card = type_named(parse_fixture("LibraryCard.java"), "LibraryCard.Stamp")

    clock = card.fields[0]
    assert clock.initializer == "Clock.systemUTC()"
    assert all(c.name != "systemUTC" for m in card.methods for c in m.calls)


# ---- errors and robustness ---------------------------------------------------------------


def test_syntax_errors_are_reported_and_what_parsed_is_kept():
    java_file = parse_fixture("BrokenLoanService.java")

    broken_line = line_of("BrokenLoanService.java", "broken(")
    assert java_file.errors == [
        f"line {broken_line}: syntax error near 'public void broken( {{' (missing ')')"
    ]
    # tree-sitter breaks the whole class up after such an error; its header and the members
    # that parsed are salvaged.
    service = type_named(java_file, "BrokenLoanService")
    assert (service.kind, service.modifiers, [a.name for a in service.annotations]) == (
        "class",
        ["public"],
        ["Service"],
    )
    ok = method_named(service, "ok")
    assert [(c.name, c.receiver) for c in ok.calls] == [("save", "repository")]


def test_at_most_five_errors():
    methods = "".join(f"  void m{i}() {{ call{i}() }}\n" for i in range(8))  # no ';' after calls

    java_file = parse_java("class S {\n" + methods + "}\n", "S.java")

    assert java_file.errors == [f"line {i + 2}: syntax error near 'call{i}()'" for i in range(5)]
    assert len(java_file.types[0].methods) == 8


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.java")))
def test_never_raises_on_truncated_source(name):
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines(keepends=True)

    for end in range(len(lines) + 1):
        java_file = parse_java("".join(lines[:end]), name)
        assert len(java_file.errors) <= 5


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.java")))
def test_round_trips_through_to_dict(name):
    java_file = parse_fixture(name)

    assert from_dict(JavaFile, to_dict(java_file)) == java_file
