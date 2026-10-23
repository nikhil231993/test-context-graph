"""Contract-test context over an index: which provider endpoints a system calls, and for one
call, what an agent needs to write a Specmatic consumer test: the request the code sends, the
types, the base-URL property to point at the mock, the provider operation it matches, the entry
points that make the call (what the test drives), and a draft example.

Specmatic judges the result: the consumer runs against a strict mock built from the provider's
spec, so the draft here only has to be a good start, not proof.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .context import ContextEngine, ContextError
from .models import (
    CALLS,
    CALLS_HTTP,
    CONFIGURED_BY,
    GENERATED_FROM,
    HTTP_CALL,
    METHOD,
    OVERRIDES,
    SERVES,
    TARGETS,
    TESTS,
    TYPE,
    USES,
    Node,
)

MOCK_BASE_URL = "http://localhost:9000"  # where `specmatic mock` listens by default
MAX_CALLERS_DEPTH = 6
MAX_ENTRY_POINTS = 5
MAX_TESTS = 5
_ENTRY_ANNOTATIONS = frozenset(
    {"KafkaListener", "JmsListener", "RabbitListener", "SqsListener", "Scheduled", "EventListener"}
)
_NUMBERS = frozenset({"int", "long", "short", "byte", "double", "float", "Integer", "Long", "Short",
                      "Double", "Float", "BigDecimal", "BigInteger", "Number"})  # fmt: skip
_BOOLEANS = frozenset({"boolean", "Boolean"})
# Java types Specmatic has a typed value for, beyond (string), (number) and (boolean).
_TYPED = {"UUID": "(uuid)", "OffsetDateTime": "(datetime)", "ZonedDateTime": "(datetime)",
          "Instant": "(datetime)", "LocalDateTime": "(datetime)", "LocalDate": "(date)",
          "URI": "(url)", "URL": "(url)"}  # fmt: skip
_LISTS = frozenset({"List", "Set", "Collection", "Iterable", "Flux"})


class ContractContext:
    def __init__(self, engine: ContextEngine) -> None:
        self.e = engine

    # ---- all calls ---------------------------------------------------------------------------

    def outbound_calls(self, symbol: str | None = None) -> dict[str, Any]:
        """Every outbound HTTP call, grouped by provider (base URL); with a symbol, only the
        calls it makes, directly or through the methods it calls."""
        calls = self._calls_of(symbol) if symbol else self._all_calls()
        groups: dict[str, list[Node]] = defaultdict(list)
        for node in calls:
            groups[_provider_key(node)].append(node)
        providers = []
        for key, nodes in sorted(groups.items()):
            first = nodes[0].attrs
            providers.append({
                "provider": key,
                "base_url": first.get("base_url"),
                "base_url_properties": first.get("base_url_properties") or [],
                "config": self._config(nodes[0].id),
                "calls": [self._summary(n) for n in sorted(nodes, key=lambda n: (n.path or "", n.start_line or 0))],
            })  # fmt: skip
        return {"symbol": symbol, "count": len(calls), "providers": providers}

    def _all_calls(self) -> list[Node]:
        return [n for n in self.e.nodes.values() if n.kind == HTTP_CALL]

    def _calls_of(self, symbol: str) -> list[Node]:
        nodes = self.e.resolve(symbol)
        if not nodes:
            raise ContextError(f"no symbol matches {symbol!r}")
        start = nodes[0]
        methods = [start.id] if start.kind == METHOD else self.e.contained(start.id, METHOD)
        seen, found = list(methods), []
        for current in seen:  # breadth first through calls, implementations included
            for edge in self.e.edges_out(current, CALLS_HTTP):
                if edge.target not in found:
                    found.append(edge.target)
            if len(seen) > 500:
                break
            nexts = [e.target for e in self.e.edges_out(current, CALLS)]
            nexts += [o for t in nexts for o in self.e.overriders(t)]
            seen += [n for n in nexts if n not in seen and n in self.e.nodes]
        return [self.e.nodes[i] for i in found if i in self.e.nodes]

    def _summary(self, node: Node) -> dict[str, Any]:
        a = node.attrs
        targets = [e.target for e in self.e.edges_out(node.id, TARGETS)]
        return {
            "id": node.id,
            "call": node.name,
            "client": a.get("client"),
            "from": self.e.label(self._caller(node)),
            "location": f"{node.path}:{node.start_line}",
            "provider_operation": [self._operation_label(t) for t in targets],
            "resolution": a.get("resolution"),
        }

    # ---- one call ----------------------------------------------------------------------------

    def call_context(self, call: str) -> dict[str, Any]:
        node = self.pick_call(call)
        a = node.attrs
        caller = self._caller(node)
        notes = list(a.get("notes") or [])
        request = self._data(node.id, "request", a.get("request_body"))
        response = self._data(node.id, "response", a.get("response"))
        operations = [self._operation(e.target) for e in self.e.edges_out(node.id, TARGETS)]
        if not operations:
            notes.append("no provider spec in the index matches this call: find the spec (e.g. "
                         "ghes-openapi-mcp find_openapi_spec) and index it in a contracts/ folder")  # fmt: skip
        config = self._config(node.id)
        overrides = self._overrides(node, config, notes)
        entries = self._entry_points(caller)
        if a.get("resolution") == "partial":
            notes.append("the path isn't fully visible in code: read the source below")
        return {
            "id": node.id,
            "call": node.name,
            "client": a.get("client"),
            "service": a.get("service"),
            "location": f"{node.path}:{node.start_line}",
            "caller": {"id": caller, "label": self.e.label(caller), "source": self.e.text(caller)},
            "request": {
                "method": a.get("http_method"), "path": a.get("path"),
                "query": a.get("query") or [], "headers": a.get("headers") or [],
                "body": request,
            },  # fmt: skip
            "response": response,
            "base_url": {"value": a.get("base_url"), "via": a.get("base_url_via"),
                         "properties": a.get("base_url_properties") or [], "config": config},  # fmt: skip
            "provider_operations": operations,
            "entry_points": entries,
            "existing_tests": self._tests(caller, entries),
            "specmatic": {
                "mock_base_url_overrides": overrides,
                "draft_example": self._draft_example(node, request, response, operations),
            },
            "notes": notes,
        }

    def pick_call(self, call: str) -> Node:
        """An http_call id (with or without "http_call:"), "POST /orders" (its name), or a
        method symbol that makes exactly one call."""
        text = " ".join(call.split())
        for candidate in (text, f"http_call:{text}"):
            node = self.e.nodes.get(candidate)
            if node is not None and node.kind == HTTP_CALL:
                return node
        named = [n for n in self._all_calls() if n.name == text or n.name.endswith(f" {text}")]
        if len(named) == 1:
            return named[0]
        if not named:
            try:
                named = self._calls_of(text)
            except ContextError:
                named = []
            if len(named) == 1:
                return named[0]
        if named:
            options = ", ".join(f"{n.id} ({n.name})" for n in named[:8])
            raise ContextError(f"{call!r} matches {len(named)} calls; pass one id: {options}")
        raise ContextError(f"no outbound HTTP call matches {call!r}; list them with list_outbound_calls")

    def _caller(self, node: Node) -> str:
        edges = self.e.edges_in(node.id, CALLS_HTTP)
        return edges[0].source if edges else node.id

    def _config(self, call_id: str) -> list[dict[str, Any]]:
        found = []
        for edge in self.e.edges_out(call_id, CONFIGURED_BY):
            c = self.e.nodes.get(edge.target)
            if c is not None:
                found.append({"key": c.attrs.get("key"), "value": c.attrs.get("value"),
                              "profile": c.attrs.get("profile"), "file": c.path, "line": c.start_line})  # fmt: skip
        return found

    def _overrides(self, node: Node, config: list[dict[str, Any]], notes: list[str]) -> list[str]:
        """How to point the consumer at the Specmatic mock."""
        keys = list(dict.fromkeys(node.attrs.get("base_url_properties") or [c["key"] for c in config]))
        if keys:
            if any(_url_has_path(c["value"]) for c in config):
                notes.append("the configured base URL has a path; the Specmatic mock serves the spec's "
                             f"paths at its root, so override it with {MOCK_BASE_URL} and no path")  # fmt: skip
            return [f"{key}={MOCK_BASE_URL}" for key in keys]
        base = node.attrs.get("base_url")
        service = node.attrs.get("service")
        if node.attrs.get("client") == "feign" and service and not base:
            notes.append(f"Feign client `{service}` is found by service discovery: point it at the "
                         "mock with the OpenFeign client URL property")  # fmt: skip
            return [f"spring.cloud.openfeign.client.config.{service}.url={MOCK_BASE_URL}"]
        if base:
            notes.append(f"the base URL is hard-coded ({base}): the consumer can't be pointed at a "
                         "mock without a code change; suggest moving it to a property")  # fmt: skip
        else:
            notes.append("no base URL found: check how the client bean is built")
        return []

    # ---- types -------------------------------------------------------------------------------

    def _data(self, call_id: str, role: str, written: str | None) -> dict[str, Any] | None:
        if not written:
            return None
        type_ids = [e.target for e in self.e.edges_out(call_id, USES) if e.attrs.get("as") == role]
        return {"type": written, "types": [self._type_fields(t) for t in type_ids]}

    def _type_fields(self, type_id: str) -> dict[str, Any]:
        node = self.e.nodes[type_id]
        a = node.attrs
        declared = (a.get("record_components") or []) + (a.get("fields") or [])
        fields = [{"name": f["name"], "type": f["type"]} for f in declared]
        result: dict[str, Any] = {"id": type_id, "name": node.name, "fields": fields}
        schemas = [e.target for e in self.e.edges_out(type_id, GENERATED_FROM)]
        if schemas and not fields:  # a generated DTO: its fields are in the schema
            schema = self.e.nodes.get(schemas[0])
            if schema is not None:
                result["schema"] = schema.id
                result["fields"] = [{"name": k, "type": v} for k, v in (schema.attrs.get("properties") or {}).items()]
        return result

    # ---- provider operation ------------------------------------------------------------------

    def _operation(self, endpoint_id: str) -> dict[str, Any]:
        ep = self.e.nodes[endpoint_id]
        a = ep.attrs
        return {
            "id": ep.id,
            "operation": ep.name,
            "spec": ep.path,
            "spec_role": a.get("spec_role"),
            "operation_id": a.get("operation_id"),
            "summary": a.get("summary"),
            "parameters": a.get("parameters") or [],
            "request_body": a.get("request_body"),
            "responses": a.get("responses") or {},
            "spec_text": self.e.text(ep.id),
        }

    def _operation_label(self, endpoint_id: str) -> str:
        ep = self.e.nodes.get(endpoint_id)
        if ep is None:
            return endpoint_id
        op = ep.attrs.get("operation_id")
        return f"{ep.name} ({ep.path}{', ' + op if op else ''})"

    # ---- what makes the call -----------------------------------------------------------------

    def _entry_points(self, method: str) -> list[dict[str, Any]]:
        """Where a test can drive the call from: endpoints this system serves, listeners and
        scheduled jobs, or the top of the call chain. Callers are found through CALLS, and
        through the interface methods a method implements (callers call the interface)."""
        paths: dict[str, list[str]] = {method: [method]}
        queue, entries = [method], []
        while queue and len(entries) < MAX_ENTRY_POINTS:
            current = queue.pop(0)
            chain = paths[current]
            callers = [e.source for t in [current, *self.e.overridden(current)]
                       for e in self.e.edges_in(t, CALLS)]  # fmt: skip
            callers = [c for c in dict.fromkeys(callers) if c in self.e.nodes and c not in paths
                       and self.e.role(_owner(c)) not in ("test", "test_support")]  # fmt: skip
            entry = self._entry(current)
            if entry is not None or (not callers and current != method):
                entries.append({**(entry or {"kind": "top of call chain"}),
                                "method": self.e.label(current),
                                "chain": [self.e.label(m) for m in reversed(chain)]})  # fmt: skip
                continue
            if len(chain) >= MAX_CALLERS_DEPTH:
                continue
            for c in callers:
                paths[c] = [*chain, c]
                queue.append(c)
        return entries

    def _entry(self, method: str) -> dict[str, Any] | None:
        for edge in self.e.edges_out(method, SERVES):
            ep = self.e.nodes.get(edge.target)
            if ep is not None:
                served = ep.attrs.get("served_path") or ep.attrs.get("spec_path") or ep.name
                return {"kind": "endpoint", "endpoint": f"{ep.attrs.get('http_method')} {served}"}
        node = self.e.nodes.get(method)
        annotations = set((node.attrs.get("annotations") or []) if node else [])
        found = sorted(annotations & _ENTRY_ANNOTATIONS)
        return {"kind": found[0]} if found else None

    def _tests(self, caller: str, entries: list[dict[str, Any]]) -> list[str]:
        tests: list[str] = []
        targets = [caller, _owner(caller)]
        for target in targets:
            for edge in self.e.edges_in(target, TESTS):
                label = self.e.label(edge.source)
                if label not in tests:
                    tests.append(label)
        return tests[:MAX_TESTS]

    # ---- Specmatic draft ---------------------------------------------------------------------

    def _draft_example(
        self, node: Node, request: dict[str, Any] | None, response: dict[str, Any] | None,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """A Specmatic example to start from: the request as the code sends it, typed values
        such as "(string)" where the code doesn't fix them. Replace path variables with
        concrete values and check it with Specmatic."""
        a = node.attrs
        http_request: dict[str, Any] = {"method": a.get("http_method") or "GET",
                                        "path": a.get("path") or "/"}  # fmt: skip
        if a.get("headers"):
            http_request["headers"] = {h: self._header_value(node, h) for h in a["headers"]}
        if a.get("query"):
            http_request["query"] = {q: "(string)" for q in a["query"]}
        if request:
            http_request["body"] = self._skeleton(request)
        status = _success_status(operations[0]["responses"]) if operations else 200
        http_response: dict[str, Any] = {"status": status}
        if response:
            http_response["body"] = self._skeleton(response)
        return {"http-request": http_request, "http-response": http_response}

    def _header_value(self, node: Node, header: str) -> str:
        """The typed value of a header the code sets from a parameter: UUID key -> (uuid)."""
        caller = self.e.nodes.get(self._caller(node))
        for p in (caller.attrs.get("parameters") or []) if caller else []:
            if "RequestHeader" in (p.get("annotations") or []) and _TYPED.get(p["type"]) and \
                    p["name"].lower().replace("-", "") in header.lower().replace("-", ""):
                return _TYPED[p["type"]]
        return "(string)"

    def _skeleton(self, data: dict[str, Any]) -> Any:
        written = data["type"]
        types = {t["name"]: t for t in data.get("types") or []}
        return self._value(written, types, depth=0)

    def _value(self, written: str, types: dict[str, dict[str, Any]], depth: int) -> Any:
        written = written.strip()
        if written.endswith("[]"):
            return [self._value(written[:-2], types, depth)]
        outer, _, inner = written.partition("<")
        outer = outer.rsplit(".", 1)[-1].strip()
        if outer in _LISTS and inner:
            return [self._value(inner.rsplit(">", 1)[0], types, depth)]
        if outer in _NUMBERS:
            return "(number)"
        if outer in _BOOLEANS:
            return "(boolean)"
        if outer in _TYPED:
            return _TYPED[outer]
        known = types.get(outer) or self._find_type(outer)
        if known is not None and depth < 3:
            return {f["name"]: self._value(f["type"], types, depth + 1) for f in known["fields"]}
        return "(string)"

    def _find_type(self, name: str) -> dict[str, Any] | None:
        if not name[:1].isupper() or name in ("String", "Object"):
            return None
        matches = [n for n in self.e.nodes.values() if n.kind == TYPE and n.name == name]
        return self._type_fields(matches[0].id) if len(matches) == 1 else None


def _url_has_path(value: str | None) -> bool:
    """http://host:8081/payments/v1 -> True; ${X:http://host:8081} -> False."""
    match = re.search(r"[a-z][\w+.-]*://[^/\s}]+(/[^\s}]*)?", value or "")
    return bool(match and match.group(1) and match.group(1) != "/")


def _provider_key(node: Node) -> str:
    a = node.attrs
    props = a.get("base_url_properties") or []
    return props[0] if props else a.get("base_url") or a.get("service") or "(unknown base URL)"


def _owner(method_id: str) -> str:
    return "type:" + method_id.removeprefix("method:").split("#", 1)[0]


def _success_status(responses: dict[str, Any]) -> int:
    for status in sorted(responses):
        if status.startswith("2") and status.isdigit():
            return int(status)
    return 200


# ---- markdown ---------------------------------------------------------------------------------


def render_calls_markdown(result: dict[str, Any]) -> str:
    lines = [f"# Outbound HTTP calls{' of ' + result['symbol'] if result.get('symbol') else ''}"
             f" ({result['count']})", ""]  # fmt: skip
    for p in result["providers"]:
        lines.append(f"## {p['provider']}")
        for c in p["config"]:
            lines.append(f"- `{c['key']}` = `{c['value']}`{' (profile ' + c['profile'] + ')' if c['profile'] else ''}"
                         f" — {c['file']}:{c['line']}")  # fmt: skip
        for call in p["calls"]:
            ops = f" → {', '.join(call['provider_operation'])}" if call["provider_operation"] else ""
            lines.append(f"- **{call['call']}** ({call['client']}) from {call['from']}{ops}  \n"
                         f"  `{call['id']}`")  # fmt: skip
        lines.append("")
    if not result["providers"]:
        lines.append("No outbound HTTP calls found (Feign, HTTP interfaces, RestTemplate, WebClient, RestClient).")
    return "\n".join(lines).rstrip() + "\n"


def render_call_markdown(ctx: dict[str, Any]) -> str:
    r = ctx["request"]
    lines = [f"# {ctx['call']} — {ctx['client']} call in {ctx['caller']['label']}", "",
             f"Location: {ctx['location']}", "", "## Request the code sends",
             f"- {r['method']} `{r['path']}`"]  # fmt: skip
    if r["query"]:
        lines.append(f"- query: {', '.join(r['query'])}")
    if r["headers"]:
        lines.append(f"- headers: {', '.join(r['headers'])}")
    for title, data in (("request body", r["body"]), ("response", ctx["response"])):
        if data:
            lines.append(f"- {title}: `{data['type']}`")
            for t in data["types"]:
                fields = ", ".join(f"{f['name']}: {f['type']}" for f in t["fields"])
                lines.append(f"  - {t['name']} {{{fields}}}")
    b = ctx["base_url"]
    lines += ["", "## Base URL", f"- {b['value'] or 'unknown'}{' (via ' + b['via'] + ')' if b['via'] else ''}"]
    for c in b["config"]:
        lines.append(f"  - `{c['key']}` = `{c['value']}`{' (profile ' + c['profile'] + ')' if c['profile'] else ''}")
    lines += ["", "## Provider operation"]
    if not ctx["provider_operations"]:
        lines.append("- none matched in the index")
    for op in ctx["provider_operations"]:
        lines.append(f"- {op['operation']} in `{op['spec']}`{' (operationId ' + op['operation_id'] + ')' if op['operation_id'] else ''}")
        lines.append(f"  - responses: {', '.join(f'{k} {v}' for k, v in op['responses'].items()) or 'none'}")
        if op["spec_text"]:
            lines += ["", "```yaml", op["spec_text"].rstrip(), "```"]
    lines += ["", "## Entry points (what a consumer test drives)"]
    for e in ctx["entry_points"] or [{"kind": "none found", "method": "", "chain": []}]:
        where = e.get("endpoint") or e["kind"]
        lines.append(f"- {where}: {' → '.join(e['chain'])}")
    if ctx["existing_tests"]:
        lines += ["", "## Existing tests", *[f"- {t}" for t in ctx["existing_tests"]]]
    s = ctx["specmatic"]
    lines += ["", "## Specmatic"]
    if s["mock_base_url_overrides"]:
        lines.append("Point the consumer at the mock: " + ", ".join(f"`{o}`" for o in s["mock_base_url_overrides"]))
    lines += ["", "Draft example (replace path variables, check with Specmatic):", "", "```json",
              json.dumps(s["draft_example"], indent=2), "```"]  # fmt: skip
    if ctx["caller"]["source"]:
        lines += ["", "## Caller source", "", "```java", ctx["caller"]["source"].rstrip(), "```"]
    if ctx["notes"]:
        lines += ["", "## Notes", *[f"- {n}" for n in ctx["notes"]]]
    return "\n".join(lines) + "\n"
