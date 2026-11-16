"""通用知识库 webhook：把一次 run 的完整文档包 POST 给外部系统。

为什么不是直接写飞书/Notion/Confluence 的 SDK：
1. 这三家的 API 差异极大，接进来就要维护三套鉴权与分页；
2. 课程演示环境没有企业租户，写了也验证不了；
3. 真正可复用的契约是"我推一份结构化 JSON，你负责落到你的知识库"。

签名用 HMAC-SHA256（`X-LivingDoc-Signature: sha256=<hex>`），收端用同一个
`KB_WEBHOOK_SECRET` 校验，防重放/伪造。没有配 secret 时不签名（内网演示）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from app.publishers.base import PublishResult, PublisherError, RunDigest


class WebhookKnowledgeBasePublisher:
    name = "webhook_kb"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def available(self, settings: Any) -> bool:
        return bool(settings.kb_webhook_url)

    def publish(self, digest: RunDigest, settings: Any) -> list[PublishResult]:
        url = settings.kb_webhook_url
        if not url:
            return [PublishResult(target=self.name, status="skipped", error="KB_WEBHOOK_URL is empty")]

        payload = digest.as_payload(include_content=True)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-LivingDoc-Event": "documents.published",
            "X-LivingDoc-Run": digest.run_id,
            "User-Agent": "LivingDoc/1.0",
        }
        if settings.kb_webhook_secret:
            signature = hmac.new(
                settings.kb_webhook_secret.encode("utf-8"), raw, hashlib.sha256
            ).hexdigest()
            headers["X-LivingDoc-Signature"] = f"sha256={signature}"

        try:
            with httpx.Client(
                timeout=float(settings.publish_timeout_seconds),
                transport=self._transport,
            ) as client:
                response = client.post(url, content=raw, headers=headers)
        except httpx.HTTPError as exc:
            raise PublisherError(f"知识库 webhook 请求失败: {type(exc).__name__}: {exc}") from exc

        if not response.is_success:
            raise PublisherError(
                f"知识库 webhook 返回 {response.status_code}: {response.text[:300]}"
            )

        external_id = (
            response.headers.get("X-Request-Id")
            or response.headers.get("X-LivingDoc-Document-Id")
            or hashlib.sha256(raw).hexdigest()[:16]
        )
        return [
            PublishResult(
                target=self.name,
                status="success",
                external_id=external_id,
                external_url=url,
                detail={
                    "status_code": response.status_code,
                    "bytes": len(raw),
                    "documents": len(payload["documents"]),
                    "signed": bool(settings.kb_webhook_secret),
                },
            )
        ]