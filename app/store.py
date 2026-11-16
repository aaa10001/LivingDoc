"""持久层：一次分析的状态、文档与版本。

为什么是 SQLAlchemy Core 而不是 ORM：这一层只有六张表、十几条语句，
用 Core 可以一眼看出每条语句打到哪张表、走哪个索引；ORM 的 session 生命周期
（detached、lazy load、autoflush）在 Celery 线程池里只会增加不确定性。
docs/architecture/database.md 里的 PostgreSQL DDL 是生产形态，这里是与它对齐的
SQLite 可运行子集：uuid 存 char(36)、数组存 JSON、时间统一存 UTC。
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()

RUN_STATUSES = (
    "queued", "cloning", "diffing", "parsing", "analyzing",
    "rendering", "publishing", "succeeded", "failed", "canceled",
    "superseded", "skipped",
)
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "superseded", "skipped"})

#: 状态机允许的迁移。写成表而不是若干 if，是为了让"非法迁移"在测试里可枚举。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"cloning", "diffing", "parsing", "failed", "canceled", "skipped", "superseded"}),
    "cloning": frozenset({"diffing", "parsing", "failed", "canceled", "superseded"}),
    "diffing": frozenset({"parsing", "analyzing", "failed", "canceled", "superseded", "skipped"}),
    "parsing": frozenset({"analyzing", "failed", "canceled", "superseded", "skipped"}),
    # analyzing -> skipped：分析阶段就发现没配 LLM key / 没有可生成内容，不必继续往下走
    "analyzing": frozenset({"rendering", "publishing", "failed", "canceled", "superseded", "skipped"}),
    "rendering": frozenset({"publishing", "failed", "canceled", "superseded"}),
    "publishing": frozenset({"succeeded", "failed", "canceled"}),
}
for _terminal in TERMINAL_STATUSES:
    ALLOWED_TRANSITIONS[_terminal] = frozenset()


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 会把时区吃掉，读回来是 naive；比较前补上 UTC，否则 TypeError。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 表定义（与 database.md 的 P0 表一一对应，去掉 PG 专有类型）
# --------------------------------------------------------------------------- #
projects = Table(
    "projects", metadata,
    Column("id", String(36), primary_key=True, default=_uuid),
    Column("slug", String(64), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    Column("provider", String(16), nullable=False),
    Column("repo_full_name", String(255), nullable=False),
    Column("repo_url", String(512), nullable=False, default=""),
    Column("default_branch", String(128), nullable=False, default="main"),
    Column("webhook_secret_enc", Text, nullable=False, default=""),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False, default=_utcnow),
    UniqueConstraint("provider", "repo_full_name", name="uq_project_repo"),
)

analysis_runs = Table(
    "analysis_runs", metadata,
    Column("id", String(36), primary_key=True, default=_uuid),
    Column("project_id", String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("trigger_type", String(16), nullable=False, default="push"),
    Column("trigger_ref", String(255)),
    Column("base_sha", String(64)),
    Column("head_sha", String(64), nullable=False),
    Column("status", String(20), nullable=False, default="queued"),
    Column("stage_detail", Text),
    Column("error_code", String(64)),
    Column("error_message", Text),
    Column("files_changed", Integer, nullable=False, default=0),
    Column("entities_changed", Integer, nullable=False, default=0),
    Column("tokens_in", Integer, nullable=False, default=0),
    Column("tokens_out", Integer, nullable=False, default=0),
    Column("idempotency_key", String(200), nullable=False),
    Column("attempt", Integer, nullable=False, default=1),
    Column("queued_at", DateTime(timezone=True), nullable=False, default=_utcnow),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint("project_id", "idempotency_key", name="uq_run_idempotent"),
)

documents = Table(
    "documents", metadata,
    Column("id", String(36), primary_key=True, default=_uuid),
    Column("project_id", String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("doc_type", String(32), nullable=False),
    Column("target_key", String(512), nullable=False),
    Column("title", String(255), nullable=False),
    Column("content_format", String(16), nullable=False),
    Column("current_revision_id", Integer),
    Column("status", String(20), nullable=False, default="draft"),
    Column("manual_edited", Boolean, nullable=False, default=False),
    Column("owner_run_id", String(36), ForeignKey("analysis_runs.id", ondelete="SET NULL")),
    Column("created_at", DateTime(timezone=True), nullable=False, default=_utcnow),
    Column("updated_at", DateTime(timezone=True), nullable=False, default=_utcnow),
    UniqueConstraint("project_id", "doc_type", "target_key", name="uq_document_target"),
)

document_revisions = Table(
    "document_revisions", metadata,
    Column("id", Integer, primary_key=True),  # SQLite 只有 INTEGER PRIMARY KEY 才会自增
    Column("document_id", String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("change_summary", Text),
    Column("render_ok", Boolean),
    Column("render_error", Text),
    Column("source_entity_ids", JSON, nullable=False, default=list),
    Column("model_name", String(64)),
    Column("prompt_version", String(32)),
    Column("created_by", String(16), nullable=False, default="ai"),
    Column("created_at", DateTime(timezone=True), nullable=False, default=_utcnow),
    UniqueConstraint("document_id", "version", name="uq_revision_version"),
)

ai_invocations = Table(
    "ai_invocations", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", String(36), ForeignKey("analysis_runs.id", ondelete="CASCADE")),
    Column("document_id", String(36), ForeignKey("documents.id", ondelete="SET NULL")),
    Column("purpose", String(48), nullable=False),
    Column("provider", String(32), nullable=False, default="deepseek"),
    Column("model", String(64), nullable=False, default=""),
    Column("prompt_version", String(32), nullable=False, default=""),
    Column("prompt_tokens", Integer, nullable=False, default=0),
    Column("completion_tokens", Integer, nullable=False, default=0),
    Column("latency_ms", Integer, nullable=False, default=0),
    Column("ok", Boolean, nullable=False, default=True),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, default=_utcnow),
)


# --------------------------------------------------------------------------- #
# 发布记录
# --------------------------------------------------------------------------- #
publish_records = Table(
    "publish_records", metadata,
    Column("id", Integer, primary_key=True),
    # run 级记录（例如一条 PR 评论）没有单份文档，所以 document_id 可空
    Column("run_id", String(36), ForeignKey("analysis_runs.id", ondelete="SET NULL")),
    Column("project_id", String(36), ForeignKey("projects.id", ondelete="CASCADE")),
    Column("document_id", String(36), ForeignKey("documents.id", ondelete="SET NULL")),
    Column("revision_id", Integer, ForeignKey("document_revisions.id", ondelete="SET NULL")),
    Column("target", String(32), nullable=False),
    Column("status", String(16), nullable=False, default="success"),
    Column("external_id", String(255)),
    Column("external_url", String(1024)),
    Column("error_message", Text),
    Column("detail", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False, default=_utcnow),
)


class InvalidTransition(RuntimeError):
    """试图让 run 走一条状态机里不存在的边。宁可炸，也不要静默跳过。"""


class Store:
    """一个 Engine 的薄包装。连接池交给 SQLAlchemy，事务边界由调用方决定。"""

    def __init__(self, url: str | None = None, *, echo: bool = False) -> None:
        if url is None:
            from app.config import get_settings

            url = get_settings().database_url
        self.url = url
        connect_args: dict[str, Any] = {}
        if url.startswith("sqlite"):
            # Celery threads 池里多线程共用一个文件；SQLite 默认 check_same_thread=True 会直接报错
            connect_args = {"check_same_thread": False, "timeout": 30}
        self.engine: Engine = create_engine(
            url, echo=echo, future=True, connect_args=connect_args, pool_pre_ping=True
        )
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - 连接级副作用
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

    def create_schema(self) -> None:
        metadata.create_all(self.engine)

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    def close(self) -> None:
        self.engine.dispose()


def open_store(url: str | None = None) -> Store:
    store = Store(url)
    store.create_schema()
    return store


# --------------------------------------------------------------------------- #
# projects / analysis_runs
# --------------------------------------------------------------------------- #
def ensure_project(
    store: Store,
    *,
    provider: str,
    repo_full_name: str,
    repo_url: str = "",
    name: str | None = None,
) -> str:
    """按 (provider, repo_full_name) 幂等取项目；不存在就建。

    并发下两个 worker 可能同时插：唯一索引会拒绝后来者，届时读回已有行即可。
    """
    with store.begin() as conn:
        row = conn.execute(
            select(projects.c.id).where(
                projects.c.provider == provider,
                projects.c.repo_full_name == repo_full_name,
            )
        ).first()
        if row:
            return row[0]

        slug = repo_full_name.replace("/", "-").lower()[:64]
        values = {
            "id": _uuid(),
            "slug": slug,
            "name": name or repo_full_name.split("/")[-1],
            "provider": provider,
            "repo_full_name": repo_full_name,
            "repo_url": repo_url,
        }
        try:
            conn.execute(insert(projects).values(**values))
            return values["id"]
        except IntegrityError:
            row = conn.execute(
                select(projects.c.id).where(
                    projects.c.provider == provider,
                    projects.c.repo_full_name == repo_full_name,
                )
            ).first()
            if row is None:  # pragma: no cover - 唯一索引冲突后行必须存在
                raise
            return row[0]


def get_project(store: Store, project_id: str) -> dict[str, Any] | None:
    with store.begin() as conn:
        row = conn.execute(select(projects).where(projects.c.id == project_id)).mappings().first()
    return dict(row) if row else None


def run_idempotency_key(project_id: str, base_sha: str, head_sha: str, trigger_type: str) -> str:
    raw = f"{project_id}|{base_sha}|{head_sha}|{trigger_type}".encode()
    return hashlib.sha256(raw).hexdigest()


def start_run(
    store: Store,
    *,
    project_id: str,
    base_sha: str,
    head_sha: str,
    trigger_type: str = "push",
    trigger_ref: str | None = None,
) -> tuple[str, bool]:
    """创建（或复用）一次 run，返回 (run_id, created)。

    复用规则：同一 project + 同一 base/head + 同一触发类型 = 同一次分析。
    重复投递不该产生第二个 run，否则文档会被两套并行任务互相覆盖。
    """
    key = run_idempotency_key(project_id, base_sha, head_sha, trigger_type)
    with store.begin() as conn:
        row = conn.execute(
            select(analysis_runs.c.id).where(
                analysis_runs.c.project_id == project_id,
                analysis_runs.c.idempotency_key == key,
            )
        ).first()
        if row:
            return row[0], False
        run_id = _uuid()
        try:
            conn.execute(
                insert(analysis_runs).values(
                    id=run_id,
                    project_id=project_id,
                    trigger_type=trigger_type,
                    trigger_ref=trigger_ref,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    status="queued",
                    idempotency_key=key,
                )
            )
            return run_id, True
        except IntegrityError:
            row = conn.execute(
                select(analysis_runs.c.id).where(
                    analysis_runs.c.project_id == project_id,
                    analysis_runs.c.idempotency_key == key,
                )
            ).first()
            if row is None:  # pragma: no cover
                raise
            return row[0], False


def get_run(store: Store, run_id: str) -> dict[str, Any] | None:
    with store.begin() as conn:
        row = conn.execute(select(analysis_runs).where(analysis_runs.c.id == run_id)).mappings().first()
    return dict(row) if row else None


def list_projects(store: Store) -> list[dict[str, Any]]:
    """评审界面左栏用。按创建时间倒序，没有项目时返回空列表而不是 None。"""
    with store.begin() as conn:
        rows = conn.execute(select(projects).order_by(projects.c.created_at.desc())).mappings().all()
    return [dict(row) for row in rows]


def list_runs(
    store: Store, project_id: str | None = None, *, limit: int = 50
) -> list[dict[str, Any]]:
    """最近的分析任务（新到旧）。project_id 为空时跨项目查，供首页用。"""
    query = select(analysis_runs)
    if project_id:
        query = query.where(analysis_runs.c.project_id == project_id)
    query = query.order_by(analysis_runs.c.queued_at.desc()).limit(max(1, min(limit, 200)))
    with store.begin() as conn:
        rows = conn.execute(query).mappings().all()
    return [dict(row) for row in rows]


def list_invocations(store: Store, run_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """一次 run 的全部模型调用；论文里的 token/延迟表格直接用。"""
    with store.begin() as conn:
        rows = conn.execute(
            select(ai_invocations)
            .where(ai_invocations.c.run_id == run_id)
            .order_by(ai_invocations.c.id.asc())
            .limit(max(1, min(limit, 1000)))
        ).mappings().all()
    return [dict(row) for row in rows]


def transition_run(store: Store, run_id: str, status: str, **fields: Any) -> dict[str, Any]:
    """推进状态机。非法迁移抛 InvalidTransition，不静默吞掉。

    同一状态重复迁移视为幂等（返回当前行），因为 Celery 的 at-least-once 语义
    会让同一个任务被执行两次，第二次不该把 run 判成失败。
    """
    if status not in RUN_STATUSES:
        raise InvalidTransition(f"未知状态 {status!r}")
    with store.begin() as conn:
        row = conn.execute(select(analysis_runs).where(analysis_runs.c.id == run_id)).mappings().first()
        if row is None:
            raise InvalidTransition(f"run {run_id} 不存在")
        current = row["status"]
        if current != status and status not in ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise InvalidTransition(f"run {run_id}: {current} -> {status} 不被允许")

        values: dict[str, Any] = {"status": status}
        if fields:
            unknown = set(fields) - set(analysis_runs.c.keys())
            if unknown:
                raise ValueError(f"analysis_runs 没有这些列: {sorted(unknown)}")
            values.update(fields)
        if status == "failed":
            values.setdefault("finished_at", _utcnow())
        if status in TERMINAL_STATUSES and "finished_at" not in values:
            values["finished_at"] = _utcnow()
        if status != "queued" and row["started_at"] is None:
            values["started_at"] = _utcnow()

        if current != status:
            conn.execute(update(analysis_runs).where(analysis_runs.c.id == run_id).values(**values))
        elif fields:
            conn.execute(update(analysis_runs).where(analysis_runs.c.id == run_id).values(**fields))
    return get_run(store, run_id) or {}


def fail_run(store: Store, run_id: str, *, code: str, message: str) -> dict[str, Any]:
    return transition_run(
        store, run_id, "failed", error_code=code, error_message=message[:2000]
    )


def active_runs(store: Store, *, older_than_minutes: int = 15) -> list[dict[str, Any]]:
    """ADR-002 的超时看门狗：找"卡在中间态太久"的 run。

    判据是业务状态 + 时间，不是队列的 visibility timeout —— 后者在 sqla broker
    下根本不存在，在 Redis 下也只覆盖"消息被取走后崩掉"这一种情况。
    """
    cutoff = _utcnow() - timedelta(minutes=older_than_minutes)
    with store.begin() as conn:
        rows = conn.execute(
            select(analysis_runs).where(
                analysis_runs.c.status.notin_(TERMINAL_STATUSES),
                analysis_runs.c.started_at.is_not(None),
            )
        ).mappings().all()
    stale: list[dict[str, Any]] = []
    for row in rows:
        started = _as_utc(row["started_at"])
        if started is not None and started < cutoff:
            stale.append(dict(row))
    return stale


def record_invocation(
    store: Store,
    *,
    run_id: str | None,
    document_id: str | None,
    purpose: str,
    model: str,
    prompt_version: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    ok: bool = True,
    error: str | None = None,
    provider: str = "deepseek",
) -> None:
    """AI 调用审计。论文里的成本/成功率图表直接从这张表出。"""
    with store.begin() as conn:
        conn.execute(
            insert(ai_invocations).values(
                run_id=run_id,
                document_id=document_id,
                purpose=purpose,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                ok=ok,
                error=error,
            )
        )


# --------------------------------------------------------------------------- #
# publish_records：一次投递的审计（成功与失败都记，失败不能让 run 失败）
# --------------------------------------------------------------------------- #
def record_publish(
    store: Store,
    *,
    target: str,
    status: str,
    run_id: str | None = None,
    project_id: str | None = None,
    document_id: str | None = None,
    revision_id: int | None = None,
    external_id: str | None = None,
    external_url: str | None = None,
    error_message: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """写一条投递记录，返回自增 id。

    target 取 local_wiki / github_pr_comment / webhook_kb；
    document_id 为空表示 run 级投递（一条 PR 评论覆盖多份文档）。
    """
    if status not in {"success", "failed", "skipped"}:
        raise ValueError(f"未知 publish 状态 {status!r}")
    with store.begin() as conn:
        result = conn.execute(
            insert(publish_records).values(
                run_id=run_id,
                project_id=project_id,
                document_id=document_id,
                revision_id=revision_id,
                target=target,
                status=status,
                external_id=external_id,
                external_url=external_url,
                error_message=(error_message or "")[:2000] or None,
                detail=detail or {},
            )
        )
        return int(result.inserted_primary_key[0])


def list_publish_records(
    store: Store,
    *,
    run_id: str | None = None,
    document_id: str | None = None,
    project_id: str | None = None,
    target: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按最新优先读投递记录；review UI 的「投递历史」直接用它。"""
    query = select(publish_records)
    if run_id:
        query = query.where(publish_records.c.run_id == run_id)
    if document_id:
        query = query.where(publish_records.c.document_id == document_id)
    if project_id:
        query = query.where(publish_records.c.project_id == project_id)
    if target:
        query = query.where(publish_records.c.target == target)
    query = query.order_by(publish_records.c.id.desc()).limit(max(1, min(limit, 500)))
    with store.begin() as conn:
        rows = conn.execute(query).mappings().all()
    return [dict(row) for row in rows]
