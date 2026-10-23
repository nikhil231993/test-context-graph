"""`tcg-mcp`: the context engine as an MCP server over stdio, for coding agents.

The index folder comes from --index or env TCG_INDEX (default ".tcg"). The engine is created on
the first call and reused; it's reloaded when the index is rebuilt (manifest.json is written
last, so its mtime marks a complete new index). Problems reach the agent as tool errors that
say what to do, e.g. "run `tcg index <repo>`".
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .context import ContextEngine, ContextError, render_markdown
from .contract import ContractContext, render_call_markdown, render_calls_markdown

INSTRUCTIONS = """\
Context for writing tests in a Java Spring codebase, from an index built by `tcg index <repo>`.
To write or fix a test for a method or class, call get_test_context first: one call returns the
code, the dependencies to mock (and how existing tests mock them), the data types (generated
DTOs with their OpenAPI fields), the endpoints it serves, existing tests to copy, conventions
and a suggested test file. Use search_code to find code by meaning when you don't know the
name, find_tests for the existing tests of a symbol, get_node and get_neighbors to explore.
For contract tests of this system as a consumer (Specmatic): list_outbound_calls lists the
provider endpoints it calls, grouped by provider; get_contract_context gives one call's request,
types, base-URL property, matching provider operation, entry points and a draft example."""

mcp = FastMCP("test-context-graph", instructions=INSTRUCTIONS)

_engine: ContextEngine | None = None
_engine_key: tuple[str, float | None] | None = None


def index_dir() -> Path:
    return Path(os.environ.get("TCG_INDEX") or ".tcg")


def get_engine() -> ContextEngine:
    """The engine for the current index: created once, again only when the index changed."""
    global _engine, _engine_key
    folder = index_dir()
    manifest = folder / "manifest.json"
    key = (str(folder.resolve()), manifest.stat().st_mtime if manifest.is_file() else None)
    if _engine is None or key != _engine_key:
        _engine = ContextEngine(folder)
        _engine_key = key
    return _engine


def reset_engine() -> None:
    """Forget the engine (tests switch indexes)."""
    global _engine, _engine_key
    _engine, _engine_key = None, None


@mcp.tool(structured_output=False)
def get_test_context(
    symbol: str, format: str = "markdown", max_tests: int = 3
) -> str | dict[str, Any]:
    """START HERE to write a test. Everything needed to test a Java method or class, in one call:
    its source and enclosing class; the dependencies to mock, with the methods it calls on each
    and how existing tests mock them; other calls (private helpers with source); the data types
    it takes and returns (generated DTOs come with their OpenAPI schema fields); the endpoints it
    serves (served path for MockMvc, parameters, responses, tests already calling them); existing
    test classes to copy (setup, mocks, related test methods with source, subclasses run under
    other profiles); implementations of interfaces; the frameworks the tests use, the naming
    convention and a suggested test file; related docs; and notes (ambiguity, generated code).

    symbol: Type#method, Type.method, Type#method(String,int), a type name (simple or
    qualified), a node id, an endpoint ("GET /owners/{ownerId}" or the served path), or a file.
    format: "markdown" (default, compact) or "json" (the same as a dict).
    max_tests: how many existing test classes to show (default 3).
    """
    if format not in ("markdown", "json"):
        raise ContextError(f"format must be 'markdown' or 'json', not {format!r}")
    context = get_engine().test_context(symbol, max_tests=max_tests)
    return context if format == "json" else render_markdown(context)


@mcp.tool(structured_output=False)
def search_code(
    query: str, kinds: list[str] | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Search the indexed code, OpenAPI endpoints and schemas, and docs by meaning (local
    embeddings), when you don't know a symbol's name: "validate a pet's birth date".
    Returns hits, best first: id (pass it to get_test_context or get_node), kind, name, path,
    lines, score, snippet.

    kinds: only these node kinds: method, type, endpoint, schema, doc (default: all).
    limit: hits to return (default 10).
    """
    return get_engine().search(query, kinds=kinds, limit=limit)


@mcp.tool(structured_output=False)
def get_node(symbol: str) -> dict[str, Any]:
    """One node of the graph (a type, method, endpoint, schema, doc section or file): its
    attributes (signature, annotations, role, served paths...), its text (source, outline, spec
    or doc), and how many edges of each kind it has. Use it to read code get_test_context left
    out. Ambiguous symbols return the best match and list the others under "candidates"."""
    return get_engine().get_node(symbol)


@mcp.tool(structured_output=False)
def get_neighbors(
    node_id: str,
    edge_kinds: list[str] | None = None,
    direction: str = "both",
    limit: int = 50,
) -> dict[str, Any]:
    """The nodes one edge away from a node, to explore the graph: who calls a method (CALLS,
    "in"), what a class injects (INJECTS, "out"), which tests test it (TESTS, "in"), what
    implements an interface (IMPLEMENTS, "in"), which method serves an endpoint (SERVES, "in").

    node_id: a node id from another tool (or any symbol get_node accepts).
    edge_kinds: CONTAINS, EXTENDS, IMPLEMENTS, INJECTS, CALLS, OVERRIDES, USES, SERVES, TESTS,
    MOCKS, GENERATED_FROM, REFERENCES, MENTIONS, CALLS_HTTP, CONFIGURED_BY, TARGETS (default: all).
    direction: "out" (from the node), "in" (to it) or "both".
    """
    return get_engine().neighbors(node_id, edge_kinds=edge_kinds, direction=direction, limit=limit)


@mcp.tool(structured_output=False)
def find_tests(symbol: str) -> dict[str, Any]:
    """The existing tests of a method, class or endpoint, strongest link first: test classes
    with why each is related (calls the method or its endpoint, tests the class or an interface
    it implements), their test methods, and subclasses that run them under other profiles."""
    return get_engine().find_tests(symbol)


@mcp.tool(structured_output=False)
def list_outbound_calls(symbol: str | None = None, format: str = "markdown") -> str | dict[str, Any]:
    """The HTTP calls this system makes to other services (Feign clients, Spring HTTP
    interfaces, RestTemplate, WebClient, RestClient), grouped by provider: the base-URL property
    and its value per profile, and per call its method and path, the client, the method that
    makes it, the provider spec operation it matches (if a spec is indexed), and an id to pass
    to get_contract_context. Start here to write consumer contract tests.

    symbol: only the calls this method or class makes, directly or through what it calls.
    format: "markdown" (default) or "json".
    """
    result = ContractContext(get_engine()).outbound_calls(symbol)
    return result if format == "json" else render_calls_markdown(result)


@mcp.tool(structured_output=False)
def get_contract_context(call: str, format: str = "markdown") -> str | dict[str, Any]:
    """Everything to write a Specmatic consumer contract test for one outbound call: the
    request the code sends (method, path, query, headers, body type with fields), the response
    type, the base-URL property to point at the Specmatic mock (and its config per profile),
    the provider operation it matches with its spec text, the entry points that make the call
    (the endpoint, listener or job a test drives), existing tests, the caller's source, a draft
    Specmatic example, and notes (hard-coded URLs, paths not visible in code).

    call: an id from list_outbound_calls, "POST /orders", or a method making one call.
    format: "markdown" (default) or "json".
    """
    result = ContractContext(get_engine()).call_context(call)
    return result if format == "json" else render_call_markdown(result)


@mcp.tool(structured_output=False)
def index_status() -> dict[str, Any]:
    """Whether an index is loaded, and what it holds: repo name and commit, when it was built
    (and how long ago), the embedding model, and counts of files, nodes, edges and chunks."""
    try:
        return {"indexed": True, **get_engine().stats()}
    except ContextError as exc:
        return {"indexed": False, "index_dir": str(index_dir()), "error": str(exc)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tcg-mcp", description="The test-context-graph MCP server (stdio)."
    )
    parser.add_argument(
        "--index", metavar="DIR", help="the index folder (default: $TCG_INDEX or .tcg)"
    )
    args = parser.parse_args(argv)
    if args.index:
        os.environ["TCG_INDEX"] = args.index
    mcp.run()


if __name__ == "__main__":
    main()
