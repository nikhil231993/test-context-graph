"""`tcg`: index a repository, then ask the index for the test context of a symbol, search it,
show a node, find tests, or describe the index.

Problems are one line on stderr and exit code 1 (2 for a usage error), never a traceback;
TCG_DEBUG=1 shows the traceback of an unexpected error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .context import CHUNK_KINDS, ContextEngine, ContextError, embedder_for_model, render_markdown
from .contract import ContractContext, render_call_markdown, render_calls_markdown
from .embeddings import Embedder, default_embedder
from .store import IndexStore

DEFAULT_INDEX = ".tcg"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ContextError as exc:
        return _fail(str(exc))
    except BrokenPipeError:  # `tcg context X | head`: the reader left, nothing to report
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        return _fail("interrupted", 130)
    except Exception as exc:  # an unexpected error: one line unless debugging
        if os.environ.get("TCG_DEBUG"):
            raise
        return _fail(f"{type(exc).__name__}: {exc} (TCG_DEBUG=1 shows the traceback)")


def build_parser() -> argparse.ArgumentParser:
    index = argparse.ArgumentParser(add_help=False)
    index.add_argument(
        "--index", metavar="DIR", help=f"the index folder (default: $TCG_INDEX or {DEFAULT_INDEX})"
    )
    as_json = argparse.ArgumentParser(add_help=False)
    as_json.add_argument("--json", action="store_true", help="print JSON instead of text")

    parser = argparse.ArgumentParser(
        prog="tcg",
        description="A context graph of a Java Spring codebase, for writing tests.",
        epilog="Start with `tcg index <repo>`, then `tcg context <Type#method>`.",
    )
    parser.add_argument("--version", action="version", version=f"tcg {__version__}")
    parser.add_argument("--index", dest="global_index", metavar="DIR", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(
        name: str, handler: Callable[[argparse.Namespace], int], about: str, *parents: Any
    ) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, parents=list(parents), help=about, description=about)
        sub.set_defaults(handler=handler)
        return sub

    sub = command("index", cmd_index, "index a repository (embeds only chunks that changed)", index)
    sub.add_argument("repo", help="the repository folder")
    sub.add_argument("--full", action="store_true", help="embed every chunk again")
    sub.add_argument(
        "--provider-spec",
        action="append",
        default=[],
        metavar="FILE",
        help="an OpenAPI spec of another system this code calls (repeatable); kept in the index",
    )
    sub.add_argument(
        "--embedder",
        choices=("fastembed", "hash"),
        help="fastembed (local model) or hash (no model, for tests); default: $TCG_EMBEDDER, "
        "else the index's model, else fastembed",
    )

    about = "everything needed to write a test for a symbol"
    sub = command("context", cmd_context, about, index, as_json)
    sub.add_argument(
        "symbol", help="Type#method, Type.method(String), a type, a node id, GET /path, a file"
    )
    sub.add_argument(
        "--max-tests", type=int, default=3, metavar="N", help="existing test classes shown (3)"
    )
    sub.add_argument(
        "--max-docs",
        type=int,
        default=3,
        metavar="N",
        help="related doc sections (3); 0 skips loading the embedding model",
    )

    about = "search code, endpoints, schemas and docs by meaning"
    sub = command("search", cmd_search, about, index, as_json)
    sub.add_argument("query")
    sub.add_argument(
        "--kind",
        action="append",
        dest="kinds",
        choices=CHUNK_KINDS,
        help="only this kind (repeatable)",
    )
    sub.add_argument("--limit", type=int, default=10, metavar="N", help="hits shown (10)")

    about = "existing tests of a symbol, and why each is related"
    command("tests", cmd_tests, about, index, as_json).add_argument("symbol")
    about = "a node: its attributes, edges and text"
    command("show", cmd_show, about, index, as_json).add_argument("symbol")
    command("stats", cmd_stats, "what is indexed, when and with which model", index, as_json)
    about = "outbound HTTP calls to other services, by provider (for contract tests)"
    sub = command("calls", cmd_calls, about, index, as_json)
    sub.add_argument("symbol", nargs="?", help="only the calls this method or class makes")
    about = "everything needed to write a Specmatic consumer test for one outbound call"
    sub = command("contract", cmd_contract, about, index, as_json)
    sub.add_argument("call", help='an id from `tcg calls`, "POST /orders", or a method')
    return parser


# ---- commands --------------------------------------------------------------------------------


def cmd_index(args: argparse.Namespace) -> int:
    from .indexer import index_repo  # the parsers: only this command needs them

    repo = Path(args.repo)
    if not repo.is_dir():
        raise ContextError(f"{repo} is not a folder")
    index_dir = _index_dir(args)
    embedder = _index_embedder(args.embedder, index_dir)
    print(f"indexing {repo} into {index_dir} with {embedder.model_name}", file=sys.stderr)
    progress = _Progress()
    result = index_repo(repo, index_dir, embedder, full=args.full, progress=progress,
                        provider_specs=args.provider_spec)
    progress.finish()
    files, counts = result.files, result.counts
    again = "; all embedded again: first run, --full or a new model"
    again = again if result.reembedded_all else ""
    print("\n".join([
        f"indexed {repo.resolve().name} into {index_dir} in {result.seconds:.1f} s "
        f"({embedder.model_name})",
        f"files:    {counts.get('files', 0)} ({len(files.added)} added, "
        f"{len(files.changed)} changed, {len(files.removed)} removed)",
        f"chunks:   {counts.get('chunks', 0)} ({result.embedded} embedded, "
        f"{result.deleted} deleted{again})",
        f"nodes:    {counts.get('nodes', 0)} ({_by_kind(counts, 'nodes')})",
        f"edges:    {counts.get('edges', 0)} ({_by_kind(counts, 'edges')})",
        f"warnings: {len(result.warnings)}",
    ]))  # fmt: skip
    for warning in result.warnings[:20]:
        print(f"warning: {warning}", file=sys.stderr)
    if len(result.warnings) > 20:
        print(f"... {len(result.warnings) - 20} more warnings", file=sys.stderr)
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    engine = _engine(args)
    context = engine.test_context(args.symbol, max_tests=args.max_tests, max_docs=args.max_docs)
    _print(context, args.json, render_markdown)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    hits = _engine(args).search(args.query, kinds=args.kinds, limit=args.limit)
    _print(hits, args.json, _search_text)
    return 0


def cmd_tests(args: argparse.Namespace) -> int:
    _print(_engine(args).find_tests(args.symbol), args.json, _tests_text)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    _print(_engine(args).get_node(args.symbol), args.json, _show_text)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    _print(_engine(args).stats(), args.json, _stats_text)
    return 0


def cmd_calls(args: argparse.Namespace) -> int:
    _print(ContractContext(_engine(args)).outbound_calls(args.symbol), args.json, render_calls_markdown)
    return 0


def cmd_contract(args: argparse.Namespace) -> int:
    _print(ContractContext(_engine(args)).call_context(args.call), args.json, render_call_markdown)
    return 0


# ---- helpers ---------------------------------------------------------------------------------


def _index_dir(args: argparse.Namespace) -> Path:
    return Path(args.index or args.global_index or os.environ.get("TCG_INDEX") or DEFAULT_INDEX)


def _engine(args: argparse.Namespace) -> ContextEngine:
    return ContextEngine(_index_dir(args))


def _index_embedder(choice: str | None, index_dir: Path) -> Embedder:
    """--embedder, else $TCG_EMBEDDER, else the model the index was built with (so a re-run
    stays incremental), else the local model."""
    if choice or os.environ.get("TCG_EMBEDDER"):
        return default_embedder(choice)
    manifest = IndexStore(index_dir).read_manifest()
    return embedder_for_model(manifest.embedding_model if manifest else None)


class _Progress:
    """Embedding progress on stderr: one updating line on a terminal, a line per batch else."""

    def __init__(self) -> None:
        self.tty = sys.stderr.isatty()
        self.shown = False

    def __call__(self, done: int, total: int) -> None:
        message = f"embedding chunks: {done}/{total}"
        if self.tty:
            print(f"\r{message}", end="", file=sys.stderr, flush=True)
        else:
            print(message, file=sys.stderr, flush=True)
        self.shown = True

    def finish(self) -> None:
        if self.tty and self.shown:
            print(file=sys.stderr)


def _print(data: Any, as_json: bool, render: Callable[[Any], str]) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) if as_json else render(data)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _fail(message: str, code: int = 1) -> int:
    print(f"tcg: error: {message}", file=sys.stderr)
    return code


def _by_kind(counts: dict[str, int], prefix: str) -> str:
    """"nodes.type": 12, "nodes.method": 26 -> "method 26, type 12"."""
    items = sorted((k.split(".", 1)[1], n) for k, n in counts.items() if k.startswith(prefix + "."))
    return ", ".join(f"{kind} {n}" for kind, n in items) or "none"


def _lines(lines: list[int | None] | None) -> str:
    start, end = (lines or [None, None])[:2]
    if start is None:
        return ""
    return f":{start}" + (f"-{end}" if end and end != start else "")


def _profiles(profiles: list[str]) -> str:
    return f" [active profiles: {', '.join(profiles)}]" if profiles else ""


# ---- text output -----------------------------------------------------------------------------


def _search_text(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "no results\n"
    out = []
    for hit in hits:
        where = f"{hit['path'] or ''}{_lines(hit['lines'])}"
        out.append(f"{hit['score']:.3f}  {hit['kind']:<8}  {hit['name']}  {where}")
        out.append(f"       {hit['snippet']}")
        out.append(f"       id: {hit['id']}")
    return "\n".join(out) + "\n"


def _tests_text(result: dict[str, Any]) -> str:
    target = result["target"]
    where = f"{target['path'] or ''}{_lines(target['lines'])}"
    out = [f"Tests of {target['label']} ({target['kind']}, {where})"]
    if result["candidates"]:
        out.append("also matches: " + ", ".join(c["label"] for c in result["candidates"]))
    for test in result["tests"]:
        abstract = " (abstract)" if test["is_abstract"] else ""
        out += ["", f"{test['name']}{abstract}{_profiles(test['active_profiles'])}  {test['path']}"]
        out += [f"  why: {why}" for why in test["why"]]
        for sub in test["subclasses"]:
            out.append(f"  subclass: {sub['name']}{_profiles(sub['active_profiles'])}")
        for method in test["methods"]:
            out.append(f"  - {method['label']}{_lines(method['lines'])}  ({method['why']})")
    if result["notes"]:
        out += [""] + [f"note: {note}" for note in result["notes"]]
    return "\n".join(out) + "\n"


def _show_text(info: dict[str, Any]) -> str:
    node = info["node"]
    out = [f"{node['kind']} {info['label']}", f"id:   {node['id']}"]
    if node.get("path"):
        out.append(f"path: {node['path']}{_lines([node.get('start_line'), node.get('end_line')])}")
    for key, value in node["attrs"].items():
        if value not in (None, [], {}, "", False):
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            out.append(f"{key}: {text if len(text) <= 300 else text[:297] + '...'}")
    for side in ("out", "in"):
        counts = ", ".join(f"{kind} {n}" for kind, n in info["edges"][side].items())
        out.append(f"edges {side}: {counts or 'none'}")
    if info["candidates"]:
        out.append("also matches: " + ", ".join(c["label"] for c in info["candidates"]))
    if info["text"]:
        out += ["", info["text"]]
    return "\n".join(out) + "\n"


def _stats_text(stats: dict[str, Any]) -> str:
    counts = stats["counts"]
    commit = (stats.get("repo_commit") or "")[:12]
    model = f"{stats.get('embedding_model') or '?'} ({stats.get('embedding_dim') or '?'} dims)"
    return "\n".join([
        f"index:   {stats['index_dir']}",
        f"repo:    {stats.get('repo_name') or '?'}" + (f" @ {commit}" if commit else ""),
        f"indexed: {stats.get('generated_at') or '?'} ({stats['age']})",
        f"model:   {model}",
        f"files:   {counts.get('files', 0)} ({_by_kind(counts, 'files')})",
        f"nodes:   {counts.get('nodes', 0)} ({_by_kind(counts, 'nodes')})",
        f"edges:   {counts.get('edges', 0)} ({_by_kind(counts, 'edges')})",
        f"chunks:  {counts.get('chunks', 0)}",
    ]) + "\n"  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
