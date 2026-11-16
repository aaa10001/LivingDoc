"""提示词测试：契约必须真的写在提示词里，否则就是注释里的一厢情愿。"""

from app.ai.prompts import (
    API_SYSTEM_PROMPT,
    CLASS_SYSTEM_PROMPT,
    SEQUENCE_SYSTEM_PROMPT,
    WARMUP_REPLY,
    WARMUP_USER,
    build_api_prompt,
    build_class_prompt,
    build_input_block,
    build_repair_prompt,
    build_sequence_prompt,
    build_warmup,
    changed_members,
    entity_names,
)
from app.ai.validator import Diagnostic
from app.parsers import ChangeSet, TreeSitterExtractor, diff_symbols

PATH = "app/services/user.py"
OLD = """class UserService:
    def load(self, user_id):
        return None

    def delete(self, user_id):
        return True
"""

NEW = """class UserService:
    def load(self, user_id):
        return {"id": user_id}
"""


def make_change_set():
    extractor = TreeSitterExtractor()
    old = extractor.parse(PATH, OLD.encode())
    new = extractor.parse(PATH, NEW.encode())
    return ChangeSet(files=[diff_symbols(PATH, old, new)], skipped_files=[])


def test_contract_items_present():
    for prompt in (CLASS_SYSTEM_PROMPT, SEQUENCE_SYSTEM_PROMPT):
        assert "@startuml" in prompt
        assert "@enduml" in prompt
        assert "markdown fences" in prompt
        assert "first character" in prompt


def test_api_prompt_forbids_invention():
    assert "Never invent" in API_SYSTEM_PROMPT
    assert "## " in API_SYSTEM_PROMPT


def test_class_prompt_carries_few_shot():
    assert "class UserService {" in CLASS_SYSTEM_PROMPT
    assert "@enduml" in CLASS_SYSTEM_PROMPT.split("## Example")[1]


def test_input_block_has_module_entities_and_code():
    block = build_input_block(make_change_set(), module="app.services")
    assert block.startswith("module: app.services")
    assert "known entities: UserService" in block
    assert "UserService.load" in block
    assert 'return {"id": user_id}' in block


def test_entity_names():
    assert entity_names(make_change_set()) == {"UserService"}


def test_changed_members_skip_removed():
    members = changed_members(make_change_set())
    assert "UserService.load" in members
    # delete 在新版本里被删掉了，不应该要求它出现在新文档里
    assert "UserService.delete" not in members


def test_changed_members_skip_formatting_only():
    """没有送进 prompt 的函数，不能被校验器要求出现在图里。"""
    extractor = TreeSitterExtractor()
    before = "class UserService:\n    def load(self):\n        return None\n"
    after = "class UserService:\n    def load(self):\n        # comment\n        return None\n"
    change = diff_symbols(
        PATH,
        extractor.parse(PATH, before.encode()),
        extractor.parse(PATH, after.encode()),
    )
    members = changed_members(ChangeSet(files=[change], skipped_files=[]))

    assert members == set()


def test_changed_members_top_level_function_has_no_double_owner():
    """顶层函数曾被拼成 has_read.has_read，校验器因此永远报 W005。"""
    extractor = TreeSitterExtractor()
    new = extractor.parse(PATH, b"def has_read(obj):\n    return hasattr(obj, 'read')\n")
    change = diff_symbols(PATH, extractor.parse(PATH, b""), new)

    members = changed_members(ChangeSet(files=[change], skipped_files=[]))

    assert members == {"has_read"}


def test_prompts_restate_the_contract():
    class_prompt = build_class_prompt(make_change_set())
    seq_prompt = build_sequence_prompt(make_change_set(), entry_point="UserService.load")
    api_prompt = build_api_prompt(make_change_set())

    for prompt in (class_prompt, seq_prompt):
        assert prompt.rstrip().endswith("output nothing else.")
        assert "@startuml" in prompt and "@enduml" in prompt

    assert "level-2 heading" in api_prompt


def test_sequence_prompt_names_entry_point():
    prompt = build_sequence_prompt(make_change_set(), entry_point="UserService.load")
    assert "Entry point to model: UserService.load" in prompt


def test_repair_prompt_contains_diagnostics_and_previous_code():
    diagnostics = [
        Diagnostic("E002", "missing @enduml", line=7, snippet="  +load()"),
        Diagnostic("W001", "class 'Ghost' is not present", line=3, severity="warning"),
    ]
    prompt = build_repair_prompt(diagnostics=diagnostics, previous_code="@startuml\nclass A", kind="class")
    assert "E002" in prompt
    assert "line 7" in prompt.lower()
    assert "+load()" in prompt
    assert "@startuml\nclass A" in prompt
    assert "Fix ONLY" in prompt


def test_warmup_is_two_messages():
    messages = build_warmup(CLASS_SYSTEM_PROMPT)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == WARMUP_USER
    assert WARMUP_REPLY == "OK"
    assert "只回复 OK" in WARMUP_USER


def test_truncation_notice():
    block = build_input_block(make_change_set(), max_chars=10)
    assert "[truncated]" in block


def test_input_block_truncation_keeps_header():
    block = build_input_block(make_change_set(), module="m", max_chars=10)
    assert block.startswith("module: m")


def test_repair_prompt_for_markdown_asks_for_heading_not_plantuml():
    """真实评测暴露的 bug：Markdown 文档进入修复轮时，旧提示词要求它输出 PlantUML，
    结果模型回 @startuml，校验器判 M001，三轮全废。"""
    from app.ai.prompts import build_repair_prompt
    from app.ai.validator import Diagnostic

    prompt = build_repair_prompt(diagnostics=[Diagnostic("M005", "no body")],
                                 previous_code="### x", kind="api")
    assert "Markdown" in prompt
    assert "@startuml" not in prompt
    assert "heading" in prompt.lower()


def test_repair_prompt_for_plantuml_keeps_output_contract():
    from app.ai.prompts import build_repair_prompt
    from app.ai.validator import Diagnostic

    prompt = build_repair_prompt(diagnostics=[Diagnostic("E002", "missing end")],
                                 previous_code="@startuml", kind="class")
    assert "@startuml" in prompt
    assert "PlantUML" in prompt


def test_all_document_prompts_ask_for_simplified_chinese():
    """真实用户要求文档正文中文；提示词必须明说，不能指望模型自己选语言。"""
    for prompt in (CLASS_SYSTEM_PROMPT, SEQUENCE_SYSTEM_PROMPT, API_SYSTEM_PROMPT):
        assert "简体中文" in prompt
        assert "Simplified" in prompt

    for kind in ("class", "sequence", "api"):
        repair = build_repair_prompt(
            diagnostics=[Diagnostic("M005", "no body")], previous_code="x", kind=kind
        )
        assert "简体中文" in repair


# --------------------------------------------------------------------------- #
# v4：提示词必须把"结构事实"当硬约束讲出来，而不只是"请画得详细一点"
# --------------------------------------------------------------------------- #
STRUCTURED_SOURCE = """from abc import ABC, abstractmethod

class UserReader(ABC):
    @abstractmethod
    def find(self, user_id: int) -> dict: ...

class UserRepository(UserReader):
    def __init__(self) -> None:
        self.cache: dict = {}

    def find(self, user_id: int) -> dict:
        if user_id <= 0:
            raise ValueError("bad id")
        for key in self.cache:
            if key == user_id:
                return self.cache[key]
        row = {"id": user_id}
        self.cache[user_id] = row
        return row

class UserService:
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    def load(self, user_id: int) -> dict:
        user = self.repo.find(user_id)
        return self.decorate(user)

    def decorate(self, user: dict) -> dict:
        return user
"""

STRUCTURED_PATH = "app/services/users.py"


def make_structured_change_set():
    extractor = TreeSitterExtractor()
    new = extractor.parse(STRUCTURED_PATH, STRUCTURED_SOURCE.encode())
    change = diff_symbols(STRUCTURED_PATH, extractor.parse(STRUCTURED_PATH, b""), new)
    return ChangeSet(files=[change], skipped_files=[])


def test_few_shot_teaches_relations_not_isolated_classes():
    """旧的 few-shot 只有一条 `A --> B : 使用`，模型照抄就得到一排孤立类框。"""
    from app.ai.prompts import FEW_SHOT_OUTPUT, SEQUENCE_FEW_SHOT_OUTPUT

    assert "--|>" in FEW_SHOT_OUTPUT
    assert "*--" in FEW_SHOT_OUTPUT
    # 时序图的例子必须有返回箭头和分支块，否则模型只学会单向调用
    assert "-->" in SEQUENCE_FEW_SHOT_OUTPUT
    assert "alt " in SEQUENCE_FEW_SHOT_OUTPUT


def test_class_prompt_lists_relations_as_ground_truth():
    from app.parsers.structure import build_structure

    change_set = make_structured_change_set()
    structure = build_structure(change_set, module="app.services")
    prompt = build_class_prompt(change_set, module="app.services", structure=structure)

    assert "relations you MUST draw" in prompt
    assert "UserRepository --|> UserReader" in prompt
    assert "call chains" in prompt


def test_sequence_prompt_carries_call_chain_and_branches():
    from app.parsers.structure import build_structure

    change_set = make_structured_change_set()
    structure = build_structure(change_set, module="app.services")
    prompt = build_sequence_prompt(
        change_set, module="app.services", entry_point="UserService.load", structure=structure
    )

    assert "UserService.load -> UserRepository.find" in prompt
    assert "control flow" in prompt
    assert "if" in prompt


def test_input_block_without_structure_has_no_facts_section():
    block = build_input_block(make_change_set(), module="app.services")
    assert "relations you MUST draw" not in block


def test_sequence_system_prompt_forbids_placeholder_participants():
    from app.ai.prompts import SEQUENCE_SYSTEM_PROMPT

    assert "调用方" in SEQUENCE_SYSTEM_PROMPT
    assert "process" in SEQUENCE_SYSTEM_PROMPT.lower()


def test_sequence_repair_prompt_asks_for_full_redraw():
    prompt = build_repair_prompt(
        diagnostics=[Diagnostic("E017", "no control flow block")],
        previous_code="@startuml\n@enduml",
        kind="sequence",
        regenerate=True,
    )
    assert "redraw" in prompt.lower() or "from scratch" in prompt.lower()
    assert "@startuml" in prompt


def test_class_prompt_explains_module_level_functions():
    """free function 不是类：不许为它编一个以模块命名的假类框。"""
    from app.ai.prompts import CLASS_SYSTEM_PROMPT

    assert "Module-level functions" in CLASS_SYSTEM_PROMPT
    assert "<<function>>" in CLASS_SYSTEM_PROMPT


def test_sequence_prompt_prefers_function_participants():
    """入口是模块级函数时，参与者它就应该是函数名本身。"""
    from app.ai.prompts import SEQUENCE_SYSTEM_PROMPT

    assert "function" in SEQUENCE_SYSTEM_PROMPT.lower()


def test_truncated_sequence_repair_asks_for_a_smaller_diagram():
    """撞到 max_tokens 的图是没尾巴的：重画时要连规模一起收。"""
    prompt = build_repair_prompt(
        diagnostics=[Diagnostic("E001", "missing @startuml")],
        previous_code="@startuml\nA -> B : x",
        kind="sequence",
        regenerate=True,
        too_long=True,
    )
    assert "cut off by the token limit" in prompt
    assert "at most 5 participants" in prompt

    plain = build_repair_prompt(
        diagnostics=[Diagnostic("E016", "missing call edge")],
        previous_code="@startuml\n@enduml",
        kind="sequence",
        regenerate=True,
    )
    assert "at most 5 participants" not in plain
