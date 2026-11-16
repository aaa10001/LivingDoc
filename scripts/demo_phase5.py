"""阶段五离线演示：真实 git 仓库 + 真实 tree-sitter + 剧本模型 + 真实 SQLite。

演示四件事：
1. 一次真实提交如何变成三份文档并落库（documents + document_revisions）；
2. 同样内容重复发布不产生新版本；
3. 人改过的文档，AI 只能写"候选"（status=stale），current_revision 不动；
4. accept_ai 之后人才把候选变成正式版本。

本机没有 LLM API key（见 docs/04-phase4-generation.md 第 7 节），模型响应是固定
剧本。这个脚本证明"链路与落库正确"，不证明"模型输出质量"。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.client import ScriptedLLM
from app.git_handler import diff_commits, read_blob
from app.pipeline import generate_documents, parse_changes, publish_drafts
from app.publish import accept_ai, get_document, list_documents, mark_manual_edit
from app.store import ensure_project, get_run, open_store, start_run, transition_run

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "var" / "phase5"

USER_PY = '''class UserRepository:
    def get(self, uid):
        return {"id": uid}

    def remove(self, uid):
        return True


class UserService:
    def __init__(self, repo):
        self.repo = repo

    def load(self, uid):
        return self.repo.get(uid)
'''

USER_PY_V2 = '''class UserRepository:
    def get(self, uid):
        return {"id": uid}

    def remove(self, uid):
        return True


class UserService:
    def __init__(self, repo):
        self.repo = repo

    def load(self, uid):
        if uid is None:
            raise ValueError("uid must not be None")
        return self.repo.get(uid)

    def delete(self, uid):
        return self.repo.remove(uid)
'''

CLASS_PUML = """@startuml
class UserService {
  +load(uid)
  +delete(uid)
}
class UserRepository {
  +get(uid)
  +remove(uid)
}
UserService --> UserRepository
@enduml
"""

CLASS_PUML_V2 = """@startuml
class UserService {
  +load(uid)
  +delete(uid)
  +count()
}
class UserRepository {
  +get(uid)
  +remove(uid)
}
UserService --> UserRepository
@enduml
"""

API_MD = """# app.services API

## UserService.load(uid)

返回用户；uid 为 None 时抛 ValueError。

## UserService.delete(uid)

删除用户并返回布尔值。
"""

SEQUENCE_PUML = """@startuml
actor Client
participant UserService
participant UserRepository
Client -> UserService: load(uid)
UserService -> UserRepository: get(uid)
UserRepository --> UserService: user
UserService --> Client: user
@enduml
"""


def force_remove(path: Path) -> None:
    """Windows 上 git 对象是只读的，rmtree 会因 WinError 5 中断。"""

    def onerror(func, target, _exc):
        import os
        import stat

        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=onerror)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=LivingDoc", "-c", "user.email=livingdoc@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def build_repo(root: Path) -> tuple[Path, str, str]:
    """造一个两提交的仓库：第一次建文件，第二次改 UserService。"""
    repo = root / "demo-repo"
    (repo / "app" / "services").mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "app" / "services" / "user.py").write_text(USER_PY, encoding="utf-8")
    (repo / "app" / "services" / "__init__.py").write_text("", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial user service")
    base = git(repo, "rev-parse", "HEAD")

    (repo / "app" / "services" / "user.py").write_text(USER_PY_V2, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "add delete and validate load")
    head = git(repo, "rev-parse", "HEAD")
    return repo, base, head


def print_table(store, docs: list[dict]) -> None:
    print(f'{"doc_type":<18}{"target_key":<22}{"status":<10}{"version":<8}content')
    for row in docs:
        doc = get_document(store, row["id"])
        current = doc.get("current") or {}
        content = (current.get("content") or "").replace("\n", " ")[:44]
        print(
            f'{doc["doc_type"]:<18}{doc["target_key"]:<22}{doc["status"]:<10}'
            f'{current.get("version", "-"):<8}{content!r}'
        )


def main() -> int:
    if OUT.exists():
        force_remove(OUT)
    OUT.mkdir(parents=True)

    repo, base_sha, head_sha = build_repo(OUT)
    print(f"repo   {repo}")
    print(f"base   {base_sha[:12]}")
    print(f"head   {head_sha[:12]}")
    print()

    changes = diff_commits(repo, base_sha, head_sha)
    print(f"git diff: {len(changes)} file(s)")
    for change in changes:
        print(f"  {change.status} {change.path}  +{change.additions} -{change.deletions}")

    change_set = parse_changes(
        files=changes,
        read_blob=lambda which, path: read_blob(repo, base_sha if which == "base" else head_sha, path),
    )
    summary = change_set.to_summary()
    print()
    print("tree-sitter: " + json.dumps(summary, ensure_ascii=False))
    print()

    db_path = OUT / "livingdoc.sqlite"
    store = open_store(f"sqlite:///{db_path.as_posix()}")
    project_id = ensure_project(store, provider="github", repo_full_name="octocat/demo-service")
    run_id, created = start_run(
        store, project_id=project_id, base_sha=base_sha, head_sha=head_sha, trigger_ref="refs/heads/main"
    )
    for status in ("cloning", "diffing", "parsing"):
        transition_run(store, run_id, status)

    docs = generate_documents(
        change_set=change_set,
        client=ScriptedLLM([CLASS_PUML, API_MD, SEQUENCE_PUML]),
        model_name="scripted-demo",
    )
    transition_run(store, run_id, "analyzing")
    transition_run(store, run_id, "publishing")
    report = publish_drafts(store, project_id=project_id, run_id=run_id, docs=docs)
    transition_run(store, run_id, "succeeded", files_changed=len(changes), entities_changed=summary["symbols_added"] + summary["symbols_modified"])

    print("第一次发布：")
    print("  " + json.dumps(report.summary(), ensure_ascii=False))
    for outcome in report.outcomes:
        print(f'  {outcome.action:<15}{outcome.status:<10}v{outcome.version or "-"}  {outcome.document_id[:8]}')
    print()
    print_table(store, list_documents(store, project_id))
    print()

    # 第二次：内容完全相同，应当 unchanged，不产生新版本
    docs_again = generate_documents(
        change_set=change_set,
        client=ScriptedLLM([CLASS_PUML, API_MD, SEQUENCE_PUML]),
        model_name="scripted-demo",
    )
    second = publish_drafts(store, project_id=project_id, run_id=run_id, docs=docs_again)
    print("第二次发布（内容未变）：")
    print("  " + json.dumps(second.summary(), ensure_ascii=False))
    print()

    # 人改文档：模拟工程师在评审里补了一句说明
    class_doc = next(d for d in list_documents(store, project_id) if d["doc_type"] == "class_diagram")
    human = mark_manual_edit(
        store,
        class_doc["id"],
        content=CLASS_PUML.replace("UserService --> UserRepository", "UserService --> UserRepository : uses"),
        summary="人工补充依赖说明",
    )
    print(f'人工编辑：{human.action} v{human.version}，manual_edited={human.manual_edited}')
    print()

    # 第三次：AI 产出新版本 -> 只能成为候选
    docs_third = generate_documents(
        change_set=change_set,
        client=ScriptedLLM([CLASS_PUML_V2, API_MD, SEQUENCE_PUML]),
        model_name="scripted-demo",
    )
    third = publish_drafts(store, project_id=project_id, run_id=run_id, docs=docs_third)
    print("第三次发布（文档已被人改过）：")
    for outcome in third.outcomes:
        print(f'  {outcome.action:<15}{outcome.status:<10}v{outcome.version or "-"}  {outcome.document_id[:8]}')
    doc = get_document(store, class_doc["id"])
    print(f'  current   = v{doc["current"]["version"]} ({doc["current"]["created_by"]})')
    print(f'  latest    = v{doc["latest_revision"]["version"]} ({doc["latest_revision"]["created_by"]})')
    print(f'  status    = {doc["status"]}')
    print()

    accepted = accept_ai(store, class_doc["id"])
    doc = get_document(store, class_doc["id"])
    print(f'接受 AI 候选：{accepted.action} -> current = v{doc["current"]["version"]} ({doc["current"]["created_by"]})')
    print(f'  manual_edited 仍为 {doc["manual_edited"]}（接受一次不等于解除保护）')
    print()

    run = get_run(store, run_id)
    print(f'run {run_id[:8]}: {run["status"]}  files={run["files_changed"]}  entities={run["entities_changed"]}')

    result = {
        "repo": str(repo),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "diff": summary,
        "run": {"id": run_id, "status": run["status"], "created": created},
        "first_publish": report.summary(),
        "second_publish": second.summary(),
        "third_publish": third.summary(),
        "documents": [
            {
                "doc_type": d["doc_type"],
                "target_key": d["target_key"],
                "status": d["status"],
                "current_version": (d.get("current") or {}).get("version"),
                "manual_edited": d["manual_edited"],
            }
            for d in list_documents(store, project_id)
        ],
    }
    (OUT / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"report -> {OUT / 'report.json'}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
