"""投递编排：从数据库拼出 RunDigest，按配置挨个调用适配器，落审计记录。

三条不可违反的规则：
1. **投递失败不能让 run 失败**。生成/校验/入库已经成功，PR 评论 502 是外部故障，
   把 run 判成 failed 会让看板上的成功率变成噪声；失败要进 publish_records。
2. **没配置就跳过，不要报错**。本地演示没有 GITHUB_TOKEN 是常态。
3. **适配器之间互不影响**。一个适配器抛异常，后面的照常执行。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from app.config import Settings, get_settings
from app.publish import get_document, latest_ai_revision, list_documents
from app.store import Store, get_project, get_run, record_publish
from app.publishers.base import (
    DocumentPayload,
    PublishResult,
    PublisherError,
    RunDigest,
    review_url,
)
from app.publishers.github_pr import GitHubPRCommentPublisher
from app.publishers.github_repo import GitHubRepoPublisher
from app.publishers.local_kb import LocalKnowledgeBasePublisher
from app.publishers.webhook import WebhookKnowledgeBasePublisher

logger = logging.getLogger(__name__)

#: 名字 -> 适配器。新增渠道只要在这里注册，配置里加个名字就能用。
PUBLISHERS = {
    "local_wiki": LocalKnowledgeBasePublisher(),
    "github_pr_comment": GitHubPRCommentPublisher(),
    "github_repo": GitHubRepoPublisher(),
    "webhook_kb": WebhookKnowledgeBasePublisher(),
}


def configured_targets(settings: Settings) -> list[str]:
    """配置里声明、且真的实现了的渠道（未知名字告警后忽略）。"""
    names: list[str] = []
    for name in settings.publish_target_names:
        if name in PUBLISHERS:
            names.append(name)
        else:
            logger.warning("未知的发布渠道 %r，已忽略（可用：%s）", name, ", ".join(PUBLISHERS))
    return names


def _payload_from_row(store: Store, row: Mapping[str, Any], *, action: str) -> DocumentPayload:
    """把 documents 行 + 当前版本 + 候选版本拼成投递载荷。"""
    doc = get_document(store, row["id"]) or dict(row)
    current = doc.get("current") or {}
    candidate = None
    if doc.get("status") == "stale" or action == "deferred_stale":
        latest = latest_ai_revision(store, row["id"])
        if latest is not None and latest["id"] != doc.get("current_revision_id"):
            candidate = latest

    return DocumentPayload(
        document_id=doc["id"],
        doc_type=doc["doc_type"],
        target_key=doc["target_key"],
        title=doc["title"],
        content=current.get("content") or "",
        content_format=doc["content_format"],
        status=doc.get("status") or "draft",
        action=action,
        version=current.get("version"),
        revision_id=current.get("id"),
        manual_edited=bool(doc.get("manual_edited")),
        render_ok=current.get("render_ok") is not False,
        candidate_content=(candidate or {}).get("content"),
        candidate_version=(candidate or {}).get("version"),
        candidate_revision_id=(candidate or {}).get("id"),
    )


def _collect_documents(
    store: Store, project_id: str, actions: Mapping[str, str]
) -> tuple[DocumentPayload, ...]:
    payloads = []
    for row in list_documents(store, project_id):
        payloads.append(_payload_from_row(store, row, action=actions.get(row["id"], "unchanged")))
    return tuple(payloads)


def build_digest(
    store: Store,
    *,
    run_id: str,
    report: Any = None,
    artifact: Mapping[str, Any] | None = None,
    settings: Settings | None = None,
    tokens: int = 0,
) -> RunDigest:
    """从 run + 发布报告构造投递摘要。`report` 可空（例如手工重发）。"""
    settings = settings or get_settings()
    run = get_run(store, run_id)
    if run is None:
        raise ValueError(f"run {run_id} 不存在")
    project = get_project(store, run["project_id"])
    if project is None:
        raise ValueError(f"project {run['project_id']} 不存在")

    actions: dict[str, str] = {}
    rejected: tuple[tuple[str, str], ...] = ()
    stats: dict[str, Any] = {}
    if report is not None:
        actions = {o.document_id: o.action for o in report.outcomes}
        rejected = tuple(report.rejected)
        stats.update(report.summary())

    artifact = artifact or {}
    pr_number = artifact.get("pr_number") or artifact.get("number")
    try:
        pr_number = int(pr_number) if pr_number is not None else None
    except (TypeError, ValueError):
        pr_number = None

    if tokens:
        stats["tokens"] = int(tokens)

    return RunDigest(
        run_id=run_id,
        project_id=project["id"],
        project_slug=project["slug"],
        project_name=project["name"],
        repo_full_name=project["repo_full_name"],
        trigger_type=run["trigger_type"],
        trigger_ref=run.get("trigger_ref"),
        base_sha=run.get("base_sha") or "",
        head_sha=run.get("head_sha") or "",
        pr_number=pr_number,
        documents=_collect_documents(store, project["id"], actions),
        rejected=rejected,
        stats=stats,
        review_url=review_url(settings.public_base_url, project_id=project["id"]),
    )


def digest_for_document(
    store: Store,
    document_id: str,
    *,
    settings: Settings | None = None,
    action: str = "accepted",
) -> RunDigest:
    """单份文档重发（评审界面点「重新发布」）：索引仍覆盖全项目。"""
    settings = settings or get_settings()
    doc = get_document(store, document_id)
    if doc is None:
        raise ValueError(f"document {document_id} 不存在")
    project = get_project(store, doc["project_id"])
    if project is None:
        raise ValueError(f"project {doc['project_id']} 不存在")

    run = get_run(store, doc["owner_run_id"]) if doc.get("owner_run_id") else None
    return RunDigest(
        run_id=(run or {}).get("id") or f"manual-{document_id[:8]}",
        project_id=project["id"],
        project_slug=project["slug"],
        project_name=project["name"],
        repo_full_name=project["repo_full_name"],
        trigger_type=(run or {}).get("trigger_type") or "manual",
        trigger_ref=(run or {}).get("trigger_ref"),
        base_sha=(run or {}).get("base_sha") or "",
        head_sha=(run or {}).get("head_sha") or "",
        documents=_collect_documents(store, project["id"], {document_id: action}),
        stats={"manual": True, "documents": 1},
        review_url=review_url(settings.public_base_url, project_id=project["id"]),
    )


def _record(store: Store, digest: RunDigest, result: PublishResult) -> None:
    try:
        record_publish(
            store,
            target=result.target,
            status=result.status,
            run_id=digest.run_id if not digest.run_id.startswith("manual-") else None,
            project_id=digest.project_id,
            document_id=result.document_id,
            revision_id=result.revision_id,
            external_id=result.external_id,
            external_url=result.external_url,
            error_message=result.error,
            detail=dict(result.detail),
        )
    except Exception:  # 审计写失败不能反过来影响投递结果
        logger.exception("写 publish_records 失败（run=%s, target=%s）", digest.run_id, result.target)


def deliver_run(
    store: Store,
    *,
    digest: RunDigest,
    settings: Settings | None = None,
    targets: Sequence[str] | None = None,
) -> list[PublishResult]:
    """按配置投递，所有异常都被收敛进 PublishResult。"""
    settings = settings or get_settings()
    names = list(targets) if targets is not None else configured_targets(settings)
    results: list[PublishResult] = []

    for name in names:
        publisher = PUBLISHERS.get(name)
        if publisher is None:
            result = PublishResult(target=name, status="skipped", error="unknown publisher")
            _record(store, digest, result)
            results.append(result)
            continue
        if not publisher.available(settings):
            result = PublishResult(
                target=name,
                status="skipped",
                error="publisher not configured (missing token/url)",
            )
            _record(store, digest, result)
            results.append(result)
            continue

        try:
            batch = list(publisher.publish(digest, settings))
        except PublisherError as exc:
            batch = [PublishResult(target=name, status="failed", error=str(exc))]
        except Exception as exc:  # 适配器 bug 也不能拖垮整次投递
            logger.exception("发布渠道 %s 异常", name)
            batch = [
                PublishResult(
                    target=name,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            ]
        for result in batch:
            _record(store, digest, result)
        results.extend(batch)

    return results