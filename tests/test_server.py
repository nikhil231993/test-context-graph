"""The `tcg-mcp` server: its tool functions called directly, the tools as FastMCP registers them,
and one round trip over stdio. The bookshop fixture is indexed once per module with
HashEmbedder."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from test_context_graph import server
from test_context_graph.context import ContextError
from test_context_graph.embeddings import HashEmbedder
from test_context_graph.indexer import index_repo

BOOKSHOP = Path(__file__).parent / "fixtures" / "bookshop"
GET_BOOK = "method:com.example.bookshop.rest.controller.BookRestController#getBook(Integer)"
TOOLS = {"get_test_context", "search_code", "get_node", "get_neighbors", "find_tests", "index_status", "list_outbound_calls", "get_contract_context"}


@pytest.fixture(scope="module")
def index_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("server")
    repo = Path(shutil.copytree(BOOKSHOP, root / "bookshop"))
    index_repo(repo, root / "index", HashEmbedder())
    return root / "index"


@pytest.fixture(autouse=True)
def use_index(index_dir, monkeypatch):
    """Every test starts with no engine, pointed at the bookshop index."""
    monkeypatch.setenv("TCG_INDEX", str(index_dir))
    server.reset_engine()
    yield
    server.reset_engine()


# ---- the tools, called directly ------------------------------------------------------------


def test_get_test_context_is_markdown_by_default():
    text = server.get_test_context("BookRestController#getBook")
    assert isinstance(text, str)
    assert text.startswith("# Test context: BookRestController#getBook(Integer)")
    assert "## Existing tests" in text and "BookRestControllerTests" in text


def test_get_test_context_as_json():
    data = server.get_test_context("BookRestController#getBook", format="json", max_tests=1)
    assert data["target"]["id"] == GET_BOOK and len(data["existing_tests"]) == 1
    json.dumps(data)  # JSON-ready


def test_get_test_context_rejects_an_unknown_format():
    with pytest.raises(ContextError, match="format must be"):
        server.get_test_context("BookRestController#getBook", format="html")


def test_search_code():
    hits = server.search_code("find a book by its id", kinds=["method"], limit=3)
    assert 0 < len(hits) <= 3 and all(h["kind"] == "method" for h in hits)


def test_get_node():
    info = server.get_node("GET /books/{bookId}")
    assert info["node"]["id"] == "endpoint:GET /books/{bookId}"
    assert "served at: /api/books/{bookId}" in info["text"]


def test_get_neighbors():
    result = server.get_neighbors("endpoint:GET /books/{bookId}", edge_kinds=["SERVES"], direction="in")
    assert [n["id"] for n in result["neighbors"]] == [GET_BOOK]


def test_find_tests():
    result = server.find_tests("BookServiceImpl#findBook")
    assert result["tests"][0]["name"] == "AbstractBookServiceTests"


def test_index_status():
    status = server.index_status()
    assert status["indexed"] and status["repo_name"] == "bookshop"
    assert status["embedding_model"] == "hash-256"


def test_index_status_without_an_index(tmp_path, monkeypatch):
    monkeypatch.setenv("TCG_INDEX", str(tmp_path / "none"))
    status = server.index_status()
    assert status == {"indexed": False, "index_dir": str(tmp_path / "none"),
                      "error": status["error"]}  # fmt: skip
    assert "run `tcg index <repo>`" in status["error"]


def test_the_engine_is_reused_until_the_index_changes(index_dir):
    engine = server.get_engine()
    assert server.get_engine() is engine
    manifest = index_dir / "manifest.json"
    stat = manifest.stat()
    os.utime(manifest, (stat.st_atime, stat.st_mtime + 10))  # what a new `tcg index` run does
    assert server.get_engine() is not engine


def test_errors_reach_the_agent_as_tool_errors():
    with pytest.raises(ContextError, match="no symbol matches 'Nope'"):
        server.get_node("Nope")


# ---- through FastMCP -----------------------------------------------------------------------


async def test_the_tools_are_registered_with_their_docs():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(tools) == TOOLS
    assert tools["get_test_context"].description.startswith("START HERE")
    parameters = set(tools["get_neighbors"].inputSchema["properties"])
    assert parameters == {"node_id", "edge_kinds", "direction", "limit"}


async def test_a_tool_call_through_fastmcp():
    content = await server.mcp.call_tool("find_tests", {"symbol": "BookService"})
    blocks = content[0] if isinstance(content, tuple) else content  # (content, structured) in newer SDKs
    assert json.loads(blocks[0].text)["tests"][0]["name"] == "AbstractBookServiceTests"


async def test_a_round_trip_over_stdio(index_dir):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "test_context_graph.server", "--index", str(index_dir)],
        env={**os.environ, "TCG_INDEX": ""},  # --index wins
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.serverInfo.name == "test-context-graph" and "get_test_context" in init.instructions
        assert {tool.name for tool in (await session.list_tools()).tools} == TOOLS
        result = await session.call_tool("get_test_context", {"symbol": "BookServiceImpl#findBook"})
        assert not result.isError
        assert result.content[0].text.startswith("# Test context: BookServiceImpl#findBook(int)")
        failed = await session.call_tool("get_node", {"symbol": "Nope"})
        assert failed.isError and "no symbol matches 'Nope'" in failed.content[0].text
