"""OpenAPI 3.0/3.1 and Swagger 2.0 documents (YAML or JSON) -> ApiSpec. Pure: text in, facts out.

Schemas are boiled down to short forms ("Owner", "array<Pet>", "integer",
"allOf<OwnerFields,object>") that name the schemas an endpoint uses, which is what graph.py needs to
link them. Line numbers come from the YAML node tree, so they work for JSON too (JSON is YAML).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from .models import ApiOperation, ApiParameter, ApiSchema, ApiSpec, join_paths

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")

# A top-level openapi/swagger key whose value is a version number. Requiring the number keeps out
# config files that have an `openapi:` section of their own (custom Spring properties).
_YAML_VERSION_KEY = re.compile(r"""^(["']?)(?:openapi|swagger)\1[ \t]*:[ \t]+["']?\d+\.\d""", re.M)
_JSON_VERSION_KEY = re.compile(r""""(?:openapi|swagger)"\s*:\s*"?\d+\.\d""")
_VERSION = re.compile(r"\d+\.\d")
_SERVER_VARIABLE = re.compile(r"\{([^{}]+)\}")

Lines = dict[tuple[str, ...], int]


def looks_like_openapi(text: str) -> bool:
    """Whether a YAML or JSON text is an OpenAPI 3 or Swagger 2 document: one with a top-level
    `openapi` or `swagger` key holding a version. Cheap enough to run on every .yml/.yaml/.json in
    a repo: a regex, plus a JSON parse only for JSON that has such a key somewhere."""
    text = text.removeprefix("﻿")
    if not text.lstrip().startswith("{"):
        return _YAML_VERSION_KEY.search(text) is not None  # top level = not indented
    if not _JSON_VERSION_KEY.search(text):
        return False
    try:
        return _is_spec(json.loads(text))  # JSON keys can sit at any indentation: check the level
    except ValueError:
        return False


def parse_openapi(text: str, path: str) -> ApiSpec:
    """The operations and schemas of a spec. ValueError if the text is not valid YAML or JSON, or
    not an OpenAPI or Swagger document."""
    doc, root = _load(text, path)
    if not _is_spec(doc):
        raise ValueError(
            f"{path}: not an OpenAPI or Swagger document (no top-level 'openapi' or 'swagger' version)"
        )
    lines = _key_lines(root)
    swagger2 = not _is_version(doc.get("openapi"))
    return ApiSpec(
        path=path,
        title=_text(_get(doc, "info", "title")),
        version=_text(_get(doc, "info", "version")),
        base_path=_base_path(doc, swagger2),
        operations=_operations(doc, lines),
        schemas=_schemas(doc, lines, swagger2),
    )


# ---- loading ----------------------------------------------------------------------------


class _SpecLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):  # type: ignore[misc]
    """Safe YAML (with libyaml when installed: ~8x faster) whose mapping keys stay the strings
    written, as in JSON: status 200 is "200" and a property named "on" doesn't become True."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[str, Any]:
        if not isinstance(node, yaml.MappingNode):
            return super().construct_mapping(node, deep=deep)  # raises PyYAML's usual error
        self.flatten_mapping(node)  # applies "<<: *anchor" merge keys
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found a key that is not a scalar", key_node.start_mark,
                )
            mapping[key_node.value] = self.construct_object(value_node, deep=deep)
        return mapping


def _load(text: str, path: str) -> tuple[Any, yaml.Node | None]:
    """The document's data, and its node tree, which knows the line of every key."""
    if text.lstrip("﻿ \t\r\n").startswith("{"):
        # JSON may put tabs between tokens, YAML may not; valid JSON has none inside strings.
        text = text.replace("\t", " ")
    loader = _SpecLoader(text)
    try:
        root = loader.get_single_node()
        return (loader.construct_document(root) if root is not None else None), root
    except yaml.YAMLError as err:
        raise ValueError(f"{path}: not valid YAML or JSON: {_describe(err)}") from err
    finally:
        loader.dispose()


def _describe(err: yaml.YAMLError) -> str:
    """One line, "mapping values are not allowed here (line 3, column 9)", not PyYAML's snippet."""
    problem, mark = getattr(err, "problem", None), getattr(err, "problem_mark", None)
    if problem and mark is not None:
        return f"{problem} (line {mark.line + 1}, column {mark.column + 1})"
    return " ".join(str(err).split())


def _key_lines(root: yaml.Node | None, depth: int = 3) -> Lines:
    """The 1-based line of each mapping key down to `depth` levels:
    ("paths", "/owners", "get") -> 98, ("components", "schemas", "Owner") -> 2040."""
    lines: Lines = {}
    pending: list[tuple[Any, tuple[str, ...]]] = [(root, ())]
    while pending:
        node, prefix = pending.pop()
        if not isinstance(node, yaml.MappingNode) or len(prefix) >= depth:
            continue
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                key = (*prefix, key_node.value)
                lines[key] = key_node.start_mark.line + 1
                pending.append((value_node, key))
    return lines


def _is_spec(doc: Any) -> bool:
    return isinstance(doc, dict) and any(_is_version(doc.get(key)) for key in ("openapi", "swagger"))


def _is_version(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and _VERSION.match(str(value)) is not None


# ---- the spec ---------------------------------------------------------------------------


def _base_path(doc: dict[str, Any], swagger2: bool) -> str:
    """The path part of the first server's URL ("/petclinic/api"), or Swagger 2's basePath."""
    if swagger2:
        url = _text(doc.get("basePath")) or ""
    else:
        servers = doc.get("servers")
        server = servers[0] if isinstance(servers, list) and servers else None
        server = server if isinstance(server, dict) else {}
        url = _fill_variables(_text(server.get("url")) or "", server.get("variables"))
    try:
        url_path = urlsplit(url).path  # works for "/api" as well as "http://host:8080/api"
    except ValueError:  # a malformed host, e.g. "http://[::1/api"
        return ""
    joined = join_paths(url_path)
    return "" if joined == "/" else joined


def _fill_variables(url: str, variables: Any) -> str:
    """Server variables replaced by their defaults: "https://{env}.example.com/{base}"."""
    if not isinstance(variables, dict):
        return url

    def default(match: re.Match[str]) -> str:
        value = _get(variables, match.group(1), "default")
        return match.group(0) if value is None else str(value)

    return _SERVER_VARIABLE.sub(default, url)


def _operations(doc: dict[str, Any], lines: Lines) -> list[ApiOperation]:
    operations = []
    paths = doc.get("paths")
    for path, item in paths.items() if isinstance(paths, dict) else []:
        item = None if path.startswith("x-") else _resolve(doc, item)
        if not isinstance(item, dict):
            continue  # an extension, or a $ref to another file
        for method, op in item.items():
            if method.lower() in HTTP_METHODS and isinstance(op, dict):
                line = lines.get(("paths", path, method))
                operations.append(_operation(doc, path, method, op, item.get("parameters"), line))
    return operations


def _operation(
    doc: dict[str, Any], path: str, method: str, op: dict[str, Any], path_params: Any,
    line: int | None,
) -> ApiOperation:
    body = _request_body(doc, op.get("requestBody"))
    parameters = []
    for param in _merged_parameters(doc, path_params, op.get("parameters")):
        if param.get("in") == "body":  # Swagger 2's request body
            body = body or _short(param.get("schema"))
        else:
            parameters.append(_parameter(param))
    return ApiOperation(
        method=method.upper(),
        path=path,
        operation_id=_text(op.get("operationId")),
        summary=_text(op.get("summary")),
        description=_text(op.get("description")),
        tags=_strings(op.get("tags")),
        parameters=parameters,
        request_body=body,
        responses=_responses(doc, op.get("responses")),
        line=line,
    )


def _merged_parameters(doc: dict[str, Any], *levels: Any) -> list[dict[str, Any]]:
    """Path-level then operation-level parameters, $refs resolved. An operation's parameter
    replaces the path's one with the same name and location, in place."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for level in levels:
        for raw in level if isinstance(level, list) else []:
            param = _resolve(doc, raw)
            if isinstance(param, dict) and param.get("name") is not None:
                merged[(str(param["name"]), str(param.get("in")))] = param
    return list(merged.values())


def _parameter(param: dict[str, Any]) -> ApiParameter:
    location = str(param.get("in") or "")
    if "schema" in param:
        schema = _short(param["schema"])
    elif "content" in param:
        schema = _short(_media_schema(param["content"]))
    else:  # Swagger 2: type and items sit on the parameter itself
        schema = _short(param) if "type" in param else None
    return ApiParameter(
        name=str(param["name"]),
        location=location,
        required=location == "path" or param.get("required") is True,  # path ones always are
        schema=schema,
    )


def _request_body(doc: dict[str, Any], body: Any) -> str | None:
    body = _resolve(doc, body)
    return _short(_media_schema(body.get("content"))) if isinstance(body, dict) else None


def _responses(doc: dict[str, Any], responses: Any) -> dict[str, str]:
    """Status -> the short form of the response's schema, or its description when it has none."""
    result: dict[str, str] = {}
    for status, raw in responses.items() if isinstance(responses, dict) else []:
        if status.startswith("x-"):
            continue
        response = _resolve(doc, raw)
        if not isinstance(response, dict):  # a $ref to another file: its name is all we know
            ref = _ref(raw)
            result[status] = _ref_name(ref) if ref else ""
            continue
        if "content" in response:
            schema = _media_schema(response["content"])
        else:
            schema = response.get("schema")  # Swagger 2
        result[status] = _short(schema) or _text(response.get("description")) or ""
    return result


def _media_schema(content: Any) -> Any:
    """The schema of the application/json content if there is one, else of the first type."""
    if not isinstance(content, dict) or not content:
        return None
    media = next(
        (m for kind, m in content.items() if kind.split(";")[0].strip().lower() == "application/json"),
        next(iter(content.values())),
    )
    return media.get("schema") if isinstance(media, dict) else None


def _schemas(doc: dict[str, Any], lines: Lines, swagger2: bool) -> list[ApiSchema]:
    prefix = ("definitions",) if swagger2 else ("components", "schemas")
    found = _get(doc, *prefix)
    schemas = []
    for name, schema in found.items() if isinstance(found, dict) else []:
        properties: dict[str, str] = {}
        required: list[str] = []
        all_of: list[str] = []
        _collect(schema, properties, required, all_of)
        schemas.append(
            ApiSchema(
                name=name,
                line=lines.get((*prefix, name)),
                properties=properties,
                required=required,
                all_of=all_of,
                text=_dump({name: schema}),
            )
        )
    return schemas


def _collect(schema: Any, properties: dict[str, str], required: list[str], all_of: list[str]) -> None:
    """A schema's properties and required names, its inline allOf parts' too. The $ref parts of
    allOf go to all_of: their properties belong to those schemas."""
    if not isinstance(schema, dict):
        return
    own = schema.get("properties")
    for name, prop in own.items() if isinstance(own, dict) else []:
        properties[name] = _short(prop) or "any"
    for name in _strings(schema.get("required")):
        if name not in required:
            required.append(name)
    parts = schema.get("allOf")
    for part in parts if isinstance(parts, list) else []:
        ref = _ref(part)
        if ref is not None:
            all_of.append(_ref_name(ref))
        else:
            _collect(part, properties, required, all_of)


# ---- schemas in short ---------------------------------------------------------------------


def _short(schema: Any) -> str | None:
    """A schema in a few characters: "Owner" (a $ref), "array<Pet>", "integer", "object",
    "map<Visit>" (an object of additionalProperties), "allOf<OwnerFields,object>". None if there is
    no schema."""
    if not isinstance(schema, dict):
        return None
    ref = _ref(schema)
    if ref is not None:
        return _ref_name(ref)
    for keyword in ("allOf", "oneOf", "anyOf"):
        parts = schema.get(keyword)
        if isinstance(parts, list) and parts:
            return f"{keyword}<{','.join(_short(part) or 'any' for part in parts)}>"
    kind = schema.get("type")
    if isinstance(kind, list):  # OpenAPI 3.1: [string, "null"]
        kind = "|".join(str(k) for k in kind if k != "null") or "null"
    if kind == "array" or (kind is None and "items" in schema):
        items = _short(schema.get("items"))
        return f"array<{items}>" if items else "array"
    if kind == "object" or (kind is None and ("properties" in schema or "additionalProperties" in schema)):
        values = schema.get("additionalProperties")
        if isinstance(values, dict) and values and not schema.get("properties"):
            return f"map<{_short(values)}>"
        return "object"
    return "any" if kind is None else str(kind)


def _ref(obj: Any) -> str | None:
    ref = obj.get("$ref") if isinstance(obj, dict) else None
    return ref if isinstance(ref, str) else None


def _ref_name(ref: str) -> str:
    """'#/components/schemas/Owner' -> 'Owner'; a whole file, 'schemas/Owner.yaml' -> 'Owner'."""
    file, _, pointer = ref.partition("#")
    if pointer.strip("/"):
        name = pointer.rstrip("/").rsplit("/", 1)[-1]
    else:
        name = re.sub(r"\.(ya?ml|json)$", "", file.rstrip("/").rsplit("/", 1)[-1])
    return unquote(name).replace("~1", "/").replace("~0", "~")


def _resolve(doc: dict[str, Any], obj: Any) -> Any:
    """obj, or what its local $ref ("#/components/parameters/Id") points to. None when the $ref
    can't be followed: another file (no I/O here), a missing target, a cycle."""
    for _ in range(32):  # a longer chain of $refs is a cycle
        ref = _ref(obj)
        if ref is None:
            return obj
        if not ref.startswith("#"):
            return None
        obj = doc
        for part in ref[1:].split("/")[1:]:
            part = unquote(part).replace("~1", "/").replace("~0", "~")
            if isinstance(obj, dict):
                obj = obj.get(part)
            elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
                obj = obj[int(part)]
            else:
                obj = None
    return None


class _Dumper(yaml.SafeDumper):
    """Writes objects shared through YAML aliases out in full rather than as &id001 anchors."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True)


# ---- small helpers ---------------------------------------------------------------------


def _get(obj: Any, *keys: str) -> Any:
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def _text(value: Any) -> str | None:
    return None if value is None else str(value).strip()


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []
