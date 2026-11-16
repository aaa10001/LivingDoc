"""并发场景：评审页的"发布"按钮会被多个人同时点。

这里不测 Celery worker，而是测同步路径上的两个风险：
1. 同一文档的 publish 并发写 publish_records，不能互相踩掉；
2. 并发人工编辑时版本号必须唯一（Unique(document_id, version) 的兜底）。
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app.publish import publish_document
from app.store import ensure_project, start_run
from app.tasks import get_store

GOOD = """@startuml
class User {
  +String id
}
@enduml
"""


def _seed() -> dict:
    store = get_store()
    suffix = uuid.uuid4().hex[:10]
    project_id = ensure_project(
        store,
        provider="github",
        repo_full_name=f"acme/concurrent-{suffix}",
        repo_url=f"https://github.com/acme/concurrent-{suffix}.git",
        name=f"concurrent-{suffix}",
    )
    run_id, _ = start_run(
        store,
        project_id=project_id,
        base_sha="c" * 40,
        head_sha="d" * 40,
        trigger_type="push",
    )
    outcome = publish_document(
        store,
        project_id=project_id,
        doc_type="class_diagram",
        target_key="app.concurrent",
        title="并发用例",
        content=GOOD,
        run_id=run_id,
    )
    return {"project_id": project_id, "run_id": run_id, "document_id": outcome.document_id}


def test_concurrent_publish_records_are_all_kept():
    seeded = _seed()
    url = f"/api/v1/documents/{seeded['document_id']}/publish"

    def hit(_: int) -> tuple[int, dict]:
        with TestClient(app) as client:
            response = client.post(url, json={"target": "local_wiki"})
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(hit, range(8)))

    assert all(status == 200 for status, _ in results), results
    assert all(payload["results"][0]["status"] == "success" for _, payload in results)

    with TestClient(app) as client:
        records = client.get(
            "/api/v1/publish-records",
            params={"document_id": seeded["document_id"], "limit": 50},
        ).json()
    assert len(records) == 8
    assert all(record["status"] == "success" for record in records)


def test_concurrent_manual_edits_get_distinct_versions():
    """两个人同时保存，不能拿到同一个 version；冲突时 publish.py 会重试。"""
    seeded = _seed()
    url = f"/api/v1/documents/{seeded['document_id']}"

    def edit(index: int) -> tuple[int, int | None]:
        body = GOOD.replace("class User {", f"class User {{\n  +String field{index}")
        with TestClient(app) as client:
            response = client.put(url, json={"content": body, "summary": f"并发 {index}"})
            if response.status_code != 200:
                return response.status_code, None
            return 200, response.json().get("current_version")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(edit, range(4)))

    assert all(status == 200 for status, _ in results), results
    versions = [version for _, version in results]
    assert len(set(versions)) == 4, versions
    assert set(versions) == {2, 3, 4, 5}

    with TestClient(app) as client:
        revisions = client.get(f"{url}/revisions").json()
    assert [row["version"] for row in revisions] == [5, 4, 3, 2, 1]
    assert all(row["created_by"] == "human" for row in revisions[:4])