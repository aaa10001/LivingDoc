"""评审 API 的端到端测试。

这些用例走 FastAPI TestClient，覆盖真实的路由、SQLAlchemy 存储与发布适配器；
和 test_publishers.py 的区别是：这里断言的是「HTTP 契约」，不是适配器内部。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.publish import get_document, publish_document
from app.store import ensure_project, start_run
from app.tasks import get_store

GOOD = """@startuml
class User {
  +String id
  +String name
}
class UserService {
  +User find(String id)
}
UserService --> User
@enduml
"""

HUMAN = """@startuml
class User {
  +String id
  +String name
  +String email
}
@enduml
"""

AI_NEW = """@startuml
class User {
  +String id
  +String displayName
}
class AuditLog {
  +String action
}
User --> AuditLog
@enduml
"""

MARKDOWN = """# 用户服务 API

## 查询用户

`GET /users/{id}` 返回单个用户。每个用户包含 id、名称与邮箱三个字段，字段均不可为空。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 用户唯一标识 |
| name | string | 展示名称 |
"""


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def seed(client: TestClient) -> dict:
    """建一个独立项目 + 一次 run + 一份类图，返回定位所需的一切。"""
    store = get_store()
    suffix = uuid.uuid4().hex[:10]
    project_id = ensure_project(
        store,
        provider="github",
        repo_full_name=f"acme/demo-{suffix}",
        repo_url=f"https://github.com/acme/demo-{suffix}.git",
        name=f"demo-{suffix}",
    )
    run_id, _ = start_run(
        store,
        project_id=project_id,
        base_sha="a" * 40,
        head_sha="b" * 40,
        trigger_type="push",
    )
    outcome = publish_document(
        store,
        project_id=project_id,
        doc_type="class_diagram",
        target_key="app.services",
        title="服务类图",
        content=GOOD,
        run_id=run_id,
        content_format="plantuml",
        model_name="deepseek-chat",
        prompt_version="v3-2025-11",
        change_summary="初始生成",
    )
    return {
        "store": store,
        "project_id": project_id,
        "run_id": run_id,
        "document_id": outcome.document_id,
        "suffix": suffix,
    }


def test_meta_reports_targets_and_version(client: TestClient):
    response = client.get("/api/v1/meta")
    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "1.0.0"
    assert "local_wiki" in payload["publish_targets"]


def test_projects_documents_and_detail(client: TestClient):
    seeded = seed(client)

    projects = client.get("/api/v1/projects").json()
    project = next(p for p in projects if p["id"] == seeded["project_id"])
    assert project["document_count"] == 1
    assert project["pending_count"] == 0
    assert project["last_run"]["id"] == seeded["run_id"]
    assert f"#project={seeded['project_id']}" in project["review_url"]

    docs = client.get(f"/api/v1/projects/{seeded['project_id']}/documents").json()
    assert len(docs) == 1
    doc = docs[0]
    assert doc["doc_type"] == "class_diagram"
    assert doc["current_version"] == 1
    assert doc["has_candidate"] is False

    detail = client.get(f"/api/v1/documents/{seeded['document_id']}").json()
    assert detail["title"] == "服务类图"
    assert "class User" in detail["current"]["content"]
    assert "#doc=" in detail["review_url"]

    # 类型过滤与搜索都走同一端点
    assert client.get(
        f"/api/v1/projects/{seeded['project_id']}/documents?doc_type=api_doc"
    ).json() == []
    assert client.get(
        f"/api/v1/projects/{seeded['project_id']}/documents?q=服务"
    ).json()[0]["id"] == seeded["document_id"]


def test_missing_resources_are_rfc7807(client: TestClient):
    response = client.get("/api/v1/documents/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["status"] == 404
    assert problem["title"] == "Document not found"
    assert problem["instance"] == "/api/v1/documents/does-not-exist"
    assert problem["type"].endswith("/not_found")


def test_project_overview_groups_modules_and_previews(client: TestClient):
    seeded = seed(client)
    store = seeded["store"]
    publish_document(
        store,
        project_id=seeded["project_id"],
        doc_type="api_doc",
        target_key="app.services",
        title="服务 API",
        content=MARKDOWN,
        run_id=seeded["run_id"],
        content_format="markdown",
    )
    publish_document(
        store,
        project_id=seeded["project_id"],
        doc_type="sequence_diagram",
        target_key="app.worker",
        title="任务时序",
        content=GOOD.replace("class User {", "participant User\nparticipant Worker"),
        run_id=seeded["run_id"],
        content_format="plantuml",
    )

    response = client.get(f"/api/v1/projects/{seeded['project_id']}/overview")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["project"]["repo_full_name"].startswith("acme/demo-")
    assert payload["stats"]["documents"] == 3
    assert payload["stats"]["modules"] == 2
    assert payload["repo"]["head_sha"] == "b" * 40
    assert payload["repo"]["head_url"].endswith("b" * 40)

    by_key = {module["key"]: module for module in payload["modules"]}
    assert set(by_key) == {"app.services", "app.worker"}
    services = by_key["app.services"]
    assert services["count"] == 2
    assert services["counts"]["class_diagram"] == 1
    assert services["counts"]["api_doc"] == 1
    previews = {doc["doc_type"]: doc["preview"] for doc in services["documents"]}
    assert "UserService" in previews["class_diagram"] or "find" in previews["class_diagram"]
    assert "查询用户" in previews["api_doc"]
    assert all("@startuml" not in value for value in previews.values())
    assert payload["runs"][0]["id"] == seeded["run_id"]


def test_project_overview_missing_project_is_rfc7807(client: TestClient):
    response = client.get("/api/v1/projects/does-not-exist/overview")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_manual_edit_creates_human_version(client: TestClient):
    seeded = seed(client)
    url = f"/api/v1/documents/{seeded['document_id']}"

    response = client.put(url, json={"content": HUMAN, "summary": "补上 email"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["changed"] is True
    assert payload["manual_edited"] is True
    assert payload["current_version"] == 2
    assert payload["current"]["created_by"] == "human"
    assert payload["current"]["change_summary"] == "补上 email"
    assert payload["outcome"]["action"] == "human_edit"

    # 内容没变时不制造新版本
    again = client.put(url, json={"content": HUMAN})
    assert again.status_code == 200
    assert again.json()["changed"] is False

    revisions = client.get(f"{url}/revisions").json()
    assert [row["version"] for row in revisions] == [2, 1]
    assert all("content" not in row for row in revisions)
    assert "AuditLog" not in revisions[0].get("content", "")

    version = client.get(f"{url}/revisions/2").json()
    assert "email" in version["content"]


def test_manual_edit_rejects_invalid_content_unless_forced(client: TestClient):
    seeded = seed(client)
    url = f"/api/v1/documents/{seeded['document_id']}"

    response = client.put(url, json={"content": "@startuml\nclass Broken\n"})
    assert response.status_code == 422
    problem = response.json()
    assert problem["title"] == "Document failed validation"
    assert problem["diagnostics"][0]["code"]

    forced = client.put(url, json={"content": "@startuml\nclass Broken\n", "force": True})
    assert forced.status_code == 200
    assert forced.json()["changed"] is True


def test_empty_edit_and_oversized_edit_are_rejected(client: TestClient):
    seeded = seed(client)
    url = f"/api/v1/documents/{seeded['document_id']}"
    assert client.put(url, json={"content": "   \n"}).status_code == 422
    huge = "x" * (512 * 1024 + 1)
    assert client.put(url, json={"content": huge}).status_code == 413


def test_ai_candidate_can_be_accepted(client: TestClient):
    seeded = seed(client)
    store = seeded["store"]
    url = f"/api/v1/documents/{seeded['document_id']}"

    assert client.put(url, json={"content": HUMAN}).status_code == 200
    publish_document(
        store,
        project_id=seeded["project_id"],
        doc_type="class_diagram",
        target_key="app.services",
        title="服务类图",
        content=AI_NEW,
        run_id=seeded["run_id"],
        change_summary="AI 更新",
    )

    detail = client.get(url).json()
    assert detail["has_candidate"] is True
    assert detail["status"] == "stale"
    assert detail["candidate"]["version"] == 3
    assert detail["current_version"] == 2

    accepted = client.post(f"{url}/accept-ai")
    assert accepted.status_code == 200, accepted.text
    payload = accepted.json()
    assert payload["outcome"]["action"] == "accepted"
    assert payload["current_version"] == 3
    assert payload["has_candidate"] is False
    # 接受不等于解除保护：下次 AI 更新仍要人再看一眼
    assert payload["manual_edited"] is True

    assert client.post(f"{url}/accept-ai").status_code == 409


def test_diff_between_revisions(client: TestClient):
    seeded = seed(client)
    store = seeded["store"]
    publish_document(
        store,
        project_id=seeded["project_id"],
        doc_type="class_diagram",
        target_key="app.services",
        title="服务类图",
        content=HUMAN,
        run_id=seeded["run_id"],
        change_summary="第二版",
    )

    url = f"/api/v1/documents/{seeded['document_id']}/diff"
    response = client.get(url, params={"from": 1, "to": 2})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["from_version"] == 1
    assert payload["to_version"] == 2
    assert payload["identical"] is False
    assert payload["added"] >= 1
    assert "+  +String email" in payload["unified_diff"]

    # 只传 to 时自动找前一个版本
    previous = client.get(url, params={"to": 2}).json()
    assert previous["from_version"] == 1

    assert client.get(url, params={"from": 99}).status_code == 404


def test_render_markdown_uses_static_validator(client: TestClient):
    store = get_store()
    seeded = seed(client)
    outcome = publish_document(
        store,
        project_id=seeded["project_id"],
        doc_type="api_doc",
        target_key="api.users",
        title="用户 API",
        content=MARKDOWN,
        run_id=seeded["run_id"],
        content_format="markdown",
    )
    response = client.post(f"/api/v1/documents/{outcome.document_id}/render")
    assert response.status_code == 200
    payload = response.json()
    assert payload["render_ok"] is True
    assert payload["renderer"] == "static"
    assert payload["svg_url"] is None
    assert payload["version"] == 1


def test_render_of_invalid_plantuml_reports_diagnostics(client: TestClient):
    store = get_store()
    seeded = seed(client)
    outcome = publish_document(
        store,
        project_id=seeded["project_id"],
        doc_type="class_diagram",
        target_key="app.broken",
        title="坏图",
        content="@startuml\nclass Broken\n",
        run_id=seeded["run_id"],
        render_ok=False,
    )
    response = client.post(f"/api/v1/documents/{outcome.document_id}/render")
    assert response.status_code == 200
    payload = response.json()
    assert payload["render_ok"] is False
    assert payload["svg_url"] is None
    assert payload["diagnostics"]


def test_render_route_rejects_traversal_names(client: TestClient):
    assert client.get("/api/v1/renders/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/api/v1/renders/not-a-hash.svg").status_code == 404


def test_publish_to_local_wiki_and_audit_trail(client: TestClient):
    seeded = seed(client)
    response = client.post(
        f"/api/v1/documents/{seeded['document_id']}/publish",
        json={"target": "local_wiki"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["results"][0]["status"] == "success"
    assert payload["results"][0]["target"] == "local_wiki"
    assert payload["document_id"] == seeded["document_id"]

    records = client.get(
        "/api/v1/publish-records", params={"document_id": seeded["document_id"]}
    ).json()
    assert records[0]["status"] == "success"
    assert records[0]["target"] == "local_wiki"

    settings = get_settings()
    written = list(Path(settings.kb_dir).rglob("*.puml"))
    assert any("app.services" in path.name for path in written)


def test_publish_rejects_unknown_target(client: TestClient):
    seeded = seed(client)
    response = client.post(
        f"/api/v1/documents/{seeded['document_id']}/publish",
        json={"target": "confluence"},
    )
    assert response.status_code == 422
    assert "confluence" in response.json()["detail"]


def test_runs_and_invocations(client: TestClient):
    seeded = seed(client)
    runs = client.get(f"/api/v1/projects/{seeded['project_id']}/runs").json()
    assert runs[0]["id"] == seeded["run_id"]
    assert runs[0]["status"] == "queued"

    run = client.get(f"/api/v1/runs/{seeded['run_id']}").json()
    assert run["id"] == seeded["run_id"]
    assert run["invocations"] == []
    assert run["publish_records"] == []


def test_review_ui_is_served(client: TestClient):
    page = client.get("/ui/")
    assert page.status_code == 200
    assert "LivingDoc" in page.text
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/style.css").status_code == 200


def test_manual_edit_after_accept_still_protected(client: TestClient):
    """人改 → 接受 AI → 再 AI 发布，应该仍然只生成候选。"""
    seeded = seed(client)
    store = seeded["store"]
    url = f"/api/v1/documents/{seeded['document_id']}"
    assert client.put(url, json={"content": HUMAN}).status_code == 200
    publish_document(
        store,
        project_id=seeded["project_id"],
        doc_type="class_diagram",
        target_key="app.services",
        title="服务类图",
        content=AI_NEW,
        run_id=seeded["run_id"],
    )
    assert client.post(f"{url}/accept-ai").status_code == 200

    later = AI_NEW.replace("AuditLog", "AuditTrail")
    outcome = publish_document(
        store,
        project_id=seeded["project_id"],
        doc_type="class_diagram",
        target_key="app.services",
        title="服务类图",
        content=later,
        run_id=seeded["run_id"],
    )
    assert outcome.action == "deferred_stale"
    detail = client.get(url).json()
    assert detail["has_candidate"] is True
    assert "AuditTrail" in detail["candidate"]["content"]
    assert "AuditTrail" not in (detail["current"]["content"] or "")

    stored = get_document(store, seeded["document_id"])
    assert stored["manual_edited"] is True