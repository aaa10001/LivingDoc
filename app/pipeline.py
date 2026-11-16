"""流水线的粘合层：diff -> AST -> 生成 -> 落库。

三段各自是一个 Celery 任务，中间只靠 run_id 和一份 JSON artifact 传递状态，
好处是任意一段失败都能单独重跑，坏处是 ChangeSet 必须能序列化——
Symbol 里有函数体原文，不能塞进 Celery 的消息体（会让 broker 里躺着 MB 级消息），
所以落成 var/work/runs/<run_id>.changes.json。

这一层的函数全部依赖注入（store / read_blob / llm client 都是参数），
测试里不需要起数据库、不需要连 GitHub、不需要调模型。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from app.ai.service import (
    DocType,
    DocumentationService,
    ModuleDocumentation,
)
from app.parsers import (
    ChangeKind,
    ChangeSet,
    FileDiff,
    FileSymbols,
    Symbol,
    SymbolChange,
    SymbolKind,
    TreeSitterExtractor,
    diff_symbols,
    profile_for_path,
)
from app.publish import PublishOutcome, publish_document
from app.store import Store

logger = logging.getLogger(__name__)

#: 单个文件超过这个大小就跳过：2MB 的源码几乎是生成的，解析只会拖垮 worker。
MAX_BLOB_BYTES = 2 * 1024 * 1024

BlobReader = Callable[[str, str], str]  # (sha, path) -> 源码文本


@dataclass(frozen=True)
class PublishReport:
    """一次发布的结果汇总。数字会写进 run 的 stage_detail，也进论文的统计表。"""

    outcomes: tuple[PublishOutcome, ...]
    rejected: tuple[tuple[str, str], ...]  # (doc_key, 原因)
    skipped_reason: str = ""

    @property
    def published(self) -> tuple[PublishOutcome, ...]:
        return tuple(o for o in self.outcomes if o.changed)

    @property
    def deferred(self) -> tuple[PublishOutcome, ...]:
        return tuple(o for o in self.outcomes if o.deferred)

    @property
    def unchanged(self) -> tuple[PublishOutcome, ...]:
        return tuple(o for o in self.outcomes if o.action == "unchanged")

    def summary(self) -> dict[str, Any]:
        return {
            "documents": len(self.outcomes),
            "published": len(self.published),
            "deferred": len(self.deferred),
            "unchanged": len(self.unchanged),
            "rejected": len(self.rejected),
            "skipped_reason": self.skipped_reason,
        }


# --------------------------------------------------------------------------- #
# S3：解析
# --------------------------------------------------------------------------- #
def _path_status(item: Any) -> tuple[str, str]:
    """兼容 FileChange 对象与从 artifact 读回来的 dict。"""
    if isinstance(item, dict):
        return str(item.get("path", "")), str(item.get("status", "M")).upper()[:1] or "M"
    return str(getattr(item, "path", "")), str(getattr(item, "status", "M")).upper()[:1] or "M"


def parse_changes(
    *,
    files: Iterable[Any],
    read_blob: BlobReader,
    extractor: TreeSitterExtractor | None = None,
) -> ChangeSet:
    """把一批文件变更解析成函数级变更集。

    跳过规则都写进 skipped_files，而不是静默丢弃——"哪些文件没被分析"本身
    就是要给用户看的信息（也是 prompt 覆盖率的分子分母）。
    """
    parser = extractor or TreeSitterExtractor()
    change_set = ChangeSet()
    seen: set[str] = set()

    for item in files:
        path, status = _path_status(item)
        if not path or path in seen:
            continue
        seen.add(path)

        profile = profile_for_path(path)
        if profile is None:
            change_set.skipped_files.append((path, "unsupported language"))
            continue

        if status not in ("A", "D", "M", "T"):
            change_set.skipped_files.append((path, f"unsupported git status {status!r}"))
            continue

        try:
            old_text = None if status == "A" else read_blob("base", path)
            new_text = None if status == "D" else read_blob("head", path)
        except Exception as exc:  # GitError / UnicodeDecodeError 都在这里
            change_set.skipped_files.append((path, f"read failed: {exc}"[:200]))
            continue

        if old_text is None and new_text is None:
            change_set.skipped_files.append((path, "both versions missing"))
            continue

        sizes = [len(t.encode("utf-8")) for t in (old_text, new_text) if t is not None]
        if max(sizes, default=0) > MAX_BLOB_BYTES:
            change_set.skipped_files.append((path, "file too large"))
            continue

        old_symbols = _parse(parser, path, old_text, change_set)
        new_symbols = _parse(parser, path, new_text, change_set)
        if old_symbols is None and old_text is not None:
            continue
        if new_symbols is None and new_text is not None:
            continue

        file_diff = diff_symbols(path, old_symbols, new_symbols)
        if not file_diff.changes:
            change_set.skipped_files.append((path, "no symbol changes"))
            continue
        if file_diff.old_has_error or file_diff.new_has_error:
            # 语法有错也照报，但要让下游知道这份 AST 打了折
            change_set.skipped_files.append((path, "syntax error tolerated (partial AST)"))
        if new_symbols is not None:
            # 改动函数之外的符号也要带上：类图要知道 `self.repo` 是哪种类型，
            # 只能靠同一文件里 `__init__(self, repo: UserRepository)` 的注解。
            file_diff = replace(file_diff, new_symbols=new_symbols.symbols)
        change_set.files.append(file_diff)

    change_set.files.sort(key=lambda f: f.path)
    return change_set


def _parse(
    parser: TreeSitterExtractor,
    path: str,
    text: str | None,
    change_set: ChangeSet,
) -> FileSymbols | None:
    if text is None:
        return None
    try:
        return parser.parse(path, text.encode("utf-8"))
    except Exception as exc:  # pragma: no cover - 解析器自身异常
        change_set.skipped_files.append((path, f"parse failed: {exc}"[:200]))
        return None


# --------------------------------------------------------------------------- #
# artifact：ChangeSet <-> JSON
# --------------------------------------------------------------------------- #
ARTIFACT_VERSION = 1


def _symbol_to_dict(symbol: Symbol) -> dict[str, Any]:
    return {
        "language": symbol.language,
        "kind": symbol.kind.value,
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "key": symbol.key,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "start_byte": symbol.start_byte,
        "end_byte": symbol.end_byte,
        "signature": symbol.signature,
        "body": symbol.body,
        "params": symbol.params,
        "decorators": list(symbol.decorators),
        "raw_hash": symbol.raw_hash,
        "semantic_hash": symbol.semantic_hash,
        "body_hash": symbol.body_hash,
        # v4：结构事实。changes.json 是 parse 与 generate 之间唯一的通道，
        # 字段漏在这里，下游的类图/时序图就只剩函数签名可用。
        "owner": symbol.owner,
        "bases": list(symbol.bases),
        "base_roles": list(symbol.base_roles),
        "class_attributes": list(symbol.class_attributes),
        "returns": symbol.returns,
        "calls": list(symbol.calls),
        "branches": list(symbol.branches),
        "is_async": symbol.is_async,
    }


def _symbol_from_dict(payload: dict[str, Any]) -> Symbol:
    return Symbol(
        language=payload["language"],
        kind=SymbolKind(payload["kind"]),
        name=payload["name"],
        qualified_name=payload["qualified_name"],
        key=payload["key"],
        start_line=int(payload["start_line"]),
        end_line=int(payload["end_line"]),
        start_byte=int(payload["start_byte"]),
        end_byte=int(payload["end_byte"]),
        signature=payload["signature"],
        body=payload["body"],
        params=payload["params"],
        decorators=tuple(payload.get("decorators") or ()),
        raw_hash=payload["raw_hash"],
        semantic_hash=payload["semantic_hash"],
        body_hash=payload.get("body_hash", ""),
        # 老版本产物（ARTIFACT_VERSION=1 之前写下的 changes.json）没有这些键：
        # 用 get 兜底成空值，而不是让整个 run 读档失败。
        owner=payload.get("owner", "") or "",
        bases=tuple(payload.get("bases") or ()),
        base_roles=tuple(payload.get("base_roles") or ()),
        class_attributes=tuple(payload.get("class_attributes") or ()),
        returns=payload.get("returns", "") or "",
        calls=tuple(payload.get("calls") or ()),
        branches=tuple(payload.get("branches") or ()),
        is_async=bool(payload.get("is_async", False)),
    )


def dump_change_set(change_set: ChangeSet) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for file_diff in change_set.files:
        files.append({
            "path": file_diff.path,
            "language": file_diff.language,
            "old_has_error": file_diff.old_has_error,
            "new_has_error": file_diff.new_has_error,
            "new_symbols": [_symbol_to_dict(s) for s in file_diff.new_symbols],
            "changes": [
                {
                    "kind": change.kind.value,
                    "formatting_only": change.formatting_only,
                    "old": _symbol_to_dict(change.old) if change.old else None,
                    "new": _symbol_to_dict(change.new) if change.new else None,
                }
                for change in file_diff.changes
            ],
        })
    return {
        "version": ARTIFACT_VERSION,
        "files": files,
        "skipped_files": [list(item) for item in change_set.skipped_files],
    }


def load_change_set(payload: dict[str, Any]) -> ChangeSet:
    if payload.get("version") != ARTIFACT_VERSION:
        raise ValueError(f"不认识的 artifact 版本: {payload.get('version')!r}")
    files: list[FileDiff] = []
    for item in payload.get("files", []):
        changes = tuple(
            SymbolChange(
                kind=ChangeKind(entry["kind"]),
                old=_symbol_from_dict(entry["old"]) if entry.get("old") else None,
                new=_symbol_from_dict(entry["new"]) if entry.get("new") else None,
                formatting_only=bool(entry.get("formatting_only", False)),
            )
            for entry in item.get("changes", [])
        )
        files.append(FileDiff(
            path=item["path"],
            language=item["language"],
            changes=changes,
            old_has_error=bool(item.get("old_has_error", False)),
            new_has_error=bool(item.get("new_has_error", False)),
            new_symbols=tuple(_symbol_from_dict(s) for s in item.get("new_symbols") or ()),
        ))
    return ChangeSet(
        files=files,
        skipped_files=[(str(p), str(r)) for p, r in payload.get("skipped_files", [])],
    )


def write_change_set(path: Path, change_set: ChangeSet) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dump_change_set(change_set), ensure_ascii=False), encoding="utf-8")
    return path


def read_change_set(path: Path) -> ChangeSet:
    return load_change_set(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# S4-S5：生成 + 发布
# --------------------------------------------------------------------------- #
def generate_documents(
    *,
    change_set: ChangeSet,
    client: Any,
    max_attempts: int = 3,
    model_name: str = "",
) -> tuple[ModuleDocumentation, ...]:
    """按模块生成三类文档。真正的重试与自纠正在 DocumentationService 里。"""
    if not change_set.files:
        return ()
    service = DocumentationService(client, max_attempts=max_attempts, model_name=model_name)
    return service.generate_all(change_set)


def publish_drafts(
    store: Store,
    *,
    project_id: str,
    run_id: str | None,
    docs: Sequence[ModuleDocumentation],
    change_summary: str | None = None,
) -> PublishReport:
    """把生成结果写库。ok=False 的草稿不发布，进 rejected 供人查看原因。

    "不发布"比"发布一张坏图"安全：历史版本还在，下一轮修好后自然补上。
    """
    outcomes: list[PublishOutcome] = []
    rejected: list[tuple[str, str]] = []

    # 允许调用方直接传单个 ModuleDocumentation（写测试时踩过：它不是可迭代的）
    if isinstance(docs, ModuleDocumentation):
        docs = (docs,)

    for module_doc in docs:
        for draft in module_doc.drafts:
            doc_key = f"{draft.doc_type.value}:{draft.target_key}" if hasattr(draft.doc_type, "value") else f"{draft.doc_type}:{draft.target_key}"
            if not draft.publishable:
                reasons = "; ".join(d.render() for d in draft.diagnostics if d.severity == "error")
                rejected.append((doc_key, reasons[:300] or "validation failed"))
                continue
            outcome = publish_document(
                store,
                project_id=project_id,
                doc_type=draft.doc_type.value,
                target_key=draft.target_key,
                title=draft.title,
                content=draft.content,
                run_id=run_id,
                extension=draft.extension,
                render_ok=draft.ok,
                model_name=draft.model,
                prompt_version=draft.prompt_version,
                change_summary=change_summary or module_doc.summary.get("module"),
            )
            outcomes.append(outcome)

    return PublishReport(outcomes=tuple(outcomes), rejected=tuple(rejected))


def count_tokens(docs: Sequence[ModuleDocumentation]) -> tuple[int, int]:
    """(输入 token, 输出 token)。输入量按模块 summary 里的 changed_functions 无法还原，
    这里只汇总输出侧，输入侧由任务层用 prompt 长度估算——不假装精确。"""
    out = sum(int(d.summary.get("tokens", 0) or 0) for d in docs)
    return 0, out


def entity_count(change_set: ChangeSet) -> int:
    return len(change_set.all_changes())


def doc_count(docs: Sequence[ModuleDocumentation]) -> int:
    return sum(len(d.drafts) for d in docs)
