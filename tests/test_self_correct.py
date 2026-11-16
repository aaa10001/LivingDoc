"""自我纠错测试：证明"诊断真的回到了模型手里"，而不只是我们以为它回去了。"""

import pytest

from app.ai.client import Completion, LLMError, ScriptedLLM
from app.ai.self_correct import SelfCorrectingGenerator, generate_with_repair

GOOD = "@startuml\nclass UserService {\n  +load(user_id: int): dict\n}\n@enduml"
BAD_TRUNCATED = "@startuml\nclass UserService {\n  +load(user_id: int): dict"
BT = "\u0060" * 3

GOOD_MD = """## app.services

### UserService.load

- signature: def load(self, user_id: int) -> dict
"""


def make_generator(responses, **kwargs):
    fake = ScriptedLLM(responses)
    return fake, SelfCorrectingGenerator(fake, **kwargs)


def test_repairs_after_validation_error():
    fake, generator = make_generator([BAD_TRUNCATED, GOOD])
    result = generator.generate(system_prompt="sys", user_prompt="task", kind="class")

    assert result.ok is True
    assert result.attempts_used == 2
    assert result.needed_repair is True
    assert result.final.ok is True

    # 第 1 轮原温度，第 2 轮降到 0
    assert fake.temperatures == [0.2, 0.0]

    # 诊断确实回灌：assistant 里是模型自己的输出，最后一条 user 里是错误报告
    second_call = fake.calls[1]
    assert [m["role"] for m in second_call] == ["system", "user", "assistant", "user"]
    assert second_call[2]["content"] == BAD_TRUNCATED
    repair = second_call[3]["content"]
    assert "E002" in repair
    assert "Fix ONLY" in repair
    assert "@startuml" in repair  # 上一版代码被贴回去了


def test_first_try_ok_needs_no_repair():
    fake, generator = make_generator([GOOD])
    result = generator.generate(system_prompt="sys", user_prompt="task")
    assert result.ok is True
    assert result.attempts_used == 1
    assert result.needed_repair is False
    assert fake.temperatures == [0.2]


def test_fail_closed_after_max_attempts():
    fake, generator = make_generator([BAD_TRUNCATED, BAD_TRUNCATED, "@startuml\nclass A {"], max_attempts=3)
    result = generator.generate(system_prompt="sys", user_prompt="task")

    assert result.ok is False
    assert result.attempts_used == 3
    assert len(fake.calls) == 3
    assert any(d.code == "E002" or d.code == "E005" for d in result.diagnostics)
    assert "attempt 2" in result.transcript()
    # 坏文档不能悄悄变成"可以发布"
    assert result.code.strip() != ""


def test_fence_is_stripped_and_accepted():
    fenced = "Sure, here it is:\n" + BT + "plantuml\n" + GOOD + "\n" + BT
    _, generator = make_generator([fenced])
    result = generator.generate(system_prompt="sys", user_prompt="task")

    assert result.ok is True
    assert result.code.startswith("@startuml")
    assert BT not in result.code
    assert result.extraction is not None and result.extraction.had_fence is True


def test_markdown_preamble_is_tolerated_but_recorded():
    """一句寒暄不值得多花一次 API 调用去纠正。

    抽取器把标题之前的话剪掉，校验器于是看不到 M001；我们把"模型违反了输出契约"
    记在 extraction.had_prose_before 里作为提示词质量的指标，而不是让它变成一轮修复。
    """
    bad = "好的，以下是 API 文档：\n\n" + GOOD_MD
    _, generator = make_generator([bad])
    result = generator.generate(system_prompt="sys", user_prompt="task", kind="api")

    assert result.ok is True
    assert result.attempts_used == 1
    assert result.code.startswith("## app.services")
    assert result.extraction.had_prose_before is True


def test_empty_api_document_is_regenerated_not_patched():
    """M005 空壳：修复提示必须是"整篇重写"，且温度回到生成温度。

    只补一句"请补充内容"的话，模型会在原文上追加一行，下一轮仍然 M005；
    真实评测中 api_doc 就是这样连续三轮失败的。
    """
    shell = "## app.services\n"
    fake, generator = make_generator([shell, GOOD_MD])
    result = generator.generate(system_prompt="sys", user_prompt="task", kind="api")

    assert result.ok is True
    assert result.attempts_used == 2
    assert fake.temperatures == [0.2, 0.2]
    repair = fake.calls[1][-1]["content"]
    assert "M005" in repair
    assert "rewrite" in repair.lower()
    assert "from scratch" in repair
    assert "Fix ONLY" not in repair
    assert "## app.services" in repair


def test_markdown_placeholder_triggers_repair():
    bad = GOOD_MD + "\n" + BT + "python\n<your-code-here>\n" + BT + "\n"
    _, generator = make_generator([bad, GOOD_MD])
    result = generator.generate(system_prompt="sys", user_prompt="task", kind="api")
    assert result.ok is True
    assert result.attempts_used == 2


def test_truncation_raises_token_budget():
    truncated = Completion(text="@startuml\nclass A {", model="m", finish_reason="length")
    _, generator = make_generator([truncated, GOOD], max_tokens=2048)
    result = generator.generate(system_prompt="sys", user_prompt="task")
    assert result.ok is True
    assert generator.max_tokens == 3072


def test_transport_error_is_not_self_corrected():
    """传输层失败应该往上抛，让 worker 的重试策略处理，而不是伪装成"模型答错了"。"""
    fake, generator = make_generator([LLMError("HTTP 500", status_code=500, retryable=True)])
    with pytest.raises(LLMError):
        generator.generate(system_prompt="sys", user_prompt="task")
    assert len(fake.calls) == 1


def test_invalid_max_attempts():
    with pytest.raises(ValueError):
        SelfCorrectingGenerator(ScriptedLLM([]), max_attempts=0)


def test_convenience_wrapper():
    fake = ScriptedLLM([GOOD])
    result = generate_with_repair(fake, system_prompt="sys", user_prompt="task")
    assert result.ok is True
