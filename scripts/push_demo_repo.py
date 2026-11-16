"""把 var/demo-complex 推到 GitHub，作为"复杂仓库真实链路"的输入。

一次提交（blobs → tree → commit → 更新 ref），这样 diff 里是一整个模块，
而不是六次零散提交。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.config import get_settings
from app.publishers.github_repo import _GitHubClient

REPO = "aaa10001/Test_LivingDOc"
BRANCH = "main"
DEMO = BASE / "var" / "demo-complex"

# 显式列出：只推源码，不推探针与缓存
FILES = [
    "src/domain/__init__.py",
    "src/domain/base.py",
    "src/services/__init__.py",
    "src/services/user_service.py",
    "src/api/__init__.py",
    "src/api/routes.py",
]

MESSAGE = """feat(src): 加入多模块用户服务样例

- src/domain/base.py: 实体继承体系、抽象仓储、TTL 缓存
- src/services/user_service.py: 组合依赖、同步/异步重试分支
- src/api/routes.py: HTTP 入口与请求分派

用于验证 LivingDoc 在真实复杂代码上的类图与时序图生成效果。
"""


def main() -> int:
    settings = get_settings()
    if not settings.github_token:
        print("GITHUB_TOKEN 未配置")
        return 1

    message = sys.argv[1] if len(sys.argv) > 1 else MESSAGE

    missing = [path for path in FILES if not (DEMO / path).exists()]
    if missing:
        print("缺少文件:", missing)
        return 1

    with _GitHubClient(
        token=settings.github_token,
        base_url=str(settings.github_api_base or "https://api.github.com"),
        timeout=30.0,
        transport=None,
    ) as gh:
        ref = gh.request("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")
        base_sha = ref["object"]["sha"]
        base_commit = gh.request("GET", f"/repos/{REPO}/git/commits/{base_sha}")
        base_tree = base_commit["tree"]["sha"]
        print("base:", base_sha)

        entries = []
        for path in FILES:
            data = (DEMO / path).read_bytes()
            blob = gh.request(
                "POST",
                f"/repos/{REPO}/git/blobs",
                json={"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
            )
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            print("blob:", path, len(data), "bytes")

        tree = gh.request("POST", f"/repos/{REPO}/git/trees", json={"base_tree": base_tree, "tree": entries})
        commit = gh.request(
            "POST",
            f"/repos/{REPO}/git/commits",
            json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
        )
        new_sha = commit["sha"]
        gh.request("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}", json={"sha": new_sha, "force": False})
        print("commit:", f"https://github.com/{REPO}/commit/{new_sha}")
        print("before:", base_sha)
        print("after:", new_sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())