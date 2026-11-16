"""提示词：如何让大模型稳定吐出一段能渲染的 PlantUML。

这里记录的是**四代提示词的演化**（论文里"Prompt 工程"那一节的原素材）：

    v1  "请根据以下代码生成 PlantUML 类图"
        -> 模型输出了 200 字解释 + markdown 围栏 + 图，程序拿不到纯代码。

    v2  追加"只输出代码，不要解释" + "如果你明白了，请只回复 OK，然后等待我发代码"
        -> 两轮握手确实压掉了一部分开场白，但代价是每次生成多一次 API 调用
           （延迟 + 成本翻倍），而且模型仍然会偶尔在图后面补一句"以上是类图"。
           关键认识：**"请只输出"是请求，不是保证**。语言模型没有输出约束能力，
           能保证格式的只有生成之后的机器校验。

    v3（当前）四层约束，把"请求"降级为"契约 + 兜底"：
        1. 输出契约：第一个字符必须是 @，最后一行必须是 @enduml，禁止围栏和任何解释
        2. 结构化输入 + 一份随任务变化的示例（few-shot）
        3. 负面清单：只许使用给定实体；不确定就写 note，不许编类
        4. 生成后由 PlantUMLValidator 机器校验，不合格就带着诊断自我纠错

    v4（当前）把"结构事实"变成一等输入。v3 的图语法全对，但内容很空：
        类图里一排互不相连的类，时序图里一句泛泛的 "process"。原因是模型只看到
        函数签名，看不到"谁继承了谁、谁持有谁、入口调用了谁、哪里写了 if/for"。
        v4 做了两件事，缺一不可：
        1. `app/parsers/structure.py` 从 AST 抽出关系/调用链/分支，渲染成
           "## code structure" 文本块塞进 user 消息，并在规则里声明
           "relations you MUST draw"（标注成 ground truth，不许漏）；
        2. 校验器新增"孤立类/缺关系边/无返回箭头/缺 alt-loop"等规则，漏画就
           带诊断重试。**只改提示词不改校验**的后果是模型可以继续画空图而无人发现。

    v2 的握手没有扔掉，而是保留成可选项 warmup=True（见 build_warmup），
    因为它确实能减少一轮浪费；但不作为正确性的依赖。
"""

from __future__ import annotations

import json
from typing import Iterable

# ---------------------------------------------------------------- 输出契约

_OUTPUT_CONTRACT = """## Output contract (violating any item makes the answer unusable)

1. Your entire reply is ONE PlantUML document.
2. The first character of your reply MUST be "@" (it starts with @startuml).
3. The last line of your reply MUST be exactly @enduml.
4. Do NOT wrap the code in markdown fences.
5. Do NOT write any sentence before @startuml or after @enduml.
   No greetings, no "Here is the diagram", no explanation, no bullet list.
6. Do not use Chinese full-width punctuation as PlantUML syntax.
7. If the input is insufficient, still reply with a diagram and put the doubt
   inside a note block. Never reply with prose instead of a diagram."""

_ENTITY_RULES = """## What you may draw

- Draw ONLY classes, interfaces and methods that appear in the input below.
- Never invent a class, field, method or parameter that is not in the input.
  Missing information is a note, not an invention.
- If a method's parameter type is one of the given classes, that is a real
  dependency and may be drawn as an arrow.
- Relationships you infer (not visible in the code) must be marked with a
  stereotype or a note so a reader can tell them apart from extracted facts.
- Every changed function listed under "changed_functions" MUST appear as a
  member of its class, including its parameter list."""

_RELATION_RULES = """## Relations are not optional

The input contains a section "## code structure" produced by static analysis of
the real source code. Its "relations you MUST draw" list is ground truth:

- Every listed relation MUST appear in the diagram, with the arrow that matches
  its kind: extends --|>, implements ..|>, composition *--, aggregation o--,
  dependency ..>.
- Keep the label in Simplified Chinese (继承 / 实现 / 组合 / 聚合 / 依赖).
- NEVER draw a diagram made of isolated class boxes. If two or more classes are
  given and the input lists no relation between them, add the relations the code
  actually implies: a field whose type is another given class is a composition;
  a parameter or return type is a dependency; a base class is an inheritance.
- Do not invent classes merely to connect existing ones. An edge must come from
  the input, never from imagination.
- Do not draw more than one arrow between the same pair of classes: pick the
  strongest true relation (继承 > 实现 > 组合 > 聚合 > 依赖).
- Types listed under "types referenced but declared in another module" are real
  and appear in the input. Declare them as empty class boxes and draw the arrow
  to them; do not drop a composition just because the class lives elsewhere."""

_LANGUAGE_RULE = """## Language

- Every human-readable sentence in your reply MUST be written in Simplified
  Chinese (简体中文). This includes titles, notes, relationship labels,
  descriptions, table headers and error explanations.
- Keep code identifiers, class/method/parameter names, file paths, type names
  and PlantUML keywords exactly as they appear in the input. Never translate
  code or invent a Chinese alias for an identifier.
- Do not write English prose when a Chinese sentence can carry the meaning."""

_PLANTUML_SUBSET = """## PlantUML dialect you must stay inside

Declarations:
    class Name
    class Name {
        +field: Type
        +method(arg: Type): ReturnType
    }
    interface Name
    enum Name
    abstract class Name

Relationships (use only these arrows):
    A --> B : uses
    A ..> B : depends on
    A --|> B : extends
    A ..|> B : implements
    A *-- B : composition
    A o-- B : aggregation

Blocks: class {{ }} , package {{ }} , note ... end note, legend ... end legend

Hard rules:
- A class name containing generics or spaces MUST be quoted: class "Repo<T>" as Repo
- A member line lives INSIDE a class block and starts with + - # ~
- A relationship line lives OUTSIDE any block
- Every { needs a matching }, every note needs an end note
- Method names never contain braces; parameter lists are comma separated
- Do not use ".." as a separator between declarations; use it only in arrows"""

_SELF_CHECK = """## Before you answer, silently check:

- first line is @startuml, last line is @enduml
- every { is closed, every note has end note
- every changed function appears exactly once
- every relation listed under "relations you MUST draw" appears with the right arrow
- no class is left floating with zero arrows (unless it is the only class)
- no class name outside the input list
- no text outside the @startuml/@enduml block"""

_FREE_FUNCTION_RULE = """## Module-level functions

Functions that are not inside a class are still part of the picture, but they are
NOT a class:

- Never invent a class named after the module or the file (for example
  `class "src.api.routes" as Routes`) just to hang free functions on it. That box
  does not exist in the source and it swallows the whole diagram.
- Draw a free function as its own box, with the stereotype that says so:
      class handle_request <<function>> {
          +handle_request(request: Request): Response
      }
- Only the functions that actually changed must appear. Leave the rest of the
  module's free functions out unless they are needed to make a relation visible.
- A free function connects to classes with `..>` (依赖) when it takes one as a
  parameter or returns one."""

_SEQUENCE_SUBSET = """## PlantUML sequence dialect you must stay inside

    participant Name
    actor Name
    Name -> Name : message
    Name --> Name : return value
    activate Name
    deactivate Name
    alt condition
    else other case
    end
    opt condition
    end
    loop every item
    end
    group 异常处理
    end

Hard rules:
- every message line must contain ":" before the label
- only -> --> ->> -->> are valid arrows
- do not declare participants you do not use
- no participant named "process", "system", "调用方服务" or similar placeholders:
  participants must be the real class names from the input (an external caller may
  be named 调用方 only when the input gives no caller type)
- a note explains ONE missing fact; never stack three notes to explain the diagram"""

_SEQUENCE_SELF_CHECK = """## Before you answer, silently check:

- the first message comes from the outside into the entry point named in the task
- every call edge listed under "call chains" appears as a message
- every forward call -> has a matching return -->
- every if/for/try in the control flow list has an alt/loop/group block
- participants are real class names; no placeholder participant
- first line is @startuml, last line is @enduml, nothing outside"""


FEW_SHOT_INPUT = """module: app/services/user.py
changed_functions:
- MODIFIED UserService.load(user_id: int) -> dict  (lines 10-12)
classes:
- BaseService (methods: run)
- UserReader (interface)
- UserRepository (methods: find_by_id, save)
- UserService (bases: BaseService, methods: __init__, load)
relations:
- UserRepository ..|> UserReader : 实现  (源码里的基类)
- UserService --|> BaseService : 继承  (源码里的基类)
- UserService *-- UserRepository : 组合  (字段 repo)
"""

FEW_SHOT_OUTPUT = """@startuml
title UserService 类图（模块 app/services/user.py）

abstract class BaseService {
  +run(payload: dict): dict
}

interface UserReader {
  +find_by_id(user_id: int): User
}

class UserRepository {
  +find_by_id(user_id: int): User
  +save(user: User): None
}

class UserService {
  -repo: UserRepository
  +__init__(repo: UserRepository)
  +load(user_id: int): dict
}

UserRepository ..|> UserReader : 实现
UserService --|> BaseService : 继承
UserService *-- UserRepository : 组合
note right of UserService
  load 委托 repo.find_by_id 查询用户；
  缓存逻辑未在本次改动中出现。
end note
@enduml"""

SEQUENCE_FEW_SHOT_INPUT = """module: app/services/user.py
Entry point to model: UserService.load
call chains:
- from UserService.load: UserService.load -> UserRepository.find_by_id
control flow:
- UserService.load: if
"""

SEQUENCE_FEW_SHOT_OUTPUT = """@startuml
title UserService.load 时序图（模块 app/services/user.py）

actor 调用方
participant UserService as svc
participant UserRepository as repo

调用方 -> svc : load(user_id)
activate svc
svc -> repo : find_by_id(user_id)
activate repo
repo --> svc : user 或 None
deactivate repo
alt user 存在
  svc --> 调用方 : {"id": user_id}
else 用户不存在
  svc --> 调用方 : 抛出 LookupError
end
deactivate svc
@enduml"""


CLASS_SYSTEM_PROMPT = f"""You are a UML generation engine embedded in a CI pipeline.
You receive the functions that changed in one commit and you return ONE PlantUML
class diagram of the affected module. Your output is parsed and rendered by a
machine: any prose, fence or syntax slip breaks the pipeline.

{_OUTPUT_CONTRACT}

{_ENTITY_RULES}

{_RELATION_RULES}

{_LANGUAGE_RULE}

{_PLANTUML_SUBSET}

{_FREE_FUNCTION_RULE}

{_SELF_CHECK}

## Example

Input:
{FEW_SHOT_INPUT}
Output:
{FEW_SHOT_OUTPUT}
"""

SEQUENCE_SYSTEM_PROMPT = f"""You are a UML generation engine embedded in a CI pipeline.
You receive changed functions and you return ONE PlantUML sequence diagram that
traces the REAL call chain of the given entry point. Output is machine-parsed.

{_OUTPUT_CONTRACT}

{_ENTITY_RULES}

{_LANGUAGE_RULE}

{_SEQUENCE_SUBSET}

## How to build the diagram (follow this order)

1. Start from the entry point named in the task. An outside actor (调用方) sends
   the first message into it: `调用方 -> 入口 : 方法名(参数)`.
2. Walk the "call chains" listed in "## code structure" breadth first. Every
   `caller -> callee` edge is one message from the caller's participant to the
   callee's participant, labelled with the real method call and its arguments.
   Spend the budget on depth two: entry -> service -> repository must all be
   there. Deeper edges are optional; draw the ones that carry the story and
   stop, do not pad the diagram with every leaf call.
3. After a call that produces a value, draw the return: `被调用方 --> 调用方 : 返回值`.
   Calls that return nothing may be answered with `--> : 无返回值`.
4. Wrap control flow in blocks taken from the "control flow" list:
   if/elif -> alt ... else ... end, for/while -> loop ... end,
   try/except/finally -> group ... end (or alt when two outcomes matter).
5. Only after the chain is drawn, add at most one note for genuinely missing
   information. Never replace the chain with a generic "process" message.

A diagram whose only message is a generic call such as `A -> B : process` is a
failure, even though it is syntactically valid PlantUML.

{_SEQUENCE_SELF_CHECK}

## Example

Input:
{SEQUENCE_FEW_SHOT_INPUT}
Output:
{SEQUENCE_FEW_SHOT_OUTPUT}
"""

API_SYSTEM_PROMPT = f"""You are a technical writer embedded in a CI pipeline.
You receive changed functions and return ONE Markdown API reference section.
Start directly with a level-2 heading; no preamble, no closing remarks.

{_LANGUAGE_RULE}

Structure:
## <module name>

### <function qualified name>

- 签名：`<signature>`
- 说明：用简体中文写 1-2 句，必须能在给出的代码里找到依据。
- 参数表：表头固定为 `| 参数 | 类型 | 必填 | 说明 |`。
- 返回值：说明返回类型与含义；没有返回值就写“无返回值”。
- 异常：只列代码里真实出现的异常；没有就写“无”。

Rules:
- Write exactly one "### <function qualified name>" section for EVERY changed function given to you. Do not skip any.
- Never invent endpoints, parameters or functions that are not in the input.
- If a detail is not visible in the code, write "代码中未体现" instead of guessing.
- All descriptive prose and table headers MUST be Simplified Chinese; identifiers stay unchanged.
- Keep code examples in fenced blocks with the language tag."""


# ---------------------------------------------------------------- 用户消息


def build_input_block(
    change_set, *, module: str | None = None, max_chars: int = 60000, structure=None
) -> str:
    """把 ChangeSet 压缩成结构化输入（不是把源码整段倒进去）。

    只给：模块名、改动类型、函数签名、函数体（按预算截断）、以及实体清单。
    这样做的原因是模型对"结构化清单"的遵循度明显高于大段自由文本。

    `structure` 是 `app/parsers/structure.ModuleStructure`：类关系、调用链、
    控制流。它放在源码文本**之前**——模型对靠前的内容更敏感，而且这些事实
    正是类图/时序图的主体，源码只用来补细节。
    """
    from ..parsers.service import build_prompt_payload  # 避免循环导入

    payload = build_prompt_payload(change_set, max_chars=max_chars)
    entities = sorted(entity_names(change_set))
    header = [f"module: {module}" if module else "module: (all changed files)"]
    if entities:
        header.append("known entities: " + ", ".join(entities))
    facts = structure.render() if structure is not None else ""
    parts = ["\n".join(header)]
    if facts:
        parts.append(facts.rstrip())
    parts.append(payload)
    return "\n\n".join(parts)


def entity_names(change_set) -> set[str]:
    """从 ChangeSet 里抽出"允许出现的实体名"（用于幻觉检查）。

    规则：函数所属的类（qualified_name 去掉最后一段），加上被改动函数本身。
    没有类的顶层函数用模块名（路径最后一段去掉扩展名）当所有者。
    """
    names: set[str] = set()
    for change in change_set.all_changes():
        symbol = change.new or change.old
        if symbol is None:
            continue
        parts = symbol.qualified_name.split(".")
        if len(parts) > 1:
            names.add(".".join(parts[:-1]))
            names.add(parts[-2])
        else:
            stem = symbol.key.rsplit("/", 1)[-1]
            names.add(stem)
    return names


def changed_members(change_set) -> set[str]:
    """必须出现在图里的 "类名.方法名"。"""
    members: set[str] = set()
    for change in change_set.all_changes():
        if not change.needs_redoc:
            continue
        if change.kind.value == "removed":
            continue
        symbol = change.new or change.old
        if symbol is None:
            continue
        parts = symbol.qualified_name.split(".")
        if len(parts) > 1:
            members.add(f"{parts[-2]}.{symbol.name}")
        else:
            # 顶层函数没有类名。旧实现硬拼出 "has_read.has_read"，
            # 校验器于是永远找不到它，真实仓库测试里直接多出一条 W005。
            members.add(symbol.name)
    return members


def build_class_prompt(
    change_set, *, module: str | None = None, max_chars: int = 60000, structure=None
) -> str:
    return (
        build_input_block(change_set, module=module, max_chars=max_chars, structure=structure)
        + "\n\nTask: draw the class diagram of the module above. "
        "Draw every relation listed under \"relations you MUST draw\", each with its own arrow; "
        "a diagram of isolated class boxes is a failed answer. "
        "Use Simplified Chinese for every human-readable label, note and title; keep identifiers unchanged. "
        "Begin your reply with @startuml, end it with @enduml, and output nothing else."
    )


def build_sequence_prompt(
    change_set,
    *,
    module: str | None = None,
    entry_point: str | None = None,
    max_chars: int = 60000,
    structure=None,
) -> str:
    focus = f"\nEntry point to model: {entry_point}" if entry_point else ""
    return (
        build_input_block(change_set, module=module, max_chars=max_chars, structure=structure)
        + focus
        + "\n\nTask: draw the sequence diagram of the changed entry point. "
        "Trace the real call chain: one message per call edge, one return arrow per call, "
        "and an alt/loop/group block for every branch listed in the control flow. "
        "Use Simplified Chinese for every human-readable message, note and title; keep identifiers unchanged. "
        "Begin your reply with @startuml, end it with @enduml, and output nothing else."
    )


def build_api_prompt(change_set, *, module: str | None = None, max_chars: int = 60000) -> str:
    return (
        build_input_block(change_set, module=module, max_chars=max_chars)
        + "\n\nTask: write the Markdown API reference for the functions above. "
        "Write all prose and table headers in Simplified Chinese; keep identifiers unchanged. "
        "Start directly with a level-2 heading and output nothing else."
    )


# ---------------------------------------------------------------- 自我纠错


def build_repair_prompt(
    *,
    diagnostics: Iterable,
    previous_code: str,
    kind: str = "class",
    regenerate: bool = False,
    too_long: bool = False,
) -> str:
    """把校验器的诊断翻译成"修复指令"。

    三个刻意的设计：
    1. 给出**行号 + 该行原文**：模型不擅长数行，但很擅长"这一行错了"。
    2. 明确说"只改这些，其余保持原样"：否则每次修复都会重写整张图，
       引入新的错误（在最坏情况下会在修复循环里振荡）。
    3. 把原始错误放在最后一条 user 消息里：离生成位置最近，遵循度最高。

    ★ 2026-11-06 修复：这里原本无论 kind 是什么都写 "PlantUML output" 并追加
    _OUTPUT_CONTRACT（要求以 @startuml 开头）。真实模型评测时 Markdown 文档
    一旦进入修复轮，就会被要求"输出 PlantUML 图"，于是回一段 @startuml，
    校验器判 M001（不是标题开头），三轮全废。api 文档必须走 Markdown 契约。

    ★ regenerate=True 是给 api 文档的"空壳"失败准备的。M003（空文档）与
    M005（标题下没有正文）说明问题不是某一行的语法，而是模型整篇没写内容；
    此时再说"只改列出的问题"会让它补一句话交差，下一轮继续 M005。这里明确
    要求从头重写，并给出正文的最低结构，把修复轮变成一次带约束的重新生成。
    """
    rendered = "\n".join(d.render() for d in diagnostics)
    if kind == "api":
        intro = (
            "Your previous Markdown document failed machine validation. "
            "Write the corrected document in Simplified Chinese (简体中文); keep identifiers unchanged."
        )
        target = "Markdown document"
        contract = (
            "Begin your reply with a level-2 Markdown heading such as ## <module>, "
            "keep every valid section from the previous version, and output nothing else: "
            "no preamble, no apology, no closing remarks, no code fence around the whole document."
        )
    else:
        intro = (
            "Your previous PlantUML output failed machine validation. "
            "Write every human-readable label, note and title in Simplified Chinese (简体中文); "
            "keep identifiers unchanged."
        )
        target = kind + " diagram"
        contract = _OUTPUT_CONTRACT

    if regenerate and kind == "api":
        return f"""{intro}

Validation errors (each item is: code, line number, offending line, how to fix):
{rendered}

Previous output:
{previous_code}

The previous answer was an empty shell: a heading with no usable body. Do NOT patch it
with one extra sentence. Rewrite the complete {target} from scratch, using the changed
functions in the original task:
- start with a level-2 Markdown heading (for example ## <module>);
- for each changed function, write one short paragraph plus a table of parameters,
  return value and raised errors;
- include at least one fenced code block showing a realistic call;
- write real prose; never leave placeholders such as TODO or <your-code>.

Return only the complete Markdown document, written in Simplified Chinese.
{contract}"""

    if regenerate and kind == "sequence":
        budget = ""
        if too_long:
            budget = (
                "\nThe previous answer was also cut off by the token limit, so the diagram was\n"
                "too big. Draw a smaller one this time:\n"
                "- at most 5 participants, at most 14 messages in total;\n"
                "- keep entry -> service -> repository, drop leaf calls and deep optional edges;\n"
                "- collapse repeated branches into one loop block instead of one message per iteration.\n"
            )
        return f"""{intro}

Validation errors (each item is: code, line number, offending line, how to fix):
{rendered}

Previous output:
{previous_code}

The previous answer was too shallow: it did not trace the call chain. Rewrite the
whole {target} from scratch instead of patching it:
- read the "call chains" and "control flow" sections of the original input again;
- emit one message per call edge, one return arrow per call;
- wrap every if/for/try in alt/loop/group blocks;
- keep participants to the real class names of the input.
{budget}
Return only the complete diagram, written in Simplified Chinese.
{contract}"""

    return f"""{intro}

Validation errors (each item is: code, line number, offending line, how to fix):
{rendered}

Previous output:
{previous_code}

Fix ONLY the problems listed above and keep everything else unchanged.
Return the complete corrected {target}, written in Simplified Chinese.
{contract}"""


# ---------------------------------------------------------------- 握手（v2 存档）


WARMUP_USER = "如果你明白了，请只回复 OK，然后等待我发代码。"
WARMUP_REPLY = "OK"


def build_warmup(system_prompt: str) -> list[dict]:
    """两轮握手的消息序列：先确认规则，再发代码。

    实测结论（写进 docs/04）：握手能压低开场白，但**不能替代校验**，
    因为模型仍可能在图后追加一句话。保留它只是省一次重试，不是正确性保证。
    """
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": WARMUP_USER},
    ]


def dumps_input(change_set, **kwargs) -> str:
    """调试用：把输入块格式化出来，便于人工检查喂了什么。"""
    return json.dumps(build_input_block(change_set, **kwargs), ensure_ascii=False, indent=2)