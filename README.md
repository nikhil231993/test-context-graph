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

> Status: work in progress. Built as a learning project; indexes the open-source
> [spring-petclinic-rest](https://github.com/spring-petclinic/spring-petclinic-rest) as its
> demo.

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
