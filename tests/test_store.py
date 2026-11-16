"""持久层与状态机的测试。

这里测的不是"能不能写进 SQLite"，而是三条会决定线上行为的规则：
重复投递不产生第二个 run、非法状态迁移必须炸、看门狗要靠时间戳而不是队列状态找人。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, update

from app.store import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidTransition,
    active_runs,
    ai_invocations,
    analysis_runs,
    ensure_project,
    fail_run,
    get_project,
    get_run,
    open_store,
    record_invocation,
    start_run,
    transition_run,
)


@pytest.fixture()
def store(tmp_path):
    s = open_store(f"sqlite:///{(tmp_path / 'livingdoc.sqlite').as_posix()}")
    yield s
    s.close()


def _new_run(store, *, head="b" * 40, base="a" * 40) -> tuple[str, str]:
    project_id = ensure_project(store, provider="github", repo_full_name="octocat/Hello-World")
    run_id, _created = start_run(store, project_id=project_id, base_sha=base, head_sha=head)
    return project_id, run_id


def test_ensure_project_is_idempotent(store):
    first = ensure_project(store, provider="github", repo_full_name="octocat/Hello-World")
    second = ensure_project(store, provider="github", repo_full_name="octocat/Hello-World")
    other = ensure_project(store, provider="github", repo_full_name="octocat/Spoon-Knife")

    assert first == second
    assert first != other
    row = get_project(store, first)
    assert row["repo_full_name"] == "octocat/Hello-World"
    assert row["slug"] == "octocat-hello-world"


def test_start_run_is_idempotent_per_push(store):
    project_id = ensure_project(store, provider="github", repo_full_name="octocat/Hello-World")
    run_id, created = start_run(store, project_id=project_id, base_sha="a" * 40, head_sha="b" * 40)
    same_id, created_again = start_run(store, project_id=project_id, base_sha="a" * 40, head_sha="b" * 40)
    other_id, created_other = start_run(store, project_id=project_id, base_sha="b" * 40, head_sha="c" * 40)

    assert created is True
    assert created_again is False
    assert same_id == run_id
    assert created_other is True
    assert other_id != run_id


def test_transition_sets_timestamps_and_rejects_illegal_edges(store):
    _project, run_id = _new_run(store)

    row = transition_run(store, run_id, "cloning")
    assert row["status"] == "cloning"
    assert row["started_at"] is not None
    assert row["finished_at"] is None

    with pytest.raises(InvalidTransition):
        transition_run(store, run_id, "succeeded")  # cloning 不能直接到 succeeded

    with pytest.raises(InvalidTransition):
        transition_run(store, run_id, "no_such_status")


def test_same_status_transition_is_idempotent(store):
    """Celery 是 at-least-once：同一段任务可能被执行两次，第二次不该判失败。"""
    _project, run_id = _new_run(store)
    transition_run(store, run_id, "parsing")
    again = transition_run(store, run_id, "parsing", stage_detail="retry")
    assert again["status"] == "parsing"
    assert again["stage_detail"] == "retry"


def test_unknown_field_is_rejected_early(store):
    _project, run_id = _new_run(store)
    with pytest.raises(ValueError):
        transition_run(store, run_id, "cloning", no_such_column=1)


def test_failed_run_records_error_and_finishes(store):
    _project, run_id = _new_run(store)
    row = fail_run(store, run_id, code="git_error", message="missing object")
    assert row["status"] == "failed"
    assert row["error_code"] == "git_error"
    assert row["finished_at"] is not None

    with pytest.raises(InvalidTransition):
        transition_run(store, run_id, "parsing")  # 终态不可复活


def test_happy_path_reaches_succeeded(store):
    _project, run_id = _new_run(store)
    for status in ("cloning", "diffing", "parsing", "analyzing", "publishing", "succeeded"):
        transition_run(store, run_id, status)
    row = get_run(store, run_id)
    assert row["status"] == "succeeded"
    assert row["finished_at"] is not None


def test_every_terminal_status_has_no_outgoing_edges():
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_active_runs_only_reports_stale_unfinished(store):
    project_id = ensure_project(store, provider="github", repo_full_name="octocat/Hello-World")
    fresh, _ = start_run(store, project_id=project_id, base_sha="a" * 40, head_sha="b" * 40)
    stale, _ = start_run(store, project_id=project_id, base_sha="b" * 40, head_sha="c" * 40)
    done, _ = start_run(store, project_id=project_id, base_sha="c" * 40, head_sha="d" * 40)

    transition_run(store, fresh, "parsing")
    transition_run(store, stale, "diffing")
    for status in ("cloning", "diffing", "parsing", "analyzing", "publishing", "succeeded"):
        transition_run(store, done, status)

    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    with store.begin() as conn:
        conn.execute(update(analysis_runs).where(analysis_runs.c.id == stale).values(started_at=old))

    stale_ids = [row["id"] for row in active_runs(store, older_than_minutes=15)]
    assert stale_ids == [stale]
    assert fresh not in stale_ids
    assert done not in stale_ids


def test_record_invocation_writes_audit_row(store):
    _project, run_id = _new_run(store)
    record_invocation(
        store, run_id=run_id, document_id=None, purpose="class_diagram",
        model="deepseek-chat", prompt_version="v3-2025-11",
        prompt_tokens=1200, completion_tokens=340, latency_ms=2500, ok=True,
    )
    with store.begin() as conn:
        rows = conn.execute(ai_invocations.select()).mappings().all()
    assert len(rows) == 1
    assert rows[0]["purpose"] == "class_diagram"
    assert rows[0]["prompt_tokens"] == 1200
