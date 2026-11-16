"""新旧两版 AST 的比对。

为什么不用 git 的文本 diff 直接判断"哪个函数变了"：
因为文本 diff 只知道行了，不知道这段行属于哪个函数 ——
一个 hunk 可能横跨两个函数的边界，也可能只是把函数的结尾挪了一行。
tree-sitter 已经给了我们每个函数的字节区间和语法指纹，直接比指纹更准。

比对分四步（顺序不能乱）：
1. 按 key 求集合差 -> 新增 / 删除。
2. 同名 key 比 semantic_hash -> 真修改；raw 变而 semantic 不变 -> 纯格式化。
3. 在"新增"与"删除"之间按 body_hash 配对 -> 重命名/移动（函数体没动，只改了签名）。
4. 剩下的才是真的新增和删除。
"""

from __future__ import annotations

from .model import ChangeKind, FileDiff, FileSymbols, Symbol, SymbolChange


def _same_key_changes(old: FileSymbols, new: FileSymbols) -> list[SymbolChange]:
    changes: list[SymbolChange] = []
    old_by_key = old.by_key()
    new_by_key = new.by_key()
    for key in sorted(set(old_by_key) & set(new_by_key)):
        before = old_by_key[key]
        after = new_by_key[key]
        if before.semantic_hash == after.semantic_hash:
            # 语义没变。若原文也没变，就是完全没动的函数，跳过。
            if before.raw_hash == after.raw_hash:
                continue
            # 原文变了但语义没变：缩进、换行、注释。值得报告，但不该触发重新生成文档。
            changes.append(
                SymbolChange(kind=ChangeKind.MODIFIED, old=before, new=after, formatting_only=True)
            )
        else:
            changes.append(
                SymbolChange(kind=ChangeKind.MODIFIED, old=before, new=after, formatting_only=False)
            )
    return changes


def _pair_renames(
    added: list[Symbol], removed: list[Symbol]
) -> tuple[list[SymbolChange], list[Symbol], list[Symbol]]:
    """把"函数体完全一样但 key 变了"的两两配对，识别为重命名。

    只做 1:1 配对：同一个 body_hash 出现多次时直接放弃配对，
    宁可报成"删一个、加一个"，也不猜 —— 猜错的代价是文档结构错乱。
    （一开始这里用的是 raw_hash，结果改名用例永远配不上对：改名必然改原文。
    换成只覆盖函数体的 body_hash 才对。）
    """
    def _key(symbol: Symbol) -> str:
        return symbol.body_hash or symbol.raw_hash

    removed_by_hash: dict[str, list[Symbol]] = {}
    for symbol in removed:
        removed_by_hash.setdefault(_key(symbol), []).append(symbol)
    added_by_hash: dict[str, list[Symbol]] = {}
    for symbol in added:
        added_by_hash.setdefault(_key(symbol), []).append(symbol)

    renamed: list[SymbolChange] = []
    matched_added: set[str] = set()
    matched_removed: set[str] = set()
    for raw_hash, new_group in added_by_hash.items():
        old_group = removed_by_hash.get(raw_hash)
        if old_group is None or len(old_group) != 1 or len(new_group) != 1:
            continue
        before, after = old_group[0], new_group[0]
        if before.qualified_name == after.qualified_name:
            # 函数体一样、限定名也一样，只有参数不同 —— 这不是重命名，
            # 是把 load(long) 换成了 load(String)，对调用方是破坏性变更，
            # 必须报成"删一个 + 加一个"，不能悄悄说成改名。
            continue
        renamed.append(
            SymbolChange(kind=ChangeKind.RENAMED, old=before, new=after, formatting_only=False)
        )
        matched_added.add(after.key)
        matched_removed.add(before.key)

    remaining_added = [s for s in added if s.key not in matched_added]
    remaining_removed = [s for s in removed if s.key not in matched_removed]
    return renamed, remaining_added, remaining_removed


def diff_symbols(
    path: str,
    old: FileSymbols | None,
    new: FileSymbols | None,
) -> FileDiff:
    """比对同一文件的两个版本。old/new 任一为 None 表示文件被新增或删除。"""
    if old is None and new is None:
        raise ValueError("both versions are None")

    # 新增文件：全部函数都是 added
    if old is None:
        assert new is not None
        changes = [
            SymbolChange(kind=ChangeKind.ADDED, old=None, new=s) for s in new.symbols
        ]
        return FileDiff(path=path, language=new.language, changes=tuple(changes))

    # 删除文件：全部函数都是 removed（下游据此删除文档里的对应章节）
    if new is None:
        changes = [
            SymbolChange(kind=ChangeKind.REMOVED, old=s, new=None) for s in old.symbols
        ]
        return FileDiff(path=path, language=old.language, changes=tuple(changes))

    old_by_key = old.by_key()
    new_by_key = new.by_key()
    added = [new_by_key[k] for k in new_by_key.keys() - old_by_key.keys()]
    removed = [old_by_key[k] for k in old_by_key.keys() - new_by_key.keys()]

    renamed, added, removed = _pair_renames(added, removed)

    changes = _same_key_changes(old, new) + renamed
    changes += [SymbolChange(kind=ChangeKind.ADDED, old=None, new=s) for s in added]
    changes += [SymbolChange(kind=ChangeKind.REMOVED, old=s, new=None) for s in removed]

    def sort_key(change: SymbolChange) -> tuple[int, str]:
        anchor = change.new or change.old
        return (anchor.start_line if anchor else 0, change.key)  # type: ignore[union-attr]

    changes.sort(key=sort_key)
    return FileDiff(
        path=path,
        language=new.language,
        changes=tuple(changes),
        old_has_error=old.has_error,
        new_has_error=new.has_error,
    )
