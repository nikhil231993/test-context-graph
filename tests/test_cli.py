"""The `tcg` command: main(argv) on a copy of the bookshop fixture, indexed once per module with
`--embedder hash` (no model, no network)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from test_context_graph import cli
from test_context_graph.cli import main
from test_context_graph.context import ContextEngine

BOOKSHOP = Path(__file__).parent / "fixtures" / "bookshop"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("TCG_INDEX", "TCG_EMBEDDER", "TCG_DEBUG"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Index the bookshop through the CLI, keeping what it printed."""
    root = tmp_path_factory.mktemp("cli")
    repo = Path(shutil.copytree(BOOKSHOP, root / "bookshop"))
    index = root / "index"
    with pytest.MonkeyPatch.context() as mp:
        captured: dict = {}
        mp.setattr(cli.sys, "stdout", _Capture(captured, "out"))
        mp.setattr(cli.sys, "stderr", _Capture(captured, "err"))
        code = main(["index", str(repo), "--index", str(index), "--embedder", "hash"])
    out, err = captured.get("out", ""), captured.get("err", "")
    return {"repo": repo, "index": index, "code": code, "out": out, "err": err}


class _Capture:
    """A stand-in stream for a module-scoped fixture, where capsys isn't available."""

    def __init__(self, store: dict, key: str) -> None:
        self.store, self.key = store, key

    def write(self, text: str) -> int:
        self.store[self.key] = self.store.get(self.key, "") + text
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def run(capsys, *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


# ---- index ---------------------------------------------------------------------------------


def test_index_prints_a_summary_and_progress(built):
    assert built["code"] == 0
    out = built["out"]
    assert out.startswith(f"indexed bookshop into {built['index']} in ")
    assert "files:    12 (12 added, 0 changed, 0 removed)" in out
    assert "chunks:   37 (37 embedded, 0 deleted; all embedded again" in out
    assert "nodes:    56 (doc 3, endpoint 2, file 12, method 26, schema 1, type 12)" in out
    assert "warnings: 0" in out
    assert "with hash-256" in built["err"] and "embedding chunks: 37/37" in built["err"]


def test_index_again_reuses_the_index_model_and_embeds_nothing(built, capsys):
    code, out, err = run(capsys, "index", str(built["repo"]), "--index", str(built["index"]))
    assert code == 0 and "(hash-256)" in out
    assert "chunks:   37 (0 embedded, 0 deleted)" in out and "0 changed" in out


def test_index_needs_a_folder(tmp_path, capsys):
    missing, index = str(tmp_path / "missing"), str(tmp_path / "i")
    code, out, err = run(capsys, "index", missing, "--index", index, "--embedder", "hash")
    assert code == 1 and out == "" and "is not a folder" in err


def test_index_rejects_an_unknown_embedder(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TCG_EMBEDDER", "word2vec")
    code, _, err = run(capsys, "index", str(BOOKSHOP), "--index", str(tmp_path / "i"))
    assert code == 1 and "unknown embedder 'word2vec'" in err and "Traceback" not in err


# ---- queries -------------------------------------------------------------------------------


def test_context_prints_markdown(built, capsys):
    code, out, err = run(capsys, "context", "BookRestController#getBook", "--index", str(built["index"]))
    assert code == 0 and err == ""
    assert out.startswith("# Test context: BookRestController#getBook(Integer)\n")
    assert "## Dependencies to mock" in out and "## Related docs" in out


def test_context_as_json_with_limits(built, capsys):
    code, out, _ = run(capsys, "context", "findBook", "--json", "--max-tests", "1", "--max-docs", "0",
                       "--index", str(built["index"]))  # fmt: skip
    data = json.loads(out)
    assert code == 0 and data["target"]["label"] == "BookServiceImpl#findBook(int)"
    assert data["related_docs"] == [] and len(data["existing_tests"]) == 1
    assert data["candidates"][0]["label"] == "BookService#findBook(int)"


def test_search_text_and_json(built, capsys):
    index = str(built["index"])
    code, out, _ = run(capsys, "search", "find a book by id", "--index", index, "--limit", "2")
    assert code == 0 and out.count("\n       id: ") == 2
    code, out, _ = run(capsys, "search", "book", "--kind", "endpoint", "--kind", "schema", "--json",
                       "--index", str(built["index"]))  # fmt: skip
    hits = json.loads(out)
    assert code == 0 and hits and {h["kind"] for h in hits} <= {"endpoint", "schema"}


def test_tests_command(built, capsys):
    code, out, _ = run(capsys, "tests", "BookService", "--index", str(built["index"]))
    assert code == 0 and out.startswith("Tests of BookService (type,")
    assert "AbstractBookServiceTests (abstract)" in out
    assert "subclass: BookServiceJdbcTests [active profiles: jdbc]" in out
    code, out, _ = run(capsys, "tests", "BookService", "--json", "--index", str(built["index"]))
    assert json.loads(out)["tests"][0]["name"] == "AbstractBookServiceTests"


def test_show_command(built, capsys):
    code, out, _ = run(capsys, "show", "BookServiceImpl#find", "--index", str(built["index"]))
    assert code == 0 and out.startswith("method BookServiceImpl#find(Supplier)\n")
    assert "edges in: CONTAINS 1, CALLS 1" in out and "return lookup.get();" in out
    code, out, _ = run(capsys, "show", "GET /books", "--json", "--index", str(built["index"]))
    assert json.loads(out)["node"]["attrs"]["served_path"] == "/api/books"


def test_stats_command(built, capsys):
    code, out, _ = run(capsys, "stats", "--index", str(built["index"]))
    assert code == 0 and "repo:    bookshop" in out and "model:   hash-256 (256 dims)" in out
    code, out, _ = run(capsys, "stats", "--json", "--index", str(built["index"]))
    assert json.loads(out)["counts"]["chunks"] == 37


def test_index_folder_from_env_or_before_the_command(built, capsys, monkeypatch):
    code, out, _ = run(capsys, "--index", str(built["index"]), "stats")
    assert code == 0 and "bookshop" in out
    monkeypatch.setenv("TCG_INDEX", str(built["index"]))
    code, out, _ = run(capsys, "stats")
    assert code == 0 and f"index:   {built['index']}" in out


# ---- errors --------------------------------------------------------------------------------


def test_a_missing_index_is_one_line_on_stderr(tmp_path, capsys):
    code, out, err = run(capsys, "context", "X", "--index", str(tmp_path / "none"))
    assert code == 1 and out == ""
    assert err.startswith("tcg: error: no index at") and "run `tcg index <repo>`" in err
    assert "Traceback" not in err and err.count("\n") == 1


def test_an_unknown_symbol_fails_with_suggestions(built, capsys):
    code, _, err = run(capsys, "context", "getBok", "--index", str(built["index"]))
    assert code == 1 and "no symbol matches 'getBok'; did you mean" in err


def test_usage_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["context"])
    assert exc.value.code == 2 and "required: symbol" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["search", "x", "--kind", "class"])
    assert exc.value.code == 2


def test_an_unexpected_error_is_one_line_unless_debugging(built, capsys, monkeypatch):
    def boom(self):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ContextEngine, "stats", boom)
    code, _, err = run(capsys, "stats", "--index", str(built["index"]))
    assert code == 1
    assert err == "tcg: error: RuntimeError: disk on fire (TCG_DEBUG=1 shows the traceback)\n"
    monkeypatch.setenv("TCG_DEBUG", "1")
    with pytest.raises(RuntimeError):
        main(["stats", "--index", str(built["index"])])
