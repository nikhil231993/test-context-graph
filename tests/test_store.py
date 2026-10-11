import hashlib
import json
import random
from dataclasses import fields
from pathlib import Path

import pytest

from test_context_graph import store as store_module
from test_context_graph.embeddings import HashEmbedder
from test_context_graph.models import (
    SCHEMA_VERSION,
    Chunk,
    Edge,
    FileEntry,
    Graph,
    Manifest,
    Node,
)
from test_context_graph.store import COLUMNS, IndexStore


@pytest.fixture
def index_dir(tmp_path: Path) -> Path:
    return tmp_path / "repo" / ".tcg"


@pytest.fixture
def store(index_dir: Path) -> IndexStore:
    return IndexStore(index_dir)


# ---- the graph ----------------------------------------------------------------------------


def sample_graph() -> Graph:
    owner_file = "src/main/java/org/example/Owner.java"
    return Graph(
        nodes=[
            Node(
                id="type:org.example.Owner",
                kind="type",
                name="Owner",
                path=owner_file,
                start_line=3,
                end_line=40,
                attrs={"role": "entity", "annotations": [{"name": "Entity", "arguments": {}}]},
            ),
            Node(id=f"file:{owner_file}", kind="file", name="Owner.java", path=owner_file),
            Node(
                id="endpoint:GET /owners/{ownerId}",
                kind="endpoint",
                name="GET /owners/{ownerId}",
                attrs={"served_path": "/api/owners/{ownerId}", "summary": "Größe – naïve"},
            ),
            Node(
                id="method:org.example.Owner#getPet(String,boolean)",
                kind="method",
                name="getPet",
                path=owner_file,
                start_line=20,
                end_line=25,
            ),
        ],
        edges=[
            Edge("method:org.example.Owner#a()", "method:org.example.Owner#b()", "CALLS",
                 {"line": 7, "resolution": "same_type"}),
            Edge("type:org.example.OwnerController", "type:org.example.OwnerService", "INJECTS",
                 {"via": "constructor", "name": "ownerService"}),
            Edge("method:org.example.Owner#a()", "method:org.example.Owner#b()", "CALLS",
                 {"line": 12, "resolution": "same_type"}),
            Edge(f"file:{owner_file}", "type:org.example.Owner", "CONTAINS"),
        ],
    )


def test_graph_round_trip_sorts_nodes_and_edges(store: IndexStore):
    graph = sample_graph()
    store.write_graph(graph, meta={"repo_name": "clinic"})
    loaded = store.read_graph()
    assert loaded.nodes == sorted(graph.nodes, key=lambda node: node.id)
    assert [(e.kind, e.source, e.target) for e in loaded.edges] == sorted(
        (e.kind, e.source, e.target) for e in graph.edges
    )
    assert sorted(loaded.edges, key=lambda e: json.dumps(e.attrs)) == sorted(
        graph.edges, key=lambda e: json.dumps(e.attrs)
    )
    assert store.read_meta() == {"repo_name": "clinic"}


def test_graph_files_are_versioned_indented_json_with_unicode_kept(store: IndexStore, index_dir: Path):
    store.write_graph(sample_graph())
    nodes_text = (index_dir / "nodes.json").read_text(encoding="utf-8")
    nodes = json.loads(nodes_text)
    edges = json.loads((index_dir / "edges.json").read_text(encoding="utf-8"))
    assert list(nodes) == ["schema_version", "meta", "nodes"]
    assert list(edges) == ["schema_version", "edges"]
    assert nodes["schema_version"] == edges["schema_version"] == SCHEMA_VERSION
    assert nodes["meta"] == {}
    assert nodes_text.splitlines()[1] == f' "schema_version": {SCHEMA_VERSION},'
    assert "Größe – naïve" in nodes_text
    assert store.read_meta() == {}


def test_graph_files_do_not_depend_on_input_order(tmp_path: Path):
    graph = sample_graph()
    shuffled = Graph(nodes=list(graph.nodes), edges=list(graph.edges))
    random.Random(7).shuffle(shuffled.nodes)
    random.Random(7).shuffle(shuffled.edges)
    first, second = IndexStore(tmp_path / "a"), IndexStore(tmp_path / "b")
    first.write_graph(graph)
    second.write_graph(shuffled)
    for name in ("nodes.json", "edges.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_writes_create_the_folder_and_leave_no_temp_files(store: IndexStore, index_dir: Path):
    assert not index_dir.exists()
    store.write_graph(sample_graph())
    store.write_graph(sample_graph())  # overwrite
    store.write_manifest(Manifest())
    assert sorted(p.name for p in index_dir.iterdir()) == ["edges.json", "manifest.json", "nodes.json"]


def test_a_failed_write_keeps_the_previous_file(store: IndexStore, index_dir: Path):
    store.write_graph(sample_graph(), meta={"run": 1})
    before = (index_dir / "nodes.json").read_bytes()
    with pytest.raises(TypeError):
        store.write_graph(sample_graph(), meta={"run": object()})  # not JSON-serialisable
    assert (index_dir / "nodes.json").read_bytes() == before
    assert sorted(p.name for p in index_dir.iterdir()) == ["edges.json", "nodes.json"]


def test_read_graph_without_an_index_says_how_to_make_one(store: IndexStore, index_dir: Path):
    with pytest.raises(FileNotFoundError, match="run `tcg index <repo>`"):
        store.read_graph()
    assert not index_dir.exists()


def test_read_graph_with_edges_missing(store: IndexStore, index_dir: Path):
    store.write_graph(sample_graph())
    (index_dir / "edges.json").unlink()
    with pytest.raises(FileNotFoundError, match="edges.json"):
        store.read_graph()


def test_read_graph_rejects_another_schema_version(store: IndexStore, index_dir: Path):
    store.write_graph(sample_graph())
    data = json.loads((index_dir / "nodes.json").read_text(encoding="utf-8"))
    data["schema_version"] = SCHEMA_VERSION + 1
    (index_dir / "nodes.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="schema version"):
        store.read_graph()


@pytest.mark.parametrize("content", ['{"schema_version": 1, "nodes": [', "[]", "\udcff"])
def test_read_graph_rejects_a_corrupt_file(store: IndexStore, index_dir: Path, content: str):
    store.write_graph(sample_graph())
    (index_dir / "nodes.json").write_bytes(content.encode("utf-8", errors="surrogateescape"))
    with pytest.raises(ValueError, match="nodes.json"):
        store.read_graph()


# ---- the manifest -------------------------------------------------------------------------


def sample_manifest() -> Manifest:
    return Manifest(
        tool_version="0.1.0",
        generated_at="2026-10-11T12:00:00Z",
        repo_name="clinic",
        repo_commit="a" * 40,
        embedding_model="hash-64",
        embedding_dim=64,
        files={
            "src/main/java/Owner.java": FileEntry(
                "src/main/java/Owner.java", "f" * 64, 120, "java", "main"
            ),
            "README.md": FileEntry("README.md", "e" * 64, 40, "markdown", "other"),
        },
        chunks={"type:org.example.Owner": "1" * 64, "doc:README.md#clinic": "2" * 64},
        counts={"nodes": 3, "edges": 2, "chunks": 2, "files": 2},
    )


def test_manifest_round_trip(store: IndexStore, index_dir: Path):
    manifest = sample_manifest()
    store.write_manifest(manifest)
    assert store.read_manifest() == manifest
    data = json.loads((index_dir / "manifest.json").read_text(encoding="utf-8"))
    assert list(data["files"]) == ["README.md", "src/main/java/Owner.java"]
    assert list(data["chunks"]) == ["doc:README.md#clinic", "type:org.example.Owner"]


def test_read_manifest_without_one_is_none(store: IndexStore, index_dir: Path):
    assert store.read_manifest() is None
    assert not index_dir.exists()


@pytest.mark.parametrize("content", ["{not json", "[]", '{"files": ["a.java"]}'])
def test_an_unreadable_manifest_reads_as_none_so_a_full_index_can_replace_it(
    store: IndexStore, index_dir: Path, content: str
):
    index_dir.mkdir(parents=True)
    (index_dir / "manifest.json").write_text(content, encoding="utf-8")
    assert store.read_manifest() is None
    store.write_manifest(sample_manifest())
    assert store.read_manifest() == sample_manifest()


# ---- the chunk table ----------------------------------------------------------------------

EMBEDDER = HashEmbedder(64)


def make_chunk(
    chunk_id: str,
    text: str = "",
    kind: str = "method",
    path: str | None = "src/main/java/org/example/OwnerService.java",
    lines: tuple[int, int] | None = (10, 20),
) -> Chunk:
    text = text or f"code of {chunk_id}"
    embed_text = f"{kind} {chunk_id}\n{text}"
    return Chunk(
        id=chunk_id,
        node_id=chunk_id.split("#part")[0],
        kind=kind,
        text=text,
        embed_text=embed_text,
        content_hash=hashlib.sha256(embed_text.encode("utf-8")).hexdigest(),
        path=path,
        start_line=lines[0] if lines else None,
        end_line=lines[1] if lines else None,
    )


def upsert(store: IndexStore, chunks: list[Chunk]) -> None:
    store.upsert_chunks(chunks, EMBEDDER.embed_documents([c.embed_text for c in chunks]))


def unit(*values: float) -> list[float]:
    """A 64-dim vector starting with `values`."""
    return list(values) + [0.0] * (EMBEDDER.dim - len(values))


TRICKY_IDS = [
    "method:org.example.Owner#getPet(String,boolean)",
    "doc:README.md#it's-a-test",
    "doc:docs/O'Brien's notes.md",
    "endpoint:GET /owners/{ownerId}",
    "schema:src/main/resources/openapi.yml#Owner",
    "doc:docs\\windows\\path.md#a\\",
    "doc:docs/naïve.md#über-uns",
    "method:org.example.Owner#<init>()#part2",
]


def test_chunk_columns_cover_every_chunk_field():
    assert sorted(COLUMNS) == sorted(f.name for f in fields(Chunk))


def test_everything_works_before_the_table_exists(store: IndexStore, index_dir: Path):
    assert store.count_chunks() == 0
    assert store.chunk_hashes() == {}
    assert store.get_chunks(["method:x#y()"]) == []
    assert store.search(unit(1.0)) == []
    store.delete_chunks(["method:x#y()"])
    store.reset_chunks()
    store.upsert_chunks([], [])
    assert not index_dir.exists()  # reading never creates an index


def test_reads_work_with_a_graph_but_no_table_yet(store: IndexStore):
    store.write_graph(sample_graph())
    assert store.count_chunks() == 0
    assert store.search(unit(1.0), kinds=["method"], path_prefix="src/") == []


def test_upsert_creates_the_table_and_reads_return_the_chunks(store: IndexStore, index_dir: Path):
    chunks = [
        make_chunk("type:org.example.Owner", kind="type"),
        make_chunk("endpoint:GET /owners", kind="endpoint", path=None, lines=None),
    ]
    upsert(store, chunks)
    assert (index_dir / "lancedb").is_dir()
    assert store.count_chunks() == 2
    assert store.chunk_hashes() == {c.id: c.content_hash for c in chunks}
    assert store.get_chunks([chunks[1].id, "missing", chunks[0].id, chunks[1].id]) == [
        chunks[1],
        chunks[0],
    ]


def test_upsert_replaces_chunks_with_the_same_id(store: IndexStore):
    old = make_chunk("method:org.example.OwnerService#find(int)", text="return repo.find(id);")
    other = make_chunk("type:org.example.OwnerService", kind="type")
    upsert(store, [old, other])
    new = make_chunk(old.id, text="return repo.findById(id).orElseThrow();", lines=(10, 22))
    upsert(store, [new])
    assert store.count_chunks() == 2
    assert store.get_chunks([old.id]) == [new]
    assert store.chunk_hashes() == {new.id: new.content_hash, other.id: other.content_hash}


def test_upsert_keeps_the_last_of_a_repeated_id(store: IndexStore):
    first, last = make_chunk("type:a.Dup", text="first"), make_chunk("type:a.Dup", text="last")
    upsert(store, [first, last])
    assert store.count_chunks() == 1
    upsert(store, [first, last])  # again, now that the id is stored
    assert store.get_chunks(["type:a.Dup"]) == [last]


def test_upsert_checks_the_vectors(store: IndexStore):
    chunk = make_chunk("type:a.B")
    with pytest.raises(ValueError, match="1 chunks but 2 vectors"):
        store.upsert_chunks([chunk], [unit(1.0), unit(1.0)])
    with pytest.raises(ValueError, match="same"):
        store.upsert_chunks([chunk, make_chunk("type:a.C")], [unit(1.0), [1.0, 0.0]])
    with pytest.raises(ValueError):
        store.upsert_chunks([chunk], [[]])


def test_a_new_dimension_needs_a_reset(store: IndexStore):
    upsert(store, [make_chunk("type:a.B")])
    with pytest.raises(ValueError, match="64-dim"):
        store.upsert_chunks([make_chunk("type:a.C")], [[1.0, 0.0, 0.0]])
    with pytest.raises(ValueError, match="64-dim"):
        store.search([1.0, 0.0, 0.0])
    store.reset_chunks()
    assert store.count_chunks() == 0
    assert store.chunk_hashes() == {}
    store.upsert_chunks([make_chunk("type:a.C")], [[1.0, 0.0, 0.0]])
    assert [c.id for c, _ in store.search([1.0, 0.0, 0.0])] == ["type:a.C"]


def test_ids_with_quotes_and_punctuation(store: IndexStore):
    chunks = [make_chunk(chunk_id) for chunk_id in TRICKY_IDS]
    upsert(store, chunks)
    assert store.get_chunks(TRICKY_IDS) == chunks
    assert set(store.chunk_hashes()) == set(TRICKY_IDS)
    store.delete_chunks([TRICKY_IDS[1], TRICKY_IDS[2], TRICKY_IDS[5], "doc:not-stored'"])
    remaining = [i for n, i in enumerate(TRICKY_IDS) if n not in (1, 2, 5)]
    assert sorted(store.chunk_hashes()) == sorted(remaining)
    store.delete_chunks([])
    assert store.count_chunks() == len(remaining)


def test_long_id_lists_are_split_into_several_filters(store: IndexStore, monkeypatch):
    monkeypatch.setattr(store_module, "_IDS_PER_FILTER", 2)
    chunks = [make_chunk(f"method:a.B#m{i}()") for i in range(7)]
    upsert(store, chunks)
    assert store.get_chunks([c.id for c in reversed(chunks)]) == list(reversed(chunks))
    store.delete_chunks([c.id for c in chunks[:5]])
    assert sorted(store.chunk_hashes()) == sorted(c.id for c in chunks[5:])


def test_reads_return_every_row_not_a_default_page(store: IndexStore):
    chunks = [make_chunk(f"method:a.B#m{i}()") for i in range(25)]
    upsert(store, chunks)
    assert store.count_chunks() == 25
    assert len(store.chunk_hashes()) == 25
    assert len(store.get_chunks([c.id for c in chunks])) == 25


def test_the_table_persists_across_store_instances(index_dir: Path):
    chunk = make_chunk("type:a.B")
    upsert(IndexStore(index_dir), [chunk])
    assert IndexStore(index_dir).get_chunks([chunk.id]) == [chunk]


# ---- search -------------------------------------------------------------------------------


@pytest.fixture
def searchable(store: IndexStore) -> IndexStore:
    """Chunks on known axes, so the expected similarities are exact."""
    main, test = "src/main/java/a/Owners.java", "src/test/java/a/OwnersTests.java"
    rows = [
        ("method:a.Owners#find()", "method", main, unit(1.0)),
        ("method:a.Owners#list()", "method", main, unit(0.8, 0.6)),
        ("type:a.Owners", "type", main, unit(0.6, 0.8)),
        ("method:a.OwnersTests#finds()", "method", test, unit(0.0, 1.0)),
        ("doc:README.md#owners", "doc", "README.md", unit(-1.0)),
        ("endpoint:GET /owners", "endpoint", None, unit(0.0, 0.0, 1.0)),
        ("doc:src/ma_n/odd%path.md", "doc", "src/ma_n/odd%path.md", unit(0.0, 1.0)),
        ("doc:docs/it's.md", "doc", "docs/it's.md", unit(0.0, 0.6, 0.8)),
    ]
    chunks = [
        make_chunk(chunk_id, kind=kind, path=path, lines=(1, 9) if path else None)
        for chunk_id, kind, path, _ in rows
    ]
    store.upsert_chunks(chunks, [vector for *_, vector in rows])
    return store


def ids(results: list[tuple[Chunk, float]]) -> list[str]:
    return [chunk.id for chunk, _ in results]


def test_search_returns_the_nearest_chunks_best_first(searchable: IndexStore):
    results = searchable.search(unit(1.0), limit=3)
    assert ids(results) == ["method:a.Owners#find()", "method:a.Owners#list()", "type:a.Owners"]
    assert [round(score, 4) for _, score in results] == [1.0, 0.8, 0.6]
    chunk, _ = results[0]
    assert chunk == searchable.get_chunks(["method:a.Owners#find()"])[0]


def test_search_similarity_runs_from_minus_one_to_one(searchable: IndexStore):
    results = searchable.search(unit(1.0), limit=100)
    assert len(results) == 8
    assert results[-1][0].id == "doc:README.md#owners"
    assert results[-1][1] == pytest.approx(-1.0, abs=1e-5)
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


def test_search_by_kind_filters_before_the_limit(searchable: IndexStore):
    # Docs are the furthest from this query: a filter after the search would find none of them.
    results = searchable.search(unit(1.0), limit=2, kinds=["doc"])
    assert ids(results) == ["doc:docs/it's.md", "doc:src/ma_n/odd%path.md"]
    assert set(ids(searchable.search(unit(1.0), limit=10, kinds=["type", "endpoint"]))) == {
        "type:a.Owners",
        "endpoint:GET /owners",
    }
    assert len(searchable.search(unit(1.0), limit=10, kinds=[])) == 8  # empty: every kind


def test_search_by_path_prefix(searchable: IndexStore):
    assert ids(searchable.search(unit(0.0, 1.0), limit=10, path_prefix="src/test/")) == [
        "method:a.OwnersTests#finds()"
    ]
    assert set(ids(searchable.search(unit(1.0), limit=10, path_prefix="src/main/"))) == {
        "method:a.Owners#find()",
        "method:a.Owners#list()",
        "type:a.Owners",
    }
    # The prefix is literal: '_' and '%' are not wildcards, a quote is just a quote.
    assert ids(searchable.search(unit(1.0), limit=10, path_prefix="src/ma_n/")) == [
        "doc:src/ma_n/odd%path.md"
    ]
    assert ids(searchable.search(unit(1.0), limit=10, path_prefix="docs/it'")) == ["doc:docs/it's.md"]
    assert searchable.search(unit(1.0), limit=10, path_prefix="src/m%") == []


def test_search_with_kind_and_path_prefix(searchable: IndexStore):
    results = searchable.search(unit(1.0), limit=10, kinds=["type"], path_prefix="src/main/")
    assert ids(results) == ["type:a.Owners"]


def test_search_with_no_limit_returns_nothing(searchable: IndexStore):
    assert searchable.search(unit(1.0), limit=0) == []


def test_search_with_hash_embeddings_finds_the_related_code(store: IndexStore):
    chunks = [
        make_chunk("method:a.OwnerRepository#findByLastName(String)",
                   text="Collection<Owner> findByLastName(String lastName);"),
        make_chunk("method:a.PetTypeFormatter#print(PetType,Locale)",
                   text="public String print(PetType petType, Locale locale) { return ...; }"),
        make_chunk("doc:README.md#database-configuration", kind="doc", path="README.md",
                   text="## Database configuration\nIn its default configuration, the app uses H2."),
    ]
    upsert(store, chunks)
    results = store.search(EMBEDDER.embed_query("find owners by last name"), limit=3)
    assert ids(results)[0] == "method:a.OwnerRepository#findByLastName(String)"
