"""阶段四演示：坏输出 -> 机器诊断 -> 带诊断重试 -> 合格文档落盘。

默认用剧本模型（离线、零成本），把"模型最常犯的三个错"逐个演一遍：
    1. 加 markdown 围栏 + 开头寒暄     -> 抽取器剥掉，不浪费 API 调用
    2. 少了 @enduml（被 max_tokens 截断）-> 校验器报 E002，触发自我纠错
    3. 文档开头写"好的，以下是..."      -> 抽取器剥掉并记录契约违规

要真调 DeepSeek（需要 LLM_API_KEY，会产生费用）：
    python scripts/demo_phase4.py --live

产物写到 var/phase4/，可直接用 PlantUML 渲染检查。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.ai.client import DeepSeekClient, ScriptedLLM  # noqa: E402
from app.ai.service import DocumentationService  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.parsers import ChangeSet, TreeSitterExtractor, diff_symbols  # noqa: E402

OUT_DIR = BASE_DIR / "var" / "phase4"
PATH = "app/services/user.py"

OLD = """class UserService:
    def __init__(self, repo):
        self.repo = repo

    def load(self, user_id):
        return None


class UserRepository:
    def find_by_id(self, user_id):
        return None
"""

NEW = """class UserService:
    def __init__(self, repo):
        self.repo = repo

    def load(self, user_id):
        return self.repo.find_by_id(user_id)

    def delete(self, user_id):
        self.repo.remove(user_id)


class UserRepository:
    def find_by_id(self, user_id):
        return None

    def remove(self, user_id):
        return True
"""

BT = chr(96) * 3

# 剧本：第 1 次故意坏（围栏 + 寒暄 + 缺 @enduml），第 2 次修好
BAD_CLASS = (
    "好的，我为您生成了类图：\n\n"
    + BT
    + "plantuml\n@startuml\nclass UserService {\n  +load(user_id)\n  +delete(user_id)\n}"
)
GOOD_CLASS = """@startuml
class UserService {
  -repo: UserRepository
  +__init__(repo: UserRepository)
  +load(user_id: int): User
  +delete(user_id: int): None
}
class UserRepository {
  +find_by_id(user_id: int): User
  +remove(user_id: int): bool
}
UserService --> UserRepository : uses
@enduml"""
GOOD_API = """## app.services

### UserService.load

- signature: load(self, user_id: int) -> User
- description: 通过仓库按 ID 读取用户。

| name | type | required | description |
| --- | --- | --- | --- |
| user_id | int | yes | 用户主键 |

### UserService.delete

- signature: delete(self, user_id: int) -> None
- description: 删除指定用户。
"""
GOOD_SEQ = """@startuml
actor Client
participant UserService
participant UserRepository
Client -> UserService: load(user_id)
UserService -> UserRepository: find_by_id(user_id)
UserRepository --> UserService: user
UserService --> Client: user
@enduml"""


def build_change_set() -> ChangeSet:
    extractor = TreeSitterExtractor()
    file_diff = diff_symbols(
        PATH, extractor.parse(PATH, OLD.encode()), extractor.parse(PATH, NEW.encode())
    )
    return ChangeSet(files=[file_diff], skipped_files=[])


def make_client(live: bool):
    if not live:
        return ScriptedLLM([BAD_CLASS, GOOD_CLASS, GOOD_API, GOOD_SEQ]), "scripted"

    settings = get_settings()
    if not settings.llm_api_key:
        raise SystemExit("LLM_API_KEY is not set; run without --live for the offline demo")
    client = DeepSeekClient(
        settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
    )
    return client, settings.llm_model


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="call the real DeepSeek API")
    args = parser.parse_args()

    change_set = build_change_set()
    print("changed functions:")
    for change in change_set.all_changes():
        symbol = change.new or change.old
        print(f"  {change.kind.value:8s} {symbol.qualified_name}  L{symbol.start_line}-{symbol.end_line}")

    client, model_name = make_client(args.live)
    service = DocumentationService(client, max_attempts=2 if args.live else 3, model_name=model_name)

    print()
    print("=" * 70)
    print("generating (live API)" if args.live else "generating (offline scripted model)")
    print("=" * 70)

    docs = service.generate_module(change_set, module="app.services")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for draft in docs.drafts:
        filename = f"{docs.module}.{draft.doc_type.value}.{draft.extension}"
        target = OUT_DIR / filename
        if not draft.content.endswith("\n"):
            target.write_text(draft.content + "\n", encoding="utf-8")
        else:
            target.write_text(draft.content, encoding="utf-8")

        print()
        print(
            f"[{draft.doc_type.value}] ok={draft.ok} attempts={draft.attempts} "
            f"review={draft.needs_review} tokens={draft.tokens} -> {target.relative_to(BASE_DIR)}"
        )
        for diagnostic in draft.diagnostics:
            print("   " + diagnostic.render())

    print()
    print("-" * 70)
    print("summary:", docs.summary)

    # 展示"诊断确实回到了模型手里"：打印第 2 次调用最后一条 user 消息的开头
    if isinstance(client, ScriptedLLM) and len(client.calls) >= 2:
        repair = client.calls[1][-1]["content"]
        print()
        print("repair message sent back to the model (first 500 chars):")
        print()
        print(repair[:500])
        print()
        print("temperatures used:", client.temperatures)

    print()
    print("next: open the .puml files in var/phase4/ or run")
    print("      java -jar tools/plantuml.jar -checkonly var/phase4/*.puml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
