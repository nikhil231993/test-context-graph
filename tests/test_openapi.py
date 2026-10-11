"""openapi.py: OpenAPI 3 and Swagger 2 text -> ApiSpec, on small made-up specs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from test_context_graph.models import ApiParameter, ApiSpec, to_dict
from test_context_graph.openapi import looks_like_openapi, parse_openapi

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bookshop() -> ApiSpec:
    return parse_openapi(read("bookshop.yml"), "api/bookshop.yml")


def operation(spec: ApiSpec, method: str, path: str):
    return next(op for op in spec.operations if op.method == method and op.path == path)


def schema(spec: ApiSpec, name: str):
    return next(s for s in spec.schemas if s.name == name)


def minimal_spec(**extra) -> str:
    return json.dumps({"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "paths": {}, **extra})


# ---- looks_like_openapi -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        read("bookshop.yml"),
        read("bookshop.json"),
        read("garden-swagger2.yml"),
        '{"swagger":"2.0","info":{"title":"t","version":"1"},"paths":{}}',
        '﻿openapi: "3.1.0"\ninfo: {}\n',
        "# leading comment\n---\nswagger: 2.0\n",
    ],
)
def test_looks_like_openapi_accepts_specs(text):
    assert looks_like_openapi(text)


@pytest.mark.parametrize(
    "text",
    [
        read("application.yml"),  # a top-level openapi: section, but no version
        read("docker-compose.yml"),  # openapi: 3.0.3 nested, not top level
        '{"name": "ui", "config": {"openapi": "3.0.1"}}',  # the same in JSON
        "name: build\non: push\njobs:\n  docs:\n    run: echo 'openapi: 3.0.0'\n",
        "openapi:3.0.0\n",  # no space after the colon: not a YAML key
        '{"openapi": "3.0.1", ',  # broken JSON
        "",
    ],
)
def test_looks_like_openapi_rejects_other_files(text):
    assert not looks_like_openapi(text)


# ---- the spec ----------------------------------------------------------------------------


def test_title_version_and_base_path_from_the_first_server_with_variable_defaults(bookshop):
    assert (bookshop.path, bookshop.title, bookshop.version) == ("api/bookshop.yml", "Bookshop", "2.1")
    # https://{region}.bookshop.example/{basePath}/ with basePath's default "shop/v2"
    assert bookshop.base_path == "/shop/v2"


@pytest.mark.parametrize(
    ("servers", "base_path"),
    [
        ([{"url": "/api/"}], "/api"),
        ([{"url": "http://localhost:9966/clinic/api"}], "/clinic/api"),
        ([{"url": "https://api.example.com"}], ""),
        ([], ""),
        (None, ""),
    ],
)
def test_base_path_is_the_path_part_of_the_server_url(servers, base_path):
    extra = {} if servers is None else {"servers": servers}
    assert parse_openapi(minimal_spec(**extra), "s.json").base_path == base_path


def test_operations_in_document_order_with_their_lines(bookshop):
    assert [(op.method, op.path, op.operation_id) for op in bookshop.operations] == [
        ("GET", "/books", "listBooks"),
        ("POST", "/books", "addBook"),
        ("GET", "/books/{bookId}", "getBook"),
        ("PUT", "/books/{bookId}", "replaceBookText"),
        ("DELETE", "/books/{bookId}", "deleteBook"),
    ]  # the path item's x-owner and parameters keys are not operations
    lines = read("bookshop.yml").splitlines()
    assert operation(bookshop, "GET", "/books").line == 16
    for op in bookshop.operations:
        assert lines[op.line - 1].strip() == f"{op.method.lower()}:"


def test_operation_texts_and_tags(bookshop):
    listing = operation(bookshop, "GET", "/books")
    assert (listing.summary, listing.description, listing.tags) == ("Lists books", None, ["books"])
    assert operation(bookshop, "GET", "/books/{bookId}").description == (
        "Returns one book.\nIts reviews are included."
    )
    assert operation(bookshop, "DELETE", "/books/{bookId}").tags == []


def test_path_level_parameters_are_merged_in_and_the_operations_own_win(bookshop):
    book_id = ApiParameter("bookId", "path", required=True, schema="integer")  # a $ref
    assert operation(bookshop, "GET", "/books/{bookId}").parameters == [
        book_id,
        ApiParameter("X-Request-Id", "header", required=False, schema="string"),
    ]
    assert operation(bookshop, "PUT", "/books/{bookId}").parameters == [
        book_id,
        ApiParameter("X-Request-Id", "header", required=True, schema="string"),
    ]


def test_ref_parameters_are_resolved(bookshop):
    assert operation(bookshop, "GET", "/books").parameters == [
        ApiParameter("size", "query", required=False, schema="integer"),
        ApiParameter("tag", "query", required=False, schema="array<string>"),
    ]


def test_request_body_is_the_json_schema_else_the_first_content_types(bookshop):
    assert operation(bookshop, "POST", "/books").request_body == "BookFields"  # via requestBodies
    assert operation(bookshop, "PUT", "/books/{bookId}").request_body == "string"  # text/plain only
    assert operation(bookshop, "GET", "/books").request_body is None


def test_responses_are_short_schema_forms_or_the_description(bookshop):
    assert operation(bookshop, "GET", "/books").responses == {"200": "array<Book>"}
    # application/json wins over the application/xml listed first; default is a $ref response
    assert operation(bookshop, "POST", "/books").responses == {"201": "Book", "default": "Problem"}
    assert operation(bookshop, "GET", "/books/{bookId}").responses == {
        "200": "allOf<Book,object>",
        "404": "No such book.",  # no content
    }
    assert operation(bookshop, "DELETE", "/books/{bookId}").responses == {"204": "Deleted."}


@pytest.mark.parametrize(
    ("prop", "short"),
    [
        ({"$ref": "#/components/schemas/Owner"}, "Owner"),
        ({"$ref": "schemas/Owner.yaml"}, "Owner"),
        ({"type": "integer", "format": "int32"}, "integer"),
        ({"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}, "array<Pet>"),
        ({"type": "array", "items": {"type": "array", "items": {"type": "string"}}}, "array<array<string>>"),
        ({"type": "array"}, "array"),
        ({"type": "object", "properties": {"a": {"type": "string"}}}, "object"),
        ({"properties": {"a": {"type": "string"}}}, "object"),
        ({"type": "object", "additionalProperties": {"$ref": "#/components/schemas/Visit"}}, "map<Visit>"),
        ({"allOf": [{"$ref": "#/components/schemas/OwnerFields"}, {"type": "object"}]}, "allOf<OwnerFields,object>"),
        ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, "oneOf<string,integer>"),
        ({"anyOf": [{"$ref": "#/components/schemas/Cat"}, {"$ref": "#/components/schemas/Dog"}]}, "anyOf<Cat,Dog>"),
        ({"type": ["string", "null"]}, "string"),  # OpenAPI 3.1
        ({}, "any"),
    ],
)
def test_short_schema_forms(prop, short):
    text = minimal_spec(components={"schemas": {"S": {"properties": {"p": prop}}}})
    assert parse_openapi(text, "s.json").schemas[0].properties == {"p": short}


def test_schemas_with_properties_required_allof_and_lines(bookshop):
    assert [s.name for s in bookshop.schemas] == ["BookFields", "Book", "Review", "RichText", "Problem"]
    book = schema(bookshop, "Book")
    # properties and required come from the inline allOf part; the $ref part goes to all_of
    assert book.properties == {"id": "integer", "reviews": "array<Review>"}
    assert (book.required, book.all_of) == (["id"], ["BookFields"])
    fields = schema(bookshop, "BookFields")
    assert fields.properties == {"title": "string", "authors": "array<string>", "prices": "map<number>"}
    assert (fields.required, fields.all_of) == (["title"], [])
    # keys stay strings as written: the property "on" is not YAML's True
    assert schema(bookshop, "Review").properties == {
        "stars": "integer", "on": "boolean", "body": "oneOf<string,RichText>",
    }
    lines = read("bookshop.yml").splitlines()
    assert book.line == lines.index("    Book:") + 1
    assert all(lines[s.line - 1] == f"    {s.name}:" for s in bookshop.schemas)


def test_schema_text_is_the_schema_as_yaml(bookshop):
    text = schema(bookshop, "Book").text
    assert text.startswith("Book:\n  description: A book with its id and reviews.\n  allOf:\n")
    assert yaml.safe_load(text) == {"Book": yaml.safe_load(read("bookshop.yml"))["components"]["schemas"]["Book"]}


def without_lines(spec: ApiSpec) -> dict:
    data = to_dict(spec)
    for item in data["operations"] + data["schemas"]:
        item["line"] = None
    return data


def test_json_gives_the_same_spec_with_json_lines(bookshop):
    from_json = parse_openapi(read("bookshop.json"), "api/bookshop.yml")
    assert without_lines(from_json) == without_lines(bookshop)
    lines = read("bookshop.json").splitlines()
    for op in from_json.operations:
        assert lines[op.line - 1].strip() == f'"{op.method.lower()}": {{'
    assert lines[schema(from_json, "Book").line - 1].strip() == '"Book": {'


def test_json_indented_with_tabs(bookshop):
    data = json.loads(read("bookshop.json"))
    spec = parse_openapi(json.dumps(data, indent="\t"), "api/bookshop.json")
    assert [op.operation_id for op in spec.operations] == [op.operation_id for op in bookshop.operations]
    assert spec.operations[0].line == 26


def test_swagger2():
    spec = parse_openapi(read("garden-swagger2.yml"), "garden.yml")
    assert (spec.title, spec.version, spec.base_path) == ("Garden", "1.0.0", "/garden/v1")
    find = operation(spec, "GET", "/plants")
    assert find.parameters == [
        ApiParameter("kinds", "query", schema="array<string>"),  # type and items on the parameter
        ApiParameter("limit", "query", schema="integer"),  # a $ref to #/parameters
    ]
    assert find.responses == {"200": "array<Plant>"}
    add = operation(spec, "POST", "/plants")
    assert (add.parameters, add.request_body) == ([], "Plant")  # the body parameter
    assert add.responses == {"201": "Plant", "400": "Invalid plant."}
    assert operation(spec, "POST", "/plants/{plantId}/photo").parameters == [
        ApiParameter("plantId", "path", required=True, schema="integer"),
        ApiParameter("photo", "formData", schema="file"),
    ]
    plant = schema(spec, "Plant")  # definitions are the schemas
    assert plant.properties == {"id": "integer", "name": "string", "tags": "array<Tag>"}
    assert plant.required == ["name"]
    assert read("garden-swagger2.yml").splitlines()[plant.line - 1] == "  Plant:"


def test_refs_to_other_files_or_in_a_cycle_are_skipped():
    text = minimal_spec(
        paths={"/x": {"get": {
            "parameters": [
                {"$ref": "common.yaml#/components/parameters/Trace"},
                {"$ref": "#/components/parameters/A"},
                {"name": "q", "in": "query"},
            ],
            "responses": {"404": {"$ref": "common.yaml#/components/responses/NotFound"}},
        }}},
        components={"parameters": {"A": {"$ref": "#/components/parameters/B"},
                                   "B": {"$ref": "#/components/parameters/A"}}},
    )
    op = parse_openapi(text, "s.json").operations[0]
    assert op.parameters == [ApiParameter("q", "query")]
    assert op.responses == {"404": "NotFound"}


def test_path_items_given_as_local_refs_are_followed():
    text = minimal_spec(
        paths={"/health": {"$ref": "#/components/pathItems/Health"}},
        components={"pathItems": {"Health": {"get": {"operationId": "health", "responses": {}}}}},
    )
    [op] = parse_openapi(text, "s.json").operations
    assert (op.method, op.path, op.operation_id, op.line) == ("GET", "/health", "health", None)


def test_yaml_aliases_and_merge_keys_are_expanded():
    text = "\n".join([
        "openapi: 3.0.0",
        "info: {title: t, version: '1'}",
        "paths: {}",
        "components:",
        "  schemas:",
        "    Named: &named",
        "      type: object",
        "      properties:",
        "        name: {type: string}",
        "    Alias: *named",
        "    Merged:",
        "      <<: *named",
        "      required: [name]",
    ])
    spec = parse_openapi(text, "s.yml")
    assert [(s.name, s.properties, s.required) for s in spec.schemas] == [
        ("Named", {"name": "string"}, []),
        ("Alias", {"name": "string"}, []),
        ("Merged", {"name": "string"}, ["name"]),
    ]
    assert "&" not in schema(spec, "Alias").text and "*" not in schema(spec, "Alias").text


def test_invalid_yaml_raises_value_error_with_the_line():
    with pytest.raises(ValueError, match=r"^api/broken\.yml: not valid YAML or JSON: .*\(line 3, column 11\)$"):
        parse_openapi("openapi: 3.0.0\ninfo:\n  title: a: b\n", "api/broken.yml")
    with pytest.raises(ValueError, match=r"not valid YAML or JSON: .*\(line 3, column 11\)$"):
        parse_openapi('{"openapi": "3.0.0",\n "info": {\n  "title" "x"}}', "api/broken.json")


@pytest.mark.parametrize("text", [read("application.yml"), "", "- a\n- list\n", "[1, 2]", "openapi: {}\n"])
def test_text_that_is_not_a_spec_raises_value_error(text):
    with pytest.raises(ValueError, match="not an OpenAPI or Swagger document"):
        parse_openapi(text, "config.yml")
