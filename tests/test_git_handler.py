"""git_handler 的单测：全部离线，用 git init 伪造仓库。

解析类代码（--name-status -z / --numstat -z）必须用真实 git 输出测，
靠 mock 毫无意义 —— 我们踩的坑都在字节格式上。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.git_handler import (
    FileChange,
    GitError,
    RepoRef,
    diff_commits,
    has_commit,
    is_zero_sha,
    read_blob,
    repo_path_for,
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        check=True,
        env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return proc.stdout.decode("utf-8", "replace").strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """构造两个 commit：修改 a.py、新增 b.py、删除 中文.py。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")

    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "中文.py").write_text("y = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c1")
    base = _git(root, "rev-parse", "HEAD")

    (root / "a.py").write_text("x = 1\nx2 = 2\n", encoding="utf-8")
    (root / "b.py").write_text("z = 3\n", encoding="utf-8")
    (root / "中文.py").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c2")
    head = _git(root, "rev-parse", "HEAD")
    return root, base, head


def test_diff_detects_add_modify_delete(repo: tuple[Path, str, str]) -> None:
    root, base, head = repo
    changes = diff_commits(root, base, head)
    by_path = {c.path: c for c in changes}

    assert by_path["a.py"].status == "M"
    assert by_path["b.py"].status == "A"
    assert by_path["b.py"].additions == 1
    # 中文路径必须原样返回，不能是 "\344\270\255.py"
    assert by_path["中文.py"].status == "D"
    assert by_path["中文.py"].is_deleted is True
    assert len(changes) == 3


def test_diff_same_commit_is_empty(repo: tuple[Path, str, str]) -> None:
    root, _, head = repo
    assert diff_commits(root, head, head) == []


def test_read_blob_returns_head_version(repo: tuple[Path, str, str]) -> None:
    root, base, head = repo
    assert "x2" in read_blob(root, head, "a.py")
    assert "x2" not in read_blob(root, base, "a.py")


def test_read_blob_missing_file_raises(repo: tuple[Path, str, str]) -> None:
    root, _, head = repo
    with pytest.raises(GitError):
        read_blob(root, head, "nope.py")


def test_has_commit(repo: tuple[Path, str, str]) -> None:
    root, base, _ = repo
    assert has_commit(root, base) is True
    assert has_commit(root, "f" * 40) is False
    assert has_commit(root, "0" * 40) is False


def test_diff_with_zero_base_raises(repo: tuple[Path, str, str]) -> None:
    root, _, head = repo
    with pytest.raises(GitError):
        diff_commits(root, "0" * 40, head)


def test_diff_respects_max_files(repo: tuple[Path, str, str]) -> None:
    root, base, head = repo
    assert len(diff_commits(root, base, head, max_files=2)) == 2


@pytest.mark.parametrize("sha", ["", None, "0" * 40, "0" * 64])
def test_is_zero_sha(sha: str | None) -> None:
    assert is_zero_sha(sha) is True


def test_repo_path_sanitizes() -> None:
    path = repo_path_for("owner/repo.name-1")
    assert path.name == "owner__repo.name-1"
    # 路径穿越字符必须被吃掉
    assert ".." not in repo_path_for("../../etc/passwd").name


def test_repo_ref_requires_full_name() -> None:
    with pytest.raises(GitError):
        RepoRef.from_webhook({"clone_url": "x"})
    ref = RepoRef.from_webhook({"full_name": "o/r", "clone_url": "https://e/r.git"})
    assert ref.clone_url == "https://e/r.git"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("fatal: Authentication failed for 'https://github.com/x/y.git/'", False),
        ("fatal: repository 'https://github.com/x/y.git/' not found", False),
        ("fatal: unable to access: Connection reset by peer", True),
        ("error: RPC failed; HTTP 502", True),
    ],
)
def test_error_classification(stderr: str, expected: bool) -> None:
    assert GitError("boom", stderr=stderr).retryable is expected


def test_file_change_defaults() -> None:
    change = FileChange(path="a.py", status="M")
    assert (change.additions, change.deletions) == (0, 0)
