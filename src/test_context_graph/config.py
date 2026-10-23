"""Spring config files -> ConfigProperties. Pure: no file, network or clock access.

Reads application*.properties / application*.yml (and bootstrap*), so that a base URL written as
"${order.api.url}" in code can be traced to its value per profile. Values of keys that look
secret are masked: an index can be shared, and a password is never test context.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

import yaml

from .models import ConfigProperty

_CONFIG_FILE = re.compile(r"^(?:application|bootstrap)(?:-([\w.-]+))?\.(?:properties|ya?ml)$", re.I)
_SECRET = re.compile(r"(password|passwd|secret|token|credential|api[-_]?key|private[-_]?key)", re.I)
_BARE_PLACEHOLDER = re.compile(r"\$\{[^}:]*\}")  # ${NAME}: names a variable, reveals nothing
_PROFILE_KEYS = ("spring.config.activate.on-profile", "spring.profiles")
MASK = "***"


def is_config_file(path: str) -> bool:
    return _CONFIG_FILE.match(posixpath.basename(path)) is not None


def file_profile(path: str) -> str | None:
    """"dev" for application-dev.yml; None for application.yml."""
    match = _CONFIG_FILE.match(posixpath.basename(path))
    return match.group(1) if match else None


def parse_config(text: str, path: str) -> list[ConfigProperty]:
    """The properties of one config file, in file order. Raises ValueError on broken YAML."""
    if path.lower().endswith(".properties"):
        found = _properties(text, path, file_profile(path))
    else:
        found = _yaml(text, path)
    return [_masked(p) for p in found]


def _masked(prop: ConfigProperty) -> ConfigProperty:
    if _SECRET.search(prop.key.rsplit(".", 1)[-1]) and not _BARE_PLACEHOLDER.fullmatch(prop.value):
        prop.value = MASK
    return prop


# ---- .properties --------------------------------------------------------------------------


def _properties(text: str, path: str, profile: str | None) -> list[ConfigProperty]:
    """java.util.Properties syntax, simplified: '=', ':' or a space separates; '\\' continues."""
    found: list[ConfigProperty] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        start, logical = i + 1, lines[i].lstrip()
        while logical.endswith("\\") and not logical.endswith("\\\\") and i + 1 < len(lines):
            i += 1
            logical = logical[:-1] + lines[i].lstrip()
        i += 1
        if not logical or logical[0] in "#!":
            continue
        match = re.match(r"((?:\\.|[^=:\s\\])+)\s*[=:\s]\s*(.*)$", logical)
        key, value = (match.group(1), match.group(2)) if match else (logical, "")
        found.append(ConfigProperty(path, _unescape(key), _unescape(value.rstrip()), start, profile))
    return found


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n"}.get(m.group(1), m.group(1)), text)


# ---- YAML ---------------------------------------------------------------------------------


def _yaml(text: str, path: str) -> list[ConfigProperty]:
    try:
        documents = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        raise ValueError(f"not valid YAML: {str(exc).splitlines()[0]}") from exc
    found: list[ConfigProperty] = []
    for root in documents:
        if root is None:
            continue
        flat: list[tuple[str, str, int]] = []
        _flatten(root, "", flat)
        profiles = {key: value for key, value, _ in flat if key in _PROFILE_KEYS}
        profile = next((profiles[k] for k in _PROFILE_KEYS if k in profiles), file_profile(path))
        found += [ConfigProperty(path, key, value, line, profile) for key, value, line in flat]
    return found


def _flatten(node: yaml.Node, prefix: str, out: list[tuple[str, str, int]]) -> None:
    """Spring's flattening: nested keys joined with '.', list items as key[0]."""
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = str(key_node.value)
            _flatten(value_node, f"{prefix}.{key}" if prefix else key, out)
    elif isinstance(node, yaml.SequenceNode):
        for n, item in enumerate(node.value):
            _flatten(item, f"{prefix}[{n}]", out)
    elif isinstance(node, yaml.ScalarNode) and prefix:
        out.append((prefix, _scalar(node.value), node.start_mark.line + 1))


def _scalar(value: Any) -> str:
    return "" if value is None else str(value)
