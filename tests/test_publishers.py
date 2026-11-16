"""投递层测试：离线可跑、不碰真实 GitHub / 真实网络。

覆盖四件容易写错的事：
1. 本地知识库真的落了文件，且候选版本不会覆盖当前版本；
2. PR 评论按 run 标记幂等（重跑是 PATCH 不是再发一条）；
3. 一个渠道失败不影响其它渠道，也不影响 run（这里只断言投递结果与审计）；
4. webhook 的 HMAC 签名可被收端复算。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.publish import mark_manual_edit, publish_document
from app.publishers import PUBLISHERS, build_digest, deliver_run, digest_for_document
from app.publishers.github_pr import GitHubPRCommentPublisher, build_comment, marker_for
from app.publishers.local_kb import LocalKnowledgeBasePublisher, _write_if_changed, safe_name
from app.publishers.webhook import WebhookKnowledgeBasePublisher
from app.store import (
    ensure_project,
    get_run,
    list_publish_records,
    open_store,
    start_run,
)
from app.pipeline import PublishReport

GOOD = "@startuml\nclass UserService {\n  +load(user_id: int): dict\n}\n@enduml"
HUMAN = "@startuml\nclass UserService {\n  +load(user_id: int, cache: bool): dict\n}\n@enduml"
AI_NEW = "@startuml\nclass UserService {\n  +load(user_id: int): User\n}\n@enduml"


@pytest.fixture
def store(tmp_path: Path):
    s = open_store(f"sqlite:///{(tmp_path / 'pub.sqlite').as_posix()}")
    yield s
    s.close()


@pytest.fixture
def seeded(store):
    project_id = ensure_project(
        store, provider="github", repo_full_name="acme/demo", repo_url="https://github.com/acme/demo.git"
    )
    run_id, _ = start_run(
        store,
        project_id=project_id,
        base_sha="a" * 40,
        head_sha="b" * 40,
        trigger_type="pull_request",
        trigger_ref="refs/pull/7/head",
    )
    outcome = publish_document(
        store,
        project_id=project_id,
        doc_type="class_diagram",
        target_key="app.services",
        title="app.services 类图",
        content=GOOD,
        run_id=run_id,
        content_format="plantuml",
    )
    report = PublishReport(outcomes=(outcome,), rejected=(("api_doc/app.services", "M005 no body"),))
    return project_id, run_id, outcome.document_id, report


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        kb_dir=tmp_path / "kb",
        publish_targets="local_wiki",
        public_base_url="http://localhost:8000",
        github_token="",
    )
    values.update(overrides)
    return Settings(**values)


def digest_of(store, run_id, report, **kwargs):
    return build_digest(
        store,
        run_id=run_id,
        report=report,
        artifact={"pr_number": 7, "repo": "acme/demo"},
        **kwargs,
    )


# ---------------------------------------------------------------- 本地知识库
def test_local_kb_writes_documents_and_index(store, seeded, tmp_path):
    project_id, run_id, document_id, report = seeded
    settings = settings_for(tmp_path)
    digest = digest_of(store, run_id, report, settings=settings, tokens=99)

    results = deliver_run(store, digest=digest, settings=settings)

    assert {r.status for r in results} == {"success"}
    target = tmp_path / "kb" / "acme-demo" / "class_diagram" / "app.services.puml"
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert 'status: "published"' in text
    assert GOOD in text

    index = (tmp_path / "kb" / "acme-demo" / "README.md").read_text(encoding="utf-8")
    assert "app.services 类图" in index
    assert "acme/demo" in index
    assert "打开在线评审界面" in index

    records = list_publish_records(store, run_id=run_id)
    assert len(records) == len(results)
    assert all(record["status"] == "success" for record in records)
    assert any(record["external_id"] == "class_diagram/app.services.puml" for record in records)


def test_local_kb_defers_candidate_and_keeps_human_current(store, seeded, tmp_path):
    project_id, run_id, document_id, report = seeded
    mark_manual_edit(store, document_id, content=HUMAN)
    # 人改之后 AI 再发一版：只应产生候选，不切 current
    outcome = publish_document(
        store,
        project_id=project_id,
        doc_type="class_diagram",
        target_key="app.services",
        title="app.services 类图",
        content=AI_NEW,
        run_id=run_id,
        content_format="plantuml",
    )
    assert outcome.action == "deferred_stale"

    settings = settings_for(tmp_path)
    digest = digest_of(store, run_id=run_id, report=PublishReport(outcomes=(outcome,), rejected=()), settings=settings)
    results = deliver_run(store, digest=digest, settings=settings)

    root = tmp_path / "kb" / "acme-demo"
    current_file = root / "class_diagram" / "app.services.puml"
    pending_file = root / "_pending" / "class_diagram" / "app.services.puml"
    assert HUMAN in current_file.read_text(encoding="utf-8")
    assert AI_NEW not in current_file.read_text(encoding="utf-8")
    assert AI_NEW in pending_file.read_text(encoding="utf-8")
    assert "waiting-for-human" in pending_file.read_text(encoding="utf-8")
    assert "待确认候选" in (root / "README.md").read_text(encoding="utf-8")
    assert any(r.detail.get("pending") for r in results)


def test_local_kb_skips_empty_content(store, seeded, tmp_path):
    project_id, run_id, document_id, report = seeded
    outcome = publish_document(
        store,
        project_id=project_id,
        doc_type="api_doc",
        target_key="app.services",
        title="空文档",
        content="   ",
        run_id=run_id,
        content_format="markdown",
    )
    settings = settings_for(tmp_path)
    digest = digest_of(store, run_id=run_id, report=PublishReport(outcomes=(outcome,), rejected=()), settings=settings)
    results = deliver_run(store, digest=digest, settings=settings)
    assert any(r.status == "skipped" and "empty content" in (r.error or "") for r in results)


# ---------------------------------------------------------------- PR 评论
def test_pr_comment_created_then_updated(store, seeded, tmp_path, monkeypatch):
    project_id, run_id, document_id, report = seeded
    calls: list[httpx.Request] = []
    state = {"posted": None}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            body = [] if state["posted"] is None else [state["posted"]]
            return httpx.Response(200, json=body)
        if request.method == "POST":
            data = json.loads(request.content)
            state["posted"] = {"id": 42, "body": data["body"], "html_url": "https://github.com/acme/demo/pull/7#issuecomment-42"}
            return httpx.Response(201, json=state["posted"])
        if request.method == "PATCH":
            data = json.loads(request.content)
            return httpx.Response(200, json={"id": 42, "body": data["body"], "html_url": "https://github.com/acme/demo/pull/7#issuecomment-42"})
        raise AssertionError(f"unexpected {request.method}")

    publisher = GitHubPRCommentPublisher(transport=httpx.MockTransport(handler))
    monkeypatch.setitem(PUBLISHERS, "github_pr_comment", publisher)
    settings = settings_for(tmp_path, publish_targets="github_pr_comment", github_token="test-token")

    digest = digest_of(store, run_id=run_id, report=report, settings=settings)
    first = deliver_run(store, digest=digest, settings=settings)
    assert len(first) == 1 and first[0].status == "success"
    assert first[0].detail["action"] == "created"

    second = deliver_run(store, digest=digest, settings=settings)
    assert second[0].detail["action"] == "updated"
    assert len(calls) == 4  # list + post, list + patch

    body = state["posted"]["body"]
    assert marker_for(run_id) in body
    assert "LivingDoc" in body
    assert "等待人工确认" not in body  # 没有 deferred 文档
    assert "M005 no body" in body
    assert calls[1].headers["authorization"] == "Bearer test-token"


def test_pr_comment_skipped_without_pr_number(store, seeded, tmp_path, monkeypatch):
    project_id, run_id, document_id, report = seeded
    calls = []
    publisher = GitHubPRCommentPublisher(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json=[])))
    monkeypatch.setitem(PUBLISHERS, "github_pr_comment", publisher)
    settings = settings_for(tmp_path, publish_targets="github_pr_comment", github_token="t")
    digest = build_digest(store, run_id=run_id, report=report, artifact={}, settings=settings)
    results = deliver_run(store, digest=digest, settings=settings)
    assert results[0].status == "skipped"
    assert "PR number" in (results[0].error or "")
    assert calls == []


def test_comment_marks_deferred_documents(store, seeded, tmp_path):
    project_id, run_id, document_id, report = seeded
    mark_manual_edit(store, document_id, content=HUMAN)
    outcome = publish_document(
        store,
        project_id=project_id,
        doc_type="class_diagram",
        target_key="app.services",
        title="app.services 类图",
        content=AI_NEW,
        run_id=run_id,
        content_format="plantuml",
    )
    settings = settings_for(tmp_path)
    digest = digest_of(store, run_id=run_id, report=PublishReport(outcomes=(outcome,), rejected=()), settings=settings)
    body = build_comment(digest)
    assert "等待人工确认" in body
    assert "当前 v2，候选 v3" in body


# ---------------------------------------------------------------- webhook
def test_webhook_payload_is_signed_and_contains_content(store, seeded, tmp_path, monkeypatch):
    project_id, run_id, document_id, report = seeded
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, headers={"X-Request-Id": "kb-1"}, json={"ok": True})

    publisher = WebhookKnowledgeBasePublisher(transport=httpx.MockTransport(handler))
    monkeypatch.setitem(PUBLISHERS, "webhook_kb", publisher)
    settings = settings_for(
        tmp_path,
        publish_targets="webhook_kb",
        kb_webhook_url="https://kb.example.test/hooks/livingdoc",
        kb_webhook_secret="s3cret",
    )
    digest = digest_of(store, run_id=run_id, report=report, settings=settings)
    results = deliver_run(store, digest=digest, settings=settings)

    assert results[0].status == "success"
    assert results[0].external_id == "kb-1"
    request = captured["request"]
    raw = request.content
    expected = hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    assert request.headers["X-LivingDoc-Signature"] == f"sha256={expected}"
    payload = json.loads(raw)
    # 入库时 normalize_content 会补末尾换行，这里只断言正文完整送达
    assert payload["documents"][0]["content"].strip() == GOOD.strip()
    assert payload["project"]["repo"] == "acme/demo"
    assert payload["rejected"][0]["reason"] == "M005 no body"


# ---------------------------------------------------------------- 失败隔离
def test_one_publisher_failure_does_not_block_others(store, seeded, tmp_path, monkeypatch):
    project_id, run_id, document_id, report = seeded
    settings = settings_for(
        tmp_path,
        publish_targets="local_wiki,webhook_kb",
        kb_webhook_url="https://kb.example.test/hook",
    )

    class Boom:
        name = "local_wiki"
        def available(self, _settings):
            return True
        def publish(self, _digest, _settings):
            raise RuntimeError("disk on fire")

    monkeypatch.setitem(PUBLISHERS, "local_wiki", Boom())
    good = WebhookKnowledgeBasePublisher(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    monkeypatch.setitem(PUBLISHERS, "webhook_kb", good)

    digest = digest_of(store, run_id=run_id, report=report, settings=settings)
    results = deliver_run(store, digest=digest, settings=settings)

    assert [r.status for r in results] == ["failed", "success"]
    records = list_publish_records(store, run_id=run_id)
    assert {r["status"] for r in records} == {"failed", "success"}
    assert "disk on fire" in next(r["error_message"] for r in records if r["status"] == "failed")


def test_unknown_target_is_recorded_as_skipped(store, seeded, tmp_path):
    project_id, run_id, document_id, report = seeded
    settings = settings_for(tmp_path, publish_targets="nope")
    digest = digest_of(store, run_id=run_id, report=report, settings=settings)
    assert deliver_run(store, digest=digest, settings=settings) == []
    # configured_targets 过滤未知名字，不产生记录
    assert list_publish_records(store, run_id=run_id) == []


def test_delivery_never_raises_for_connection_errors(store, seeded, tmp_path, monkeypatch):
    project_id, run_id, document_id, report = seeded

    class Boom:
        name = "webhook_kb"
        def available(self, _settings):
            return True
        def publish(self, _digest, _settings):
            raise ConnectionError("no route")

    monkeypatch.setitem(PUBLISHERS, "webhook_kb", Boom())
    settings = settings_for(tmp_path, publish_targets="webhook_kb", kb_webhook_url="https://x.test")
    digest = digest_of(store, run_id=run_id, report=report, settings=settings)
    results = deliver_run(store, digest=digest, settings=settings)
    assert results[0].status == "failed"
    assert "ConnectionError" in results[0].error


# ---------------------------------------------------------------- 摘要/工具
def test_digest_contains_pr_and_counts(store, seeded):
    project_id, run_id, document_id, report = seeded
    digest = digest_of(store, run_id=run_id, report=report)
    assert digest.pr_number == 7
    assert digest.counts["documents"] == 1
    assert digest.counts["changed"] == 1
    payload = digest.as_payload()
    assert payload["trigger"]["pr_number"] == 7
    assert payload["documents"][0]["target_key"] == "app.services"
    assert payload["documents"][0]["review_url"].endswith(f"#doc={document_id}")


def test_digest_for_single_document_keeps_whole_index(store, seeded):
    project_id, run_id, document_id, report = seeded
    publish_document(
        store,
        project_id=project_id,
        doc_type="api_doc",
        target_key="app.services",
        title="API",
        content="## app.services\n\n### load\n\n- signature: load(user_id: int) -> dict\n",
        run_id=run_id,
        content_format="markdown",
    )
    digest = digest_for_document(store, document_id)
    assert len(digest.documents) == 2
    assert [d.action for d in digest.documents if d.document_id == document_id] == ["accepted"]
    assert sum(1 for d in digest.documents if d.action == "unchanged") == 1


def test_safe_name_keeps_chinese_and_strips_separators():
    assert safe_name("app/services:User<T>") == "app-services-User-T"
    assert safe_name("用户服务") == "用户服务"
    assert safe_name("...") == "untitled"


def test_configured_targets_deduplicates():
    s = Settings(_env_file=None, publish_targets="local_wiki, github_pr_comment,local_wiki,bogus")
    from app.publishers.dispatch import configured_targets

    assert configured_targets(s) == ["local_wiki", "github_pr_comment"]


def test_concurrent_kb_writes_do_not_share_a_temp_file(tmp_path: Path):
    """并发发布会同时写同一个知识库文件，临时文件名不能固定。

    固定 `<path>.tmp` 时，先完成 replace 的线程会把 tmp 改名走，后一个线程
    再 replace 就 FileNotFoundError；Linux 容器里的并发测试复现过。
    """
    path = tmp_path / "doc.puml"

    def write(index: int) -> None:
        _write_if_changed(path, f"' version {index}\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    assert path.read_text(encoding="utf-8").startswith("' version ")
    assert [p.name for p in tmp_path.iterdir()] == [path.name]