"""任务层测试：三段式链路的接线。

用真实的 SQLite、真实的 tree-sitter、真实的文档校验，只把"外部世界"
（git 远端、大模型 HTTP）换成剧本。这样测的是接线本身，而不是又一次单元测试。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.tasks as tasks
from app.ai.client import ScriptedLLM
from app.config import Settings
from app.git_handler import FileChange
from app.store import documents, get_run, open_store, document_revisions

PATH = "app/services/user.py"

OLD_SOURCE = '''class UserService:
    def __init__(self, repo):
        self.repo = repo

    def load(self, uid):
        return self.repo.get(uid)
'''

NEW_SOURCE = '''class UserService:
    def __init__(self, repo):
        self.repo = repo

    def load(self, uid):
        if uid is None:
            raise ValueError("uid")
        return self.repo.get(uid)

    def delete(self, uid):
        self.repo.remove(uid)
'''

CLASS_PUML = """@startuml
class UserService {
  +load(uid)
  +delete(uid)
}
@enduml
"""
API_MD = """## app.services

### UserService.load(uid)

Returns the user record, or raises ValueError when the id is unknown.
The error path is deliberate: callers should handle a missing user the
same way whether the id never existed or the record was deleted.

### UserService.delete(uid)

Removes the user from the repository. It is intentionally idempotent:
deleting an id that is already gone is not treated as an error.
"""
SEQUENCE_PUML = """@startuml
actor Client
participant UserService
Client -> UserService: load(uid)
UserService --> Client: user
@enduml
"""

JOB = {
    "delivery_id": "delivery-1",
    "repo_full_name": "octocat/Hello-World",
    "clone_url": "https://github.com/octocat/Hello-World.git",
    "base_sha": "a" * 40,
    "head_sha": "b" * 40,
    "event": "push",
    "ref": "refs/heads/main",
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把任务模块的外部依赖全部换成本地替身。"""
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    store = open_store(f"sqlite:///{(tmp_path / 'tasks.sqlite').as_posix()}")
    settings = Settings(
        pipeline_enabled=True,
        work_dir=work,
        repo_cache_dir=tmp_path / "repos",
        database_url=f"sqlite:///{(tmp_path / 'tasks.sqlite').as_posix()}",
        llm_api_key="test-key",
        llm_model="scripted",
    )

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "get_store", lambda: store)
    monkeypatch.setattr(tasks, "ensure_repo", lambda ref: tmp_path / "repo")
    monkeypatch.setattr(tasks, "fetch_commits", lambda repo_dir, shas: [])
    monkeypatch.setattr(tasks, "diff_commits", lambda repo_dir, base, head: [FileChange(path=PATH, status="M", additions=2, deletions=0)])
    monkeypatch.setattr(tasks, "repo_size_mb", lambda repo_dir: 0.01)
    monkeypatch.setattr(tasks, "repo_path_for", lambda full_name: tmp_path / "repo")
    monkeypatch.setattr(tasks, "read_blob", lambda repo_dir, sha, path: OLD_SOURCE if sha == "a" * 40 else NEW_SOURCE)
    monkeypatch.setattr(tasks, "_build_client", lambda s: ScriptedLLM([CLASS_PUML, API_MD, SEQUENCE_PUML]))

    # 默认把"入队下一段"变成同步调用：这样一次任务调用能跑完整条链
    calls: list[str] = []

    def _sync_parse(run_id):
        calls.append("parse")
        return tasks.parse_run.run(run_id)

    def _sync_generate(run_id):
        calls.append("generate")
        return tasks.generate_run.run(run_id)

    monkeypatch.setattr(tasks.parse_run, "delay", _sync_parse)
    monkeypatch.setattr(tasks.generate_run, "delay", _sync_generate)

    yield {"store": store, "settings": settings, "calls": calls, "work": work}
    store.close()


def test_full_chain_creates_documents(env):
    result = tasks.analyze_push.run(dict(JOB))

    assert result["status"] == "succeeded"
    assert env["calls"] == ["parse", "generate"]

    run = get_run(env["store"], result["run_id"])
    assert run["status"] == "succeeded"
    assert run["files_changed"] == 1
    assert run["entities_changed"] >= 2

    with env["store"].begin() as conn:
        docs = conn.execute(documents.select()).mappings().all()
        revs = conn.execute(document_revisions.select()).mappings().all()
    assert {d["doc_type"] for d in docs} == {"class_diagram", "api_doc", "sequence_diagram"}
    assert all(d["status"] == "published" for d in docs)
    assert len(revs) == 3

    # 中间产物要能被人打开检查
    assert (env["work"] / "runs" / f"{result['run_id']}.json").exists()
    assert (env["work"] / "runs" / f"{result['run_id']}.changes.json").exists()


def test_duplicate_delivery_does_not_start_second_run(env):
    first = tasks.analyze_push.run(dict(JOB))
    second = tasks.analyze_push.run(dict(JOB))

    assert first["status"] == "succeeded"
    assert second["status"] == "duplicate_run"
    assert second["run_id"] == first["run_id"]
    assert env["calls"] == ["parse", "generate"]  # 第二份投递没有重跑整条链


def test_generate_without_api_key_marks_skipped(env, monkeypatch):
    env["settings"].llm_api_key = ""

    def _no_generate(run_id):  # 只跑到解析为止
        return {"status": "succeeded", "run_id": run_id}

    monkeypatch.setattr(tasks.generate_run, "delay", _no_generate)

    result = tasks.analyze_push.run(dict(JOB))
    run = get_run(env["store"], result["run_id"])
    # analyze_push 的返回仍是 succeeded（diff 本身成功了），run 则由 generate_run 标 skipped
    assert run["status"] == "parsing"

    skipped = tasks.generate_run.run(result["run_id"])
    assert skipped["reason"] == "llm_not_configured"
    assert get_run(env["store"], result["run_id"])["status"] == "skipped"


def test_parse_failure_is_recorded_on_run(env, monkeypatch):
    def boom(files, read_blob):
        raise RuntimeError("tree-sitter exploded")

    monkeypatch.setattr(tasks, "parse_changes", boom)
    result = tasks.analyze_push.run(dict(JOB))

    run = get_run(env["store"], result["run_id"])
    assert run["status"] == "failed"
    assert run["error_code"] == "parse_error"
    assert "tree-sitter exploded" in run["error_message"]


def test_watchdog_requeues_stale_run(env, monkeypatch):
    result = tasks.analyze_push.run(dict(JOB))
    run_id = result["run_id"]

    # 人为把 run 拉回中间态并让 started_at 变旧
    with env["store"].begin() as conn:
        from sqlalchemy import update
        from app.store import analysis_runs
        from datetime import datetime, timedelta, timezone

        conn.execute(
            update(analysis_runs).where(analysis_runs.c.id == run_id).values(
                status="parsing",
                finished_at=None,
                started_at=datetime.now(timezone.utc) - timedelta(minutes=45),
            )
        )

    requeued: list[str] = []
    monkeypatch.setattr(tasks.parse_run, "delay", lambda rid: requeued.append(rid))

    report = tasks.watchdog.run(15)
    assert report["count"] == 1
    assert requeued == [run_id]
    assert get_run(env["store"], run_id)["attempt"] == 2
