"""把 git diff 的产物接到解析器上：一次 push 里到底改了哪些函数。

阶段二的 tasks.py 产出的是"文件级"信息（哪个文件 A/M/D、加删了多少行）。
这里往下一层，产出"函数级"信息 —— 正好是阶段四喂给 LLM 的粒度：
    prompt 输入 = 被改函数的 signature + body + 所属模块
    而不是整份文件。

调用方式（阶段四接进 tasks.py 时）：

    service = ChangedFunctionService()
    change_set = service.analyze([
        FilePayload(path="app/service.py", old_source=blob_old, new_source=blob_new),
    ])
    change_set.to_summary()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .differ import diff_symbols
from .extractor import TreeSitterExtractor, UnsupportedLanguage
from .model import ChangeSet, FileDiff
from .profiles import profile_for_path


@dataclass(frozen=True)
class FilePayload:
    """一个文件的两个版本。文件被新增时 old_source=None，被删除时 new_source=None。"""

    path: str
    old_source: bytes | None = None
    new_source: bytes | None = None

    def __post_init__(self) -> None:
        if self.old_source is None and self.new_source is None:
            raise ValueError(f"FilePayload({self.path}) has no content at all")


class ChangedFunctionService:
    def __init__(self, extractor: TreeSitterExtractor | None = None) -> None:
        self._extractor = extractor or TreeSitterExtractor()

    def analyze(self, payloads: Iterable[FilePayload]) -> ChangeSet:
        change_set = ChangeSet()
        for payload in payloads:
            profile = profile_for_path(payload.path)
            if profile is None:
                change_set.skipped_files.append((payload.path, "unsupported_language"))
                continue
            try:
                old = (
                    self._extractor.parse(payload.path, payload.old_source)
                    if payload.old_source is not None
                    else None
                )
                new = (
                    self._extractor.parse(payload.path, payload.new_source)
                    if payload.new_source is not None
                    else None
                )
            except UnsupportedLanguage:
                change_set.skipped_files.append((payload.path, "unsupported_language"))
                continue

            file_diff: FileDiff = diff_symbols(payload.path, old, new)
            if file_diff.changes:
                change_set.files.append(file_diff)
        return change_set

    def extract(self, path: str, source: bytes):
        """只要一个文件的函数清单（不带比对），给"全量重建文档"用。"""
        return self._extractor.parse(path, source)


def build_prompt_payload(change_set: ChangeSet, max_chars: int = 60000) -> str:
    """把变更集压成一段给 LLM 的文本。

    这里刻意不塞整份文件：只塞函数的 signature + body。
    超预算时按"新增 > 修改 > 重命名"的优先级丢弃，并在末尾注明截断了多少个 ——
    模型知道"还有东西没给你"，比它以为拿全了要安全得多。
    """
    priority = {
        "added": 0,
        "modified": 1,
        "renamed": 2,
        "removed": 3,
    }
    items = sorted(
        (c for c in change_set.all_changes() if c.needs_redoc),
        key=lambda c: (priority.get(c.kind.value, 9), c.key),
    )

    chunks: list[str] = []
    used = 0
    dropped = 0
    for change in items:
        symbol = change.new or change.old
        assert symbol is not None
        header = f"### {change.kind.value.upper()} {symbol.qualified_name} ({symbol.language})"
        block = f"{header}\n{symbol.body}\n"
        if used + len(block) > max_chars:
            dropped += 1
            continue
        chunks.append(block)
        used += len(block)

    if dropped:
        chunks.append(f"\n[truncated] {dropped} more changes were omitted for context budget.\n")
    return "\n".join(chunks)
