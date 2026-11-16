#!/usr/bin/env python
"""给评审界面/压测准备一份可重复的本地演示数据。

做这件事的理由：`docker compose` 之外，评审页不该依赖"先配置 GitHub Webhook
再等 LLM 跑一轮"才能看到东西。这个脚本直接写 store，不碰模型，离线可跑。

用法：
    .\.venv\Scripts\python.exe scripts\seed_demo.py
    .\.venv\Scripts\python.exe scripts\seed_demo.py --database-url sqlite:///var/loadtest.sqlite --force

生成：
- 项目 acme/livingdoc-demo
- 3 份文档（类图 / 时序图 / API）
- 类图故意留一份"人改 + AI 候选"，用来演示评审闸门
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 直接 `python scripts/xxx.py` 时 sys.path[0] 是 scripts/，仓库根不在路径里。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CLASS_DIAGRAM = """@startuml
title LivingDoc 文档生成链路

class WebhookController {
  +handlePush(payload)
}
class AnalyzeTask {
  +run(job)
}
class DocumentationService {
  +generateModule(module)
}
class Publisher {
  +deliver(digest)
}
class ReviewAPI {
  +saveManualEdit(documentId)
}

WebhookController --> AnalyzeTask : enqueue
AnalyzeTask --> DocumentationService : generate
DocumentationService --> Publisher : publish
ReviewAPI --> Publisher : republish
@enduml
"""

SEQUENCE_DIAGRAM = """@startuml
title 一次 push 的文档生成时序

actor Developer
participant "GitHub" as GH
participant "FastAPI" as API
participant "Celery" as Worker
participant "LLM" as LLM
database "PostgreSQL" as DB

Developer -> GH : git push
GH -> API : POST /webhook/github
API -> Worker : analyze_push
Worker -> Worker : git diff + tree-sitter
Worker -> LLM : 生成文档草稿
LLM --> Worker : PlantUML / Markdown
Worker -> Worker : 校验 + 自纠错
Worker -> DB : 写入 documents / revisions
Worker --> GH : PR 评论（可选）
@enduml
"""

API_DOC = """# LivingDoc 评审 API

## 读取文档

`GET /api/v1/documents/{id}` 返回文档元信息、当前版本正文与待确认候选。
评审页面用这个端点渲染类图、时序图与 API 文档。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| current | object | 当前生效版本，包含完整正文 |
| candidate | object | AI 新版本，等待人工接受 |
| has_candidate | boolean | 是否存在待确认候选 |

## 人工编辑

`PUT /api/v1/documents/{id}` 写入一个 `created_by=human` 的新版本。
内容未通过 PlantUML/Markdown 校验时返回 422；确需保存可以传 `force=true`。
"""

HUMAN_CLASS = CLASS_DIAGRAM.replace(
    "class ReviewAPI {\n  +saveManualEdit(documentId)\n}",
    "class ReviewAPI {\n  +saveManualEdit(documentId)\n  +acceptCandidate(documentId)\n}",
)

AI_CLASS = CLASS_DIAGRAM.replace(
    "class Publisher {\n  +deliver(digest)\n}",
    "class Publisher {\n  +deliver(digest)\n  +retry(target)\n}",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="写入 LivingDoc 评审演示数据")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", "sqlite:///var/livingdoc.sqlite"),
    )
    parser.add_argument("--force", action="store_true", help="项目已存在时仍然再发布一版")
    args = parser.parse_args()

    # 必须在导入 app.config 之前设置：get_settings 是 lru_cache。
    os.environ["DATABASE_URL"] = args.database_url

    from app.config import get_settings
    from app.publish import list_documents, mark_manual_edit, publish_document
    from app.store import ensure_project, start_run
    from app.store import open_store

    settings = get_settings()
    settings.ensure_dirs()
    store = open_store(settings.database_url)

    project_id = ensure_project(
        store,
        provider="github",
        repo_full_name="acme/livingdoc-demo",
        repo_url="https://github.com/acme/livingdoc-demo.git",
        name="LivingDoc Demo",
    )
    existing = list_documents(store, project_id)
    if existing and not args.force:
        print(f"项目已存在（{len(existing)} 份文档）。要再加一版请传 --force。")
        print(f"project_id = {project_id}")
        return 0

    run_id, _ = start_run(
        store,
        project_id=project_id,
        base_sha="1" * 40,
        head_sha="2" * 40,
        trigger_type="push",
        trigger_ref="refs/heads/main",
    )

    docs = [
        ("class_diagram", "app.pipeline", "文档生成链路类图", CLASS_DIAGRAM),
        ("sequence_diagram", "app.pipeline.push", "一次 push 的生成时序", SEQUENCE_DIAGRAM),
        ("api_doc", "api.review", "评审 API 说明", API_DOC),
    ]
    document_ids: dict[str, str] = {}
    for doc_type, target_key, title, content in docs:
        outcome = publish_document(
            store,
            project_id=project_id,
            doc_type=doc_type,
            target_key=target_key,
            title=title,
            content=content,
            run_id=run_id,
            content_format="markdown" if doc_type == "api_doc" else "plantuml",
            model_name="seed-demo",
            prompt_version="v3-2025-11",
            change_summary="演示数据初始化",
        )
        document_ids[doc_type] = outcome.document_id
        print(f"  {doc_type:<18} v{outcome.version}  {target_key}")

    # 演示 ADR-006：人改一版，AI 的下一版只能停在候选区。
    human = mark_manual_edit(
        store,
        document_ids["class_diagram"],
        content=HUMAN_CLASS,
        summary="人工补充 acceptCandidate",
    )
    candidate = publish_document(
        store,
        project_id=project_id,
        doc_type="class_diagram",
        target_key="app.pipeline",
        title="文档生成链路类图",
        content=AI_CLASS,
        run_id=run_id,
        change_summary="AI 新增 Publisher.retry（待确认）",
    )
    print(
        f"  人工编辑        v{human.version}  app.pipeline\n"
        f"  AI 候选         v{candidate.version}  action={candidate.action}"
    )
    print(f"\nproject_id = {project_id}")
    print("启动：uvicorn app.main:app --port 8000")
    print("打开：http://127.0.0.1:8000/ui/")
    return 0


if __name__ == "__main__":
    sys.exit(main())