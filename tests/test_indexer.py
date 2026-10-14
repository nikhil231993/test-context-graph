"""Indexing a repository end to end, and re-indexing only what changed."""

import json
import shutil
from pathlib import Path

import pytest

from test_context_graph.embeddings import HashEmbedder
from test_context_graph.indexer import index_repo
from test_context_graph.store import IndexStore

BOOKSHOP = Path(__file__).parent / "fixtures" / "bookshop"
CONTROLLER = "src/main/java/com/example/bookshop/rest/controller/BookRestController.java"


@pytest.fixture
def repo(tmp_path):
    """A copy of the bookshop fixture, so tests can change it."""
    return Path(shutil.copytree(BOOKSHOP, tmp_path / "bookshop"))


@pytest.fixture
def index(tmp_path):
    return tmp_path / "index"


def test_first_run_indexes_everything(repo, index):
    result = index_repo(repo, index, HashEmbedder())

    assert result.reembedded_all
    assert result.files.added == sorted(result.files.added) and len(result.files.added) == 12
    assert result.counts["files.java"] == 10
    assert result.counts["files.openapi"] == 1 and result.counts["files.markdown"] == 1
    assert result.embedded == result.counts["chunks"] > 0
    assert result.warnings == []

    store = IndexStore(index)
    manifest = store.read_manifest()
    assert manifest.repo_name == "bookshop" and manifest.embedding_model == "hash-256"
    assert manifest.embedding_dim == 256
    assert len(manifest.chunks) == store.count_chunks() == result.embedded
    nodes = json.loads((index / "nodes.json").read_text())
    ids = {node["id"] for node in nodes["nodes"]}
    assert "type:com.example.bookshop.rest.controller.BookRestController" in ids
    assert "endpoint:GET /books/{bookId}" in ids
    assert nodes["meta"]["repo_name"] == "bookshop"


def test_second_run_on_an_unchanged_repo_embeds_nothing(repo, index):
    index_repo(repo, index, HashEmbedder())
    result = index_repo(repo, index, HashEmbedder())

    assert not result.files.has_changes
    assert (result.embedded, result.deleted, result.reembedded_all) == (0, 0, False)


def test_a_changed_method_is_re_embedded_alone(repo, index):
    first = index_repo(repo, index, HashEmbedder())
    controller = repo / CONTROLLER
    controller.write_text(controller.read_text().replace("HttpStatus.NOT_FOUND", "HttpStatus.GONE"))

    result = index_repo(repo, index, HashEmbedder())

    assert result.files.changed == [CONTROLLER]
    assert 1 <= result.embedded < first.embedded
    assert result.deleted == 0
    chunk = IndexStore(index).get_chunks(
        ["method:com.example.bookshop.rest.controller.BookRestController#getBook(Integer)"]
    )[0]
    assert "HttpStatus.GONE" in chunk.text


def test_a_removed_file_removes_its_chunks(repo, index):
    index_repo(repo, index, HashEmbedder())
    (repo / "src/main/java/com/example/bookshop/repository/jdbc/JdbcBookRepository.java").unlink()

    result = index_repo(repo, index, HashEmbedder())

    assert result.files.removed == ["src/main/java/com/example/bookshop/repository/jdbc/JdbcBookRepository.java"]
    assert result.deleted >= 1 and result.embedded == 0
    assert all("JdbcBookRepository" not in chunk_id for chunk_id in IndexStore(index).chunk_hashes())


def test_a_new_embedding_model_or_full_re_embeds_everything(repo, index):
    index_repo(repo, index, HashEmbedder())

    other_model = index_repo(repo, index, HashEmbedder(dim=64))
    assert other_model.reembedded_all and other_model.embedded == other_model.counts["chunks"]
    assert IndexStore(index).read_manifest().embedding_dim == 64

    full = index_repo(repo, index, HashEmbedder(dim=64), full=True)
    assert full.reembedded_all and full.embedded == full.counts["chunks"]


def test_unparseable_files_become_warnings(repo, index):
    (repo / "src/main/java/com/example/bookshop/Broken.java").write_text("class Broken { void f( }")
    (repo / "src/main/resources/broken-api.yml").write_text("openapi: 3.0.1\npaths: [unclosed\n")

    result = index_repo(repo, index, HashEmbedder())

    assert any(w.startswith("src/main/java/com/example/bookshop/Broken.java: line 1") for w in result.warnings)
    assert any(w.startswith("src/main/resources/broken-api.yml:") for w in result.warnings)
    assert result.counts["chunks"] > 0  # the rest is still indexed


def test_progress_is_reported(repo, index):
    calls = []
    index_repo(repo, index, HashEmbedder(), progress=lambda done, total: calls.append((done, total)))
    assert calls and calls[-1][0] == calls[-1][1]
