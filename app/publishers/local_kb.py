"""本地知识库适配器：把文档写成一棵可以直接用编辑器/静态站点打开的目录树。

这是"推送到知识库"的可离线演示形态：不依赖飞书/Notion 的账号，答辩机上也能跑。
布局：

    var/kb/<project-slug>/
        README.md                  # 索引（按文档类型分组）
        class_diagram/app.services.puml
        sequence_diagram/app.services.puml
        api_doc/app.services.md
        _pending/class_diagram/... # 因人工修改保护而待确认的 AI 候选

设计取舍：
- 只写"当前生效版本"（current），不把候选混进正文；候选单独进 `_pending/`，
  和 ADR-006 的语义一致：KB 是已发布事实，候选是待审内容。
- 文件内容不变就不落盘（避免每次 push 都改 mtime）；但每次仍返回 success，
  让审计表能看出"这次 run 确实同步过这份文档"。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from app.publishers.base import DocumentPayload, PublishResult, PublisherError, RunDigest

_SAFE = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")

_DOC_TYPE_LABEL = {
    "class_diagram": "类图",
    "sequence_diagram": "时序图",
    "api_doc": "API 文档",
}


def safe_name(value: str, *, max_length: int = 120) -> str:
    """把 target_key 变成跨平台安全的文件名（保留中文与点号）。"""
    name = _SAFE.sub("-", value.strip()).strip("-.")
    if not name:
        name = "untitled"
    if name.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
        name = f"_{name}"
    return name[:max_length]


def _front_matter(
    doc: DocumentPayload,
    *,
    project_slug: str,
    status: str,
    version: int | None,
    pending: bool,
) -> str:
    fields = {
        "title": doc.title,
        "doc_type": doc.doc_type,
        "target_key": doc.target_key,
        "project": project_slug,
        "status": status,
        "version": "" if version is None else str(version),
        "manual_edited": "true" if doc.manual_edited else "false",
    }
    if pending:
        fields["review"] = "waiting-for-human"
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# 同一进程内的发布线程串行写 KB：Windows 上 os.replace 遇到"目标正被另一个线程读"
# 会抛 PermissionError（POSIX 不会）。写文件本身很快，串行化的代价远小于让评审页 500。
_WRITE_LOCK = threading.Lock()


def _write_if_changed(path: Path, text: str) -> bool:
    with _WRITE_LOCK:
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except FileNotFoundError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件名必须每次唯一：固定 `<path>.tmp` 在并发发布会互相踩，
        # 先完成 replace 的线程会把 tmp 改名走，后一个线程 replace 时文件已不存在
        # （Linux 容器里的并发测试就是这样炸的；Windows 上只是运气好没撞到）。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            # 杀毒软件/编辑器可能短暂占用目标文件；Windows 上重试几次即可。
            for attempt in range(1, 6):
                try:
                    os.replace(tmp_name, path)  # 原子替换，读者不会看到半个文件
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.01 * attempt)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return True


class LocalKnowledgeBasePublisher:
    name = "local_wiki"

    def available(self, settings: Any) -> bool:
        return True

    def publish(self, digest: RunDigest, settings: Any) -> list[PublishResult]:
        root = Path(settings.kb_dir) / digest.project_slug
        results: list[PublishResult] = []

        for doc in digest.documents:
            if not doc.content.strip():
                results.append(
                    PublishResult(
                        target=self.name,
                        status="skipped",
                        document_id=doc.document_id,
                        revision_id=doc.revision_id,
                        error="empty content",
                    )
                )
                continue

            relative = Path(doc.doc_type) / safe_name(doc.target_key + doc.extension)
            text = _front_matter(
                doc,
                project_slug=digest.project_slug,
                status=doc.status,
                version=doc.version,
                pending=False,
            ) + doc.content
            written = _write_if_changed(root / relative, text)
            results.append(
                PublishResult(
                    target=self.name,
                    status="success",
                    document_id=doc.document_id,
                    revision_id=doc.revision_id,
                    external_id=relative.as_posix(),
                    external_url=(root / relative).as_uri(),
                    detail={"written": written, "bytes": len(text.encode("utf-8"))},
                )
            )

            if doc.deferred and doc.candidate_content:
                pending_rel = (
                    Path("_pending") / doc.doc_type / safe_name(doc.target_key + doc.extension)
                )
                pending_text = _front_matter(
                    doc,
                    project_slug=digest.project_slug,
                    status="pending_review",
                    version=doc.candidate_version,
                    pending=True,
                ) + doc.candidate_content
                pending_written = _write_if_changed(root / pending_rel, pending_text)
                results.append(
                    PublishResult(
                        target=self.name,
                        status="success",
                        document_id=doc.document_id,
                        revision_id=doc.candidate_revision_id,
                        external_id=pending_rel.as_posix(),
                        external_url=(root / pending_rel).as_uri(),
                        detail={
                            "written": pending_written,
                            "pending": True,
                            "bytes": len(pending_text.encode("utf-8")),
                        },
                    )
                )

        try:
            index_written = self._write_index(root, digest)
        except OSError as exc:  # 磁盘满/权限问题：记失败，不掩盖文档已写的事实
            raise PublisherError(f"写入知识库索引失败: {exc}") from exc
        results.append(
            PublishResult(
                target=self.name,
                status="success",
                external_id="README.md",
                external_url=(root / "README.md").as_uri(),
                detail={"written": index_written, "index": True},
            )
        )
        return results

    def _write_index(self, root: Path, digest: RunDigest) -> bool:
        grouped: dict[str, list[DocumentPayload]] = {}
        for doc in digest.documents:
            grouped.setdefault(doc.doc_type, []).append(doc)

        lines = [
            f"# {digest.project_name} · 活文档",
            "",
            f"- 仓库：`{digest.repo_full_name}`",
            f"- 最近运行：`{digest.run_id}`",
            f"- 本次变更：{len(digest.changed_documents)} 份 / 待评审：{len(digest.deferred_documents)} 份",
            "",
        ]
        if digest.review_url:
            lines += [f"👉 [打开在线评审界面]({digest.review_url})", ""]

        for doc_type in sorted(grouped):
            label = _DOC_TYPE_LABEL.get(doc_type, doc_type)
            lines += [f"## {label}", "", "| 文档 | 状态 | 版本 | 文件 |", "|---|---|---|---|"]
            for doc in sorted(grouped[doc_type], key=lambda d: d.target_key):
                rel = Path(doc_type) / safe_name(doc.target_key + doc.extension)
                status = "⏸ 待确认候选" if doc.deferred else doc.status
                version = f"v{doc.version}" if doc.version else "-"
                lines.append(
                    f"| {doc.title} | {status} | {version} | [{rel.as_posix()}]({rel.as_posix()}) |"
                )
            lines.append("")

        pending = digest.deferred_documents
        if pending:
            lines += ["## 待确认候选（人工修改保护）", ""]
            for doc in pending:
                rel = Path("_pending") / doc.doc_type / safe_name(doc.target_key + doc.extension)
                lines.append(f"- {doc.title} · [{rel.as_posix()}]({rel.as_posix()})")
            lines.append("")

        lines += [
            "---",
            "",
            "> 由 LivingDoc 自动同步。`_pending/` 下的候选不会自动生效，需在评审界面点击「接受 AI 候选」。",
            "",
        ]
        return _write_if_changed(root / "README.md", "\n".join(lines))