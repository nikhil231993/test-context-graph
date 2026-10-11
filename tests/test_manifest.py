import hashlib
import os
from pathlib import Path

import pytest

from test_context_graph.manifest import (
    SKIP_DIRS,
    FileChanges,
    classify,
    diff_files,
    git_commit,
    looks_like_spec,
    plan_chunks,
    scan_repo,
    source_set,
)
from test_context_graph.models import Chunk, FileEntry

SPEC_YAML = """\
openapi: 3.0.1
info:
  title: Pet Clinic
  version: 1.0.0
paths: {}
"""

APPLICATION_YML = """\
spring:
  profiles:
    active: h2,spring-data-jpa
springdoc:
  swagger-ui:
    path: /swagger-ui.html
  api-docs:
    path: /api-docs
openapi:
  dev-url: http://localhost:9966
"""

DOCKER_COMPOSE_YML = """\
services:
  swagger:
    image: swaggerapi/swagger-ui
  openapi:
    image: example/openapi-mock:3.0
"""


# ---- classify -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/main/java/org/example/Owner.java", ("java", "main")),
        ("src/test/java/org/example/OwnerTests.java", ("java", "test")),
        ("clinic-service/src/test/java/org/example/OwnerTests.java", ("java", "test")),
        ("clinic-service/src/main/java/org/example/Owner.java", ("java", "main")),
        ("tools/Generator.java", ("java", "other")),
        ("src/testing/Owner.java", ("java", "other")),
        ("README.md", ("markdown", "other")),
        ("docs/guide.markdown", ("markdown", "other")),
        ("src/test/postman/README.MD", ("markdown", "test")),
        ("notes.txt", None),
        ("pom.xml", None),
        ("src/main/resources/application.properties", None),
    ],
)
def test_classify_by_extension_and_source_set(path, expected):
    assert classify(path, "") == expected


def test_classify_finds_specs_by_their_content():
    assert classify("src/main/resources/openapi.yml", SPEC_YAML) == ("openapi", "main")
    assert classify("src/test/resources/api.yaml", SPEC_YAML) == ("openapi", "test")
    assert classify("api/petstore.json", '{"swagger": "2.0", "info": {}}') == ("openapi", "other")


def test_classify_ignores_config_yaml_and_json_that_are_not_specs():
    assert classify("src/main/resources/application.yml", APPLICATION_YML) is None
    assert classify("docker-compose.yml", DOCKER_COMPOSE_YML) is None
    package_json = '{"name": "ui", "dependencies": {"swagger": "0.7.5"}}'
    assert classify("frontend/package.json", package_json) is None
    assert classify("src/main/resources/openapi.yml", "") is None


@pytest.mark.parametrize(
    "head",
    [
        "openapi: 3.0.1\n",
        "openapi: '3.1.0'\ninfo: {}\n",
        'openapi: "3.0.0"\n',
        "swagger: '2.0'\n",
        '"openapi": "3.0.3"\n',
        "﻿openapi: 3.0.1\n",
        "# Pet Clinic API\n# (made up)\n---\nopenapi: 3.0.1\n",
        "info:\n  title: x\nopenapi: 3.0.1\n",
        '{"openapi":"3.1.0","info":{"title":"x"}}',
        '{\n  "info": {"title": "x", "openapi": "no"},\n  "openapi": "3.0.1"\n}',
        '{"swagger": "2.0"}',
    ],
)
def test_looks_like_spec(head):
    assert looks_like_spec(head)


@pytest.mark.parametrize(
    "head",
    [
        "",
        "info:\n  openapi: 3.0.1\n",  # not top-level
        "openapi:\n  dev-url: http://localhost\n",  # a config block, no version
        "openapi-version: 3.0.1\n",
        "# openapi: 3.0.1\n",
        '{"info": {"openapi": "3.0.1"}}',  # nested
        '[{"openapi": "3.0.1"}]',  # inside an array
        '{"openapi": {"enabled": true}}',
        '{"name": "openapi", "version": "3.0.1"}',  # a value, not a key
        '{"description": "has \\"openapi\\": 3 inside a string"}',
    ],
)
def test_looks_like_spec_rejects(head):
    assert not looks_like_spec(head)


def test_source_set():
    assert source_set("src/test/resources/openapi.yml") == "test"
    assert source_set("module/src/main/resources/openapi.yml") == "main"
    assert source_set("readme.md") == "other"


# ---- scan_repo ----------------------------------------------------------------------------


def write(root: Path, rel: str, content: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "clinic"
    write(root, "src/main/java/org/example/Owner.java", "class Owner {}\n")
    write(root, "src/test/java/org/example/OwnerTests.java", "class OwnerTests {}\n")
    write(root, "src/main/resources/openapi.yml", SPEC_YAML)
    write(root, "src/main/resources/application.yml", APPLICATION_YML)
    write(root, "README.md", "# Clinic\n")
    write(root, "docs/setup.markdown", "# Setup\n")
    write(root, "notes.txt", "not indexed\n")
    for skipped in sorted(SKIP_DIRS) + [".github", ".hidden"]:
        write(root, f"{skipped}/Skipped.java", "class Skipped {}\n")
        write(root, f"{skipped}/README.md", "# skipped\n")
    write(root, "module/target/generated/Gen.java", "class Gen {}\n")
    write(root, "big.md", "x" * 101)
    return root


def test_scan_repo_finds_indexable_files_and_skips_the_rest(repo: Path):
    entries = scan_repo(repo, max_bytes=100)
    assert list(entries) == [
        "README.md",
        "docs/setup.markdown",
        "src/main/java/org/example/Owner.java",
        "src/main/resources/openapi.yml",
        "src/test/java/org/example/OwnerTests.java",
    ]
    owner = entries["src/main/java/org/example/Owner.java"]
    content = (repo / owner.path).read_bytes()
    assert owner == FileEntry(
        path="src/main/java/org/example/Owner.java",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        kind="java",
        source_set="main",
    )
    assert entries["src/main/resources/openapi.yml"].kind == "openapi"
    assert entries["src/test/java/org/example/OwnerTests.java"].source_set == "test"


def test_scan_repo_keeps_files_up_to_max_bytes(repo: Path):
    assert "big.md" in scan_repo(repo, max_bytes=101)
    assert "big.md" not in scan_repo(repo, max_bytes=100)


def test_scan_repo_works_inside_a_hidden_folder(tmp_path: Path):
    root = tmp_path / ".demo" / "clinic"
    write(root, "src/main/java/Owner.java", "class Owner {}\n")
    assert list(scan_repo(root)) == ["src/main/java/Owner.java"]


def test_scan_repo_skips_symlinks(tmp_path: Path):
    root = tmp_path / "clinic"
    target = write(root, "README.md", "# Clinic\n")
    os.symlink(target, root / "LINK.md")
    os.symlink(tmp_path, root / "linked-dir")  # a loop back to the root's parent: not followed
    assert list(scan_repo(root)) == ["README.md"]


def test_scan_repo_reads_files_that_are_not_utf8(tmp_path: Path):
    root = tmp_path / "clinic"
    write(root, "src/main/java/Owner.java", "class Owner { String n = \"Müller\"; }".encode("latin-1"))
    assert scan_repo(root)["src/main/java/Owner.java"].kind == "java"


def test_scan_repo_needs_a_directory(tmp_path: Path):
    with pytest.raises(NotADirectoryError):
        scan_repo(tmp_path / "missing")


# ---- diff_files ---------------------------------------------------------------------------


def entry(path: str, sha: str, kind: str = "java") -> FileEntry:
    return FileEntry(path=path, sha256=sha, size=10, kind=kind, source_set="main")


def test_diff_files():
    old = {
        "b.java": entry("b.java", "1"),
        "a.java": entry("a.java", "1"),
        "gone.java": entry("gone.java", "1"),
        "spec.yml": entry("spec.yml", "1", kind="openapi"),
        "z.md": entry("z.md", "1", kind="markdown"),
    }
    new = {
        "z.md": entry("z.md", "1", kind="markdown"),
        "new.java": entry("new.java", "1"),
        "b.java": entry("b.java", "2"),
        "a.java": entry("a.java", "1"),
        "spec.yml": entry("spec.yml", "1", kind="markdown"),  # same bytes, classified differently
    }
    changes = diff_files(old, new)
    assert changes == FileChanges(
        added=["new.java"],
        changed=["b.java", "spec.yml"],
        removed=["gone.java"],
        unchanged=["a.java", "z.md"],
    )
    assert changes.has_changes


def test_diff_files_without_changes():
    files = {"a.java": entry("a.java", "1")}
    changes = diff_files(files, dict(files))
    assert changes == FileChanges(unchanged=["a.java"])
    assert not changes.has_changes
    assert diff_files({}, {}) == FileChanges()


# ---- plan_chunks --------------------------------------------------------------------------


def chunk(chunk_id: str, content_hash: str) -> Chunk:
    return Chunk(
        id=chunk_id,
        node_id=chunk_id.split("#part")[0],
        kind="method",
        text="text",
        embed_text="embed",
        content_hash=content_hash,
    )


def test_plan_chunks_embeds_new_and_changed_chunks_and_deletes_vanished_ones():
    old = {"same": "h1", "changed": "h1", "gone-b": "h1", "gone-a": "h1"}
    new = [chunk("new", "h1"), chunk("same", "h1"), chunk("changed", "h2")]
    to_embed, to_delete = plan_chunks(old, new)
    assert [c.id for c in to_embed] == ["new", "changed"]
    assert to_delete == ["gone-a", "gone-b"]


def test_plan_chunks_on_an_empty_store_embeds_everything():
    new = [chunk("a", "h1"), chunk("b", "h1")]
    assert plan_chunks({}, new) == (new, [])
    assert plan_chunks({"a": "h1"}, []) == ([], ["a"])


def test_plan_chunks_keeps_the_last_of_a_repeated_id():
    first, last = chunk("dup", "h1"), chunk("dup", "h2")
    assert plan_chunks({}, [first, last]) == ([last], [])
    assert plan_chunks({"dup": "h2"}, [first, last]) == ([], [])


# ---- git_commit ---------------------------------------------------------------------------

SHA_A = "a" * 40
SHA_B = "0123456789abcdef0123456789abcdef01234567"
SHA_C = "c" * 40


def fake_git(
    root: Path, head: str, refs: dict[str, str] | None = None, packed: str | None = None
) -> Path:
    git = root / ".git"
    write(git, "HEAD", head)
    for ref, sha in (refs or {}).items():
        write(git, ref, sha + "\n")
    if packed is not None:
        write(git, "packed-refs", packed)
    return git


def test_git_commit_follows_head_to_a_loose_branch_ref(tmp_path: Path):
    fake_git(tmp_path, "ref: refs/heads/main\n", refs={"refs/heads/main": SHA_A})
    assert git_commit(tmp_path) == SHA_A


def test_git_commit_reads_packed_refs(tmp_path: Path):
    packed = (
        "# pack-refs with: peeled fully-peeled sorted \n"
        f"{SHA_C} refs/heads/feature\n"
        f"{SHA_B} refs/heads/main\n"
        f"{SHA_A} refs/tags/v1\n"
        f"^{SHA_C}\n"
    )
    fake_git(tmp_path, "ref: refs/heads/main\n", packed=packed)
    assert git_commit(tmp_path) == SHA_B


def test_git_commit_prefers_a_loose_ref_over_packed_refs(tmp_path: Path):
    fake_git(
        tmp_path,
        "ref: refs/heads/main\n",
        refs={"refs/heads/main": SHA_A},
        packed=f"{SHA_B} refs/heads/main\n",
    )
    assert git_commit(tmp_path) == SHA_A


def test_git_commit_with_a_detached_head(tmp_path: Path):
    fake_git(tmp_path, SHA_B.upper() + "\n")
    assert git_commit(tmp_path) == SHA_B


def test_git_commit_from_a_subfolder_of_the_repo(tmp_path: Path):
    fake_git(tmp_path, "ref: refs/heads/main\n", refs={"refs/heads/main": SHA_A})
    sub = tmp_path / "services" / "clinic"
    sub.mkdir(parents=True)
    assert git_commit(sub) == SHA_A


def test_git_commit_in_a_linked_worktree(tmp_path: Path):
    main = tmp_path / "main"
    git = fake_git(main, "ref: refs/heads/main\n", refs={"refs/heads/feature": SHA_C})
    worktree_git = git / "worktrees" / "feature"
    write(worktree_git, "HEAD", "ref: refs/heads/feature\n")
    write(worktree_git, "commondir", "../..\n")
    worktree = tmp_path / "feature"
    write(worktree, ".git", f"gitdir: {worktree_git}\n")
    assert git_commit(worktree) == SHA_C


def test_git_commit_with_a_relative_gitdir_file(tmp_path: Path):
    fake_git(tmp_path / "store", SHA_A + "\n")
    write(tmp_path / "work", ".git", "gitdir: ../store/.git\n")
    assert git_commit(tmp_path / "work") == SHA_A


def test_git_commit_is_none_outside_a_repo_or_before_the_first_commit(tmp_path: Path):
    assert git_commit(tmp_path) is None
    fake_git(tmp_path, "ref: refs/heads/main\n")  # unborn branch: no ref file, no packed-refs
    assert git_commit(tmp_path) is None


@pytest.mark.parametrize("head", ["", "not a sha\n", "ref: ../../etc/passwd\n", "abc123\n"])
def test_git_commit_is_none_for_a_head_it_cannot_read(tmp_path: Path, head: str):
    fake_git(tmp_path, head)
    assert git_commit(tmp_path) is None
