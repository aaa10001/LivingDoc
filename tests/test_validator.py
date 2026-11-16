"""校验器测试：每一条规则都要有一个"模型真的会这么错"的样例。"""

import pytest

from app.ai.validator import (
    MarkdownValidator,
    PlantUMLValidator,
    extract_markdown,
    extract_plantuml,
    validate_markdown,
    validate_plantuml,
)

BT = "\u0060" * 3


GOOD_CLASS = """@startuml
class UserService {
  +load(user_id: int): dict
}
class UserRepository {
  +find_by_id(user_id: int): User
}
UserService --> UserRepository : uses
@enduml"""


def codes(result):
    return {d.code for d in result.diagnostics}


def seq_with_edges(edges):
    """按边的顺序拼一张最小时序图：每条边一条消息 + 一条返回。"""
    lines = ["@startuml"]
    for source, target in edges:
        lines.append(f"{source} -> {target} : 调用")
        lines.append(f"{target} --> {source} : 返回")
    lines.append("@enduml")
    return "\n".join(lines)


def test_valid_class_diagram_passes():
    result = validate_plantuml(GOOD_CLASS)
    assert result.ok, result.render()
    assert codes(result) == set()


def test_extract_strips_fence_and_prose():
    raw = "Here you go:\n\n" + BT + "plantuml\n" + GOOD_CLASS + "\n" + BT + "\n\nHope this helps!"
    extraction = extract_plantuml(raw)
    assert extraction.code.startswith("@startuml")
    assert extraction.code.rstrip().endswith("@enduml")
    assert extraction.had_fence is True
    assert extraction.had_prose_before is True


def test_extract_does_not_invent_start_tag():
    extraction = extract_plantuml("class A\n@enduml")
    # 不替模型补 @startuml：补了就等于把错误藏起来，校验器必须看见它
    assert "@startuml" not in extraction.code


def test_missing_start_tag():
    assert "E001" in codes(validate_plantuml("class A\n@enduml"))


def test_truncated_output_is_e002():
    result = validate_plantuml("@startuml\nclass A {\n  +go()\n")
    assert result.ok is False
    assert "E002" in codes(result)


def test_prose_after_end_tag():
    result = validate_plantuml(GOOD_CLASS + "\nThe diagram above shows the service layer.")
    assert "E003" in codes(result)


def test_empty_diagram():
    assert "E004" in codes(validate_plantuml("@startuml\n@enduml"))


def test_unclosed_brace():
    code = "@startuml\nclass A {\n  +go()\n@enduml"
    result = validate_plantuml(code)
    assert "E005" in codes(result)
    assert result.ok is False


def test_extra_closing_brace():
    code = "@startuml\nclass A {\n  +go()\n}\n}\n@enduml"
    assert "E006" in codes(validate_plantuml(code))


def test_unbalanced_quote():
    code = '@startuml\nclass A {\n  +go()\n}\nnote left: say "hi\n@enduml'
    assert "E007" in codes(validate_plantuml(code))


def test_unknown_arrow():
    code = "@startuml\nclass A\nclass B\nA <-|> B : weird\n@enduml"
    assert "E008" in codes(validate_plantuml(code))


def test_sequence_message_without_colon():
    code = "@startuml\nAlice -> Bob doSomething\n@enduml"
    assert "E009" in codes(validate_plantuml(code, kind="sequence"))


def test_unclosed_note():
    code = "@startuml\nnote left\n  this never ends\n@enduml"
    assert "E010" in codes(validate_plantuml(code))


def test_member_outside_class():
    code = "@startuml\n+strayMethod(x: int)\nclass A\n@enduml"
    assert "E011" in codes(validate_plantuml(code))


def test_unbalanced_method_parens():
    code = "@startuml\nclass A {\n  +go(x: int\n}\n@enduml"
    assert "E012" in codes(validate_plantuml(code))


def test_hallucinated_class_is_warning_not_error():
    code = "@startuml\nclass UserService\nclass PaymentGateway\n@enduml"
    result = PlantUMLValidator(allowed_entities={"UserService"}).validate(code)
    assert result.ok is True  # 能渲染，所以不阻塞
    assert "W001" in codes(result)
    warn = [d for d in result.diagnostics if d.code == "W001"][0]
    assert warn.severity == "warning"
    assert "PaymentGateway" in warn.message


def test_missing_changed_member_is_warning():
    code = "@startuml\nclass UserService {\n  +load()\n}\n@enduml"
    result = PlantUMLValidator(required_members={"UserService.delete"}).validate(code)
    assert "W002" in codes(result)
    assert result.ok is True


def test_member_coverage_is_case_insensitive():
    code = "@startuml\nclass UserService {\n  +Delete()\n}\n@enduml"
    result = PlantUMLValidator(required_members={"UserService.delete"}).validate(code)
    assert "W002" not in codes(result)


def test_unquoted_generic_class_name():
    code = "@startuml\nclass <T>\n@enduml"
    assert "W003" in codes(validate_plantuml(code))


# ---------------------------------------------------------------- Markdown

GOOD_MD = """## app.services

### UserService.load

- signature: def load(self, user_id: int) -> dict
- returns: the user record

| name | type | required |
| --- | --- | --- |
| user_id | int | yes |
"""


def test_markdown_ok():
    assert validate_markdown(GOOD_MD).ok


def test_inner_code_fence_is_not_treated_as_wrapper():
    """真实 GitHub 文档的第一个函数带示例代码，旧实现从这里截断了整篇。"""
    raw = GOOD_MD + "\n" + BT + "python\nprint(1)\n" + BT + "\n"

    extraction = extract_markdown(raw)

    assert extraction.code.startswith("## app.services")
    assert "print(1)" in extraction.code
    assert extraction.had_fence is False


def test_outer_fence_with_inner_code_block_is_still_unwrapped():
    raw = BT + "markdown\n" + GOOD_MD + "\n" + BT + "python\nprint(1)\n" + BT + "\n" + BT

    extraction = extract_markdown(raw)

    assert extraction.had_fence is True
    assert extraction.code.startswith("## app.services")
    assert "print(1)" in extraction.code


def test_markdown_preamble_is_error():
    result = validate_markdown("好的，以下是 API 文档：\n\n" + GOOD_MD)
    assert result.ok is False
    assert "M001" in codes(result)


def test_markdown_unbalanced_fence():
    text = GOOD_MD + "\n" + BT + "python\nprint(1)\n"
    assert "M002" in codes(validate_markdown(text))


def test_markdown_empty():
    assert "M003" in codes(validate_markdown(""))


def test_markdown_placeholder():
    text = GOOD_MD + "\nTODO: <your-code-here>\n"
    assert "M004" in codes(validate_markdown(text))


def test_markdown_missing_required_function():
    validator = MarkdownValidator(required_members={"UserService.delete"})

    api = validator.validate(GOOD_MD, kind="api")
    assert "W005" in codes(api)
    assert api.ok is False

    # 图里允许合并/省略次要成员，仍然只是 warning。
    diagram = validator.validate(GOOD_MD, kind="class")
    assert "W005" in codes(diagram)
    assert diagram.ok is True


def test_extract_markdown_strips_preamble():
    raw = "Sure! Here is the doc.\n\n" + GOOD_MD
    extraction = extract_markdown(raw)
    assert extraction.had_prose_before is True
    assert extraction.code.startswith("## app.services")


def test_extract_markdown_from_fenced_block():
    raw = BT + "markdown\n" + GOOD_MD + BT
    extraction = extract_markdown(raw)
    assert extraction.had_fence is True
    assert extraction.code.startswith("## app.services")


@pytest.mark.parametrize("kind", ["class", "sequence"])
def test_kind_is_echoed(kind):
    result = validate_plantuml("@startuml\n@enduml", kind=kind)
    assert result.kind == kind


def test_markdown_heading_only_is_error():
    """真实模型回过 29 个字符的"文档"：标题 + 一行反引号。
    M001-M004 全过，但它是垃圾，必须被 M005 拦住。"""
    from app.ai.validator import validate_markdown

    result = validate_markdown("### `UserRepository.delete`\n")
    assert result.ok is False
    assert [d.code for d in result.diagnostics] == ["M005"]


def test_markdown_short_body_is_warning():
    from app.ai.validator import validate_markdown

    result = validate_markdown("## app\n\nA short but non-empty body, still far below the comfortable size.\n")
    assert result.ok is True
    assert "W006" in [d.code for d in result.diagnostics]


def test_markdown_normal_body_has_no_length_diagnostic():
    from app.ai.validator import validate_markdown

    body = "## app\n\n### load\n\nLoads a user by id. Raises UserNotFound when the id does not exist. "
    body += "Parameters: user_id (int). Returns: User. Errors: UserNotFound. "
    body += "Parameters: user_id (int). Returns: User.\n"
    result = validate_markdown(body)
    assert not any(d.code in {"M005", "W006"} for d in result.diagnostics)


# --------------------------------------------------------------------------- #
# v4：图必须画出结构事实（否则"一排孤立类框"也能通过语法校验）
# --------------------------------------------------------------------------- #
ISOLATED_CLASS = """@startuml
class UserService {
  +load(user_id: int): dict
}
class UserRepository {
  +find_by_id(user_id: int): User
}
@enduml"""

RELATED_CLASS = """@startuml
class UserService {
  +load(user_id: int): dict
}
class UserRepository {
  +find_by_id(user_id: int): User
}
UserService --> UserRepository : 使用
@enduml"""

HIERARCHY_CLASS = """@startuml
class BaseService
class UserService
class UserReader
class UserRepository
UserService --|> BaseService : 继承
UserRepository ..|> UserReader : 实现
UserService *-- UserRepository : 组合
@enduml"""


def test_isolated_classes_are_an_error_when_relations_expected():
    validator = PlantUMLValidator(expected_relations={("UserService", "UserRepository", "uses")})
    result = validator.validate(ISOLATED_CLASS, kind="class")
    assert result.ok is False
    assert "E013" in codes(result)
    assert {d.code for d in result.diagnostics if d.severity == "error"} == {"E013", "E014"}


def test_no_relation_expectation_keeps_old_behaviour():
    """没传 expected_relations 时（老调用方）不做孤立类检查。"""
    assert validate_plantuml(ISOLATED_CLASS).ok is True


def test_drawn_relation_satisfies_expectation():
    validator = PlantUMLValidator(expected_relations={("UserService", "UserRepository", "uses")})
    assert validator.validate(RELATED_CLASS, kind="class").ok is True


def test_expected_relation_missing_is_e014():
    # 源码里 Cache 是 UserService 的字段，图上却完全没提 Cache
    validator = PlantUMLValidator(expected_relations={("UserService", "Cache", "composition")})
    result = validator.validate(RELATED_CLASS, kind="class")
    assert result.ok is False
    assert "E014" in codes(result)


def test_wrong_arrow_kind_is_warning_not_error():
    WRONG_ARROW = """@startuml
class UserService
class UserRepository
UserService --> UserRepository : 使用
@enduml"""
    validator = PlantUMLValidator(expected_relations={("UserService", "UserRepository", "composition")})
    result = validator.validate(WRONG_ARROW, kind="class")
    assert result.ok is True
    assert "W008" in codes(result)


def test_hierarchy_arrows_are_recognised():
    validator = PlantUMLValidator(
        expected_relations={
            ("UserService", "BaseService", "extends"),
            ("UserRepository", "UserReader", "implements"),
            ("UserService", "UserRepository", "composition"),
        }
    )
    result = validator.validate(HIERARCHY_CLASS, kind="class")
    assert result.ok, result.render()
    assert not any(d.code.startswith("E01") for d in result.diagnostics)


SEQ_CALL_ONLY = """@startuml
actor 调用方
调用方 -> UserService : load(1)
UserService -> UserRepository : find_by_id(1)
@enduml"""

SEQ_WITH_RETURN = """@startuml
actor 调用方
调用方 -> UserService : load(1)
UserService -> UserRepository : find_by_id(1)
UserRepository --> UserService : user
UserService --> 调用方 : dict
@enduml"""


def test_forward_calls_without_returns_is_error():
    result = validate_plantuml(SEQ_CALL_ONLY, kind="sequence")
    assert result.ok is False
    assert "E015" in codes(result)


def test_returns_are_dashed_arrows():
    result = validate_plantuml(SEQ_WITH_RETURN, kind="sequence")
    assert "E015" not in codes(result)
    assert "W010" not in codes(result)


def test_missing_expected_call_is_e016():
    validator = PlantUMLValidator(expected_calls={("UserService", "UserRepository")})
    assert validator.validate(SEQ_WITH_RETURN, kind="sequence").ok is True

    validator = PlantUMLValidator(expected_calls={("UserService", "Cache")})
    result = validator.validate(SEQ_WITH_RETURN, kind="sequence")
    assert result.ok is False
    assert "E016" in codes(result)


def test_expected_call_matches_across_aliases():
    puml = """@startuml
participant UserService as svc
participant UserRepository as repo
svc -> repo : find_by_id(1)
repo --> svc : user
@enduml"""
    validator = PlantUMLValidator(expected_calls={("UserService", "UserRepository")})
    assert validator.validate(puml, kind="sequence").ok is True


def test_missing_control_flow_block_is_e017():
    validator = PlantUMLValidator(expect_control_flow=True)
    result = validator.validate(SEQ_WITH_RETURN, kind="sequence")
    assert result.ok is False
    assert "E017" in codes(result)


def test_alt_block_satisfies_control_flow():
    puml = """@startuml
调用方 -> UserService : load(1)
alt 用户存在
  UserService -> UserRepository : find_by_id(1)
  UserRepository --> UserService : user
else 用户不存在
  UserService -> UserService : 返回空
end
UserService --> 调用方 : dict
@enduml"""
    validator = PlantUMLValidator(expect_control_flow=True)
    assert validator.validate(puml, kind="sequence").ok is True


def test_placeholder_participant_is_flagged_when_explicit():
    puml = """@startuml
participant process
participant system
process -> system : go()
system --> process : ok
@enduml"""
    result = validate_plantuml(puml, kind="sequence", allowed_entities={"UserService"})
    assert "W009" in codes(result)


def test_deep_call_edge_missing_is_only_advice():
    """第四跳的边漏了不该毙稿：一张图装不下整棵调用树，主干才必须齐。"""
    validator = PlantUMLValidator(
        expected_calls={("调用方", "UserService")},
        advisory_calls={("Cache", "Storage")},
    )
    result = validator.validate(SEQ_WITH_RETURN, kind="sequence")
    assert result.ok is True
    assert "W011" in codes(result)
    assert "E016" not in codes(result)


def test_deep_call_edge_drawn_produces_no_warning():
    puml = seq_with_edges([("调用方", "UserService"), ("UserService", "UserRepository"), ("Cache", "Storage")])
    validator = PlantUMLValidator(advisory_calls={("Cache", "Storage")})
    result = validator.validate(puml, kind="sequence")
    assert result.ok is True
    assert "W011" not in codes(result)


def test_expected_call_wins_over_advisory_for_the_same_edge():
    """同一条边既是必需又是深层：按必需算，不能被 advisory 降级。"""
    validator = PlantUMLValidator(
        expected_calls={("UserService", "Cache")},
        advisory_calls={("UserService", "Cache")},
    )
    result = validator.validate(SEQ_WITH_RETURN, kind="sequence")
    assert "E016" in codes(result)
    # 不该同时报一条"深层边没画"的提醒
    assert "W011" not in codes(result)


def test_incidental_forward_call_is_not_counted_as_missing_return():
    """`logger.warning(...)` 这类单向调用本来就没有返回值，不该逼模型编一个。"""
    puml = """@startuml
UserService -> UserRepository : find_by_id(1)
UserRepository --> UserService : user
UserService -> Logger : warning("x")
UserService --> 调用方 : dict
@enduml"""
    validator = PlantUMLValidator(expected_calls={("UserService", "UserRepository")})
    result = validator.validate(puml, kind="sequence")
    assert "W010" not in codes(result)
    assert result.ok is True


def test_missing_return_on_chain_edge_is_still_w010():
    puml = """@startuml
UserService -> UserRepository : find_by_id(1)
UserService --> 调用方 : dict
@enduml"""
    validator = PlantUMLValidator(expected_calls={("UserService", "UserRepository")})
    result = validator.validate(puml, kind="sequence")
    assert "W010" in codes(result)
