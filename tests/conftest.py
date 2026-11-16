"""测试夹具。

关键点：环境变量必须在**导入 app 之前**设置好，因为 app.main 在模块级就
实例化了一次 Settings 并建目录。用 pytest 的 fixture 设环境变量来不及。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="livingdoc-test-"))
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))

os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
os.environ["GITHUB_TOKEN"] = ""
os.environ["WORK_DIR"] = str(_TMP / "work")
os.environ["REPO_CACHE_DIR"] = str(_TMP / "repos")
os.environ["BROKER_URL"] = "memory://"
os.environ["RESULT_BACKEND"] = "cache+memory://"
# 每个测试进程一个独立库文件；不要让测试写进仓库的 var/livingdoc.sqlite
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'livingdoc.sqlite').as_posix()}"
os.environ["PIPELINE_ENABLED"] = "false"  # 默认不自动入队，需要入队的用例自己 monkeypatch
# 投递默认开 local_wiki；必须指到临时目录，否则测试会写进仓库的 var/kb
os.environ["KB_DIR"] = str(_TMP / "kb")

TEST_SECRET = "test-secret"


@pytest.fixture(autouse=True)
def _clean_dedupe():
    from app.dedupe import delivery_cache

    delivery_cache.clear()
    yield
    delivery_cache.clear()


@pytest.fixture
def push_payload() -> dict:
    """一份最小但结构真实的 GitHub push payload（字段名与官方一致）。"""
    return {
        "ref": "refs/heads/main",
        "before": "a" * 40,
        "after": "b" * 40,
        "deleted": False,
        "repository": {
            "full_name": "octocat/Hello-World",
            "clone_url": "https://github.com/octocat/Hello-World.git",
            "private": False,
        },
        "sender": {"login": "octocat"},
    }


def dump(obj: dict) -> bytes:
    """必须和发送时用的是同一份字节 —— 重新序列化会改变摘要。"""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")
