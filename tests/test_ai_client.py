"""客户端测试：用 httpx.MockTransport 精确复现"限流、5xx、掉线、坏响应"。

这些用例一条外网都不用，所以能在没有 API key 的机器上跑。
"""

import json

import httpx
import pytest

from app.ai.client import (
    Completion,
    DeepSeekClient,
    LLMError,
    RetryPolicy,
    ScriptedLLM,
)

OK_BODY = {
    "model": "deepseek-chat",
    "choices": [{"message": {"content": "@startuml\nclass A\n@enduml"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


def make_client(handler, **kwargs):
    sleeps = []
    policy = kwargs.pop("policy", RetryPolicy(max_retries=2, jitter=lambda: 0.0))
    client = DeepSeekClient(
        "test-key",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        policy=policy,
        **kwargs,
    )
    return client, sleeps


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


def test_happy_path():
    def handler(request):
        payload = body_of(request)
        assert payload["model"] == "deepseek-chat"
        assert payload["stream"] is False
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json=OK_BODY)

    client, sleeps = make_client(handler)
    result = client.complete([{"role": "user", "content": "hi"}], temperature=0.1)

    assert result.text.startswith("@startuml")
    assert result.attempts == 1
    assert result.total_tokens == 15
    assert result.truncated is False
    assert sleeps == []


def test_401_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    client, _ = make_client(handler)
    with pytest.raises(LLMError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])

    assert excinfo.value.status_code == 401
    assert excinfo.value.retryable is False
    # 密钥错重试三次只会浪费时间和配额
    assert len(calls) == 1


def test_500_is_retried_then_succeeds():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(500, text="upstream exploded")
        return httpx.Response(200, json=OK_BODY)

    client, sleeps = make_client(handler)
    result = client.complete([{"role": "user", "content": "hi"}])

    assert result.attempts == 2
    assert len(sleeps) == 1


def test_backoff_doubles_with_attempts():
    def handler(request):
        return httpx.Response(503, text="busy")

    client, sleeps = make_client(handler)
    with pytest.raises(LLMError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])

    # policy: base=1.0, jitter=0 -> delay = 1*2^n*0.5
    assert sleeps == [0.5, 1.0]
    assert excinfo.value.retryable is True
    assert excinfo.value.attempts == 3


def test_retry_after_header_wins():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(200, json=OK_BODY)

    client, sleeps = make_client(handler)
    result = client.complete([{"role": "user", "content": "hi"}])

    assert result.attempts == 2
    assert sleeps == [2.0]


def test_retry_after_is_capped():
    policy = RetryPolicy(max_retries=1, backoff_max=5.0, jitter=lambda: 0.0)
    assert policy.delay_for(0, retry_after=3600) == 5.0


def test_transport_error_is_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout("connection reset", request=request)

    client, sleeps = make_client(handler)
    with pytest.raises(LLMError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])

    assert calls["n"] == 3
    assert excinfo.value.retryable is True
    assert len(sleeps) == 2


def test_truncated_output_is_flagged():
    body = {
        "model": "deepseek-chat",
        "choices": [{"message": {"content": "@startuml\nclass A"}, "finish_reason": "length"}],
    }

    def handler(request):
        return httpx.Response(200, json=body)

    client, _ = make_client(handler)
    result = client.complete([{"role": "user", "content": "hi"}])
    assert result.truncated is True


def test_malformed_body_raises_without_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"choices": []})

    client, _ = make_client(handler)
    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1


def test_reasoning_content_fallback():
    body = {
        "choices": [{"message": {"content": None, "reasoning_content": "@startuml\nclass A\n@enduml"}, "finish_reason": "stop"}]
    }

    def handler(request):
        return httpx.Response(200, json=body)

    client, _ = make_client(handler)
    assert client.complete([{"role": "user", "content": "hi"}]).text.startswith("@startuml")


def test_jitter_bounds():
    low = RetryPolicy(max_retries=0, backoff_base=4.0, jitter=lambda: 0.0).delay_for(0)
    high = RetryPolicy(max_retries=0, backoff_base=4.0, jitter=lambda: 1.0).delay_for(0)
    assert low == 2.0
    assert high == 4.0


def test_api_key_required():
    with pytest.raises(ValueError):
        DeepSeekClient("")


def test_scripted_llm_records_calls():
    fake = ScriptedLLM(["a", "b"])
    assert fake.complete([{"role": "user", "content": "x"}]).text == "a"
    assert fake.complete([{"role": "user", "content": "y"}], temperature=0.0).text == "b"
    assert fake.temperatures == [0.2, 0.0]
    with pytest.raises(AssertionError):
        fake.complete([{"role": "user", "content": "z"}])


def test_scripted_llm_can_raise():
    fake = ScriptedLLM([LLMError("boom", retryable=False), "@startuml\n@enduml"])
    with pytest.raises(LLMError):
        fake.complete([{"role": "user", "content": "x"}])
    assert fake.complete([{"role": "user", "content": "x"}]).text.startswith("@startuml")


def test_completion_properties():
    done = Completion(text="x", model="m", prompt_tokens=3, completion_tokens=4, finish_reason="stop")
    assert done.total_tokens == 7
    assert done.truncated is False
