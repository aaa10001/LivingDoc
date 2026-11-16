"""基于 tree-sitter 的函数提取器。

设计要点（三条，都是踩过坑才定下来的）：

1. 用 Query（S-expression）抓节点，而不是 Python 里递归 n.type 判断。
   Query 在 C 层执行，且新增语言时只改 profiles.py。
   但 Query 抓不到"外层是谁"，所以外层类名靠 parent 链回溯 —— 混合策略。

2. 语义指纹 semantic_hash 跳过 comment 节点、只收录叶子文本。
   tree-sitter 的叶子节点不含前后空白，因此纯缩进/换行变更天然不改变指纹。
   这是"改个缩进不要重生成整篇文档"的实现基础。

3. 行号是 1-based 闭区间。tree-sitter 的 Point 是 0-based，且 end_point
   指向最后一个字符"之后"，所以 end_line 要按 column 是否为 0 分情况。
"""

from __future__ import annotations

import bisect
import hashlib
import importlib
import re
import threading
import time
from typing import Any

from .model import FileSymbols, Symbol, SymbolKind
from .profiles import LanguageProfile, profile_for_path


class UnsupportedLanguage(Exception):
    """扩展名不在支持列表里。调用方应跳过该文件，而不是让整次分析失败。"""

    def __init__(self, path: str) -> None:
        super().__init__(f"unsupported language for file: {path}")
        self.path = path


# profile.name -> 对应的 tree-sitter 语言包模块名
_LANGUAGE_MODULES = {
    "python": "tree_sitter_python",
    "java": "tree_sitter_java",
}

_cache: dict[str, tuple[Any, Any, Any]] = {}  # name -> (Language, Parser, Query)
_cache_lock = threading.Lock()


def _captures(query: Any, root: Any) -> tuple[dict[str, list[Any]], Any]:
    """跑 Query 拿捕获结果，并把 cursor 一起返回。

    tree-sitter 0.25 起把 Query.captures() 移到了 QueryCursor 上：
        QueryCursor(query).captures(node) -> {捕获名: [Node, ...]}
    老版本反过来是 query.captures(node)。两个都兼容，免得锁死版本。

    必须把 cursor 返回给调用方持有：查询结果里的 Node 是游标持有的 C 结构的
    轻量视图，cursor 一旦被 GC 回收，再访问这些 Node 的 children / text 就是
    野指针。实测表现是 Windows 上随机 access violation（exit code -1073741819），
    而且只在"分配量大到触发 GC"的文件上复现 —— 极难排查，所以这里写死。
    """
    try:
        from tree_sitter import QueryCursor
    except ImportError:  # pragma: no cover - 老版本
        return query.captures(root), None

    cursor = QueryCursor(query)
    return cursor.captures(root), cursor


def _load(profile: LanguageProfile) -> tuple[Any, Any, Any]:
    """懒加载并缓存 (Language, Parser, Query)。

    Parser 不是线程安全的（内部有可变状态），所以每个线程各持一个：
    这里用 threading.local 存 Parser，Language/Query 是只读的可以共享。
    """
    with _cache_lock:
        entry = _cache.get(profile.name)
        if entry is not None:
            return entry

        from tree_sitter import Language, Parser, Query

        module_name = _LANGUAGE_MODULES.get(profile.name)
        if module_name is None:
            raise UnsupportedLanguage(f"no tree-sitter module for language {profile.name}")
        module = importlib.import_module(module_name)

        language = Language(module.language())
        # 注意 0.24+ 的 API：Parser(language) 构造，Query(language, source)
        parser = Parser(language)
        query = Query(language, profile.query)
        _cache[profile.name] = (language, parser, query)
        return language, parser, query


def _text(node: Any, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _semantic_hash(node: Any, comment_types: frozenset[str], source: bytes) -> str:
    """结构指纹：跳过注释，只对"节点类型 + 叶子文本"做序列化后 hash。

    用迭代式 DFS（带 closing 标记）而不是递归，避免深层嵌套触发
    RecursionError —— 生成的代码、minified 风格的 Java 都能有几百层嵌套。
    """
    digest = hashlib.sha256()
    stack: list[tuple[Any, bool]] = [(node, False)]
    while stack:
        current, closing = stack.pop()
        if closing:
            digest.update(b")")
            continue
        if current.type in comment_types:
            continue  # 注释整棵子树跳过：改注释不影响语义
        digest.update(b"(")
        digest.update(current.type.encode())
        if current.child_count == 0:
            digest.update(b"=")
            # 叶子文本从 source 切片取，不用 Node.text：
            # 0.26 的 Node 不持有 Tree 引用，跨 GC 访问 .text 会拿到野指针
            # （Windows 上表现为随机 access violation，退出码 -1073741819）。
            digest.update(source[current.start_byte : current.end_byte])
            digest.update(b")")
            continue
        stack.append((current, True))
        for child in reversed(current.children):
            stack.append((child, False))
    return digest.hexdigest()


def _wrapped(node: Any, profile: LanguageProfile) -> Any:
    """返回应该代表"整个函数"的那个节点。

    Python 的装饰器在父节点 decorated_definition 上；不回溯的话，
    @app.get("/users") 改了我们也发现不了 —— 而它恰恰是 API 文档的入口。
    """
    wrapper = profile.decorator_wrapper
    if wrapper and node.parent is not None and node.parent.type == wrapper:
        return node.parent
    return node


def _collect_decorators(node: Any, profile: LanguageProfile, source: bytes) -> tuple[str, ...]:
    node_type = profile.decorator_node
    if not node_type:
        return ()
    found: list[str] = []
    # Python: 装饰器是 decorated_definition 的直接子节点
    # Java: 注解在 modifiers 里，也可能直接挂在声明上
    search_roots = [node]
    for child in getattr(node, "children", []):
        if child.type == "modifiers":
            search_roots.append(child)
    for root in search_roots:
        for child in getattr(root, "children", []):
            if child.type == node_type or child.type == "marker_annotation":
                found.append(_text(child, source).strip())
    return tuple(found)


def _enclosing_names(node: Any, profile: LanguageProfile, source: bytes) -> list[str]:
    """沿 parent 链收集外层命名，用于拼 qualified_name。

    类与（嵌套）函数都算一层：内部函数会得到 outer.<locals>.inner 的效果，
    只是这里不写 <locals>，直接 outer.inner。
    """
    names: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in profile.class_types or current.type in profile.function_types:
            name_node = current.child_by_field_name("name")
            if name_node is not None:
                names.append(_text(name_node, source))
        current = current.parent
    names.reverse()
    return names


def _normalize_params(text: str) -> str:
    """参数文本归一化：压掉多余空白，并去掉外层括号。

    parameters 节点本身就带括号，去掉后 key 才不会变成 load((int id))。
    """
    text = " ".join(text.split())
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


_COMMENT_LINE_PREFIXES = ("#", "//", "/*", "*", '"""', "'''")

# 控制流节点 -> 提示词/校验器认识的关键字。之所以按节点类型而不是按文本匹配，
# 是因为文本里的 "if" 可能出现在字符串或注释里。
_BRANCH_KEYWORDS = {
    "if_statement": "if",
    "elif_clause": "elif",
    "else_clause": "else",
    "conditional_expression": "if",
    "for_statement": "for",
    "while_statement": "while",
    "try_statement": "try",
    "except_clause": "except",
    "except_statement": "except",
    "catch_clause": "catch",
    "finally_clause": "finally",
    "with_statement": "with",
    "match_statement": "match",
    "case_clause": "case",
    "switch_expression": "switch",
    "switch_block": "switch",
    "do_statement": "do",
    "synchronized_statement": "synchronized",
}

# 单个函数最多记录多少条调用/分支：这些数字会进 Prompt，必须封顶；
# 一个 600 行的 handler 能把上下文吃干净。
_MAX_CALLS = 24
_MAX_BRANCHES = 8
_MAX_ATTRIBUTES = 12

_INSTANCE_ATTR_ANNOTATED = re.compile(r"self\.([A-Za-z_]\w*)\s*:\s*([\w\.\[\]]+)\s*=")
_INSTANCE_ATTR_CTOR = re.compile(r"self\.([A-Za-z_]\w*)\s*=\s*([A-Z][\w\.]*)\s*\(")
_INSTANCE_ATTR_PLAIN = re.compile(r"self\.([A-Za-z_]\w*)\s*=")


def _signature_text(source: bytes, start: int, end: int) -> str:
    """截取"函数头部"：从装饰器/注解开始，到函数体第一行之前。

    直接按 body 节点的 start_byte 切不行 —— Python 的函数体块节点会把开头
    的注释跳过（extra 节点），导致签名里混进一行注释。所以这里从尾部回退，
    把注释行和空行裁掉。
    """
    text = source[start:end].decode("utf-8", "replace")
    lines = text.splitlines()
    while lines:
        stripped = lines[-1].strip()
        if not stripped or stripped.startswith(_COMMENT_LINE_PREFIXES):
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


def _kind_for(node_type: str, name: str, in_class: bool) -> SymbolKind:
    if node_type == "constructor_declaration" or name == "__init__":
        return SymbolKind.CONSTRUCTOR
    if in_class:
        return SymbolKind.METHOD
    return SymbolKind.FUNCTION


class _LineIndex:
    """把字节偏移换算成 1-based 行号。

    为什么不用 Node.start_point / Node.end_point（官方 API）：
    tree-sitter 0.26.0 的 Windows wheel 在"行内列号 > 255"时物化 Point 会让
    进程 access violation（退出码 -1073741819，Python 层连 traceback 都没有）。
    实测 253 列正常、313 列必崩，所以只要项目里有一行超过 255 字符的代码
    （压缩过的 JS 风格、长字符串常量很常见）整条分析流水线就会静默死掉。

    这里改成对换行位置做一次索引 + 二分，既绕开缺陷，也比 Point 更快。
    """

    __slots__ = ("_newlines",)

    def __init__(self, source: bytes) -> None:
        self._newlines = [i for i, byte in enumerate(source) if byte == 0x0A]

    def line_of(self, byte_offset: int) -> int:
        return bisect.bisect_left(self._newlines, byte_offset) + 1

    def span(self, start_byte: int, end_byte: int) -> tuple[int, int]:
        """返回 (起始行, 结束行)，闭区间。

        end_byte 指向最后一个字符之后，所以按 end_byte - 1 回退一格；
        节点以换行结尾时这正好落在该行上，不会多算一行。
        """
        start_line = self.line_of(start_byte)
        end_line = self.line_of(max(start_byte, end_byte - 1))
        return start_line, end_line


class TreeSitterExtractor:
    """把一个源码文件解析成 FileSymbols。

    这个类是无状态的（除内部缓存），可以放心在多线程 worker 里复用；
    同一实例被并发调用时，Parser 由 _load 里的锁保护创建过程。
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def supports(self, path: str) -> bool:
        return profile_for_path(path) is not None

    def _parser_for(self, profile: LanguageProfile) -> tuple[Any, Any]:
        _language, parser, query = _load(profile)
        # Parser 不能跨线程共享：每个线程各建一个（Query 是只读的，可共享）
        local_parser = getattr(self._local, "parser", None)
        if local_parser is None or getattr(self._local, "language", None) != profile.name:
            from tree_sitter import Parser

            local_parser = Parser(_language)
            self._local.parser = local_parser
            self._local.language = profile.name
        return local_parser, query

    def parse(self, path: str, source: bytes) -> FileSymbols:
        profile = profile_for_path(path)
        if profile is None:
            raise UnsupportedLanguage(path)

        started = time.perf_counter()
        parser, query = self._parser_for(profile)
        tree = parser.parse(source)
        root = tree.root_node

        captures, cursor = _captures(query, root)
        function_nodes = list(captures.get("fn", []))
        # 保持 cursor 存活到解析结束（理由见 _captures 的注释）
        cursor = cursor

        lines = _LineIndex(source)
        symbols: list[Symbol] = []
        # 同一个类的每个方法都要带同样的基类/字段；按类节点缓存，别每个方法重算一遍
        class_cache: dict[tuple[int, int], tuple[str, tuple[str, ...], tuple[str, ...]]] = {}
        for node in function_nodes:
            # 定义在另一个函数体内的嵌套函数：先收录，但 qualified_name 会带上前缀。
            # 阶段四做 Prompt 时可以按 qualified_name 里点号的数量过滤掉它们。
            names = _enclosing_names(node, profile, source)
            name_node = node.child_by_field_name("name")
            name = _text(name_node, source) if name_node is not None else node.type
            # 构造器的名字就是类名，再拼一次会得到 UserService.UserService
            qualified = ".".join(names) if (names and name == names[-1]) else ".".join([*names, name])

            outer = _wrapped(node, profile)
            params_node = node.child_by_field_name("parameters")
            params = _normalize_params(_text(params_node, source))

            body_start = outer.start_byte
            body_node = node.child_by_field_name("body")
            signature_end = body_node.start_byte if body_node is not None else node.end_byte
            signature = _signature_text(source, body_start, signature_end)
            body = source[body_start : node.end_byte].decode("utf-8", "replace")

            in_class = any(
                p.type in profile.class_types
                for p in _parents(node)
            )
            kind = _kind_for(node.type, name, in_class)

            # 身份键：支持重载的语言必须带参数类型，否则两个 add() 会被合并成一个
            key = f"{qualified}({params})" if profile.overloads else qualified

            # ---- 结构事实：类是谁、继承了谁、有哪些字段、调用了谁、有哪些分支
            owner_node = None
            for parent in _parents(node):
                if parent.type in profile.class_types:
                    owner_node = parent
                    break
            owner = ""
            bases: tuple[str, ...] = ()
            base_roles: tuple[str, ...] = ()
            attributes: tuple[str, ...] = ()
            if owner_node is not None:
                owner_name_node = owner_node.child_by_field_name("name")
                owner = _text_of(owner_name_node, source) if owner_name_node is not None else ""
                cache_key = (owner_node.start_byte, owner_node.end_byte)
                cached = class_cache.get(cache_key)
                if cached is None:
                    # 实例字段要从**整个类**的源码里扫，而不是从当前方法的函数体里扫：
                    # 否则 self.repo 只在 __init__ 那条记录上，同一个类的其他方法
                    # 就成了"没有字段的类"，结构清单会自相矛盾。
                    merged: dict[str, str] = {}
                    for entry in (
                        *_class_attributes(owner_node, profile, source),
                        *_instance_attributes(_text_of(owner_node, source)),
                    ):
                        merged.setdefault(entry.split(":", 1)[0].strip(), entry)
                    cached = (
                        owner,
                        _base_names(owner_node, profile, source),
                        _base_roles(owner_node, profile, source),
                        tuple(merged.values())[:_MAX_ATTRIBUTES],
                    )
                    class_cache[cache_key] = cached
                owner, bases, base_roles, attributes = cached

            returns = ""
            return_field = getattr(profile, "return_field", "return_type")
            return_node = node.child_by_field_name(return_field)
            if return_node is None and return_field != "return_type":
                return_node = node.child_by_field_name("return_type")
            if return_node is not None:
                returns = " ".join(_text_of(return_node, source).split())

            is_async = _text_of(node, source).lstrip().startswith("async")
            if not is_async and profile.name == "java":
                modifiers = next(
                    (c for c in getattr(node, "children", []) or [] if c.type == "modifiers"),
                    None,
                )
                is_async = modifiers is not None and "async" in _text_of(modifiers, source)

            start_line, end_line = lines.span(outer.start_byte, node.end_byte)

            raw_bytes = source[outer.start_byte : node.end_byte]
            raw_hash = hashlib.sha256(raw_bytes).hexdigest()
            # 函数体指纹：从 body 子节点起算，签名（def 行、参数、装饰器）不参与。
            # 这样"只改了函数名"的两版会得到同一个指纹，才能被识别成重命名。
            if body_node is not None:
                body_hash = hashlib.sha256(source[body_node.start_byte : node.end_byte]).hexdigest()
            else:
                body_hash = raw_hash

            symbols.append(
                Symbol(
                    language=profile.name,
                    kind=kind,
                    name=name,
                    qualified_name=qualified,
                    key=key,
                    start_line=start_line,
                    end_line=end_line,
                    start_byte=outer.start_byte,
                    end_byte=node.end_byte,
                    signature=signature,
                    body=body,
                    params=params,
                    decorators=_collect_decorators(outer if profile.decorator_wrapper else node, profile, source),
                    raw_hash=raw_hash,
                    body_hash=body_hash,
                    semantic_hash=_semantic_hash(outer, profile.comment_types, source),
                    owner=owner,
                    bases=bases,
                    base_roles=base_roles,
                    class_attributes=attributes,
                    returns=returns,
                    calls=_calls(node, profile, source),
                    branches=_branches(node, profile),
                    is_async=is_async,
                )
            )

        symbols.sort(key=lambda s: (s.start_line, s.qualified_name))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return FileSymbols(
            path=path,
            language=profile.name,
            symbols=tuple(symbols),
            has_error=bool(getattr(root, "has_error", False)),
            parse_ms=elapsed_ms,
        )


def _parents(node: Any):
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


# ---------------------------------------------------------------- 结构事实
# 下面这组函数只在"函数节点"上跑一次，产出的东西直接决定类图/时序图能不能
# 画出真实关系。原则：宁可少给（漏一条依赖），也不要猜（编一条不存在的继承）。


def _text_of(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _iter_nodes(node: Any, profile: LanguageProfile | None = None):
    """迭代子树（含自身）。遇到嵌套函数定义不再深入：那些调用属于内层函数。"""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        children = getattr(current, "children", None) or []
        for child in reversed(children):
            if child is not node and profile is not None and child.type in profile.function_types:
                continue
            stack.append(child)


def _split_type_list(text: str) -> list[str]:
    """把 "(Base, Mixin[T])" / "extends Base" / "implements A, B" 拆成名字列表。

    泛型参数里的逗号不能当分隔符（Map<K, V>），所以按括号深度切。
    """
    flat = " ".join(text.split())
    for prefix in ("extends ", "implements "):
        if flat.startswith(prefix):
            flat = flat[len(prefix) :]
    if flat.startswith("(") and flat.endswith(")"):
        flat = flat[1:-1]
    parts: list[str] = []
    buffer = ""
    depth = 0
    for char in flat:
        if char in "([<":
            depth += 1
        elif char in ")]>":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append(buffer)
            buffer = ""
        else:
            buffer += char
    if buffer.strip():
        parts.append(buffer)
    return [part.strip() for part in parts if part.strip()]


def _base_entries(
    class_node: Any, profile: LanguageProfile, source: bytes
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """抽基类名和"这条继承到底是 extends 还是 implements"。

    Java 的 `class A extends B implements C, D` 里，B 与 C/D 在类图上必须画成
    不同的箭头（实线三角 vs 虚线三角）。名字与角色分开返回、按位置对齐，
    是因为 Symbol 是扁平结构，不值得为两三个基类再套一层对象。
    """
    names: list[str] = []
    roles: list[str] = []
    for field, relation in profile.base_fields:
        child = class_node.child_by_field_name(field)
        if child is None:
            continue
        for name in _split_type_list(_text_of(child, source)):
            if name and name not in names:
                names.append(name)
                roles.append(relation)
    return tuple(names), tuple(roles)


def _base_names(class_node: Any, profile: LanguageProfile, source: bytes) -> tuple[str, ...]:
    # 去重但保序：class Foo(Bar, Bar) 是写错了，但也不能画两条一样的边
    return _base_entries(class_node, profile, source)[0]


def _base_roles(class_node: Any, profile: LanguageProfile, source: bytes) -> tuple[str, ...]:
    return _base_entries(class_node, profile, source)[1]


def _attribute_entry(node: Any, source: bytes) -> str:
    """从一个"类体直接子节点"里抽出 "name: Type"。不是字段声明就返回空串。"""
    if node.type == "field_declaration":  # Java
        name_node = node.child_by_field_name("declarator")
        type_node = node.child_by_field_name("type")
        if name_node is None:
            return ""
        raw_name = _text_of(name_node, source).split("=")[0].strip()
        if not raw_name:
            return ""
        return f"{raw_name}: {_text_of(type_node, source).strip()}" if type_node is not None else raw_name

    target = node
    if node.type == "expression_statement":
        if not node.children:
            return ""
        target = node.children[0]
    if target.type not in {"assignment", "annotated_assignment"}:
        return ""
    left = target.child_by_field_name("left")
    # self.x = 1 / items[0] = 1 / a, b = ... 都不是类字段声明
    if left is None or left.type != "identifier":
        return ""
    name = _text_of(left, source)
    type_node = target.child_by_field_name("type")
    if type_node is not None:
        return f"{name}: {_text_of(type_node, source).strip()}"
    right = target.child_by_field_name("right")
    if right is not None and right.type == "call":
        function = right.child_by_field_name("function")
        if function is not None:
            callee = _text_of(function, source).strip()
            # 只认大写开头的构造调用，repo = get_repo() 是工厂函数，不是字段类型
            if callee and callee[0].isupper():
                return f"{name}: {callee}"
    return name


def _class_attributes(class_node: Any, profile: LanguageProfile, source: bytes) -> tuple[str, ...]:
    body = class_node.child_by_field_name("body")
    if body is None:
        return ()
    found: list[str] = []
    for child in getattr(body, "children", []) or []:
        entry = _attribute_entry(child, source)
        if entry and entry not in found:
            found.append(entry)
        if len(found) >= _MAX_ATTRIBUTES:
            break
    return tuple(found)


def _instance_attributes(body: str) -> list[str]:
    """从 self.x = ... 里捞实例字段。

    类体里通常只放*类级*字段，真正的状态（self.repo）写在 __init__ 里。
    只给类体字段的话，组合关系（A *-- B）基本抽不出来，所以这里补一层启发式：
    有注解的优先，其次是构造调用（大写开头），最后只留名字。
    """
    found: dict[str, str] = {}
    for name, type_name in _INSTANCE_ATTR_ANNOTATED.findall(body):
        found.setdefault(name, f"{name}: {type_name}")
    for name, type_name in _INSTANCE_ATTR_CTOR.findall(body):
        found.setdefault(name, f"{name}: {type_name}")
    for name in _INSTANCE_ATTR_PLAIN.findall(body):
        found.setdefault(name, name)
    return list(found.values())


def _calls(node: Any, profile: LanguageProfile, source: bytes) -> tuple[str, ...]:
    if not profile.call_types:
        return ()
    found: list[str] = []
    for child in _iter_nodes(node, profile):
        if child.type not in profile.call_types:
            continue
        name_node = (
            child.child_by_field_name(profile.call_name_field)
            if profile.call_name_field
            else None
        )
        if name_node is None:
            continue
        target = _text_of(name_node, source).strip()
        if profile.call_receiver_field:
            receiver = child.child_by_field_name(profile.call_receiver_field)
            if receiver is not None:
                receiver_text = _text_of(receiver, source).strip()
                if receiver_text.startswith("this."):
                    receiver_text = receiver_text[len("this.") :]
                if receiver_text:
                    target = f"{receiver_text}.{target}"
        for prefix in ("self.", "cls."):
            if target.startswith(prefix):
                target = target[len(prefix) :]
        target = " ".join(target.split())
        if target and target not in found:
            found.append(target)
        if len(found) >= _MAX_CALLS:
            break
    return tuple(found)


def _branches(node: Any, profile: LanguageProfile) -> tuple[str, ...]:
    found: list[str] = []
    for child in _iter_nodes(node, profile):
        keyword = _BRANCH_KEYWORDS.get(child.type)
        if keyword and keyword not in found:
            found.append(keyword)
        if len(found) >= _MAX_BRANCHES:
            break
    return tuple(found)
