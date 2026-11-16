"""AST 提取阶段的数据模型。

刻意全部用 frozen dataclass：这些对象会作为 Celery 任务的返回值、
也会被序列化进 JSON 当 Prompt 的输入，可变对象很容易被下游悄悄改坏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SymbolKind(str, Enum):
    """函数级实体的类型。注意这些都是 str 枚举，序列化成 JSON 时就是普通字符串。"""

    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"


@dataclass(frozen=True)
class Symbol:
    """一个函数/方法的完整快照。

    body 是整个节点原文（含 def 行），signature 是"头部"（def 行 + 装饰器），
    两者都保留是因为阶段四的 Prompt 需要"签名 + 实现"分别喂：
    签名决定文档里的接口表格，实现决定时序图。
    """

    language: str
    kind: SymbolKind
    name: str
    qualified_name: str
    key: str  # 跨版本比对用的唯一键（qualified_name + 参数签名）
    start_line: int  # 1-based，含装饰器
    end_line: int  # 1-based，闭区间
    start_byte: int
    end_byte: int
    signature: str
    body: str
    params: str
    decorators: tuple[str, ...]
    raw_hash: str
    semantic_hash: str
    # 只对"函数体"（不含签名）取指纹。重命名改的是签名、不动函数体，
    # 所以识别重命名必须用它；用 raw_hash 会永远配不上对。
    body_hash: str = ""

    # ---- 结构事实（阶段四画类图/时序图的依据，全部来自 AST，不靠模型猜）
    # 为什么塞进 Symbol 而不是新增一个并行的结构体：Symbol 是唯一已经完整穿过
    # ChangeSet -> changes.json -> generate_run 这条链的类型（见 pipeline.dump_change_set），
    # 新增字段只需要改两处序列化，不必给 artifact 加第二套版本兼容。
    owner: str = ""  # 所属类短名；顶层函数为空串
    bases: tuple[str, ...] = ()  # 所属类的基类/实现接口
    # 与 bases 按位置对齐："extends" 或 "implements"；Python 恒为 extends
    base_roles: tuple[str, ...] = ()
    class_attributes: tuple[str, ...] = ()  # 所属类的字段声明（"name: Type" 形式）
    returns: str = ""  # 返回类型注解原文；无注解为空串
    calls: tuple[str, ...] = ()  # 函数体里出现过的调用目标（已去掉 self./cls. 前缀）
    branches: tuple[str, ...] = ()  # 函数体里出现过的控制流关键字：if/for/while/try/...
    is_async: bool = False

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass(frozen=True)
class FileSymbols:
    """一个文件解析后的全部函数。

    has_error=True 表示 tree-sitter 在树上留下了 ERROR 节点（源码语法有问题）。
    tree-sitter 是容错的，这种文件依然能提取出部分函数，只是结果要打折看待。
    """

    path: str
    language: str
    symbols: tuple[Symbol, ...]
    has_error: bool = False
    parse_ms: int = 0

    def by_key(self) -> dict[str, Symbol]:
        return {s.key: s for s in self.symbols}


class ChangeKind(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"  # 函数体没动，只是名字/位置变了


@dataclass(frozen=True)
class SymbolChange:
    kind: ChangeKind
    old: Symbol | None
    new: Symbol | None
    formatting_only: bool = False

    @property
    def key(self) -> str:
        return (self.new or self.old).key  # type: ignore[union-attr]

    @property
    def name(self) -> str:
        return (self.new or self.old).qualified_name  # type: ignore[union-attr]

    @property
    def needs_redoc(self) -> bool:
        """要不要为这个变更重新生成文档。

        纯格式化/纯注释变更直接跳过 —— 这是本阶段最省钱的一条规则。
        """
        return not self.formatting_only


@dataclass(frozen=True)
class FileDiff:
    path: str
    language: str
    changes: tuple[SymbolChange, ...]
    old_has_error: bool = False
    new_has_error: bool = False
    # 新版本整个文件的符号（不只是改动的那几个）。类图/时序图需要它来补全
    # "这个字段是什么类型""这个类还有哪些方法"——只给改动函数时，
    # `self.repo.save()` 里的 repo 就退化成没有类型的字段，组合边整条丢失。
    new_symbols: tuple[Symbol, ...] = ()

    @property
    def changed(self) -> tuple[SymbolChange, ...]:
        return tuple(c for c in self.changes if c.kind is not ChangeKind.REMOVED)

    @property
    def redoc_targets(self) -> tuple[SymbolChange, ...]:
        return tuple(c for c in self.changes if c.needs_redoc)


@dataclass
class ChangeSet:
    """一次 push/PR 里所有受影响函数的集合 —— 阶段四 Prompt 的输入。"""

    files: list[FileDiff] = field(default_factory=list)
    skipped_files: list[tuple[str, str]] = field(default_factory=list)  # (path, 原因)

    def all_changes(self) -> list[SymbolChange]:
        return [c for f in self.files for c in f.changes]

    def to_summary(self) -> dict[str, object]:
        changes = self.all_changes()
        counts = {k.value: 0 for k in ChangeKind}
        for c in changes:
            counts[c.kind.value] += 1
        added = sum(c.new.line_count for c in changes if c.new)
        removed = sum(c.old.line_count for c in changes if c.old)
        return {
            "files_analyzed": len(self.files),
            "files_skipped": len(self.skipped_files),
            "symbols_added": counts["added"],
            "symbols_removed": counts["removed"],
            "symbols_modified": counts["modified"],
            "symbols_renamed": counts["renamed"],
            "formatting_only": sum(1 for c in changes if c.formatting_only),
            "lines_in_new_version": added,
            "lines_in_old_version": removed,
            "redoc_targets": sum(1 for c in changes if c.needs_redoc),
        }
