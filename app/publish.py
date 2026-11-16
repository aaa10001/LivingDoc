"""文档发布：把一份 AI 草稿变成 documents / document_revisions 里的一行。

这里承载 ADR-006（人工修改保护）的全部逻辑，也是整个系统最容易写错的地方：
"自动更新"与"人改过的文档"是同一张表上的两种写意图，处理顺序错了就会静默覆盖人的修改。

三条规则：
1. 内容没变就不产生新版本（否则每次 push 都 +1，版本号失去意义，也无法回答"这份文档这周改过吗"）。
2. manual_edited=True 的文档，AI 只能写"候选版本"（stale），current_revision_id 不动；
   人调 accept_ai 之后才切换。检测并让路，不自动合并。
3. version 在事务内生成 + 唯一索引兜底；冲突就重试，不做"先查后写"的乐观假设。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.store import Store, documents, document_revisions

# 并发编辑同一文档时，version 冲突是正常竞争而不是错误。3 次在 Windows 单机够用，
# 但 Linux 容器 + 4 个 TestClient 线程实测会连续撞到第 4 次；给到 8 次，退避仍然很短。
MAX_PUBLISH_ATTEMPTS = 8

#: 草稿扩展名 -> 文档格式（与 database.md 的 content_format CHECK 对齐）
FORMAT_BY_EXTENSION = {".puml": "plantuml", ".md": "markdown", ".mmd": "mermaid"}


class PublishError(RuntimeError):
    """发布失败且重试无意义（例如要接受的文档根本没有任何 AI 版本）。"""


@dataclass(frozen=True)
class PublishOutcome:
    document_id: str
    action: str           # created | updated | unchanged | deferred_stale | accepted | human_edit | flagged
    status: str
    revision_id: int | None = None
    version: int | None = None
    manual_edited: bool = False

    @property
    def changed(self) -> bool:
        return self.action in ("created", "updated", "accepted", "human_edit")

    @property
    def deferred(self) -> bool:
        return self.action == "deferred_stale"


def normalize_content(content: str) -> str:
    """统一换行、去掉尾部空白、保证以单个换行结尾。

    不同平台的编辑器会在文件末尾加/不加换行；不归一化的话，同样的内容会被判成"变了"，
    每次 run 都产生一个空版本。
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip() + "\n"


def content_hash(content: str) -> str:
    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _latest(conn, document_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        select(document_revisions)
        .where(document_revisions.c.document_id == document_id)
        .order_by(document_revisions.c.version.desc())
        .limit(1)
    ).mappings().first()
    return dict(row) if row else None


def _latest_ai(conn, document_id: str) -> dict[str, Any] | None:
    """最近一个 AI 版本。人改过的文档要拿它比内容，而不是拿人改的那版比——
    否则每次重跑都会把同一份 AI 内容当成"新候选"再存一遍。"""
    row = conn.execute(
        select(document_revisions)
        .where(
            document_revisions.c.document_id == document_id,
            document_revisions.c.created_by.in_(("ai", "ai+human")),
        )
        .order_by(document_revisions.c.version.desc())
        .limit(1)
    ).mappings().first()
    return dict(row) if row else None


def _next_version(conn, document_id: str) -> int:
    current = conn.execute(
        select(func.max(document_revisions.c.version)).where(
            document_revisions.c.document_id == document_id
        )
    ).scalar()
    return int(current or 0) + 1


def publish_document(
    store: Store,
    *,
    project_id: str,
    doc_type: str,
    target_key: str,
    title: str,
    content: str,
    run_id: str | None = None,
    content_format: str | None = None,
    extension: str = ".puml",
    render_ok: bool = True,
    model_name: str = "",
    prompt_version: str = "",
    change_summary: str | None = None,
    source_entity_ids: list[int] | None = None,
) -> PublishOutcome:
    """写入一份文档的 AI 版本，按 ADR-006 决定是生效还是挂起。"""
    normalized = normalize_content(content)
    digest = content_hash(normalized)
    fmt = content_format or FORMAT_BY_EXTENSION.get(extension, "markdown")

    last_error: IntegrityError | None = None
    for _ in range(MAX_PUBLISH_ATTEMPTS):
        try:
            with store.begin() as conn:
                doc = conn.execute(
                    select(documents).where(
                        documents.c.project_id == project_id,
                        documents.c.doc_type == doc_type,
                        documents.c.target_key == target_key,
                    )
                ).mappings().first()

                if doc is None:
                    document_id = _insert_document(
                        conn, project_id=project_id, doc_type=doc_type, target_key=target_key,
                        title=title, content_format=fmt, run_id=run_id, render_ok=render_ok,
                    )
                    version = 1
                    revision_id = _insert_revision(
                        conn, document_id=document_id, version=version, content=normalized,
                        digest=digest, run_id=run_id, render_ok=render_ok, model_name=model_name,
                        prompt_version=prompt_version, change_summary=change_summary,
                        source_entity_ids=source_entity_ids,
                    )
                    conn.execute(
                        update(documents).where(documents.c.id == document_id).values(
                            current_revision_id=revision_id,
                            status="published" if render_ok else "draft",
                            updated_at=_utcnow(),
                        )
                    )
                    return PublishOutcome(document_id, "created", "published" if render_ok else "draft",
                                          revision_id, version, False)

                document_id = doc["id"]
                current = _latest(conn, document_id)

                # 规则 1：内容没变 -> 不产生版本
                if current and current["content_hash"] == digest and not doc["manual_edited"]:
                    return PublishOutcome(document_id, "unchanged", doc["status"],
                                          current["id"], current["version"], False)

                # 人改过的文档：拿"上一个 AI 候选"比，避免重复堆同一份候选
                if doc["manual_edited"]:
                    candidate = _latest_ai(conn, document_id)
                    if candidate and candidate["content_hash"] == digest:
                        return PublishOutcome(document_id, "unchanged", doc["status"],
                                              candidate["id"], candidate["version"], True)

                version = _next_version(conn, document_id)
                revision_id = _insert_revision(
                    conn, document_id=document_id, version=version, content=normalized,
                    digest=digest, run_id=run_id, render_ok=render_ok, model_name=model_name,
                    prompt_version=prompt_version, change_summary=change_summary,
                    source_entity_ids=source_entity_ids,
                )

                if doc["manual_edited"]:
                    # 规则 2：人改过 -> 只挂候选，不改 current
                    conn.execute(
                        update(documents).where(documents.c.id == document_id).values(
                            status="stale",
                            owner_run_id=run_id,
                            updated_at=_utcnow(),
                        )
                    )
                    return PublishOutcome(document_id, "deferred_stale", "stale",
                                          revision_id, version, True)

                conn.execute(
                    update(documents).where(documents.c.id == document_id).values(
                        current_revision_id=revision_id,
                        status="published" if render_ok else "draft",
                        owner_run_id=run_id,
                        updated_at=_utcnow(),
                    )
                )
                return PublishOutcome(document_id, "updated",
                                      "published" if render_ok else "draft",
                                      revision_id, version, False)
        except IntegrityError as exc:
            # 版本号或文档唯一键被并发写者抢占：重来一次，下次读到的 max(version) 已经变了
            last_error = exc
            continue

    raise PublishError(f"发布冲突，重试 {MAX_PUBLISH_ATTEMPTS} 次仍失败: {last_error}")


def _insert_document(conn, *, project_id: str, doc_type: str, target_key: str, title: str,
                     content_format: str, run_id: str | None, render_ok: bool) -> str:
    import uuid

    document_id = str(uuid.uuid4())
    conn.execute(
        insert(documents).values(
            id=document_id,
            project_id=project_id,
            doc_type=doc_type,
            target_key=target_key,
            title=title[:255],
            content_format=content_format,
            status="published" if render_ok else "draft",
            owner_run_id=run_id,
        )
    )
    return document_id


def _insert_revision(conn, *, document_id: str, version: int, content: str, digest: str,
                     run_id: str | None, render_ok: bool, model_name: str,
                     prompt_version: str, change_summary: str | None,
                     source_entity_ids: list[int] | None,
                     created_by: str = "ai") -> int:
    result = conn.execute(
        insert(document_revisions).values(
            document_id=document_id,
            version=version,
            content=content,
            content_hash=digest,
            change_summary=change_summary,
            render_ok=render_ok,
            render_error=None if render_ok else "validation failed",
            source_entity_ids=list(source_entity_ids or []),
            model_name=model_name or None,
            prompt_version=prompt_version or None,
            created_by=created_by,
        )
    )
    return int(result.inserted_primary_key[0])


# --------------------------------------------------------------------------- #
# 人的一侧：接受 AI 候选 / 标记人工编辑
# --------------------------------------------------------------------------- #
def get_document(store: Store, document_id: str) -> dict[str, Any] | None:
    with store.begin() as conn:
        row = conn.execute(select(documents).where(documents.c.id == document_id)).mappings().first()
        if row is None:
            return None
        doc = dict(row)
        current = None
        if doc["current_revision_id"]:
            current = conn.execute(
                select(document_revisions).where(
                    document_revisions.c.id == doc["current_revision_id"]
                )
            ).mappings().first()
        doc["current"] = dict(current) if current else None
        latest = _latest(conn, document_id)
        doc["latest_revision"] = latest
        return doc


def list_documents(store: Store, project_id: str, *, doc_type: str | None = None) -> list[dict[str, Any]]:
    query = select(documents).where(documents.c.project_id == project_id)
    if doc_type:
        query = query.where(documents.c.doc_type == doc_type)
    query = query.order_by(documents.c.doc_type, documents.c.target_key)
    with store.begin() as conn:
        rows = conn.execute(query).mappings().all()
    return [dict(row) for row in rows]


def list_revisions(store: Store, document_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """版本历史（新到旧）。评审页的"历史"标签直接用。"""
    with store.begin() as conn:
        rows = conn.execute(
            select(document_revisions)
            .where(document_revisions.c.document_id == document_id)
            .order_by(document_revisions.c.version.desc())
            .limit(max(1, min(limit, 200)))
        ).mappings().all()
    return [dict(row) for row in rows]


def get_revision(store: Store, document_id: str, version: int) -> dict[str, Any] | None:
    with store.begin() as conn:
        row = conn.execute(
            select(document_revisions).where(
                document_revisions.c.document_id == document_id,
                document_revisions.c.version == version,
            )
        ).mappings().first()
    return dict(row) if row else None


def latest_ai_revision(store: Store, document_id: str) -> dict[str, Any] | None:
    """公开版 _latest_ai；发布适配器需要它拿"待确认候选"的内容。"""
    with store.begin() as conn:
        return _latest_ai(conn, document_id)


def extension_for_format(content_format: str) -> str:
    for extension, fmt in FORMAT_BY_EXTENSION.items():
        if fmt == content_format:
            return extension
    return ".txt"


def accept_ai(store: Store, document_id: str) -> PublishOutcome:
    """人确认"这份 AI 候选可以生效"。

    注意：接受之后 manual_edited 仍然是 True。接受 AI 内容是人做的一次动作，
    不是解除保护——下次 AI 更新还是要人再看一眼。保护标志只在显式 API 调用时解除。
    """
    with store.begin() as conn:
        doc = conn.execute(select(documents).where(documents.c.id == document_id)).mappings().first()
        if doc is None:
            raise PublishError(f"文档 {document_id} 不存在")
        candidate = _latest(conn, document_id)
        if candidate is None or candidate["created_by"] not in ("ai", "ai+human"):
            raise PublishError("没有可接受的 AI 版本")
        if doc["current_revision_id"] == candidate["id"]:
            return PublishOutcome(document_id, "unchanged", doc["status"],
                                  candidate["id"], candidate["version"], doc["manual_edited"])
        conn.execute(
            update(documents).where(documents.c.id == document_id).values(
                current_revision_id=candidate["id"],
                status="published" if candidate["render_ok"] is not False else "draft",
                updated_at=_utcnow(),
            )
        )
        return PublishOutcome(document_id, "accepted", "published",
                              candidate["id"], candidate["version"], doc["manual_edited"])


def mark_manual_edit(store: Store, document_id: str, *, content: str | None = None,
                     summary: str | None = None) -> PublishOutcome:
    """把文档标记为"人动过"。带 content 时同时写入一个 human 版本。

    并发编辑会同时读到同一个 max(version)，于是抢同一个 version。这里和
    publish_document 一样用"重算 + 重试"兜住：唯一约束是最后一道防线，
    不能把 500 甩给评审页面。
    """
    if content is None:
        with store.begin() as conn:
            doc = conn.execute(select(documents).where(documents.c.id == document_id)).mappings().first()
            if doc is None:
                raise PublishError(f"文档 {document_id} 不存在")
            conn.execute(
                update(documents).where(documents.c.id == document_id).values(
                    manual_edited=True, updated_at=_utcnow()
                )
            )
            return PublishOutcome(document_id, "flagged", doc["status"], None, None, True)

    normalized = normalize_content(content)
    last_error: IntegrityError | None = None
    for attempt in range(1, MAX_PUBLISH_ATTEMPTS + 1):
        try:
            with store.begin() as conn:
                doc = conn.execute(
                    select(documents).where(documents.c.id == document_id)
                ).mappings().first()
                if doc is None:
                    raise PublishError(f"文档 {document_id} 不存在")

                version = _next_version(conn, document_id)
                revision_id = _insert_revision(
                    conn, document_id=document_id, version=version, content=normalized,
                    digest=content_hash(normalized), run_id=None, render_ok=True,
                    model_name="", prompt_version="", change_summary=summary,
                    source_entity_ids=None, created_by="human",
                )
                conn.execute(
                    update(documents).where(documents.c.id == document_id).values(
                        manual_edited=True,
                        current_revision_id=revision_id,
                        status="published",
                        updated_at=_utcnow(),
                    )
                )
                return PublishOutcome(
                    document_id, "human_edit", "published", revision_id, version, True
                )
        except IntegrityError as exc:
            # 另一个编辑者刚插入了同一个 version；重新读 max(version) 再来。
            last_error = exc
            time.sleep(0.01 * attempt)
    raise PublishError(f"文档 {document_id} 的版本号连续冲突 {MAX_PUBLISH_ATTEMPTS} 次") from last_error
