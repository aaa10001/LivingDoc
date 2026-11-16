"""编排层测试：一次变更应该稳定地产出三份草稿，并把失败隔离在单份文档里。"""

import pytest

from app.ai.client import LLMError, ScriptedLLM
from app.ai.service import (
    DocType,
    DocumentDraft,
    DocumentationService,
    group_by_module,
    module_key_for,
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

CLASS_PUML = """@startuml
class UserService {
  +load(user_id: int): dict
}
@enduml"""

SEQ_PUML = """@startuml
UserService -> UserRepository: find_by_id
UserRepository --> UserService: user
@enduml"""

API_MD = """## app.services

### UserService.load

- signature: def load(self, user_id: int) -> dict
- returns: the user record

Loads a user record from the repository. Raises ValueError when the id is missing
or the record cannot be decoded, so callers can treat both cases the same way.
"""


def make_change_set():
    extractor = TreeSitterExtractor()
    return ChangeSet(
        files=[diff_symbols(PATH, extractor.parse(PATH, OLD.encode()), extractor.parse(PATH, NEW.encode()))],
        skipped_files=[],
    )


def test_module_key_for():
    assert module_key_for("app/services/user.py") == "app.services"
    assert module_key_for("main.py") == "(root)"
    assert module_key_for("/abs/path/to/mod.py") == "abs.path.to"


def test_generates_three_documents():
    fake = ScriptedLLM([CLASS_PUML, API_MD, SEQ_PUML])
    docs = DocumentationService(fake, model_name="scripted").generate_module(
        make_change_set(), module="app.services"
    )

    types = [d.doc_type for d in docs.drafts]
    assert types == [DocType.CLASS_DIAGRAM, DocType.API_DOC, DocType.SEQUENCE_DIAGRAM]
    # 顺序与 API 调用顺序一致，测试替身才对得上
    assert [d.ok for d in docs.drafts] == [True, True, True]

    assert docs.target_key == "module:app.services"
    assert docs.all_ok is True
    assert docs.needs_review is False
    assert docs.summary["docs_ok"] == 3
    assert docs.summary["repair_rounds"] == 0
    assert docs.by_type(DocType.CLASS_DIAGRAM).extension == "puml"
    assert docs.by_type(DocType.API_DOC).extension == "md"


def test_removed_only_change_skips_sequence_diagram():
    fake = ScriptedLLM([CLASS_PUML, API_MD])
    extractor = TreeSitterExtractor()
    base = NEW
    with_extra = NEW + "\n    def delete(self, user_id):\n        return True\n"
    removed = ChangeSet(
        files=[
            diff_symbols(PATH, extractor.parse(PATH, with_extra.encode()), extractor.parse(PATH, base.encode()))
        ],
        skipped_files=[],
    )
    docs = DocumentationService(fake).generate_module(removed, module="app.services")

    assert [d.doc_type for d in docs.drafts] == [DocType.CLASS_DIAGRAM, DocType.API_DOC]
    assert len(fake.calls) == 2


def test_generation_failure_is_isolated_per_document():
    fake = ScriptedLLM([LLMError("HTTP 401", status_code=401), API_MD, SEQ_PUML])
    docs = DocumentationService(fake).generate_module(make_change_set(), module="app.services")

    class_draft = docs.by_type(DocType.CLASS_DIAGRAM)
    assert class_draft.ok is False
    assert class_draft.needs_review is True
    assert "X001" in {d.code for d in class_draft.diagnostics}

    # 其它两份不受影响，模块级 all_ok 为假，但不需要整体重跑
    assert docs.by_type(DocType.API_DOC).ok is True
    assert docs.all_ok is False
    assert docs.needs_review is True


def test_group_by_module_splits_change_set():
    extractor = TreeSitterExtractor()
    a = diff_symbols(
        "app/services/user.py",
        extractor.parse("app/services/user.py", OLD.encode()),
        extractor.parse("app/services/user.py", NEW.encode()),
    )
    b = diff_symbols(
        "app/api/routes.py",
        extractor.parse("app/api/routes.py", OLD.encode()),
        extractor.parse("app/api/routes.py", NEW.encode()),
    )
    grouped = group_by_module(ChangeSet(files=[a, b], skipped_files=[]))

    assert set(grouped) == {"app.services", "app.api"}
    assert all(len(bucket.files) == 1 for bucket in grouped.values())


def test_empty_change_set_is_not_an_error():
    docs = DocumentationService(ScriptedLLM([])).generate_module(ChangeSet(files=[], skipped_files=[]))
    assert docs.drafts == ()
    assert docs.summary["reason"] == "no changed files"


def test_generate_all_covers_every_module():
    extractor = TreeSitterExtractor()
    a = diff_symbols(
        "app/services/user.py",
        extractor.parse("app/services/user.py", OLD.encode()),
        extractor.parse("app/services/user.py", NEW.encode()),
    )
    fake = ScriptedLLM([CLASS_PUML, API_MD, SEQ_PUML])
    results = DocumentationService(fake).generate_all(ChangeSet(files=[a], skipped_files=[]))
    assert len(results) == 1
    assert results[0].module == "app.services"


def test_formatting_only_change_does_not_rewrite_documents():
    """真实 GitHub 测试：纯注释/换行提交曾生成一堆 'not documented in code'。"""
    extractor = TreeSitterExtractor()
    before = "class UserService:\n    def load(self, user_id):\n        return None\n"
    after = (
        "class UserService:\n"
        "    def load(self, user_id):\n"
        "        # 只加了一行注释\n"
        "        return None\n"
    )
    change = diff_symbols(
        "app/services/user.py",
        extractor.parse("app/services/user.py", before.encode()),
        extractor.parse("app/services/user.py", after.encode()),
    )
    assert change.changes[0].needs_redoc is False

    docs = DocumentationService(ScriptedLLM([])).generate_module(
        ChangeSet(files=[change], skipped_files=[])
    )

    assert docs.drafts == ()
    assert docs.summary["reason"] == "no semantic changes"


def test_validation_errors_are_not_hidden_by_warnings():
    """真实 GitHub 测试的误导现场：日志里只有 W005，看不到拒稿的 M005。"""
    draft = DocumentDraft(
        doc_type=DocType.API_DOC,
        target_key="module:src.requests",
        title="src.requests API reference",
        content="## src.requests\n",
        ok=False,
        needs_review=True,
        diagnostics=(
            Diagnostic("M005", "document has no body"),
            Diagnostic("W005", "changed function 'Foo.bar' is not documented", severity="warning"),
        ),
    )

    assert "M005" not in draft.notes()
    assert "M005" in draft.validation_errors()
    assert "W005" not in draft.validation_errors()

def test_chain_edges_compare_participant_names_not_qualified_names():
    """图里写的是 `UserService -> UserService`，比对也必须按参与者名。"""
    from app.ai.service import _split_chain_edges

    class FakeStructure:
        def chain_for(self, entry):
            assert entry == "UserService.deactivate"
            return (
                "UserService.deactivate -> UserService.load",
                "UserService.load -> UserRepository.find",
                "UserRepository.find -> TTLCache.get",
            )

    required, advisory = _split_chain_edges(FakeStructure(), "UserService.deactivate")
    assert ("UserService", "UserService") in required
    assert ("UserService", "UserRepository") in required
    assert ("UserRepository", "TTLCache") in advisory
    assert not required & advisory
