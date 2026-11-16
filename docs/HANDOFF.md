# 交接文档：LivingDoc 项目现状（给下一个会话 / 接手的人）

> 写于 2026-11-06。这份文档的目的：**不读历史对话，也能在 10 分钟内接手这个项目。**
> 它记录的是"现在是什么状态、为什么是这样、下一步该做什么"，不是教程。
> 按顺序读：§1 现状 → §4 仓库地图 → §7 运行手册 → §10 未完成事项。
> 如果你是 AI 会话：先读 §9 的雷区，那里的每一条都会让你浪费时间。

---

## 1. 一句话现状

LivingDoc 的**主链路已经全部打通并验证**：Git Webhook → Celery（Redis/docker 或 SQLite/本地）
→ git diff → tree-sitter 提取变更函数 → 真实大模型生成 PlantUML/Markdown → 机器校验与自纠错
→ PostgreSQL 落库 → 人工修改闸门 → 评审界面 / PR 评论 / 知识库推送。**225 项单测全绿，
Docker Compose 全栈实测通过，真实模型 7 轮 21 份文档的一次通过率 86%，镜像从 850MB 降到 581MB。
2026-11-09 又在真实仓库 psf/requests 上跑通：3 文件提交 → 6 份文档、0 拒稿、7/7 投递成功。**

除了"多模型横向对比"（用户明确本轮不做），HANDOFF §10 的功能项均已落地。

---

## 2. 项目背景（为什么做、评分点）

研究生课程《高级软件工程》大作业：做一个基于 AI 的"活文档 / 架构逆向生成"系统。
代码提交后自动更新 UML 类图、时序图与 API 文档，并推送到知识库。

作业明确看重三件事，项目的设计都围绕它们：

| 评分点 | 本项目的回答 | 证据 |
|---|---|---|
| 大模型上下文有限，如何分块 | tree-sitter AST 提取"受影响函数"，只把这些函数喂给模型；单次输入有 60k 字符预算并按 added>modified>renamed>removed 排序 | docs/03-phase3-ast.md、app/parsers/ |
| Prompt 工程：严格输出合规 PlantUML | 四层输出契约 + 生成后机器校验（E001–E012 / W001–W004）+ 带诊断的自我纠错 | docs/04-phase4-generation.md、app/ai/ |
| 国产工具 | DeepSeek 模型（走阿里云百炼 MaaS 的 OpenAI 兼容端点） | docs/07-live-llm.md |

另外两个贯穿全局的论点（论文可直接用）：

1. **"请只输出"是请求，不是保证。** 语言模型没有输出约束能力，能保证格式的只有生成之后的机器校验。
2. **真实模型不会按你的想象失败。** 21 份真实产物里 3 份失败，失败全部集中在 api_doc，
   其中两次是提示词 bug、一次是校验器漏判——都不是"模型画错了图"。

---

## 3. 完成度矩阵

| 阶段 | 内容 | 状态 | 文档 |
|---|---|---|---|
| 一 | 架构、数据流、数据库设计、45 个 REST 端点、7 条 ADR | ✅ 完成 | docs/architecture/ |
| 二 | FastAPI + Webhook 验签 + Celery + git 克隆/diff | ✅ 实测（真实 GitHub 仓库 202/200/401） | 02-phase2-skeleton.md |
| 三 | tree-sitter AST 提取 + 函数级 diff + 上下文预算 | ✅ 26 项测试 | 03-phase3-ast.md |
| 四 | Prompt v1→v3、校验器、Retry、自纠错、编排 | ✅ 73 项测试 + 离线演示 | 04-phase4-generation.md |
| 五 | run 状态机、三段式任务、幂等、看门狗、发布闸门、PG/SQLite 双跑 | ✅ 37 项测试 + 演示 | 05-phase5-pipeline.md |
| 六 | Docker 镜像 + Compose（PG/Redis/prefork）+ 三条 ADR 实测 | ✅ 多阶段镜像 581MB，全栈 e2e | 06-docker.md |
| 七 | 真实模型接入 + 7 轮评测 + 两个 bug 修复 | ✅ 86% 一次通过，PlantUML 14/14 | 07-live-llm.md |
| 八 | PR 评论 / 知识库推送、Web 评审界面、负载测试、M005 重生成、真实仓库回归 | ✅ 投递适配器 + `/api/v1` + `/ui` + psf/requests 6 文档 0 拒稿 | 08-phase8-delivery.md（新增） |
| 未做 | GLM-4 横向对比 | ⬜ 用户明确本轮跳过 | — |

---

## 4. 仓库地图

### 4.1 运行时代码

| 文件 | 职责 | 关键点 |
|---|---|---|
| app/main.py | FastAPI 入口 | 原始 body 验签、25MB 上限、五条返回路径（202/200/200/400/401/503） |
| app/security.py | HMAC 验签 | fail-closed，compare_digest |
| app/dedupe.py | 内存 TTL 去重 | 只做快速挡板，正确性靠 DB 唯一索引 |
| app/git_handler.py | 克隆/fetch/diff/read_blob | --filter=blob:none、token 走 -c http.extraHeader 不落盘、--no-renames -z |
| app/tasks.py | 三段任务 + watchdog | analyze_push → parse_run → generate_run；无 LLM key 时 skipped 不失败 |
| app/pipeline.py | 编排纯函数 + artifact 序列化 | parse_changes / dump_change_set / publish_drafts |
| app/store.py | SQLAlchemy Core 持久层 | 6 张表、12 态状态机、幂等 run、active_runs 看门狗查询、publish_records |
| app/publish.py | 文档版本与人工修改闸门 | created/updated/unchanged/deferred_stale/accepted；版本号冲突重试 |
| app/config.py | 全部配置 | 环境变量 / .env；数据库、投递目标、KB 目录 |
| app/render.py | PlantUML/Markdown 渲染与校验 | jar 缺失时降级静态校验；SVG 内容寻址缓存 |
| app/api/review.py | `/api/v1` 评审 API | 文档列表/版本/diff/人工编辑/接受候选/发布/run 审计 |
| app/publishers/ | 投递适配器 | local_wiki、github_pr_comment、webhook_kb；失败隔离 + publish_records |
| app/static/ | 评审前端 | 原生 HTML/CSS/JS，无构建链；`/ui` 静态挂载 |

### 4.2 AI 层

| 文件 | 职责 |
|---|---|
| app/ai/prompts.py | v1→v3 三代 Prompt、输出契约、修复提示词（**按 kind 分支，见 §9**） |
| app/ai/validator.py | PlantUML E001–E012/W001–W004、Markdown M001–M005/W005–W006 |
| app/ai/client.py | OpenAI 兼容客户端 + RetryPolicy（429/5xx/超时） |
| app/ai/self_correct.py | 多轮修复：温度 0.2→0.0、截断时预算 ×1.5、fail-closed |
| app/ai/service.py | 模块级编排：class → api → sequence，失败隔离 |

### 4.3 解析层

| 文件 | 职责 |
|---|---|
| app/parsers/extractor.py | tree-sitter 封装；**不用 Point，用 _LineIndex**（Windows 崩溃修复） |
| app/parsers/differ.py | 新旧符号比对、重命名配对（body_hash）、formatting-only 判定 |
| app/parsers/model.py | Symbol / FileSymbols / SymbolChange / ChangeSet |

### 4.4 脚本与测试

| 文件 | 用途 |
|---|---|
| scripts/send_test_webhook.py | 本地伪造 GitHub push（支持 --clone-url file:// 离线） |
| scripts/demo_phase4.py | 剧本模型演示（--live 走真实 API） |
| scripts/demo_phase5.py | 真实 git + AST + 真 SQLite 的发布闸门演示 |
| scripts/live_eval.py | **真实模型批量评测**，写 var/live/report.json |
| scripts/container_smoke.sh | 容器内自检（pytest + tree-sitter + plantuml 正反例） |
| scripts/init_db.py | 容器启动建表（串行，避免并发 create_all 撞 PG） |
| scripts/seed_demo.py | 造 3 份文档 + 一条人工候选，供评审界面演示 |
| scripts/load_test.py | 纯 asyncio+httpx 压测（read/publish/mixed，输出 p50/p95/p99） |
| tests/ | 19 个文件，225 项 |

### 4.5 文档

| 文件 | 内容 |
|---|---|
| docs/architecture/ | 架构、数据流、ER、45 端点、7 条 ADR |
| docs/02…08 | 每个阶段一篇，含实测输出、踩坑、人机协同记录 |
| docs/README.md | 索引（v0.8） |
| docs/HANDOFF.md | 本文件 |

---

## 5. 数据流与状态机（30 秒版）

~~~
POST /webhook/github (验签, 202)
   └─ analyze_push: 建 project/run → 克隆 → fetch → diff → 写 <run_id>.json → 入队
        └─ parse_run: tree-sitter → ChangeSet → 写 <run_id>.changes.json → 入队
             └─ generate_run: LLM + 校验 + 自纠错 → publish（versions）
                  └─ deliver_run: local_wiki / github_pr_comment / webhook_kb（失败不影响 run）
~~~

- run 的 12 个状态定义在 app/store.py；非法迁移抛 InvalidTransition，同状态重复迁移幂等。
- 幂等键 = sha256(project|base_sha|head_sha|trigger)；重复投递返回 duplicate_run，不碰 git。
- 看门狗按"业务数据 15 分钟没动"重排，不依赖队列 visibility timeout。
- 文档发布：未人改 → 直接更新 current；已人改 → 只写候选 + status=stale，accept_ai 才切换。

---

## 6. 关键设计决策（答辩可直接讲）

| ADR | 决策 | 为什么（一句话） |
|---|---|---|
| 001 | Windows 用 threads 池，Linux/容器用 prefork + max-tasks-per-child | billiard prefork 依赖 fork()，Windows 上不可用；本项目 I/O 占比 >95% |
| 002 | 开发用 sqla+sqlite broker，生产用 Redis | Windows 没有官方 Redis；正确性靠看门狗按业务状态兜底，不靠 visibility timeout |
| 003 | Webhook 幂等靠 (project_id, dedupe_key) 唯一索引，不靠 Redis SETNX | 缓存解决性能，约束解决正确性；重复投递返回 200 而非 409 |
| 004 | 不上 Neo4j，用邻接表 + 递归 CTE | 5000 实体/2 万边规模下 3 跳 20–80ms，图数据库是过度设计 |
| 005 | 不 clone 全量，主路径用平台 Compare API | 单次分析 30s+ → 1–3s；clone 作为降级路径 |
| 006 | 人工改过的文档，AI 只能写候选（status=stale），accept_ai 才切换 | 文档所有权是状态机，不是布尔值 |
| 007 | PlantUML 校验必须离线可跑（镜像内 JRE + jar），远程只做复核 | 闸门依赖外网等于没有闸门 |
| 补充 | current_revision 与 latest_revision 分离；人改文档重复候选用 hash 去重 | 否则评审界面会堆一串一模一样的"待确认" |

两条从真实数据里长出来的规则（阶段七）：

- **M005**：Markdown 正文 <40 字符判 error。因为模型真的会只回一个 31 字符的标题，
  而 M001–M004 全过。
- **修复提示词按 kind 分支**：否则 Markdown 进入修复轮会被要求输出 PlantUML，三轮全废。

---

## 7. 运行手册

### 7.1 本地 Windows（开发默认 SQLite）

~~~powershell
# 依赖（本机 pip 因 schannel 证书问题不可用，必须走 uv）
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt

# 测试
.\.venv\Scripts\python.exe -m pytest tests -q

# 起 API（一个终端）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 起 worker（另一个终端；Windows 必须 threads）
.\.venv\Scripts\celery.exe -A app.celery_app worker --pool=threads --concurrency=4 -Q analysis --loglevel=info

# 发一条假 webhook（离线可用 --clone-url file:///...）
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe scripts\send_test_webhook.py --repo octocat/Hello-World --before <sha1> --after <sha2>

# 演示与评测
.\.venv\Scripts\python.exe scripts\demo_phase4.py --live
.\.venv\Scripts\python.exe scripts\live_eval.py --runs 2
~~~

### 7.2 Docker 全栈（PG + Redis + prefork）

~~~powershell
# 构建运行镜像（默认 release 阶段）与测试镜像
docker build -t livingdoc:slim .
docker build --target test -t livingdoc:test .

docker compose up -d --build
docker compose ps
docker compose logs -f api worker
curl http://127.0.0.1:8000/healthz
docker run --rm livingdoc:test sh scripts/container_smoke.sh
docker compose down        # 保留数据卷；down -v 连数据一起删
~~~

### 7.3 关键环境变量

| 变量 | 用途 | 本地默认 | 容器 |
|---|---|---|---|
| DATABASE_URL | 持久化 | sqlite:///var/livingdoc.sqlite | postgresql+psycopg://…@postgres:5432/livingdoc |
| BROKER_URL | 队列 | sqla+sqlite:///var/celery-broker.sqlite | redis://redis:6379/0 |
| RESULT_BACKEND | 结果 | db+sqlite:///… | redis://redis:6379/1 |
| LLM_API_KEY | 模型密钥 | 空（空则 generate_run 走 skipped） | 从 .env 注入 |
| LLM_BASE_URL | 兼容端点 | https://api.deepseek.com | 同左（可覆盖） |
| LLM_MODEL | 模型名 | deepseek-chat | deepseek-v4.1-flash-1017 |
| PIPELINE_ENABLED | 是否自动入队 | true（测试里 false） | true |
| PUBLISH_TARGETS | 发布渠道（逗号分隔） | local_wiki | local_wiki |
| KB_DIR | 本地知识库根目录 | var/kb | /app/var/kb |
| PUBLIC_BASE_URL | PR 评论里的评审链接前缀 | http://127.0.0.1:8000 | 部署域名 |
| KB_WEBHOOK_URL / KB_WEBHOOK_SECRET | 外部知识库 webhook | 空（skipped） | 按需注入 |

---

## 8. 实测数据（写论文直接引用）

| 指标 | 数值 |
|---|---|
| 单测 | 225 项全绿（宿主 Windows）；容器内 215 项（2026-11-06 镜像构建时的快照） |
| 容器镜像 | livingdoc:slim 581MB（原单阶段 850MB，-31.6%），Python 3.11.18 / JRE 17.0.21 / PlantUML 1.2024.7 |
| 真实模型 | deepseek-v4.1-flash-1017，7 轮 × 3 份 = 21 份 |
| 一次通过率 | 18/21 = 86%（class 7/7、sequence 7/7、api 4/7） |
| token 成本 | 七轮合计 48,883；单轮三份 5,000–8,900；单份均值约 2,330 |
| 单轮耗时 | 18.7–38.1 s（三份文档） |
| PlantUML 独立校验 | 14/14 通过 -checkonly -failfast2 |
| 全栈 e2e | webhook 202 → 15.9s → 3 份 published v1，tokens_out=5722 |
| 评审 API 压测（SQLite，读） | 120 请求 / 并发 8，0 失败，RPS 191.96，p95 70.34ms |
| 评审 API 压测（SQLite，读写混合） | 60 请求 / 并发 6，0 失败，RPS 67.25；publish p95 498.82ms |
| 评审 API 压测（Compose + PG，读） | 200 请求 / 并发 20，0 失败，RPS 31.19，p95 1173.37ms |
| 评审 API 压测（Compose + PG，混合） | 100 请求 / 并发 10，0 失败；publish 55/55 success，p95 1224.21ms |
| PG 并发人工编辑 | 8 并发 PUT 同一文档，8/8 200，版本号连续无重复 |
| 真实仓库回归（psf/requests） | commit 6f66281（3 文件、7 个变更函数）：81.4s，6 份文档全部 published，7/7 投递成功，0 rejected |
| 真实仓库 API 文档完整性 | 4/4 个变更函数各一节，v1 3,092 字符；修复前只写 1/4 |
| 真实仓库纯格式化提交 | commit 661970d1：4.0s 识别为 no semantic changes，documents=0（不重写） |
| 阶段五演示 | 3 篇 created → unchanged → human_edit → deferred_stale → accept_ai |

---

## 9. 雷区（这些坑都真实踩过，别再踩）

### 9.1 Windows / 工具链

1. **tree-sitter 0.26 的 Windows wheel 在行内列号 >255 时物化 Point 会崩**（exit 0xC0000005，无 traceback）。
   解法：extractor.py 里一律不用 Node.start_point/end_point，用 _LineIndex + bisect。不要"优化"回去。
2. **pip 不可用**：schannel 报 SEC_E_NO_CREDENTIALS。用 uv，且 UV_CACHE_DIR 指到工作区内。
3. **git file:// 传输在 Windows 会 fork sh.exe 失败**，本地仓库测试要绕开（容器内 file:// 正常）。
4. **本地路径含中文**（Desktop\软件工程\大作业）：cmd 重定向日志会静默失败，日志写 %TEMP%。
5. **subprocess text=True 在 Windows 按 GBK 解码**，源码里中文注释会抛 UnicodeDecodeError
   （而且是在 reader 线程里抛，主线程只看到 stdout=None）。读 git 输出必须 capture_output + decode utf-8。

### 9.2 DSH 会话 / Docker

6. **沙箱默认禁止访问 Windows 命名管道**，docker CLI 报 permission denied。需要 danger-full-access。
   wsl --status 返回 Wsl/E_ACCESS_DENIED 也是同一原因，**不代表 WSL 没装**。
7. **run_code 有约 120s 硬上限，且会杀掉子进程树**（detached 也杀）。
   长构建只能用 buildx 缓存分多次跑；计划任务 schtasks 实测不可靠（报成功但不执行）。
8. **buildx 报 "component is missing" 的真因常是子进程环境缺 USERPROFILE/HOME**，
   不是没装插件。compose 插件同理（~/.docker/cli-plugins）。
9. **容器内访问 github.com 超时**（国内网络），端到端测试用本地裸仓库 + file://；
   模型端点（阿里云）容器内可达。
10. **headless JRE 不等于无图形依赖**：PlantUML -checkonly 仍需 libfreetype6/libfontconfig1/libharfbuzz0b。
11. **worker 不能继承镜像的 HTTP healthcheck**，否则永远 unhealthy。
12. **python scripts/xxx.py 的 sys.path 是 scripts/**，容器里要 ENV PYTHONPATH=/app。

### 9.3 代码语义

13. **修复提示词必须按 kind 分支**：api 文档走 Markdown 契约，否则会被要求输出 @startuml（M001）。
14. **Markdown 校验有 M005**：正文 <40 字符是 error。不要为了"让测试过"把短文档判合格。
15. **Celery apply_async 的真实签名**：apply_async(args=None, kwargs=None, task_id=None, **options)，
    测试替身必须照抄形参名，否则任务入参取不到。
16. **publish_drafts 允许传单个 ModuleDocumentation**（已做 isinstance 归一化），
    但新代码最好统一传 tuple。
17. **人改文档的 AI 候选要按 hash 去重**（_latest_ai），否则重复跑会堆相同版本。

### 9.4 评审与投递

18. **知识库并发写不能共用固定 `.tmp` 文件名**：先完成 `os.replace` 的线程会把 tmp 改名走，
    后一个线程 `FileNotFoundError`；Windows 上还会因为目标被读而 `PermissionError`。
    用 `tempfile.mkstemp` + 进程内 `_WRITE_LOCK` + 短暂重试。
19. **人工编辑的版本号冲突要重试，不要直接 500**：`MAX_PUBLISH_ATTEMPTS=8`，
    每次重试重新读 current 再算 version；SQLite 与 PG 都可能撞。
20. **投递失败不能让 run 失败**：PR 评论 502 是外部故障，run 已经生成并入库；
    失败只写 `publish_records`，`generate_run` 的返回里单独给 `delivery` 统计。
21. **`publish_records.document_id` 可空**：run 级失败（拿不到具体文档）也要能审计。

### 9.5 上下文工具

22. 上一次会话的 compress 工具持续 413；本次会话已恢复正常。若再遇到，直接继续做事，
    别反复重试（每次失败都会让 payload 更大）。

### 9.6 真实仓库回归（2026-11-09）

23. **纯格式化提交不要重写文档**：`semantic_hash` 不含注释/换行，`needs_redoc=False`
    会让 prompt payload 为空，模型只能拿实体名硬编。`generate_module` 在 redoc 为空时
    直接返回 `drafts=()`，`changed_members` 也跳过 formatting-only。
24. **Markdown 先有标题、再出现内部代码块时，不能把第一个围栏当外层包装剥掉**：
    `extract_markdown` 只在"首个围栏前无标题 + 末个围栏后无内容"时才剥；否则会把
    正文前半截连同多个 `###` 小节一起丢掉，触发 M005/M001。
25. **API 文档的 W005 是 error**：每个变更函数必须有一节；类图/时序图仍是 warning，
    允许合并次要成员。提示词里同步写死 "every changed function"，否则模型只写一个。
26. **顶层函数不要拼成 `owner.name`**：`changed_members` 旧逻辑 `owner=parts[0]`
    会产出 `has_read.has_read`，校验器永远找不到。
27. **`CELERY_TASK_ALWAYS_EAGER` 与 `TASK_ALWAYS_EAGER` 都要认**：字段名是
    `task_always_eager`，pydantic 默认不吃 CELERY_ 前缀；用 AliasChoices + populate_by_name。
    路径类配置（KB_DIR 等）必须在 Settings 里解析成绝对路径，否则 `.as_uri()` 抛
    `relative path can't be expressed as a file URI`。

---

## 10. 未完成事项（2026-11-09 更新）

除用户明确跳过的多模型对比外，原 §10 的功能项均已完成；2026-11-09 补了真实仓库回归：

| 原优先级 | 事项 | 状态 | 证据 / 入口 |
|---|---|---|---|
| P0 | GLM-4 横向对比 | ⏸ 本轮不做（用户要求） | — |
| P1 | PR 评论 / 知识库推送 | ✅ 完成 | app/publishers/、14+1 项测试、docs/08-phase8-delivery.md |
| P2 | Web 评审界面 | ✅ 完成 | app/api/review.py、app/static/、16 项 API 测试 |
| P2 | PostgreSQL 压测与并发 publish | ✅ 完成 | scripts/load_test.py、tests/test_concurrency.py、§8 数据 |
| P3 | 镜像瘦身（多阶段） | ✅ 完成 | 850MB → 581MB，Dockerfile 五阶段 |
| P3 | api_doc 空内容重生成 | ✅ 完成 | self_correct 的 M003/M005 重新生成分支 + 测试 |
| P1 | 真实 GitHub 仓库回归 | ✅ 完成 | psf/requests @ 6f66281：6 文档 / 0 拒稿 / 7-7 投递，§8 |

仍然值得做、但不属于本轮验收的事：

| 优先级 | 事项 | 为什么值得做 | 起点 |
|---|---|---|---|
| P1 | GLM-4 横向对比 | 作业点名"国产工具"，两模型通过率/修复率对比是论文素材 | live_eval.py 加 --model 参数，配置第二个 base_url |
| P2 | 大仓库压力样本 | 当前压测是演示数据；真实大 diff 的解析/生成耗时未知 | 找 1k+ 文件的仓库跑 analyze_push |
| P3 | 前端 e2e | 目前只验证 API + 静态资源，没有浏览器自动化 | Playwright 或手工回归清单 |
| P3 | SSE 实时进度 | api.md 设计里有，当前用轮询 | review.py 增加 `/runs/{id}/events` |

---

## 11. 交接注意

1. **密钥只在 .env**（已在 .gitignore）。不要写进代码、文档、测试、日志。它已经出现在聊天记录里，
   建议在供应商控制台轮换一次；换 key 只改 .env，代码不用动。
2. 当前 Docker 栈可能还在运行（api/worker/postgres/redis）。先 docker compose ps 看一眼，
   不用就 docker compose down。镜像默认目标已改为 release，重新构建后是 581MB。
3. 仓库不是 git 仓库（没有 .git），所有产物直接落盘；如果要提交，先 git init 并确认 .gitignore。
4. var/ 下是运行时产物（work/、repos/、live/、kb/、renders/、phase4/、phase5/），已被 .dockerignore 与 .gitignore 排除。
5. 改 Dockerfile 后必须重建镜像再跑 compose；改 app 代码后 compose 需要 --force-recreate 才会用新镜像。
6. 写文档时不要用反引号嵌套进模板字符串（会截断），这是工具层面的坑，不影响代码。

---

## 12. 一页速查

**五分钟自检**

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests -q          # 期望 288 passed
docker compose ps                                       # 期望 api healthy / worker Up
curl http://127.0.0.1:8000/healthz                      # {"status":"ok","env":"dev","queue":"redis"}
docker run --rm livingdoc:test sh scripts/container_smoke.sh   # 期望 SMOKE PASSED
~~~

**关键数字**：288 测试 / 581MB 镜像 / 86% 一次通过 / 48,883 tokens / 14/14 PlantUML / 15.9s 全栈 e2e / 真实仓库 6 文档 0 拒稿。

**最该读的三篇**：docs/05-phase5-pipeline.md（状态机与闸门）、docs/06-docker.md（三条 ADR 的实证）、
docs/07-live-llm.md（真实失败率与两个 bug）；新功能看 docs/08-phase8-delivery.md。

---

## 13. 2026-11-09 第二轮：结构感知的类图 / 时序图（v4）

### 13.1 为什么要动

v3 的类图经常是"一排孤立类框"：提示词只说"用输入里的类"，而输入里只有变更函数列表，
模型根本没有基类、字段类型、调用关系可画。时序图同理，画出 `A -> B : process` 这种泛泛一句，
没有返回箭头、没有 alt/loop。结论：**不是模型不会画，是输入里没有可画的边。**

### 13.2 做法：先抽结构事实，再喂提示词，再用校验器兜底

| 层 | 改动 | 关键位置 |
|---|---|---|
| 解析 | `Symbol` 增加 `owner/bases/base_roles/class_attributes/returns/calls/branches/is_async`；`FileDiff.new_symbols` 带整文件符号 | app/parsers/model.py、app/parsers/extractor.py、app/pipeline.py |
| 结构 | 新模块 `app/parsers/structure.py`：类/函数/关系/调用链/分支/入口点，`render()` 成"ground truth"文本块 | build_structure() |
| 提示词 | `_RELATION_RULES`（关系不是可选项）、`_SEQUENCE_SUBSET` 五步法 + depth-two 预算、free function 用 `<<function>>` 不编假类框、few-shot 换成带继承+组合的例子 | app/ai/prompts.py |
| 校验 | E013 孤立类 / E014 缺期望关系 / W008 箭头种类不符 / E015 无返回 / E016 缺调用边 / E017 缺控制流块 / W009 占位参与者 / W010 缺对应返回 / W011 深层边未画（仅提示） | app/ai/validator.py |
| 编排 | 类图拿 `expected_relations`，时序图拿 `expected_calls`（前两跳必需）+ `advisory_calls`（封顶 4 条，仅 W011） | app/ai/service.py |

关键判断：**归一到参与者名再比对**。图里写的是 `UserService -> UserService : load(...)`
（自调用），而调用链是 `UserService.deactivate -> UserService.load`；按限定名比对永远不中，
真实链路里这条 E016 误杀了整份时序图。`_participant_name()` 取倒数第二段解决。

### 13.3 复杂样例仓库（任务 4 素材）

`var/demo-complex/`（推送到 aaa10001/Test_LivingDOc）：

- `src/domain/base.py`：`DomainError/NotFound` 异常层次、`@dataclass Entity` → `User` → `AdminUser`、
  `Repository(ABC)` → `UserRepository`（持 `TTLCache`，find/save/delete/by_email）、`TTLCache`。
- `src/services/user_service.py`：`BaseService.retry`（for+try+except）、`UserService`（继承 + 组合
  UserRepository/TTLCache/AuditLog，load/register/promote/require/deactivate）、`SyncService`
  （`async sync` / `async sync_with_retry`，退避重试）、`AuditLog`。
- `src/api/routes.py`：`handle_request` 入口（/health、POST /users、DELETE /users/{id}、GET /users/{id}，
  try/except 三类异常）、`user_id_from`、`health_check`、`create_user/get_user/promote_user/to_payload`。

### 13.4 真实链路结果（8001 服务，DB=var/github-user-zh.sqlite）

| run | 变更 | 结果 |
|---|---|---|
| 8e88468f | 首推三模块（a73d515→920a6ce） | 7 文档 / 7 发布 / **2 拒稿**（两条时序图 E016） |
| ab635b35 | UserService.deactivate | 3/3 发布，0 拒稿，但类图字段无类型（当时只吃变更符号） |
| 1c8c45be | DELETE 路由 | 3/3 发布；类图出现假类框 `class "src.api.routes" as Routes`（W001/W002） |
| d82c5f43 | /health + user_id_from | 3/3 发布，0 拒稿；src.api 时序图有 alt/group 嵌套、每条调用都有返回 |
| bb5007a2 | AuditLog 组合 | 2/2 发布 + **1 拒稿**（src.services 时序图被 tokens 截断 → E001） |
| fa026973 | count/stats（cc23ef6→18b7df4） | **9/9 发布，0 拒稿**；src.services 一次过（0 修复轮），src.domain 1 轮修复 |

fa026973 是最终验收轮，三个模块 × 三份文档全部发布。修复前的两个根因（都已在真实链路上验证消失）：
1. 截断：`too_long=True` 时修复提示词要求"≤5 参与者 / ≤14 消息"，并整张重画；可选边封顶 4 条。
2. E016 误判：`_participant_name()` 归一化（`UserService.deactivate -> UserService.load` → `UserService -> UserService`）。

同一轮还补了跨模块 callee：字段类型明确（`self.repo: UserRepository`）但方法定义在别的模块时，
`_resolve` 通过 `_external_callee()` 合成 `UserRepository.count` 节点，于是
`UserService.stats -> UserRepository.count` 这条边不再整条消失（时序图里能看到 `svc -> repo : count()`）。

产物抽样（module:src.services，v4-2026-11）：

```
UserService --|> BaseService : 继承
UserService *-- UserRepository : 组合
UserService *-- TTLCache : 组合
UserService *-- AuditLog : 组合
UserService ..> AdminUser : 依赖
```

时序图从入口 `stats()` 起画：`调用方 -> svc : stats()`、`svc -> repo : count()`、
`svc -> audit : recent(limit=5)`、每条调用配一条 `-->` 返回、末尾 `alt active < 0` 两分支。

### 13.5 文档回写

`scripts/sync_github_docs.py` 跑过两次：
- `9e4a6e925a3641ca404c723b1a461b3ed84b70be`（9 份，run d82c5f43）
- `50ece29eea8ca8c68679ce166daf4c83f490b08e`（10 份有变更，run fa026973）—— 当前 main HEAD

仓库 main 分支现在是「代码 18b7df4 + 文档 50ece29」两个 commit。文档目录结构：
`docs/livingdoc/README.md` + `{api_doc,class_diagram,sequence_diagram}/module-src.{api,domain,services}.md`。

### 13.6 浏览器巡检（bsk）

`scripts/bsk_ui_audit.ps1` + `scripts/ui_shot_audit.py`：桌面 1600×1200 / 移动 iPhone 14 两档，
`/ui/showcase.html` 与 `/ui/` 均无横向溢出、无 console 报错、a11y 计数全 0、对比度 15.37，
截图落在 `var/ui-audit/`（报告 report.json）。bsk 安装位置：`.bsk-cli\bsk.exe`、扩展 `.bsk-ext\`、
`BSK_HOME=<repo>\.bsk-home`（默认 ~\.bsk 与命名管道在受限沙箱下不可用）。

收尾复检（新文档落库之后）：`/ui/showcase.html` 桌面 docHeight 3472，列出 **12 份文档 / 4 个 target**
（`module:src` 旧版 + `module:src.api` / `src.domain` / `src.services`），移动端 390×844 无横向溢出
（scrollWidth == clientWidth == 390）；console 仅剩 `chrome-extension://invalid/` 一条扩展自身噪音。
截图 `var/ui-audit/showcase-final-top.png`。

### 13.7 收尾状态（2026-11-09 18:55）

四项任务全部完成，无遗留阻塞。提交链：

```
a73d515  docs（v3 时代的 4 份文档）
920a6ce  feat(src): 多模块样例（8e88468f 拒稿 2）
715dcde  deactivate（ab635b35）
685cdb1  DELETE 路由（1c8c45be）
faaa9be  /health + user_id_from（d82c5f43）
9e4a6e9  docs（9 份）
cc23ef6  AuditLog（bb5007a2 拒稿 1）
18b7df4  count/stats（fa026973，9/9 发布）
50ece29  docs（10 份变更）← main HEAD
```

复盘用的一句话：拒稿率从 2/7 降到 0/9，靠的不是换模型或加温度，而是把 AST 里本来就有、
但没进提示词的**结构事实**（基类、字段类型、调用边、控制流）抽出来当 ground truth，
再用 E013–E017 把"画没画出来"变成可判定的错误。

下一轮若要继续（都不是必须的）：
1. 把 `app/parsers/structure.py` 的 `max_call_depth=4` 做成按图大小自适应（现在靠 `_split_chain_edges` 事后裁剪）。
2. `W011` 目前只在不影响发布的前提下提示，可以考虑在文档末尾自动列出"未画出的深层边"。
3. showcase 页可以加一个"结构事实 vs 生成图"的对照视图，答辩时很有说服力。
