"""真实模型批量评测：同一份真实 diff 反复生成，统计首次通过率、修复轮数与 token 成本。

为什么单独写这个脚本，而不是复用 demo_phase4.py：
    demo 用的是"剧本模型 + 固定夹具"，目的是演示流程；
    这里要的是**可统计的量**：每份文档 attempts 分布、ok/needs_review、token 数、耗时。
    所以它必须落 JSON，并且能重复跑 N 次。

用法：
    .\.venv\Scripts\python.exe scripts\live_eval.py --runs 3
    .\.venv\Scripts\python.exe scripts\live_eval.py --runs 1 --only class_diagram

产物：
    var/live/report.json      追加式记录（每次运行一条）
    var/live/<run>/<doc>.puml|.md   原始产物，供 plantuml -checkonly 与人工审阅
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.ai.client import DeepSeekClient, RetryPolicy  # noqa: E402
from app.ai.service import DocumentationService  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.parsers import TreeSitterExtractor, diff_symbols  # noqa: E402
from app.parsers.service import ChangeSet  # noqa: E402
from app.pipeline import parse_changes  # noqa: E402

OUT_DIR = BASE_DIR / "var" / "live"
PATH = "app/domain.py"

OLD = '''from dataclasses import dataclass
from datetime import datetime


class DomainError(Exception):
    """领域层异常基类。"""


@dataclass(frozen=True)
class User:
    id: int
    email: str
    created_at: datetime


class UserRepository:
    def find_by_id(self, user_id: int) -> User | None:
        raise NotImplementedError

    def save(self, user: User) -> None:
        raise NotImplementedError


class UserService:
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    def load(self, user_id: int) -> User:
        user = self.repo.find_by_id(user_id)
        if user is None:
            raise DomainError("not found")
        return user
'''

NEW = '''from dataclasses import dataclass
from datetime import datetime


class DomainError(Exception):
    """领域层异常基类。"""


class UserNotFound(DomainError):
    """按 id 找不到用户。"""


@dataclass(frozen=True)
class User:
    id: int
    email: str
    created_at: datetime

    def is_active(self) -> bool:
        return bool(self.email)


class UserRepository:
    def find_by_id(self, user_id: int) -> User | None:
        raise NotImplementedError

    def save(self, user: User) -> None:
        raise NotImplementedError

    def delete(self, user_id: int) -> None:
        raise NotImplementedError


class UserService:
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    def load(self, user_id: int) -> User:
        user = self.repo.find_by_id(user_id)
        if user is None:
            raise UserNotFound(f"user {user_id} not found")
        return user

    def deactivate(self, user_id: int) -> None:
        user = self.load(user_id)
        self.repo.save(User(id=user.id, email="", created_at=user.created_at))

    def remove(self, user_id: int) -> None:
        self.load(user_id)
        self.repo.delete(user_id)
'''


def build_repo(root: Path) -> tuple[Path, str, str]:
    """在本机造一个两提交的 git 仓库，返回 (repo, base_sha, head_sha)。

    不用 clone 真实仓库：容器/宿主对 github 的连通性都不稳定，
    而这里要测的是"模型在这一份真实 AST 上的表现"，源码内容可控更重要。
    """
    import shutil

    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    work = root / "work"
    (work / "app").mkdir(parents=True)

    env = {
        "GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@example.com",
        "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@example.com",
    }

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=work, capture_output=True, encoding="utf-8", errors="replace",
            env={**__import__("os").environ, **env},
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip()

    git("init", "-q", "-b", "main")
    (work / PATH).write_text(OLD, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")

    (work / PATH).write_text(NEW, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "feature")
    head = git("rev-parse", "HEAD")
    return work, base, head


def read_at(repo: Path, sha: str, path: str) -> str:
    # 注意：不能用 text=True —— Windows 下默认按 GBK 解码，源码里的中文注释会直接抛
    # UnicodeDecodeError（而且是在 reader 线程里抛，主线程只看到 stdout=None）。
    result = subprocess.run(
        ["git", "show", f"{sha}:{path}"], cwd=repo, capture_output=True
    )
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""


def build_change_set(repo: Path, base: str, head: str) -> ChangeSet:
    extractor = TreeSitterExtractor()
    old_symbols = extractor.parse(PATH, read_at(repo, base, PATH).encode("utf-8"))
    new_symbols = extractor.parse(PATH, read_at(repo, head, PATH).encode("utf-8"))
    file_diff = diff_symbols(PATH, old_symbols, new_symbols)
    return ChangeSet(files=[file_diff], skipped_files=[])


def main() -> int:
    parser = argparse.ArgumentParser(description="真实模型批量评测")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()

    settings = get_settings()
    if not settings.llm_api_key:
        print("LLM_API_KEY 未配置：先写 .env", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    repo, base, head = build_repo(out_dir / "repo")
    change_set = build_change_set(repo, base, head)
    summary = change_set.to_summary()
    print(f"diff: {summary['files_analyzed']} file(s), redoc_targets={summary['redoc_targets']}")
    print(f"model: {settings.llm_model} @ {settings.llm_base_url}")

    client = DeepSeekClient(
        settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        max_tokens=settings.llm_max_tokens,
        policy=RetryPolicy(max_retries=settings.llm_max_retries),
    )
    service = DocumentationService(
        client, max_attempts=settings.self_correct_max_attempts, model_name=settings.llm_model
    )

    report_path = out_dir / "report.json"
    history = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else []

    for run_index in range(1, args.runs + 1):
        started = time.perf_counter()
        run_dir = out_dir / f"run{len(history) + 1:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        docs = service.generate_all(change_set)
        batch = {
            "run": len(history) + 1,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": settings.llm_model,
            "prompt_version": "v3-2025-11",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "change_set": summary,
            "docs": [],
        }
        for module in docs:
            for draft in module.drafts:
                ext = draft.extension if draft.extension.startswith(".") else "." + draft.extension
                path = run_dir / f"{draft.doc_type.value}{ext}"
                path.write_text(draft.content, encoding="utf-8")
                errors = [d for d in draft.diagnostics if d.severity == "error"]
                warnings = [d for d in draft.diagnostics if d.severity == "warning"]
                batch["docs"].append({
                    "doc_type": draft.doc_type.value,
                    "ok": draft.ok,
                    "needs_review": draft.needs_review,
                    "attempts": draft.attempts,
                    "tokens": draft.tokens,
                    "latency_ms": draft.latency_ms,
                    "errors": [d.code for d in errors],
                    "warnings": [d.code for d in warnings],
                    "chars": len(draft.content),
                    "file": str(path.relative_to(BASE_DIR)),
                })
                flag = "OK " if draft.ok else "BAD"
                print(f"  [{flag}] {draft.doc_type.value:<16} attempts={draft.attempts} "
                      f"tokens={draft.tokens:<5} {draft.latency_ms:>5}ms "
                      f"errors={[d.code for d in errors]} warnings={[d.code for d in warnings]}")

        history.append(batch)
        report_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        ok_count = sum(1 for d in batch["docs"] if d["ok"])
        print(f"run {batch['run']}: {ok_count}/{len(batch['docs'])} ok, "
              f"tokens={sum(d['tokens'] for d in batch['docs'])}, {batch['duration_ms']}ms")

    all_docs = [d for batch in history for d in batch["docs"]]
    first_pass = sum(1 for d in all_docs if d["attempts"] == 1)
    print("\n=== 累计统计 ===")
    print(f"文档数={len(all_docs)} 首次通过={first_pass} "
          f"一次通过率={first_pass / max(len(all_docs), 1):.0%} "
          f"修复轮总数={sum(max(d['attempts'] - 1, 0) for d in all_docs)} "
          f"token总数={sum(d['tokens'] for d in all_docs)} "
          f"失败={sum(1 for d in all_docs if not d['ok'])}")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
