"""GitHub PR 评论适配器：把一次 run 的结果贴回 Pull Request。

为什么值得做：文档更新如果只躺在系统里，评审的人根本不会打开第二个网站。
把"哪几份文档变了、哪几份被人工修改保护拦住"贴到 PR 时间线，
评审动作才发生在工程师本来就在看的地方。

幂等：评论正文里带 `<!-- livingdoc:run:<run_id> -->` 标记。同一个 run 重试时
先按标记找已有评论并 PATCH，而不是再发一条——否则一次网络抖动就会刷屏。
"""

from __future__ import annotations

import html
import json
from typing import Any

import httpx

from app.publishers.base import (
    DocumentPayload,
    PublishResult,
    PublisherError,
    RunDigest,
)

_ACTION_LABEL = {
    "created": "🆕 新增",
    "updated": "✏️ 更新",
    "accepted": "✅ 接受候选",
    "human_edit": "👤 人工修改",
    "deferred_stale": "⏸ 待确认",
    "unchanged": "· 无变化",
    "flagged": "👤 已标记人工",
}

_TYPE_LABEL = {
    "class_diagram": "类图",
    "sequence_diagram": "时序图",
    "api_doc": "API 文档",
}


def marker_for(run_id: str) -> str:
    return f"<!-- livingdoc:run:{run_id} -->"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _doc_url(digest: RunDigest, doc: DocumentPayload) -> str:
    if not digest.review_url:
        return ""
    base = digest.review_url.split("#")[0]
    return f"{base}#doc={doc.document_id}"


def build_comment(digest: RunDigest) -> str:
    """生成 PR 评论 Markdown。独立成函数，单测可以不碰网络直接断言正文。"""
    counts = digest.counts
    base = digest.base_sha[:7] if digest.base_sha else "?"
    head = digest.head_sha[:7] if digest.head_sha else "?"
    trigger = digest.trigger_type
    if digest.pr_number:
        trigger += f" #{digest.pr_number}"

    lines = [
        marker_for(digest.run_id),
        "## 📘 LivingDoc · 文档已更新",
        "",
        f"`{digest.repo_full_name}` · `{head}` ← `{base}` · 触发：{trigger}",
    ]
    if digest.review_url:
        lines.append(f"[打开在线评审界面]({digest.review_url})")
    lines += [
        "",
        "| 文档 | 类型 | 结果 | 版本 |",
        "|---|---|---|---|",
    ]

    changed = digest.changed_documents
    for doc in changed:
        label = _ACTION_LABEL.get(doc.action, doc.action)
        if not doc.render_ok:
            label += " · ⚠️ 校验未过"
        version = f"v{doc.version}" if doc.version else "-"
        title = _cell(doc.title)
        url = _doc_url(digest, doc)
        if url:
            title = f"[{title}]({url})"
        lines.append(f"| {title} | {_TYPE_LABEL.get(doc.doc_type, doc.doc_type)} | {label} | {version} |")

    if not changed:
        lines.append("| _本次没有文档变更_ | - | - | - |")

    deferred = digest.deferred_documents
    if deferred:
        lines += ["", f"### ⏸ {len(deferred)} 份文档等待人工确认", ""]
        lines.append("这些文档被人修改过，AI 只写了候选版本，**没有自动覆盖**：")
        lines.append("")
        for doc in deferred:
            url = _doc_url(digest, doc)
            name = f"[{_cell(doc.title)}]({url})" if url else _cell(doc.title)
            lines.append(f"- {name}（当前 v{doc.version}，候选 v{doc.candidate_version}）")

    if digest.rejected:
        lines += ["", f"### 🚫 {len(digest.rejected)} 份草稿未通过校验", ""]
        for key, reason in digest.rejected[:10]:
            lines.append(f"- `{_cell(key)}` — {_cell(reason)[:200]}")
        if len(digest.rejected) > 10:
            lines.append(f"- …另有 {len(digest.rejected) - 10} 条")

    stats = digest.stats or {}
    tokens = int(stats.get("tokens", 0) or 0)
    footer = (
        f"本次 {counts['documents']} 份文档 · 变更 {counts['changed']} 份 · "
        f"待确认 {counts['deferred']} 份"
    )
    if tokens:
        footer += f" · tokens {tokens}"
    lines += ["", f"<sub>{footer} · run `{digest.run_id[:8]}`</sub>"]
    return "\n".join(lines)


class GitHubPRCommentPublisher:
    name = "github_pr_comment"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        # 测试注入 MockTransport；生产为 None，走真实网络
        self._transport = transport

    def available(self, settings: Any) -> bool:
        return bool(settings.github_token)

    def publish(self, digest: RunDigest, settings: Any) -> list[PublishResult]:
        if not digest.pr_number:
            return [
                PublishResult(
                    target=self.name,
                    status="skipped",
                    error="not a pull request event (no PR number)",
                )
            ]
        if not settings.github_token:
            return [
                PublishResult(
                    target=self.name,
                    status="skipped",
                    error="GITHUB_TOKEN is empty",
                )
            ]

        body = build_comment(digest)
        api = (settings.github_api_base or "https://api.github.com").rstrip("/")
        repo = digest.repo_full_name
        number = digest.pr_number
        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "LivingDoc/1.0",
        }
        timeout = float(settings.publish_timeout_seconds)

        try:
            with httpx.Client(
                base_url=api,
                headers=headers,
                timeout=timeout,
                transport=self._transport,
            ) as client:
                comment_id, html_url, action = self._upsert_comment(
                    client, repo=repo, number=number, body=body, run_id=digest.run_id
                )
        except httpx.HTTPError as exc:
            raise PublisherError(f"GitHub 请求失败: {type(exc).__name__}: {exc}") from exc

        return [
            PublishResult(
                target=self.name,
                status="success",
                external_id=str(comment_id),
                external_url=html_url,
                detail={"action": action, "pr": number},
            )
        ]

    def _upsert_comment(
        self,
        client: httpx.Client,
        *,
        repo: str,
        number: int,
        body: str,
        run_id: str,
    ) -> tuple[int, str, str]:
        marker = marker_for(run_id)
        listing = client.get(f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100})
        self._raise_for_status(listing, "list comments")
        existing = None
        for item in listing.json():
            if marker in (item.get("body") or ""):
                existing = item
                break

        if existing is not None:
            response = client.patch(
                f"/repos/{repo}/issues/comments/{existing['id']}", json={"body": body}
            )
            self._raise_for_status(response, "update comment")
            payload = response.json()
            return int(payload["id"]), str(payload.get("html_url") or ""), "updated"

        response = client.post(f"/repos/{repo}/issues/{number}/comments", json={"body": body})
        self._raise_for_status(response, "create comment")
        payload = response.json()
        return int(payload["id"]), str(payload.get("html_url") or ""), "created"

    @staticmethod
    def _raise_for_status(response: httpx.Response, what: str) -> None:
        if response.is_success:
            return
        # GitHub 的错误正文里可能有字段说明，但绝不可能有我们的 token；截断防刷屏
        snippet = response.text[:300].replace("\n", " ")
        try:
            data = response.json()
            snippet = json.dumps(data, ensure_ascii=False)[:300]
        except (json.JSONDecodeError, ValueError):
            pass
        raise PublisherError(f"GitHub API {response.status_code} ({what}): {html.escape(snippet)}")