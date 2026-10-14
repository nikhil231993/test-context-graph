# Status

The live handoff file: what's decided, what's done, what's running, what's next. **Read it
first when picking the work up** (a new Claude Code session, Cursor, or a person), and update it
at every milestone.

_Last updated: 2026-10-14 15:05_

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
| 2 | Java parser | `java_parser.py` + tests | **done** (verified by maintainer: tests pass; all 109 demo files parse, 0 errors, 0.09 s; 49% of calls get a receiver_type) |
| 3 | OpenAPI and Markdown parsers | `openapi.py`, `docs.py` + tests | **done** (verified by maintainer: demo spec gives 37 ops, 17 schemas, base /petclinic/api, no false positives; readme gives 33 sections) |
| 4 | Embeddings, manifest, store | `embeddings.py`, `manifest.py`, `store.py` + tests | **done** (agent finished; 126 tests pass, 1 real-model test skipped unless `TCG_TEST_FASTEMBED=1`); maintainer code review pending |
| 5 | Graph builder and chunker | `graph.py`, `chunks.py` + tests | **done** (57 tests; demo spot checks pass: SERVES, INJECTS, TESTS, MOCKS, GENERATED_FROM) |
| 6 | Indexer: connect 2–5, incremental runs | `indexer.py` + tests | **done** (7 tests; real run on the demo below) |
| 7 | Context engine, CLI, MCP server | `context.py`, `cli.py`, `server.py` + tests | **done** (97 tests; demo checks pass for getOwner and findOwnerById, examples/ saved; `tcg context` 0.8 s, 1.0–1.5 s with the docs model) |
| 8 | Run on petclinic, review, README results, example output | README, `examples/` | **done** (README: quick start, MCP setup, example, results, limitations; examples/ from package 7) |
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

- 2026-10-14 14:45: package 7 done by its sub-agent (97 new tests; all "done when" checks pass on the
  demo; `tcg context` 0.8 s, ~6 ms warm in the MCP server). Fixed a problem it found: the model was
  cached in the temp folder, so an MCP client's minimal environment downloaded it again; now
  cached in ~/.cache/test-context-graph/models (TCG_MODEL_CACHE) and loaded offline first.
  README finished (package 8). 420 tests pass, 1 skipped. Not yet committed since the
  maintainer's "Initial commit" (2026-10-11 13:01); remote origin is set to the GitHub repo.
  Open: choose a license; package 9 (evaluation).
- 2026-10-14 15:05: MIT license added (maintainer's choice). Committed packages 5–8. Next: code
  review of all modules by two review sub-agents, then package 9 (evaluation) and a CI workflow.
- 2026-10-14 13:40: resumed. State checked (322 tests pass, demo index intact); package 7 restarted.
- 2026-10-12, end of day 1: stopped for the day at the maintainer's request. Package 7's agent was
  stopped while still reading (no files written). State: packages 0–6 done, 322 tests pass
  (1 skipped), demo index built in .tcg/ with the real model (git-ignored; rebuild with
  `tcg index` once package 7 exists, or `index_repo` from Python). Nothing committed yet.
  **Tomorrow:** (1) start package 7 from its full spec in docs/work-packages.md#7 (a sub-agent
  or directly); (2) package 8: review, README results and usage, examples/; (3) ask the
  maintainer about committing and pushing to a private GitHub repo.
- 2026-10-12 00:35: packages 5 and 6 done; 322 tests pass, 1 skipped. Real run on the demo with
  bge-small: first index 80.6 s (embedding 682 chunks on CPU), unchanged re-run 0.27 s embedding
  nothing; 113 files -> 885 nodes, 2,639 edges, 682 chunks, 0 parse problems, 3.3 MB in .tcg/.
  Search works ("find an owner by id" -> GET /owners/{ownerId}, OwnerRepository#findById).
  Optional contract additions suggested by package 5 (not done): CallSite.class_literals (to see
  mock(X.class) inside test methods), MethodDecl.type_parameters. Known graph gaps: receiver types
  only from the same file; Spring Data inherited methods have no nodes; one served_path per endpoint.
  Package 7 started.
- 2026-10-11 23:40: indexer written; made-up fixture app `tests/fixtures/bookshop/` added (API-first
  controller, generated DTO/Api stubs, profiles, abstract test base, MockMvc) for the indexer and package 7 tests.
- 2026-10-11 23:25: the usage limit stopped agents 2 and 3 during their final self-review (no final
  reports). Maintainer verified both: 258 tests pass, 1 skipped; real-code checks above. Package 5
  restarted.
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
