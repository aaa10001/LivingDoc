# 阶段二：基础代码骨架

> 状态：已完成 · 代码可运行（需联网装依赖）· 自动化测试 30 项全绿
> 本阶段只做**入口链路**：Webhook 进来 → 验签 → 入队 → 克隆/取 diff → 落产物。
> AST 解析、AI 调用、PlantUML 生成属于阶段三/四，这里只留接口锚点。

## 1. 交付物

| 文件 | 职责 |
|---|---|
| `app/main.py` | FastAPI 入口：3 个路由（`/`、`/healthz`、`/webhook/github`），验签 → 解析事件 → 入队 |
| `app/security.py` | Webhook 签名验证（SHA-256 / SHA-1），唯一安全边界 |
| `app/git_handler.py` | 克隆/更新仓库、按 sha 取 commit、diff 解析、读文件内容 |
| `app/tasks.py` | Celery 任务 `analyze_push`：git 阶段 + 变更清单产物 |
| `app/celery_app.py` | Celery 实例与可靠性配置（acks_late / prefetch=1 / 只收 JSON） |
| `app/dedupe.py` | 投递去重（进程内 TTL 缓存，阶段三替换为 DB 唯一索引） |
| `app/config.py` | 全部配置项集中一处，环境变量 / .env 覆盖 |
| `tests/` | 30 项单测，**全部离线可跑** |
| `scripts/send_test_webhook.py` | 本地伪造 GitHub 投递，不需要公网 / ngrok |
| `requirements.txt` · `.env.example` · `pytest.ini` | 依赖、配置样例、测试配置 |

## 2. 十五分钟跑通

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 窗口 1：API
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000

# 窗口 2：worker（Windows 必须 --pool=threads，原因见 ADR-001）
.\.venv\Scripts\python.exe -m celery -A app.celery_app worker --loglevel=info --pool=threads --concurrency=4 -Q analysis

# 窗口 3：发一条假投递
.\.venv\Scripts\python.exe scripts\send_test_webhook.py --repo octocat/Hello-World
~~~

期望结果：窗口 3 打印 `HTTP 202` 和 `task_id`；窗口 2 打印 `开始分析` / `完成 N 个文件变更`；
`var/work/runs/<delivery_id>.json` 出现产物文件。

不想起 worker 时，在 `.env` 里设 `CELERY_TASK_ALWAYS_EAGER=true`，任务会在 API 进程内同步执行（只适合单机演示）。

接真实 GitHub：仓库 Settings → Webhooks → Payload URL 填公网地址，Secret 与 `.env` 一致，事件选 push + pull_request。
本机没有公网 IP 可以用 `npx smee-client --url https://smee.io/xxx --target http://127.0.0.1:8000/webhook/github`，
smee 会原样透传 `X-Hub-Signature-256`，验签逻辑无需改动。

## 3. 链路（本阶段真实执行的部分）

~~~text
GitHub --POST--> main.py
                   |  1. 读原始 body
                   |  2. verify_github_signature()   <- 失败即 401，不再往下
                   |  3. build_job(): 事件 -> 扁平 dict（只保留 8 个字段）
                   |  4. delivery_cache.add(dedupe_key)  <- 重复则 200
                   v
              Celery(queue=analysis)
                   v
             tasks.analyze_push
                   |  ensure_repo()      克隆或增量 fetch
                   |  fetch_commits()    确保 base/head 两个 sha 在本地
                   |  diff_commits()     --name-status + --numstat -> FileChange[]
                   |  group_by_module()  按路径首段聚合（模块级文档粒度的雏形）
                   v
            var/work/runs/<delivery>.json
~~~

## 4. 八个必须做对的细节

每一条都写成「错误做法 → 后果 → 我们的做法」，答辩时可以直接讲。

### 4.1 验签必须对**原始字节**做

- 错误做法：`payload = await request.json()`，然后 `json.dumps(payload)` 回去算 HMAC。
- 后果：键顺序、空格、`ensure_ascii` 都会变，签名永远对不上；更糟的是有人以为「算法不对」，
  去改用别的摘要算法，把问题越修越远。
- 做法：`body = await request.body()` 先拿字节，验签通过之后才 `json.loads(body)`。
  单测 `test_reformatted_json_fails` 专门锁死这一点（只多一个空格就失败）。

### 4.2 比较必须用 hmac.compare_digest

- 错误做法：`if digest == header`。
- 后果：`==` 逐字节短路比较，响应时间随匹配前缀长度变化，可被计时攻击逐字节猜出签名。
- 做法：`hmac.compare_digest(expected, received)`，常数时间比较。

### 4.3 剥掉 `sha256=` 前缀，而不是拿整个头去比

- 头上是 `sha256=<hex>`，摘要只是等号右边那一段。同时兼容旧的 `X-Hub-Signature`（SHA-1）。

### 4.4 git 调用必须关掉交互式提示

- 错误做法：直接 `git clone https://...`。
- 后果：私有仓库令牌过期时 git 会弹凭据对话框等待输入；worker 没有终端，
  **任务不是失败而是永久挂起**，队列越堵越死，日志里一个字都没有。
- 做法：`GIT_TERMINAL_PROMPT=0` + `GIT_ASKPASS=echo` + `GCM_INTERACTIVE=never`，
  并且给每条命令加超时（默认 300s，超时抛 `GitError` 而不是等到天荒地老）。

### 4.5 令牌不能落进 .git/config

- 错误做法：把 `https://x-access-token:<token>@github.com/...` 直接当 remote URL 克隆。
- 后果：令牌被写进 `.git/config`，仓库目录被打包 / 上传 / 拷走就泄露了。
- 做法：`git -c http.extraHeader="AUTHORIZATION: basic <base64>"` 逐条命令注入，
  `-c` 只对本次进程生效。另外所有 stderr 在写日志前过一遍 `_redact()` 抹掉令牌。

### 4.6 克隆要原子落位

- 错误做法：两个 webhook 同时到达，各自往同一个目录 `git clone`。
- 后果：工作区被互相覆盖，之后每次 fetch 都报诡异的 `index.lock` / `bad object`。
- 做法：先克隆到 `<dir>.tmp-<pid>-<ts>`，成功后 `os.replace()` 原子改名；
  改名失败说明别的 worker 抢先了，删掉自己的临时目录复用它。

### 4.7 diff 解析统一走 -z

- 错误做法：解析 `git diff --name-status` 的文本输出。
- 后果：中文文件名被转义成 `"\344\270\255.txt"`，得自己写反转义；路径里有空格 / 制表符还会错位。
- 做法：`-z`（NUL 分隔）+ `-c core.quotepath=false`，拿到的就是原始 UTF-8。
  用 `--no-renames` 也是刻意的：改名在文档语义上就是「旧实体消失 + 新实体出现」，且解析零歧义。

### 4.8 幂等冲突返回 200，不返回 409

- 这条直接来自 ADR-003。GitHub 收到非 2xx 会按指数退避重投；
  重复投递返回 409 会让平台一直重试，最后把队列塞满。
- 做法：重复投递返回 `200 {"status":"duplicate"}`；只有验签失败（401）和队列不可用（503，**故意**让平台重投）用非 2xx。

## 5. HTTP 状态码契约

| 码 | 含义 | 平台行为 |
|---|---|---|
| 202 | 已入队 | 成功，不重投 |
| 200 + duplicate | 重复投递（同一 `X-GitHub-Delivery`） | 成功，不重投 |
| 200 + ignored | 事件不需要处理（分支删除 / 草稿 PR / star 等） | 成功，不重投 |
| 400 | payload 结构非法 / 不是 JSON | 视为失败（应当人工排查） |
| 401 | 验签失败 | 失败（Secret 配错了） |
| 503 | broker 不可用 | **期望**平台重投 |

## 6. 已经验证了什么 / 没验证什么

诚实记录，避免答辩被问住。

**2026-11-05 更新：依赖已装齐，全链路实测通过。**

本机 pip 访问 PyPI 会失败（TLS 拿不到凭据：`schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`），
但 **uv** 可以——它自带 rustls 根证书，不走 Windows schannel。装依赖改用：

~~~powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"   # 必须放工作区内：沙箱拒绝写 %LOCALAPPDATA%\uv\cache
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt
~~~

**实测结果**

| 项目 | 结果 |
|---|---|
| `pytest tests/ -q` | **40 passed, 0 skipped** |
| uvicorn + `GET /healthz` | 200 `{"status":"ok","env":"dev","queue":"sqla+sqlite"}` |
| worker（threads pool，concurrency=4） | 已注册 `livingdoc.analyze_push`，连上 `sqla+sqlite` broker |
| 端到端 push | 202 accepted → worker 4.5 s 完成 → 落盘 `var/work/runs/e2e-004.json` |
| 克隆 | 真实 HTTPS：`git clone --filter=blob:none https://github.com/octocat/Hello-World.git` |
| diff | `7fd1a60b..b3cbd5bb` → 1 个文件（CONTRIBUTING.md，状态 A，+1 行） |

产物关键字段：`stats.files=1`、`modules={"(root)":1}`、`next_stage="tree-sitter-parse"`。

**本机沙箱的两个限制（环境问题，不是代码问题）**

1. git 的**本地路径传输**不可用：`git clone D:\path` / `git push` 走 `file://` 传输时需要 fork
   MSYS 的 `sh.exe`，沙箱下报 `sh.exe: fatal error - couldn't create signal pipe, Win32 error 5`。
   **HTTPS 传输不受影响**，所以端到端测试用公开仓库；真实部署（Docker/Linux）没有这个限制。
2. `uv` 默认缓存在 `%LOCALAPPDATA%\uv\cache`，沙箱拒绝写入，必须用 `UV_CACHE_DIR` 指到工作区内。

## 7. 留给阶段三的锚点

| 位置 | 锚点 | 阶段三要做什么 |
|---|---|---|
| `tasks.py: analyze_push` | `"next_stage": "tree-sitter-parse"` | 按扩展名分流语言 → tree-sitter 解析 → 提取受影响的函数 / 类 |
| `tasks.py: group_by_module` | 用路径首段近似模块 | 换成「从 AST 读 package / namespace」 |
| `git_handler.diff_commits` | `--no-renames` | 改 `--find-renames`，在 `changed_files` 里存 `old_path` 以保住文档链接 |
| `git_handler.read_blob` | 已实现 | 直接喂给解析器，不需要再写 git 代码 |
| `app/dedupe.py` | 进程内 TTL 缓存 | 删除，改由 `webhook_deliveries` 唯一索引 + `ON CONFLICT DO NOTHING` |
| `main.py: build_job` | 已压平 8 字段 | 增加 `project_id`（先 upsert projects 表） |

## 8. 人机协同记录（本阶段）

**记录 1：AI 初稿把「重试分类」写成子串匹配，被单测抓出真实 bug**

AI 生成的 `GitError.retryable` 用 `"repository not found" in stderr` 判断「仓库不存在」。
但 git 的真实输出是：

~~~text
fatal: repository 'https://github.com/x/y.git' not found
~~~

仓库 URL 夹在中间，子串永远匹配不上 → 对着一个 404 的仓库也会老老实实重试 3 次，
每次都在日志里刷 5 分钟。改成正则 `repository\b.*\bnot found` 之后修复，
并把这个真实报错原文写进了参数化测试（`test_error_classification`）。

> 这条正好是作业要求的「AI 生成的代码存在小瑕疵，人类通过补充真实上下文引导修正」的样本：
> 瑕疵不在语法，而在 **AI 凭印象假设了报错文案**。人类的介入方式是「把 git 的真实 stderr 贴回来」。

**记录 2：AI 主动放弃 GitPython 换 subprocess**

AI 初稿用 GitPython（`Repo.clone_from`），理由是「代码短、面向对象」。
人类追问：「Windows 上删临时目录会不会因为句柄没关而失败？」
→ AI 承认 `git.Repo` 不 close 会持有文件句柄，在 Windows 上 `shutil.rmtree` 会报
`PermissionError: [WinError 32]`，且 GitPython 的进度输出会污染 Celery 日志。
→ 改为 `subprocess` 直接调 git：报错就是 git 原文（可复现、可 grep），代价只是自己解析输出。

**记录 3：人类否决了「用永不清理的 dict 做去重」**

AI 首版用普通 `dict` 存 delivery_id，永不清理 → 内存泄漏（长期运行必爆）。
改为 `TTLSet`（TTL + 容量上限 + `OrderedDict` 惰性清理），并在注释里写明
「这只是阶段二的占位，阶段三换成 `webhook_deliveries` 唯一索引」，避免把临时方案带进生产。

**记录 4：装好依赖后，第一次真跑就红了两片 —— 状态码语义被装饰器默认值污染**

`@app.post(..., status_code=202)` 把默认码写在了装饰器上，于是 `ping` 返回 `{"status":"pong"}`、
被忽略的事件返回 `{"status":"ignored"}`、重复投递返回 `{"status":"duplicate"}` —— 全是 **202**。
测试断言 200，红了。根因不是「少了几个 200」，而是**接口把两种成功语义混成了一个状态码**：
客户端无法区分「已排队」和「什么都没做」。修复：去掉装饰器默认码，五条返回路径各自
显式返回 `JSONResponse(status_code=...)`，202 只在「真的入队了」时出现。

> 这类 bug 在只有 mock 的测试里永远发现不了，它需要「端点真的被注册到 FastAPI 路由表上」才暴露。

**记录 5：`broker_transport_options` 不是「通用配置」**

AI 在 `celery_app.py` 里无条件写了 `broker_transport_options={"visibility_timeout": 1800}`，
worker 一启动就崩：

~~~text
TypeError: Invalid argument(s) 'visibility_timeout' sent to create_engine(),
using configuration SQLiteDialect_pysqlite/QueuePool/Engine.
~~~

根因：kombu 的 `sqla` transport 会把 `transport_options` 整个 dict **原样转给 `create_engine()`**，
而 `visibility_timeout` 只有 Redis transport 认识。修复为按协议条件构造（`redis://` / `rediss://`
才填），并在注释里写明 SQLAlchemy broker 本来就没有真正的 visibility timeout，靠 ADR-002 的看门狗兜底。
这与 ADR-002 里「开发用 sqla、生产用 redis，两者语义不等价」的判断是一致的。

**记录 6：测试替身的签名漂移，比被测代码的 bug 更隐蔽**

`tests/test_webhook.py` 里的假任务是这么写的：

~~~python
def fake_apply_async(*args, **kwargs):
    captured.append(kwargs["job"])      # ← 错
~~~

但 Celery 的真实签名是 `apply_async(args=None, kwargs=None, task_id=None, **options)`，
应用调用的是 `apply_async(kwargs={"job": job}, queue="analysis")`，
所以任务入参在 `kwargs["kwargs"]["job"]`。结果每个 webhook 用例都拿到
`KeyError: 'job'`，被端点捕获后一律 503。修复是让替身**照抄真实形参名**，并加断言
`assert "job" in kwargs`，让「应用必须用 kwargs 投递」这条约定也被测试锁住。

> 教训：测试替身一旦不遵守真实库的签名，测试保护的就变成了替身自己，而不是系统。
