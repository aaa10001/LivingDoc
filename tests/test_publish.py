"""发布层测试：ADR-006 的人工修改保护是这里的核心。

判断标准只有一条：AI 的自动更新有没有可能覆盖人改过的内容。
下面每个用例都是这句话的一个反例场景。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.publish import (
    PublishError,
    accept_ai,
    get_document,
    list_documents,
    mark_manual_edit,
    normalize_content,
    publish_document,
)
from app.store import (
    document_revisions,
    documents,
    ensure_project,
    open_store,
)


@pytest.fixture()
def store(tmp_path):
    s = open_store(f"sqlite:///{(tmp_path / 'livingdoc.sqlite').as_posix()}")
    yield s
    s.close()


@pytest.fixture()
def project_id(store):
    return ensure_project(store, provider="github", repo_full_name="octocat/Hello-World")


PUML_V1 = "@startuml\nclass A\n@enduml\n"
PUML_V2 = "@startuml\nclass A\nclass B\n@enduml\n"


def _publish(store, project_id, content=PUML_V1, **kwargs):
    return publish_document(
        store,
        project_id=project_id,
        doc_type="class_diagram",
        target_key="module:app.services",
        title="app.services 类图",
        content=content,
        run_id=kwargs.pop("run_id", None),
        extension=".puml",
        **kwargs,
    )


def _versions(store, document_id):
    with store.begin() as conn:
        rows = conn.execute(
            select(document_revisions)
            .where(document_revisions.c.document_id == document_id)
            .order_by(document_revisions.c.version)
        ).mappings().all()
    return [dict(row) for row in rows]


def test_create_then_unchanged_does_not_bump_version(store, project_id):
    first = _publish(store, project_id)
    second = _publish(store, project_id)

    assert first.action == "created"
    assert first.version == 1
    assert first.status == "published"
    assert second.action == "unchanged"
    assert second.version == 1
    assert len(_versions(store, first.document_id)) == 1


def test_crlf_and_missing_trailing_newline_are_not_changes(store, project_id):
    _publish(store, project_id, "@startuml\r\nclass A\r\n@enduml")
    again = _publish(store, project_id, "@startuml\nclass A\n@enduml\n\n")
    assert again.action == "unchanged"
    assert normalize_content("a\r\nb\r\n\r\n") == "a\nb\n"


def test_changed_content_moves_current_pointer(store, project_id):
    first = _publish(store, project_id)
    second = _publish(store, project_id, PUML_V2)

    assert second.action == "updated"
    assert second.version == 2
    doc = get_document(store, first.document_id)
    assert doc["current_revision_id"] == second.revision_id
    assert doc["current"]["content"] == PUML_V2


def test_render_failure_is_saved_as_draft_not_published(store, project_id):
    outcome = _publish(store, project_id, "@startuml\nclass A\n", render_ok=False)
    assert outcome.status == "draft"
    revisions = _versions(store, outcome.document_id)
    assert revisions[0]["render_ok"] is False
    assert revisions[0]["render_error"] == "validation failed"


def test_manual_edit_defers_ai_update(store, project_id):
    created = _publish(store, project_id)
    edited = mark_manual_edit(store, created.document_id, content="@startuml\nnote \"human\"\n@enduml\n")
    assert edited.action == "human_edit"
    assert edited.version == 2

    deferred = _publish(store, project_id, PUML_V2)
    assert deferred.action == "deferred_stale"
    assert deferred.deferred is True
    assert deferred.version == 3
    assert deferred.manual_edited is True

    doc = get_document(store, created.document_id)
    assert doc["status"] == "stale"
    # 关键断言：current 仍然指着人改的那一版
    assert doc["current_revision_id"] == edited.revision_id
    assert "human" in doc["current"]["content"]
    # AI 的候选版本确实写下来了，只是没生效
    assert doc["latest_revision"]["version"] == 3
    assert doc["latest_revision"]["created_by"] == "ai"


def test_manual_doc_does_not_pile_identical_candidates(store, project_id):
    created = _publish(store, project_id)
    mark_manual_edit(store, created.document_id, content="human version")
    _publish(store, project_id, PUML_V2)
    again = _publish(store, project_id, PUML_V2)

    assert again.action == "unchanged"
    assert len(_versions(store, created.document_id)) == 3  # v1 ai + v2 human + v3 ai


def test_accept_ai_switches_current_but_keeps_protection(store, project_id):
    created = _publish(store, project_id)
    mark_manual_edit(store, created.document_id, content="@startuml\nnote \"human\"\n@enduml\n")
    deferred = _publish(store, project_id, PUML_V2)

    accepted = accept_ai(store, created.document_id)
    assert accepted.action == "accepted"
    assert accepted.version == deferred.version

    doc = get_document(store, created.document_id)
    assert doc["current_revision_id"] == deferred.revision_id
    assert doc["status"] == "published"
    # 接受 AI 是人做的一次动作，不是解除保护：下次 AI 更新还要人再看
    assert doc["manual_edited"] is True

    assert accept_ai(store, created.document_id).action == "unchanged"


def test_accept_ai_without_ai_revision_raises(store, project_id):
    created = _publish(store, project_id)
    mark_manual_edit(store, created.document_id, content="human only")
    # 人改的版本是 latest，且没有更新的 AI 候选
    with pytest.raises(PublishError):
        accept_ai(store, created.document_id)


def test_flag_without_content_keeps_current(store, project_id):
    created = _publish(store, project_id)
    flagged = mark_manual_edit(store, created.document_id)
    assert flagged.action == "flagged"
    doc = get_document(store, created.document_id)
    assert doc["manual_edited"] is True
    assert doc["current_revision_id"] == created.revision_id


def test_extension_maps_to_content_format(store, project_id):
    _publish(store, project_id)
    md = publish_document(
        store, project_id=project_id, doc_type="api_doc", target_key="module:app.services",
        title="API", content="# API\n", extension=".md",
    )
    doc = get_document(store, md.document_id)
    assert doc["content_format"] == "markdown"


def test_list_documents_is_sorted_and_filterable(store, project_id):
    publish_document(store, project_id=project_id, doc_type="class_diagram",
                     target_key="module:b", title="b", content="x", extension=".puml")
    publish_document(store, project_id=project_id, doc_type="api_doc",
                     target_key="module:a", title="a", content="# a\n", extension=".md")
    publish_document(store, project_id=project_id, doc_type="class_diagram",
                     target_key="module:a", title="a", content="y", extension=".puml")

    all_docs = list_documents(store, project_id)
    assert [d["target_key"] for d in all_docs] == ["module:a", "module:a", "module:b"]
    class_only = list_documents(store, project_id, doc_type="class_diagram")
    assert {d["doc_type"] for d in class_only} == {"class_diagram"}
    assert len(class_only) == 2


def test_missing_document_raises(store):
    with pytest.raises(PublishError):
        accept_ai(store, "no-such-id")
