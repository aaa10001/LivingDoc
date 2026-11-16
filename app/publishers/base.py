"""发布层的公共数据形状。

为什么要单独一层"投递"：生成（generate）只关心"这份文档对不对"，投递（publish）
关心"送到哪里、以什么形状送、失败了怎么办"。两者的失败语义完全不同——
文档没过校验是一次生成失败，PR 评论 502 是一次投递失败，不应该互相拖垮。

这里刻意不依赖 FastAPI / Celery，publisher 可以被脚本、API、worker 复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

#: doc_type -> 扩展名（与 publish.FORMAT_BY_EXTENSION 对齐）
_EXTENSION = {
    "plantuml": ".puml",
    "markdown": ".md",
    "mermaid": ".mmd",
}


def extension_for(content_format: str) -> str:
    return _EXTENSION.get(content_format, ".txt")


def review_url(public_base_url: str, *, project_id: str, document_id: str | None = None) -> str:
    """评审界面的深链。UI 用 hash 路由，静态托管也能直接打开某份文档。"""
    base = (public_base_url or "").rstrip("/")
    if not base:
        return ""
    if document_id:
        return f"{base}/ui/#doc={document_id}"
    return f"{base}/ui/#project={project_id}"


@dataclass(frozen=True)
class DocumentPayload:
    """一份文档在"当前生效版本"上的快照，外加可选的人工待确认候选。"""

    document_id: str
    doc_type: str
    target_key: str
    title: str
    content: str
    content_format: str
    status: str
    action: str = "unchanged"  # created | updated | accepted | human_edit | deferred_stale | unchanged
    version: int | None = None
    revision_id: int | None = None
    manual_edited: bool = False
    render_ok: bool = True
    candidate_content: str | None = None
    candidate_version: int | None = None
    candidate_revision_id: int | None = None

    @property
    def extension(self) -> str:
        return extension_for(self.content_format)

    @property
    def filename(self) -> str:
        return f"{self.target_key}{self.extension}"

    @property
    def deferred(self) -> bool:
        return self.action == "deferred_stale"

    @property
    def changed(self) -> bool:
        return self.action not in ("unchanged", "flagged")

    @property
    def has_candidate(self) -> bool:
        return bool(self.candidate_content) and self.candidate_revision_id != self.revision_id


@dataclass(frozen=True)
class RunDigest:
    """一次 run 的投递摘要：PR 评论和 webhook 都消费这个对象。"""

    run_id: str
    project_id: str
    project_slug: str
    project_name: str
    repo_full_name: str
    trigger_type: str
    trigger_ref: str | None
    base_sha: str
    head_sha: str
    pr_number: int | None = None
    documents: tuple[DocumentPayload, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    stats: Mapping[str, Any] = field(default_factory=dict)
    review_url: str = ""

    @property
    def changed_documents(self) -> tuple[DocumentPayload, ...]:
        return tuple(doc for doc in self.documents if doc.changed)

    @property
    def deferred_documents(self) -> tuple[DocumentPayload, ...]:
        return tuple(doc for doc in self.documents if doc.deferred)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "documents": len(self.documents),
            "changed": len(self.changed_documents),
            "deferred": len(self.deferred_documents),
            "rejected": len(self.rejected),
        }

    def as_payload(self, *, include_content: bool = True) -> dict[str, Any]:
        """给外部知识库的 JSON 形状（也用于入库审计，别塞进超大字段）。"""
        docs: list[dict[str, Any]] = []
        for doc in self.documents:
            item: dict[str, Any] = {
                "document_id": doc.document_id,
                "doc_type": doc.doc_type,
                "target_key": doc.target_key,
                "title": doc.title,
                "content_format": doc.content_format,
                "status": doc.status,
                "action": doc.action,
                "version": doc.version,
                "manual_edited": doc.manual_edited,
                "review_url": (
                    f"{self.review_url.split('#')[0]}#doc={doc.document_id}"
                    if self.review_url
                    else ""
                ),
            }
            if include_content:
                item["content"] = doc.content
                if doc.candidate_content:
                    item["candidate_content"] = doc.candidate_content
                    item["candidate_version"] = doc.candidate_version
            docs.append(item)
        return {
            "event": "livingdoc.documents.published",
            "run_id": self.run_id,
            "project": {
                "id": self.project_id,
                "slug": self.project_slug,
                "name": self.project_name,
                "repo": self.repo_full_name,
            },
            "trigger": {
                "type": self.trigger_type,
                "ref": self.trigger_ref,
                "pr_number": self.pr_number,
            },
            "commits": {"base": self.base_sha, "head": self.head_sha},
            "stats": dict(self.stats),
            "rejected": [{"document": key, "reason": reason} for key, reason in self.rejected],
            "documents": docs,
        }


@dataclass(frozen=True)
class PublishResult:
    """一次投递的结果。status 只有 success / failed / skipped 三种。"""

    target: str
    status: str
    document_id: str | None = None
    revision_id: int | None = None
    external_id: str | None = None
    external_url: str | None = None
    error: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"


@runtime_checkable
class Publisher(Protocol):
    """适配器协议：配置是否可用 + 一次投递（失败要把原因写进结果）。"""

    name: str

    def available(self, settings: Any) -> bool: ...

    def publish(self, digest: RunDigest, settings: Any) -> list[PublishResult]: ...


class PublisherError(RuntimeError):
    """投递失败。dispatch 会把它变成 failed 记录，不让 run 失败。"""