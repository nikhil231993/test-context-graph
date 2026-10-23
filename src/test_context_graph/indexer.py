"""Index a repository: scan it, parse it, build the graph and chunks, embed what changed, write it.

The graph is rebuilt in full on every run: parsing a hundred files takes well under a second.
Only chunks whose content changed are embedded again, because embedding is the slow part.
"""

from __future__ import annotations

import shutil
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .chunks import make_chunks
from .config import parse_config
from .docs import parse_markdown
from .embeddings import Embedder, default_embedder
from .graph import build_graph
from .java_parser import parse_java
from .manifest import FileChanges, diff_files, git_commit, plan_chunks, scan_repo
from .models import (
    ApiSpec, Chunk, ConfigProperty, DocSection, FileEntry, Graph, JavaFile, Manifest,
)  # fmt: skip
from .openapi import parse_openapi
from .store import IndexStore

EMBED_BATCH = 64
PROVIDER_SPECS = "provider-specs"  # in the index folder: other systems' specs this code calls


@dataclass
class IndexResult:
    files: FileChanges
    embedded: int  # chunks embedded in this run
    deleted: int  # chunks removed from the index
    counts: dict[str, int]
    reembedded_all: bool  # a first run, --full, or a new embedding model
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class Parsed:
    java_files: list[JavaFile] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # Java path -> text, for chunks
    specs: list[ApiSpec] = field(default_factory=list)
    docs: list[DocSection] = field(default_factory=list)
    config: list[ConfigProperty] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_files(root: Path, files: dict[str, FileEntry]) -> Parsed:
    """Parse every scanned file. A file that can't be parsed becomes a warning, not a failure."""
    parsed = Parsed()
    for path, entry in files.items():
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        if entry.kind == "java":
            java_file = parse_java(text, path)
            parsed.java_files.append(java_file)
            parsed.sources[path] = text
            parsed.warnings += [f"{path}: {error}" for error in java_file.errors]
        elif entry.kind == "openapi":
            try:
                parsed.specs.append(parse_openapi(text, path))
            except ValueError as exc:
                parsed.warnings.append(f"{path}: {exc}")
        elif entry.kind == "markdown":
            parsed.docs += parse_markdown(text, path)
        elif entry.kind == "config":
            try:
                parsed.config += parse_config(text, path)
            except ValueError as exc:
                parsed.warnings.append(f"{path}: {exc}")
    return parsed


def count(graph: Graph, chunks: list[Chunk], files: dict[str, FileEntry]) -> dict[str, int]:
    """Totals for the manifest and the CLI: files, nodes and edges by kind, chunks."""
    counts: Counter[str] = Counter()
    counts["files"] = len(files)
    counts.update(f"files.{entry.kind}" for entry in files.values())
    counts["nodes"] = len(graph.nodes)
    counts.update(f"nodes.{node.kind}" for node in graph.nodes)
    counts["edges"] = len(graph.edges)
    counts.update(f"edges.{edge.kind}" for edge in graph.edges)
    counts["chunks"] = len(chunks)
    return dict(sorted(counts.items()))


def index_repo(
    repo: str | Path,
    index_dir: str | Path,
    embedder: Embedder | None = None,
    full: bool = False,
    progress: Callable[[int, int], None] | None = None,
    provider_specs: Sequence[str | Path] = (),
) -> IndexResult:
    """Index `repo` into `index_dir`. A second run on an unchanged repo embeds nothing.

    provider_specs: OpenAPI files of other systems this code calls (found elsewhere, e.g. with
    ghes-openapi-mcp). They're copied into the index folder, so later runs keep them, and
    indexed as provider specs: outbound calls link to their operations (TARGETS)."""
    started = time.monotonic()
    root = Path(repo).resolve()
    store = IndexStore(index_dir)
    embedder = embedder or default_embedder()

    previous = store.read_manifest()
    files = scan_repo(root)
    changes = diff_files(previous.files if previous else {}, files)
    parsed = parse_files(root, files)
    parsed.specs += _provider_specs(Path(index_dir), provider_specs, parsed.warnings)
    graph = build_graph(parsed.java_files, parsed.specs, parsed.docs, parsed.config)
    chunks = make_chunks(graph, parsed.java_files, parsed.sources, parsed.specs, parsed.docs)

    # The table's own hashes, not the manifest's: they're right even after an interrupted run.
    reembed_all = full or previous is None or previous.embedding_model != embedder.model_name
    if reembed_all:
        store.reset_chunks()
    to_embed, to_delete = plan_chunks({} if reembed_all else store.chunk_hashes(), chunks)
    for start in range(0, len(to_embed), EMBED_BATCH):
        batch = to_embed[start : start + EMBED_BATCH]
        store.upsert_chunks(batch, embedder.embed_documents([chunk.embed_text for chunk in batch]))
        if progress:
            progress(start + len(batch), len(to_embed))
    store.delete_chunks(to_delete)

    # The manifest goes last: it marks the index as complete.
    commit = git_commit(root)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = count(graph, chunks, files)
    dim = previous.embedding_dim if previous and not reembed_all and not to_embed else embedder.dim
    store.write_graph(graph, meta={"repo_name": root.name, "repo_commit": commit, "generated_at": generated_at})
    store.write_manifest(
        Manifest(
            tool_version=__version__,
            generated_at=generated_at,
            repo_name=root.name,
            repo_commit=commit,
            embedding_model=embedder.model_name,
            embedding_dim=dim,
            files=files,
            chunks={chunk.id: chunk.content_hash for chunk in chunks},
            counts=counts,
        )
    )
    return IndexResult(
        files=changes,
        embedded=len(to_embed),
        deleted=len(to_delete),
        counts=counts,
        reembedded_all=reembed_all,
        warnings=parsed.warnings,
        seconds=round(time.monotonic() - started, 2),
    )


def _provider_specs(index_dir: Path, new: Sequence[str | Path], warnings: list[str]) -> list[ApiSpec]:
    """Copy new provider specs into the index folder, then parse every one kept there."""
    folder = index_dir / PROVIDER_SPECS
    for source in map(Path, new):
        if not source.is_file():
            raise FileNotFoundError(f"provider spec not found: {source}")
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, folder / source.name)
    specs = []
    for spec_file in sorted(folder.glob("*")) if folder.is_dir() else []:
        path = f"{PROVIDER_SPECS}/{spec_file.name}"
        try:
            spec = parse_openapi(spec_file.read_text(encoding="utf-8", errors="replace"), path)
        except ValueError as exc:
            warnings.append(f"{path}: {exc}")
            continue
        spec.role = "provider"
        specs.append(spec)
    return specs
