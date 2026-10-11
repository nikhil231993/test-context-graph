# Notes for AI coding agents

`test-context-graph` indexes a Java Spring codebase into a context graph (nodes.json,
edges.json), searchable chunks (LanceDB, local embeddings) and a manifest, so that a coding agent
can ask for the context it needs to write a test. Read `README.md` for the design.

**Start with `STATUS.md`**: what's decided, done, in progress and next, and how to resume. The
spec of every work package is in `docs/work-packages.md`. Update `STATUS.md` at every milestone,
so the work can be picked up if a session ends.

## Layout

```
src/test_context_graph/
  models.py        THE CONTRACT: every dataclass passed between modules and written to disk
  java_parser.py   Java source -> JavaFile (tree-sitter-java). Pure, no I/O.
  openapi.py       OpenAPI YAML/JSON text -> ApiSpec. Pure, no I/O.
  docs.py          Markdown text -> DocSections. Pure, no I/O.
  graph.py         JavaFiles + ApiSpecs + DocSections -> Graph (nodes, edges). Pure, no I/O.
  chunks.py        The same inputs + Graph -> Chunks to embed. Pure, no I/O.
  embeddings.py    Embedder interface: FastEmbedEmbedder (local model) and HashEmbedder (tests)
  manifest.py      File hashing and the manifest: what changed since the last run
  store.py         Reads/writes the index folder: JSON files and the LanceDB chunk table
  indexer.py       Walks a repo and runs everything above; incremental by chunk hash
  context.py       Answers questions over an index: test context for a symbol, search, neighbours
  cli.py           `tcg` command
  server.py        `tcg-mcp` MCP server (stdio) exposing context.py to agents
tests/             pytest; fixtures/ holds small made-up Java, OpenAPI and Markdown files
```

## Rules

- **`models.py` is the contract.** Build against it as written. If you need something it
  doesn't have, add a field with a default (so nothing else breaks) and say so in your report.
  Never rename or remove a field.
- **Pure modules stay pure**: no file, network or clock access in java_parser, openapi, docs,
  graph, chunks. Tests feed them strings and dataclasses.
- **Tests need no network**: use `HashEmbedder`, never the real model, in tests. Run them with
  `.venv/bin/python -m pytest -q` (or one file: `... -q tests/test_x.py`).
- **Add a test for every behaviour.** Fixtures are small, made-up code that imitates Spring
  patterns; never copy code from the demo target or from any company.
- **No company code, names or data**, ever. The demo target is the open-source
  spring-petclinic-rest, cloned into `.demo/` (git-ignored) by `scripts/fetch-demo.sh`.
- Style: Python 3.10+, type hints, dataclasses, short docstrings that say why, not what.
  Match the surrounding code. Keep functions small; no new dependencies without a reason.
- Don't commit; the maintainer reviews and commits.
