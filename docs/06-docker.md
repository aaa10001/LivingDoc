# 阶段六：容器化——把三条"未验证"变成"已验证"

> 前五个阶段的文档里反复出现同一句话："本机没有 Docker / JRE / Redis，所以这部分未实测"。
> 这一阶段把 Docker 装上了，于是 ADR-001、ADR-002、ADR-007 第一次有了运行证据。
> 同时暴露了一批只有真正跑起来才会出现的问题（字体库、PYTHONPATH、命名管道、buildx 插件发现）。

---

## 1. 为什么这个项目值得容器化

不是为了"看起来工程化"，而是因为三条核心决策此前都停在纸面：

| ADR | 决策内容 | 没有容器时的状态 | 容器里怎么变成证据 |
|---|---|---|---|
| ADR-001 | Windows 上 Celery 只能用 threads 池；生产用 prefork + max-tasks-per-child | 只在文档里"据说 prefork 在 Linux 可用" | worker 启动日志写着 concurrency: 4 (prefork)，ForkPoolWorker 实际执行任务 |
| ADR-002 | 生产 broker 用 Redis，配 visibility_timeout；开发用 sqla+sqlite | 从未连过真实 Redis | compose 起 redis:7，worker 日志 transport: redis://…:6379/0 并成功收任务 |
| ADR-007 | PlantUML 需要 JRE，-checkonly 做渲染闸门 | 本机无 Java，闸门从未执行 | 镜像内精简 JRE 17 + PlantUML 1.2024.7，合法图 rc=0、坏图被识别 |

另外还有一条隐含收益：**评审者不必重现我的 Windows 环境**。一条 docker compose up -d --build
就能得到同样的 Python 3.11、同样的 tree-sitter、同样的 JRE 与 PlantUML 版本。

---

## 2. 环境与版本（实测）

| 项 | 值 |
|---|---|
| 宿主 | Windows 11 25H2 build 26200（家庭中文版） |
| WSL | WSL 2.4.13，Ubuntu（WSL2），docker-desktop 发行版 |
| Docker | Docker Desktop 4.98.0；CLI/Server 29.9.2；Buildx v0.38.1；Compose v5.6.1 |
| 内核 | 5.15.167.4-microsoft-standard-WSL2（linux/amd64） |
| 资源 | 16 CPU / 7.576 GiB 可见 |
| 镜像 | livingdoc:slim，581MB（旧的单阶段 livingdoc:dev 为 850MB） |
| 基础镜像 | python:3.11-slim-bookworm（Debian 12，Python 3.11.18） |
| 容器内关键版本 | celery 5.7.0、kombu 5.7.0、SQLAlchemy 2.1.8、fastapi 0.162.0、psycopg 3.3.8、tree-sitter 0.26.0、pytest 9.2.0、OpenJDK 17.0.21（jlink 精简）、PlantUML 1.2024.7 |

---

## 3. 镜像设计

### 3.1 多阶段：构建工具留在构建阶段

单阶段镜像 850MB，其中一大半是「只在构建时用得上」的东西：pip、setuptools、apt 缓存、
完整的 JDK、编译工具链。现在拆成五个阶段，运行镜像只拿产物：

~~~
base      python:3.11-slim-bookworm + 环境变量 + apt 源
builder   /opt/venv（只装 requirements，装完删 pip/setuptools/__pycache__）
plantuml  完整 JDK + plantuml.jar → jlink 裁剪出 /opt/java（49MB）
runtime   COPY venv + /opt/java + plantuml.jar + app/scripts（非 root）
test      FROM runtime + tests/（构建期验证用，不进发布镜像）
release   FROM runtime（默认目标，所以 docker build . 不会产出带测试的镜像）
~~~

分层顺序仍然重要：requirements 在代码之前 COPY 并安装，改一行 Python 代码只重跑最后两层。
`release` 放在文件末尾是刻意的——Docker 默认构建最后一个阶段，忘了写 `--target` 也不会把
tests 带进生产镜像。

### 3.2 JRE 用 jlink 裁剪，但字体库一个都不能少

第一版用 Debian 的 plantuml 包，它会把 default-jre（带 GUI 库）一起拖进来；后来改成
openjdk-21-jre-headless + 从 Maven Central 直接下 jar，仍然要 305MB。现在构建阶段装完整 JDK，
再用 jlink 只保留 PlantUML 实际用到的模块：

~~~
jlink --add-modules java.base,java.desktop,java.logging,java.naming,java.xml,jdk.unsupported \
      --strip-debug --no-man-pages --no-header-files --compress=2 --output /opt/java
~~~

结果 /opt/java 只有 49MB（整包 JRE 184MB）。两个踩过的坑：

- **bookworm 而不是 trixie**：trixie 已经移除 openjdk-17-jre-headless，只剩 21，体积更大；
  PlantUML 只要求 Java 8+，没有理由追新。
- **jlink 需要 objcopy**：不装 binutils 会报
  `Error: java.io.IOException: Cannot run program "objcopy"`，因为 `--strip-debug` 要调它。

但 headless 不等于"不需要图形相关库"。第一次执行 -checkonly 直接崩：

~~~
Exception in thread "main" java.lang.UnsatisfiedLinkError:
  /usr/lib/jvm/java-21-openjdk-amd64/lib/libfontmanager.so:
  libharfbuzz.so.0: cannot open shared object file: No such file or directory
    at net.sourceforge.plantuml.Run.forceOpenJdkResourceLoad(Run.java:251)
~~~

PlantUML 在启动时会初始化字体子系统（哪怕只做语法检查）。runtime 阶段仍然保留
libfreetype6、libfontconfig1、libharfbuzz0b。**这是本阶段"实跑才暴露"的第一个依赖**，
文档里此前完全没有线索。

（顺带发现：镜像里没有 graphviz，PlantUML 会报 Cannot run program "/opt/local/bin/dot"。
-checkonly 不受影响，但真正渲染 PNG/SVG 需要再装 graphviz。这一条留在边界里。）

### 3.3 PYTHONPATH：脚本不是模块

scripts/init_db.py 里 from app.config import ...，直接 python scripts/init_db.py 时
sys.path[0] 是 /app/scripts，于是 ModuleNotFoundError: No module named 'app'。
本地能跑是因为 pytest.ini 里有 pythonpath = .，而容器启动命令不走 pytest。

修法是在镜像里设 ENV PYTHONPATH=/app。这个 bug 只有容器里才会遇到——本地永远有项目根在 path 上。

### 3.4 非 root 与卷权限

镜像里 useradd --uid 1001 livingdoc 并把 /app（含 /app/var）chown 给它。
数据卷 var-data 挂到 /app/var，第一次由镜像里的目录初始化，权限随镜像，因此 worker 可写。

一个相关的坑：docker cp 进去的文件属主是 root，git 会拒绝访问并报
detected dubious ownership in repository。本地端到端测试时用 chown -R livingdoc:livingdoc 解决。

---

## 4. compose 拓扑

~~~
postgres:16-alpine ─┐
                    ├─► api    (uvicorn, 8000, healthcheck /healthz)
redis:7-alpine ─────┤
                    └─► worker (celery prefork×4 -Q analysis)
~~~

四个服务，三条依赖边都用 condition: service_healthy，而不是只写 depends_on：

- api 等 postgres 与 redis 健康；
- worker 等 postgres、redis 健康，并且 api 已启动；
- 两个进程的启动命令都是 python scripts/init_db.py && exec ...。

### 为什么建表要单独一步

api 与 worker 是同一镜像的两个进程，谁先启动不确定。两者都调 metadata.create_all()
时，PostgreSQL 上会撞：

~~~
duplicate key value violates unique constraint "pg_type_typname_nsp_index"
~~~

这是并发 CREATE TABLE 的典型报错（系统目录里两个事务同时插同名类型）。
把建表放进 scripts/init_db.py 并在两个启动命令里串行执行，冲突就消失了。
脚本里还有一次 OperationalError 重试（30 次 × 2s），用来兜住"数据库 healthcheck 刚过、
但还没真正接受连接"的窗口。

### worker 必须关掉镜像自带的 healthcheck

镜像的 HEALTHCHECK 打 http://127.0.0.1:8000/healthz，对 api 合理，对 worker 是错的——
worker 不监听端口，会永远显示 (unhealthy)，进而让依赖它的编排逻辑误判。
compose 里给 worker 写 healthcheck: { disable: true }。

---

## 5. 验证结果

### 5.1 镜像自检（scripts/container_smoke.sh）

| 检查 | 结果 |
|---|---|
| Python | 3.11.18 |
| pytest（SQLite 路径） | 215 项全过（exit 0） |
| tree-sitter 原生扩展 | tree_sitter 0.26.0，解析 class A: def m 得到 ['A.m'] |
| JRE | OpenJDK 17.0.21（jlink 裁剪，49MB） |
| PlantUML | 1.2024.7 |
| -checkonly 合法图 | GOOD rc=0 |
| -checkonly 坏图（花括号未闭合） | 输出 Some diagram description contains errors，判定为坏图 |

第一次跑这份自检时，GOOD 判成了 "rejected"：我把合法图写成了单行
class Order {+id: str}，PlantUML 不接受同一行里写成员声明。改成多行后通过。
**这提醒一件事：校验收到的"失败"要先怀疑夹具，再怀疑被验证的东西。**

### 5.2 端到端：一条真实推送如何穿过容器

容器内访问 github.com 超时（宿主网络对 443 不稳定，容器内更严格），于是改用本地裸仓库：

1. 宿主用 git 造两个提交（修改 app/service.py 并新增方法），git clone --bare 得到仓库；
2. docker cp 进 worker 容器 /app/e2e.git 并 chown；
3. 发一条伪造 webhook，clone_url=file:///app/e2e.git。

结果：

~~~
API:      HTTP 202 {"status":"accepted","delivery_id":"6573bc67…"}
worker:   concurrency: 4 (prefork)，transport: redis://:**@redis:6379/0
run:      cloning → diffing → parsing → analyzing → skipped(llm_not_configured)
parse:    files_analyzed=1, symbols_added=1, symbols_modified=1, redoc_targets=2, 17ms
PG:       analysis_runs 一行，files_changed=1, entities_changed=2
~~~

skipped(llm_not_configured) 是预期行为：镜像里没有 LLM key，系统降级但不报错，
已解析的 changes.json 保留下来供后续重跑。

### 5.3 PostgreSQL 上的发布闸门

在 api 容器里直接调 app.publish（连的是 compose 的 PG）：

~~~
publish1:                  created published v1
publish2（同样内容）:        unchanged  v1
人工编辑:                    human_edit（current=v2, manual_edited=True）
AI 再发布（内容不同）:        deferred_stale stale v3
                            current=v2 (human)，latest=v3 (ai)
accept_ai:                  accepted → current=v3 (ai)，manual_edited 仍为 True
~~~

这与 SQLite 上的行为完全一致，说明 store/publish 层没有依赖 SQLite 的隐式语义
（Boolean、JSON、Integer 自增在 PG 上都按预期工作）。

---

## 6. 三条 ADR 的结论更新

- **ADR-001 成立，且比文档更具体**：prefork 模式下日志明确写 concurrency: 4 (prefork)，
  任务由 ForkPoolWorker-N 执行。Windows 上必须 threads 的限制得到反向印证——同一份代码
  在 Linux 容器里用 threads 反而浪费 CPU。
- **ADR-002 成立**：Redis 作为 broker 工作正常，_transport_options 里只有 redis 分支才会
  注入 visibility_timeout，代码里那条"用协议前缀判断"的写法因此被真实环境检验了一次。
- **ADR-007 成立但代价被低估**：JRE 之外还需要 freetype/fontconfig/harfbuzz；
  纯 headless 环境并不会自动具备字体子系统。文档此前只写"需要 JRE 17"，是不够的。

---

## 7. 踩坑清单（按发现顺序）

| # | 现象 | 根因 | 处理 |
|---|---|---|---|
| 1 | permission denied … npipe:////./pipe/dockerDesktopLinuxEngine | DSH 沙箱禁止访问 Windows 命名管道 | 该会话切到 danger-full-access（用户批准） |
| 2 | exec: "docker-credential-desktop": executable file not found in %PATH% | docker CLI 需要凭据助手 | 把 Docker 的 bin 目录加入 PATH |
| 3 | BuildKit is enabled but the buildx component is missing or broken | 子进程环境缺 USERPROFILE/HOME，CLI 找不到插件目录 | 显式设置 USERPROFILE/HOME；compose 插件也复制到 ~/.docker/cli-plugins |
| 4 | 后台构建日志为空、进程消失 | run_code 的 120s 上限会连同子进程树一起杀 | 拆成多次构建，靠 buildx 缓存续跑 |
| 5 | 计划任务 schtasks /Run 报成功但 bat 不执行 | 未深究（ASCII 探针也不写文件），判定该方案不可靠 | 放弃，回到"短时多次构建" |
| 6 | 日志重定向到中文路径后为空 | cmd 的代码页与 UTF-8 路径 | 日志写 %TEMP% 下的 ASCII 路径 |
| 7 | ModuleNotFoundError: No module named 'app' | python scripts/init_db.py 的 sys.path 是 scripts/ | ENV PYTHONPATH=/app |
| 8 | UnsatisfiedLinkError: libharfbuzz.so.0 | headless JRE 缺字体子系统 | 装 freetype/fontconfig/harfbuzz |
| 9 | detected dubious ownership in repository | docker cp 进去的文件属主是 root | chown -R livingdoc:livingdoc |
| 10 | worker 永远 unhealthy | 镜像 HEALTHCHECK 打 /healthz，worker 不监听端口 | compose 里 healthcheck: { disable: true } |
| 11 | PlantUML 报 Cannot run program "/opt/local/bin/dot" | 未装 graphviz，只影响渲染不影响 -checkonly | 记为边界，需要出图时再装 |

---

## 8. 边界：这一阶段仍然没有证明的事

1. **真实 LLM 未调用**：镜像里没有 key，generate_run 走的是 skipped(llm_not_configured)。
   文档生成质量、修复成功率、token 成本仍然是未知数。
2. **容器内访问 github.com 失败**：宿主 443 不稳定、容器内直接超时。端到端用的是本地裸仓库，
   因此"真实 GitHub 克隆 + 大仓库 diff"的性能没有测。
3. **镜像瘦身已完成，但不是终点**：多阶段 + jlink + 删 pip/setuptools 后，运行镜像从
   850MB 降到 581MB（-31.6%），测试镜像 582MB。剩余大头是 venv 162MB 与 Debian 基础层；
   再往下压要换 alpine/musl 或 distroless，tree-sitter 原生扩展的编译成本会上升，暂不做。
4. **压测补上了 API 面，没补 worker 面**：阶段八用 `scripts/load_test.py` 对 compose 里的
   API + PG 跑了 read 200/并发 20、mixed 100/并发 10，0 失败；8 并发人工编辑 8/8 成功。
   但 prefork worker 的并发任务、Redis visibility_timeout 与看门狗的真实交互仍未压测。
5. **没有 graphviz**：-checkonly 可用，渲染 PNG/SVG 会失败。
6. **Windows 特有的 workaround 没有进镜像**：命名管道提权、PATH、buildx 插件发现，都是宿主侧问题，
   换台 Linux 机器或 CI 都不需要，故不写进 Dockerfile。

---

## 9. 使用命令

~~~powershell
# 构建运行镜像（默认就是最后的 release 阶段）
docker build -t livingdoc:slim .

# 构建带测试的镜像（用于容器内 pytest / 自检）
docker build --target test -t livingdoc:test .

# 起全栈
docker compose up -d --build

# 看状态与日志
docker compose ps
docker compose logs -f api worker

# 健康检查（宿主侧）
curl http://127.0.0.1:8000/healthz
# {"status":"ok","env":"dev","queue":"redis"}

# 容器内自检（215 项测试 + plantuml 判定）
docker run --rm livingdoc:test sh scripts/container_smoke.sh

# 停止（保留数据） / 连数据一起删
docker compose down
docker compose down -v
~~~

---

## 10. 人机协同记录（本阶段）

这一阶段的"人"的部分主要是**提供环境与授权**，而"AI"的部分是**在受限环境里找替代路径**：

1. 我最初判断"WSL 未安装"，用户纠正"我之前已经装过了带 ubuntu 版本的 wsl"。复查发现是沙箱
   禁命名管道造成的假阴性（wsl --status 返回 E_ACCESS_DENIED）。**教训：在受限环境里，
   命令的失败不一定代表被测对象的状态。** 这条后来写进了 §7 第 1 行的根因栏。
2. 用户装好 Docker 后，我把"能不能用"拆成六个可判定项（client/server/context/hello-world/
   compose/wsl 发行版），逐项确认，而不是直接开始写 Dockerfile。
3. 构建反复失败时，我没有一次改多处，而是按"环境变量 → 插件发现 → 证书 → 网络 → 依赖版本"
   逐层缩小范围，每次只改一个变量。§7 的 11 条基本就是这个过程的产物。
4. 自检脚本第一次给出 GOOD rejected 时，我差点去怀疑 PlantUML 版本，最后发现是夹具写错。
   这条被写进了 §5.1 的结论里：**先怀疑夹具，再怀疑被测对象**。

---

## 11. 对论文的意义

这一阶段提供的不是新功能，而是**证据等级的变化**：

- 阶段一到五的结论是"设计上应该成立 + 单元测试通过"；
- 阶段六之后，三条核心 ADR 有了运行日志与可复现命令；
- 同时暴露了"纸面设计漏掉的依赖"（字体库、PYTHONPATH、健康检查语义），
  这些恰恰是"AI 生成的代码/文档在真实环境里会漏什么"的例证。

如果要给出一条可引用的结论：**容器化最大的价值不是部署，而是把"我以为能跑"变成"换一台机器也能跑，
并且有日志"。**
