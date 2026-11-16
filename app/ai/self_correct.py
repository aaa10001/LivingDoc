"""自我纠错：让模型看着校验器给出的错误改自己的作业。

为什么这件事值得单独写一个模块（而不是在调用处 while 循环）：

1. 责任边界。Retry 管"请求没送到"，Self-Correction 管"送到的答案是错的"。
   两者的失败计数、退避策略、日志字段完全不同，混在一个循环里迟早写错。
2. 可审计。每次尝试都要留档（原始输出、抽取后的代码、诊断、耗时），
   否则线上出问题时你只能看到"最后一版是坏的"，不知道它错了几轮、错在哪。
3. fail-closed。轮数用尽仍然是坏的，就必须把 ok=False 往上抛，
   让上层把文档标成 stale，而不是"凑合发一版"。静默发布坏图比不发布更糟：
   读者会以为架构真的长那样。

对话结构（关键）：
    system      规则
    user        任务 + 代码
    assistant   模型上次的原始输出     <- 必须有，否则模型不知道"它"写了什么
    user        校验诊断 + 上一版代码 + "只改这些"

为什么把上一版代码再贴一遍：模型的上下文里虽然有 assistant 消息，但把"要修的代码"
放在最后一条 user 消息里，遵循度明显更高（末尾信息权重最大），也避免它去改
我们其实没报错的部分。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Sequence

from .client import Completion, LLMClient, LLMError
from .prompts import build_repair_prompt
from .validator import (
    Diagnostic,
    Extraction,
    MarkdownValidator,
    PlantUMLValidator,
    extract_markdown,
    extract_plantuml,
)

logger = logging.getLogger(__name__)

_PLANTUML_KINDS = frozenset({"class", "sequence"})


@dataclass(frozen=True)
class Attempt:
    """一轮尝试的完整留档。"""

    index: int
    raw: str
    code: str
    ok: bool
    diagnostics: tuple[Diagnostic, ...]
    temperature: float
    latency_ms: int = 0
    finish_reason: str = ""
    tokens: int = 0

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity == "error")

    def summary(self) -> str:
        codes = ", ".join(sorted({d.code for d in self.diagnostics})) or "none"
        return f"attempt {self.index}: ok={self.ok} codes=[{codes}]"


@dataclass(frozen=True)
class GenerationResult:
    ok: bool
    code: str
    kind: str
    attempts: tuple[Attempt, ...] = ()
    extraction: Extraction | None = None

    @property
    def final(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def attempts_used(self) -> int:
        return len(self.attempts)

    @property
    def needed_repair(self) -> bool:
        # 第一轮就过 = 不需要修复；否则说明提示词或模型有问题，值得记指标
        return self.attempts_used > 1

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        final = self.final
        return final.diagnostics if final else ()

    def transcript(self) -> str:
        """给报告/日志用的一行式轨迹。"""
        return " | ".join(a.summary() for a in self.attempts)


class SelfCorrectingGenerator:
    """generate -> extract -> validate -> (带诊断重试) 的闭环。"""

    def __init__(
        self,
        client: LLMClient,
        *,
        max_attempts: int = 3,
        temperature: float = 0.2,
        repair_temperature: float = 0.0,
        max_tokens: int = 2048,
        sleep=time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.client = client
        self.max_attempts = max_attempts
        self.temperature = temperature
        # 修复时温度降到 0：这个阶段要的是"改对"，不是"发挥"
        self.repair_temperature = repair_temperature
        self.max_tokens = max_tokens
        self.sleep = sleep

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        kind: str = "class",
        allowed_entities: set[str] | None = None,
        required_members: set[str] | None = None,
        expected_relations: set[tuple[str, str, str]] | None = None,
        expected_calls: set[tuple[str, str]] | None = None,
    advisory_calls: set[tuple[str, str]] | None = None,
        expect_control_flow: bool = False,
    ) -> GenerationResult:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        attempts: list[Attempt] = []
        last_extraction: Extraction | None = None
        # 上一轮是否是"整篇没内容"（api 文档的 M003/M005）。是的话下一轮要重新生成，
        # 而不是继续"只改这一行"——空壳文档的毛病不在细节。
        regenerate_next = False

        for index in range(self.max_attempts):
            temperature = (
                self.temperature
                if index == 0 or regenerate_next
                else self.repair_temperature
            )
            started = time.perf_counter()
            completion: Completion = self.client.complete(
                messages, temperature=temperature, max_tokens=self.max_tokens
            )
            latency = int((time.perf_counter() - started) * 1000)

            extraction = self._extract(completion.text, kind)
            last_extraction = extraction
            result = self._validate(
                extraction.code,
                kind=kind,
                allowed_entities=allowed_entities,
                required_members=required_members,
                expected_relations=expected_relations,
                expected_calls=expected_calls,
                advisory_calls=advisory_calls,
                expect_control_flow=expect_control_flow,
            )
            attempt = Attempt(
                index=index,
                raw=completion.text,
                code=extraction.code,
                ok=result.ok,
                diagnostics=tuple(result.diagnostics),
                temperature=temperature,
                latency_ms=latency,
                finish_reason=completion.finish_reason,
                tokens=completion.total_tokens,
            )
            attempts.append(attempt)
            logger.info("generation %s kind=%s", attempt.summary(), kind)

            # 顺带记录"模型自己加了围栏/寒暄"的比例，这是提示词质量的定量指标
            if extraction.had_fence or extraction.had_prose_before:
                logger.info(
                    "model violated output contract: fence=%s prose_before=%s",
                    extraction.had_fence,
                    extraction.had_prose_before,
                )

            if result.ok:
                return GenerationResult(ok=True, code=extraction.code, kind=kind, attempts=tuple(attempts), extraction=extraction)

            # 没有下一轮机会了，直接 fail-closed
            if index == self.max_attempts - 1:
                break

            # 被 max_tokens 截断时，重试的意义不在于"改语法"而在于"给足预算"
            too_long = False
            if completion.truncated:
                logger.warning(
                    "output truncated by max_tokens (%s); raising budget for repair round",
                    completion.finish_reason,
                )
                self.max_tokens = int(self.max_tokens * 1.5)
                # 时序图被截断说明"想画的东西比预算多"：光加预算会一路撞墙，
                # 得同时要求它砍掉枝节。整张重画，否则补出来的图仍是断的。
                too_long = kind == "sequence"

            # 空壳文档（M003 完全空 / M005 标题下没有正文）不是"改一行"能救的：
            # 让修复轮退回生成温度并明确要求整篇重写，否则模型只会补一句话，
            # 下一轮继续 M005，三轮全废（真实评测里 api 文档就这么挂过）。
            regenerate_next = (
                (kind == "api" and any(d.code in {"M003", "M005"} for d in attempt.errors))
                # 时序图"根本没画调用链"（E016 缺调用边、E017 缺分支块）不是补一行能修的：
                # 补一行只会得到一张"多了一条消息但仍然没有返回箭头"的图，下一轮继续失败。
                # 这类失败要求整张重画，让模型回到生成温度和完整输入。
                or (kind == "sequence" and any(d.code in {"E016", "E017"} for d in attempt.errors))
                # 截断的图是没有尾巴的，接着改只会越补越乱
                or too_long
            )
            if regenerate_next:
                logger.warning(
                    "%s document is structurally incomplete (attempt %s); regenerating from scratch",
                    kind,
                    index + 1,
                )

            repair = build_repair_prompt(
                diagnostics=attempt.errors or attempt.diagnostics,
                previous_code=extraction.code,
                kind=kind,
                regenerate=regenerate_next,
                too_long=too_long,
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": completion.text},
                {"role": "user", "content": repair},
            ]

        return GenerationResult(ok=False, code=attempts[-1].code, kind=kind, attempts=tuple(attempts), extraction=last_extraction)

    # ---- 内部

    def _extract(self, raw: str, kind: str) -> Extraction:
        if kind in _PLANTUML_KINDS:
            return extract_plantuml(raw)
        return extract_markdown(raw)

    def _validate(
        self,
        code: str,
        *,
        kind: str,
        allowed_entities: set[str] | None,
        required_members: set[str] | None,
        expected_relations: set[tuple[str, str, str]] | None = None,
        expected_calls: set[tuple[str, str]] | None = None,
    advisory_calls: set[tuple[str, str]] | None = None,
        expect_control_flow: bool = False,
    ):
        if kind in _PLANTUML_KINDS:
            return PlantUMLValidator(
                allowed_entities=allowed_entities,
                required_members=required_members,
                expected_relations=expected_relations,
                expected_calls=expected_calls,
                advisory_calls=advisory_calls,
                expect_control_flow=expect_control_flow,
            ).validate(code, kind=kind)
        return MarkdownValidator(required_members=required_members).validate(code, kind="api")


def generate_with_repair(
    client: LLMClient,
    *,
    system_prompt: str,
    user_prompt: str,
    kind: str = "class",
    max_attempts: int = 3,
    allowed_entities: set[str] | None = None,
    required_members: set[str] | None = None,
    expected_relations: set[tuple[str, str, str]] | None = None,
    expected_calls: set[tuple[str, str]] | None = None,
    advisory_calls: set[tuple[str, str]] | None = None,
    expect_control_flow: bool = False,
) -> GenerationResult:
    """一行式便捷入口。"""
    return SelfCorrectingGenerator(client, max_attempts=max_attempts).generate(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        kind=kind,
        allowed_entities=allowed_entities,
        required_members=required_members,
        expected_relations=expected_relations,
        expected_calls=expected_calls,
        advisory_calls=advisory_calls,
        expect_control_flow=expect_control_flow,
    )
