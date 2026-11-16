# 阶段八：把文档送到人面前（投递、评审与瘦身）

> 阶段七结束时，系统能生成并入库，但"人怎么看到、怎么改、怎么确认"仍然只有命令行。
> 这一阶段补上最后一段路：**投递到知识库/PR、在浏览器里评审、用压测证明并发下不崩**，
> 同时把 850MB 的镜像压到 581MB，并修掉两个只有并发才会出现的真实 bug。
>
> 对应代码：app/publishers/、app/api/review.py、app/render.py、app/static/、
> scripts/seed_demo.py、scripts/load_test.py；测试从 181 增至 **225 项**。

---

## 1. 为什么投递失败不能拖垮 run

生成、校验、入库是核心事务；PR 评论、知识库推送是**外部副作用**。后者的失败方式不可控：
GitHub 可能 502，知识库目录可能没权限，webhook 对端可能超时。如果把这类失败判成
`run.failed`，一次已经产出并入库的文档会被统计成"生成失败"，成功率数据就失真了。

所以边界划在这里：

- `publish_drafts` 成功 → run 进入 `succeeded`；
- 投递结果单独写 `publish_records`（success / failed / skipped），并在 run 的
  `stage_detail.delivery` 里给出总数；
- 任何一个适配器抛异常，只记录，不影响其它适配器，也不改变 run 状态。

这不是"吞异常"：失败有审计、有 error_message、有 skipped 原因，只是不升级为 run 失败。

---

## 2. 三个投递适配器

| target | 类 | 行为 | 未配置时 |
|---|---|---|---|
| `local_wiki` | `LocalKnowledgeBasePublisher` | 写 `<KB_DIR>/<project>/<doc_type>/<target_key>.puml`，生成 README 索引；候选写 `_pending/` | 恒可用 |
| `github_pr_comment` | `GitHubPRCommentPublisher` | 在 PR 下发一条变更表格；正文含 `<!-- livingdoc:run:<id> -->`，重跑用 PATCH 而不是再发一条 | 无 token / 无 PR 号 → skipped |
| `webhook_kb` | `WebhookKnowledgeBasePublisher` | POST JSON（含正文与候选），可选 HMAC-SHA256 签名 | 无 URL → skipped |

三个设计点：

1. **本地知识库按 ADR-006 分离 current 与候选**：正文目录只放 current，候选进 `_pending/`，
   README 里单独列"待人工确认"。评审页接受候选后，下一次投递才会把它写进正文。
2. **PR 评论幂等靠标记**：先 GET 评论列表找 marker，找到就 PATCH，找不到才 POST。
   否则每次重跑都会在 PR 下堆一条重复评论。
3. **webhook 签名可复算**：`X-LivingDoc-Signature: sha256=<hex>`，测试里用同一 secret
   独立算一遍 HMAC，避免"签名代码自己验证自己"。

`publish_targets` 用逗号分隔，未知 target 会被过滤并 warning；失败只进审计。

---

## 3. 评审 API 与前端

后端 `app/api/review.py` 提供 `/api/v1` 下的一组端点（完整清单见
[architecture/api.md](architecture/api.md) §2.8）：

- 项目 / 文档列表与详情、版本列表、单版本、unified diff；
- 人工编辑（创建 `created_by='human'` 新版本，校验失败 422 + diagnostics，可 force）；
- 接受 AI 候选（ADR-006 的切换入口）；
- PlantUML 渲染（jar 存在则编译 SVG，不存在则降级为静态校验并说明原因）；
- 发布到指定 target，返回每条投递的结果；
- run 详情、AI 调用审计、publish_records 查询。

前端 `app/static/` 是**原生 HTML/CSS/JS，没有构建链**：答辩现场不需要 Node。
`/ui/` 由 FastAPI 静态挂载，hash 路由 `#project=<id>&doc=<id>`，支持类型筛选、搜索、
四个 tab（当前版本 / AI 候选 / 历史 / diff）、编辑、接受候选、发布。
Markdown 用一个小的自写渲染器（先转义再解析标题/列表/表格/代码围栏），
避免为一个页面引入 markdown 依赖。

设计取舍：**没有做认证**。这是课程演示系统，本地/内网运行；真实部署必须加 token 与权限，
这一条写进边界，而不是假装已经安全。

---

## 4. 并发：两个只有真跑才会出现的 bug

### 4.1 固定临时文件名

知识库写入原本用 `<path>.tmp` 再 `os.replace`。并发发布同一份文档时：

1. 线程 A 把 `.tmp` 改名为正式文件；
2. 线程 B 的 `os.replace` 找不到 `.tmp` → `FileNotFoundError`。

容器内 8 线程发布测试稳定复现。修复：

- `tempfile.mkstemp` 生成每次唯一的临时文件；
- 进程内 `_WRITE_LOCK` 串行化 KB 写入（写文件很快，串行代价远小于 500）；
- Windows 上目标文件可能被杀毒/编辑器占用，`os.replace` 重试 5 次。

回归测试：8 线程 × 40 次写同一路径，断言无异常、最终内容是某个版本、没有残留 tmp。

### 4.2 版本号竞争

人工编辑要"读 current → version+1 → 插入"。4 个请求同时进来会撞
`UNIQUE(document_id, version)`。最初重试 3 次，Linux 容器 + 4 线程实测会连续撞到第 4 次，
于是把 `MAX_PUBLISH_ATTEMPTS` 提到 8，每次重试重新读 current，退避 0.01×attempt。

PostgreSQL 上直接对运行中的 API 做 8 个并发 PUT：**8/8 返回 200**，版本号 4–11 连续无重复，
没有 409/500。SQLite 上同样 4 线程测试通过。

---

## 5. 负载测试

`scripts/load_test.py` 是纯 asyncio + httpx（不引入 locust），支持 read / publish / mixed，
输出 RPS、p50/p95/p99、按端点延迟、失败样例，错误率超阈值退出码 1。

### 5.1 本机 SQLite

| 模式 | 请求 / 并发 | 失败 | RPS | p50 / p95 / p99 |
|---|---|---|---|---|
| read | 120 / 8 | 0 | 191.96 | 35.92 / 70.34 / 82.87 ms |
| mixed | 60 / 6 | 0 | 67.25 | 32.88 / 193.24 / 585.24 ms |

mixed 里 publish 共 33 次，p50 61.38ms、p95 498.82ms——本地知识库写文件是主要成本。

### 5.2 Docker Compose + PostgreSQL

| 模式 | 请求 / 并发 | 失败 | RPS | p50 / p95 / p99 |
|---|---|---|---|---|
| read | 200 / 20 | 0 | 31.19 | 470.36 / 1173.37 / 1831.70 ms |
| mixed | 100 / 10 | 0 | 14.41 | 721.39 / 1174.58 / 1278.15 ms |

mixed 里 publish 55 次全部 success，p50 1026.55ms、p95 1224.21ms；`publish_records`
累计 240 条 success（含前面演示数据），8 并发人工编辑 8/8 成功。

**怎么解释这组数字（答辩不要回避）**：PG 单机读比 SQLite 慢，主要成本是每次请求经过
Docker Desktop 的端口映射 + 连接池上限（默认 5+10）与 `pool_pre_ping` 的往返，不是 SQL 本身；
这次没有做连接池调优。PG 的价值在多进程共享与并发写一致性，而不是本地单机读延迟。
换句话说，**这组数据证明的是"并发下不丢记录、不 500"，不是"PG 比 SQLite 快"**。

---

## 6. api_doc 空内容：从"修一行"改成"重新生成"

真实评测里 api 文档的失败不是语法错，而是 M003（完全空）/ M005（标题下没有正文）。
原来的修复提示词说"只改列出的问题，其余保持不变"，模型就补一句话交差，下一轮继续 M005，
三轮全废。

修复在 `SelfCorrectingGenerator.generate`：

- 若 `kind == "api"` 且本轮出现 M003/M005，置 `regenerate_next=True`；
- 下一轮温度回到生成温度（0.2），而不是修复温度（0.0）；
- `build_repair_prompt(..., regenerate=True)` 明确要求整篇重写，并给出最低结构：
  每个变更函数一段说明 + 参数/返回/异常表 + 至少一个可运行调用示例。

测试 `test_empty_api_document_is_regenerated_not_patched` 断言第二轮提示包含
"from scratch" 与正文结构要求，而不是"只改这一行"。

---

## 7. 镜像瘦身

单阶段 850MB → 多阶段 581MB（-31.6%），测试镜像 582MB。关键动作：

| 动作 | 收益 |
|---|---|
| 多阶段：builder / plantuml / runtime / test / release | 构建工具与测试不进运行镜像 |
| jlink 裁剪 JRE | 184MB → 49MB |
| 删 venv 里的 pip/setuptools/`__pycache__` | -28MB |
| 默认目标改为最后的 release | 避免误把 tests 打进生产镜像 |

固定 Debian bookworm 而不是 trixie：trixie 已移除 openjdk-17，只剩更大的 21。
细节与踩坑见 [06-docker.md](06-docker.md)。

---

## 8. 真实仓库回归：psf/requests

2026-11-09 用真实公开仓库补了一次回归，目的不是"再跑一次 demo"，而是检验生成内容是否真的基于变更代码。

**对象**：`psf/requests`，clone 到 `var/repos/psf__requests`（6,496 commits，无 GITHUB_TOKEN，git 正常）。
**提交**：`6f66281a`（base `6f205ff4`，3 文件：`_types.py`、`models.py`、`test_requests.py`，7 个变更函数）。

| 指标 | 结果 |
|---|---|
| 耗时 | 81.4s（run 内部 stage_detail；墙钟约 84s） |
| 文档 | 6 份 published（src.requests、tests 各 class/api/sequence） |
| 拒稿 / 候选 | 0 rejected / 0 deferred |
| 投递 | 7/7 success（local_wiki） |
| API 文档 | 3,092 字符，4/4 变更函数各一节 |

这一轮抓出并修掉四个问题：

1. **纯格式化提交也生成文档**：`semantic_hash` 不含注释，`needs_redoc=False`，payload 为空，
   模型只能拿实体名硬编，产出大量 "not documented in code"。现在 `generate_module` 在
   `redoc` 为空时直接跳过（同一仓库的 `661970d1` 4.0s 返回 documents=0）。
2. **Markdown 外层围栏解析错误**：正文里第一个内部代码块被当成外层包装剥掉，前半截
   （含多个 `###` 小节）被丢弃，触发 M005/M001。`extract_markdown` 现在只在
   "首个围栏前无标题、末个围栏后无内容"时才剥。
3. **API 文档漏函数**：W005 从 warning 改为 api 下的 error，并要求每个变更函数一节；
   提示词同步写死 "every changed function"。
4. **顶层函数限定名拼错**：`changed_members` 旧逻辑产出 `has_read.has_read`，
   校验器永远找不到；顶层直接使用 `symbol.name`。

另外修了两个配置/可观测性问题：`CELERY_TASK_ALWAYS_EAGER` 别名不生效；
相对 `KB_DIR` 让 `Path.as_uri()` 抛 `relative path can't be expressed as a file URI`。
被拒文档的 invocation 之前只显示 W005 warning，看不到真正 error，现在用
`DocumentDraft.validation_errors()`。

**边界**：只验证了 3 文件 / 10 文件的公开仓库提交，没有做 1k+ 文件大仓库；
GitHub API 未认证（rate limit=0），PR 评论仍只做了 MockTransport 协议验证。

---

## 9. 边界

1. 没有认证与权限；`/api/v1` 与 `/ui/` 默认只适合本地/内网。
2. 没有浏览器自动化测试；前端只做了 `node --check` 与静态资源/API 层测试。
3. PG 压测是演示数据规模，没有做连接池调优、慢查询分析、大仓库样本。
4. 多模型横向对比按用户要求本轮跳过。
5. 真实 GitHub PR 评论没有在仓库上实际发过（无 token）；用 MockTransport 验证了
   创建/更新/幂等/错误分支，属于"协议层验证"，不是"线上验证"。