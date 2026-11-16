"""本地模拟 GitHub Webhook —— 不需要公网、不需要 ngrok。

用法（先启动 API）：
    .venv\Scripts\python.exe scripts\send_test_webhook.py --repo octocat/Hello-World \
        --before <sha1> --after <sha2>

只测 HTTP 层（验签/去重/状态码）时可以不传 sha，默认值必然在 git 阶段失败，
但接口依然返回 202 —— 这正好能演示"API 快速返回、失败发生在异步任务里"。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

# 直接 python scripts/send_test_webhook.py 时，sys.path[0] 是 scripts/ 而不是项目根，
# 于是 import app 会失败。显式把项目根插到最前面，保证文档里的命令能照抄即用。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def build_push_payload(
    repo: str, before: str, after: str, clone_url: str | None = None
) -> dict[str, Any]:
    return {
        "ref": "refs/heads/main",
        "before": before,
        "after": after,
        "deleted": False,
        "repository": {
            "full_name": repo,
            # Gitee / 自建 GitLab / 离线本地仓库都可以用 --clone-url 覆盖
            "clone_url": clone_url or f"https://github.com/{repo}.git",
            "private": False,
        },
        "sender": {"login": "local-tester"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="发送一条伪造的 GitHub push 事件")
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhook/github")
    parser.add_argument("--secret", default="dev-secret-change-me")
    parser.add_argument("--repo", default="octocat/Hello-World")
    parser.add_argument("--before", default="a" * 40)
    parser.add_argument("--after", default="b" * 40)
    parser.add_argument("--delivery", default=None, help="默认每次随机，便于重复测试")
    parser.add_argument("--bad-signature", action="store_true", help="故意用错密钥，验证 401")
    parser.add_argument("--event", default="push")
    parser.add_argument(
        "--clone-url",
        default=None,
        help="覆盖 clone 地址，用于离线端到端测试（如 D:/tmp/origin.git）",
    )
    args = parser.parse_args()

    payload = build_push_payload(args.repo, args.before, args.after, args.clone_url)
    # 必须序列化一次，签名和发送用的是同一份字节
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    from app.security import sign_github_payload  # 复用被测代码，避免两边实现不一致

    secret = args.secret if not args.bad_signature else args.secret + "-wrong"
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": args.event,
        "X-GitHub-Delivery": args.delivery or str(uuid.uuid4()),
        "X-Hub-Signature-256": sign_github_payload(secret, body, "sha256"),
    }

    resp = httpx.post(args.url, content=body, headers=headers, timeout=15)
    print(f"HTTP {resp.status_code}")
    try:
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    except Exception:
        print(resp.text)
    return 0 if resp.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
