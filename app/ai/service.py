"""文档编排：把一次 ChangeSet 变成可以直接落库的文档草稿。

这一层刻意不做三件事，以保持可测：
    不发 HTTP（客户端传进来）
    不碰数据库（返回 dataclass）
    不决定发布（只告诉上层 ok / needs_review，发布策略在 tasks.py）

产出三种文档（用户定稿：文档粒度 = 模块级，target_key = module:xxx）：
    class_diagram     模块类图，一张
    sequence_diagram  模块入口函数的时序图
    api_doc           Markdown API 参考

"模块"怎么定义：取文件所在目录，把 / 换成 .，例如
    app/services/user.py -> app.services
    main.py              -> (root)
这比"每个类一张图"粗，比"整个仓库一张图"细；粒度对齐了人 review 的单位
（一个 PR 通常只动一两个模块），也避免了图里塞进整个仓库导致渲染爆炸。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from ..parsers.model import ChangeKind, ChangeSet
from ..parsers.structure import build_structure
from .client import LLMClient
from .prompts import (
    API_SYSTEM_PROMPT,
    CLASS_SYSTEM_PROMPT,
    SEQUENCE_SYSTEM_PROMPT,
    build_api_prompt,
    build_class_prompt,
    build_sequence_prompt,
    changed_members,
    entity_names,
)
from .self_correct import GenerationResult, SelfCorrectingGenerator
from .validator import Diagnostic

logger = logging.getLogger(__name__)

#: v4：类图/时序图开始消费 AST 抽出的结构事实（继承、组合、调用链、分支）。
#: 版本号跟着提示词契约走，落库时能一眼看出某份文档是哪一代提示词产出的。
PROMPT_VERSION = "v4-2026-11"


class DocType(str, Enum):
    CLASS_DIAGRAM = "class_diagram"
    SEQUENCE_DIAGRAM = "sequence_diagram"
    API_DOC = "api_doc"


# 文档类型 -> (系统提示词, 校验 kind, 文件扩展名)
_SPEC = {
    DocType.CLASS_DIAGRAM: (CLASS_SYSTEM_PROMPT, "class", "puml"),
    DocType.SEQUENCE_DIAGRAM: (SEQUENCE_SYSTEM_PROMPT, "sequence", "puml"),
    DocType.API_DOC: (API_SYSTEM_PROMPT, "api", "md"),
}


@dataclass(frozen=True)
class DocumentDraft:
    doc_type: DocType
    target_key: str
    title: str
    content: str
    ok: bool
    needs_review: bool
    diagnostics: tuple[Diagnostic, ...] = ()
    attempts: int = 0
    model: str = ""
    tokens: int = 0
    latency_ms: int = 0
    prompt_version: str = PROMPT_VERSION

    @property
    def extension(self) -> str:
        return _SPEC[self.doc_type][2]

    @property
    def publishable(self) -> bool:
        """能进知识库的最低门槛。

        注意 needs_review 不看：带 warning 的文档允许发布，但要挂"待复核"标签。
        error 则一定不能发——渲染都会失败的图发出去没有任何意义。
        """
        return self.ok

    def notes(self) -> str:
        """warning 摘要，发布时写进 PR 评论，提醒人工复核。"""
        items = [d.render() for d in self.diagnostics if d.severity != "error"]
        return "\n".join(items)

    def validation_errors(self) -> str:
        """真正导致 fail-closed 的 error 诊断。

        `notes()` 故意只返回 warning；以前 record_invocation 误用它当错误信息，
        结果日志里只剩 W005 警告，看起来像"只有 warning 却被拒"，排查时非常误导。
        """
        return "\n".join(d.render() for d in self.diagnostics if d.severity == "error")


@dataclass(frozen=True)
class ModuleDocumentation:
    module: str
    target_key: str
    drafts: tuple[DocumentDraft, ...]
    summary: dict

    @property
    def all_ok(self) -> bool:
        return all(d.ok for d in self.drafts)

    @property
    def needs_review(self) -> bool:
        return any(d.needs_review for d in self.drafts)

    def by_type(self, doc_type: DocType) -> DocumentDraft | None:
        for draft in self.drafts:
            if draft.doc_type == doc_type:
                return draft
        return None


def module_key_for(path: str) -> str:
    normalised = path.replace("\\", "/").strip("/")
    parts = normalised.split("/")
    if len(parts) <= 1:
        return "(root)"
    return ".".join(parts[:-1])


def _skipped_path(item) -> str:
    """skipped_files 目前是 (path, reason) 元组，但历史调用可能传对象；
    两种都接受，免得模块归属算错。"""
    if isinstance(item, tuple) and item and isinstance(item[0], str):
        return item[0]
    return getattr(item, "path", "")


def group_by_module(change_set: ChangeSet) -> dict[str, ChangeSet]:
    """把 ChangeSet 按模块切开。

    保留 skipped_files：某个模块里可能有解析失败的文件，告警要跟着走。
    """
    buckets: dict[str, list] = {}
    for file_diff in change_set.files:
        buckets.setdefault(module_key_for(file_diff.path), []).append(file_diff)

    grouped: dict[str, ChangeSet] = {}
    for module, files in buckets.items():
        grouped[module] = ChangeSet(
            files=tuple(files),
            skipped_files=tuple(
                s for s in change_set.skipped_files if module_key_for(_skipped_path(s)) == module
            ),
        )
    return grouped


class DocumentationService:
    """生成器 + 三种文档的编排。"""

    def __init__(self, client: LLMClient, *, max_attempts: int = 3, model_name: str = "") -> None:
        self.client = client
        self.generator = SelfCorrectingGenerator(client, max_attempts=max_attempts)
        self.model_name = model_name or getattr(client, "model", "")

    def generate_module(
        self,
        change_set: ChangeSet,
        *,
        module: str | None = None,
        context: tuple[str, ...] = (),
    ) -> ModuleDocumentation:
        if not change_set.files:
            return ModuleDocumentation(
                module=module or "(root)",
                target_key=f"module:{module or '(root)'}",
                drafts=(),
                summary={"reason": "no changed files"},
            )

        name = module or module_key_for(change_set.files[0].path)
        target_key = f"module:{name}"
        redoc = [c for c in change_set.all_changes() if c.needs_redoc]
        if not redoc:
            # 真实仓库测试踩到的坑：commit 只改了注释/换行，semantic_hash 没变，
            # build_prompt_payload 于是返回空串，但文档照样生成，模型只能拿
            # entity 名字硬编。纯格式化变更不应该触发重写文档。
            logger.info("skip module %s: no semantic changes", name)
            return ModuleDocumentation(
                module=name,
                target_key=target_key,
                drafts=(),
                summary={"reason": "no semantic changes", "module": name, "target_key": target_key},
            )

        entities = entity_names(change_set)
        members = changed_members(change_set)
        # 结构事实只算一次，提示词和校验器共用同一份：
        # 提示词负责"告诉模型该画什么"，校验器负责"确认它画了"。两边各算一份
        # 就有可能漂移（比如提示词给了 5 条关系、校验器只认 3 条）。
        structure = build_structure(change_set, module=name, context=context)
        # 跨模块引用的类型也要进白名单：否则模型画出 `UserService *-- UserRepository`
        # 反而会被判成 W001 幻觉。
        if structure.externals:
            entities = set(entities) | set(structure.externals)
        entry_point = _pick_entry_point(change_set, structure=structure)
        # 时序图只关心"有入口意义"的改动：removed 的函数已经没有调用者了
        has_executable_change = any(c.kind is not ChangeKind.REMOVED for c in change_set.all_changes())
        # 时序图的参与者既可能是类，也可能是模块级函数（handle_request 这种入口）。
        # 只拿"改动实体"当白名单，会把图里真实存在的函数名判成编造的组件。
        sequence_entities = set(entities) | {
            name for name in (_short_name(f.name) for f in structure.functions) if name
        }
        sequence_entities |= {cls.name for cls in structure.classes}

        drafts = [
            self._one(
                change_set,
                doc_type=DocType.CLASS_DIAGRAM,
                prompt=build_class_prompt(change_set, module=name, structure=structure),
                module=name,
                target_key=target_key,
                title=f"{name} 类图",
                entities=entities,
                members=members,
                structure=structure,
            ),
            self._one(
                change_set,
                doc_type=DocType.API_DOC,
                prompt=build_api_prompt(change_set, module=name),
                module=name,
                target_key=target_key,
                title=f"{name} API 参考",
                entities=None,
                members=members,
                structure=structure,
            ),
        ]

        if has_executable_change:
            drafts.append(
                self._one(
                    change_set,
                    doc_type=DocType.SEQUENCE_DIAGRAM,
                    prompt=build_sequence_prompt(
                        change_set, module=name, entry_point=entry_point, structure=structure
                    ),
                    module=name,
                    target_key=target_key,
                    title=f"{name} 时序图",
                    entities=sequence_entities,
                    members=members,
                    structure=structure,
                )
            )

        summary = {
            "module": name,
            "target_key": target_key,
            "docs_total": len(drafts),
            "docs_ok": sum(1 for d in drafts if d.ok),
            "docs_needing_review": sum(1 for d in drafts if d.needs_review),
            "repair_rounds": sum(max(0, d.attempts - 1) for d in drafts),
            "tokens": sum(d.tokens for d in drafts),
            "changed_functions": len(members),
        }
        logger.info("module documentation %s", summary)
        return ModuleDocumentation(module=name, target_key=target_key, drafts=tuple(drafts), summary=summary)

    def generate_all(self, change_set: ChangeSet) -> tuple[ModuleDocumentation, ...]:
        # 整个仓库的类名要传给每个模块：模块图里最能说明问题的边
        # （UserService *-- UserRepository）往往跨模块，只认本模块类型就整条丢了。
        context = tuple(sorted(entity_names(change_set)))
        return tuple(
            self.generate_module(bucket, module=module, context=context)
            for module, bucket in sorted(group_by_module(change_set).items())
        )

    # ---- 内部

    def _one(
        self,
        change_set: ChangeSet,
        *,
        doc_type: DocType,
        prompt: str,
        module: str,
        target_key: str,
        title: str,
        entities: set[str] | None,
        members: set[str],
        structure=None,
    ) -> DocumentDraft:
        system_prompt, kind, _ = _SPEC[doc_type]
        expected_relations = None
        expected_calls = None
        advisory_calls = None
        expect_control_flow = False
        if structure is not None and doc_type is DocType.CLASS_DIAGRAM:
            expected_relations = structure.expected_relations() or None
        elif structure is not None and doc_type is DocType.SEQUENCE_DIAGRAM:
            # 时序图只校验"入口所在的调用链"，不要求模型画出模块里的每一条链路：
            # 一张图画三条互不相关的链路，比画错还难读。
            entry = _pick_entry_point(change_set, structure=structure)
            required, advisory = _split_chain_edges(structure, entry)
            expected_calls = required or None
            advisory_calls = advisory or None
            # 分支块只在"入口自己就有 if/for/try"时才要求：模块里别的函数有分支，
            # 不该逼着这张图画一个和它对不上的 alt。
            expect_control_flow = entry in structure.branchy_functions()
        try:
            result: GenerationResult = self.generator.generate(
                system_prompt=system_prompt,
                user_prompt=prompt,
                kind=kind,
                allowed_entities=entities,
                required_members=members if doc_type is not DocType.SEQUENCE_DIAGRAM else None,
                expected_relations=expected_relations,
                expected_calls=expected_calls,
                advisory_calls=advisory_calls,
                expect_control_flow=expect_control_flow,
            )
        except Exception as exc:
            # 传输层彻底失败（重试耗尽/密钥错误）：不要抛出打断整个模块，
            # 让其它文档继续生成，这一份标记为不可发布 + 需要人工处理。
            logger.error("%s generation failed for %s: %s", doc_type.value, module, exc)
            return DocumentDraft(
                doc_type=doc_type,
                target_key=target_key,
                title=title,
                content="",
                ok=False,
                needs_review=True,
                diagnostics=(
                    Diagnostic("X001", f"generation failed: {exc}", severity="error", hint="check LLM credentials and quota"),
                ),
                model=self.model_name,
                prompt_version=PROMPT_VERSION,
            )

        final = result.final
        warnings = [d for d in result.diagnostics if d.severity != "error"]
        if not result.ok:
            # 失败时把 error 诊断写出来。之前只记 transcript（只有错误码），
            # 真实 GitHub 测试里一路只看到 W005 警告，根本不知道是 M005 把文档拒了。
            details = "; ".join(
                f"{d.code} {d.message}" for d in (final.diagnostics if final else ())
            )
            logger.warning(
                "generation rejected for %s/%s after %d attempt(s): %s",
                module,
                doc_type.value,
                result.attempts_used,
                details or "no diagnostics",
            )
        return DocumentDraft(
            doc_type=doc_type,
            target_key=target_key,
            title=title,
            content=result.code,
            ok=result.ok,
            needs_review=(not result.ok) or bool(warnings) or result.needed_repair,
            diagnostics=result.diagnostics,
            attempts=result.attempts_used,
            model=self.model_name,
            tokens=sum(a.tokens for a in result.attempts),
            latency_ms=final.latency_ms if final else 0,
            prompt_version=PROMPT_VERSION,
        )


def _pick_entry_point(change_set: ChangeSet, *, structure=None) -> str:
    """挑一个"最像入口"的改动函数做时序图主角。

    优先级：调用链最长的函数（静态分析事实）> 公开方法（不以 _ 开头）>
    带路由装饰器 > 名字含 handle/main/run > 第一个。

    v4 把"调用链长度"放到第一位：一个只有三行、谁都不调的函数就算名字叫
    `process`，画出来的时序图也只有一句话；而调用链最长的函数才可能画出一张
    有分支、有返回的图。
    """
    if structure is not None:
        best = structure.best_entry_point()
        if best:
            return best

    candidates = [c for c in change_set.all_changes() if c.new or c.old]
    if not candidates:
        return ""

    def score(change) -> int:
        symbol = change.new or change.old
        assert symbol is not None
        value = 0
        if not symbol.name.startswith("_"):
            value += 3
        if any("route" in d or "get" in d or "post" in d for d in symbol.decorators):
            value += 5
        if any(word in symbol.name.lower() for word in ("handle", "main", "run", "execute", "process")):
            value += 2
        if change.kind is ChangeKind.ADDED:
            value += 1
        return value

    best = max(candidates, key=score)
    symbol = best.new or best.old
    return symbol.qualified_name if symbol else ""


def _short_name(name: str) -> str:
    """`a.b.C.method` -> `method`；白名单比对只认短名。"""
    return (name or "").split(".")[-1]


def _participant_name(qualified: str) -> str:
    """`UserService.load` -> `UserService`；`handle_request` -> `handle_request`。

    时序图里的参与者是**类**（方法调用就是该类的一条消息），只有模块级函数
    才以函数名当参与者。用完整限定名比对，永远对不上 `UserService -> UserService`
    这种自调用——真实链路里 `deactivate -> load` 就是这样被误判成"没画"的。
    """
    parts = [part for part in (qualified or "").split(".") if part]
    if len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else ""


def _chain_edges(structure, entry_point: str) -> set[tuple[str, str]]:
    """把 "A -> B" 形式的调用链边拆成 (A, B) 参与者名对，供校验器比对。"""
    edges: set[tuple[str, str]] = set()
    if structure is None or not entry_point:
        return edges
    for edge in structure.chain_for(entry_point):
        caller, _, callee = edge.partition(" -> ")
        if caller and callee:
            edges.add((_participant_name(caller.strip()), _participant_name(callee.strip())))
    return edges


def _chain_depth(structure, entry_point: str) -> dict[tuple[str, str], int]:
    """算出每条调用边离入口有多远（入口直接调用的边是第 1 跳）。"""
    depths: dict[tuple[str, str], int] = {}
    if structure is None or not entry_point:
        return depths
    distance: dict[str, int] = {entry_point: 0}
    for edge in structure.chain_for(entry_point):
        caller, _, callee = edge.partition(" -> ")
        caller, callee = caller.strip(), callee.strip()
        if not caller or not callee:
            continue
        hop = distance.get(caller, 0) + 1
        distance.setdefault(callee, hop)
        depths[(caller, callee)] = hop
    return depths


def _split_chain_edges(
    structure,
    entry_point: str,
    *,
    required_hops: int = 2,
    max_advisory: int = 4,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """把调用边分成"必须画"和"画了更好"两档。

    真实模型在大模块上很难把四跳以外的边全画出来，硬要求只会换来三次拒稿。
    前两跳（入口 → 服务 → 仓储）是这张图的主干，漏了就等于没画；
    更深的边（缓存命中后再失效之类）记 W011，提醒但不太监。

    `max_advisory` 再给"可选边"封顶：把十几条深层边全列给模型，它会照着全画，
    然后在一张图上撞到 max_tokens（真实链路里确实这样被截断过）。
    只留最靠近入口的几条，既保住"链是连的"，又不逼它写小作文。
    """
    depths = _chain_depth(structure, entry_point)
    # 归一到"参与者名"再分档：图里写的是类名（`UserService -> UserService`），
    # 这里留着 `deactivate -> load` 的限定名就永远比不中。
    merged: dict[tuple[str, str], int] = {}
    for (caller, callee), hop in depths.items():
        key = (_participant_name(caller), _participant_name(callee))
        if not key[0] or not key[1]:
            continue
        merged[key] = min(hop, merged.get(key, hop))
    required = {edge for edge, hop in merged.items() if hop <= required_hops}
    deeper = sorted(
        ((edge, hop) for edge, hop in merged.items() if hop > required_hops),
        key=lambda item: (item[1], item[0]),
    )
    advisory = {edge for edge, _hop in deeper[:max_advisory]} - required
    return required, advisory
