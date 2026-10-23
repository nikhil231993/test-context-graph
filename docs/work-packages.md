# Work packages

The spec each package is built from. Packages 2–5 were given to parallel sub-agents; anyone
finishing or redoing a package works from its section here, against `src/test_context_graph/models.py`
(the contract) and `AGENTS.md` (the rules). Facts about the demo target: `docs/demo-target-notes.md`.

Common rules for every package: own only the listed files; don't change the contract except by
adding a field with a default; pure modules do no I/O; tests use small made-up fixtures and no
network (`HashEmbedder`); check the result on the demo target in `.demo/spring-petclinic-rest`.

## 2. Java parser: `java_parser.py`, `tests/test_java_parser.py`, `tests/fixtures/java/`

`parse_java(source: str | bytes, path: str) -> JavaFile`, pure, with a module-level tree-sitter
Language/Parser. Fill every field of JavaFile, Import, TypeDecl, MethodDecl, FieldDecl, Parameter,
Annotation, TypeRef, CallSite as `models.py` documents them.

- Types: class, interface, enum, record, @interface. Nested types flattened into `JavaFile.types`
  with `outer` and dotted names (`Outer.Inner`). Anonymous and local classes are not types; their
  calls, and calls in lambdas, belong to the enclosing method.
- Methods: constructors (`<init>`, kind constructor), record compact constructors, interface
  methods (`has_body` False unless default). Line ranges include annotations. `signature`: modifier
  keywords (no annotations), type parameters, return type, name, `(type name, ...)`, throws, one
  line.
- Javadoc: the `/** */` block just before the declaration, markers stripped.
- Annotations: marker, single value under "value", named pairs; string literals unquoted, string
  arrays as lists, anything else (nested annotations, enum constants, `X.class`) as raw text.
- TypeRef: generics recursively, arrays, varargs (1 dim), qualified names, primitives, wildcards
  (name "?", bound as its single arg).
- Fields: one per declarator, `initializer` raw text.
- CallSites (call, new, method_ref) in order. `receiver_type` from the same file: parameters,
  locals (`var x = new Foo()` -> Foo), enhanced-for and catch variables, typed lambda params,
  fields of the enclosing and outer types, `this.x`, `this`/`super`, a capitalised non-variable
  identifier (static call). Chained calls: receiver text, receiver_type None. Method-level scoping.
  `string_args`: literals unquoted; `+` concatenations with non-literal parts as `{}`.
- `errors`: up to 5 "line N: syntax error near '...'".
- Done when: a test per behaviour passes, and every .java file of the demo parses without an
  exception (report counts and the share of calls with a receiver_type).

## 3. OpenAPI and Markdown: `openapi.py`, `docs.py`, their tests and fixtures

- `looks_like_openapi(text) -> bool`: top-level `openapi`/`swagger` key; false for
  application.yml, docker-compose.yml.
- `parse_openapi(text, path) -> ApiSpec`: OpenAPI 3.0/3.1 YAML or JSON, basic Swagger 2.0.
  ValueError on invalid input. Lines from `yaml.compose` marks. `base_path` = path of
  servers[0].url (variables' defaults substituted). Operations for the 8 HTTP methods; path-level
  parameters merged (operation wins), `$ref` parameters resolved. Short schema forms: `$ref` ->
  name, `array<X>`, primitive type, `object`, `allOf<A,B>`. request_body prefers
  application/json. responses: status -> schema or description. Schemas: properties (inline allOf
  parts included), required, all_of, YAML text.
- `parse_markdown(text, path) -> list[DocSection]`: ATX headings (setext nice to have), ignore
  fenced code, level 0 section for text before the first heading, each section up to the next
  heading of any level, breadcrumbs, GitHub-style anchors with -1, -2 for duplicates.
- Done when: tests pass; the demo spec gives 37 operations, 17 schemas, base_path
  "/petclinic/api"; the demo readmes parse.

## 4. Embeddings, manifest, store: `embeddings.py`, `manifest.py`, `store.py` and their tests

- `Embedder` protocol (`model_name`, `dim`, `embed_documents`, `embed_query`);
  `FastEmbedEmbedder` (lazy load, bge query prefix via query_embed, normalised);
  `HashEmbedder(dim=256)` (stable token hashing, normalised); `default_embedder()` (env
  `TCG_EMBEDDER=hash` for the hash one).
- `manifest.py`: `SKIP_DIRS`; `classify(path, head) -> (kind, source_set) | None`;
  `scan_repo(root, max_bytes)`; `diff_files(old, new) -> FileChanges`;
  `plan_chunks(old_hashes, new_chunks) -> (to_embed, to_delete)`; `git_commit(root)` from .git
  files without subprocess.
- `IndexStore(index_dir)`: `write_graph`/`read_graph` (sorted, indent=1, atomic),
  `write_manifest`/`read_manifest`, LanceDB `chunks` table with an explicit schema:
  `upsert_chunks`, `delete_chunks`, `reset_chunks`, `chunk_hashes`, `get_chunks`, `count_chunks`,
  `search(vector, limit, kinds, path_prefix)` (cosine, similarity = 1 - distance); everything works
  before the table exists.
- Done when: tests pass without network; a real-model test is skipped unless `TCG_TEST_FASTEMBED=1`.

## 5. Graph and chunks: `graph.py`, `chunks.py` and their tests

`build_graph(java_files, specs=(), docs=()) -> Graph`, pure and deterministic (sorted, no duplicate
edges). Node and edge kinds and ids as in `models.py`.

- Nodes: files (language, source_set, package); types (type_kind, qualified_name, package,
  source_set, role, annotations, profiles, active_profiles, modifiers, type_parameters,
  extends/implements, fields, is_abstract, external, javadoc); stub types (external, generated) for
  types imported from the project's own packages but absent from the source (project prefix =
  common package prefix of main sources); methods (signature, method_kind, modifiers, annotations,
  return_type, parameters, is_test, has_body, javadoc); endpoints (http_method, spec_path,
  operation_id, summary, description, tags, parameters, request_body, responses, url_path,
  served_path, sources); schemas; doc sections.
- Roles, first match: test, test_support, controller, controller_advice, service, repository,
  entity, mapper, config, dto/api_interface (stubs), other.
- Type resolution: qualified name, nested types of the enclosing chain, single-type imports, same
  package, wildcard imports; primitives and java.lang ignored.
- Edges: CONTAINS; EXTENDS/IMPLEMENTS; INJECTS (constructor, @Autowired/@Inject/@Resource field,
  @Autowired setter); CALLS (receiver_type -> type and supertypes, overloads by arity; same type;
  super; name_only when unique) with `resolution`; USES (parameter, return, new); OVERRIDES
  (transitive); SERVES via operation_id (controllers or implementers of an `*Api` stub; sets
  served_path = class @RequestMapping + spec path) or via annotation (merge with a matching spec
  endpoint, else a code-only endpoint); TESTS (name convention incl. Abstract prefix, injected or
  `new`-initialised fields, @WebMvcTest, calls to non-mocked main types, MockMvc URLs matched to
  endpoints ignoring query strings, `{}` and `{param}` as wildcards) with `reason`; MOCKS
  (@Mock/@MockBean/@MockitoBean/@Spy/`mock()`); GENERATED_FROM (Dto stub -> schema);
  REFERENCES (endpoint -> request/response schemas; schema -> allOf/property schemas); MENTIONS
  (doc -> types and endpoints named in it, max 30 per section).
- `make_chunks(graph, java_files, sources, specs=(), docs=(), max_chars=4000) -> list[Chunk]`:
  methods (source + javadoc; skip trivial accessors in entity/dto/other roles), type skeletons,
  endpoints (readable summary incl. serving methods), schemas (YAML), doc sections (non-empty);
  embed_text = header + text; content_hash = sha256(embed_text); long chunks split at lines into
  `#part2`...; sorted by id.
- Done when: a test per rule passes.

## 6. Indexer: `indexer.py`, `tests/test_indexer.py` (maintainer)

`index_repo(repo: Path, index_dir: Path, embedder: Embedder | None = None, full: bool = False)
-> IndexResult` (files changes, chunks embedded/deleted, counts, warnings, seconds):
scan_repo -> diff with the previous manifest -> parse every Java file (errors become warnings),
spec and doc -> build_graph -> make_chunks -> reset the chunk table if `full` or the embedding
model changed -> plan_chunks against the table's hashes -> embed in batches -> upsert, delete ->
write graph and manifest. A second run on an unchanged repo embeds nothing.

## 7. Context engine, CLI, MCP server: `context.py`, `cli.py`, `server.py` and their tests

The product's main promise: one call, `get_test_context(symbol)`, gives a coding agent everything
it needs to write a good test for that symbol. Build on: `IndexStore` (read_graph, read_meta,
read_manifest, get_chunks, search, count_chunks), `embeddings` (FastEmbedEmbedder, HashEmbedder,
default_embedder), `indexer.index_repo`, `graph.match_url`/`type_role`. Tests index the made-up
app `tests/fixtures/bookshop` (copied to a tmp dir) with `HashEmbedder` in a module- or
session-scoped fixture. `pyproject.toml` already declares `tcg` and `tcg-mcp`.

### context.py
`ContextEngine(index_dir, embedder=None)`: loads the graph once (in-memory adjacency by edge kind,
both directions). The embedder is created lazily, only for vector search, and matches the index:
the manifest's embedding_model ("hash-<dim>" -> HashEmbedder(dim), else FastEmbedEmbedder(model))
unless one is passed. A missing index gives a clear error: run `tcg index <repo>`.

- `resolve(symbol) -> list[Node]`, ranked: a node id; a qualified or simple type name;
  `Type#method`, `Type.method`, `Type#method(String)`; a bare method name; a file path (-> its
  types); an endpoint "GET /owners/{id}" or a served/url path. Ambiguity returns several
  candidates, which callers report instead of guessing silently.
- `test_context(symbol, max_tests=3, max_docs=3) -> dict` (JSON-ready), for a method (or a type:
  the whole class):
  - target: id, kind, signature, role, path + lines, source (chunk text).
  - enclosing type: outline chunk text, role, annotations, profiles.
  - dependencies to mock: types it INJECTS (via, name), each with the methods the target CALLS on
    it (signatures); interfaces marked.
  - collaborators called: resolved CALLS from the target (signature, resolution), minus the above.
  - data types from USES: fields for project types; for generated DTO stubs, the OpenAPI schema
    properties/required via GENERATED_FROM (how an agent learns OwnerDto's fields).
  - endpoints SERVED (by the method, or the type's methods): http_method, served_path, url_path,
    operation_id, summary, parameters, request_body, responses, and the test methods already
    hitting it (TESTS reason http).
  - existing tests: TESTS into the target, its type, the interfaces it implements and the methods
    it overrides (ClinicServiceImpl finds AbstractClinicServiceTests via ClinicService), plus
    subclasses of abstract test bases with their active_profiles. For up to max_tests test types:
    path, annotations, setup fields with annotations (@Autowired subject, @MockitoBean mocks),
    MOCKS, and the source of one or two test methods most related to the target (prefer TESTS
    edges to the target or its endpoints).
  - implementations: for an interface target or dependency, implementing types and profiles.
  - conventions: frameworks seen in the tests (JUnit 5, Mockito/BDDMockito, MockMvc, AssertJ,
    Spring Boot test annotations), dominant suffix ("Tests" vs "Test"), a suggested test file
    path (mirror the package under src/test/java; if a test class exists, say to add to it).
  - related docs: top max_docs doc chunks by vector search on name + signature + javadoc.
  - notes: e.g. "OwnerDto is generated at build time from schema Owner", ambiguous candidates.
  - Bounded: long sources truncated (~4,000 chars), list lengths capped.
- `render_markdown(context) -> str`: compact Markdown for an LLM or a terminal.
- `search(query, kinds=None, limit=10)` (hits with id, kind, name, path, lines, score, snippet);
  `get_node(symbol)` (node + chunk text + edge counts); `neighbors(node_id, edge_kinds=None,
  direction="both", limit=50)`; `find_tests(symbol)`; `stats()` (manifest counts, index age,
  repo name/commit, embedding model).

### cli.py (`tcg`, argparse)
`tcg index <repo> [--index DIR] [--full] [--embedder fastembed|hash]` (progress on stderr, then a
summary: files added/changed/removed, chunks embedded/deleted, counts, warnings, seconds);
`tcg context <symbol> [--json]` (Markdown by default); `tcg search <query> [--kind K]... [--limit N]
[--json]`; `tcg tests <symbol> [--json]`; `tcg show <symbol> [--json]`; `tcg stats [--json]`.
`--index` defaults to env TCG_INDEX or ".tcg". Errors -> stderr message, exit code 1/2, no traceback.

### server.py (`tcg-mcp`, FastMCP, stdio)
Tools: `get_test_context(symbol, format="markdown", max_tests=3)` (Markdown string by default, the
dict for "json"), `search_code(query, kinds=None, limit=10)`, `get_node(symbol)`,
`get_neighbors(node_id, edge_kinds=None, direction="both", limit=50)`, `find_tests(symbol)`,
`index_status()`. Index folder from env TCG_INDEX (default ".tcg"); engine created lazily and
reused. Docstrings say what each tool returns and when to use it (get_test_context first for
writing tests). Tests call the tool functions directly.

### Done when
Tests pass (full suite too), and on the real demo index in `.tcg/` (built with the real model):
- `tcg context OwnerRestControllerV1#getOwner`: dependencies include ClinicService (with
  findOwnerById) and OwnerMapper; data types include OwnerDto with the Owner schema fields;
  endpoint GET /owners/{ownerId} with served_path /api/owners/{ownerId}; existing tests include
  OwnerRestControllerV1Tests with its @MockitoBean ClinicService setup and a getOwner-related test
  method; a suggested test path.
- `tcg context ClinicServiceImpl#findOwnerById`: OwnerRepository to mock; AbstractClinicServiceTests
  found through ClinicService, with its profile subclasses.
- `tcg search "validate pet birth date"` and `tcg stats` work; `tcg context` well under 2 s apart
  from loading the model for docs.
- The Markdown output of the first two saved to `examples/petclinic-getOwner-context.md` and
  `examples/petclinic-findOwnerById-context.md` (open-source demo output, fine to commit).

## 8. Run on petclinic and polish

Index the demo with the real model; review every module (correctness, simplicity); fix what the
end-to-end run shows; add results and usage to the README; commit an `examples/` folder with sample
`tcg context` output.

## 9. Evaluation (later)

Pick ~20 methods; have an agent write a test for each with and without `get_test_context`; compare
how many compile, pass, and add coverage. Report the numbers in the README.

## 10. Code review

Two review sub-agents, each on its own half (A: java_parser, openapi, docs, manifest, embeddings,
store; B: graph, chunks, indexer, context, cli, server, README accuracy). They fix only proven
bugs, each with a regression test, and report larger issues. Done when both reports are in, the
full suite passes and the demo answers are unchanged; then commit and push.

## 11. Most Java and Spring projects

Java only. Priority patterns: Lombok, Gradle + multi-module, microservice clients, messaging and jobs.

- **(a) Measure.** `eval/corpus.yaml` lists 6–8 varied open-source Spring projects (pinned
  commits, licences noted) covering Maven and Gradle, single- and multi-module, Lombok, WebFlux,
  Feign/WebClient/RestTemplate clients, @KafkaListener/@Scheduled, JUnit 4 and 5. `eval/compat.py`
  clones them into `.demo/corpus/`, indexes each with HashEmbedder, and writes
  `docs/compatibility.md`: per project, crashes, parse errors, files by source set, call
  resolution rate, mapped endpoints found vs mapping annotations in code, controllers/services
  with dependencies found, test classes linked to what they test, role coverage, two sample
  `tcg context` checks, and counts of the priority patterns with what the tool did with them.
  Ends with the gaps ranked by impact, each with real examples.
- **(b) Fix** the top gaps, as work packages split by file ownership.
- **(c) Re-measure**; before/after table in the README.


## 12. Outbound API calls and contract-test context (Specmatic)

Goal: an agent asks "which provider endpoints does this system call, and with what?", and gets
enough to write Specmatic consumer contract tests: `specmatic.yaml`, example JSON files (the
requests the code really sends) and the base-URL override. Specmatic, not the agent, then
judges them: the consumer runs against a strict Specmatic mock built from the provider's spec.

Stages:

- **12a. Graph (this repo).**
  - `CallSite.args`: each argument's source text, one line (for `Product.class`,
    `HttpMethod.POST`, `baseUrl + "/x"`).
  - Config files: `application*.properties|yml|yaml` and `bootstrap*` are indexed (kind
    `config`); `config.py` parses them to `ConfigProperty` (key, value, line, profile). Values
    of secret-looking keys are masked. Node `config:<path>#<key>`.
  - `http_calls.py` (pure) finds outbound HTTP calls: Feign (`@FeignClient`) and Spring HTTP
    interfaces (`@HttpExchange`, `@GetExchange`...), `RestTemplate`, `WebClient` and `RestClient`
    chains. Node `http_call`, with: client, HTTP method, path template, query and header names,
    request body and response types, base URL (literal or `${property}`), and how each was found.
  - Edges: `CALLS_HTTP` method -> http_call; `CONFIGURED_BY` http_call -> config (base URL);
    `TARGETS` http_call -> endpoint, when an indexed spec has a matching operation.
  - `contract.py` + MCP tools `list_outbound_calls(symbol?)` and `get_contract_context(call)`:
    per call, the call site and its callers, request/response type fields, the base-URL property
    per profile, the matched spec operation, and existing tests. CLI `tcg calls`.
  - Done when: fixture tests for each client style pass; the full suite passes; a real
    open-source Java consumer gives its outbound calls with correct paths.
- **12b. Agent writes the Specmatic files** for one open-source Java consumer, from the tools
  above; the provider spec is found with ghes-openapi-mcp (`find_openapi_spec`) and recorded in
  `specmatic.yaml`, which is the single "where are the specs" file.
- **12c. Pipeline**: CI checks the examples against the spec, then runs the consumer against the
  Specmatic mock; a demo where a changed provider spec fails the build.
- **12d.** Seed examples from existing WireMock stubs (artifactory-mocks-mcp).

Open questions for the maintainer: Specmatic edition (open-source `io.specmatic` vs
Enterprise); the open-source Java consumer to demo on.
