"""The data shared between modules. This file is the contract: parsers produce the facts below,
graph.py and chunks.py turn them into a Graph and Chunks, store.py writes those to the index
folder, and context.py reads them back.

Conventions:
- Paths are repo-relative and '/'-separated: src/main/java/org/example/Owner.java
- Lines are 1-based and inclusive.
- Every dataclass round-trips through to_dict() / from_dict(), the JSON written to disk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, TypeVar, get_type_hints

SCHEMA_VERSION = 1

T = TypeVar("T")


# ---- (de)serialisation ------------------------------------------------------------------


def to_dict(obj: Any) -> Any:
    """A dataclass (or list/dict of them) as plain JSON-ready data."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, list):
        return [to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {key: to_dict(value) for key, value in obj.items()}
    return obj


def from_dict(cls: type[T], data: dict[str, Any]) -> T:
    """The inverse of to_dict for one dataclass, including nested dataclasses and lists of them."""
    hints = get_type_hints(cls)
    values: dict[str, Any] = {}
    for f in fields(cls):  # type: ignore[arg-type]
        if f.name not in data:
            continue
        values[f.name] = _convert(hints[f.name], data[f.name])
    return cls(**values)


def _convert(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = getattr(hint, "__origin__", None)
    args = getattr(hint, "__args__", ())
    if origin is list and args and is_dataclass(args[0]):
        return [from_dict(args[0], item) for item in value]
    if origin is dict and len(args) == 2 and is_dataclass(args[1]):
        return {key: from_dict(args[1], item) for key, item in value.items()}
    if is_dataclass(hint):
        return from_dict(hint, value)
    # Optional[X] / X | None
    if args and type(None) in args:
        inner = [a for a in args if a is not type(None)]
        if len(inner) == 1:
            return _convert(inner[0], value)
    return value


# ---- Java facts: java_parser.parse_java() returns a JavaFile ---------------------------


@dataclass
class Annotation:
    name: str  # as written, without '@' and package: "GetMapping", "RequestMapping", "Test"
    # Arguments by name. A single unnamed argument is stored under "value". Values are a string
    # (string literals unquoted), a list of strings (for {"a", "b"}), or the raw source text of
    # anything else (e.g. "RequestMethod.GET", "HttpStatus.OK", "Owner.class").
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class TypeRef:
    """A type as written in the source."""

    text: str  # exactly as written: "List<OwnerDto>", "Map<String, List<Pet>>", "int[]"
    name: str  # without generic arguments or array brackets: "List", "Map", "int", "java.util.List"
    args: list[TypeRef] = field(default_factory=list)  # generic arguments, recursively
    array_dims: int = 0  # 1 for "int[]", 2 for "int[][]"; "String..." counts as 1


@dataclass
class Parameter:
    name: str
    type: TypeRef
    annotations: list[Annotation] = field(default_factory=list)  # @PathVariable, @RequestBody...


@dataclass
class FieldDecl:
    name: str  # one FieldDecl per declared name: "int a, b;" gives two
    type: TypeRef
    line: int
    modifiers: list[str] = field(default_factory=list)  # "private", "static", "final"...
    annotations: list[Annotation] = field(default_factory=list)  # @Autowired, @Mock, @MockitoBean...
    initializer: str | None = None  # raw text after '=', e.g. "new PetAgeValidator()"


@dataclass
class CallSite:
    """A method call, an object creation or a method reference inside a method body."""

    kind: str  # "call" (foo.bar()), "new" (new Foo(...)) or "method_ref" (Foo::bar, this::bar)
    name: str  # the method name; for "new", the created type's name as written ("Owner")
    line: int
    # The receiver expression as written: "ownerRepository", "this.ownerRepository", "Owner",
    # "mockMvc.perform(get(\"/x\"))", "super", "this". None for an unqualified call (bar()) and
    # for "new".
    receiver: str | None = None
    # The receiver's type name (TypeRef.name), when the parser can tell it from the same file:
    # a local variable, parameter or field of the enclosing types, "this"/"super" (the enclosing
    # type / its superclass), or a capitalised identifier taken as a type name (static call).
    # For "new", the created type's name. None when unknown (e.g. chained calls).
    receiver_type: str | None = None
    arg_count: int = 0
    # String arguments, unquoted, in order: get("/api/owners/1") -> ["/api/owners/1"]. A string
    # concatenation counts too, each non-literal part replaced by "{}":
    # put("/api/owners/" + ownerId + "/pets") -> ["/api/owners/{}/pets"]. Other arguments are
    # skipped. Lets graph.py link MockMvc calls in tests to endpoints.
    string_args: list[str] = field(default_factory=list)


@dataclass
class MethodDecl:
    name: str  # "<init>" for constructors
    kind: str  # "method" or "constructor"
    start_line: int
    end_line: int
    # One line, normalised whitespace, no body: "public ResponseEntity<OwnerDto> getOwner(Integer ownerId)"
    signature: str
    parameters: list[Parameter] = field(default_factory=list)
    return_type: TypeRef | None = None  # None for constructors; TypeRef("void", "void") for void
    modifiers: list[str] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    throws: list[str] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)  # in source order
    javadoc: str | None = None  # the /** */ comment just before, without the comment markers
    has_body: bool = True  # False for abstract and interface methods without a default body


@dataclass
class TypeDecl:
    name: str  # simple name; for a nested type the dotted path from the top type: "Outer.Inner"
    qualified_name: str  # package + name: "org.example.Outer.Inner"
    kind: str  # "class", "interface", "enum", "record" or "annotation"
    start_line: int
    end_line: int
    modifiers: list[str] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    type_parameters: list[str] = field(default_factory=list)  # as written: ["T", "ID extends Serializable"]
    extends: list[TypeRef] = field(default_factory=list)  # a class has 0 or 1; an interface 0..n
    implements: list[TypeRef] = field(default_factory=list)
    fields: list[FieldDecl] = field(default_factory=list)
    methods: list[MethodDecl] = field(default_factory=list)  # constructors included
    record_components: list[Parameter] = field(default_factory=list)
    enum_constants: list[str] = field(default_factory=list)
    javadoc: str | None = None
    outer: str | None = None  # qualified name of the enclosing type, for nested types


@dataclass
class Import:
    name: str  # "org.example.model.Owner"; for a wildcard, the package: "org.example.model"
    static: bool = False
    wildcard: bool = False


@dataclass
class JavaFile:
    path: str
    package: str  # "" for the default package
    imports: list[Import] = field(default_factory=list)
    types: list[TypeDecl] = field(default_factory=list)  # every type, nested ones included, in source order
    errors: list[str] = field(default_factory=list)  # syntax problems: "line 12: unexpected ..."


# ---- OpenAPI: openapi.parse_openapi() returns an ApiSpec -------------------------------


@dataclass
class ApiParameter:
    name: str
    location: str  # "path", "query", "header", "cookie"
    required: bool = False
    schema: str | None = None  # short form: "integer", "string", "array<string>", or a $ref name


@dataclass
class ApiOperation:
    method: str  # upper case: "GET"
    path: str  # as in the spec, without base path: "/owners/{ownerId}"
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    parameters: list[ApiParameter] = field(default_factory=list)  # path-level ones merged in
    request_body: str | None = None  # schema name ($ref last segment) or short type, if any
    responses: dict[str, str] = field(default_factory=dict)  # status -> schema name, or the description
    line: int | None = None  # line of the operation in the spec, when known


@dataclass
class ApiSchema:
    name: str  # components/schemas key
    line: int | None = None
    properties: dict[str, str] = field(default_factory=dict)  # property -> short type ("string", "Pet", "array<Visit>")
    required: list[str] = field(default_factory=list)
    all_of: list[str] = field(default_factory=list)  # schema names composed with allOf ($ref last segment)
    text: str = ""  # the schema as YAML, for chunks


@dataclass
class ApiSpec:
    path: str  # the spec file
    title: str | None = None
    version: str | None = None
    base_path: str = ""  # path part of servers[0].url: "/petclinic/api"; "" if none
    operations: list[ApiOperation] = field(default_factory=list)
    schemas: list[ApiSchema] = field(default_factory=list)


# ---- Docs: docs.parse_markdown() returns DocSections -----------------------------------


@dataclass
class DocSection:
    path: str
    heading: str  # "Running petclinic locally"; "" for text before the first heading
    anchor: str  # GitHub-style slug of the heading: "running-petclinic-locally"
    level: int  # 1 for '#', 2 for '##'...; 0 for text before the first heading
    start_line: int
    end_line: int
    text: str  # the section, heading line included, without its subsections
    breadcrumbs: list[str] = field(default_factory=list)  # parent headings, outermost first


# ---- The graph: graph.build_graph() returns a Graph, stored as nodes.json / edges.json ----

# Node kinds
FILE = "file"
TYPE = "type"
METHOD = "method"
ENDPOINT = "endpoint"
SCHEMA = "schema"
DOC = "doc"

# Edge kinds (source -> target)
CONTAINS = "CONTAINS"  # file -> type, type -> nested type, type -> method
EXTENDS = "EXTENDS"  # type -> type
IMPLEMENTS = "IMPLEMENTS"  # type -> type
INJECTS = "INJECTS"  # type -> type it receives as a dependency; attrs: via (constructor|field|setter), name
CALLS = "CALLS"  # method -> method; attrs: line, resolution (receiver_type|same_type|name_only)
OVERRIDES = "OVERRIDES"  # method -> the method it overrides or implements
USES = "USES"  # method -> project type in its signature or body; attrs: as (parameter|return|new)
SERVES = "SERVES"  # method -> endpoint; attrs: via (annotation|operation_id)
TESTS = "TESTS"  # test method/type -> method/type/endpoint it tests; attrs: reason
MOCKS = "MOCKS"  # test type -> type it replaces with a mock; attrs: via (MockitoBean, MockBean, Mock, mock())
GENERATED_FROM = "GENERATED_FROM"  # generated type (not in source) -> schema it's generated from
REFERENCES = "REFERENCES"  # endpoint -> schema (attrs: as = request|response); schema -> schema
MENTIONS = "MENTIONS"  # doc -> type/endpoint named in its text


def join_paths(*parts: str) -> str:
    """URL paths joined and normalised: join_paths("/api/", "owners/") -> "/api/owners"; "" -> "/"."""
    segments = [s for part in parts for s in (part or "").split("/") if s]
    return "/" + "/".join(segments)


def erase(type_ref: TypeRef) -> str:
    """A parameter type as used in method ids: simple name, no generics, [] per array dimension."""
    return type_ref.name.rsplit(".", 1)[-1] + "[]" * type_ref.array_dims


def file_id(path: str) -> str:
    return f"file:{path}"


def type_id(qualified_name: str) -> str:
    return f"type:{qualified_name}"


def method_id(type_qualified_name: str, name: str, param_types: list[TypeRef]) -> str:
    return f"method:{type_qualified_name}#{name}({','.join(erase(t) for t in param_types)})"


def endpoint_id(http_method: str, full_path: str) -> str:
    return f"endpoint:{http_method.upper()} {join_paths(full_path)}"


def schema_id(spec_path: str, name: str) -> str:
    return f"schema:{spec_path}#{name}"


def doc_id(path: str, anchor: str) -> str:
    return f"doc:{path}#{anchor}" if anchor else f"doc:{path}"


@dataclass
class Node:
    """ids (use the helpers above):  file:<path>   type:<qualified name>
    method:<qualified type>#<name>(<erased param types>)   endpoint:<METHOD> <path>
    schema:<spec path>#<name>   doc:<path>#<anchor>
    e.g. method:org.example.Owner#getPet(String,boolean); constructors are named <init>.
    An endpoint's <path> is the OpenAPI path as written in the spec ("/owners/{ownerId}"), or for
    an endpoint found only in code, its mapped path ("/api/owners/{ownerId}"). The paths it's
    served at are in attrs: served_path (controller prefix + spec path, what MockMvc calls) and
    url_path (spec base path + spec path, what a client calls)."""

    id: str
    kind: str
    name: str  # short display name: "Owner", "getPet", "GET /api/owners/{ownerId}"
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    kind: str
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


# ---- Chunks: chunks.make_chunks() returns them; store.py embeds them into LanceDB -------


@dataclass
class Chunk:
    id: str  # the node id it comes from; "<node id>#part<n>" (n from 2) when a node is split
    node_id: str
    kind: str  # the node's kind: "method", "type", "endpoint", "schema", "doc"
    text: str  # what an agent is shown: the source code or doc text
    embed_text: str  # what is embedded: a short header (package, type, signature...) + text
    content_hash: str  # sha256 hex of embed_text; a chunk is re-embedded only when it changes
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None


# ---- The manifest: manifest.json, what was indexed and how ------------------------------


@dataclass
class FileEntry:
    path: str
    sha256: str
    size: int
    kind: str  # "java", "openapi" or "markdown"
    source_set: str  # "main", "test" or "other"


@dataclass
class Manifest:
    schema_version: int = SCHEMA_VERSION
    tool_version: str = ""
    generated_at: str = ""  # ISO 8601, UTC
    repo_name: str = ""  # the folder name; no absolute paths, so the index can be shared
    repo_commit: str | None = None  # HEAD of the indexed repo, if it's a git repo
    embedding_model: str = ""
    embedding_dim: int = 0
    files: dict[str, FileEntry] = field(default_factory=dict)  # by path
    chunks: dict[str, str] = field(default_factory=dict)  # chunk id -> content_hash
    counts: dict[str, int] = field(default_factory=dict)  # nodes/edges by kind, chunks, files
