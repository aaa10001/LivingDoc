"""Celery 应用实例。

单独一个模块，避免 main.py <-> tasks.py 循环导入：
    main.py  -> tasks.py -> celery_app.py
    worker   -> celery_app.py（通过 include 自动发现 tasks）

Windows 上的启动方式见 docs/02-phase2-skeleton.md：
官方不支持 prefork（billiard 依赖 os.fork），必须 --pool=threads。
"""

from __future__ import annotations

from typing import Any

from celery import Celery

from app.config import get_settings

_settings = get_settings()
# worker 是独立进程，不经过 main.py，所以这里也要保证 broker 的 sqlite 目录存在
_settings.ensure_dirs()

# broker_transport_options 只对特定 transport 有意义：
#   redis -> visibility_timeout 等有效
#   sqla+sqlite -> kombu 会把整个 dict 原样传给 create_engine()，
#                 塞 visibility_timeout 进来会直接 TypeError（已实测）
# 所以按 broker 协议条件构造，而不是无脑写死一个 dict。
_transport_options: dict[str, Any] = {}
if _settings.broker_url.startswith(("redis://", "rediss://")):
    _transport_options = {
        "visibility_timeout": 1800,  # 与 ADR-002 的超时看门狗配套
        "socket_keepalive": True,
    }

celery_app = Celery(
    "livingdoc",
    broker=_settings.broker_url,
    backend=_settings.result_backend,
    include=["app.tasks"],
)

celery_app.conf.update(
    # ---- 序列化：只收 JSON。pickle 反序列化等于远程代码执行 ----
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # ---- 时区 ----
    timezone=_settings.timezone,
    enable_utc=True,
    # ---- 可靠性 ----
    # acks_late：任务执行完才 ack。worker 崩溃时消息回到队列，不会丢。
    # 代价是任务可能被执行两次 -> 所以每个任务必须幂等（我们靠 dedupe_key）。
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # 预取 1 条：分析任务耗时长且不均匀，预取多了会造成队列里明明还有任务、
    # 另一个 worker 却闲着的假象。
    worker_prefetch_multiplier=1,
    # 注意：这个配置在 --pool=threads 下不生效，只在 prefork 生效。
    # Windows 本地开发享受不到，Linux 生产环境才有意义。
    worker_max_tasks_per_child=50,
    broker_connection_retry_on_startup=True,
    result_expires=24 * 3600,
    task_track_started=True,
    # ---- 路由 ----
    task_default_queue="analysis",
    task_routes={"livingdoc.analyze_push": {"queue": "analysis"}},
    # ---- 开发便利：设 CELERY_TASK_ALWAYS_EAGER=true 就不需要起 worker ----
    task_always_eager=_settings.task_always_eager,
    task_eager_propagates=_settings.task_always_eager,
    # 见文件上方说明：仅 Redis 下非空。SQLAlchemy broker 靠 ADR-002 的
    # 超时看门狗兜底，本来就没有真正的 visibility timeout。
    broker_transport_options=_transport_options,
)
