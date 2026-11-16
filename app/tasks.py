"""Celery 异步任务：webhook 收到的活，全部在这里干。

阶段五把三段串起来（run 的每一次状态迁移都落在 analysis_runs 里）：

    analyze_push   克隆/取 diff          -> runs/<run_id>.json
    parse_run      tree-sitter 解析       -> runs/<run_id>.changes.json
    generate_run   调模型 + 写 documents

任务之间只传 run_id。理由：ChangeSet 里有函数体原文，塞进 broker 会让队列里躺着
MB 级消息；而 stage 之间要能独立重跑，所以中间产物落盘、状态落库。

任务入参是纯 JSON dict（不是 ORM 对象、不是 Path）：Celery 用 JSON 序列化，
传对象会在反序列化时炸。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.celery_app import celery_app
from app.config import get_settings
from app.git_handler import (
    FileChange,
    GitError,
    RepoRef,
    diff_commits,
    ensure_repo,
    fetch_commits,
    read_blob,
    repo_path_for,
    repo_size_mb,
)
from app.pipeline import (
    count_tokens,
    doc_count,
    entity_count,
    generate_documents,
    parse_changes,
    publish_drafts,
    read_change_set,
    write_change_set,
)
from app.publishers import build_digest, deliver_run
from app.store import (
    TERMINAL_STATUSES,
    Store,
    active_runs,
    ensure_project,
    fail_run,
    get_project,
    get_run,
    open_store,
    record_invocation,
    start_run,
    transition_run,
)

logger = logging.getLogger("livingdoc.tasks")

_TRIGGER_TYPES = {"push": "push", "pull_request": "pr", "pull_request_review": "pr"}


@lru_cache(maxsize=4)
def _store_for(url: str) -> Store:
    return open_store(url)


def get_store() -> Store:
    """一个进程一个 Store（Engine + 连接池），不要每次任务都新建。"""
    return _store_for(get_settings().database_url)


def _trigger_type(job: dict[str, Any]) -> str:
    return _TRIGGER_TYPES.get(str(job.get("event") or "push"), "push")


def _runs_dir() -> Path:
    path = get_settings().work_dir / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _artifact_path(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.json"


def _changes_path(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.changes.json"


def group_by_module(changes: list[FileChange]) -> dict[str, int]:
    """按"模块"聚合变更文件数（首段近似，真正的模块映射在 app/ai/service.module_key_for）。"""
    modules: dict[str, int] = {}
    for change in changes:
        parts = change.path.split("/")
        module = parts[0] if len(parts) > 1 else "(root)"
        modules[module] = modules.get(module, 0) + 1
    return dict(sorted(modules.items(), key=lambda kv: (-kv[1], kv[0])))


def _enqueue(task, run_id: str) -> bool:
    """入队下一段。broker 挂了不该让已经算出来的 diff 白费：记日志，留给看门狗。"""
    if not get_settings().pipeline_enabled:
        logger.info("[%s] pipeline_enabled=false，停在本段", run_id[:8])
        return False
    try:
        task.delay(run_id)
        return True
    except Exception as exc:  # pragma: no cover - 需要 broker 故障才触发
        logger.warning("[%s] 入队下一段失败: %s", run_id[:8], exc)
        return False


# --------------------------------------------------------------------------- #
# S1-S2：克隆 + diff
# --------------------------------------------------------------------------- #
@celery_app.task(bind=True, name="livingdoc.analyze_push")
def analyze_push(self, job: dict[str, Any]) -> dict[str, Any]:
    """分析一次 push / PR 带来的代码变更。

    幂等性靠两处：start_run 的 (project, idempotency_key) 唯一索引挡住重复投递；
    产物按 run_id 命名，重复执行只会覆盖同一个文件。
    """
    settings = get_settings()
    delivery_id = str(job.get("delivery_id") or "unknown")
    repo_full_name = str(job.get("repo_full_name") or "")
    base_sha = str(job.get("base_sha") or "")
    head_sha = str(job.get("head_sha") or "")
    clone_url = str(job.get("clone_url") or f"https://github.com/{repo_full_name}.git")
    attempt = self.request.retries + 1

    logger.info(
        "[%s] 开始分析 %s %s..%s (attempt=%d)",
        delivery_id, repo_full_name, base_sha[:8], head_sha[:8], attempt,
    )
    started = time.perf_counter()

    store = get_store()
    project_id = ensure_project(
        store, provider="github", repo_full_name=repo_full_name, repo_url=clone_url
    )
    run_id, created = start_run(
        store,
        project_id=project_id,
        base_sha=base_sha,
        head_sha=head_sha,
        trigger_type=_trigger_type(job),
        trigger_ref=job.get("ref"),
    )
    if not created:
        existing = get_run(store, run_id) or {}
        logger.info("[%s] 同一 push 已有 run %s（%s），跳过", delivery_id, run_id[:8], existing.get("status"))
        return {
            "status": "duplicate_run",
            "run_id": run_id,
            "run_status": existing.get("status"),
            "delivery_id": delivery_id,
        }

    try:
        transition_run(store, run_id, "cloning", stage_detail=f"delivery={delivery_id}")
        ref = RepoRef(full_name=repo_full_name, clone_url=clone_url)
        repo_dir = ensure_repo(ref)
        missing = fetch_commits(repo_dir, [base_sha, head_sha])
        if missing:
            raise GitError(
                f"无法获取 commit: {', '.join(s[:8] for s in missing)}"
                "（仓库可能被 force-push 或删除了历史）",
                stderr="missing object",
            )
        transition_run(store, run_id, "diffing")
        changes = diff_commits(repo_dir, base_sha, head_sha)
    except GitError as exc:
        logger.warning("[%s] git 阶段失败 retryable=%s: %s", delivery_id, exc.retryable, exc)
        if exc.retryable and self.request.retries < settings.analyze_max_retries:
            raise self.retry(exc=exc, countdown=min(60, 2 ** (self.request.retries + 1)))
        fail_run(store, run_id, code="git_error", message=str(exc))
        return {
            "status": "failed",
            "run_id": run_id,
            "delivery_id": delivery_id,
            "stage": "git",
            "retryable": exc.retryable,
            "error": str(exc),
        }

    duration_ms = int((time.perf_counter() - started) * 1000)
    modules = group_by_module(changes)
    truncated = len(changes) >= settings.diff_max_files

    artifact: dict[str, Any] = {
        "run_id": run_id,
        "delivery_id": delivery_id,
        "event": job.get("event"),
        "repo": repo_full_name,
        "ref": job.get("ref"),
        # PR 事件才有；analysis_runs 没有这一列，靠 artifact 传给 generate_run，
        # 这样加投递功能不需要改库表/写迁移。
        "pr_number": job.get("pr_number"),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "truncated": truncated,
        "stats": {
            "files": len(changes),
            "added": sum(1 for c in changes if c.status == "A"),
            "modified": sum(1 for c in changes if c.status == "M"),
            "deleted": sum(1 for c in changes if c.status == "D"),
            "lines_added": sum(c.additions for c in changes),
            "lines_deleted": sum(c.deletions for c in changes),
            "repo_size_mb": repo_size_mb(repo_dir),
        },
        "modules": modules,
        "files": [
            {"path": c.path, "status": c.status,
             "additions": c.additions, "deletions": c.deletions}
            for c in changes
        ],
        "next_stage": "tree-sitter-parse",
    }
    artifact_path = _artifact_path(run_id)
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    transition_run(
        store, run_id, "diffing",
        stage_detail=f"diff ready: {len(changes)} files",
        files_changed=len(changes),
    )
    if not changes:
        transition_run(store, run_id, "skipped", error_code="no_file_changes",
                       error_message="base 与 head 之间没有文件变更")
        return {"status": "skipped", "run_id": run_id, "delivery_id": delivery_id,
                "reason": "no_file_changes", "artifact": str(artifact_path)}

    enqueued = _enqueue(parse_run, run_id)
    logger.info(
        "[%s] diff 完成 %d 个文件，模块: %s，产物: %s（%dms）",
        delivery_id, len(changes), ", ".join(modules) or "无", artifact_path, duration_ms,
    )
    return {
        "status": "succeeded",
        "run_id": run_id,
        "delivery_id": delivery_id,
        "files": len(changes),
        "modules": modules,
        "truncated": truncated,
        "artifact": str(artifact_path),
        "enqueued": enqueued,
        "duration_ms": duration_ms,
    }


# --------------------------------------------------------------------------- #
# S3：AST 解析
# --------------------------------------------------------------------------- #
@celery_app.task(bind=True, name="livingdoc.parse_run")
def parse_run(self, run_id: str) -> dict[str, Any]:
    settings = get_settings()
    store = get_store()
    run = get_run(store, run_id)
    if run is None:
        return {"status": "failed", "run_id": run_id, "error": "run not found"}
    if run["status"] in TERMINAL_STATUSES:
        return {"status": "ignored", "run_id": run_id, "run_status": run["status"]}

    project = get_project(store, run["project_id"])
    artifact_path = _artifact_path(run_id)
    if project is None or not artifact_path.exists():
        message = "project 缺失" if project is None else f"diff 产物缺失: {artifact_path}"
        fail_run(store, run_id, code="artifact_missing", message=message)
        return {"status": "failed", "run_id": run_id, "error": message}

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    repo_dir = repo_path_for(project["repo_full_name"])
    base_sha, head_sha = run["base_sha"], run["head_sha"]

    def _read(which: str, path: str) -> str:
        sha = base_sha if which == "base" else head_sha
        return read_blob(repo_dir, sha, path)

    started = time.perf_counter()
    try:
        transition_run(store, run_id, "parsing")
        change_set = parse_changes(files=artifact.get("files", []), read_blob=_read)
        write_change_set(_changes_path(run_id), change_set)
    except Exception as exc:  # 解析器崩溃不能把整条流水线留在 parsing
        logger.exception("[%s] 解析失败", run_id[:8])
        fail_run(store, run_id, code="parse_error", message=str(exc))
        return {"status": "failed", "run_id": run_id, "stage": "parse", "error": str(exc)}

    duration_ms = int((time.perf_counter() - started) * 1000)
    summary = change_set.to_summary()
    transition_run(
        store, run_id, "parsing",
        stage_detail=json.dumps(summary, ensure_ascii=False),
        files_changed=len(change_set.files),
        entities_changed=entity_count(change_set),
    )

    if not change_set.files:
        transition_run(
            store, run_id, "skipped",
            error_code="no_doc_relevant_changes",
            error_message="没有受支持的函数级变更（可能只改了文档/配置/非目标语言）",
        )
        return {"status": "skipped", "run_id": run_id, "reason": "no_doc_relevant_changes",
                "summary": summary, "duration_ms": duration_ms}

    enqueued = _enqueue(generate_run, run_id)
    logger.info("[%s] 解析完成 %s，%dms，入队=%s", run_id[:8], summary, duration_ms, enqueued)
    return {"status": "succeeded", "run_id": run_id, "summary": summary,
            "enqueued": enqueued, "duration_ms": duration_ms}


# --------------------------------------------------------------------------- #
# S4-S6：生成 + 落库
# --------------------------------------------------------------------------- #
def _build_client(settings):
    """单独抽出来，测试里 monkeypatch 这一个函数就能换成剧本模型。"""
    from app.ai.client import DeepSeekClient, RetryPolicy

    return DeepSeekClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        policy=RetryPolicy(max_retries=settings.llm_max_retries),
    )


@celery_app.task(bind=True, name="livingdoc.generate_run")
def generate_run(self, run_id: str) -> dict[str, Any]:
    settings = get_settings()
    store = get_store()
    run = get_run(store, run_id)
    if run is None:
        return {"status": "failed", "run_id": run_id, "error": "run not found"}
    if run["status"] in TERMINAL_STATUSES:
        return {"status": "ignored", "run_id": run_id, "run_status": run["status"]}

    changes_path = _changes_path(run_id)
    if not changes_path.exists():
        fail_run(store, run_id, code="artifact_missing", message=f"变更集缺失: {changes_path}")
        return {"status": "failed", "run_id": run_id, "error": "changes artifact missing"}

    # PR 号/仓库信息在 diff 阶段的 artifact 里（老 artifact 没有该字段，按 None 处理）
    artifact_path = _artifact_path(run_id)
    artifact: dict[str, Any] = {}
    if artifact_path.exists():
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("[%s] diff artifact 读取失败，投递将缺少 PR 号", run_id[:8])

    transition_run(store, run_id, "analyzing")

    if not settings.llm_api_key:
        # 没有 key 不是崩溃，是"这个环境跑不了这一段"：标 skipped，别污染失败率统计
        transition_run(
            store, run_id, "skipped",
            error_code="llm_not_configured",
            error_message="未配置 LLM_API_KEY；解析结果已保存在 changes.json",
        )
        return {"status": "skipped", "run_id": run_id, "reason": "llm_not_configured"}

    change_set = read_change_set(changes_path)
    started = time.perf_counter()
    try:
        client = _build_client(settings)
        docs = generate_documents(
            change_set=change_set,
            client=client,
            max_attempts=settings.self_correct_max_attempts,
            model_name=settings.llm_model,
        )
        transition_run(store, run_id, "publishing",
                       stage_detail=f"{len(docs)} module(s), {doc_count(docs)} document(s)")
        report = publish_drafts(
            store,
            project_id=run["project_id"],
            run_id=run_id,
            docs=docs,
            change_summary=f"run {run_id[:8]}",
        )
    except Exception as exc:
        logger.exception("[%s] 生成/发布失败", run_id[:8])
        fail_run(store, run_id, code="generation_error", message=str(exc))
        return {"status": "failed", "run_id": run_id, "stage": "generate", "error": str(exc)}

    _tokens_in, tokens_out = count_tokens(docs)
    for module_doc in docs:
        for draft in module_doc.drafts:
            record_invocation(
                store,
                run_id=run_id,
                document_id=None,
                purpose=draft.doc_type.value,
                model=draft.model or settings.llm_model,
                prompt_version=draft.prompt_version,
                completion_tokens=draft.tokens,
                latency_ms=draft.latency_ms,
                ok=draft.ok,
                error=None if draft.ok else (draft.validation_errors()[:2000] or "validation failed"),
            )

    # ---------- 投递（P1）：PR 评论 / 本地知识库 / 外部 webhook ----------
    # 关键约束：投递是"尽力而为"的后置动作。任何渠道失败都只进 publish_records，
    # 不能把一次已成功入库的生成判成失败。
    delivery: list[dict[str, Any]] = []
    try:
        digest = build_digest(
            store,
            run_id=run_id,
            report=report,
            artifact=artifact,
            settings=settings,
            tokens=tokens_out,
        )
        results = deliver_run(store, digest=digest, settings=settings)
        delivery = [
            {
                "target": r.target,
                "status": r.status,
                "document_id": r.document_id,
                "url": r.external_url,
                "error": r.error,
            }
            for r in results
        ]
    except Exception:  # 拼摘要/写审计的 bug 也不该抹掉已完成的生成
        logger.exception("[%s] 文档投递失败（不影响 run 状态）", run_id[:8])

    summary = report.summary()
    summary["modules"] = len(docs)
    summary["duration_ms"] = int((time.perf_counter() - started) * 1000)
    summary["delivery"] = {
        "total": len(delivery),
        "success": sum(1 for item in delivery if item["status"] == "success"),
        "failed": sum(1 for item in delivery if item["status"] == "failed"),
        "skipped": sum(1 for item in delivery if item["status"] == "skipped"),
    }
    transition_run(
        store, run_id, "succeeded",
        stage_detail=json.dumps(summary, ensure_ascii=False),
        tokens_out=tokens_out,
    )
    logger.info("[%s] 完成 %s", run_id[:8], summary)
    return {
        "status": "succeeded",
        "run_id": run_id,
        "documents": summary["documents"],
        "published": summary["published"],
        "deferred": summary["deferred"],
        "rejected": [key for key, _reason in report.rejected],
        "outcomes": [
            {"document_id": o.document_id, "action": o.action, "version": o.version}
            for o in report.outcomes
        ],
        "delivery": delivery,
        "duration_ms": summary["duration_ms"],
    }


# --------------------------------------------------------------------------- #
# ADR-002 看门狗：把卡在中间态的 run 拉回来
# --------------------------------------------------------------------------- #
@celery_app.task(name="livingdoc.watchdog")
def watchdog(older_than_minutes: int = 15) -> dict[str, Any]:
    """扫"状态非终态且 started_at 过旧"的 run，按当前阶段重新入队。

    判据是业务状态而不是队列的 visibility timeout：后者在 sqla broker 下不存在，
    在 Redis 下也只覆盖"消息被取走后进程崩了"这一种情况。
    """
    settings = get_settings()
    store = get_store()
    requeued: list[dict[str, Any]] = []
    for run in active_runs(store, older_than_minutes=older_than_minutes):
        status = run["status"]
        attempt = int(run.get("attempt") or 1) + 1
        transition_run(store, run["id"], status, attempt=attempt,
                       stage_detail=f"requeued by watchdog (attempt {attempt})")
        project = get_project(store, run["project_id"]) or {}
        if status in ("queued", "cloning", "diffing"):
            analyze_push.delay({
                "run_id": run["id"],
                "delivery_id": f"watchdog-{run['id'][:8]}",
                "repo_full_name": project.get("repo_full_name", ""),
                "clone_url": project.get("repo_url", ""),
                "base_sha": run.get("base_sha") or "",
                "head_sha": run.get("head_sha") or "",
                "event": "push" if run.get("trigger_type") == "push" else "pull_request",
                "ref": run.get("trigger_ref"),
            })
        elif status == "parsing":
            parse_run.delay(run["id"])
        else:  # analyzing / rendering / publishing
            generate_run.delay(run["id"])
        requeued.append({"run_id": run["id"], "status": status, "attempt": attempt})

    if requeued:
        logger.warning("看门狗重新入队 %d 个 run（阈值 %d 分钟）", len(requeued), older_than_minutes)
    return {"requeued": requeued, "count": len(requeued),
            "older_than_minutes": older_than_minutes, "pipeline_enabled": settings.pipeline_enabled}
