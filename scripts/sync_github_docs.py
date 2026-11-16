#!/usr/bin/env python
"""把数据库里“当前生效”的文档重新同步到 GitHub 仓库。

设计目的：生成文档要花 token，网络抖动/权限配错不该逼着人重跑一遍模型。
这个脚本只做投递：读取某次 run 的项目与文档，调用 ``github_repo`` 适配器，
一次提交所有变更，并打印 commit / PR 链接。

用法::

    # 先写 .env 或临时环境变量：GITHUB_TOKEN=<fine-grained PAT>
    python scripts/sync_github_docs.py --repo aaa10001/Test_LivingDOc --branch main --no-pr

默认行为是提交到 ``livingdoc/docs`` 分支并开 PR；要直接同步 main，
显式传 ``--branch main --no-pr``。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把 LivingDoc 当前文档同步到 GitHub 仓库")
    parser.add_argument("--database-url", help="覆盖 DATABASE_URL，例如 sqlite:///var/github-user-zh.sqlite")
    parser.add_argument("--repo", help="仓库 full_name，例如 aaa10001/Test_LivingDOc")
    parser.add_argument("--project-id", help="项目 id；不给则按 --repo 或最近创建的项目")
    parser.add_argument("--run-id", help="指定 run；不给则用该项目最近一次 run")
    parser.add_argument("--branch", help="目标分支；默认 livingdoc/docs")
    parser.add_argument("--dir", dest="docs_dir", help="仓库内目录；默认 docs/livingdoc")
    parser.add_argument(
        "--no-pr",
        action="store_true",
        help="不建 PR（通常配合 --branch main 直接同步）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    if args.branch:
        os.environ["GITHUB_DOCS_BRANCH"] = args.branch
    if args.docs_dir:
        os.environ["GITHUB_DOCS_DIR"] = args.docs_dir
    if args.no_pr:
        os.environ["GITHUB_DOCS_OPEN_PR"] = "false"

    from app.config import get_settings
    from app.publishers import build_digest, deliver_run
    from app.store import list_projects, list_runs, open_store

    settings = get_settings()
    if not settings.github_token:
        print("缺少 GITHUB_TOKEN：请先在 .env 配置一个有 Contents: Read and write 的 fine-grained PAT。")
        return 2

    store = open_store(settings.database_url)
    try:
        projects = list_projects(store)
        if not projects:
            print("数据库里没有项目，先跑一次 analyze_push 或 seed_demo。")
            return 2

        if args.project_id:
            project = next((p for p in projects if p["id"] == args.project_id), None)
        elif args.repo:
            project = next((p for p in projects if p["repo_full_name"] == args.repo), None)
        else:
            project = projects[0]
        if project is None:
            print(f"找不到项目（repo={args.repo!r}, project_id={args.project_id!r}）。")
            return 2

        if args.run_id:
            run_id = args.run_id
        else:
            runs = list_runs(store, project["id"], limit=1)
            if not runs:
                print(f"项目 {project['repo_full_name']} 还没有 run。")
                return 2
            run_id = runs[0]["id"]

        digest = build_digest(store, run_id=run_id, settings=settings)
        print(f"项目：{project['repo_full_name']}  run：{run_id}")
        print(f"分支：{settings.github_docs_branch}  目录：{settings.github_docs_dir}  "
              f"开 PR：{settings.github_docs_open_pr}")
        results = deliver_run(store, digest=digest, settings=settings, targets=["github_repo"])
    finally:
        store.close()

    failed = False
    for result in results:
        print(f"- {result.target}: {result.status}")
        if result.error:
            print(f"  error: {result.error}")
        if result.external_url:
            print(f"  url: {result.external_url}")
        for key, value in (result.detail or {}).items():
            if value not in ("", None, 0, [], {}):
                print(f"  {key}: {value}")
        failed = failed or result.status == "failed"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())