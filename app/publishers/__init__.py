"""投递层：把已发布的文档送到 PR 评论 / 本地知识库 / 外部 webhook。

用法（worker 和 API 都走同一个入口）：

    from app.publishers import build_digest, deliver_run

    digest = build_digest(store, run_id=run_id, report=report, artifact=artifact)
    results = deliver_run(store, digest=digest, settings=settings)
"""

from app.publishers.base import (
    DocumentPayload,
    PublishResult,
    Publisher,
    PublisherError,
    RunDigest,
)
from app.publishers.dispatch import (
    PUBLISHERS,
    build_digest,
    configured_targets,
    deliver_run,
    digest_for_document,
)

__all__ = [
    "PUBLISHERS",
    "DocumentPayload",
    "PublishResult",
    "Publisher",
    "PublisherError",
    "RunDigest",
    "build_digest",
    "configured_targets",
    "deliver_run",
    "digest_for_document",
]