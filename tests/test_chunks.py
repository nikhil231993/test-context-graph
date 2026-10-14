"""chunks.make_chunks on a small made-up Spring codebase (a zoo), an OpenAPI spec and a doc."""

from __future__ import annotations

import hashlib

import pytest

from test_context_graph.chunks import make_chunks
from test_context_graph.graph import build_graph
from test_context_graph.java_parser import parse_java
from test_context_graph.models import (
    ApiOperation,
    ApiParameter,
    ApiSchema,
    ApiSpec,
    Chunk,
    DocSection,
    to_dict,
)

ZOO = "src/main/java/org/acme/zoo"
KEEPER_PATH = f"{ZOO}/Keeper.java"
SPEC_PATH = "src/main/resources/zoo.yml"

SOURCES = {
    KEEPER_PATH: """package org.acme.zoo;

import org.acme.zoo.rest.dto.AnimalDto;

/**
 * Keeps animals.
 */
@Service
public class Keeper {
    @Autowired
    private Feeder feeder;
    private int count = 0;

    /**
     * Feeds an animal.
     * Twice if hungry.
     */
    @Transactional
    public void feed(String animal) {
        feeder.give(animal);
        count++;
    }

    public int getCount() {
        return count;
    }

    public AnimalDto toDto() {
        return null;
    }

    class Shift {
    }
}
""",
    f"{ZOO}/Animal.java": """package org.acme.zoo;

@Entity
public class Animal {
    @Column(name = "animal_name", nullable = false)
    private String name;

    public String getName() {
        return name;
    }

    @JsonIgnore
    public void setName(String name) {
        this.name = name;
    }

    public boolean isHungry() {
        boolean hungry = name.isEmpty();
        boolean late = name.length() > 3;
        return hungry || late;
    }
}
""",
    f"{ZOO}/Feeder.java": """package org.acme.zoo;

public class Feeder {
    public void give(String animal) {
    }
}
""",
    f"{ZOO}/web/AnimalController.java": """package org.acme.zoo.web;

@RestController
@RequestMapping("/api")
public class AnimalController {
    @GetMapping("/animals/{id}")
    public String getAnimal(@PathVariable int id) {
        return "lion";
    }
}
""",
}

SPEC = ApiSpec(
    path=SPEC_PATH,
    base_path="/zoo",
    operations=[
        ApiOperation(
            "GET", "/animals/{animalId}", operation_id="getAnimal", summary="Get an animal",
            description="One animal.", tags=["animals"],
            parameters=[ApiParameter("animalId", "path", True, "integer")],
            responses={"200": "Animal", "404": "Not found."}, line=12,
        ),
    ],
    schemas=[
        ApiSchema(
            "Animal", 30, {"name": "string"}, ["name"],
            text="Animal:\n  type: object\n  properties:\n    name:\n      type: string\n",
        ),
    ],
)  # fmt: skip

DOCS = [
    DocSection("docs/zoo.md", "", "", 0, 1, 1, "Intro line.\n"),
    DocSection("docs/zoo.md", "Zoo", "zoo", 1, 2, 4, "# Zoo\n\nThe Keeper feeds animals.\n"),
    DocSection("docs/zoo.md", "Hours", "hours", 2, 5, 5, "## Hours\n", ["Zoo"]),
    DocSection("docs/zoo.md", "Feeding", "feeding", 2, 6, 8, "## Feeding\n\nAt noon.\n", ["Zoo"]),
]  # fmt: skip


def line_of(path: str, snippet: str) -> int:
    return next(i for i, line in enumerate(SOURCES[path].split("\n"), 1) if snippet in line)


@pytest.fixture(scope="module")
def java_files() -> list:
    return [parse_java(text, path) for path, text in SOURCES.items()]


@pytest.fixture(scope="module")
def graph(java_files):
    return build_graph(java_files, [SPEC], DOCS)


@pytest.fixture(scope="module")
def chunks(graph, java_files) -> dict[str, Chunk]:
    return {c.id: c for c in make_chunks(graph, java_files, SOURCES, [SPEC], DOCS)}


def test_one_chunk_per_node_but_files(chunks, graph):
    assert {c.kind for c in chunks.values()} == {"method", "type", "endpoint", "schema", "doc"}
    assert not any(c.id.startswith("file:") for c in chunks.values())
    nodes = {n.id: n for n in graph.nodes}
    for chunk in chunks.values():
        assert chunk.node_id == chunk.id and chunk.kind == nodes[chunk.id].kind


def test_method_chunk_is_its_source_with_the_javadoc(chunks):
    chunk = chunks["method:org.acme.zoo.Keeper#feed(String)"]
    assert chunk.text == (
        "/**\n * Feeds an animal.\n * Twice if hungry.\n */\n"
        "@Transactional\npublic void feed(String animal) {\n    feeder.give(animal);\n"
        "    count++;\n}"
    )
    header = f"{KEEPER_PATH}\nclass org.acme.zoo.Keeper (service)\npublic void feed(String animal)"
    assert chunk.embed_text == header + "\n" + chunk.text
    assert chunk.content_hash == hashlib.sha256(chunk.embed_text.encode()).hexdigest()
    assert chunk.path == KEEPER_PATH
    assert chunk.start_line == line_of(KEEPER_PATH, "@Transactional")
    assert chunk.end_line == line_of(KEEPER_PATH, "count++") + 1


def test_trivial_accessors_are_skipped_in_entities_only(chunks):
    assert "method:org.acme.zoo.Animal#getName()" not in chunks
    assert "method:org.acme.zoo.Animal#setName(String)" not in chunks  # annotation aside
    assert "method:org.acme.zoo.Animal#isHungry()" in chunks  # more than 3 lines
    assert "method:org.acme.zoo.Keeper#getCount()" in chunks  # a service's getter
    assert "method:org.acme.zoo.Feeder#give(String)" in chunks  # not an accessor


def test_type_chunk_is_an_outline(chunks):
    chunk = chunks["type:org.acme.zoo.Keeper"]
    assert chunk.text == "\n".join(
        [
            "/**",
            " * Keeps animals.",
            " */",
            "@Service",
            "public class Keeper {",
            "    @Autowired private Feeder feeder;",
            "    private int count = 0;",
            "",
            "    @Transactional public void feed(String animal);",
            "    public int getCount();",
            "    public AnimalDto toDto();",
            "",
            "    class Shift { ... }",
            "}",
        ]
    )
    assert chunk.embed_text.startswith(f"{KEEPER_PATH}\nclass org.acme.zoo.Keeper (service)\n/**")
    assert (chunk.start_line, chunk.end_line) == (line_of(KEEPER_PATH, "@Service"), 34)
    animal = chunks["type:org.acme.zoo.Animal"].text
    assert '    @Column(name = "animal_name", nullable = false) private String name;' in animal
    assert "type:org.acme.zoo.Keeper.Shift" in chunks
    assert "type:org.acme.zoo.rest.dto.AnimalDto" not in chunks  # a stub: no source


def test_endpoint_chunk_reads_like_a_contract(chunks):
    chunk = chunks["endpoint:GET /animals/{animalId}"]
    assert chunk.text == "\n".join(
        [
            "GET /animals/{animalId}",
            "operationId: getAnimal",
            "summary: Get an animal",
            "description: One animal.",
            "tags: animals",
            "parameters:",
            "  animalId (path, required): integer",
            "responses:",
            "  200: Animal",
            "  404: Not found.",
            "served at: /api/animals/{animalId}",
            "url: /zoo/animals/{animalId}",
            "served by:",
            "  method:org.acme.zoo.web.AnimalController#getAnimal(int)",
        ]
    )
    assert chunk.embed_text.startswith(f"{SPEC_PATH}\nendpoint GET /animals/{{animalId}}\nGET")
    assert (chunk.path, chunk.start_line) == (SPEC_PATH, 12)


def test_schema_chunk_is_its_yaml(chunks, graph, java_files):
    chunk = chunks[f"schema:{SPEC_PATH}#Animal"]
    assert chunk.text == SPEC.schemas[0].text.rstrip()
    assert chunk.embed_text == f"{SPEC_PATH}\nschema Animal\n{chunk.text}"
    without_spec = {c.id: c for c in make_chunks(graph, java_files, SOURCES)}
    outline = without_spec[f"schema:{SPEC_PATH}#Animal"].text
    assert outline == "Animal:\n  required: name\n  properties:\n    name: string"


def test_doc_chunks_skip_sections_with_only_a_heading(chunks):
    zoo = chunks["doc:docs/zoo.md#zoo"]
    assert zoo.text == "# Zoo\n\nThe Keeper feeds animals."
    assert zoo.embed_text == "docs/zoo.md\nZoo\n" + zoo.text
    assert (zoo.path, zoo.start_line, zoo.end_line) == ("docs/zoo.md", 2, 4)
    assert chunks["doc:docs/zoo.md#feeding"].embed_text.startswith("docs/zoo.md\nZoo > Feeding\n")
    assert chunks["doc:docs/zoo.md"].embed_text == "docs/zoo.md\nIntro line."
    assert "doc:docs/zoo.md#hours" not in chunks


def test_a_missing_source_falls_back_to_the_signature(graph, java_files):
    chunks = {c.id: c for c in make_chunks(graph, java_files, {}, [SPEC], DOCS)}
    feed = chunks["method:org.acme.zoo.Keeper#feed(String)"]
    assert feed.text == "/**\n * Feeds an animal.\n * Twice if hungry.\n */\npublic void feed(String animal)"
    assert "method:org.acme.zoo.Animal#getName()" not in chunks  # still trivial, by its lines


def long_method_files(lines: int = 60, width: int = 30) -> tuple[list, dict[str, str]]:
    body = "\n".join(f'        log("{"x" * width} {i}");' for i in range(lines))
    path = f"{ZOO}/Logger.java"
    source = (
        "package org.acme.zoo;\n\n@Service\npublic class Logger {\n"
        f"    public void logAll() {{\n{body}\n    }}\n\n    void log(String s) {{\n    }}\n}}\n"
    )
    return [parse_java(source, path)], {path: source}


def test_long_chunks_are_split_at_line_boundaries():
    files, sources = long_method_files()
    graph = build_graph(files)
    whole = {c.id: c for c in make_chunks(graph, files, sources)}["method:org.acme.zoo.Logger#logAll()"]
    parts = [c for c in make_chunks(graph, files, sources, max_chars=400) if c.node_id == whole.id]
    assert [c.id for c in parts] == [whole.id] + [f"{whole.id}#part{n}" for n in range(2, len(parts) + 1)]
    assert len(parts) > 3
    header = whole.embed_text[: -len(whole.text)]
    for part in parts:
        assert part.embed_text == header + part.text and len(part.embed_text) <= 400
        assert part.kind == "method" and part.path == whole.path
        assert part.content_hash == hashlib.sha256(part.embed_text.encode()).hexdigest()
    assert "\n".join(p.text for p in parts) == whole.text
    assert parts[0].start_line == whole.start_line and parts[-1].end_line == whole.end_line
    for before, after in zip(parts, parts[1:]):
        assert after.start_line == before.end_line + 1


def test_a_line_longer_than_the_budget_is_cut():
    files, sources = long_method_files(lines=2, width=900)
    chunks = make_chunks(build_graph(files), files, sources, max_chars=300)
    assert all(len(c.embed_text) <= 300 for c in chunks)
    assert len([c for c in chunks if c.node_id.endswith("#logAll()")]) > 6


def test_sorted_by_id_and_deterministic(graph, java_files):
    first = make_chunks(graph, java_files, SOURCES, [SPEC], DOCS)
    assert [c.id for c in first] == sorted(c.id for c in first)
    again = make_chunks(graph, list(reversed(java_files)), SOURCES, [SPEC], list(reversed(DOCS)))
    assert to_dict(again) == to_dict(first)
