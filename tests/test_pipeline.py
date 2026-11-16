"""流水线粘合层：解析、artifact 序列化、生成到落库的全链路。

这里最值钱的两个用例是 test_change_set_roundtrip（stage 之间的契约）
和 test_end_to_end_with_scripted_model（不需要网络也能跑完 S3->S6）。
"""

from __future__ import annotations

import json

import pytest

from app.ai.client import ScriptedLLM
from app.ai.service import DocType, DocumentDraft, ModuleDocumentation
from app.ai.validator import Diagnostic
from app.pipeline import (
    PublishReport,
    dump_change_set,
    generate_documents,
    load_change_set,
    parse_changes,
    publish_drafts,
    read_change_set,
    write_change_set,
)
from app.store import ensure_project, open_store

OLD_SOURCE = '''class UserService:
    def __init__(self, repo):
        self.repo = repo

    def load(self, uid):
        return self.repo.get(uid)


def entry_point():
    return 1
'''

NEW_SOURCE = '''class UserService:
    def __init__(self, repo):
        self.repo = repo

    def load(self, uid):
        if uid is None:
            raise ValueError("uid")
        return self.repo.get(uid)

    def delete(self, uid):
        self.repo.remove(uid)


def entry_point():
    return 2
'''

DELETED_SOURCE = '''def legacy_handler(payload):
    return len(payload)
'''

PATH = "app/services/user.py"


def _reader(mapping):
    def _read(which, path):
        return mapping[(which, path)]
    return _read


@pytest.fixture()
def change_set():
    mapping = {
        ("base", PATH): OLD_SOURCE,
        ("head", PATH): NEW_SOURCE,
        ("base", "app/legacy.py"): DELETED_SOURCE,
    }
    return parse_changes(
        files=[
            {"path": PATH, "status": "M"},
            {"path": "app/legacy.py", "status": "D"},
            {"path": "README.md", "status": "M"},
            {"path": "assets/logo.png", "status": "M"},
        ],
        read_blob=_reader(mapping),
    )


def test_parse_changes_detects_modified_added_removed(change_set):
    assert [f.path for f in change_set.files] == ["app/legacy.py", PATH]
    changes = {f.path: {c.kind.value for c in f.changes} for f in change_set.files}
    assert "removed" in changes["app/legacy.py"]
    assert {"modified", "added"} <= changes[PATH]

    summary = change_set.to_summary()
    assert summary["files_analyzed"] == 2
    assert summary["symbols_added"] >= 1
    assert summary["symbols_modified"] >= 1
    assert summary["redoc_targets"] >= 2


def test_unsupported_files_are_reported_not_silently_dropped(change_set):
    reasons = dict(change_set.skipped_files)
    assert reasons["README.md"] == "unsupported language"
    assert reasons["assets/logo.png"] == "unsupported language"


def test_read_failure_skips_file_with_reason():
    def boom(which, path):
        raise RuntimeError("git show failed")

    cs = parse_changes(files=[{"path": PATH, "status": "M"}], read_blob=boom)
    assert cs.files == []
    assert "read failed" in cs.skipped_files[0][1]


def test_oversized_file_is_skipped():
    big = "x = 1\n" * 400_000  # ~2.8MB，超过 MAX_BLOB_BYTES
    cs = parse_changes(
        files=[{"path": PATH, "status": "M"}],
        read_blob=_reader({("base", PATH): "x = 1\n", ("head", PATH): big}),
    )
    assert cs.skipped_files[0][1] == "file too large"


def test_change_set_roundtrip_preserves_everything_needed_by_prompts(change_set):
    payload = json.loads(json.dumps(dump_change_set(change_set), ensure_ascii=False))
    restored = load_change_set(payload)

    assert restored.to_summary() == change_set.to_summary()
    original = change_set.files[1].changes[0]
    restored_first = restored.files[1].changes[0]
    assert restored_first.kind is original.kind
    assert restored_first.new.body == original.new.body
    assert restored_first.new.signature == original.new.signature
    assert restored_first.new.params == original.new.params


def test_write_and_read_change_set(tmp_path, change_set):
    path = write_change_set(tmp_path / "run" / "x.changes.json", change_set)
    assert path.exists()
    assert read_change_set(path).to_summary() == change_set.to_summary()


def test_unknown_artifact_version_is_rejected():
    with pytest.raises(ValueError):
        load_change_set({"version": 99, "files": []})


def test_empty_change_set_produces_no_documents():
    docs = generate_documents(
        change_set=parse_changes(files=[], read_blob=lambda w, p: ""),
        client=ScriptedLLM([]),
    )
    assert docs == ()


def _draft(doc_type: DocType, ok: bool, content: str = "@startuml\nclass A\n@enduml\n") -> DocumentDraft:
    diagnostics = () if ok else (Diagnostic("E001", "missing @startuml", line=1),)
    return DocumentDraft(
        doc_type=doc_type, target_key="module:app.services", title="t",
        content=content, ok=ok, needs_review=False, diagnostics=diagnostics,
        attempts=1, model="scripted", tokens=10, latency_ms=5,
    )


def test_publish_drafts_rejects_invalid_and_publishes_valid(tmp_path):
    store = open_store(f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    project_id = ensure_project(store, provider="github", repo_full_name="octocat/Hello-World")
    docs = (ModuleDocumentation(
        module="app.services", target_key="module:app.services",
        drafts=(_draft(DocType.CLASS_DIAGRAM, True), _draft(DocType.API_DOC, False, "# x")),
        summary={"module": "app.services"},
    ))

    report = publish_drafts(store, project_id=project_id, run_id=None, docs=(docs,))

    assert isinstance(report, PublishReport)
    assert len(report.published) == 1
    assert report.rejected[0][0] == "api_doc:module:app.services"
    assert "E001" in report.rejected[0][1]
    assert report.summary()["documents"] == 1
    store.close()


CLASS_PUML = """@startuml
class UserService {
  +load(uid)
  +delete(uid)
}
@enduml
"""

API_MD = """## app.services

### UserService.load(uid)

Returns the user record, or raises ValueError when the id is unknown.
The error path is deliberate: callers should handle a missing user the
same way whether the id never existed or the record was deleted.

### UserService.delete(uid)

Removes the user from the repository. It is intentionally idempotent:
deleting an id that is already gone is not treated as an error.

### entry_point()

Returns a small integer used by the module's smoke test. The value is
not meaningful outside the test harness and carries no business logic.
"""

SEQUENCE_PUML = """@startuml
actor Client
participant UserService
Client -> UserService: load(uid)
alt uid 为空
  UserService --> Client: ValueError
else 正常
  UserService --> Client: user
end
@enduml
"""


def test_end_to_end_generation_and_publication(tmp_path):
    """S3 -> S6：真实 AST -> 剧本模型 -> 落库，全程不联网。

    只放一个模块，因为每个模块固定生成三类文档；模块多了剧本就要按倍数写，
    测试会变成"数调用次数"，反而看不出业务含义。
    """
    single = parse_changes(
        files=[{"path": PATH, "status": "M"}],
        read_blob=_reader({("base", PATH): OLD_SOURCE, ("head", PATH): NEW_SOURCE}),
    )
    store = open_store(f"sqlite:///{(tmp_path / 'e2e.sqlite').as_posix()}")
    project_id = ensure_project(store, provider="github", repo_full_name="octocat/Hello-World")

    llm = ScriptedLLM([CLASS_PUML, API_MD, SEQUENCE_PUML])
    docs = generate_documents(change_set=single, client=llm, model_name="scripted")
    assert len(docs) == 1
    assert [d.doc_type.value for d in docs[0].drafts] == [
        "class_diagram", "api_doc", "sequence_diagram"
    ]

    report = publish_drafts(store, project_id=project_id, run_id=None, docs=docs)
    assert report.summary()["published"] == 3
    assert all(o.version == 1 for o in report.published)
    assert {o.action for o in report.outcomes} == {"created"}
    store.close()
