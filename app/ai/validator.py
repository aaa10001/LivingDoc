"""PlantUML 结构校验器：不依赖 Java 也能判断"这段图有没有硬伤"。

为什么需要它（ADR-007 的延续）：
真正的语法校验要靠 plantuml.jar -checkonly，但本机没有 JRE，CI 里也不一定允许装。
如果校验依赖外网（官方 plantuml.com 服务），那对方一断网闸门就失效 —— 闸门不能有外部依赖。

所以分三层，从便宜到昂贵：
    StructuralValidator  纯 Python，永远可用，抓"90% 的低级错误"
    JavaValidator        java -jar plantuml.jar -checkonly，有 JRE 才启用
    RemoteValidator      官方 HTTP 服务，仅作可选复核，永不作为唯一判据

结构校验器抓什么（每一条都对应真实模型会犯的错）：
    E001 没有 @startuml          -> 模型直接开始写 class A
    E002 没有 @enduml            -> 输出被 max_tokens 截断（最常见！）
    E003 @enduml 之后还有内容     -> 模型在图后面补了一句"以上是类图"
    E004 图是空的                -> 模型只回了 @startuml @enduml
    E005 花括号没闭合            -> class A { ... 忘了 }
    E006 多余的 }
    E007 引号没配平              -> 中文引号/英文引号混用
    E008 类图里的关系箭头不认识   -> 模型自创箭头（A ~~> B）
    E009 时序图消息缺冒号        -> A -> B 请求数据（少了 :）
    E010 note 块没写 end note
    E011 成员行出现在类块之外
    W001 声明了输入里没有的类（幻觉）
    W002 输入里被修改的函数没出现在图里（漏了）
    W003 泛型类名没加引号        -> class Repo<T> 在 PlantUML 里是语法错误

v4 新增的"内容规则"（语法全对但图没用时才会触发）：
    E013 孤立类：图里有两个以上类，某个类一条边都没有
    E014 静态分析给出的关系没画出来（继承/实现/组合/依赖漏画）
    E015 时序图只有正向调用、没有任何返回箭头
    E016 入口调用链上的某次调用没有出现在时序图里
    E017 代码里有 if/for/try，图中却没有 alt/loop/group 块
    W008 关系画了，但箭头类型与源码事实不一致（该用继承画成了依赖）
    W009 时序图里出现了输入之外的参与者（编造的组件）
    W010 正向调用比返回箭头多（只画了一半的交互）

为什么区分 error / warning：
error 意味着这张图渲染不出来，必须自我纠错；
warning 意味着能渲染但内容不可信（幻觉、漏项），记进报告，不一定阻塞发布。

E013–E017 是 error 而不是 warning，因为它们是"能渲染但等于没画"的失败：
一张全是孤立类框的类图、一张只有一句 process 的时序图，对读者毫无价值。
如果只把它们记成 warning，自我纠错循环就永远不会去修它们 —— 这正是 v3 的教训。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class Diagnostic:
    """一条可读、可回灌给大模型的诊断。

    line 用 1-based，指向**被校验文本**里的行号（不是原始模型输出的行号）——
    回灌时我们同时给出该行原文，模型不需要自己数行。
    """

    code: str
    message: str
    line: int = 0
    hint: str = ""
    severity: str = "error"  # error | warning
    snippet: str = ""

    def render(self) -> str:
        head = f"[{self.code}] line {self.line}: {self.message}" if self.line else f"[{self.code}] {self.message}"
        if self.snippet:
            head += f"\n    offending line: {self.snippet}"
        if self.hint:
            head += f"\n    fix: {self.hint}"
        return head


@dataclass
class ValidationResult:
    ok: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)
    kind: str = "class"

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    def render(self) -> str:
        if not self.diagnostics:
            return "OK"
        return "\n".join(d.render() for d in self.diagnostics)


# ---------------------------------------------------------------- 抽取


_FENCE = re.compile(r"^[ \t]*```[ \t]*(?:plantuml|puml)?[ \t]*\n(.*?)^[ \t]*```", re.S | re.I | re.M)


@dataclass
class Extraction:
    """从模型的原始输出里抠出 PlantUML 代码。

    raw 保留原文：答辩时最有说服力的证据就是"模型第一版输出了 120 字废话"，
    所以 transcript 里要能看到它，而不是被清洗掉。
    """

    code: str
    had_fence: bool = False
    had_prose_before: bool = False
    had_prose_after: bool = False


def extract_plantuml(raw: str) -> Extraction:
    """容忍 markdown 围栏和前后废话，抠出 @startuml..@enduml 这一段。

    刻意不"修复"缺失的 @startuml：宁可让校验器报 E001 触发自我纠错，
    也不要在客户端猜模型想画什么 —— 猜错会产出看似正常、实际内容错误的图。
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    had_fence = False
    prose_before_fence = ""
    match = _FENCE.search(text)
    if match:
        had_fence = True
        prose_before_fence = text[: match.start()].strip()
        text = match.group(1)

    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip().startswith("@startuml")), None)
    if start is None:
        return Extraction(code=text.strip(), had_fence=had_fence)

    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith("@enduml"):
            end = i
            break

    before = "\n".join(lines[:start]).strip() or prose_before_fence
    if end is None:
        # 没有 @enduml（多半是被 max_tokens 截断）：保留剩余全部内容，交给校验器报 E002
        return Extraction(
            code="\n".join(lines[start:]).strip(),
            had_fence=had_fence,
            had_prose_before=bool(before),
        )

    after = "\n".join(lines[end + 1 :]).strip()
    return Extraction(
        code="\n".join(lines[start : end + 1]).strip(),
        had_fence=had_fence,
        had_prose_before=bool(before),
        had_prose_after=bool(after),
    )


# ---------------------------------------------------------------- 词法常量

# PlantUML 类图里合法的关系箭头。左侧是常见写法，右侧是正则。
_CLASS_ARROWS = (
    "--", "..", "==", "__",
    "-->", "<--", "..>", "<..", "--|>", "<|--", "..|>", "<|..",
    "*--", "--*", "o--", "--o", "#--", "--#", "+--", "--+", "^--", "--^",
    "->", "<-",
)
_SEQ_ARROWS = (
    "->", "-->", "->>", "-->>", "<-", "<--", "<<-", "<<--",
    "-[", "->x", "->o",
)
_DECL_KEYWORDS = (
    "class", "interface", "enum", "abstract", "annotation", "struct", "protocol",
    "package", "namespace", "note", "title", "caption", "legend", "skinparam",
    "hide", "show", "left to right direction", "allowmixing", "together",
    "participant", "actor", "boundary", "control", "entity", "database", "collections",
    "queue", "activate", "deactivate", "destroy", "create", "autonumber", "alt", "else",
    "end", "loop", "group", "opt", "par", "break", "critical", "ref over", "return",
    "delay", "===", "...", "---",
)


def _strip_inline_comment(line: str) -> str:
    """去掉 ' 开头的行内注释。

    只在引号外切分，避免把 class "A ' B" 里的引号内容当注释切掉。
    """
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == "'" and not in_quote:
            return line[:i]
    return line


def _balanced_quotes(line: str) -> bool:
    return line.count('"') % 2 == 0


# ---------------------------------------------------------------- 图事实抽取（v4）

#: 箭头 -> 关系种类。左右方向都登记，因为 PlantUML 允许 A <|-- B 这种写法。
_ARROW_KINDS: dict[str, str] = {
    "--|>": "extends",
    "<|--": "extends",
    "..|>": "implements",
    "<|..": "implements",
    "*--": "composition",
    "--*": "composition",
    "o--": "aggregation",
    "--o": "aggregation",
    "..>": "dependency",
    "<..": "dependency",
    "-->": "uses",
    "<--": "uses",
}

_RELATION_LINE = re.compile(r'^([\w."<>\[\]]+)\s*([-.<>=*o#+^|]{2,})\s*([\w."<>\[\]]+)')

_SEQUENCE_MESSAGE = re.compile(
    r"^(?P<src>.+?)\s*(?P<arrow><<--|<<-|<--|<-|-->>|->>|-->|->|->x|->o)\s*(?P<dst>[^:]+?)\s*:\s*(?P<label>.*)$"
)

_SEQUENCE_BLOCK_KEYWORDS = ("alt", "else", "loop", "opt", "group", "par", "break", "critical")

#: 时序图里允许出现、但不在输入实体列表里的外部参与者。
_EXTERNAL_ACTORS = {"调用方", "用户", "客户端", "浏览器", "外部系统", "caller", "client", "user"}


def _short_entity(name: str) -> str:
    """统一实体名：去引号、去泛型、取最后一段、小写。"""
    cleaned = (name or "").strip().strip('"')
    cleaned = cleaned.split("<")[0]
    return cleaned.split(".")[-1].strip().lower()


def _is_return_arrow(arrow: str) -> bool:
    """这条时序消息是"返回值"还是"调用"。

    PlantUML 的约定是虚线表示返回：`-->`（同步返回）、`-->>`（异步返回）；
    `<-` 这种反向实线也算返回，因为箭头本身就指向调用方。
    `->` / `->>` 才是发起调用。
    """
    return arrow.startswith("<") or arrow.startswith("--")


@dataclass
class GraphFacts:
    """从一段已通过语法校验的 PlantUML 里抽出来的"图画了什么"。"""

    declared: list[tuple[int, str]] = field(default_factory=list)
    #: (源, 目标) -> 关系种类；同一对类只记第一次出现的
    edges: dict[tuple[str, str], str] = field(default_factory=dict)
    #: 出现在任意一条边两端的实体
    connected: set[str] = field(default_factory=set)
    participants: set[str] = field(default_factory=set)
    #: 用 participant/actor 关键字显式声明的参与者（隐式参与者不参与幻觉检查）
    declared_participants: set[str] = field(default_factory=set)
    #: 别名 -> 真实参与者名（participant UserService as svc）
    aliases: dict[str, str] = field(default_factory=dict)
    messages: list[tuple[str, str, int]] = field(default_factory=list)
    #: 正向消息的 (源, 目标)，与 returns 的计数分开存：只数总数没法判断
    #: "哪一条调用没有回应"，会把无关的单向调用也算进来。
    forward_pairs: set[tuple[str, str]] = field(default_factory=set)
    #: 返回消息的 (源, 目标)，按图里的书写方向（被调用方, 调用方）
    return_pairs: set[tuple[str, str]] = field(default_factory=set)
    forward: int = 0
    returns: int = 0
    blocks: set[str] = field(default_factory=set)


def collect_facts(body: list[str], start_idx: int, kind: str) -> GraphFacts:
    """扫描图体，抽取判定"这张图有没有内容"所需的事实。

    刻意做成一次独立的轻量扫描，而不是塞进主校验循环：主循环关心的是
    "这一行语法对不对"，这里关心的是"整张图连起来了没有"，两种关注点混在
    一起会让两边都难改。
    """
    facts = GraphFacts()
    depth = 0
    in_note = False
    for offset, raw_line in enumerate(body):
        line_no = start_idx + offset + 2
        stripped = _strip_inline_comment(raw_line).strip()
        if not stripped:
            continue
        lowered = stripped.lower()

        if in_note:
            if lowered.startswith("end note") or lowered == "endnote":
                in_note = False
            continue
        if re.match(r"^(note|hnote|rnote)\b", lowered):
            if ":" not in stripped:
                in_note = True
            continue
        if lowered.startswith(("legend", "title", "caption", "header", "footer", "skinparam", "hide", "show")):
            continue
        if lowered.startswith("}"):
            depth = max(0, depth - 1)
            continue

        if kind == "sequence":
            # 按"第一个单词"匹配，不能用 startswith(元组)：`participant` 也以
            # `par` 开头，会把参与者声明整行吃掉，别名与幻觉检查跟着一起失效。
            if lowered.split()[0] in _SEQUENCE_BLOCK_KEYWORDS:
                facts.blocks.add(lowered.split()[0])
                continue
            if lowered.startswith(("participant", "actor", "boundary", "control", "entity", "database", "collections", "queue")):
                alias = re.search(r"\bas\s+(\w+)\s*$", stripped, re.I)
                head = stripped.split()[1] if len(stripped.split()) > 1 else ""
                name = _short_entity(head or stripped.split(maxsplit=1)[-1])
                if name:
                    facts.participants.add(name)
                    facts.declared_participants.add(name)
                    if alias:
                        facts.aliases[alias.group(1).lower()] = name
                continue
            message = _SEQUENCE_MESSAGE.match(stripped)
            if message:
                source = _short_entity(message.group("src"))
                target = _short_entity(message.group("dst"))
                source = facts.aliases.get(source, source)
                target = facts.aliases.get(target, target)
                arrow = message.group("arrow")
                facts.messages.append((source, target, line_no))
                facts.participants.update({source, target})
                if _is_return_arrow(arrow):
                    facts.returns += 1
                    facts.return_pairs.add((source, target))
                else:
                    facts.forward += 1
                    facts.forward_pairs.add((source, target))
            continue

        # ---- 类图
        declaration = re.match(
            r"^(?:abstract\s+)?(class|interface|enum|annotation|struct|protocol)\s+([^\s{<]+)", stripped, re.I
        )
        if stripped.endswith("{"):
            if declaration:
                facts.declared.append((line_no, declaration.group(2).strip().strip('"')))
            depth += 1
            continue
        if depth > 0:
            continue
        if declaration:
            facts.declared.append((line_no, declaration.group(2).strip().strip('"')))
            continue
        relation = _RELATION_LINE.match(stripped)
        if relation and relation.group(2) in _ARROW_KINDS:
            source = _short_entity(relation.group(1))
            target = _short_entity(relation.group(3))
            if relation.group(2).startswith("<"):
                source, target = target, source
            facts.edges.setdefault((source, target), _ARROW_KINDS[relation.group(2)])
            facts.connected.update({source, target})
    return facts


# ---------------------------------------------------------------- 校验器


class PlantUMLValidator:
    """结构校验。kind: "class" | "sequence"。

    allowed_entities / required_members 用于"幻觉检查"：
      allowed_entities  输入里真实存在的类名（含限定名与短名）
      required_members  必须出现在图里的 "类名.方法名"

    expected_relations / expected_calls / expect_control_flow 来自
    `app/parsers/structure.py` 的静态分析结果，用于"内容检查"：
      expected_relations  {(源类, 目标类, 关系种类)}，漏画关系就是 E014
      expected_calls      {(调用者, 被调用者)}，调用链断了就是 E016
      advisory_calls      深于两跳的调用边：漏了只算 W011，不毙稿
      expect_control_flow 代码里有 if/for/try，却没有 alt/loop/group 就是 E017
    三者默认都是空，因此老调用方（只做语法校验）的行为完全不变。

    为什么深度要分层：入口往下三跳以后的边（缓存命中后的 invalidate 之类）
    在一张 30 行的时序图里很难同时画清楚，硬要求只会逼模型堆消息、
    或者直接拒稿。命脉是前两跳——入口到服务、服务到仓储。
    """

    def __init__(
        self,
        *,
        allowed_entities: set[str] | None = None,
        required_members: set[str] | None = None,
        expected_relations: set[tuple[str, str, str]] | None = None,
        expected_calls: set[tuple[str, str]] | None = None,
        advisory_calls: set[tuple[str, str]] | None = None,
        expect_control_flow: bool = False,
    ) -> None:
        self.allowed = {e.lower() for e in (allowed_entities or set())}
        self.required = {m.lower() for m in (required_members or set())}
        self.expected_relations = {
            (_short_entity(source), _short_entity(target), kind)
            for source, target, kind in (expected_relations or set())
        }
        self.expected_calls = {
            (_short_entity(caller), _short_entity(callee)) for caller, callee in (expected_calls or set())
        }
        self.advisory_calls = {
            (_short_entity(caller), _short_entity(callee))
            for caller, callee in (advisory_calls or set())
        } - self.expected_calls
        self.expect_control_flow = expect_control_flow

    def validate(self, code: str, kind: str = "class") -> ValidationResult:
        diagnostics: list[Diagnostic] = []
        code = code.replace("\r\n", "\n").replace("\r", "\n")
        lines = code.split("\n")

        # ---- 整体结构：起止标记
        start_idx = next((i for i, line in enumerate(lines) if line.strip().startswith("@startuml")), None)
        end_idx = next(
            (i for i in range(len(lines) - 1, -1, -1) if lines[i].strip().startswith("@enduml")),
            None,
        )
        if start_idx is None:
            diagnostics.append(
                Diagnostic(
                    "E001",
                    "missing @startuml",
                    line=0,
                    hint="the output must begin with @startuml on its own line",
                )
            )
            return ValidationResult(ok=False, diagnostics=diagnostics, kind=kind)
        if end_idx is None or end_idx < start_idx:
            diagnostics.append(
                Diagnostic(
                    "E002",
                    "missing @enduml (output is probably truncated or the model did not finish)",
                    line=len(lines),
                    snippet=lines[-1][:120] if lines else "",
                    hint="close the diagram with @enduml; keep the diagram shorter if it does not fit",
                )
            )
            return ValidationResult(ok=False, diagnostics=diagnostics, kind=kind)

        body = lines[start_idx + 1 : end_idx]
        if face := [line for line in lines[end_idx + 1 :] if line.strip()]:
            diagnostics.append(
                Diagnostic(
                    "E003",
                    "content after @enduml (explanatory prose is not allowed)",
                    line=end_idx + 2,
                    snippet=face[0][:120],
                    hint="remove everything after @enduml",
                )
            )
        if not [line for line in body if line.strip() and not line.strip().startswith("'")]:
            diagnostics.append(
                Diagnostic("E004", "the diagram is empty", line=start_idx + 2, hint="draw at least one declaration")
            )
            return ValidationResult(ok=False, diagnostics=diagnostics, kind=kind)

        # ---- 逐行：块配对、引号、箭头、note 块
        brace_depth = 0
        open_stack: list[tuple[int, str]] = []
        note_kind: str | None = None
        declared: dict[str, tuple[int, str]] = {}
        members_seen: set[str] = set()

        for offset, raw_line in enumerate(body):
            line_no = start_idx + offset + 2  # 1-based，指向 code 里的真实行号
            line = _strip_inline_comment(raw_line).rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            if not _balanced_quotes(line):
                diagnostics.append(
                    Diagnostic(
                        "E007",
                        "unbalanced double quotes on this line",
                        line=line_no,
                        snippet=stripped[:120],
                        hint='Chinese quotes are not delimiters; escape or remove the stray " character',
                    )
                )

            # note ... end note 是跨行块，单独处理
            if note_kind is not None:
                if stripped.startswith("end note") or stripped == "endnote":
                    note_kind = None
                continue
            if re.match(r"^(note|hnote|rnote|legend|header|footer)\b", stripped):
                if ":" not in stripped and not stripped.startswith(("legend", "header", "footer")):
                    note_kind = "note"
                continue

            if stripped.startswith("}"):
                if brace_depth == 0:
                    diagnostics.append(
                        Diagnostic(
                            "E006",
                            "unexpected closing brace",
                            line=line_no,
                            snippet=stripped[:120],
                            hint="remove the extra } or add the matching { on its class/package line",
                        )
                    )
                else:
                    brace_depth -= 1
                    open_stack.pop()
                continue
            if stripped.endswith("{"):
                opening = stripped[:-1].strip()
                brace_depth += 1
                open_stack.append((line_no, opening or "block"))
                if opening and not any(opening.lower().startswith(k) for k in _DECL_KEYWORDS):
                    diagnostics.append(
                        Diagnostic(
                            "W004",
                            "block does not start with a known declaration keyword",
                            line=line_no,
                            snippet=stripped[:120],
                            severity="warning",
                            hint="did you mean class/interface/enum/package?",
                        )
                    )
                continue

            if brace_depth > 0:
                # 类块里的成员行
                if stripped.startswith(("+", "-", "#", "~")):
                    member = re.sub(r"^[+\-#~]\s*", "", stripped)
                    if "(" in member:
                        name = member.split("(")[0].strip()
                        owner = open_stack[-1][1].split()[-1].strip('"') if open_stack else ""
                        if owner:
                            members_seen.add(f"{owner.split('<')[0]}.{name}".lower())
                    if stripped.count("(") != stripped.count(")"):
                        diagnostics.append(
                            Diagnostic(
                                "E012",
                                "unbalanced parentheses in method declaration",
                                line=line_no,
                                snippet=stripped[:120],
                                hint="every ( needs a )",
                            )
                        )
                continue

            # 类块之外的行
            if stripped.startswith(("+", "-", "#", "~")) and "(" in stripped:
                diagnostics.append(
                    Diagnostic(
                        "E011",
                        "member line outside of any class block",
                        line=line_no,
                        snippet=stripped[:120],
                        hint="this line belongs inside class Name { ... }",
                    )
                )

            declaration = re.match(
                r"^(?:abstract\s+)?(class|interface|enum|annotation|struct|protocol)\s+([^\s{<]+|<[^>]+>)",
                stripped,
                re.I,
            )
            if declaration:
                name = declaration.group(2).strip()
                declared.setdefault(name.lower(), (line_no, name))
                if name.startswith("<"):
                    diagnostics.append(
                        Diagnostic(
                            "W003",
                            "generic class name must be quoted",
                            line=line_no,
                            snippet=stripped[:120],
                            severity="warning",
                            hint='write class "Repo<T>" as Repo instead',
                        )
                    )

            if kind == "sequence":
                diagnostics.extend(self._check_sequence_line(stripped, line_no))
            else:
                diagnostics.extend(self._check_class_line(stripped, line_no))

        while open_stack:
            line_no, opening = open_stack.pop()
            diagnostics.append(
                Diagnostic(
                    "E005",
                    f"unclosed block opened on this line: {opening}",
                    line=line_no,
                    hint="add the missing } before @enduml",
                )
            )
        if note_kind is not None:
            diagnostics.append(
                Diagnostic("E010", "note block is not closed", hint="add 'end note' after the note text")
            )

        diagnostics.extend(self._check_hallucination(declared, members_seen))

        # ---- 内容检查：语法全对但图没用时才会触发（v4）
        facts = collect_facts(body, start_idx, kind)
        if kind == "sequence":
            diagnostics.extend(self._check_sequence_content(facts))
        else:
            diagnostics.extend(self._check_class_content(facts))

        ok = not any(d.severity == "error" for d in diagnostics)
        return ValidationResult(ok=ok, diagnostics=diagnostics, kind=kind)

    # ---- 内容规则（v4）

    def _check_class_content(self, facts: GraphFacts) -> list[Diagnostic]:
        """类图的内容规则：不许有孤立类、不许漏画静态分析给出的关系。"""
        from ..parsers.structure import ARROWS, RELATION_LABELS

        out: list[Diagnostic] = []
        declared = {_short_entity(name): (line_no, name) for line_no, name in facts.declared}

        if self.expected_relations and len(declared) >= 2:
            for short, (line_no, raw_name) in declared.items():
                if short in facts.connected:
                    continue
                out.append(
                    Diagnostic(
                        "E013",
                        f"class {raw_name!r} is isolated: it has no relationship to any other class",
                        line=line_no,
                        snippet=f"class {raw_name}",
                        hint=(
                            "connect it: a base class is --|>, a field typed with another class is *--, "
                            "a parameter or return type is ..>; never leave a class floating"
                        ),
                    )
                )

        for source, target, kind in sorted(self.expected_relations):
            drawn = facts.edges.get((source, target)) or facts.edges.get((target, source))
            label = RELATION_LABELS.get(kind, kind)
            if drawn is None:
                out.append(
                    Diagnostic(
                        "E014",
                        f"missing relationship {source} -> {target} ({label}) present in the source code",
                        line=0,
                        hint=f"add the line: {source} {ARROWS.get(kind, '-->')} {target} : {label}",
                    )
                )
            elif drawn != kind:
                out.append(
                    Diagnostic(
                        "W008",
                        f"relationship {source} -> {target} is drawn as {drawn}, but the source says {kind}",
                        line=0,
                        severity="warning",
                        hint=f"use the arrow for {kind}: {ARROWS.get(kind, '-->')}",
                    )
                )
        return out

    def _check_sequence_content(self, facts: GraphFacts) -> list[Diagnostic]:
        """时序图的内容规则：要有返回箭头、要覆盖调用链、要有分支块。"""
        out: list[Diagnostic] = []

        # 只按"调用链上的边"计账：`logger.warning(...)` 这种顺手的单向调用
        # 本来就没有返回值，把它算进缺返回里只会教模型去编造一个假返回。
        known_edges = self.expected_calls | self.advisory_calls
        if known_edges:
            # 按边逐条核对：只数"调用链上的边有没有被回应"。
            awaiting = sum(
                1
                for caller, callee in known_edges
                if (caller, callee) in facts.forward_pairs
                and (callee, caller) not in facts.return_pairs
            )
        else:
            awaiting = max(0, facts.forward - facts.returns)

        if facts.forward and not facts.returns:
            out.append(
                Diagnostic(
                    "E015",
                    "the diagram has forward calls but no return arrows: the interaction is only half drawn",
                    line=facts.messages[0][2] if facts.messages else 0,
                    hint="answer every call with a return: 被调用方 --> 调用方 : 返回值",
                )
            )
        elif awaiting:
            out.append(
                Diagnostic(
                    "W010",
                    f"{awaiting} forward call(s) have no matching return arrow",
                    line=0,
                    severity="warning",
                    hint="each call that produces a value should be answered with -->",
                )
            )

        if self.expected_calls:
            drawn = {(source, target) for source, target, _ in facts.messages}
            drawn |= {(target, source) for source, target, _ in facts.messages}
            for caller, callee in sorted(self.expected_calls):
                if (caller, callee) in drawn:
                    continue
                out.append(
                    Diagnostic(
                        "E016",
                        f"call edge {caller} -> {callee} from the source code is not drawn",
                        line=0,
                        hint=f"add the message: {caller} -> {callee} : 方法名(参数)",
                    )
                )

        if self.advisory_calls:
            drawn_advisory = {(source, target) for source, target, _ in facts.messages}
            drawn_advisory |= {(target, source) for source, target, _ in facts.messages}
            for caller, callee in sorted(self.advisory_calls):
                if (caller, callee) in drawn_advisory:
                    continue
                out.append(
                    Diagnostic(
                        "W011",
                        f"deep call edge {caller} -> {callee} is not drawn (allowed, but the chain stops here)",
                        line=0,
                        severity="warning",
                        hint="optional: one more message keeps the chain connected",
                    )
                )

        if self.expect_control_flow and not facts.blocks:
            out.append(
                Diagnostic(
                    "E017",
                    "the changed code contains branches, but the diagram has no alt/loop/group block",
                    line=0,
                    hint="wrap each if/for/try from the control flow list in alt/loop/group ... end",
                )
            )

        if self.allowed:
            allowed_short = {entry.split(".")[-1] for entry in self.allowed}
            # 只查显式声明的参与者：消息里的隐式参与者通常是模块外的协作方
            # （仓储接口、HTTP 客户端），把它们当"编造"会淹掉真正的幻觉。
            for name in sorted(facts.declared_participants):
                if name in _EXTERNAL_ACTORS or name in allowed_short:
                    continue
                out.append(
                    Diagnostic(
                        "W009",
                        f"participant {name!r} is declared but not present in the input changes (possible invention)",
                        line=0,
                        severity="warning",
                        hint="declare only the class names from the input; the outside caller may be named 调用方",
                    )
                )
        return out

    # ---- 行级规则

    def _check_class_line(self, stripped: str, line_no: int) -> list[Diagnostic]:
        # 只检查"看起来像关系"的行：左右两侧都有标识符、中间是一个箭头
        relation = re.match(r"^([\w.\"<>\[\]]+)\s*([^\s\w\"]+)\s*([\w.\"<>\[\]]+)", stripped)
        if relation and re.search(r"[-.=*o#<>+^|]{2,}", relation.group(2)):
            arrow = relation.group(2)
            if arrow not in _CLASS_ARROWS:
                return [
                    Diagnostic(
                        "E008",
                        f"unknown relationship arrow {arrow!r}",
                        line=line_no,
                        snippet=stripped[:120],
                        hint="use one of: --> <-- ..> <.. --|> ..|> *-- o--",
                    )
                ]
        return []

    def _check_sequence_line(self, stripped: str, line_no: int) -> list[Diagnostic]:
        lowered = stripped.lower()
        if lowered.startswith(("participant", "actor", "boundary", "control", "entity", "database", "collections", "queue", "activate", "deactivate", "autonumber", "title", "skinparam", "hide", "note", "alt", "else", "end", "loop", "group", "opt", "par", "break", "critical", "return", "delay", "==", "...", "---")):
            return []
        # 消息行必须形如  A -> B: text
        match = re.match(r"^(.+?)\s*([-<>.\[\]ox]{2,})\s*([^:]+?)\s*(:.*)?$", stripped)
        if not match:
            return []
        arrow = match.group(2)
        if not any(arrow.startswith(a) or arrow == a for a in _SEQ_ARROWS):
            return [
                Diagnostic(
                    "E008",
                    f"unknown message arrow {arrow!r}",
                    line=line_no,
                    snippet=stripped[:120],
                    hint="use -> --> ->> -->> for messages",
                )
            ]
        if ":" not in stripped:
            return [
                Diagnostic(
                    "E009",
                    "sequence message is missing ':' before the label",
                    line=line_no,
                    snippet=stripped[:120],
                    hint="write A -> B: label",
                )
            ]
        return []

    # ---- 幻觉检查

    def _check_hallucination(
        self, declared: dict[str, tuple[int, str]], members_seen: set[str]
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        if self.allowed:
            for name, (line_no, raw_name) in declared.items():
                short = re.split(r"[.<>]", name)[-1]
                if name not in self.allowed and short not in self.allowed:
                    out.append(
                        Diagnostic(
                            "W001",
                            f"class {raw_name!r} is not present in the input changes (possible hallucination)",
                            line=line_no,
                            severity="warning",
                            hint="only draw entities that appear in the provided code; if unsure, use a note",
                        )
                    )
        missing = sorted(self.required - members_seen)
        for item in missing:
            out.append(
                Diagnostic(
                    "W002",
                    f"changed function {item!r} does not appear in the diagram",
                    severity="warning",
                    hint="add it as a member of its class",
                )
            )
        return out


_DEFAULT = PlantUMLValidator()


def validate_plantuml(
    code: str,
    kind: str = "class",
    *,
    allowed_entities: set[str] | None = None,
    required_members: set[str] | None = None,
    expected_relations: set[tuple[str, str, str]] | None = None,
    expected_calls: set[tuple[str, str]] | None = None,
    advisory_calls: set[tuple[str, str]] | None = None,
    expect_control_flow: bool = False,
) -> ValidationResult:
    validator = (
        _DEFAULT
        if allowed_entities is None
        and required_members is None
        and expected_relations is None
        and expected_calls is None
        and advisory_calls is None
        and not expect_control_flow
        else PlantUMLValidator(
            allowed_entities=allowed_entities,
            required_members=required_members,
            expected_relations=expected_relations,
            expected_calls=expected_calls,
            advisory_calls=advisory_calls,
            expect_control_flow=expect_control_flow,
        )
    )
    return validator.validate(code, kind=kind)


# ---------------------------------------------------------------- Markdown 校验


_MD_FENCE = re.compile(r"^[ \t]*```.*$", re.M)
_MD_PREAMBLE = (
    "作为一个",
    "作为AI",
    "作为 AI",
    "以下是",
    "下面是",
    "好的",
    "当然",
    "here is",
    "here's",
    "sure,",
    "certainly",
    "of course",
)


def _clip(text: str, limit: int = 120) -> str:
    """诊断里带的原文片段必须单行且短：多行片段会把错误报告撑爆。"""
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")



class MarkdownValidator:
    """API 文档的"能交付"检查。

    和 PlantUML 不同，Markdown 几乎不会被渲染器拒绝，所以这里的错误不是
    "渲染不出来"，而是"交出去会被人笑"：
        M001 正文前面有寒暄/道歉 -> 说明模型没遵守契约，产物里混入了对话
        M002 代码围栏没有闭合     -> 后面整篇文档都会被当成代码块
        M003 文档是空的
        M004 文档里出现占位符     -> 模型偷懒写了 <TODO> / <...>
        W005 required_members 里的函数没被写到（api 下是 error，图和时序图是 warning）

    M001 为什么算 error 而不是 warning：它意味着模型把"对话"和"产物"混在一起，
    同一轮输出里的其它内容同样不可信。这种输出不能进知识库。
    """

    def __init__(self, required_members: set[str] | None = None) -> None:
        self.required = set(required_members or ())

    def validate(self, code: str, kind: str = "api") -> ValidationResult:
        text = code.strip()
        diagnostics: list[Diagnostic] = []
        lines = text.splitlines()

        if not lines:
            diagnostics.append(Diagnostic("M003", "document is empty", hint="emit at least one section"))
            return ValidationResult(ok=False, diagnostics=diagnostics, kind=kind)

        # M001：第一个非空行必须是标题
        first = lines[0].lstrip()
        looks_like_heading = first.startswith("#")
        lowered = first.lower()
        if not looks_like_heading:
            if any(lowered.startswith(p.lower()) for p in _MD_PREAMBLE):
                diagnostics.append(
                    Diagnostic(
                        "M001",
                        "document starts with conversational text instead of a heading",
                        line=1,
                        hint="drop the preamble; start with '## <module>'",
                        snippet=_clip(first),
                    )
                )
            else:
                diagnostics.append(
                    Diagnostic(
                        "M001",
                        "document does not start with a Markdown heading",
                        line=1,
                        hint="start with '## <module>'",
                        snippet=_clip(first),
                    )
                )

        # M002：围栏配平
        fences = _MD_FENCE.findall(text)
        if len(fences) % 2 == 1:
            diagnostics.append(
                Diagnostic(
                    "M002",
                    f"unbalanced code fences: found {len(fences)} fence lines",
                    hint="close every triple-backtick block",
                )
            )

        # M004：占位符
        for index, line in enumerate(lines, start=1):
            if re.search(r"<(TODO|TBD|\.\.\.|your[-_ ]?code)[^>]*>", line, re.I):
                diagnostics.append(
                    Diagnostic(
                        "M004",
                        "document contains a placeholder instead of content",
                        line=index,
                        hint="write the real value or 'not documented in code'",
                        snippet=_clip(line),
                    )
                )

        # M005 / W006：结构合格但"没有内容"的文档
        # 真实模型在一次运行里只回了 29 个字符（一个标题 + 一行反引号），而 M001-M004
        # 全部通过——因为它确实以标题开头、围栏配平、没有占位符。
        # 这类文档会进知识库，评审时被当成"系统坏了"。所以必须在闸门上拦住：
        # 正文（去掉标题行与围栏行）不足 40 字符算 error，不足 120 字符算 warning。
        body_lines = [
            line
            for line in lines
            if line.strip() and not line.lstrip().startswith("#") and not line.strip().startswith("```")
        ]
        body_text = "\n".join(body_lines).strip()
        if len(body_text) < 40:
            diagnostics.append(
                Diagnostic(
                    "M005",
                    f"document has no body: only {len(body_text)} characters outside headings",
                    hint="write at least one paragraph or table per changed function",
                )
            )
        elif len(body_text) < 120:
            diagnostics.append(
                Diagnostic(
                    "W006",
                    f"document body is very short ({len(body_text)} characters)",
                    severity="warning",
                    hint="expand each section with parameters, return value and errors",
                )
            )

        # W005：要求覆盖的函数是否出现。
        # API 文档按"每个变更函数一节"组织，漏掉一节就是内容缺失，必须拦住；
        # 类图/时序图允许合并或省略次要成员，仍然只算 warning。
        missing = sorted(name for name in self.required if name not in text)
        for name in missing:
            diagnostics.append(
                Diagnostic(
                    "W005",
                    f"changed function {name!r} is not documented",
                    severity="error" if kind == "api" else "warning",
                    hint="add a section for this function",
                )
            )

        return ValidationResult(
            ok=not any(d.severity == "error" for d in diagnostics),
            diagnostics=diagnostics,
            kind=kind,
        )


def extract_markdown(raw: str) -> Extraction:
    """剥掉围栏，并去掉第一个标题之前的所有对话文本。

    关键点：文档正文里本来就会有 ```python 代码块。不能看到第一个围栏就当成
    "模型把整篇文档包在围栏里"——真实 GitHub 测试里，API 文档第一个函数带示例
    代码，旧实现从那个内部围栏开始截，结果整篇文档被截掉，校验器报 M001/M005。
    只有"围栏之外没有标题"且"闭合围栏在文本末尾"时，才认为是外层包装。
    """
    text = raw.replace("\r\n", "\n").strip()
    had_fence = False

    first = re.search(r"^[ \t]*```[^\n]*$", text, re.M)
    if first and not re.search(r"^#{1,3} ", text[: first.start()], re.M):
        tail = text[first.end() :]
        closes = list(re.finditer(r"^[ \t]*```[ \t]*$", tail, re.M))
        if closes:
            last = closes[-1]
            body = tail[: last.start()].strip()
            after = tail[last.end() :].strip()
            if not after and re.search(r"^#{1,3} ", body, re.M):
                had_fence = True
                text = body

    heading = re.search(r"^#{1,3} .*$", text, re.M)
    had_prose_before = False
    if heading and heading.start() > 0:
        prefix = text[: heading.start()].strip()
        if prefix:
            had_prose_before = True
            text = text[heading.start() :].strip()

    code = text.rstrip() + "\n"
    return Extraction(
        code=code,
        had_fence=had_fence,
        had_prose_before=had_prose_before,
        had_prose_after=False,
    )


def validate_markdown(code: str, *, required_members: set[str] | None = None) -> ValidationResult:
    return MarkdownValidator(required_members=required_members).validate(code, kind="api")
