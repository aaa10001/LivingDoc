"""FastAPI 入口：只做三件事 —— 验签、解析事件、把活丢给 Celery。

设计原则：Webhook 接口必须**快进快出**。GitHub 的超时是 10 秒，超时会重投；
而克隆 + AI 分析动辄几十秒。所以这里绝不做同步重活，一律丢队列，立刻返回 202。

状态码约定（和 ADR-003 一致，很重要）：
  202 已受理          -> 平台认为投递成功，不会重投
  200 重复投递/忽略    -> 同样不能返回 4xx/5xx，否则平台会持续重试
  401 验签失败        -> 唯一应当立刻失败的情况
  503 队列不可用      -> 故意返回 5xx，让平台稍后重投（这是**期望**的行为）
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import router as review_router
from app.config import BASE_DIR, get_settings
from app.dedupe import delivery_cache
from app.security import verify_github_signature
from app.tasks import analyze_push

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)
logger = logging.getLogger("livingdoc.api")

settings = get_settings()
settings.ensure_dirs()

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="基于 AI 的活文档 / 架构逆向生成系统 —— Webhook 接入 + 人工评审",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(review_router)

# 评审界面是"原生 HTML + JS + 静态资源"，刻意不引入 Node 构建链：
# 大作业的演示机器上不该为了一个页面再装一套前端工具。
_STATIC_DIR = BASE_DIR / "app" / "static"
if _STATIC_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")


@app.exception_handler(HTTPException)
async def problem_details(request: Request, exc: HTTPException) -> JSONResponse:
    """把 HTTPException 统一成 RFC 7807 形状；前端只认这一种错误体。"""
    detail = exc.detail
    if isinstance(detail, dict):
        payload: dict[str, Any] = {
            "type": detail.get("type") or f"https://livingdoc.dev/errors/{detail.get('error', 'http_error')}",
            "title": str(detail.get("title") or detail.get("error") or "Request failed"),
            "status": exc.status_code,
            "detail": str(detail.get("detail") or detail.get("message") or ""),
            "instance": request.url.path,
        }
        for key, value in detail.items():
            if key not in {"type", "title", "status", "detail", "message", "error"}:
                payload[key] = value
    else:
        payload = {
            "type": "about:blank",
            "title": str(detail),
            "status": exc.status_code,
            "detail": "",
            "instance": request.url.path,
        }
    return JSONResponse(
        status_code=exc.status_code,
        content=payload,
        media_type="application/problem+json",
        headers=exc.headers,
    )

# 只处理这两类事件；其余（如 ping、star）直接忽略
HANDLED_EVENTS = {"push", "pull_request"}
PR_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


# --------------------------------------------------------------------------- #
# 事件 -> 任务
# --------------------------------------------------------------------------- #
def build_job(event: str, payload: dict[str, Any], delivery_id: str) -> tuple[dict[str, Any] | None, str]:
    """把 Webhook payload 压平成 Celery 任务能吃的扁平 dict。

    返回 (job, reason)：job 为 None 表示这个事件不需要处理，reason 说明原因。
    """
    if event == "push":
        after = str(payload.get("after") or "")
        # 分支删除：after 是 40 个 0；force push 之外还要看 deleted 标志
        if payload.get("deleted") or set(after) == {"0"}:
            return None, "branch_deleted"
        before = str(payload.get("before") or "")
        if not before or set(before) == {"0"}:
            # 新分支首次推送没有 before，用 after 的父提交在阶段三处理
            return None, "no_base_commit"
        repo = payload.get("repository") or {}
        return (
            {
                "delivery_id": delivery_id,
                "event": "push",
                "repo_full_name": repo.get("full_name"),
                "clone_url": repo.get("clone_url"),
                "ref": payload.get("ref"),
                "base_sha": before,
                "head_sha": after,
                "sender": (payload.get("sender") or {}).get("login"),
            },
            "ok",
        )

    if event == "pull_request":
        action = str(payload.get("action") or "")
        if action not in PR_ACTIONS:
            return None, f"pr_action_{action}_ignored"
        pr = payload.get("pull_request") or {}
        if pr.get("draft") and action != "ready_for_review":
            return None, "pr_draft"
        repo = payload.get("repository") or {}
        base = (pr.get("base") or {}).get("sha")
        head = (pr.get("head") or {}).get("sha")
        if not base or not head:
            return None, "pr_missing_sha"
        return (
            {
                "delivery_id": delivery_id,
                "event": "pull_request",
                "repo_full_name": repo.get("full_name"),
                "clone_url": repo.get("clone_url"),
                "ref": (pr.get("head") or {}).get("ref"),
                "base_sha": base,
                "head_sha": head,
                "pr_number": pr.get("number"),
                "sender": (payload.get("sender") or {}).get("login"),
            },
            "ok",
        )

    return None, "unhandled_event"


def dedupe_key(delivery_id: str | None, job: dict[str, Any] | None) -> str:
    """优先用 X-GitHub-Delivery：GitHub 重投时会复用同一个 GUID。

    没有这个头（本地模拟、Gitee）时退化成内容哈希 —— 与 ADR-003 的
    dedupe_key = sha256(project|event|after|pr) 同构。
    """
    if delivery_id:
        return f"delivery:{delivery_id}"
    assert job is not None
    raw = f"{job.get('repo_full_name')}|{job.get('event')}|{job.get('head_sha')}|{job.get('pr_number')}"
    return "content:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "app": settings.app_name,
        "version": "1.0.0",
        "docs": "/docs",
        "review_ui": "/ui/",
        "api": "/api/v1/projects",
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "env": settings.env, "queue": settings.broker_url.split(":")[0]}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
    x_hub_signature: str | None = Header(default=None),
) -> JSONResponse:
    # 刻意不给路由设默认 status_code：本接口有 202/200 两种成功语义，
    # 用一个默认值会让"忽略事件"也返回 202，客户端无法区分"已排队"和"什么都没做"。
    # ---- 1. 先拿原始字节，任何解析都必须发生在验签之后 ----
    body = await request.body()
    if len(body) > settings.payload_max_bytes:
        # 用数字而不是 status.HTTP_413_REQUEST_ENTITY_TOO_LARGE：
        # 新版 starlette 已把它改名成 HTTP_413_CONTENT_TOO_LARGE，写数字不受影响
        raise HTTPException(
            status_code=413,
            detail={"error": "payload_too_large", "limit": settings.payload_max_bytes},
        )

    signature = x_hub_signature_256 or x_hub_signature
    if not verify_github_signature(settings.github_webhook_secret, body, signature):
        logger.warning(
            "验签失败 delivery=%s event=%s len=%d",
            x_github_delivery, x_github_event, len(body),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_signature"},
        )

    # ---- 2. 解析事件（此时 body 已可信）----
    event = (x_github_event or "").strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_json", "message": str(exc)},
        ) from exc

    if event == "ping":
        return JSONResponse(status_code=200, content={"status": "pong", "zen": payload.get("zen")})

    if event not in HANDLED_EVENTS:
        logger.info("忽略事件 %s delivery=%s", event, x_github_delivery)
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": "unhandled_event", "event": event},
        )

    job, reason = build_job(event, payload, x_github_delivery or "")
    if job is None:
        logger.info("跳过 %s/%s delivery=%s", event, reason, x_github_delivery)
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": reason, "event": event},
        )

    # payload 结构不合格（缺仓库名/提交号）：属于调用方问题，400 让平台别再重投
    if not job.get("repo_full_name") or not job.get("head_sha"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_payload", "message": "缺少 repository.full_name 或 head sha"},
        )

    # ---- 3. 去重（阶段二为进程内；阶段三换成 webhook_deliveries 唯一索引）----
    key = dedupe_key(x_github_delivery, job)
    if not delivery_cache.add(key):
        logger.info("重复投递 %s，直接返回 200", key)
        return JSONResponse(
            status_code=200,
            content={"status": "duplicate", "delivery_id": x_github_delivery},
        )

    # ---- 4. 入队 ----
    try:
        async_result = analyze_push.apply_async(kwargs={"job": job}, queue="analysis")
    except Exception as exc:  # broker 连不上（sqlite 锁、redis 宕机）
        logger.error("任务入队失败: %s", exc)
        # 故意返回 503：让 GitHub 稍后重投，而不是把这次投递吞掉
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "queue_unavailable", "message": str(exc)[:200]},
        ) from exc

    logger.info(
        "已入队 delivery=%s repo=%s %s..%s task=%s",
        x_github_delivery, job["repo_full_name"],
        str(job["base_sha"])[:8], str(job["head_sha"])[:8], async_result.id,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "delivery_id": x_github_delivery,
            "task_id": async_result.id,
            "repo": job["repo_full_name"],
        },
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
