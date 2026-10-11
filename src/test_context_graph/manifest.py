"""What's in a repo and what changed since the last index: file hashes, chunk hashes and HEAD.

The indexer compares a fresh scan with the previous manifest (diff_files), and the new chunks
with the hashes already in the chunk table (plan_chunks), so a run on an unchanged repo embeds
nothing.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Chunk, FileEntry

# Build output, dependencies, IDE and tool folders. Any other hidden folder is skipped too.
SKIP_DIRS = frozenset(
    {".git", "target", "build", "out", "node_modules", ".idea", ".gradle", ".mvn", ".venv",
     "__pycache__", ".tcg"}
)
HEAD_BYTES = 4096  # how much of a YAML/JSON file classify() sees to tell a spec from config

_SPEC_SUFFIXES = (".yml", ".yaml", ".json")
_MARKDOWN_SUFFIXES = (".md", ".markdown")
_SUFFIXES = frozenset((".java", *_SPEC_SUFFIXES, *_MARKDOWN_SUFFIXES))


# ---- what a file is -----------------------------------------------------------------------


def classify(path: str, head: str) -> tuple[str, str] | None:
    """(kind, source_set) of a repo file, or None if it isn't indexed. `head` is the start of
    the file; only YAML and JSON files need it."""
    suffix = posixpath.splitext(path)[1].lower()
    if suffix == ".java":
        kind = "java"
    elif suffix in _MARKDOWN_SUFFIXES:
        kind = "markdown"
    elif suffix in _SPEC_SUFFIXES and looks_like_spec(head):
        kind = "openapi"
    else:
        return None
    return kind, source_set(path)


def source_set(path: str) -> str:
    """"test" under a src/test/ folder, "main" under src/main/, else "other" (any module depth)."""
    padded = "/" + path
    if "/src/test/" in padded:
        return "test"
    if "/src/main/" in padded:
        return "main"
    return "other"


# A top-level `openapi: 3.0.1` or `swagger: "2.0"` line. A version must follow the key, so an
# application.yml with a top-level `openapi:` block of custom properties doesn't count.
_YAML_SPEC_KEY = re.compile(r"""^["']?(?:openapi|swagger)["']?[ \t]*:[ \t]*["']?\d""", re.MULTILINE)


def looks_like_spec(head: str) -> bool:
    """Whether the start of a YAML or JSON file is an OpenAPI 3 or Swagger 2 document."""
    head = head.lstrip("﻿")
    return bool(_YAML_SPEC_KEY.search(head)) or _json_has_spec_key(head)


def _json_has_spec_key(head: str) -> bool:
    """A top-level "openapi"/"swagger" key with a version, found by tracking nesting depth, so
    e.g. a package.json depending on the "swagger" npm package doesn't count."""
    depth, i, n = 0, 0, len(head)
    while i < n:
        char = head[i]
        if char == '"':
            end = i + 1
            while end < n and head[end] != '"':
                end += 2 if head[end] == "\\" else 1
            if end >= n:
                return False  # the head ends inside a string
            if depth == 1 and head[i + 1 : end] in ("openapi", "swagger"):
                rest = head[end + 1 :].lstrip()
                if rest.startswith(":") and rest[1:].lstrip().lstrip('"')[:1].isdigit():
                    return True
            i = end + 1
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        i += 1
    return False


# ---- scanning -----------------------------------------------------------------------------


def scan_repo(root: Path, max_bytes: int = 2_000_000) -> dict[str, FileEntry]:
    """Every indexable file under `root` by repo-relative '/' path, sorted. Skips SKIP_DIRS,
    hidden folders, symlinks and files over `max_bytes` (generated or vendored, not context)."""
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    entries: dict[str, FileEntry] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if posixpath.splitext(name)[1].lower() not in _SUFFIXES:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            entry = _file_entry(full, rel, max_bytes)
            if entry is not None:
                entries[rel] = entry
    return dict(sorted(entries.items()))


def _file_entry(full: str, rel: str, max_bytes: int) -> FileEntry | None:
    try:
        if os.path.islink(full) or os.path.getsize(full) > max_bytes:
            return None
        with open(full, "rb") as f:
            data = f.read(max_bytes + 1)
    except OSError:  # unreadable, or gone since the walk listed it
        return None
    if len(data) > max_bytes:  # grew after the size check
        return None
    classified = classify(rel, data[:HEAD_BYTES].decode("utf-8", errors="replace"))
    if classified is None:
        return None
    kind, file_source_set = classified
    return FileEntry(
        path=rel,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        kind=kind,
        source_set=file_source_set,
    )


# ---- what changed -------------------------------------------------------------------------


@dataclass
class FileChanges:
    """Paths by what happened to them between two scans, each list sorted."""

    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed or self.removed)


def diff_files(old: dict[str, FileEntry], new: dict[str, FileEntry]) -> FileChanges:
    """A file is changed if its entry differs: content, or how it's classified."""
    changes = FileChanges(removed=sorted(set(old) - set(new)))
    for path in sorted(new):
        if path not in old:
            changes.added.append(path)
        elif old[path] != new[path]:
            changes.changed.append(path)
        else:
            changes.unchanged.append(path)
    return changes


def plan_chunks(old: dict[str, str], new: list[Chunk]) -> tuple[list[Chunk], list[str]]:
    """(chunks to embed, chunk ids to delete). `old` maps chunk id -> content_hash of what's
    stored; pass the table's own hashes (IndexStore.chunk_hashes) so a lost or reset table
    is refilled. A repeated id in `new` keeps its last chunk, as a dict of hashes would."""
    latest: dict[str, Chunk] = {}
    for chunk in new:
        latest[chunk.id] = chunk
    to_embed = [chunk for chunk in latest.values() if old.get(chunk.id) != chunk.content_hash]
    to_delete = sorted(set(old) - set(latest))
    return to_embed, to_delete


# ---- the commit ---------------------------------------------------------------------------

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # SHA-1, or SHA-256 repositories


def git_commit(root: Path) -> str | None:
    """HEAD's commit, read from the .git files (no git binary needed); None if `root` isn't in
    a git repository or HEAD has no commit yet. Like git, looks in parent folders too."""
    git_dir = _find_git_dir(Path(root))
    if git_dir is None:
        return None
    common_dir = _common_dir(git_dir)
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return _resolve(head, git_dir, common_dir)


def _find_git_dir(root: Path) -> Path | None:
    root = root.resolve()
    for folder in (root, *root.parents):
        dot_git = folder / ".git"
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():  # a linked worktree or a submodule: "gitdir: <path>"
            try:
                text = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if not text.startswith("gitdir:"):
                return None
            return (folder / text[len("gitdir:") :].strip()).resolve()
    return None


def _common_dir(git_dir: Path) -> Path:
    """Where branches and packed-refs live: the main .git folder, for a linked worktree."""
    try:
        text = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    return (git_dir / text).resolve()


def _resolve(value: str, git_dir: Path, common_dir: Path, depth: int = 0) -> str | None:
    """A HEAD or ref file's content -> commit: a sha as is, "ref: refs/..." followed."""
    if value.startswith("ref:"):
        ref = value[len("ref:") :].strip()
        if depth > 5 or not ref.startswith("refs/"):
            return None
        for base in (git_dir, common_dir):  # per-worktree refs first, then shared ones
            try:
                target = (base / ref).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            return _resolve(target, git_dir, common_dir, depth + 1)
        return _packed_ref(common_dir, ref)
    value = value.lower()
    return value if _SHA.fullmatch(value) else None


def _packed_ref(common_dir: Path, ref: str) -> str | None:
    try:
        lines = (common_dir / "packed-refs").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line or line[0] in "#^":  # header comment, or the peeled commit of a tag
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            sha = sha.lower()
            return sha if _SHA.fullmatch(sha) else None
    return None
