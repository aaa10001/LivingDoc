"""大模型客户端：把"调用 DeepSeek"这件事收敛成一个可测试的接口。

两件事必须分清楚，混在一起就会写出既贵又慢的流水线：

    Retry（本文件）        传输层问题：连接失败、超时、429、5xx
                           -> 提示词不变，退避重发，指数退避 + 抖动，尊重 Retry-After
    Self-Correction       语义层问题：模型答非所问、PlantUML 语法错
    （self_correct.py）    -> 提示词要变：带上校验诊断，温度降到 0

为什么要抖动（jitter）：
一个 PR 触发 3 个文档生成任务，它们同时撞上 429，如果退避时间相同，
重试会在同一毫秒再次撞在一起，形成"重试风暴"。全抖动把重试摊平成随机分布。

为什么 400/401/403/422 不重试：
这些是"请求本身错了"——密钥错、模型名错、参数不合法。重试 3 次只是把同一个错误
重复 3 次，还把 5xx 的预算花光。非幂等错误必须立刻上抛，让人看见。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

import httpx

logger = logging.getLogger(__name__)

# 明确可重试的 HTTP 状态码（429 = 限流，5xx = 服务端问题）
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """调用失败。retryable 让上层决定是重试还是直接失败。"""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False, attempts: int = 1) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.attempts = attempts


@dataclass(frozen=True)
class Completion:
    """一次成功调用的结果，带上所有可观测字段。"""

    text: str
    model: str
    attempts: int = 1
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def truncated(self) -> bool:
        # 被 max_tokens 截断是 PlantUML 缺 @enduml 的头号原因，必须显式暴露
        return self.finish_reason == "length"


class LLMClient(Protocol):
    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> Completion: ...


@dataclass
class RetryPolicy:
    max_retries: int = 3  # 总尝试次数 = max_retries + 1
    backoff_base: float = 1.0
    backoff_max: float = 20.0
    jitter: Callable[[], float] = random.random

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return max(0.0, min(retry_after, self.backoff_max))
        raw = min(self.backoff_max, self.backoff_base * (2**attempt))
        # 全抖动：在 [50%, 100%] 之间取，既打散风暴又不至于退避太短
        return raw * (0.5 + 0.5 * self.jitter())


class DeepSeekClient:
    """DeepSeek / 任何 OpenAI 兼容的 chat/completions 端点。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: float = 120.0,
        policy: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_tokens: int = 2048,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required (set LLM_API_KEY)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.policy = policy or RetryPolicy()
        self.sleep = sleep
        self.default_max_tokens = max_tokens
        # transport 只为测试注入（httpx.MockTransport），生产留 None
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # ---- 对外接口

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens or self.default_max_tokens,
            "stream": False,
        }
        started = time.perf_counter()
        last_error: LLMError | None = None

        for attempt in range(self.policy.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = LLMError(
                    f"transport error on attempt {attempt + 1}/{self.policy.max_retries + 1}: {exc!r}",
                    retryable=True,
                    attempts=attempt + 1,
                )
                if attempt >= self.policy.max_retries:
                    break
                delay = self.policy.delay_for(attempt)
                logger.warning("%s; retrying in %.1fs", last_error, delay)
                self.sleep(delay)
                continue

            if response.status_code >= 400:
                retryable = response.status_code in _RETRYABLE_STATUS
                last_error = LLMError(
                    f"HTTP {response.status_code}: {_short_body(response)}",
                    status_code=response.status_code,
                    retryable=retryable,
                    attempts=attempt + 1,
                )
                if not retryable or attempt >= self.policy.max_retries:
                    raise last_error
                delay = self.policy.delay_for(attempt, _retry_after_seconds(response))
                logger.warning("%s; retrying in %.1fs", last_error, delay)
                self.sleep(delay)
                continue

            try:
                text = _extract_text(response.json())
            except Exception as exc:  # JSON 结构不符合预期，属于"不可重试的协议错误"
                raise LLMError(f"malformed response body: {exc!r}; body={_short_body(response)}") from exc

            usage = response.json().get("usage") or {}
            return Completion(
                text=text,
                model=response.json().get("model", self.model),
                attempts=attempt + 1,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                finish_reason=str(choices_first(response.json()).get("finish_reason") or ""),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        assert last_error is not None
        raise last_error

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DeepSeekClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------- 响应解析


def choices_first(body: dict) -> dict:
    choices = body.get("choices") or []
    if not choices:
        raise ValueError("response has no choices")
    return choices[0]


def _extract_text(body: dict) -> str:
    choice = choices_first(body)
    message = choice.get("message") or {}
    content = message.get("content")
    if content is None:
        # 推理模型可能把内容放在 reasoning_content，正文为空
        content = message.get("reasoning_content") or ""
    if not isinstance(content, str):
        raise ValueError(f"content is not a string: {type(content).__name__}")
    return content


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        # Retry-After 允许是 HTTP-date，这里不做完整解析，退化为默认退避
        return None


def _short_body(response: httpx.Response, limit: int = 300) -> str:
    try:
        return response.text[:limit]
    except Exception:
        return "<unreadable body>"


# ---------------------------------------------------------------- 测试替身


@dataclass
class ScriptedLLM:
    """按剧本返回内容的假模型。

    两种用途：
    - 单测里精确控制"第一次错、第二次对"，验证自我纠错真的把诊断带回去了；
    - 演示脚本里在没有 API key / 没有外网的机器上跑通全链路。
    可以塞 Exception 实例来模拟传输失败，从而测试 Retry。
    """

    responses: list[Any]
    model: str = "scripted"
    calls: list[list[dict[str, str]]] = field(default_factory=list)
    temperatures: list[float] = field(default_factory=list)
    _index: int = 0

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> Completion:
        self.calls.append(list(messages))
        self.temperatures.append(temperature)
        if self._index >= len(self.responses):
            raise AssertionError(
                f"ScriptedLLM ran out of responses after {self._index} calls; "
                f"self-correction probably looped more than expected"
            )
        item = self.responses[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        if isinstance(item, Completion):
            return item
        return Completion(text=str(item), model=self.model, attempts=1)
