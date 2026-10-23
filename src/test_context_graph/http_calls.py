"""Outbound HTTP calls in Java code: which provider endpoints a system calls, and with what.
Pure: parser facts in, HttpCallFacts out.

Found from what one file shows, like the rest of the graph, and each fact says how (`resolution`):

- "declared": a declarative client, where annotations say it all: Feign (@FeignClient) and
  Spring HTTP interfaces (@HttpExchange, @GetExchange...).
- "literal": an imperative call (RestTemplate, WebClient, RestClient) whose path is a string
  literal or a concatenation with literal parts.
- "partial": an imperative call whose path isn't visible (a variable, a builder elsewhere).

Base URLs come from @FeignClient(url), a `baseUrl(...)`/`rootUri(...)` builder call (in the same
type or in a @Bean method), or the first part of a concatenated URL; an @Value field or
parameter, or a @ConfigurationProperties getter, turns them into a "${property}" placeholder.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import Annotation, CallSite, JavaFile, MethodDecl, Parameter, TypeDecl, TypeRef

# Declarative clients: annotation -> HTTP method (None: given by `method`).
_DECLARATIVE: dict[str, str | None] = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH", "RequestMapping": None,
    "GetExchange": "GET", "PostExchange": "POST", "PutExchange": "PUT",
    "DeleteExchange": "DELETE", "PatchExchange": "PATCH", "HttpExchange": None,
    "RequestLine": None,  # OpenFeign's own contract: @RequestLine("GET /orders/{id}")
}  # fmt: skip
_EXCHANGES = frozenset(a for a in _DECLARATIVE if a.endswith("Exchange"))

CLIENT_TYPES = {"RestTemplate": "rest_template", "WebClient": "web_client", "RestClient": "rest_client",
                "TestRestTemplate": "rest_template"}  # fmt: skip
_FLUENT_VERBS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH",
                 "head": "HEAD", "options": "OPTIONS", "method": None}  # fmt: skip
# RestTemplate: method -> (HTTP method or None for the 2nd argument, request body arg, response arg)
_REST_TEMPLATE: dict[str, tuple[str | None, int | None, int | None]] = {
    "getForObject": ("GET", None, 1), "getForEntity": ("GET", None, 1),
    "postForObject": ("POST", 1, 2), "postForEntity": ("POST", 1, 2),
    "postForLocation": ("POST", 1, None), "put": ("PUT", 1, None), "delete": ("DELETE", None, None),
    "patchForObject": ("PATCH", 1, 2), "headForHeaders": ("HEAD", None, None),
    "exchange": (None, 2, 3), "execute": (None, None, None),
}  # fmt: skip
_RESPONSE_CALLS = frozenset({"bodyToMono", "bodyToFlux", "toEntity", "toEntityList", "toEntityFlux"})
_BASE_URL_CALLS = frozenset({"baseUrl", "rootUri"})
_WRAPPERS = frozenset({"ResponseEntity", "Mono", "Flux", "Optional", "CompletableFuture", "HttpEntity"})

_PRIMITIVE = frozenset({"int", "long", "boolean", "double", "float", "short", "byte", "char"})
_SCALARS = frozenset({"String", "Integer", "Long", "Boolean", "Double", "UUID", "LocalDate"})
_HTTP_METHOD = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b")
_CLASS_LITERAL = re.compile(r"^([\w$.]+(?:\[\])*)\.class$")
_TYPE_REFERENCE = re.compile(r"ParameterizedTypeReference\s*<\s*(.+?)\s*>\s*\(")
_PLACEHOLDER_KEY = re.compile(r"\$\{([^}:]+)")
_STRING = re.compile(r'"((?:\\.|[^"\\])*)"')
_LAMBDA_PATH = re.compile(r'\.path\(\s*"([^"]*)"')
_LAMBDA_QUERY = re.compile(r'\.queryParam\(\s*"([^"]+)"')
_GETTER = re.compile(r"^(?:this\.)?(\w+)\.(?:get)?(\w+)\(\)$")
_IDENT = re.compile(r"^(?:this\.)?([A-Za-z_$][\w$]*)$")
# A constant as annotation source text: ORDERS_URL, or a dotted name with a capitalised part.
_CONSTANT = re.compile(r"[A-Z_][A-Z0-9_]*|(?=[\w$.]*\b[A-Z])[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+")
_HEADER_SET = re.compile(r'\.(?:set|add)\(\s*"([A-Za-z][\w-]*)"')


@dataclass
class HttpCallFact:
    method: MethodDecl  # the method that makes the call (for a declarative client: its method)
    client: str  # feign | http_interface | rest_template | web_client | rest_client
    line: int
    resolution: str  # declared | literal | partial
    http_method: str | None = None
    path: str | None = None  # template: "/orders/{id}"; "{}" marks a value built in code
    query: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    request_body: str | None = None  # type as written, or the argument text when unknown
    response: str | None = None  # type as written: "Order", "List<Order>"
    base_url: str | None = None  # "${order.api.url}", "http://orders:8080", or None
    base_url_via: str | None = None
    service: str | None = None  # @FeignClient name, or the client field/bean name
    notes: list[str] = field(default_factory=list)

    @property
    def property_keys(self) -> list[str]:
        return _PLACEHOLDER_KEY.findall(self.base_url or "")


@dataclass
class ClientBean:
    """A @Bean method that builds a client with a base URL: WebClient orderClient(...)."""

    name: str
    client_type: str
    base_url: str
    via: str


@dataclass
class ClientEnv:
    """What one file can't show, for one build module (one application): client beans,
    @ConfigurationProperties prefixes, and the base URLs of HTTP interfaces."""

    beans: dict[str, list[ClientBean]] = field(default_factory=dict)  # by client type name
    config_prefixes: dict[str, str] = field(default_factory=dict)  # simple type name -> prefix
    interface_bases: dict[str, tuple[str, str]] = field(default_factory=dict)  # simple name -> (base, via)
    config_bases: list[tuple[str, str]] = field(default_factory=list)  # baseUrl(...) in config types


def module_of(path: str) -> str:
    """The build module a file belongs to: "order-service" for order-service/src/main/...,
    "" for a single-module repo."""
    index = ("/" + path).find("/src/")
    return path[: index - 1] if index > 0 else ""


def client_env(files: Iterable[JavaFile]) -> ClientEnv:
    """The environment of one module's files; build one per module (see module_of)."""
    env = ClientEnv()
    files = list(files)
    for jf in files:
        for decl in jf.types:
            props = _annotation(decl.annotations, "ConfigurationProperties")
            if props is not None:
                prefix = _first_string(props, "prefix", "value")
                if prefix:
                    env.config_prefixes[decl.name.rsplit(".", 1)[-1]] = prefix
    for jf in files:
        for decl in jf.types:
            _scan_config_type(decl, env)
    return env


def _scan_config_type(decl: TypeDecl, env: ClientEnv) -> None:
    """@Bean methods building clients, and @ImportHttpServices groups with their baseUrl."""
    names = {a.name for a in decl.annotations}
    type_bases = []
    for m in decl.methods:
        is_bean = "Bean" in {a.name for a in m.annotations}
        base, via = _builder_base_url(m.calls, decl, m, env)
        if not base or not (is_bean or names & {"Configuration", "ImportHttpServices"}):
            continue
        where = f"{decl.name}#{m.name}: {via}"
        type_bases.append((base, where))
        env.config_bases.append((base, where))
        returned = _simple(m.return_type.name) if m.return_type else ""
        if not is_bean:
            continue
        if returned in CLIENT_TYPES:
            bean_name = _first_string(_annotation(m.annotations, "Bean"), "name", "value") or m.name
            env.beans.setdefault(returned, []).append(ClientBean(bean_name, returned, base, f"@Bean {where}"))
        elif returned and returned[:1].isupper():  # @Bean OrderClient orderClient(): an HTTP interface proxy
            env.interface_bases.setdefault(returned, (base, f"@Bean {where}"))
    distinct = list(dict.fromkeys(b for b, _ in type_bases))
    for a in decl.annotations:
        if a.name == "ImportHttpServices" and len(distinct) == 1:
            for text in _strings(a.arguments.get("types")) + _strings(a.arguments.get("value")):
                for literal in re.findall(r"([\w$.]+)\.class", text):
                    via = next(w for b, w in type_bases if b == distinct[0])
                    env.interface_bases.setdefault(_simple(literal), (distinct[0], f"@ImportHttpServices {via}"))


def find_http_calls(decl: TypeDecl, env: ClientEnv | None = None) -> list[HttpCallFact]:
    """The outbound HTTP calls one type makes (or declares, for a client interface)."""
    env = env or ClientEnv()
    if decl.kind == "interface":
        return _declarative(decl, env)
    return _imperative(decl, env)


# ---- declarative clients ------------------------------------------------------------------


def _declarative(decl: TypeDecl, env: ClientEnv) -> list[HttpCallFact]:
    feign = _annotation(decl.annotations, "FeignClient")
    exchange = _annotation(decl.annotations, "HttpExchange")
    methods = [m for m in decl.methods if any(a.name in _DECLARATIVE for a in m.annotations)]
    is_http_interface = exchange is not None or any(
        a.name in _EXCHANGES for m in methods for a in m.annotations
    )
    if feign is None and not is_http_interface:
        return []  # an interface with @GetMapping and no @FeignClient is a server-side API
    client = "feign" if feign is not None else "http_interface"
    base_url = service = None
    prefix = ""
    if feign is not None:
        base_url = _first_string(feign, "url") or None
        service = _first_string(feign, "name", "value", "contextId") or None
        prefix = _first_string(feign, "path") or ""
    mapping = _annotation(decl.annotations, "RequestMapping")
    if mapping is not None:
        prefix = _join(prefix, _first_string(mapping, "value", "path") or "")
    if exchange is not None:
        url = _first_string(exchange, "value", "url") or ""
        base_part, path_part = _split_base(url)
        base_url, prefix = base_url or base_part, _join(prefix, path_part)
    base_url_via = (f"@{'FeignClient' if feign else 'HttpExchange'}(url)") if base_url else None
    if base_url is None and client == "http_interface":
        simple = _simple(decl.name)
        bases = list(dict.fromkeys(b for b, _ in env.config_bases))
        if simple in env.interface_bases:
            base_url, base_url_via = env.interface_bases[simple]
        elif len(bases) == 1:  # one baseUrl(...) in the module's config: the client group's
            base_url = bases[0]
            base_url_via = "the only baseUrl(...) in the module's config: " + next(
                w for b, w in env.config_bases if b == bases[0]
            )
    facts = []
    for m in methods:
        a = next(a for a in m.annotations if a.name in _DECLARATIVE)
        verb, path = _declared_route(a)
        fact = HttpCallFact(
            method=m, client=client, line=m.start_line, resolution="declared",
            http_method=verb, path=_join(prefix, path) if path is not None else prefix or None,
            response=_unwrap(m.return_type), base_url=base_url, base_url_via=base_url_via,
            service=service,
        )  # fmt: skip
        _declared_params(fact, m.parameters, client)
        facts.append(fact)
    return facts


def _declared_route(a: Annotation) -> tuple[str | None, str | None]:
    if a.name == "RequestLine":  # "GET /orders/{id}?x={x}"
        text = _first_string(a, "value") or ""
        verb, _, path = text.partition(" ")
        return (verb.upper() or None), (path.strip() or None)
    verb = _DECLARATIVE[a.name]
    if verb is None:
        found = _HTTP_METHOD.findall(" ".join(_strings(a.arguments.get("method"))))
        verb = found[0] if found else None
    path = _first_string(a, "value", "path", "url")
    return verb, path


def _declared_params(fact: HttpCallFact, params: list[Parameter], client: str) -> None:
    for p in params:
        names = {a.name for a in p.annotations}
        if "RequestParam" in names or "Param" in names:
            fact.query.append(_param_name(p, "RequestParam", "Param"))
        elif "RequestHeader" in names or "Header" in names:
            fact.headers.append(_param_name(p, "RequestHeader", "Header"))
        elif "RequestBody" in names:
            fact.request_body = p.type.text
        elif "SpringQueryMap" in names or "QueryMap" in names:
            fact.notes.append(f"query parameters from the fields of {p.type.text} ({p.name})")
        elif not names and client == "feign" and fact.request_body is None and not _scalar(p.type):
            fact.request_body = p.type.text  # Feign sends an unannotated parameter as the body
    fact.path = fact.path or None
    _split_query(fact)


def _param_name(p: Parameter, *annotations: str) -> str:
    for a in p.annotations:
        if a.name in annotations:
            return _first_string(a, "value", "name") or p.name
    return p.name


# ---- imperative clients -------------------------------------------------------------------


def _imperative(decl: TypeDecl, env: ClientEnv) -> list[HttpCallFact]:
    clients = _client_fields(decl)
    if not clients and not _uses_client_params(decl):
        return []
    type_bases = _type_base_urls(decl, env)
    facts: list[HttpCallFact] = []
    for m in decl.methods:
        if "Bean" in {a.name for a in m.annotations}:
            continue  # a bean method builds a client; it doesn't call one
        local = {p.name: _simple(p.type.name) for p in m.parameters if _simple(p.type.name) in CLIENT_TYPES}
        known = {**clients, **local}
        facts += _rest_template_calls(decl, m, known, env)
        facts += _fluent_calls(decl, m, known, env)
    for fact in facts:
        if fact.base_url is None:
            bean = _bean_for(fact.service, fact.client, decl, env)
            if bean is not None:
                fact.base_url, fact.base_url_via = bean.base_url, bean.via
            else:
                client_type = next(t for t, c in CLIENT_TYPES.items() if c == fact.client)
                if client_type in type_bases:
                    fact.base_url, fact.base_url_via = type_bases[client_type]
    return sorted(facts, key=lambda f: f.line)


def _client_fields(decl: TypeDecl) -> dict[str, str]:
    """Field name -> client type, for fields typed RestTemplate, WebClient or RestClient."""
    return {f.name: _simple(f.type.name) for f in decl.fields if _simple(f.type.name) in CLIENT_TYPES}


def _uses_client_params(decl: TypeDecl) -> bool:
    """Any client or client builder in a field, a parameter or a static call: worth a look."""
    names = [f.type.name for f in decl.fields]
    names += [p.type.name for m in decl.methods for p in m.parameters]
    if any(_simple(n.removesuffix(".Builder")) in CLIENT_TYPES for n in names):
        return True
    return any(
        (c.receiver or "").startswith(("WebClient.", "RestClient.")) for m in decl.methods for c in m.calls
    )


def _receiver_root(receiver: str | None) -> str | None:
    if not receiver:
        return None
    root = receiver.removeprefix("this.").split(".", 1)[0].split("(", 1)[0]
    return root or None


def _rest_template_calls(
    decl: TypeDecl, m: MethodDecl, clients: dict[str, str], env: ClientEnv
) -> list[HttpCallFact]:
    facts = []
    for c in m.calls:
        root = _receiver_root(c.receiver)
        if c.kind != "call" or c.name not in _REST_TEMPLATE or c.receiver is None:
            continue
        if clients.get(root or "") not in ("RestTemplate", "TestRestTemplate") or "." in c.receiver.removeprefix("this."):
            continue
        verb, body_at, response_at = _REST_TEMPLATE[c.name]
        if verb is None and len(c.args) > 1:
            found = _HTTP_METHOD.findall(c.args[1])
            verb = found[0] if found else None
        fact = HttpCallFact(method=m, client="rest_template", line=c.line, resolution="partial",
                            http_method=verb, service=root)  # fmt: skip
        if c.args:
            _apply_url(fact, c.args[0], decl, m, env)
        if body_at is not None and len(c.args) > body_at and c.args[body_at] != "null":
            fact.request_body = _value_type(c.args[body_at], decl, m)
        if response_at is not None and len(c.args) > response_at:
            fact.response = _response_type(c.args[response_at])
        if c.name in ("exchange", "execute") and len(c.args) == 1:
            fact.notes.append("a RequestEntity built elsewhere: method and URL not visible here")
        facts.append(fact)
    return facts


def _fluent_calls(
    decl: TypeDecl, m: MethodDecl, clients: dict[str, str], env: ClientEnv
) -> list[HttpCallFact]:
    """webClient.post().uri("/orders").header("X", v).bodyValue(o).retrieve().bodyToMono(Order.class):
    a chain starts at a verb call on a WebClient/RestClient; later calls whose receiver text
    starts with that call belong to it (the latest such start wins)."""
    chains: list[tuple[str, HttpCallFact, dict[str, bool]]] = []
    for c in m.calls:
        if c.kind != "call" or c.receiver is None:
            continue
        receiver = c.receiver.removeprefix("this.")
        client = clients.get(receiver) or _built_client(receiver, decl, m)
        if client in ("WebClient", "RestClient") and c.name in _FLUENT_VERBS:
            verb = _FLUENT_VERBS[c.name]
            if verb is None and c.args:
                found = _HTTP_METHOD.findall(c.args[0])
                verb = found[0] if found else None
            fact = HttpCallFact(method=m, client=CLIENT_TYPES[client], line=c.line,
                                resolution="partial", http_method=verb, service=receiver)  # fmt: skip
            chains.append((f"{receiver}.{c.name}(", fact, {"retrieved": False}))
            continue
        owner = next((ch for ch in reversed(chains) if receiver.startswith(ch[0])), None)
        if owner is not None:
            _chain_member(owner[1], owner[2], c, decl, m, env)
    return [fact for _, fact, _ in chains]


def _built_client(receiver: str, decl: TypeDecl, m: MethodDecl) -> str | None:
    """A client made on the spot: webClientBuilder.build(), WebClient.create(url),
    RestClient.builder().baseUrl(url).build()."""
    created = re.fullmatch(r"(WebClient|RestClient)\.(?:create\(.*\)|builder\(\).*\.build\(\))", receiver)
    if created:
        return created.group(1)
    built = re.fullmatch(r"([A-Za-z_$][\w$]*)(?:\.[\w$]+\(.*\))*\.build\(\)", receiver)
    return _builder_client(built.group(1), decl, m) if built else None


def _chain_member(
    fact: HttpCallFact, state: dict[str, bool], c: CallSite, decl: TypeDecl, m: MethodDecl,
    env: ClientEnv,
) -> None:
    name, args = c.name, c.args
    if name == "uri" and args:
        lambda_text = args[0]
        if "->" in lambda_text:
            paths = _LAMBDA_PATH.findall(lambda_text)
            fact.path = _join(*paths) if paths else None
            fact.query += _LAMBDA_QUERY.findall(lambda_text)
            fact.resolution = "literal" if paths else "partial"
        else:
            _apply_url(fact, args[0], decl, m, env)
    elif name == "header" and c.string_args:
        fact.headers.append(c.string_args[0])
    elif name == "contentType":
        fact.headers.append("Content-Type")
    elif name == "accept":
        fact.headers.append("Accept")
    elif name == "headers" and args:
        if "BearerAuth" in args[0] or "BasicAuth" in args[0]:
            fact.headers.append("Authorization")
        fact.headers += _HEADER_SET.findall(args[0])
    elif name in ("bodyValue", "syncBody") and args:
        fact.request_body = _value_type(args[0], decl, m)
    elif name == "body" and args and not state["retrieved"]:
        if len(args) >= 2:  # WebClient body(publisher, Order.class)
            fact.request_body = _response_type(args[1]) or _value_type(args[0], decl, m)
        else:
            fact.request_body = _value_type(args[0], decl, m)
    elif name in ("retrieve", "exchange", "exchangeToMono", "exchangeToFlux"):
        state["retrieved"] = True
    elif (name in _RESPONSE_CALLS or (name == "body" and state["retrieved"])) and args:
        fact.response = _response_type(args[0])
        if name in ("bodyToFlux", "toEntityList", "toEntityFlux") and fact.response:
            fact.response = f"List<{fact.response}>"
    fact.headers = list(dict.fromkeys(fact.headers))


# ---- URLs ---------------------------------------------------------------------------------


def _apply_url(fact: HttpCallFact, text: str, decl: TypeDecl, m: MethodDecl, env: ClientEnv) -> None:
    """A URL argument: a literal, a concatenation, or a variable."""
    parts = _concat(text)
    if not any(is_literal for _, is_literal in parts):
        fact.notes.append(f"URL from `{text}`: not visible here")
        return
    first, first_literal = parts[0]
    if not first_literal:  # baseUrl + "/orders/" + id
        base = _expression_value(first, decl, m, env)
        if base:
            fact.base_url, fact.base_url_via = base, f"`{first}` in the URL"
        else:
            fact.notes.append(f"base URL from `{first}`: not resolved (a local variable, a "
                              "method or service discovery); read the caller source")  # fmt: skip
        parts = parts[1:]
    url = "".join(p if is_literal else "{}" for p, is_literal in parts)
    base_part, path = _split_base(url)
    if base_part and not fact.base_url:
        fact.base_url, fact.base_url_via = base_part, "literal URL"
    fact.path = "/" + path.lstrip("/") if path else "/"
    fact.resolution = "literal"
    _split_query(fact)


def _concat(text: str) -> list[tuple[str, bool]]:
    """`base + "/x/" + id` -> [("base", False), ("/x/", True), ("id", False)]."""
    parts, depth, current, i = [], 0, "", 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            match = _STRING.match(text, i)
            if match is None:
                break
            current += match.group(0)
            i = match.end()
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "+" and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
        i += 1
    parts.append(current.strip())
    result = []
    for part in parts:
        match = _STRING.fullmatch(part)
        result.append((match.group(1).replace('\\"', '"'), True) if match else (part, False))
    return [(p, lit) for p, lit in result if p or lit]


def _split_base(url: str) -> tuple[str | None, str]:
    """"http://orders:8080/api/orders" -> ("http://orders:8080", "/api/orders")."""
    match = re.match(r"^([a-zA-Z][\w+.-]*://[^/]*|\$\{[^}]*\})(.*)$", url)
    if match:
        return match.group(1), match.group(2) or ""
    return None, url


def _split_query(fact: HttpCallFact) -> None:
    if fact.path and "?" in fact.path:
        fact.path, _, query = fact.path.partition("?")
        fact.query += [q.split("=", 1)[0] for q in query.split("&") if q.split("=", 1)[0]]
    fact.query = list(dict.fromkeys(fact.query))


def _join(*parts: str) -> str:
    joined = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return "/" + joined if joined else ""


# ---- base URLs ----------------------------------------------------------------------------


def _type_base_urls(decl: TypeDecl, env: ClientEnv) -> dict[str, tuple[str, str]]:
    """Client type -> base URL, from baseUrl(...) builder calls in the type's own constructors
    and methods; only when there is one per client type."""
    found: dict[str, set[tuple[str, str]]] = {}
    for m in decl.methods:
        for c in m.calls:
            if c.kind != "call" or c.name not in _BASE_URL_CALLS or not c.args:
                continue
            client = _builder_client(c.receiver, decl, m)
            value = _expression_value(c.args[0], decl, m, env)
            if client and value:
                where = m.name if m.kind == "method" else "<init>"
                found.setdefault(client, set()).add((value, f"{c.name}({c.args[0]}) in {decl.name}#{where}"))
    return {client: next(iter(v)) for client, v in found.items() if len({b for b, _ in v}) == 1}


def _builder_client(receiver: str | None, decl: TypeDecl, m: MethodDecl) -> str | None:
    """The client type a builder chain makes: RestClient.builder()..., or a RestClient.Builder
    variable."""
    root = _receiver_root(receiver)
    if root in CLIENT_TYPES:
        return root
    typed = [p.type.name for p in m.parameters if p.name == root] + [
        f.type.name for f in decl.fields if f.name == root
    ]
    for name in typed:
        owner = name.removesuffix(".Builder")
        if owner != name and _simple(owner) in CLIENT_TYPES:
            return _simple(owner)
        if _simple(name) == "RestTemplateBuilder":
            return "RestTemplate"
    return None


def _builder_base_url(
    calls: list[CallSite], decl: TypeDecl, m: MethodDecl, env: ClientEnv
) -> tuple[str | None, str | None]:
    for c in calls:
        if c.kind == "call" and c.name in _BASE_URL_CALLS and c.args:
            value = _expression_value(c.args[0], decl, m, env)
            if value:
                return value, f"{c.name}({c.args[0]})"
    return None, None


def _bean_for(name: str | None, client: str, decl: TypeDecl, env: ClientEnv) -> ClientBean | None:
    """The bean Spring would inject: by @Qualifier, then by field name, then the only one."""
    client_type = next((t for t, c in CLIENT_TYPES.items() if c == client and t != "TestRestTemplate"), "")
    beans = env.beans.get(client_type, [])
    if not beans:
        return None
    wanted = name
    for f in decl.fields:
        if f.name == name:
            wanted = _first_string(_annotation(f.annotations, "Qualifier"), "value") or name
    for p in (p for m in decl.methods if m.kind == "constructor" for p in m.parameters):
        if p.name == name:
            wanted = _first_string(_annotation(p.annotations, "Qualifier"), "value") or name
    named = [b for b in beans if b.name == wanted]
    return named[0] if named else beans[0] if len(beans) == 1 else None


def _expression_value(text: str, decl: TypeDecl, m: MethodDecl, env: ClientEnv) -> str | None:
    """A string literal, an @Value field or parameter, or a @ConfigurationProperties getter."""
    literal = _STRING.fullmatch(text.strip())
    if literal:
        return literal.group(1)
    ident = _IDENT.match(text.strip())
    if ident:
        name = ident.group(1)
        for p in m.parameters:
            if p.name == name:
                return _value_annotation(p.annotations)
        for f in decl.fields:
            if f.name == name:
                return _value_annotation(f.annotations) or _constant(f.initializer)
        for ctor in (x for x in decl.methods if x.kind == "constructor"):
            for p in ctor.parameters:
                if p.name == name:  # a field assigned from a constructor @Value parameter
                    return _value_annotation(p.annotations)
        return None
    getter = _GETTER.match(text.strip())
    if getter:
        holder, prop = getter.groups()
        holder_type = next((f.type.name for f in decl.fields if f.name == holder), None)
        holder_type = holder_type or next((p.type.name for p in m.parameters if p.name == holder), None)
        prefix = env.config_prefixes.get(_simple(holder_type or ""))
        if prefix:
            return "${" + f"{prefix}.{_kebab(prop)}" + "}"
    return None


def _value_annotation(annotations: list[Annotation]) -> str | None:
    value = _annotation(annotations, "Value")
    return _first_string(value, "value") or None if value else None


def _constant(initializer: str | None) -> str | None:
    match = _STRING.fullmatch((initializer or "").strip())
    return match.group(1) if match else None


def _kebab(name: str) -> str:
    """Spring's relaxed binding: getBaseUrl() reads base-url (or baseUrl)."""
    name = name[:1].lower() + name[1:]
    return re.sub(r"(?<!^)([A-Z])", lambda m: "-" + m.group(1).lower(), name)


# ---- types --------------------------------------------------------------------------------


def _response_type(text: str) -> str | None:
    text = text.strip()
    literal = _CLASS_LITERAL.match(text)
    if literal:
        name = literal.group(1).rsplit(".", 1)[-1]
        return None if name == "Void" else name
    ref = _TYPE_REFERENCE.search(text)
    if ref:
        return ref.group(1)
    return None


def _value_type(text: str, decl: TypeDecl, m: MethodDecl) -> str:
    """The declared type of a parameter or field passed as a body; the text itself otherwise."""
    text = text.strip()
    ident = _IDENT.match(text)
    if ident:
        for p in m.parameters:
            if p.name == ident.group(1):
                return _unwrap(p.type) or p.type.text
        for f in decl.fields:
            if f.name == ident.group(1):
                return f.type.text
    created = re.match(r"^new\s+([\w.<>, ]+?)\s*\(", text)
    if created:
        created_type = created.group(1)
        if _simple(created_type.split("<", 1)[0]) == "HttpEntity" and "<" in created_type:
            return created_type.split("<", 1)[1].rsplit(">", 1)[0]
        return created_type
    return text


def _unwrap(ref: TypeRef | None) -> str | None:
    """ResponseEntity<Order> -> Order; Mono<List<Order>> -> List<Order>; void -> None."""
    if ref is None or ref.name == "void":
        return None
    while _simple(ref.name) in _WRAPPERS and ref.args:
        ref = ref.args[0]
    if _simple(ref.name) in ("Void", "?"):
        return None
    return ref.text


def _scalar(ref: TypeRef) -> bool:
    return ref.name in _PRIMITIVE or _simple(ref.name) in _SCALARS


# ---- annotations --------------------------------------------------------------------------


def _annotation(annotations: list[Annotation], name: str) -> Annotation | None:
    return next((a for a in annotations if a.name == name), None)


def _strings(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)] if value is not None else []


def _first_string(a: Annotation | None, *keys: str) -> str | None:
    """The first string argument among keys. A constant (ApiPaths.ORDERS, ORDERS_URL) is raw
    source text with no known value, so it doesn't count."""
    if a is None:
        return None
    for key in keys:
        for value in _strings(a.arguments.get(key)):
            if value and not _CONSTANT.fullmatch(value):
                return value
    return None


def _simple(name: str) -> str:
    return name.rsplit(".", 1)[-1]
