# test-context-graph

**A context graph of a Java Spring codebase, built so that a coding agent can write good tests.**

When an agent is asked to "write a unit test for `OwnerRestControllerV1.getOwner`", reading the
method isn't enough. It also needs to know:

- which dependencies to mock (`ClinicService`), and what the method calls on them;
- what the input and output types look like, even when they're generated at build time
  (`OwnerDto` doesn't exist in the source tree; it's generated from the OpenAPI spec);
- which HTTP endpoint the method serves (`GET /api/owners/{ownerId}`), with its parameters
  and responses;
- how this repository already tests similar code: JUnit 5, `@MockitoBean`, MockMvc with
  `standaloneSetup`, `@WithMockUser`, files named `*Tests`;
- what the docs say.

`test-context-graph` indexes a repository into a graph that links all of these, plus
embedded chunks for semantic search, then gives an agent everything above in one call
(`get_test_context`), through a CLI or an MCP server.

Built as a learning project. The demo is the open-source
[spring-petclinic-rest](https://github.com/spring-petclinic/spring-petclinic-rest): see
[what an agent gets](#example-what-an-agent-gets) and [the numbers](#results-on-spring-petclinic-rest).

## How it works

```
Java sources ──tree-sitter──► JavaFiles ─┐
openapi.yml  ──PyYAML───────► ApiSpecs  ─┼─► graph.py ──► nodes.json, edges.json
*.md         ──────────────► DocSections┘      │
                                               └─► chunks.py ──► local embeddings ──► LanceDB
                                                                  (bge-small, 384 dims)
file hashes + chunk hashes ─────────────────────────────────────► manifest.json

agent ──MCP──► tcg-mcp ──► context.py: graph walk + vector search ──► test context
```

1. **Parse.** tree-sitter-java gives every class, method, field, annotation and call site,
   with the receiver's type when the file shows it. OpenAPI specs give operations and schemas;
   Markdown gives sections.
2. **Link.** `graph.py` resolves names across files (imports, packages, nested types) and
   adds edges: calls, injections, overrides, which method serves which endpoint, and which
   test tests what.
3. **Chunk and embed.** Every method, class outline, endpoint, schema and doc section becomes a
   chunk, embedded with a small local model. No API key, and no code leaves the machine.
4. **Track changes.** The manifest keeps a hash per file and per chunk, so re-indexing only
   re-embeds chunks whose content changed.
5. **Answer.** `context.py` walks the graph from a symbol and adds vector search results, to
   build a test context.

## The graph

Nodes (`nodes.json`):

| Kind | Id | Example |
|---|---|---|
| file | `file:<path>` | `file:src/main/java/.../OwnerRestControllerV1.java` |
| type | `type:<qualified name>` | `type:org.springframework.samples.petclinic.model.Owner` |
| method | `method:<type>#<name>(<param types>)` | `method:...OwnerRestControllerV1#getOwner(Integer)` |
| endpoint | `endpoint:<METHOD> <spec path>` | `endpoint:GET /owners/{ownerId}` |
| schema | `schema:<spec file>#<name>` | `schema:src/main/resources/openapi.yml#Owner` |
| doc | `doc:<file>#<anchor>` | `doc:readme.md#database-configuration` |

Types generated at build time (OpenAPI `*Api` interfaces and `*Dto` classes, MapStruct
`*MapperImpl`) get stub nodes (`external: true`), so that calls and signatures still resolve,
and DTOs link to the OpenAPI schema they're generated from.

Edges (`edges.json`):

| Edge | From → to | Meaning |
|---|---|---|
| CONTAINS | file → type → method; spec → endpoint, schema; doc file → section | structure |
| EXTENDS, IMPLEMENTS | type → type | inheritance |
| INJECTS | type → type | a dependency received through the constructor, a field or a setter |
| CALLS | method → method | resolved by the receiver's type, or by name when unique |
| OVERRIDES | method → method | implements or overrides a supertype's method |
| USES | method → type | in its signature, or created with `new` |
| SERVES | method → endpoint | through `operationId` (API-first) or `@GetMapping` and similar annotations |
| TESTS | test → method, type or endpoint | by name, injected field, calls, or MockMvc URLs |
| MOCKS | test type → type | replaced by `@MockitoBean`, `@Mock`, `mock()` |
| GENERATED_FROM | DTO stub → schema | `OwnerDto` comes from `Owner` |
| REFERENCES | endpoint → schema, schema → schema | request and response bodies, `allOf`, properties |
| MENTIONS | doc → type, endpoint | a doc section names it |

The exact data model is [`src/test_context_graph/models.py`](src/test_context_graph/models.py).

## Design decisions

1. **Plain JSON for the graph, LanceDB for vectors.** `nodes.json` and `edges.json` can be read,
   diffed and loaded by any tool. Only the chunks, which need vector search, go in a database,
   and LanceDB is a file-based one with no server.
2. **Local embeddings.** `BAAI/bge-small-en-v1.5` through fastembed (ONNX, CPU): free, offline,
   and nothing leaves the machine. That matters for company code. The quality is enough here,
   because the graph does most of the work and vectors only rank docs and similar code.
3. **Re-parse everything, re-embed only what changed.** Parsing about 100 files with
   tree-sitter takes well under a second; embedding is the slow part. So the graph is rebuilt in
   full each run, which is simple and never stale, and chunks are re-embedded only when their
   hash changes.
4. **Heuristics over a compiler.** Real type resolution would need the build (Maven, generated
   sources, dependencies). tree-sitter works on any checkout without building it, and the
   heuristics are recorded on each edge (`resolution`, `reason`), so an agent can tell a sure
   link from a guess.

## Quick start

Python 3.10 or later.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

scripts/fetch-demo.sh                      # clones spring-petclinic-rest into .demo/
tcg index .demo/spring-petclinic-rest      # writes .tcg/; the first run downloads the model (~70 MB)
tcg context 'OwnerRestControllerV1#getOwner'
```

| Command | What it does |
|---|---|
| `tcg index <repo> [--full] [--embedder fastembed\|hash]` | Index a repository. Re-runs only re-embed chunks that changed. |
| `tcg context <symbol> [--json]` | Everything needed to write a test for a method or class |
| `tcg search <query> [--kind method] [--limit N]` | Search code, endpoints, schemas and docs by meaning |
| `tcg tests <symbol>` | Existing tests of a symbol, and why each is related |
| `tcg show <symbol>` | One node: attributes, edges and text |
| `tcg stats` | What is indexed, when, and with which model |
| `tcg calls [symbol]` | Outbound HTTP calls to other services, by provider, for contract tests |
| `tcg contract <call>` | Everything to write a Specmatic consumer test for one call |

A symbol can be `Type#method`, `Type.method`, a type name, a node id, a file path or an
endpoint (`GET /owners/{ownerId}`). The index folder is `.tcg` unless `--index` or `TCG_INDEX`
says otherwise. Models are cached in `~/.cache/test-context-graph/models` (`TCG_MODEL_CACHE`)
and load offline once downloaded.

## Use it from an agent (MCP)

`tcg-mcp` is an MCP server (stdio) with eight tools: `get_test_context` (the one to call before
writing a test), `search_code`, `get_node`, `get_neighbors`, `find_tests`, `index_status`, and
for consumer contract tests `list_outbound_calls` and `get_contract_context` (see below).

Claude Code:

```bash
claude mcp add test-context-graph -- /absolute/path/.venv/bin/tcg-mcp --index /absolute/path/.tcg
```

Cursor (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "test-context-graph": {
      "command": "/absolute/path/.venv/bin/tcg-mcp",
      "args": ["--index", "/absolute/path/.tcg"]
    }
  }
}
```

Then ask the agent, for example: *"Use get_test_context for OwnerRestControllerV1#getOwner, then
write its unit test."*

## Example: what an agent gets

From `tcg context 'OwnerRestControllerV1#getOwner'` (shortened; full output in
[`examples/`](examples/petclinic-getOwner-context.md)):

```
## Dependencies to mock
- ClinicService (interface; field `clinicService` via constructor)
  - calls `Owner findOwnerById(int id) throws DataAccessException`
  - in tests: mocked in 7 test classes, e.g. OwnerRestControllerV1Tests (@MockitoBean)
- OwnerMapper (interface, mapper; field `ownerMapper` via constructor)
  - calls `OwnerDto toOwnerDto(Owner owner)`

## Data types
- OwnerDto — generated from OpenAPI schema `Owner` (src/main/resources/openapi.yml)
  - properties (* required): firstName: string*, lastName: string*, address: string*, ...

## Endpoints
- GET /owners/{ownerId} — operationId `getOwner`: Get a pet owner by ID
  - served at `/api/owners/{ownerId}` (what MockMvc calls)
  - responses: 200 Owner · 304 Not modified. · 400 ProblemDetail · 404 ProblemDetail · 500 ...
  - tested by: OwnerRestControllerV1Tests#testGetOwnerSuccess(), ...#testGetOwnerNotFound()

## Existing tests
### OwnerRestControllerV1Tests — @SpringBootTest, @MockitoBean ClinicService, MockMvc ...
```

`OwnerDto` doesn't exist in the source tree: it's generated at build time. The agent still gets
its fields, from the OpenAPI schema it's generated from. For a service method,
[`ClinicServiceImpl#findOwnerById`](examples/petclinic-findOwnerById-context.md) lists the
repository to mock and finds the existing tests through the `ClinicService` interface: an
abstract test class run under four database profiles.

## Contract tests: the calls a system makes

For [Specmatic](https://specmatic.io) consumer contract tests, an agent needs the other side of
the graph: the provider endpoints this system **calls**. The indexer finds outbound HTTP calls
made with Feign (`@FeignClient`), Spring HTTP interfaces (`@HttpExchange`, `@GetExchange`...),
`RestTemplate`, `WebClient` and `RestClient`, and links each one to:

- the request the code sends: method, path, query parameters, headers, body and response types;
- its base URL and the property that sets it (from `@FeignClient(url)`, `@Value`,
  `@ConfigurationProperties`, a `baseUrl(...)` in a `@Bean` or `@ImportHttpServices` config),
  with the property's value per profile from `application*.properties|yml`;
- the provider's operation, when the provider's spec is in the index (in a `contracts/`,
  `clients/` or `providers/` folder), kept apart from the system's own API;
- the entry points that make the call: the endpoint it serves, a `@KafkaListener` or a
  `@Scheduled` job, with the call chain, which is what a consumer test drives.

`get_contract_context` adds the property to override to point the consumer at the Specmatic mock
(`orders.api.url=http://localhost:9000`) and a draft Specmatic example built from the code.
Specmatic then judges the test: the consumer runs against a strict mock built from the
provider's spec. Each call records how it was found (`resolution`), and the notes say when a base
URL is hard-coded or a path isn't visible in code.

```
tcg calls                      # every outbound call, grouped by provider
tcg contract "POST /orders"    # request, types, base-URL property, provider operation, draft example
```

## Results on spring-petclinic-rest

| | |
|---|---|
| Indexed | 113 files (109 Java, 1 OpenAPI spec, 3 Markdown), no parse errors |
| Graph | 885 nodes, 2,639 edges (695 CALLS, 370 TESTS, 37 SERVES: every implemented operation) |
| Chunks | 682, embedded with bge-small (384 dims); the index is 3.3 MB |
| First index | 81 s on a laptop CPU, almost all of it embedding |
| Re-index, nothing changed | 0.3 s, nothing re-embedded |
| `tcg context` | 0.8 s from the CLI; about 6 ms per call in the running MCP server |
| Tests | 420, no network needed |

## Limitations

- **Heuristic, not compiled.** A call's receiver type is known when the same file shows it (a
  field, parameter or local variable). About half of all call sites qualify; chained calls
  often don't. Every edge records how it was resolved, so guesses can be told from sure links.
- Methods inherited from Spring Data (`findAll()` on a repository interface) have no nodes, so
  calls to them aren't linked.
- Search is vector-only, without keyword boosting.
- Java and Spring conventions only; tested on one demo repository.

## Next steps

1. **Evaluation:** have an agent write tests for about 20 methods with and without
   `get_test_context`, and compare how many compile, pass and add coverage.
2. Hybrid search: keyword and vector.
3. Gradle source sets (`src/integrationTest/`) and multi-module builds.

## How it was built

The design and the data contract ([`models.py`](src/test_context_graph/models.py)) came first.
Then AI coding agents built the modules in parallel, each against the contract and with its own
tests: a survey of the demo repository, the Java parser, the OpenAPI and Markdown parsers,
storage and embeddings, the graph builder, and the context engine with its CLI and MCP server.
Each module was checked on the real demo before being accepted. [`STATUS.md`](STATUS.md) and
[`docs/work-packages.md`](docs/work-packages.md) record the plan and its progress.

## Development

```bash
.venv/bin/python -m pytest -q                        # 420 tests, no network
TCG_TEST_FASTEMBED=1 .venv/bin/python -m pytest -q   # also loads the real model
```

See [`AGENTS.md`](AGENTS.md) for the layout and the rules for contributors, human or AI.

## License

[MIT](LICENSE). The demo target, spring-petclinic-rest, is Apache-2.0 licensed and isn't included in this repository:
`scripts/fetch-demo.sh` clones it into `.demo/`. The files in `examples/` are output generated
from it.
