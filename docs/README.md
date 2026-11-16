# LivingDoc 架构文档索引

> 研究生课程《高级软件工程》大作业 · 阶段一（架构）+ 阶段二（骨架）+ 阶段三（AST）+ 阶段四（AI 生成）+ 阶段五（落库与发布）+ 阶段六（容器化）+ 阶段七（真实模型评测）+ 阶段八（投递与评审）
> **接手项目请先读 [HANDOFF.md](HANDOFF.md)**：现状、仓库地图、运行手册、雷区、未完成事项。
> 文档版本：v0.9 · 主链路已打通 · 阶段七接真实模型跑完 7 轮 21 份文档 · 阶段八补 PR 评论/知识库推送、Web 评审与真实仓库回归 · **225 项单测全绿**

本目录只回答三个问题：**系统长什么样、为什么这样设计、边界在哪。**
所有设计决策的争议过程记录在 [decisions.md](architecture/decisions.md)，这份文件是「人机协同」论证的主要素材来源。

## 目录

| 文件 | 内容 | 优先级 |
|---|---|---|
| [architecture/00-overview.md](architecture/00-overview.md) | 技术栈、分层、七阶段流水线、非功能设计、得分点映射 | ★★★ 先读 |
| [architecture/data-flow.mmd](architecture/data-flow.mmd) | 核心数据流图（Mermaid flowchart） | ★★★ |
| [architecture/sequence-analyze.mmd](architecture/sequence-analyze.mmd) | 一次 Webhook 到文档发布的完整时序（含幂等分支、重试回路） | ★★★ |
| [architecture/erd.mmd](architecture/erd.mmd) | 实体关系图（Mermaid erDiagram） | ★★ |
| [architecture/database.md](architecture/database.md) | 全量 PostgreSQL DDL、索引约束、SQLite 降级、数据生命周期 | ★★★ |
| [architecture/api.md](architecture/api.md) | RESTful 接口清单、错误码、幂等与分页约定 | ★★ |
| [architecture/decisions.md](architecture/decisions.md) | **ADR 决策记录 + 人机讨论原文**（7 条） | ★★★ 答辩必看 |
| [02-phase2-skeleton.md](02-phase2-skeleton.md) | 阶段二：Webhook 入口、验签、克隆、Celery 任务、状态码契约 | ★★★ |
| [03-phase3-ast.md](03-phase3-ast.md) | 阶段三：tree-sitter 解析、函数身份、三种指纹、上下文预算 | ★★★ |
| [04-phase4-generation.md](04-phase4-generation.md) | 阶段四：Prompt 三代演化、校验规则表、Retry vs Self-Correction | ★★★ |
| [05-phase5-pipeline.md](05-phase5-pipeline.md) | 阶段五：三段式流水线、run 状态机、幂等、看门狗、ADR-006 发布闸门 | ★★★ |
| [06-docker.md](06-docker.md) | 阶段六：镜像与 Compose 设计、ADR-001/002/007 的实测证据、11 条踩坑记录 | ★★★ |
| [07-live-llm.md](07-live-llm.md) | 阶段七：真实模型 7 轮 21 份文档的通过率、M005 与修复提示词两个真实 bug、PlantUML 独立校验、全栈 e2e | ★★★ |
| [08-phase8-delivery.md](08-phase8-delivery.md) | 阶段八：投递适配器、评审 API/UI、负载测试、M005 重生成、镜像瘦身 | ★★★ |

## 如何渲染

- Mermaid：GitHub / VS Code（装 Markdown Preview Mermaid Support）/ Typora 直接渲染；导出图片用 mermaid-cli。
- PlantUML：阶段四起产出 .puml 文件（var/phase4/）。本机无 JRE，用 VS Code 的 PlantUML 插件或在线服务查看；生产环境由 tools/plantuml.jar 校验。
- 论文插图建议：把 .mmd 导出 SVG 后插入，保留 .mmd 源文件作为可复现证据。

## 与作业要求的对应

| 作业要求 | 本文档的回应位置 |
|---|---|
| 讲清楚如何用 AI 辅助开发 | decisions.md 的「人机讨论记录」+ 04 文档 §8 的 Prompt 五轮演化 |
| 说明如何解决开发中的问题 | 00-overview.md §6 非功能设计；ADR-001 / 002 / 005；04 文档 §4（传输层与语义层分离） |
| 体现工程判断力 | decisions.md 中 3 处**否决或修正了 AI 初始建议**的记录（Redis 防抖、Neo4j、Celery 默认配置）；04 文档 §8 第 3、4 轮 |
| 系统架构合理性 | data-flow.mmd + sequence-analyze.mmd + database.md |
| 软件成果可运行 | [02-phase2-skeleton.md](02-phase2-skeleton.md) + [03-phase3-ast.md](03-phase3-ast.md) + [04-phase4-generation.md](04-phase4-generation.md) + [05-phase5-pipeline.md](05-phase5-pipeline.md) + [06-docker.md](06-docker.md) + [07-live-llm.md](07-live-llm.md) + 仓库根的 app/、tests/、Dockerfile、docker-compose.yml |
| **上下文有限**如何解决 | [03-phase3-ast.md](03-phase3-ast.md) §5–§7：语义指纹 + 只喂变化函数 + 预算截断；04 文档 §10 |
| **Prompt 工程** | [04-phase4-generation.md](04-phase4-generation.md) §2–§3：契约可执行化、few-shot、负面清单、机器校验 |

## 阶段二：代码骨架

| 文件 | 内容 |
|---|---|
| [02-phase2-skeleton.md](02-phase2-skeleton.md) | 交付物清单、15 分钟跑通、**八个必须做对的细节**、状态码契约、已验证/未验证范围、人机协同记录 |

对应代码：app/main.py（入口 + 验签 + 入队）、app/git_handler.py（克隆 + diff 解析）、
app/tasks.py（Celery 任务）、app/security.py（签名验证）、tests/（40 项单测）。
运行方式见 02 文档 §2；测试命令 python -m pytest tests/。

**实测状态（2026-11-05）**：uvicorn + Celery(threads) 同时启动，投递一条伪造 push
（octocat/Hello-World，7fd1a60b..b3cbd5bb），4.5 秒后产出 var/work/runs/e2e-004.json；
202 / 200-duplicate / 200-ignored / 401 四种状态码均已用真实 HTTP 请求验证。

## 阶段三：AST 解析与上下文裁剪

| 文件 | 内容 |
|---|---|
| [03-phase3-ast.md](03-phase3-ast.md) | 为什么正则不可行、S-expression Query、函数身份 key、三种指纹、比对四步、Prompt 预算、**access violation 根因调查** |

对应代码：app/parsers/（model / profiles / extractor / differ / service）、tests/test_parsers.py（26 项）、
scripts/diff_functions.py（真实仓库函数级变更演示）。

**本阶段最值得写进论文的一件事**：tree-sitter 0.26.0 的 Windows 轮子在行内列号 > 255 时物化 Point
会让进程 access violation（无 traceback、无日志），排查与修复全过程见 03 文档 §9。

**实测状态（2026-11-05）**：对真实 diff 跑出 modified / added / renamed 三类变更，
输出函数级摘要与 redoc_targets=2；66 项单测全绿。

## 阶段四：AI 生成 UML 与 API 文档

| 文件 | 内容 |
|---|---|
| [04-phase4-generation.md](04-phase4-generation.md) | Prompt 三代演化的原文、输出契约、校验规则表（E001–E012 / W001–W004 / M001–M004）、Retry 与自发纠错的分工、失败策略、**未真调 API 的如实声明** |

对应代码：app/ai/（prompts / validator / client / self_correct / service）、5 个测试文件（73 项）、
scripts/demo_phase4.py（离线剧本演示）。

**本阶段最值得写进论文的一件事**：把"让模型自己保证格式"改成"契约 + 机器校验 + 带诊断修复"。
提示词只能提高概率，正确性必须由生成之后的校验兜底（04 文档 §2.3、§4）。

**实测状态（2026-11-05，离线剧本模型）**：坏输出（围栏 + 寒暄 + 缺 @enduml）被判定 E002，
诊断回灌后第 2 轮通过，温度序列 [0.2, 0.0, 0.2, 0.2]，repair_rounds=1，三份文档落盘 var/phase4/。
**真实 DeepSeek API 未调用**（无 key），plantuml.jar 校验未执行（无 JRE）——见 04 文档 §7。

## 阶段五：落库、发布闸门与三段式流水线

| 文件 | 内容 |
|---|---|
| [05-phase5-pipeline.md](05-phase5-pipeline.md) | 为什么拆三段、12 态状态机与非法迁移、run 级幂等、看门狗、SQLAlchemy Core 的取舍、ADR-006 五条发布规则的判定顺序、跳过原因表、176 项测试分布、真实 git 演示输出、人机协同五条记录 |

对应代码：app/store.py、app/publish.py、app/pipeline.py、app/tasks.py（重写）、
tests/test_store.py + test_publish.py + test_pipeline.py + test_tasks.py（37 项）、
scripts/demo_phase5.py（真实 git + 真实 AST + 剧本模型 + 真 SQLite）。

**本阶段最值得写进论文的一件事**：文档的所有权是一个状态机，不是一个布尔值。
"AI 能不能覆盖"取决于 current 与 latest 是否分离、manual_edited 是否保持、
候选是否重复——这三条各自都能被单独改坏，所以全部用测试固定。

**实测状态（2026-11-05，剧本模型）**：一次真实提交产出 3 篇文档 v1；重复发布 3 篇全部 unchanged；
人工编辑后 current=v2(human)、latest=v3(ai)、status=stale；accept_ai 后 current 切到 v3，
manual_edited 仍为 True。**真实 LLM 与 PostgreSQL 仍未实测**——见 05 文档 §13。

## 阶段六：容器化——把"未验证"变成"已验证"

| 文件 | 内容 |
|---|---|
| [06-docker.md](06-docker.md) | 为什么容器化、镜像分层与非 root、headless JRE 与字体库的坑、compose 拓扑与串行建表、smoke 与端到端实测、11 条踩坑、边界声明 |

对应文件：Dockerfile、docker-compose.yml、.dockerignore、scripts/init_db.py、
scripts/container_smoke.sh、requirements-postgres.txt。

**本阶段最值得写进论文的一件事**：容器化最大的价值不是部署，而是把"我以为能跑"变成"换一台机器也能跑，
并且有日志"。阶段一到五是"设计成立 + 单测通过"，阶段六之后 ADR-001（prefork）、ADR-002（Redis broker）、
ADR-007（plantuml -checkonly）第一次有了运行证据。

**实测状态（2026-11-06）**：多阶段镜像 581MB（原 850MB）；容器内 215 项测试通过；`-checkonly` 合法图 rc=0、坏图被识别；
compose 起 pg16 + redis7 + api + worker(prefork×4)，webhook 202 → cloning → diffing → parsing →
skipped(llm_not_configured)，17ms 解析出 symbols_added=1/symbols_modified=1；PostgreSQL 上
created → unchanged → human_edit → deferred_stale → accepted 全链路复现。
**真实 LLM 仍未调用；容器内访问 github.com 超时，端到端改用本地裸仓库**——见 06 文档 §8。

## 阶段七：接上真实模型（有失败率的数据）

| 文件 | 内容 |
|---|---|
| [07-live-llm.md](07-live-llm.md) | 接入方式与密钥边界、评测方法（scripts/live_eval.py）、7 轮 21 份文档的统计、失败模式、M005 与修复提示词两个 bug、强制截断实验、PlantUML 独立校验 14/14、全栈 e2e、成本与边界 |

对应代码/脚本：scripts/live_eval.py、app/ai/validator.py（新增 M005/W006）、
app/ai/prompts.py（build_repair_prompt 按 kind 分支）。

**本阶段最值得写进论文的一件事**：真实模型不会按你的想象失败，它失败的方式恰恰是你最没写校验的地方。
21 份真实产物里 18 份一次通过（86%），3 份失败全部集中在 api_doc；其中两次是我自己的提示词 bug
（Markdown 进入修复轮被要求输出 PlantUML），一次是校验器漏判 31 字符的空壳标题。
这两处都已修复并配了测试。

**实测状态（2026-11-06）**：全栈 e2e —— webhook 202 → prefork worker 解析（entities_changed=2）
→ 真实 API 生成 3 份文档（15.9s，tokens_out=5722）→ PostgreSQL 落库 published v1，render_ok=true。
**未做**：多模型对比、大仓库压力样本、人工对照评测、真实 429 限流场景。

## 阶段八：把文档送到人面前（投递 + 评审）

| 文件 | 内容 |
|---|---|
| [08-phase8-delivery.md](08-phase8-delivery.md) | 为什么投递失败不能拖垮 run、三个适配器、评审 API/UI、并发版本号竞争、负载测试数字、M005 重生成、镜像瘦身 |

对应代码：app/publishers/（local_kb / github_pr / webhook / dispatch）、app/api/review.py、
app/render.py、app/static/（index.html / style.css / app.js）、scripts/seed_demo.py、scripts/load_test.py。

**本阶段最值得写进论文的一件事**：生成成功不等于交付成功。PR 评论接口 502、知识库目录不可写，
都不应该把一次已经产出文档的 run 判成 failed；投递结果单独进 publish_records，run 的成败只由
"文档是否生成并入库"决定。这是"外部副作用"与"核心事务"的边界。

**实测状态（2026-11-06）**：本机 SQLite 读压测 RPS 191.96（p95 70ms）、读写混合 RPS 67.25（0 失败）；
PostgreSQL 并发发布验证见 08 文档；评审页 `/ui/` 可浏览、编辑、diff、接受候选、发布；镜像 581MB。

**真实仓库回归（2026-11-09）**：`psf/requests` 的 3 文件提交（7 个变更函数）→ 81.4s、6 份文档、
0 拒稿、7/7 投递成功；API 文档 4/4 函数各一节。这轮修掉了"纯格式化提交空 payload 仍生成"、
"Markdown 内部代码块被当外层围栏剥掉"、"API 文档漏函数"三个真实缺陷。细节见
[08-phase8-delivery.md](08-phase8-delivery.md) §8。

## 依赖与运行环境

- Python 3.11.2 起，venv 在 .venv/
- 真实模型：在 .env 里配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（密钥不入库）
- 容器化运行：Docker Desktop（WSL2 后端）+ docker compose，见 [06-docker.md](06-docker.md)
- 安装走 uv（本机 pip 因 schannel 证书问题不可用）：
  $env:UV_CACHE_DIR = "$PWD\.uv-cache"; uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt
- Celery 在 Windows 必须用 --pool=threads（见 ADR-001）
- 环境变量模板见 .env.example
