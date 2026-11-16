# 架构决策记录（ADR）+ 人机讨论原文

> 本文件是作业「如何通过人与 AI 协同解决开发问题」的**核心证据**。
> 每条 ADR 记录：背景 → 备选 → **人类质疑与 AI 回应（原文）** → 决策 → 代价。
> 标记说明：🟢 = AI 初始建议被采纳　🟡 = 被修正　🔴 = 被否决

| 编号 | 主题 | 结论 | 谁主导 |
|---|---|---|---|
| ADR-001 | Celery 在 Windows 上跑不起来 | 改用 threads pool，生产上 WSL2 | 🟡 人修正 AI |
| ADR-002 | 消息队列的 Broker 选型 | 开发用 SQLAlchemy(SQLite)，生产用 Redis | 🟢 双方一致 |
| ADR-003 | Webhook 防抖要不要用 Redis | **不用 Redis 做正确性保证**，改数据库唯一键 | 🔴 人否决 AI |
| ADR-004 | 要不要上 Neo4j | 不上，用邻接表 + 递归 CTE | 🔴 人否决 AI |
| ADR-005 | 拉代码用 clone 还是 Compare API | **主路径用 Compare API**，clone 降为 fallback | 🟡 人修正 AI |
| ADR-006 | AI 重新生成会不会覆盖人的修改 | 加 manual_edited 闸门，二期做三方合并 | 🟢 人提出，AI 补方案 |
| ADR-007 | 没有 Java 怎么渲染 PlantUML | 装 JRE 做本地校验 + HTTP 兜底 | 🟡 环境约束倒逼 |

---

## ADR-001　Celery 在 Windows 上的可行配置

**状态**：已决策　**影响**：任务层能否在本机跑通　**日期**：阶段一

### 背景

选题方案里写的是「Celery 或消息队列」。开发机是 Windows 11，无 Docker。

### 备选

1. 原样使用 Celery 默认配置（prefork）
2. `--pool=solo`（单进程串行）
3. `--pool=threads --concurrency=4`
4. 换 Huey / Dramatiq / RQ
5. 整个项目挪进 WSL2

### 讨论

> **AI（初始方案）**：使用 Celery 5.4 + Redis，`celery -A app.worker worker -l info` 即可。

> **人类**：Windows 上直接这么跑会怎样？我记得 Celery 在 Windows 上一直有问题。

> **AI**：你记得没错。Celery 4.x 起官方**明确不支持 Windows**，根因在 billiard 的 prefork 池：它依赖 `os.fork()`，而 Windows 没有 fork 语义，只能靠模拟。实际表现是启动即崩或任务卡死，典型报错是 `ValueError: not enough values to unpack (expected 3, got 0)` 或 `OSError: [WinError 6] 句柄无效`。官方建议是「用 WSL2 或 Docker」，但你的机器上二者都不具备。

> **人类**：那 `--pool=solo` 呢？我看很多人这么绕。

> **AI**：能跑，但是**串行**。对纯 CPU 任务这是灾难，不过对本项目要评估一下：一次 run 的耗时里，`git fetch` 和 LLM 调用占了 95% 以上，都是等待 I/O。串行的真实代价不是"CPU 闲着"，而是"任务 B 必须等任务 A 的 40 秒 LLM 调用结束才能开始"。

> **人类**：那就用 threads。I/O 密集用线程是对的，而且 Python 的 GIL 在等网络时会释放。但我想确认一个风险：threads pool 里如果某个任务抛异常，会不会把整个 worker 带崩？

> **AI**：不会。threads pool 的异常被 task 层捕获后写进 result backend，worker 进程存活；真正危险的是 **C 扩展段错误**（比如 tree-sitter 的 C 库在极端输入下崩），那会带走整个进程。所以需要两层防护：一是 tree-sitter 解析放在独立子进程里跑（`multiprocessing` 或 `concurrent.futures.ProcessPoolExecutor`，超时 10s 强杀），二是 Celery 加 `--max-tasks-per-child` 定期回收。注意：`--max-tasks-per-child` 在 threads pool 下不生效，它是 prefork 的特性 —— 这一条要写进文档，否则会变成一个"配了但没生效"的隐性 bug。

> **人类**：好，那最终形态是什么？

### 决策

| 环境 | 配置 | 说明 |
|---|---|---|
| Windows 本地开发 | `celery -A app.worker worker -l info --pool=threads --concurrency=4` | 够用；不追求生产级 |
| WSL2 / Linux（答辩演示可切） | 默认 prefork，`--concurrency=2 --max-tasks-per-child=50` | 展示"同一份代码，部署形态可切换" |
| 生产 | Docker + prefork + 多副本 | 论文里说明这是目标形态 |

**tree-sitter 解析隔离**：无论哪种 pool，解析都在独立进程池里执行，超时强杀。这是"第三方 C 扩展不可信"的通用防御，不只是为了 Celery。

### 代价

- threads pool 下 `--max-tasks-per-child` 无效，内存泄漏只能靠重启整个 worker 兜底（用一个定时任务检测 RSS 超阈值后自杀，由 supervisor 拉起）。
- 单机并发上限约 4，压测数据不能代表生产。

### 论文可用论点

> 「Celery 在 Windows 上的限制不是配置问题而是进程模型的语义缺失（fork vs spawn）。选择 threads pool 的**依据不是"能跑"，而是本任务 I/O 占比 > 95% 的定量判断**——这让一个看似妥协的选择变成了有依据的工程决策。」

---

## ADR-002　Broker 选型：开发零依赖，生产可切换

**状态**：已决策　**影响**：环境搭建成本　**日期**：阶段一

### 背景

Redis 官方不提供 Windows 版本（微软的 3.2 分支早已停更）；Docker 未安装。而队列是架构必需。

### 讨论

> **AI**：可以先装 Memurai（Redis 的 Windows 兼容实现）或者用 WSL 跑 Redis。

> **人类**：这是给同学跑的环境，多一步安装就多一批人跑不起来。有没有完全不装东西的方案？

> **AI**：有。Celery 的底层是 kombu，kombu 支持多种 transport，其中 `sqla+sqlite:///celery.sqlite` 和 `filesystem://` 都是零依赖的。SQLAlchemy transport 把队列消息存进一张表，语义上够用。

> **人类**：那它的缺陷是什么？我不想答辩时被问住。

> **AI**：四条，都必须写进文档：
> 1. **没有真正的 visibility timeout** —— 消息被取出后如果 worker 挂了，消息会永久处于 "in progress"，不会自动重回队列。Redis transport 有 visibility_timeout 可以兜底。
> 2. **轮询而非推送** —— kombu 的 SQLAlchemy transport 默认靠轮询，延迟通常 0.5–1s。对本项目（分钟级任务）无感，但如果要演示"实时"就得接受这点延迟。
> 3. **并发写入有锁竞争** —— SQLite 单写者模型，4 个 worker 抢一张队列表会偶发 `database is locked`。需要开 WAL 模式 + `busy_timeout`。
> 4. **不支持优先级队列** —— 本项目打算把 LLM 调用单独排队限速，这个能力用不上，但要说明"不是不需要，是当前规模不需要"。

> **人类**：那 worker 崩溃后消息卡死怎么办？总不能靠人去改表。

> **AI**：加一个"看门狗"定时任务：每 60s 扫 `analysis_runs` 里 `status IN ('cloning','diffing','parsing','analyzing')` 且 `started_at < now() - interval '15 minutes'` 的 run，重新入队（`attempt + 1`）。这其实比 visibility timeout 更可靠，因为它的依据是**业务状态**而不是队列状态 —— 队列只知道"消息被取了"，不知道"任务死了"。**这个设计即使在 Redis 下也应该保留。**

### 决策

- 抽象层：`BROKER_URL` 环境变量，代码里不出现任何 broker 特定逻辑。
- 开发默认：`sqla+sqlite:///./var/celery-broker.sqlite`（WAL + busy_timeout=5000）。
- 生产/演示：`redis://localhost:6379/0`。
- 结果后端：**不用 Redis**，直接落 `analysis_runs` 表 —— 结果本来就是业务数据，没必要在队列系统里再存一份。
- 无论哪种 broker，都启用看门狗（基于业务状态的超时重入队）。

### 代价

- SQLite broker 下单机吞吐上限约 50 msg/s，够用但无扩展性。
- 需要额外维护看门狗任务（约 40 行代码）。

### 论文可用论点

> 「消息队列的可靠性不应该依赖 broker 的语义（visibility timeout 是"尽力而为"的），而应该由**业务状态机 + 超时看门狗**保证。这一判断使系统在 SQLite 与 Redis 之间切换时行为完全一致。」

---

## ADR-003　Webhook 防抖：为什么最终**没有**用 Redis 🔴

**状态**：已决策（否决了 AI 的初始建议）　**影响**：数据正确性　**日期**：阶段一

> 这是本次设计中最重要的一次分歧，也是「人类工程判断力」最典型的体现。

### 背景

一次 push 可能触发多个 Webhook（GitHub 的 push + PR synchronize 会同时来）；rebase / force-push 会产生一串 after_sha；平台在超时后会重发同一条事件。如果不做控制，同一个提交会被分析多次，产生多份文档版本，**而且可能把人的手工修改覆盖掉**。

### AI 的初始建议

> **AI**：用 Redis 做防抖。key 设计 `wh:{project_id}:{after_sha}`，`SET NX EX 60`，抢到锁的才入队，抢不到的丢弃。简单有效，还能顺带做限流。

### 人类的质疑

> **人类**：等一下。Redis 的 SETNX 是「尽力而为」的。如果 Redis 重启了呢？如果 TTL 刚好在两条重复请求之间到期呢？如果以后起了两个 API 实例、时钟有几十毫秒偏差呢？

> **AI**：这些情况都会导致锁失效、重复入队。

> **人类**：那重复入队的后果是什么？

> **AI**：会产生第二个 run，重新生成一份文档，覆盖掉第一次的结果 —— **包括人在这期间手改的内容**。

> **人类**：所以这不是"防抖"问题，是"数据不能重复写"的问题。防抖是性能优化，幂等是正确性要求。正确性不应该建立在一个会过期的缓存上，应该建立在数据库约束上。

> **AI**：同意。而且 Redis 方案还有一个隐患：它不留痕。出问题时你无法回答"这条 webhook 为什么被丢了"，因为它压根没落库。

### 决策（两层结构）

**第一层：正确性 —— 数据库唯一约束（必须有）**

```sql
-- 同一 project 的同一次变更，物理上只允许一条
CREATE UNIQUE INDEX ux_delivery_dedupe
    ON webhook_deliveries (project_id, dedupe_key);

-- dedupe_key = sha256(project_id | event_type | action | after_sha | pr_number)
```

插入冲突时捕获 `UniqueViolation`，返回 **200 + {status:"duplicate"}**。注意不能返回 409：平台会把非 2xx 视为投递失败并持续重试，雪上加霜。

**第二层：性能 —— 短窗口合并（可选，不加也正确）**

同一 project 已有 queued/running 的 run 时，新 push 若其 head_sha 是当前 run head 的**后代**（用 `git merge-base --is-ancestor` 判断），则：

- 新 run 标记 `status='superseded'`，不执行；
- 正在跑的 run 在进入 `analyzing` 阶段前**重新读取最新的 head_sha**，一次性分析到最新。

术语叫 **latest-wins coalescing**。它把「5 次快速 push」压成「1 次分析」，是成本优化，但即使它失效（比如两个 run 同时跑），正确性也不受影响 —— 因为产物层还有第三层保护。

**第三层：产物层 —— upsert + 版本号由 DB 生成**

`documents` 上 `(project_id, doc_type, target_key)` 唯一，写时 UPSERT；`document_revisions.version` 由 `max(version)+1` 在事务内生成并受唯一约束保护。即使两个 run 并发写同一份文档，结果也是"两个连续版本"而不是"内容交错损坏"。

### 什么情况下才真的需要 Redis

写进论文，作为"何时该引入缓存"的判据：

- 单 project 的 webhook 峰值 > 50 QPS（本项目实际 < 1 QPS）
- 需要跨进程共享的滑动窗口限流（本项目用 DB 计数即可）
- 需要亚毫秒级的重复判断（本项目容忍 10ms 的 DB 唯一键判断）

### 代价

- 每次 webhook 多一次 DB 写（约 2ms）。对一个分钟级异步系统，这个代价可以忽略。
- `webhook_deliveries` 会因重复投递而保留多条"duplicate"记录 —— 这**是特性不是缺陷**，它构成了审计线索。

### 论文可用论点

> 「缓存解决性能问题，约束解决正确性问题。把正确性放在 TTL 上，等于让系统在缓存过期的那一刻改变行为。本系统将幂等下沉到数据库唯一约束，Redis 只作为可选的性能层——这条分层原则贯穿了接入层、任务层、产物层三级设计。」

---

## ADR-004　要不要上 Neo4j 🔴

**状态**：已决策　**影响**：部署复杂度　**日期**：阶段一

### 讨论

> **AI（初始建议）**：题目提到可以用 Neo4j 存代码实体关系，这是加分项。建议引入，因为「类图生成本质是图查询」。

> **人类**：加分项也要能跑起来。没 Docker，Neo4j 怎么装？就算装上了，JVM 吃 1–2G 内存，我的笔记本还要同时跑 PG、Celery、模型调用。而且答辩时如果 Neo4j 挂了，整个演示就废了。

> **AI**：可以让 Neo4j 变成可选的。

> **人类**：那问题就变成：不上 Neo4j，现在这套关系查询还够用吗？我需要一个判据，而不是"凑合能用"。

> **AI**：可以量化。本项目的图查询只有三类：
> 1. 取某实体的 1–2 跳邻域（画类图/时序图时的上下文）
> 2. 反向 N 跳影响面分析（改这个函数会波及谁）
> 3. 路径查找（A 到 B 经过哪些调用）—— 这个 MVP 不做
>
> 中等项目约 5000 个实体、2 万条关系。邻接表 + 递归 CTE 的 3 跳查询在这个规模是毫秒级；实测（同类结构的基准）3 跳约 20–80ms。Neo4j 的优势要到**千万级边 + 多跳路径查询**才显现。

> **人类**：那就在数据库里做，但接口要留口子。以后真要换，不能重写业务代码。

### 决策

- 用 `entity_relations` 邻接表（见 database.md §4），双向索引 `ix_rel_src / ix_rel_dst`。
- 对外只暴露 `/entities/{id}/graph` 和 `/projects/{id}/impact` 两个接口，返回统一的 `{nodes, edges}` 结构。
- 预留 `?format=cypher` 导出接口 —— 真要迁移时，把这张表导成 Cypher 脚本即可，上层无感。
- 论文中明确写出「何时应该引入图数据库」的三条判据：①边数 > 10^7；②需要 4 跳以上的路径查询；③查询本身需要 Cypher 的表达力（可变长路径、最短路径）。

### 论文可用论点

> 「技术选型要能回答"什么时候该换"，而不只是"现在够用"。本系统为图数据库预留了导出接口与明确的替换判据，把"不用"从权宜之计变成了有边界的设计决策。」

---

## ADR-005　拉代码：clone 还是 Compare API 🟡

**状态**：已决策　**影响**：单次分析耗时　**日期**：阶段一

### 讨论

> **AI**：Worker 第一步 `git clone --depth=1`，然后 `git diff`。

> **人类**：`--depth=1` 只有 head 一个提交，你拿什么跟 `base_sha` diff？base 根本不在本地。

> **AI**：那改成 `git fetch origin <base_sha> <head_sha> --depth=1`。

> **人类**：可行，但每次都要建目录、fetch、算 diff、再删。一个 200MB 的仓库，光 fetch 就几十秒，而我要的只是"改了哪 12 个文件"。有没有不用 clone 的路？

> **AI**：有。GitHub 有 `GET /repos/{o}/{r}/compare/{base}...{head}`，直接返回 `files[]`（含 patch、additions、deletions、status）。Gitee 也有对应的 `/compare`。一次 HTTP 请求拿到全部文件级变更。

> **人类**：那 Tree-sitter 怎么办？patch 里只有 hunk，我要解析的是完整文件。

> **AI**：分两步：Compare 拿"改了哪些文件"，再对每个变更文件 `GET /repos/{o}/{r}/contents/{path}?ref={head_sha}` 拿完整内容。两步都是 HTTP，无本地仓库。

> **人类**：听上去更简单，但风险呢？

> **AI**：三个风险，都可控：
> 1. **截断** —— GitHub compare 对超过 300 个文件或超大 diff 会截断，`files` 数组不完整。应对：检测返回中的 `truncated` 标志，超限则降级走 clone 路径。
> 2. **限流** —— 未认证 60 次/小时，认证 5000 次/小时。一次 run 约 1 + N 次请求（N = 变更文件数，通常 < 20）。20 次 push = 400 次，在额度内。需做请求计数与 429 退避。
> 3. **拿不到历史** —— 无法做"跨多次提交的演进分析"。这属于 v2 能力，MVP 不需要。

### 决策

| 路径 | 适用 | 耗时（估） |
|---|---|---|
| **主路径：Compare API + Contents API** | 变更文件 < 200 且未截断 | 1–3s |
| 降级：本地 clone/fetch | 超过阈值、API 限流、需要历史分析 | 10–60s |

并且**仓库缓存**（`repo_cache/{project_id}`）只在降级路径使用，用 `--filter=blob:none --no-checkout` 部分克隆减小体积。

### 代价

- 依赖平台 API 的稳定性与配额；Gitee 的 compare 接口文档较弱，需要实测。
- 两条路径都要维护（约多 150 行代码），但换来"绝大多数情况 2 秒内出结果"。

### 论文可用论点

> 「分布式系统里最贵的操作是搬运数据。原方案默认"要分析代码就得先有代码"，但实际需求是"分析变更"——把数据搬运降级为按需取用，单次分析从 30s+ 降到 2s。这是一次**对隐含前提的质疑**带来的优化。」

---

## ADR-006　AI 重新生成会不会覆盖人的修改 🟢

**状态**：一期做闸门，二期做合并　**影响**：产品可信度　**日期**：阶段一

### 讨论

> **人类**：活文档最大的风险其实是这个 —— 我在 AI 生成的类图里补了一段业务说明，下次有人 push，AI 重新生成，我的说明就没了。这比"文档不更新"更让人不想用。

> **AI**：同意，这是产品能否被接受的分水岭。方案有三档：
> - A. 只要有人改过，就永不自动更新（安全但文档会腐化）
> - B. 每次覆盖，但保留历史版本（简单但用户会生气）
> - C. 三方合并：base = 上次 AI 生成版，ours = 当前（含人工修改），theirs = 新 AI 生成版；冲突则标记待人工确认

> **人类**：C 太复杂了，而且 PlantUML 的三方合并基本没法自动化 —— 图不是文本行，语义冲突无法靠文本 diff 判断。先用 A + 提示。

### 决策（一期）

1. `documents.manual_edited = true` 后，自动更新**不写** `current_revision_id`，而是生成一个 `status='stale'` 的候选版本。
2. 在 PR 评论里给出：「检测到 X 类图有 3 处实体变更，但该文档含人工修改，未自动更新。点此查看候选版本并接受」。
3. 用户点 `POST /documents/{id}/accept-ai` 才生效。
4. 冲突提示必须包含 **diff 视图**（人改了什么 vs AI 想改什么），否则用户无法决策。

### 二期（写进 backlog）

- 对 Markdown 类文档（api_doc）可以做三方合并，因为它是行文本；
- 对 PlantUML 类图只做"实体级 diff"：比较 `source_entity_ids` 的集合变化，提示"新增 2 个类、删除 1 个方法"，而不是合并文本。

### 论文可用论点

> 「人机协同的关键不是"AI 做得更好"，而是**在 AI 不确定的时候把决定权交还给人，并且交还得足够省力**（给出 diff，而不是让人自己比）。这条 'detect-and-defer' 原则比任何生成质量优化都更能提升系统可信度。」

---

## ADR-007　没有 Java 怎么渲染 PlantUML 🟡

**状态**：已决策　**影响**：阶段二实现　**日期**：阶段一（环境勘察时发现）

### 背景

环境勘察发现本机 **没有 Java**，而 PlantUML 的官方实现是 jar，必须 JRE。可选的替代：
1. 安装 JRE 17（约 180MB）
2. 用 PlantUML 官方服务器的编码 URL（`https://www.plantuml.com/plantuml/svg/~1{encoded}`）
3. Kroki 自托管（需 Docker，排除）
4. 用 Mermaid 替代 PlantUML（node 已有，`mermaid-cli` 可本地渲染）

### 决策

- **校验**（必须离线、必须快）：装 JRE，本地 `java -jar plantuml.jar -checkonly -failfast2 xxx.puml`。这是"AI 输出可信"的闸门，不能依赖外网。
- **渲染成图**（可接受慢、可接受失败）：优先本地 jar，失败则回退到 HTTP 服务。渲染失败不影响文档落库，只影响展示。
- **Mermaid 作为第二输出格式**：前端预览用 Mermaid（浏览器直接渲染，零依赖），归档/论文插图用 PlantUML（表达力更强，类图更规范）。两种格式由同一份"结构化中间表示"（JSON）生成，避免二次调用模型。

### 为什么不是"直接用 Mermaid 省掉 Java"

> 因为 PlantUML 的类图语法（`>--`、`..|>` 等关系符号、包分组）在表达架构关系上更完整，且它是软件工程领域的通用语汇，答辩时评审一眼能看懂。Mermaid 的类图语法较新，部分关系表达不出来。**保底方案不能变成主方案，否则会限制表达力。**

### 代价

- 多一个 180MB 的运行时依赖，部署文档要写清楚。
- CI 里需要 `apt install default-jre-headless`。

---

## 附录 A　待定问题（TBD）

| # | 问题 | 当前倾向 | 何时必须定 |
|---|---|---|---|
| 1 | 文档粒度：一个类一份文档，还是一个模块一份？ | 模块级（target_key = `module:path`），类级作为可选 | 阶段二开工前 |
| 2 | 时序图从哪来：静态调用链 vs 运行时 trace | 静态（AST 推导调用关系），运行时 trace 需要跑测试，风险高 | 阶段三 |
| 3 | 首次全量生成（backfill）是否需要 | 需要，但作为独立离线命令，不走 Webhook 链路 | 阶段四 |
| 4 | 是否要做前端 | 做最小页面（文档列表 + 图预览 + run 进度），不做编辑器 | 阶段五 |
| 5 | 模型价格变动如何应对 | 抽象 `LLMProvider` 接口，价格表配置化 | 阶段二 |

---

## 附录 B　记录模板（后续每条决策照此补）

```markdown
## ADR-00X　标题

**状态**：提议 / 已决策 / 已废弃　**影响**：　**日期**：

### 背景
（是什么问题逼着我们必须决策？不决策会怎样？）

### 备选
1. ...
2. ...

### 讨论
> **人类**：...
> **AI**：...
> **人类**：...

（保留真实的来回，包括 AI 的错误和被纠正的过程 —— 这才是"协同"的证据，比只记录结论有价值）

### 决策

### 代价
（选择了它，我们失去了什么？）

### 论文可用论点
（一句话，能直接进论文的"设计权衡"段落）
```
