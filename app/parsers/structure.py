"""把 ChangeSet 还原成"这个模块长什么样"：类、继承、字段、调用链、分支。

为什么要有这一层：类图和时序图最大的质量问题是**模型只能看到函数签名**，
看不到类与类之间的关系，于是它只能画一排孤立的类；时序图同理，它不知道
入口函数调用了谁，就只能写一句泛泛的 "process"。这些关系本来就在 AST 里，
只是之前解析完就被丢掉了。

设计约束：
1. 只做**事实**抽取，不做语义推断。拿不到就留空，让模型写 note，而不是猜。
2. 全部基于已解析的 Symbol 字段（owner/bases/class_attributes/calls/branches），
   因此天然适配 changes.json 的往返序列化，不需要重新读源码。
3. 输出的文本块是**给模型看的输入**，所以既要有信息量，又不能太长——
   一个模块最多列 12 个类、每个类最多 12 个方法。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 关系种类 -> PlantUML 箭头。渲染与校验共用同一张表，避免两边说法不一致。
ARROWS: dict[str, str] = {
    "extends": "--|>",
    "implements": "..|>",
    "composition": "*--",
    "aggregation": "o--",
    "dependency": "..>",
}

RELATION_LABELS: dict[str, str] = {
    "extends": "继承",
    "implements": "实现",
    "composition": "组合",
    "aggregation": "聚合",
    "dependency": "依赖",
}

#: 同一对类之间只能画一条线，按这个顺序取最强的那条
_KIND_PRIORITY: dict[str, int] = {
    "extends": 5,
    "implements": 4,
    "composition": 3,
    "aggregation": 2,
    "dependency": 1,
}

# Python 里出现这些基类说明是"抽象/协议"，画成 <<abstract>> 比画成普通类准确
_ABSTRACT_BASES = frozenset({"ABC", "ABCMeta", "Protocol", "Generic"})
# 标准库里这些基类画进类图只会增加噪音
_IGNORED_BASES = frozenset({"object"})

_MAX_CLASSES = 12
_MAX_METHODS_PER_CLASS = 12

#: 这些名字大写开头，但不是"类"：画成类框只会让图更吵
_BUILTIN_TYPE_NAMES = frozenset({
    "Any", "Callable", "Coroutine", "Dict", "Enum", "Float", "FrozenSet", "IO",
    "Iterable", "Iterator", "List", "Literal", "Mapping", "MutableMapping", "None",
    "Optional", "Path", "Sequence", "Set", "Tuple", "Type", "TypeVar", "Union",
    "Awaitable", "Exception", "BaseException", "IntEnum", "StrEnum", "Decimal",
    "DateTime", "UUID", "Self", "Protocol", "Generic", "ABC", "ABCMeta",
})


def _plausible_type(name: str) -> bool:
    """`UserRepository` 像类名，`active`/`dict` 不像。

    没有这条判断时，`self.repo: UserRepository` 里的 UserRepository 因为
    不在本模块的类表里而被丢掉，跨模块组合边就整条消失了。
    """
    if not name or name in _BUILTIN_TYPE_NAMES or name in _IGNORED_BASES:
        return False
    return name[0].isupper() and name.isidentifier() and len(name) > 1

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True)
class ClassInfo:
    """一个类的结构画像。methods 是"name(params) -> Ret"的可读形式。"""

    name: str
    qualified_name: str
    path: str
    bases: tuple[str, ...] = ()
    #: 与 bases 对齐的 "extends" / "implements"
    base_roles: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()

    def base_pairs(self) -> tuple[tuple[str, str], ...]:
        """(基类名, extends/implements)，缺角色时按 extends 兜底。"""
        roles = self.base_roles or ("extends",) * len(self.bases)
        return tuple(zip(self.bases, roles))

    @property
    def is_abstract(self) -> bool:
        return any(_short_type(base) in _ABSTRACT_BASES for base in self.bases)


@dataclass(frozen=True)
class FunctionInfo:
    qualified_name: str
    name: str
    owner: str
    path: str
    params: str = ""
    returns: str = ""
    calls: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    is_async: bool = False

    @property
    def display(self) -> str:
        suffix = f" -> {self.returns}" if self.returns else ""
        prefix = "async " if self.is_async else ""
        return f"{prefix}{self.qualified_name}({self.params}){suffix}"


@dataclass(frozen=True)
class Relation:
    source: str
    target: str
    kind: str
    evidence: str = ""

    @property
    def arrow(self) -> str:
        return ARROWS.get(self.kind, "-->")

    @property
    def label(self) -> str:
        return RELATION_LABELS.get(self.kind, self.kind)

    def render(self) -> str:
        tail = f"  ({self.evidence})" if self.evidence else ""
        return f"- {self.source} {self.arrow} {self.target} : {self.label}{tail}"

    def key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.kind)


@dataclass(frozen=True)
class ModuleStructure:
    module: str
    classes: tuple[ClassInfo, ...] = ()
    functions: tuple[FunctionInfo, ...] = ()
    relations: tuple[Relation, ...] = ()
    #: 入口函数 -> 调用链上的边（"A.x -> B.y"），按 BFS 顺序
    chains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 函数 -> 出现过的控制流关键字
    branches: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 这个模块里"看起来最像入口"的函数（调用链最长者优先）
    entry_points: tuple[str, ...] = ()
    #: 被这个模块引用、但定义在别的模块里的类型（组合/依赖边的另一端）
    externals: tuple[str, ...] = ()

    # ---------------------------------------------------------------- 查询
    def known_names(self) -> set[str]:
        """允许出现在图里的实体名：类短名 + 类全名 + 基类名 + 跨模块类型。"""
        names: set[str] = set(self.externals)
        for cls in self.classes:
            names.add(cls.name)
            names.add(cls.qualified_name)
            names.update(_short_type(base) for base in cls.bases)
        return {name for name in names if name}

    def expected_relations(self) -> set[tuple[str, str, str]]:
        """每对类只留一条最强的边。

        `UserService *-- UserRepository` 与 `UserService ..> UserRepository`
        同时成立时，图里只该画组合：同一对类之间铺两条线，读者要花时间
        分辨哪条才算数，而提示词里也明确禁止了这种画法。
        """
        strongest: dict[tuple[str, str], tuple[int, str]] = {}
        for relation in self.relations:
            pair = (relation.source, relation.target)
            rank = _KIND_PRIORITY.get(relation.kind, 0)
            if pair not in strongest or rank > strongest[pair][0]:
                strongest[pair] = (rank, relation.kind)
        return {(source, target, kind) for (source, target), (_rank, kind) in strongest.items()}

    def branchy_functions(self) -> set[str]:
        return {name for name, words in self.branches.items() if words}

    def chain_for(self, entry: str) -> tuple[str, ...]:
        return self.chains.get(entry, ())

    def best_entry_point(self) -> str:
        return self.entry_points[0] if self.entry_points else ""

    def has_content(self) -> bool:
        return bool(self.classes or self.functions)

    # ---------------------------------------------------------------- 渲染
    def render(self, *, entry_point: str | None = None) -> str:
        """把结构事实渲染成提示词里的一段文本。空结构返回空串。"""
        if not self.has_content():
            return ""

        lines: list[str] = ["## code structure (extracted from the AST; treat it as ground truth)"]
        if entry_point:
            lines.append(f"entry_point: {entry_point}")
        lines.append("")

        if self.classes:
            lines.append(f"classes ({len(self.classes)}):")
            for cls in self.classes:
                bases = ", ".join(cls.bases) if cls.bases else "(none)"
                marker = " [abstract]" if cls.is_abstract else ""
                lines.append(f"- {cls.name}{marker}  file={cls.path}  bases={bases}")
                if cls.attributes:
                    lines.append(f"    fields: {', '.join(cls.attributes)}")
                if cls.methods:
                    lines.append(f"    methods: {', '.join(cls.methods)}")
            lines.append("")

        if self.functions:
            lines.append("module level functions:")
            for func in self.functions:
                lines.append(f"- {func.display}  file={func.path}")
            lines.append("")

        if self.relations:
            lines.append("relations you MUST draw (they exist in the source; omitting them is an error):")
            keep = {relation.key() for relation in self.relations
                    if relation.key() in self.expected_relations()}
            for relation in self.relations:
                if relation.key() in keep:
                    # 同一对类只留最强的那条，避免"组合 + 依赖"两条线并排
                    lines.append(relation.render())
                    keep.discard(relation.key())
            lines.append("")

        if self.externals:
            lines.append("types referenced but declared in another module (declare the class box, no members needed):")
            lines.append("- " + ", ".join(self.externals))
            lines.append("")

        interesting: list[str] = []
        if entry_point and entry_point in self.chains:
            interesting.append(entry_point)
        interesting.extend(name for name in self.chains if name not in interesting)
        interesting = interesting[:4]
        if interesting:
            lines.append("call chains (caller -> callee, extracted from the AST):")
            for name in interesting:
                lines.append(f"- from {name}: {'; '.join(self.chains[name])}")
            lines.append("")

        branchy = [(name, words) for name, words in self.branches.items() if words]
        if branchy:
            lines.append("control flow found in each function (each one needs an alt/loop/group block):")
            for name, words in branchy[:8]:
                lines.append(f"- {name}: {', '.join(words)}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 构建


def _type_tokens(text: str) -> list[str]:
    """从 "dict[str, Order]" 这样的类型文本里抽出所有标识符。"""
    return _IDENTIFIER.findall(text or "")


def _short_type(text: str) -> str:
    """取出最主要的外层类型名。

    `Map<K, V>` → `Map`，`TTLCache | None` → `TTLCache`，
    `"UserRepository | None"`（字符串注解）→ `UserRepository`。
    联合类型取第一个非 None 的分支：`self.cache: Cache | None = None`
    在语义上就是"有一个 Cache 字段"，不该因为带了 None 就丢掉这条组合边。
    """
    cleaned = (text or "").strip().strip('"').strip("'").strip()
    if not cleaned:
        return ""
    for part in cleaned.split("|"):
        candidate = part.split("[")[0].strip().strip('"').strip("'")
        if candidate and candidate not in {"None", "NoneType", "null"}:
            return candidate
    return ""


def build_structure(
    change_set,
    *,
    module: str = "",
    max_call_depth: int = 4,
    context: tuple[str, ...] | set[str] | None = None,
) -> ModuleStructure:
    """从 ChangeSet 里抽出模块结构。

    只吃 `new` 版本：已经删掉的函数/类不该出现在"当前架构"里。
    路径从 FileDiff 上取（Symbol 本身不记路径，它只记 qualified_name）。

    `context` 是整个仓库里可见的类名。模块内的类图如果只认本模块的类型，
    `UserService *-- UserRepository` 这种跨模块组合就会整条消失——而它恰恰是
    类图最有价值的那条边。
    """
    pairs: list[tuple[object, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    changed_names: set[str] = set()
    for file_diff in change_set.files:
        # 先放整文件的符号，再放改动符号：前者的类节点带着基类与字段，
        # 后者的 __init__ 也常常没变，光看改动函数推不出字段类型。
        for symbol in getattr(file_diff, "new_symbols", ()) or ():
            marker = (file_diff.path, symbol.key)
            if marker in seen_keys:
                continue
            seen_keys.add(marker)
            pairs.append((symbol, file_diff.path))
        for change in file_diff.changes:
            if change.new is None:
                continue
            changed_names.add(change.new.qualified_name)
            marker = (file_diff.path, change.new.key)
            if marker in seen_keys:
                continue
            seen_keys.add(marker)
            pairs.append((change.new, file_diff.path))

    # ---- 类聚合：同一个类的每个方法都带一份 (bases, fields)，按 owner 合并
    class_seed: dict[str, ClassInfo] = {}
    methods_by_owner: dict[str, list[str]] = {}
    for symbol, path in pairs:
        owner = getattr(symbol, "owner", "") or ""
        if not owner:
            continue
        qualified_owner = ".".join(symbol.qualified_name.split(".")[:-1]) or owner
        seed = class_seed.get(qualified_owner)
        bases = tuple(getattr(symbol, "bases", ()) or ())
        base_roles = tuple(getattr(symbol, "base_roles", ()) or ())
        attributes = tuple(getattr(symbol, "class_attributes", ()) or ())
        if seed is None:
            class_seed[qualified_owner] = ClassInfo(
                name=owner,
                qualified_name=qualified_owner,
                path=path,
                bases=bases,
                base_roles=base_roles,
                attributes=attributes,
            )
        else:
            class_seed[qualified_owner] = ClassInfo(
                name=seed.name,
                qualified_name=seed.qualified_name,
                path=seed.path,
                bases=seed.bases or bases,
                base_roles=seed.base_roles or base_roles,
                attributes=seed.attributes or attributes,
            )
        display = _method_display(symbol)
        bucket = methods_by_owner.setdefault(qualified_owner, [])
        if display not in bucket:
            bucket.append(display)

    class_infos = tuple(
        ClassInfo(
            name=seed.name,
            qualified_name=seed.qualified_name,
            path=seed.path,
            bases=seed.bases,
            base_roles=seed.base_roles,
            attributes=seed.attributes,
            methods=tuple(methods_by_owner.get(key, ())[:_MAX_METHODS_PER_CLASS]),
        )
        for key, seed in sorted(class_seed.items())
    )[:_MAX_CLASSES]

    functions = tuple(
        FunctionInfo(
            qualified_name=symbol.qualified_name,
            name=symbol.name,
            owner="",
            path=path,
            params=symbol.params,
            returns=getattr(symbol, "returns", "") or "",
            calls=tuple(getattr(symbol, "calls", ()) or ()),
            branches=tuple(getattr(symbol, "branches", ()) or ()),
            is_async=bool(getattr(symbol, "is_async", False)),
        )
        for symbol, path in pairs
        if not getattr(symbol, "owner", "")
    )

    own_names = {info.name for info in class_infos} | {info.qualified_name for info in class_infos}
    visible = {_short_type(name) for name in (context or ())}
    visible |= {name for name in (context or ()) if name}
    known = own_names | {name for name in visible if name}

    # `self.cache = cache` 这种写法在 AST 里看不出类型，只能靠 __init__ 的参数注解
    # 反推：参数名与字段名同名、且参数类型是本模块里的类，就是一条组合关系。
    ctor_types: dict[str, dict[str, str]] = {}
    for symbol, _path in pairs:
        if symbol.name != "__init__":
            continue
        owner = ".".join(symbol.qualified_name.split(".")[:-1])
        ctor_types[owner] = _param_types(symbol.params)

    # ---- 关系：继承、组合/聚合、依赖
    relations: list[Relation] = []
    seen_relations: set[tuple[str, str, str]] = set()

    def add_relation(source: str, target: str, kind: str, evidence: str = "") -> None:
        if not source or not target or source == target:
            return
        key = (source, target, kind)
        if key in seen_relations:
            return
        seen_relations.add(key)
        relations.append(Relation(source=source, target=target, kind=kind, evidence=evidence))

    for info in class_infos:
        for base, role in info.base_pairs():
            base_name = _short_type(base)
            if not base_name or base_name in _IGNORED_BASES:
                continue
            # ABC/Protocol 这类只是"这是个抽象基类"的标记，不是要画进图里的业务父类。
            # 保留它们会让 expected_relations 里出现模型根本无法声明的目标（E014 永远无法满足）。
            if base_name in _ABSTRACT_BASES:
                continue
            kind = role if role in {"extends", "implements"} else "extends"
            evidence = "源码里的实现接口" if kind == "implements" else "源码里的基类"
            add_relation(info.name, base_name, kind, evidence)
        for attribute in info.attributes:
            if ":" not in attribute:
                # 只有字段名，类型丢了：用同名构造参数的注解补上
                inferred = ctor_types.get(info.qualified_name, {}).get(attribute.strip(), "")
                target = _short_type(inferred) if inferred else ""
                if target and (target in known or _plausible_type(target)):
                    add_relation(info.name, target, "composition", f"字段 {attribute.strip()}")
                continue
            name, _, type_text = attribute.partition(":")
            type_name = _short_type(type_text)
            if type_name in known or _plausible_type(type_name):
                kind = "aggregation" if "[" in type_text else "composition"
                add_relation(info.name, type_name, kind, f"字段 {name.strip()}")
                continue
            for token in _type_tokens(type_text):
                if token in known or _plausible_type(token):
                    add_relation(info.name, token, "dependency", f"字段 {name.strip()}")
                    break

    # 参数/返回值里的类 = 真实依赖。比"调用"更可靠：调用点太多，画出来是满天虚线。
    for symbol, _path in pairs:
        owner = getattr(symbol, "owner", "") or ""
        if not owner:
            continue
        tokens = {
            token
            for token in _type_tokens(symbol.params)
            if token in known or _plausible_type(token)
        }
        if getattr(symbol, "returns", ""):
            tokens.update(
                token
                for token in _type_tokens(symbol.returns)
                if token in known or _plausible_type(token)
            )
        tokens.discard(owner)
        for token in sorted(tokens):
            add_relation(owner, token, "dependency", f"{symbol.name} 的参数/返回类型")

    # ---- 调用链与分支
    index = _function_index(pairs)
    by_name: dict[str, list[FunctionInfo]] = {}
    for info in index:
        by_name.setdefault(info.name, []).append(info)

    # 字段名 -> 字段类型，用来把 `self.repo.find()` 这种调用解析到 UserRepository。
    # 没有这张表时，`find` 同时存在于 UserReader 与 UserRepository，只能放弃解析，
    # 时序图就断在最关键的那一跳上。
    field_types: dict[str, dict[str, str]] = {}
    for info in class_infos:
        mapping: dict[str, str] = {}
        for attribute in info.attributes:
            field_name, _, type_text = attribute.partition(":")
            if type_text.strip():
                mapping[field_name.strip()] = _short_type(type_text)
        for param_name, type_text in ctor_types.get(info.qualified_name, {}).items():
            mapping.setdefault(param_name, _short_type(type_text))
        field_types[info.qualified_name] = mapping

    chains: dict[str, tuple[str, ...]] = {}
    branches: dict[str, tuple[str, ...]] = {}
    for info in index:
        if info.branches:
            branches[info.qualified_name] = info.branches
        edges = _walk_chain(info, by_name, field_types=field_types, max_depth=max_call_depth)
        if edges:
            chains[info.qualified_name] = edges

    # 时序图的入口必须是"这次真的改了的函数"。上下文符号补进来之后，
    # 没改过的 build_service 也可能有调用链，让它当选会画出与本次改动无关的图。
    entry_candidates = {name: edges for name, edges in chains.items() if name in changed_names}
    if not entry_candidates:
        entry_candidates = chains
    entry_points = tuple(
        sorted(entry_candidates, key=lambda name: (-len(entry_candidates[name]), name))[:5]
    )

    externals = tuple(sorted({r.target for r in relations if r.target not in own_names}))

    return ModuleStructure(
        module=module,
        classes=class_infos,
        functions=functions,
        relations=tuple(relations),
        chains=chains,
        branches=branches,
        entry_points=entry_points,
        externals=externals,
    )


def _method_display(symbol) -> str:
    suffix = f" -> {symbol.returns}" if getattr(symbol, "returns", "") else ""
    prefix = "async " if getattr(symbol, "is_async", False) else ""
    return f"{prefix}{symbol.name}({symbol.params}){suffix}"


def _param_types(params: str) -> dict[str, str]:
    """把 "self, cache: Cache, size: int = 16" 拆成 {参数名: 类型文本}。

    没有注解的参数直接跳过——没有类型的猜测没有价值。
    """
    found: dict[str, str] = {}
    depth = 0
    buffer = ""
    parts: list[str] = []
    for char in params or "":
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append(buffer)
            buffer = ""
        else:
            buffer += char
    if buffer.strip():
        parts.append(buffer)
    for part in parts:
        if ":" not in part:
            continue
        name, _, type_text = part.partition(":")
        clean_name = name.strip().lstrip("*")
        clean_type = type_text.split("=")[0].strip()
        if clean_name and clean_type:
            found[clean_name] = clean_type
    return found


def _function_index(pairs: list[tuple[object, str]]) -> tuple[FunctionInfo, ...]:
    return tuple(
        FunctionInfo(
            qualified_name=symbol.qualified_name,
            name=symbol.name,
            owner=getattr(symbol, "owner", "") or "",
            path=path,
            params=symbol.params,
            returns=getattr(symbol, "returns", "") or "",
            calls=tuple(getattr(symbol, "calls", ()) or ()),
            branches=tuple(getattr(symbol, "branches", ()) or ()),
            is_async=bool(getattr(symbol, "is_async", False)),
        )
        for symbol, path in pairs
    )


def _external_callee(owner_type: str, name: str) -> FunctionInfo:
    """合成一个"定义在别的模块"的 callee 节点。

    只用来把调用链连起来（`UserService.stats -> UserRepository.count`）；
    它的 calls 是空的，BFS 到这里就停，不会编出更深的边。
    """
    return FunctionInfo(
        qualified_name=f"{owner_type}.{name}",
        name=name,
        owner=owner_type,
        path="",
        params="",
        returns="",
        calls=(),
        branches=(),
        is_async=False,
    )


def _receiver_hint(
    caller: FunctionInfo,
    receiver_name: str,
    field_types: dict[str, dict[str, str]] | None,
) -> tuple[str, ...]:
    """接收者的类型：先查字段类型，再查参数注解。都没有就返回空。"""
    owner_key = ".".join(caller.qualified_name.split(".")[:-1])
    hinted = (field_types or {}).get(owner_key, {}).get(receiver_name, "")
    if not hinted:
        # `service.register(...)`：service 是参数，类型写在签名里
        hinted = _param_types(caller.params).get(receiver_name, "")
    if not hinted:
        return ()
    tokens = tuple(_type_tokens(hinted))
    return tokens or (_short_type(hinted),)


def _resolve(
    caller: FunctionInfo,
    target: str,
    by_name: dict[str, list[FunctionInfo]],
    field_types: dict[str, dict[str, str]] | None = None,
) -> FunctionInfo | None:
    """把 "repo.find" / "find" / "_touch" 解析成模块内的函数。

    歧义时返回 None：猜错会画出一张看起来合理、实际完全错误的时序图。
    唯一的例外是接收者能靠类型定死（`repo.find` 且 `self.repo: UserRepository`），
    这时不是猜，是算出来的。
    """
    name = target.split(".")[-1].strip()
    candidates = by_name.get(name, ())
    receiver = target.rsplit(".", 1)[0] if "." in target else ""
    receiver_name = receiver.split(".")[-1] if receiver else ""

    if not candidates:
        # 名字不在本模块的符号表里，但接收者类型写得很清楚：这是跨模块调用
        # （`self.repo.count()` 而 UserRepository 定义在 src.domain）。
        # 凭类型断言"调的是 UserRepository.count"不算猜；少了这条，
        # `UserService.stats -> UserRepository.count` 会整条消失。
        for token in _receiver_hint(caller, receiver_name, field_types):
            if _plausible_type(token):
                return _external_callee(token, name)
        return None

    if receiver:
        # 带接收者的调用只能靠类型定死，不能退回"同名唯一就算它"：
        # `data.get("email")` 里的 data 是局部变量，退回去就会连到 TTLCache.get，
        # 画出一条源代码里根本不存在的边。
        hinted = _receiver_hint(caller, receiver_name, field_types)
        if hinted:
            hinted_tokens = set(hinted)
            matches = [c for c in candidates if c.owner in hinted_tokens]
            if len(matches) == 1:
                return matches[0]
            for token in hinted_tokens:
                if _plausible_type(token):
                    return _external_callee(token, name)
        # 静态调用 / 类名直接调用：接收者本身就是类名
        by_owner = [c for c in candidates if c.owner == receiver_name]
        if len(by_owner) == 1:
            return by_owner[0]
        return None

    if len(candidates) == 1:
        return candidates[0]
    same_owner = [c for c in candidates if c.owner and c.owner == caller.owner]
    if len(same_owner) == 1:
        return same_owner[0]
    return None


def _walk_chain(
    entry: FunctionInfo,
    by_name: dict[str, list[FunctionInfo]],
    *,
    field_types: dict[str, dict[str, str]] | None = None,
    max_depth: int = 4,
    max_edges: int = 12,
) -> tuple[str, ...]:
    """从入口出发做 BFS，返回 "caller -> callee" 形式的边列表。"""
    edges: list[str] = []
    seen: set[str] = {entry.qualified_name}
    frontier: list[tuple[FunctionInfo, int]] = [(entry, 0)]
    while frontier and len(edges) < max_edges:
        current, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for target in current.calls:
            callee = _resolve(current, target, by_name, field_types)
            if callee is None or callee.qualified_name in seen:
                continue
            seen.add(callee.qualified_name)
            edges.append(f"{current.qualified_name} -> {callee.qualified_name}")
            frontier.append((callee, depth + 1))
            if len(edges) >= max_edges:
                break
    return tuple(edges)