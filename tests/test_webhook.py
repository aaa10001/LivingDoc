"""Webhook 端点的端到端测试（不启动 uvicorn，用 TestClient 直接打 ASGI）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

# 环境里没有安装 fastapi/celery 时（例如离线机器）整组跳过，
# 而不是让整个测试会话红掉 —— 另外两个测试文件只依赖标准库 + pydantic。
pytest.importorskip("fastapi", reason="需要 fastapi（pip install -r requirements.txt）")
pytest.importorskip("celery", reason="需要 celery（pip install -r requirements.txt）")

from fastapi.testclient import TestClient  # noqa: E402

SECRET = "test-secret"
URL = "/webhook/github"


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _headers(body: bytes, event: str = "push", delivery: str = "d-1") -> dict[str, str]:
    from app.security import sign_github_payload

    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": sign_github_payload(SECRET, body, "sha256"),
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    import app.main as main

    captured: list[dict[str, Any]] = []

    class _Result:
        id = "task-abc123"

    # 假实现必须照抄 Celery 的真实签名 apply_async(args=None, kwargs=None, **options)：
    # app 调用的是 apply_async(kwargs={"job": job}, queue="analysis")，
    # 所以任务入参在 kwargs["kwargs"]["job"] 里，而不是 kwargs["job"]。
    def fake_apply_async(
        args: Any = None,
        kwargs: dict[str, Any] | None = None,
        **options: Any,
    ) -> _Result:
        assert kwargs is not None and "job" in kwargs, "应用必须先构造 job 再入队"
        captured.append(kwargs["job"])
        return _Result()

    monkeypatch.setattr(main.analyze_push, "apply_async", fake_apply_async)
    with TestClient(main.app) as test_client:
        yield test_client, captured


def test_valid_push_is_accepted(client: Any, push_payload: dict) -> None:
    test_client, captured = client
    body = _body(push_payload)
    resp = test_client.post(URL, content=body, headers=_headers(body))
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    assert len(captured) == 1
    job = captured[0]
    assert job["repo_full_name"] == "octocat/Hello-World"
    assert job["base_sha"] == "a" * 40
    assert job["head_sha"] == "b" * 40


def test_bad_signature_is_rejected(client: Any, push_payload: dict) -> None:
    test_client, captured = client
    body = _body(push_payload)
    headers = _headers(body)
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    resp = test_client.post(URL, content=body, headers=headers)
    assert resp.status_code == 401
    assert captured == []


def test_missing_signature_is_rejected(client: Any, push_payload: dict) -> None:
    test_client, captured = client
    body = _body(push_payload)
    headers = _headers(body)
    headers.pop("X-Hub-Signature-256")
    resp = test_client.post(URL, content=body, headers=headers)
    assert resp.status_code == 401
    assert captured == []


def test_duplicate_delivery_returns_200_not_error(client: Any, push_payload: dict) -> None:
    test_client, captured = client
    body = _body(push_payload)
    headers = _headers(body, delivery="dup-1")
    assert test_client.post(URL, content=body, headers=headers).status_code == 202
    second = test_client.post(URL, content=body, headers=headers)
    # 关键：重复投递必须返回 2xx，否则平台会一直重试
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(captured) == 1


def test_branch_deleted_is_ignored(client: Any, push_payload: dict) -> None:
    test_client, captured = client
    push_payload["deleted"] = True
    push_payload["after"] = "0" * 40
    body = _body(push_payload)
    resp = test_client.post(URL, content=body, headers=_headers(body, delivery="d-del"))
    assert resp.status_code == 200
    assert resp.json()["reason"] == "branch_deleted"
    assert captured == []


def test_ping_event_returns_pong(client: Any) -> None:
    test_client, _ = client
    body = _body({"zen": "Non-blocking is better than blocking."})
    resp = test_client.post(URL, content=body, headers=_headers(body, event="ping"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "pong"


def test_unhandled_event_is_ignored(client: Any) -> None:
    test_client, _ = client
    body = _body({"action": "created"})
    resp = test_client.post(URL, content=body, headers=_headers(body, event="star"))
    assert resp.status_code == 200
    assert resp.json()["reason"] == "unhandled_event"


def test_invalid_json_returns_400(client: Any) -> None:
    test_client, _ = client
    body = b"{not json"
    resp = test_client.post(URL, content=body, headers=_headers(body, delivery="d-bad"))
    assert resp.status_code == 400


def test_pull_request_synchronize_is_accepted(client: Any) -> None:
    test_client, captured = client
    payload = {
        "action": "synchronize",
        "number": 7,
        "pull_request": {
            "number": 7,
            "draft": False,
            "base": {"sha": "c" * 40},
            "head": {"sha": "d" * 40, "ref": "feature/x"},
        },
        "repository": {
            "full_name": "octocat/Hello-World",
            "clone_url": "https://github.com/octocat/Hello-World.git",
        },
    }
    body = _body(payload)
    resp = test_client.post(URL, content=body, headers=_headers(body, event="pull_request"))
    assert resp.status_code == 202
    assert captured[0]["pr_number"] == 7
    assert captured[0]["head_sha"] == "d" * 40


def test_queue_failure_returns_503(client: Any, push_payload: dict) -> None:
    test_client, _ = client
    import app.main as main

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("broker down")

    original = main.analyze_push.apply_async
    main.analyze_push.apply_async = boom  # type: ignore[method-assign]
    try:
        body = _body(push_payload)
        resp = test_client.post(URL, content=body, headers=_headers(body, delivery="d-q"))
    finally:
        main.analyze_push.apply_async = original  # type: ignore[method-assign]
    # 5xx 是故意的：让平台稍后重投，而不是静默丢单
    assert resp.status_code == 503
