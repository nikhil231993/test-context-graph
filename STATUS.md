# Status

The live handoff file: what's decided, what's done, what's running, what's next. **Read it
first when picking the work up** (a new Claude Code session, Cursor, or a person), and update it
at every milestone.

_Last updated: 2026-10-11 12:56_

## Goal

Index a Java Spring codebase into a context graph (`nodes.json`, `edges.json`), embedded chunks
(LanceDB) and a manifest, and serve a `get_test_context(symbol)` call over a CLI and an MCP
server, so a coding agent gets what it needs to write a test: the code, what to mock, the data
types (even generated ones), the endpoint and its spec, existing tests to copy the style from,
and docs. A learning and portfolio project; see `README.md` for the design.

## Decisions made with the maintainer

- Name `test-context-graph`, Python 3.12, Java Spring parsed with tree-sitter.
- Demo target: the open-source **spring-petclinic-rest** (Apache-2.0), pinned to commit
  `afc8fc1d3b8ec8de69414482b3bd70a1da78cb95`, cloned into `.demo/` by `scripts/fetch-demo.sh`
  (git-ignored). **Never company code or names.**
- Embeddings: local, `BAAI/bge-small-en-v1.5` via fastembed (384 dims). Tests use `HashEmbedder`.
- Storage in `.tcg/`: `nodes.json`, `edges.json`, `manifest.json` + LanceDB table `chunks`.
- Endpoint node ids use the OpenAPI spec path (`endpoint:GET /owners/{ownerId}`); the paths it's
  served at go in attrs `served_path` (`/api/owners/{ownerId}`, what MockMvc calls) and `url_path`
  (`/petclinic/api/owners/{ownerId}`). Decided after the survey of the demo target.
- Built with parallel sub-agents, one module each, all against the contract in `models.py`.

## Work packages

Specs for each package: [`docs/work-packages.md`](docs/work-packages.md). Facts about the demo
target that drove the design: [`docs/demo-target-notes.md`](docs/demo-target-notes.md).

| # | Package | Files | Status |
|---|---|---|---|
| 0 | Scaffold, contract, AGENTS.md, README draft, fetch script | `models.py`, `pyproject.toml`, ... | done |
| 1 | Survey of the demo target | `docs/demo-target-notes.md` | done |
| 2 | Java parser | `java_parser.py` + tests | in progress (sub-agent): parser written; tests at 2026-10-11 12:56: 1 failed, 64 passed in 0.17s |
| 3 | OpenAPI and Markdown parsers | `openapi.py`, `docs.py` + tests | in progress (sub-agent): both modules written; tests at 2026-10-11 12:56: openapi 51 passed in 0.05s, docs 14 passed in 0.02s |
| 4 | Embeddings, manifest, store | `embeddings.py`, `manifest.py`, `store.py` + tests | **done** (agent finished; 126 tests pass, 1 real-model test skipped unless `TCG_TEST_FASTEMBED=1`); maintainer code review pending |
| 5 | Graph builder and chunker | `graph.py`, `chunks.py` + tests | **paused** (stopped to save usage quota before writing any file); restart from its spec when quota allows |
| 6 | Indexer: connect 2–5, incremental runs | `indexer.py` + tests | todo (maintainer) |
| 7 | Context engine, CLI, MCP server | `context.py`, `cli.py`, `server.py` + tests | todo (sub-agent) |
| 8 | Run on petclinic, review, README results, example output | README, `examples/` | todo |
| 9 | Evaluation: an agent writes tests with and without the context | `eval/` | later |

"in progress (sub-agent)" means an agent was building it when this file was last updated. If the
session that ran it is gone, check the package's files and tests (below) and finish it from its
spec.

## How to resume

```bash
cd ~/Nikhil/test-context-graph
scripts/fetch-demo.sh                                    # if .demo/ is missing
uv venv --python 3.12 .venv                              # if .venv/ is missing
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q                            # which packages pass?
```

Then, for each package not marked done: check its files exist and its tests pass; finish it from
its spec in `docs/work-packages.md`; mark it done here. Then start the first "todo".

## Log

- 2026-10-11 12:56: agent states saved. Running: 2 (Java parser), 3 (OpenAPI/docs). Done: 1, 4.
  Paused: 5. If the session ends, finish 2 and 3 from their specs: their files are on disk.
- 2026-10-11: scaffold, contract and demo survey done; sub-agents started on packages 2–5.
- 2026-10-11 12:51: agents 2–5 still running (17 min). See the table for each one's progress.
  Nothing committed yet. If a session ends: re-run each package's tests, review its files, and
  finish it from docs/work-packages.md.
- 2026-10-11: package 5's agent stopped at the maintainer's request to save usage quota. It had
  only read the contract; no files were written. Restart it from docs/work-packages.md#5 later.
- 2026-10-11: package 4 finished. On the demo: scan_repo finds 113 files (87 main + 22 test
  Java, 1 spec, 3 Markdown); git_commit matches HEAD; an unchanged re-plan embeds 0 chunks; the
  real model embeds ~113 texts in ~15 s on CPU. No contract changes. Extras: default_embedder(name)
  ("fastembed"/"hash", for the CLI), IndexStore.read_meta(), manifest.source_set().
  **For the indexer (package 6):** pass store.chunk_hashes() (not the manifest's) to plan_chunks;
  call reset_chunks() when the embedding model changes. FastEmbedEmbedder adds the BGE query
  prefix itself. graph.py must use the same source_set rule (src/test/ -> test, src/main/ -> main).
  Known gaps: spec key after the first 4 KB is missed; LanceDB keeps old versions (no cleanup,
  as it would break a concurrent reader); nodes.json/edges.json are atomic each, not as a pair.
