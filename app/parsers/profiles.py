"""语言配置：把"语言差异"全部收敛到这里。

新增一门语言 = 加一个 LanguageProfile，提取器与比对器一行都不用改。

Query 用的是 tree-sitter 的 S-expression 语法（.scm），例如：
    (function_definition) @fn
含义是"匹配任意 function_definition 节点，并打上 fn 这个捕获名"。
只写节点类型、不写字段约束，是因为我们随后还要沿 parent 链找外层 class，
用 Query 做"全量抓取"、用 Python 做"求上下文"，比在 Query 里写嵌套模式更好维护。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LanguageProfile:
    name: str
    extensions: frozenset[str]
    query: str
    function_types: frozenset[str]
    class_types: frozenset[str]
    comment_types: frozenset[str]
    # 有些语言（Python）把 def 包在 decorated_definition 里，装饰器属于签名的一部分
    decorator_wrapper: str | None = None
    decorator_node: str | None = None
    # 语言是否支持重载。Java 支持，所以函数身份必须带参数类型（add(int,int) != add(long,long)）；
    # Python 不支持重载，同名即同一个函数，改参数属于"修改"而不是"删除+新增"。
    overloads: bool = False

    # ---- 结构事实（类图/时序图要画真实关系，必须从 AST 里拿，不能让模型猜）
    # 类节点上"基类列表"的字段名。Python 是 superclasses（argument_list），
    # Java 是 superclass（extends）+ interfaces（implements），两者语义不同，
    # 所以用 (字段名, 关系种类) 的二元组表达。
    base_fields: tuple[tuple[str, str], ...] = ()
    # 类体里"字段声明"的节点类型（Python 的赋值/带注解赋值、Java 的 field_declaration）
    attribute_node_types: frozenset[str] = frozenset()
    # 调用点的节点类型，以及取"被调用者名字"的字段（Python: call.function，
    # Java: method_invocation.name + object）
    call_types: frozenset[str] = frozenset()
    call_name_field: str = "function"
    call_receiver_field: str = ""
    # 返回类型所在的字段名（Python: return_type，Java: type）
    return_field: str = "return_type"


PYTHON_PROFILE = LanguageProfile(
    name="python",
    extensions=frozenset({".py", ".pyi"}),
    query="""
    (function_definition) @fn
    (class_definition) @class
    """,
    function_types=frozenset({"function_definition"}),
    class_types=frozenset({"class_definition"}),
    # Python 的注释不是 AST 节点，而是 comment 节点（tree-sitter 里是具名叶子）
    comment_types=frozenset({"comment"}),
    decorator_wrapper="decorated_definition",
    decorator_node="decorator",
    base_fields=(("superclasses", "extends"),),
    attribute_node_types=frozenset(
        {"expression_statement", "assignment", "annotated_assignment"}
    ),
    call_types=frozenset({"call"}),
    call_name_field="function",
)

JAVA_PROFILE = LanguageProfile(
    name="java",
    extensions=frozenset({".java"}),
    query="""
    (method_declaration) @fn
    (constructor_declaration) @fn
    (class_declaration) @class
    (interface_declaration) @class
    (enum_declaration) @class
    (record_declaration) @class
    """,
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    class_types=frozenset(
        {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}
    ),
    comment_types=frozenset({"line_comment", "block_comment"}),
    decorator_wrapper=None,
    # Java 的注解是修饰符列表的一部分，不是独立包装节点
    decorator_node="annotation",
    overloads=True,
    base_fields=(("superclass", "extends"), ("interfaces", "implements")),
    attribute_node_types=frozenset({"field_declaration"}),
    call_types=frozenset({"method_invocation"}),
    call_name_field="name",
    call_receiver_field="object",
    return_field="type",
)

PROFILES: tuple[LanguageProfile, ...] = (PYTHON_PROFILE, JAVA_PROFILE)

_BY_EXT: dict[str, LanguageProfile] = {
    ext: p for p in PROFILES for ext in p.extensions
}


def profile_for_path(path: str | Path) -> LanguageProfile | None:
    """按扩展名选语言。不认识的扩展名返回 None —— 调用方据此跳过该文件。"""
    return _BY_EXT.get(Path(path).suffix.lower())


def profile_by_name(name: str) -> LanguageProfile | None:
    for p in PROFILES:
        if p.name == name:
            return p
    return None


SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(_BY_EXT)
