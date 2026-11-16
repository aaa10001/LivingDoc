"""文档评审 REST API（/api/v1）。

这一层只做三件事：读、人工裁决、触发投递。它刻意**不**做生成：
生成属于 Celery 任务，HTTP 请求里跑 LLM 会超时，也会让 webhook 的重试语义变复杂。

人工裁决的语义（ADR-006）：
- PUT 写一个 created_by=human 的新版本，并置 manual_edited=true；
- 之后的 AI 更新只写候选（status=stale），必须显式 accept-ai 才切换 current。
所以"保存"和"接受 AI"是两个按钮，不是同一个动作。
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.publish import (
    PublishError,
    accept_ai,
    get_document,
    get_revision,
    list_documents,
    list_revisions,
    mark_manual_edit,
    normalize_content,
)
from app.publishers import PUBLISHERS, deliver_run, digest_for_document
from app.publishers.base import review_url
from app.publishers.dispatch import configured_targets
from app.render import diagnostic_payload, render_svg, validate_document
from app.store import Store, get_project, get_run, list_invocations, list_projects, list_publish_records, list_runs

router = APIRouter(prefix="/api/v1", tags=["review"])


# 所有路由共用一个依赖；测试里用 app.dependency_overrides 换成临时库。
def _store_dependency() -> Store:  # pragma: no cover - 由 FastAPI 调用
    from app.tasks import get_store

    return get_store()


StoreDep = Depends(_store_dependency)

_MAX_EDIT_BYTES = 512 * 1024
_RENDER_NAME = re.compile(r"^[0-9a-f]{32}\.svg$")


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class ManualEditRequest(BaseModel):
    content: str
    summary: str | None = None
    # 人工明知图有语法问题也要先存下来（例如改到一半），显式 force 才放行。
    force: bool = False


class PublishRequest(BaseModel):
    target: str | None = None
    message: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def _iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _revision_payload(revision: dict[str, Any] | None, *, include_content: bool = True) -> dict[str, Any] | None:
    if not revision:
        return None
    content = revision.get("content") or ""
    payload = {
        "id": revision.get("id"),
        "version": revision.get("version"),
        "created_by": revision.get("created_by"),
        "created_at": _iso(revision.get("created_at")),
        "content_hash": revision.get("content_hash"),
        "change_summary": revision.get("change_summary"),
        "render_ok": revision.get("render_ok"),
        "render_error": revision.get("render_error"),
        "model_name": revision.get("model_name"),
        "prompt_version": revision.get("prompt_version"),
        "source_entity_ids": revision.get("source_entity_ids") or [],
        "content_length": len(content),
    }
    if include_content:
        payload["content"] = content
    return payload


def _document_meta(doc: dict[str, Any], *, settings: Any) -> dict[str, Any]:
    url = review_url(settings.public_base_url, project_id=doc["project_id"], document_id=doc["id"])
    return {
        "id": doc["id"],
        "project_id": doc["project_id"],
        "doc_type": doc["doc_type"],
        "target_key": doc["target_key"],
        "title": doc["title"],
        "content_format": doc.get("content_format"),
        "status": doc.get("status"),
        "manual_edited": bool(doc.get("manual_edited")),
        "current_revision_id": doc.get("current_revision_id"),
        "owner_run_id": doc.get("owner_run_id"),
        "created_at": _iso(doc.get("created_at")),
        "updated_at": _iso(doc.get("updated_at")),
        "review_url": url,
    }


def _document_detail(doc: dict[str, Any], *, settings: Any) -> dict[str, Any]:
    payload = _document_meta(doc, settings=settings)
    current = doc.get("current")
    latest = doc.get("latest_revision")
    payload["current"] = _revision_payload(current)
    payload["latest"] = _revision_payload(latest, include_content=False)
    payload["current_version"] = (current or {}).get("version")
    payload["has_candidate"] = bool(
        latest and current and latest.get("id") != current.get("id")
    )
    candidate = latest if payload["has_candidate"] else None
    payload["candidate"] = _revision_payload(candidate)
    return payload


def _run_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "trigger_type": row.get("trigger_type"),
        "trigger_ref": row.get("trigger_ref"),
        "base_sha": row.get("base_sha"),
        "head_sha": row.get("head_sha"),
        "status": row.get("status"),
        "stage_detail": row.get("stage_detail"),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "files_changed": row.get("files_changed"),
        "entities_changed": row.get("entities_changed"),
        "tokens_in": row.get("tokens_in"),
        "tokens_out": row.get("tokens_out"),
        "attempt": row.get("attempt"),
        "queued_at": _iso(row.get("queued_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
    }


def _problem(
    status_code: int, code: str, title: str, detail: str, *, extra: dict[str, Any] | None = None
) -> HTTPException:
    payload: dict[str, Any] = {
        "type": f"https://livingdoc.dev/errors/{code}",
        "title": title,
        "status": status_code,
        "detail": detail,
    }
    if extra:
        payload.update(extra)
    return HTTPException(status_code=status_code, detail=payload)


def _require_project(store: Store, project_id: str) -> dict[str, Any]:
    project = get_project(store, project_id)
    if project is None:
        raise _problem(404, "not_found", "Project not found", f"项目 {project_id} 不存在")
    return project


def _require_document(store: Store, document_id: str, *, settings: Any) -> dict[str, Any]:
    doc = get_document(store, document_id)
    if doc is None:
        raise _problem(404, "not_found", "Document not found", f"文档 {document_id} 不存在")
    return _document_detail(doc, settings=settings)


def _document_summary(doc: dict[str, Any], *, settings: Any) -> dict[str, Any]:
    meta = _document_meta(doc, settings=settings)
    current = doc.get("current") or {}
    latest = doc.get("latest_revision") or {}
    meta["current_version"] = current.get("version")
    meta["latest_version"] = latest.get("version")
    meta["has_candidate"] = bool(current and latest and current.get("id") != latest.get("id"))
    return meta


# --------------------------------------------------------------------------- #
# 元信息 / 项目
# --------------------------------------------------------------------------- #
@router.get("/meta")
def api_meta() -> dict[str, Any]:
    settings = get_settings()
    return {
        "app": settings.app_name,
        "version": "1.0.0",
        "env": settings.env,
        "publish_targets": configured_targets(settings),
        "public_base_url": settings.public_base_url,
    }


@router.get("/projects")
def list_projects_endpoint(store: Store = StoreDep) -> list[dict[str, Any]]:
    settings = get_settings()
    result: list[dict[str, Any]] = []
    for project in list_projects(store):
        docs = list_documents(store, project["id"])
        runs = list_runs(store, project["id"], limit=1)
        result.append(
            {
                "id": project["id"],
                "slug": project["slug"],
                "name": project["name"],
                "provider": project["provider"],
                "repo_full_name": project["repo_full_name"],
                "repo_url": project.get("repo_url"),
                "default_branch": project.get("default_branch"),
                "is_active": bool(project.get("is_active")),
                "created_at": _iso(project.get("created_at")),
                "document_count": len(docs),
                "pending_count": sum(
                    1 for d in docs if d.get("manual_edited") and d.get("status") == "stale"
                ),
                "last_run": _run_payload(runs[0]) if runs else None,
                "review_url": review_url(settings.public_base_url, project_id=project["id"]),
            }
        )
    return result


@router.get("/projects/{project_id}")
def get_project_endpoint(project_id: str, store: Store = StoreDep) -> dict[str, Any]:
    settings = get_settings()
    project = _require_project(store, project_id)
    return {
        **{
            k: project.get(k)
            for k in (
                "id", "slug", "name", "provider", "repo_full_name", "repo_url",
                "default_branch", "is_active",
            )
        },
        "created_at": _iso(project.get("created_at")),
        "review_url": review_url(settings.public_base_url, project_id=project_id),
    }


def _preview_text(content: str, limit: int = 180) -> str:
    """给总览页的文档卡片一段“可读的摘要”。

    不直接把源码塞进 HTML：PlantUML 的 @startuml、skinparam、class 花括号
    对读者没有信息量，留在卡片里只会显得吵。这里做一次轻量清洗，
    真正的全文仍然由详情接口提供。
    """

    text = re.sub(r"(?m)^\s*@(?:start|end)uml\s*$", " ", content)
    text = re.sub(
        r"(?m)^\s*(?:skinparam|hide|show|title|note|participant|actor|database|"
        r"package|namespace|class\s+\w+\s*\{).*$",
        " ",
        text,
    )
    text = re.sub(r"(?m)^\s*#+\s*", "", text)
    text = re.sub(r"[|{}<>]", " ", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _module_counts(documents: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"class_diagram": 0, "sequence_diagram": 0, "api_doc": 0}
    for doc in documents:
        key = doc.get("doc_type")
        if key in counts:
            counts[key] += 1
    return counts


@router.get("/projects/{project_id}/overview")
def project_overview_endpoint(project_id: str, store: Store = StoreDep) -> dict[str, Any]:
    """架构总览页的一次性数据源。

    前端如果按“项目 → 文档 → 每个文档详情”串行拉，首屏会有明显的瀑布；
    这里把模块分组、统计、最近 run 和文档摘要聚合到一个响应里。
    文档正文只给 preview，点开卡片时再取全文。
    """

    settings = get_settings()
    project = _require_project(store, project_id)
    docs = list_documents(store, project_id)
    runs = list_runs(store, project_id, limit=12)

    modules: dict[str, dict[str, Any]] = {}
    type_counts = {"class_diagram": 0, "sequence_diagram": 0, "api_doc": 0}
    pending = 0

    for doc in docs:
        detail = get_document(store, doc["id"])
        if detail is None:
            continue
        current = detail.get("current") or {}
        latest = detail.get("latest_revision") or {}
        doc_type = detail.get("doc_type") or "unknown"
        if doc_type in type_counts:
            type_counts[doc_type] += 1
        is_pending = bool(detail.get("manual_edited") and detail.get("status") == "stale")
        if is_pending:
            pending += 1
        item = {
            "id": detail["id"],
            "doc_type": doc_type,
            "title": detail.get("title") or detail.get("target_key") or "未命名文档",
            "target_key": detail.get("target_key") or "",
            "content_format": detail.get("content_format"),
            "status": detail.get("status"),
            "manual_edited": bool(detail.get("manual_edited")),
            "current_version": (current or {}).get("version"),
            "latest_version": (latest or {}).get("version"),
            "has_candidate": bool(detail.get("has_candidate")),
            "preview": _preview_text(current.get("content") or ""),
            "review_url": detail.get("review_url"),
            "updated_at": detail.get("updated_at"),
        }
        key = item["target_key"] or "未分组"
        bucket = modules.setdefault(
            key,
            {"key": key, "label": key, "documents": [], "pending": 0},
        )
        bucket["documents"].append(item)
        if is_pending:
            bucket["pending"] += 1

    module_list = []
    for bucket in sorted(modules.values(), key=lambda m: str(m["key"]).casefold()):
        documents = sorted(bucket["documents"], key=lambda d: d["doc_type"])
        bucket["documents"] = documents
        bucket["count"] = len(documents)
        bucket["counts"] = _module_counts(documents)
        module_list.append(bucket)

    last_run = runs[0] if runs else None
    head_sha = str((last_run or {}).get("head_sha") or "")
    repo_full_name = project.get("repo_full_name") or ""
    repo_web = (project.get("repo_url") or f"https://github.com/{repo_full_name}").removesuffix(".git")
    head_url = f"https://github.com/{repo_full_name}/commit/{head_sha}" if head_sha else ""

    return {
        "project": {
            "id": project["id"],
            "slug": project.get("slug"),
            "name": project.get("name"),
            "provider": project.get("provider"),
            "repo_full_name": repo_full_name,
            "repo_url": project.get("repo_url"),
            "repo_web_url": repo_web,
            "default_branch": project.get("default_branch"),
            "review_url": review_url(settings.public_base_url, project_id=project_id),
        },
        "stats": {
            "documents": len(docs),
            "modules": len(module_list),
            "pending": pending,
            "tokens_out": sum(int(run.get("tokens_out") or 0) for run in runs),
            "document_types": type_counts,
        },
        "repo": {
            "full_name": repo_full_name,
            "web_url": repo_web,
            "branch": project.get("default_branch") or "",
            "head_sha": head_sha,
            "head_url": head_url,
            "docs_path": settings.github_docs_dir,
        },
        "modules": module_list,
        "last_run": _run_payload(last_run) if last_run else None,
        "runs": [_run_payload(run) for run in runs[:8]],
    }


@router.get("/projects/{project_id}/documents")
def list_documents_endpoint(
    project_id: str,
    doc_type: str | None = None,
    doc_status: str | None = Query(default=None, alias="status"),
    q: str | None = None,
    store: Store = StoreDep,
) -> list[dict[str, Any]]:
    settings = get_settings()
    _require_project(store, project_id)
    docs = list_documents(store, project_id, doc_type=doc_type)
    if doc_status:
        docs = [d for d in docs if d.get("status") == doc_status]
    if q:
        needle = q.casefold()
        docs = [
            d for d in docs
            if needle in (d.get("title") or "").casefold()
            or needle in (d.get("target_key") or "").casefold()
        ]
    result = []
    for doc in docs:
        detail = get_document(store, doc["id"])
        if detail:
            result.append(_document_summary(detail, settings=settings))
    return result


@router.get("/projects/{project_id}/runs")
def list_project_runs(
    project_id: str, limit: int = Query(default=30, ge=1, le=200), store: Store = StoreDep
) -> list[dict[str, Any]]:
    _require_project(store, project_id)
    return [_run_payload(row) for row in list_runs(store, project_id, limit=limit)]


# --------------------------------------------------------------------------- #
# 文档
# --------------------------------------------------------------------------- #
@router.get("/documents/{document_id}")
def get_document_endpoint(document_id: str, store: Store = StoreDep) -> dict[str, Any]:
    settings = get_settings()
    return _require_document(store, document_id, settings=settings)


@router.get("/documents/{document_id}/revisions")
def list_revisions_endpoint(
    document_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    store: Store = StoreDep,
) -> list[dict[str, Any]]:
    _require_document(store, document_id, settings=get_settings())
    return [
        _revision_payload(revision, include_content=False)
        for revision in list_revisions(store, document_id, limit=limit)
    ]


@router.get("/documents/{document_id}/revisions/{version}")
def get_revision_endpoint(document_id: str, version: int, store: Store = StoreDep) -> dict[str, Any]:
    _require_document(store, document_id, settings=get_settings())
    revision = get_revision(store, document_id, version)
    if revision is None:
        raise _problem(404, "not_found", "Revision not found", f"版本 v{version} 不存在")
    return _revision_payload(revision)


@router.get("/documents/{document_id}/diff")
def diff_endpoint(
    document_id: str,
    from_version: int | None = Query(default=None, alias="from", ge=1),
    to_version: int | None = Query(default=None, alias="to", ge=1),
    store: Store = StoreDep,
) -> dict[str, Any]:
    doc = _require_document(store, document_id, settings=get_settings())
    current_version = doc.get("current_version")
    revisions = {r["version"]: r for r in list_revisions(store, document_id, limit=200)}
    if not revisions:
        raise _problem(409, "conflict_state", "No revisions", "文档还没有任何版本")
    to_v = to_version or current_version or max(revisions)
    if to_v not in revisions:
        raise _problem(404, "not_found", "Revision not found", f"版本 v{to_v} 不存在")
    if from_version is None:
        earlier = [v for v in revisions if v < to_v]
        from_v = max(earlier) if earlier else to_v
    else:
        from_v = from_version
        if from_v not in revisions:
            raise _problem(404, "not_found", "Revision not found", f"版本 v{from_v} 不存在")

    before = (revisions[from_v].get("content") or "").splitlines()
    after = (revisions[to_v].get("content") or "").splitlines()
    lines = list(
        difflib.unified_diff(before, after, fromfile=f"v{from_v}", tofile=f"v{to_v}", lineterm="")
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return {
        "document_id": document_id,
        "from_version": from_v,
        "to_version": to_v,
        "identical": before == after,
        "added": added,
        "removed": removed,
        "unified_diff": "\n".join(lines),
    }


@router.put("/documents/{document_id}")
def save_manual_edit(
    document_id: str, body: ManualEditRequest, store: Store = StoreDep
) -> dict[str, Any]:
    settings = get_settings()
    doc = _require_document(store, document_id, settings=settings)
    raw = body.content.encode("utf-8")
    if len(raw) > _MAX_EDIT_BYTES:
        raise _problem(413, "payload_too_large", "Content too large", "人工编辑内容超过 512KB")

    normalized = normalize_content(body.content)
    if not normalized.strip():
        raise _problem(422, "invalid_payload", "Empty content", "内容不能为空")

    current = doc.get("current") or {}
    if normalized == (current.get("content") or "") and current:
        return {**doc, "changed": False}

    validation = validate_document(normalized, doc.get("content_format") or "markdown", doc["doc_type"])
    if not validation.ok and not body.force:
        raise _problem(
            422,
            "invalid_content",
            "Document failed validation",
            "内容未通过机器校验；确需保存请在请求体里传 force=true",
            extra={"diagnostics": diagnostic_payload(validation)},
        )

    try:
        outcome = mark_manual_edit(store, document_id, content=normalized, summary=body.summary or "人工编辑")
    except PublishError as exc:
        raise _problem(409, "conflict_state", "Cannot edit document", str(exc)) from exc

    detail = _require_document(store, document_id, settings=settings)
    return {
        **detail,
        "changed": True,
        "outcome": {"action": outcome.action, "version": outcome.version},
        "diagnostics": diagnostic_payload(validation),
    }


@router.post("/documents/{document_id}/accept-ai")
def accept_ai_endpoint(document_id: str, store: Store = StoreDep) -> dict[str, Any]:
    settings = get_settings()
    doc = _require_document(store, document_id, settings=settings)
    if not doc["has_candidate"]:
        raise _problem(409, "conflict_state", "No AI candidate", "当前没有待确认的 AI 候选版本")
    try:
        outcome = accept_ai(store, document_id)
    except PublishError as exc:
        raise _problem(409, "conflict_state", "Cannot accept candidate", str(exc)) from exc
    detail = _require_document(store, document_id, settings=settings)
    return {
        **detail,
        "outcome": {"action": outcome.action, "version": outcome.version},
    }


@router.post("/documents/{document_id}/render")
def render_endpoint(
    document_id: str,
    version: int | None = Query(default=None, ge=1),
    store: Store = StoreDep,
) -> dict[str, Any]:
    settings = get_settings()
    doc = _require_document(store, document_id, settings=settings)
    current = doc.get("current") or {}
    revision = current
    if version is not None:
        revision = get_revision(store, document_id, version) or {}
        if not revision:
            raise _problem(404, "not_found", "Revision not found", f"版本 v{version} 不存在")
    content = revision.get("content") or ""
    if not content:
        raise _problem(409, "conflict_state", "No content", "文档当前没有内容可渲染")

    content_format = doc.get("content_format") or "plantuml"
    validation = validate_document(content, content_format, doc["doc_type"])
    payload: dict[str, Any] = {
        "document_id": document_id,
        "version": revision.get("version"),
        "content_format": content_format,
        "render_ok": validation.ok,
        "renderer": "static",
        "diagnostics": diagnostic_payload(validation),
        "svg_url": None,
        "error": None,
    }
    if not validation.ok:
        return payload

    if content_format in {"plantuml", "puml"}:
        name, error = render_svg(content, settings)
        if name:
            path = f"/api/v1/renders/{name}"
            base = settings.public_base_url.rstrip("/")
            payload.update({"renderer": "plantuml", "svg_url": f"{base}{path}" if base else path})
        else:
            payload["error"] = error
    return payload


@router.get("/renders/{name}")
def get_render(name: str) -> FileResponse:
    if not _RENDER_NAME.match(name):
        raise _problem(404, "not_found", "Render not found", "非法的渲染文件名")
    path = get_settings().renders_dir / name
    if not path.exists():
        raise _problem(404, "not_found", "Render not found", "渲染结果不存在，请重新渲染")
    return FileResponse(path, media_type="image/svg+xml", filename=name)


@router.post("/documents/{document_id}/publish")
def publish_document_endpoint(
    document_id: str, body: PublishRequest | None = None, store: Store = StoreDep
) -> dict[str, Any]:
    settings = get_settings()
    doc = _require_document(store, document_id, settings=settings)
    if not (doc.get("current") or {}).get("content"):
        raise _problem(409, "conflict_state", "No content", "文档当前没有可发布的内容")

    targets = None
    if body and body.target:
        if body.target not in PUBLISHERS:
            raise _problem(
                422,
                "invalid_payload",
                "Unknown publish target",
                f"未知的投递目标 {body.target!r}；可选：{', '.join(sorted(PUBLISHERS))}",
            )
        targets = [body.target]

    action = "human_edit" if doc["manual_edited"] else "published"
    digest = digest_for_document(store, document_id, settings=settings, action=action)
    results = deliver_run(store, digest=digest, settings=settings, targets=targets)
    return {
        "document_id": document_id,
        "message": (body.message if body else None),
        "counts": digest.counts,
        "results": [
            {
                "target": r.target,
                "status": r.status,
                "external_id": r.external_id,
                "external_url": r.external_url,
                "error": r.error,
                "detail": r.detail,
            }
            for r in results
        ],
    }


# --------------------------------------------------------------------------- #
# Run / 审计
# --------------------------------------------------------------------------- #
@router.get("/runs")
def list_runs_endpoint(
    project_id: str | None = None,
    limit: int = Query(default=30, ge=1, le=200),
    store: Store = StoreDep,
) -> list[dict[str, Any]]:
    return [_run_payload(row) for row in list_runs(store, project_id, limit=limit)]


@router.get("/runs/{run_id}")
def get_run_endpoint(run_id: str, store: Store = StoreDep) -> dict[str, Any]:
    row = get_run(store, run_id)
    if row is None:
        raise _problem(404, "not_found", "Run not found", f"任务 {run_id} 不存在")
    invocations = [
        {
            "id": inv["id"],
            "document_id": inv.get("document_id"),
            "purpose": inv.get("purpose"),
            "provider": inv.get("provider"),
            "model": inv.get("model"),
            "prompt_version": inv.get("prompt_version"),
            "prompt_tokens": inv.get("prompt_tokens"),
            "completion_tokens": inv.get("completion_tokens"),
            "latency_ms": inv.get("latency_ms"),
            "ok": bool(inv.get("ok")),
            "error": inv.get("error"),
            "created_at": _iso(inv.get("created_at")),
        }
        for inv in list_invocations(store, run_id)
    ]
    records = list_publish_records(store, run_id=run_id, limit=100)
    return {
        **_run_payload(row),
        "invocations": invocations,
        "publish_records": [
            {
                "id": record["id"],
                "target": record["target"],
                "status": record["status"],
                "document_id": record.get("document_id"),
                "external_id": record.get("external_id"),
                "external_url": record.get("external_url"),
                "error_message": record.get("error_message"),
                "detail": record.get("detail"),
                "created_at": _iso(record.get("created_at")),
            }
            for record in records
        ],
    }


@router.get("/publish-records")
def list_publish_records_endpoint(
    run_id: str | None = None,
    document_id: str | None = None,
    project_id: str | None = None,
    target: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    store: Store = StoreDep,
) -> list[dict[str, Any]]:
    try:
        rows = list_publish_records(
            store, run_id=run_id, document_id=document_id, project_id=project_id, target=target, limit=limit
        )
    except SQLAlchemyError as exc:  # pragma: no cover - 表缺失时给出可读错误
        raise _problem(500, "internal_error", "Query failed", str(exc)[:300]) from exc
    return [
        {
            "id": row["id"],
            "run_id": row.get("run_id"),
            "project_id": row.get("project_id"),
            "document_id": row.get("document_id"),
            "revision_id": row.get("revision_id"),
            "target": row["target"],
            "status": row["status"],
            "external_id": row.get("external_id"),
            "external_url": row.get("external_url"),
            "error_message": row.get("error_message"),
            "detail": row.get("detail"),
            "created_at": _iso(row.get("created_at")),
        }
        for row in rows
    ]


__all__ = ["router"]