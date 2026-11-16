# 阶段五：从"能生成"到"能上线"——持久化、发布闸门与三段式流水线

> 阶段四结束时，系统能对一段 diff 产出三份合格文档，但产物只落在 var/phase4/ 目录里，
> 进程一退就没了。阶段五要回答的问题是：**文档存在哪、谁有权改、AI 与人同时改同一份文档时谁赢。**
>
> 本阶段新增 37 项测试，全套 176 项全绿。演示脚本：scripts/demo_phase5.py。

---

## 1. 交付清单

| 文件 | 职责 | 行数 |
|---|---|---|
| app/store.py | 5 张表的读写、run 状态机、幂等、看门狗查询 | ~430 |
| app/publish.py | 文档版本写入、ADR-006 人工修改闸门、接受 AI 候选 | ~350 |
| app/pipeline.py | 解析/生成/发布的纯函数编排，artifact 序列化 | ~300 |
| app/tasks.py | analyze_push → parse_run → generate_run 三段任务 + watchdog（重写） | ~380 |
| app/config.py | 新增 database_url、pipeline_enabled | +2 行 |
| tests/test_store.py | 状态机、幂等、看门狗查询 | 10 项 |
| tests/test_publish.py | 版本与闸门语义 | 12 项 |
| tests/test_pipeline.py | 跳过原因、artifact 往返、单模块端到端 | 10 项 |
| tests/test_tasks.py | 三段接线、重复投递、无 key 降级、看门狗重排 | 5 项 |
| scripts/demo_phase5.py | 真实 git + 真实 AST + 剧本模型 + 真 SQLite 的完整演示 | ~300 |

数据流从阶段四的"一次函数调用"变成三段：

~~~
POST /webhook/github
      │ 202 + task_id
      ▼
analyze_push   git 取 diff ──► var/work/runs/<run_id>.json（粗粒度 file 列表）
      │ enqueue
      ▼
parse_run      tree-sitter ──► var/work/runs/<run_id>.changes.json（函数级 ChangeSet）
      │ enqueue
      ▼
generate_run   LLM + 校验 ──► documents / document_revisions（SQLite 或 PG）
~~~

---

## 2. 为什么拆成三段，而不是一个大任务

这是本阶段第一个被认真讨论的设计问题。阶段二到四的代码本来可以串在一个 Celery 任务里：
拿到 job 就一直跑到文档落库。我最初也打算这么做，理由是"少两次序列化"。

拆开的理由最终压过了它，按重要性排序：

1. **失败重试的经济性不同。** git 克隆 30 秒起、LLM 生成每份文档 5–20 秒、解析 100ms 级。
   如果一个大任务在第三步失败，重试要重新克隆、重新 diff。拆段之后，parse_run 失败只需重跑解析，
   generate_run 失败只需重跑生成。
2. **中间产物是排障证据。** 阶段四的教训是"模型输出不合格时必须能回看原始输入"。
   两段 artifact 落盘后，任何人可以打开 .changes.json 看"到底喂了模型什么"，
   不必重放整条链。
3. **并发模型不同。** 克隆是网络 I/O、解析是 CPU、生成是网络 I/O 且受服务端限流。
   拆开后可以给 analysis 队列和 generation 队列配不同的并发度，而不是让慢任务占住 worker。
4. **状态机才有意义。** 一个大任务只有"跑着/成功/失败"，拆段之后 analysis_runs.status
   能精确表示卡在哪一步，这也是看门狗（§5）能按业务状态重排的前提。

代价也如实记录：两次任务投递、两份 JSON 落盘、run 状态需要显式推进。
这些代价换来的是**可定位**，我认为值。

---

## 3. 状态机：让"卡住"变成一种可查询的状态

analysis_runs.status 共 12 态，与阶段一的 database.md 一致：

~~~
queued → cloning → diffing → parsing → analyzing → rendering → publishing → succeeded
   │        │         │         │          │           │           │
   └────────┴─────────┴─────────┴──────────┴───────────┴───────────┴→ failed / canceled / superseded / skipped
~~~

三条实现约束：

1. **非法迁移抛异常，不写库。** ALLOWED_TRANSITIONS 显式列出每条边；cloning 直接跳 succeeded
   会抛 InvalidTransition。好处是"状态错乱"这类 bug 在第一次出现时就炸在调用点，而不是变成
   一份状态诡异的数据在报表里躺三个月。
2. **同状态重复迁移视为幂等。** Celery 是 at-least-once：任务可能被执行两次。第二次进
   parsing 时不该报"非法迁移"。因此 transition_run 遇到 status 与当前相同，只更新传入的字段
   （stage_detail、计数），不报错。
3. **时间戳由状态推导，不由调用方传。** 非 queued 且 started_at 为空时写 started_at；
   failed 或任何终态写 finished_at。调用方少一个可以传错的参数。

有一个迁移是阶段四之后才补的：**analyzing → skipped**。原因是"没配 LLM key"不是错误，
是环境没准备好；标 failed 会让看门狗反复重试一个永远不会成功的任务，也会污染失败率指标。
skipped 表示"这一步主动跳过了"，并在 error_code 里写 llm_not_configured，人一看就知道该干什么。

---

## 4. 幂等：同一份 diff 只应该有一个 run

ADR-003 在 Webhook 入口用 (project_id, dedupe_key) 唯一索引挡住了重复投递。
但那只挡"同一次平台重试"。阶段五多了一个入口：看门狗重排、人工重放、Celery 至少一次，
都可能让**同一个 base..head** 再次进入队列。

start_run 的做法是给 run 也算一个幂等键：

~~~python
run_idempotency_key(project_id, base_sha, head_sha, trigger_type)
    = sha256(f"{project_id}|{base_sha}|{head_sha}|{trigger_type}")
~~~

(project_id, idempotency_key) 上有唯一约束；start_run 先查后插，返回 (run_id, created)。
analyze_push 拿到 created=False 时**直接返回 duplicate_run，不执行任何 git 操作**。

为什么必须停在入口：如果两个 run 并行处理同一段 diff，它们会各自生成一份文档、
各自 publish，后写的覆盖先写的。如果期间人改过文档，还会触发两次 deferred_stale，
在评审界面里出现两份一模一样的候选。**幂等不是性能优化，是正确性要求。**

以下情况仍然会产生新 run，这是有意的：
- 不同 head_sha（哪怕只多一个提交）；
- 不同 trigger_type（push 与 pr 对同一对 sha 的语义不同，PR 可能需要评论）；
- 同一对 sha 但人工显式要求 force（当前未做接口，预留在 trigger_type 上）。

---

## 5. 看门狗：按业务状态判断"卡住"，而不是按队列

ADR-002 记录了 SQLite 作为 broker 时没有真正的 visibility timeout。
阶段五的 watchdog 是对这条缺陷的正面回应，判据完全在业务侧：

~~~sql
SELECT * FROM analysis_runs
WHERE status NOT IN ('succeeded','failed','canceled','superseded','skipped')
  AND started_at IS NOT NULL
~~~

再在 Python 侧筛 started_at < now() - 15 分钟（SQLite 读回的是 naive datetime，
统一补 UTC 再比较，否则 TypeError）。

命中后：attempt+1，写 stage_detail="requeued by watchdog (attempt N)"，
按当前状态决定重投哪一段：queued/cloning/diffing → analyze_push；parsing → parse_run；
其余 → generate_run。

这条设计比 visibility timeout 更可靠的地方在于：**它不关心消息在哪、是否被 ack**，
只关心"这行业务数据已经 15 分钟没动了"。Redis 换回来之后这个看门狗也应该保留——
消息队列的超时语义与业务超时语义本来就不是一回事。

---

## 6. 持久层：为什么用 SQLAlchemy Core 而不是 ORM

这一层只有 5 张表、十几条语句，却要跑在 Celery 的线程池里。选 Core 的三条理由：

1. **操作意图一眼可见。** ORM 里 update 一行要先 get、改属性、再 flush，中间隔着 session
   生命周期；Core 里就是一条 UPDATE，谁在什么时候改了哪几列，读代码时不用在脑子里维护 identity map。
2. **session 生命周期在长驻 worker 里是负担。** detached instance、lazy load、autoflush
   在"任务进来、开连接、干完、关连接"的模式下只会增加不确定性。
3. **迁移成本可控。** 表结构先用 Core 的 metadata 描述，上 PG 时把类型换掉即可；
   database.md 里的 PG DDL 仍然是生产形态的权威定义。

SQLite 与 PG 的差异在 store 里集中处理：

| 关注点 | SQLite 实现 | 生产 PG |
|---|---|---|
| uuid | char(36) 字符串 | uuid + gen_random_uuid() |
| 数组 | JSON 文本 | text[] 或 jsonb |
| 时间 | datetime（读回 naive） | timestamptz |
| 自增主键 | INTEGER PRIMARY KEY | bigserial / identity |
| 并发 | PRAGMA journal_mode=WAL + busy_timeout=5000 | MVCC |
| 部分索引 | 未使用 | ix_runs_active WHERE status NOT IN (...) |

连接参数 connect_args={"check_same_thread": False, "timeout": 30} 是线程池必需的；
WAL 与 busy_timeout 在 connect 事件里设，保证每个新连接都生效。

---

## 7. 发布闸门：ADR-006 的落地规则

publish_document 是"AI 写文档"的唯一入口，行为按以下顺序判定（顺序本身就是设计）：

| 序 | 条件 | 结果 | action |
|---|---|---|---|
| 1 | 文档不存在 | 建 documents + v1，current 指向 v1 | created |
| 2 | 未人改 + 内容 hash 相同 | 不写任何行 | unchanged |
| 3 | 已人改 + 与**上一个 AI 版本** hash 相同 | 不写任何行 | unchanged |
| 4 | 已人改 | 写新版本，status=stale，current 不动 | deferred_stale |
| 5 | 未人改 + 内容变化 | 写新版本，current 移动 | updated |

三处值得单独说明：

**内容比较必须先归一化。** 平台之间换行符不同（GitHub 返回 CRLF、本地文件常是 LF），
如果不统一，第一次发布之后每次都会"检测到变化"并产生新版本。
normalize_content 做三件事：CRLF→LF、去尾部空白、补一个换行。hash 在归一化之后算。

**规则 3 是阶段五新加的。** 最初版本只有规则 4：人改过的文档，AI 每次都写一份候选。
测试里发现，连续两次用同样的模型输入重跑，会得到 v3、v4 两份内容完全相同的候选，
评审界面里堆一串一模一样的"待确认"。于是补了 _latest_ai：人改文档时拿最近的 AI 版本
比 hash，相同就 unchanged。这是我改代码，不是 AI 改——因为 AI 当初只被要求"实现 ADR-006"，
没有意识到 ADR-006 与"重复执行"叠加后的这个后果。

**accept_ai 之后 manual_edited 保持 True。** 接受 AI 内容是人做的一次动作，
不是解除保护。下次 AI 更新仍然只写候选、仍需人看一眼。保护标志只在显式 API 调用时解除。
这一条在测试里被固定下来（test_accept_ai_switches_current_but_keeps_protection），
因为它最容易被后来的重构"顺手"改掉。

---

## 8. 编排层：纯函数 + 依赖注入

pipeline.py 里没有全局状态，也没有直接 import store 或 client：

~~~python
parse_changes(*, files, read_blob, extractor=None) -> ChangeSet
generate_documents(*, change_set, client, max_attempts=3, model_name="") -> tuple[ModuleDocumentation, ...]
publish_drafts(store, *, project_id, run_id, docs, change_summary=None) -> PublishReport
~~~

read_blob 是一个 (which, path) -> str 的回调，which ∈ {"base","head"}。
好处是解析层完全不知道 git 的存在：测试里传一个字典，任务里传 read_blob(repo_dir, sha, path)。

parse_changes 的每个跳过原因都写进 skipped_files，而不是静默丢弃：

| 原因 | 触发条件 | 为什么不用异常 |
|---|---|---|
| unsupported language | 扩展名不在 profiles 里 | 一个仓库里总有 yaml/json/md |
| unsupported git status 'X' | 除 A/M/D 之外的状态 | 重命名等尚未支持，跳过比猜语义安全 |
| read failed: ... | git show 失败 | 单个文件读不到不该毁掉整次分析 |
| both versions missing | 两边都取不到内容 | 空 diff 的边界 |
| file too large | 超过 2MB | 防 OOM，也防把巨文件喂给模型 |
| no symbol changes | 只有注释/空白变化 | 这才是"真的不需要更新文档" |
| syntax error tolerated (partial AST) | 解析有 ERROR 节点 | 半棵树也比没有强，但要标记 |

artifact 带版本号（ARTIFACT_VERSION=1），load 时版本不符直接 ValueError。
这样升级解析器之后，旧的中间产物不会被新代码误读成新格式。

---

## 9. 任务层：三段各自的契约

| 任务 | 输入 | 成功返回 | 失败处理 |
|---|---|---|---|
| analyze_push | job dict（delivery_id/repo_full_name/base_sha/head_sha/...） | status, run_id, files, modules, artifact, enqueued | GitError 可重试则 self.retry(2^n 秒)，否则 fail_run(git_error) |
| parse_run | run_id | status, files, entities, changes_artifact | 异常 → fail_run(parse_error) |
| generate_run | run_id | documents, published, deferred, rejected, outcomes | 异常 → fail_run(generation_error) |
| watchdog | older_than_minutes=15 | requeued 列表 + count | — |

统一约定：
- run 不存在 → failed；run 已是终态 → {"status": "ignored"}（幂等，不重复干活）。
- artifact 缺失 → fail_run(artifact_missing)，因为这说明前一段没跑完，重试这一段没有意义。
- 没有变更 → transition skipped，error_code 说明原因，不当作失败。
- generate_run 发现 llm_api_key 为空 → skipped + llm_not_configured，**并把已解析的
  changes.json 保留下来**。这是"降级但可用"：人配好 key 之后可以直接重跑生成，不用重新克隆。

---

## 10. 测试：176 项怎么分布

| 阶段 | 文件 | 项数 |
|---|---|---|
| 二 | test_security / test_dedupe / test_git_handler / test_webhook | 40 |
| 三 | test_parsers | 26 |
| 四 | test_validator / test_prompts / test_ai_client / test_self_correct / test_service | 73 |
| 五 | test_store / test_publish / test_pipeline / test_tasks | 37 |
| 合计 | | 176 |

阶段五这 37 项里，真正有信息量的是这几条：

1. test_store：cloning → succeeded 必须抛 InvalidTransition；所有终态没有出边；
   active_runs 只报"未终结且 started_at 已过期"。
2. test_publish：连续两次相同内容 → unchanged 且版本数不变；
   人改后 AI 更新 → current 仍指人改那版、latest 是 AI 候选；
   人改文档重复同一 AI 内容 → unchanged，不堆候选。
3. test_pipeline：超 2MB 文件被跳过且原因正确；artifact 往返保留 body/signature/params；
   版本号 99 抛 ValueError；单模块端到端一次产出 3 篇文档。
4. test_tasks：一次 analyze_push.run 跑完整条链（parse_run.delay 被换成同步调用）；
   重复投递返回 duplicate_run 且没有第二次解析；无 key 时 run 停在 parsing、
   手动跑 generate_run 得到 skipped；watchdog 把 45 分钟前的 parsing run 重排且 attempt=2。

---

## 11. 实测：一次真实的提交如何变成三份文档

运行：

~~~powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONPATH=(Get-Location).Path
.\.venv\Scripts\python.exe scripts\demo_phase5.py
~~~

脚本用 git init 造一个两提交的仓库（不经网络，避开本机 file:// 传输的 sh.exe 问题），
第一次提交建 UserService/UserRepository，第二次提交给 load 加校验、新增 delete。
输出（sha 每次运行不同）：

~~~
git diff: 1 file(s)
  M app/services/user.py  +5 -0
tree-sitter: {"files_analyzed": 1, "files_skipped": 0, "symbols_added": 1,
  "symbols_removed": 0, "symbols_modified": 1, "symbols_renamed": 0,
  "formatting_only": 0, "lines_in_new_version": 6, "lines_in_old_version": 2,
  "redoc_targets": 2}

第一次发布：
  {"documents": 3, "published": 3, "deferred": 0, "unchanged": 0, "rejected": 0}
  created  published v1  45e7b5de   （class_diagram）
  created  published v1  380bd7bc   （api_doc）
  created  published v1  39cf2015   （sequence_diagram）

第二次发布（内容未变）：
  {"documents": 3, "published": 0, "deferred": 0, "unchanged": 3}

人工编辑：human_edit v2，manual_edited=True

第三次发布（文档已被人改过）：
  deferred_stale  stale      v3  45e7b5de
  unchanged       published  v1  380bd7bc
  unchanged       published  v1  39cf2015
  current  = v2 (human)
  latest   = v3 (ai)
  status   = stale

接受 AI 候选：accepted -> current = v3 (ai)
  manual_edited 仍为 True（接受一次不等于解除保护）

run 6033c90a: succeeded  files=1  entities=2
~~~

这段输出同时验证了四件事：内容不变不产生版本、人改后 AI 只能写候选、
current 与 latest 可以分离、accept_ai 才切换。结果同时写进 var/phase5/report.json。

---

## 12. 人机协同记录（本阶段）

| # | 现象 | 谁先发现 | 根因 | 处理 |
|---|---|---|---|---|
| 1 | TypeError: 'ModuleDocumentation' object is not iterable | 测试 | publish_drafts 假设 docs 是序列，而 generate_module 返回单个对象 | 加 isinstance 归一化；测试同时覆盖单对象与元组 |
| 2 | ScriptedLLM ran out of responses after 3 calls | 测试 | 我按"两个模块"写夹具，但每模块固定 3 篇，剧本只给了 3 条；且 REMOVED-only 模块会跳过时序图 | 改用单模块夹具，让调用次数与业务语义对齐（AI 建议"多写几条剧本"，我否了——那是在掩盖夹具与实际调用次数不匹配） |
| 3 | 人改文档连续重跑堆出多份相同候选 | 我 | ADR-006 只规定"人改后写候选"，没规定"候选相同则不写" | 增加 _latest_ai 比 hash；这是设计补丁，不是 bug 修复 |
| 4 | shutil.rmtree 在 .git/objects 上 WinError 5 | 演示脚本 | git 对象文件是只读的 | onerror 回调 chmod 后重试 |
| 5 | KeyError: 'sym_added' | 演示脚本 | 我凭记忆写了 ChangeSet.to_summary 的键名 | 按实际输出改正；教训是跨阶段复用接口时先读代码再写调用 |

第 2 条值得在论文里展开一句：AI 给出的修复（"把剧本加到 9 条"）能让测试变绿，
但会让测试失去意义——它把一个"调用次数与夹具不匹配"的信号改成了"正好够用"。
人类在这里做的是**判断测试该证明什么**，而不是修到不报错。

---

## 13. 边界：本阶段没有证明的事

1. **真实 LLM 仍未调用**（无 API key）。三段链路的模型调用点被剧本模型替换，
   链路正确性与模型质量是两件事。
2. **PostgreSQL 未实测**。store 的 SQLite 路径已验证；PG 只保证 DDL 与类型映射在设计上成立。
3. **跨进程异步未在本阶段重测**。阶段二验证过 uvicorn + Celery(threads) 的真实队列，
   阶段五的 test_tasks 用同步替身验证接线；两者合起来能推出"能跑"，但没有再跑一次
   端到端的真实队列（缺的是一个带 Python 代码的公开仓库做素材）。
4. **文档推送知识库未做**。当前只落库，未接 Confluence/语雀/PR 评论。
5. **并发 publish 的版本竞争只用 IntegrityError 重试兜底**，未做压力测试。

---

## 14. 复现命令

~~~powershell
# 全部测试
.\.venv\Scripts\python.exe -m pytest tests\ -q

# 只跑阶段五
.\.venv\Scripts\python.exe -m pytest tests\test_store.py tests\test_publish.py tests\test_pipeline.py tests\test_tasks.py -q

# 演示（真实 git + 真实 AST + 剧本模型 + 真 SQLite）
$env:PYTHONPATH=(Get-Location).Path
.\.venv\Scripts\python.exe scripts\demo_phase5.py
# 产物：var/phase5/report.json、var/phase5/livingdoc.sqlite、var/phase5/demo-repo/
~~~
