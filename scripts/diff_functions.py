"""演示：从真实 git 仓库的两个 commit 中提取"函数级变更"。

用法（在仓库任意位置执行）：
    python scripts/diff_functions.py --repo D:\path\to\repo --base HEAD~1 --head HEAD
    python scripts/diff_functions.py --repo . --base main --head feature/x --json out.json

它做的事和线上流水线阶段三完全一致：
    git diff 拿到变更文件清单（阶段二已有）
      -> 对每个受支持的源文件，分别取 base 版和 head 版的内容
      -> tree-sitter 解析出两版的函数集合
      -> 按 key 比语义指纹，得出 added / removed / modified / renamed
      -> 打印成人类可读的报告，并给出喂给大模型的 prompt 载荷统计

注意：这不是"文本 diff 的函数化包装"，而是真的解析 AST。你可以用
"只改缩进"或"只加注释"的提交试一下 —— 报告里会显示 formatting_only，
这类变更不应该触发文档重新生成。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.git_handler import diff_commits  # noqa: E402
from app.parsers import (  # noqa: E402
    ChangeKind,
    ChangedFunctionService,
    FilePayload,
    build_prompt_payload,
)


def _git(repo: Path, *args: str) -> bytes:
    """读文件内容用 git show，而不是切分支：无副作用、可并发、不动工作区。"""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(repo),
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{result.stderr.decode('utf-8', 'replace')}")
    return result.stdout


def _show(repo: Path, rev: str, path: str) -> bytes | None:
    """取某版本某文件的内容；文件在该版本不存在时返回 None。"""
    try:
        return _git(repo, "show", f"{rev}:{path}")
    except SystemExit:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="本地 git 仓库路径")
    parser.add_argument("--base", required=True, help="旧版本（commit / 分支 / HEAD~1）")
    parser.add_argument("--head", required=True, help="新版本")
    parser.add_argument("--json", help="把完整结果另存为 JSON")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    started = time.perf_counter()

    changes = diff_commits(repo, args.base, args.head)
    print(f"仓库：{repo}")
    print(f"区间：{args.base}..{args.head}  变更文件 {len(changes)} 个\n")

    payloads: list[FilePayload] = []
    for change in changes:
        if change.is_deleted:
            payloads.append(FilePayload(path=change.path, new_source=None))
            continue
        payloads.append(
            FilePayload(
                path=change.path,
                old_source=_show(repo, args.base, change.path),
                new_source=_show(repo, args.head, change.path),
            )
        )

    service = ChangedFunctionService()
    change_set = service.analyze(payloads)
    summary = change_set.to_summary()

    for file_diff in change_set.files:
        if not file_diff.changes:
            continue
        print(f"── {file_diff.path}  [{file_diff.language}]")
        for change in file_diff.changes:
            tag = change.kind.value
            if change.formatting_only:
                tag += " (formatting-only)"
            new = change.new
            where = f"L{new.start_line}-L{new.end_line}" if new else ""
            label = change.key
            if change.kind is ChangeKind.RENAMED and change.old is not None:
                label = f"{change.old.key} -> {change.key}"
            print(f"   {tag:26} {label} {where}")
        print()

    print("汇总：")
    for key, value in summary.items():
        print(f"   {key:22} {value}")

    prompt = build_prompt_payload(change_set)
    print(f"\n喂给大模型的载荷：{len(prompt)} 字符")
    print(f"   （仅包含 needs_redoc 的函数体，共 {summary['redoc_targets']} 个目标）")

    if change_set.skipped_files:
        print("\n跳过的文件（语言不支持或解析失败）：")
        for path, reason in change_set.skipped_files:
            print(f"   {path}  ->  {reason}")

    if args.json:
        payload = {
            "base": args.base,
            "head": args.head,
            "summary": summary,
            "files": [
                {
                    "path": f.path,
                    "language": f.language,
                    "changes": [
                        {
                            "kind": c.kind.value,
                            "key": c.key,
                            "formatting_only": c.formatting_only,
                            "needs_redoc": c.needs_redoc,
                            "start_line": c.new.start_line if c.new else None,
                            "end_line": c.new.end_line if c.new else None,
                        }
                        for c in f.changes
                    ],
                }
                for f in change_set.files
            ],
        }
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 已写入 {args.json}")

    print(f"\n耗时 {int((time.perf_counter() - started) * 1000)} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
