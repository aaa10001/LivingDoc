# LivingDoc REST API 设计

> Base URL: `/api/v1`　｜　内容类型: `application/json`　｜　错误格式: RFC 7807 (`application/problem+json`)

> **实现状态（2026-11-06）**：本文是完整产品形态的设计稿（45 个端点）；当前代码实现的是其中
> 支撑评审闭环的一个子集，见 `app/api/review.py`。已实现：项目/文档列表与详情、版本列表与
> 单版本、版本 diff、人工编辑、接受 AI 候选、渲染（PlantUML 编译或静态校验降级）、发布到
> 投递渠道、run 与 AI 调用审计、publish_records 查询，以及 `/ui` 静态评审页。
> 未实现：认证/Token、SSE、实体图、Prompt 模板、归档、全文检索等。设计表中的这些端点仍是
> 后续演进方向，不要按"已可用"理解。

---

## 1. 全局约定

| 项 | 约定 | 理由 |
|---|---|---|
| 版本 | 路径前缀 `/api/v1` | 大版本变更时才升；字段演进用"加法"兼容 |
| 认证 | `Authorization: Bearer <token><` | 管理员可签发细粒度 scope token |
| Webhook 认证 | `X-Hub-Signature-256` (GitHub) / `X-Gitee-Token` (Gitee) | 平台各自规范，不改 |
| 分页 | 游标式 `?limit=20&cursor=xxx` | 不用 offset：run 表持续追加，offset 会漏行/重行 |
| 排序 | `?sort=-received_at`，减号表倒序 | 单一约定，避免每接口自定义 |
| 幂等 | 写操作支持 `Idempotency-Key` 头，24h 内重复请求返回原响应 | 前端重试 / 手动重放的安全网 |
| 时间 | 一律 RFC 3339 带时区，`2026-11-05T15:50:24+08:00` | 与 `timestamptz` 对齐 |
| 长任务 | 返回 `202 Accepted` + `Location: /api/v1/runs/{id}` | 客户端轮询或订阅 SSE |
| 字段命名 | `snake_case` | 与数据库一致，少一层心智映射 |

---

## 2. 端点清单

### 2.1 Webhook 接入（无 Bearer 认证，靠签名）

| 方法 | 路径 | 说明 | 返回 |
|---|---|---|---|
| POST | `/webhooks/{provider}` | 接收 push / pull_request / ping。provider ∈ github \| gitee \| gitlab | 202 已入队 / 200 duplicate / 401 验签失败 |
| GET | `/webhooks/deliveries` | 投递审计列表，支持 `project_id,status,since` 过滤 | 分页列表 |
| GET | `/webhooks/deliveries/{id}` | 单条投递详情（含 payload） | 详情 |
| POST | `/webhooks/deliveries/{id}/replay` | 重放投递（调试必备，答辩演示"补分析"） | 202 + 新 run_id |

**关键行为**：验签失败返回 401 且**不落库**（防止伪造请求刷表）；验签成功但 `dedupe_key` 冲突返回 **200** 而非 409 —— 平台会把非 2xx 当作投递失败并持续重试，必须让它"成功退出"。

### 2.2 项目

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/projects` | 列表（含最近一次 run 状态摘要） |
| POST | `/projects` | 创建；返回 `webhook_secret` **仅此一次**（之后只可轮换不可读） |
| GET | `/projects/{id}` | 详情 |
| PATCH | `/projects/{id}` | 改配置（settings 用 JSON Merge Patch） |
| DELETE | `/projects/{id}` | 软删除（`is_active=false`），保留历史 run |
| POST | `/projects/{id}/rotate-secret` | 轮换密钥，旧密钥保留 24h 容忍在途请求 |
| POST | `/projects/{id}/test-connection` | 校验 token 能读仓库、能列分支；返回延迟 |
| GET | `/projects/{id}/webhook-setup` | 返回需要用户粘贴到平台的 URL 与 secret 提示（新手引导） |

### 2.3 分析任务（Run）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/projects/{id}/runs` | 手动触发。body: `{base_sha?, head_sha?, branch?, doc_types?}`；缺省用最近两次提交 |
| GET | `/projects/{id}/runs` | 历史列表，可按 `status,trigger_type,since` 过滤 |
| GET | `/runs/{run_id}` | 详情（含各阶段耗时、token 消耗、错误） |
| GET | `/runs/{run_id}/events` | **SSE 进度流**：`event: stage` / `event: log` / `event: done`。答辩实时演示的关键 |
| GET | `/runs/{run_id}/files` | 本次变更文件列表（含解析状态） |
| GET | `/runs/{run_id}/entities` | 本次变更影响的实体（带 `change`: added/modified/removed） |
| GET | `/runs/{run_id}/invocations` | 该 run 的全部 AI 调用（token/延迟/状态）→ 论文表格直接由它生成 |
| POST | `/runs/{run_id}/cancel` | 取消（仅 queued/早期阶段可取消） |
| POST | `/runs/{run_id}/retry` | 从失败阶段重入，`attempt + 1` |

### 2.4 代码实体与关系图

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/projects/{id}/entities?q=&file=&type=&limit=` | 实体检索（名称前缀 / 文件 / 类型） |
| GET | `/entities/{entity_id}` | 实体详情（含源码片段、docstring、所属 run） |
| GET | `/entities/{entity_id}/graph?depth=2&relations=calls,inherits` | 邻域子图，返回 `{nodes, edges}`（前端直接喂给 ECharts/Cytoscape） |
| GET | `/projects/{id}/impact?entity_id=&depth=3` | **影响面分析**：改这个函数会波及哪些实体（反向递归 CTE） |
| GET | `/projects/{id}/graph/export?format=cypher` | 导出为 Cypher 脚本（未来接 Neo4j 的迁移口，ADR-004） |

### 2.5 文档

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/projects/{id}/documents?doc_type=&status=&q=` | 文档列表 |
| GET | `/documents/{doc_id}` | 当前版本全文（`?format=plantuml\|svg\|markdown`） |
| GET | `/documents/{doc_id}/revisions` | 版本列表（含 `created_by`、`change_summary`） |
| GET | `/documents/{doc_id}/revisions/{version}` | 指定版本 |
| GET | `/documents/{doc_id}/diff?from=3&to=4` | **文档版本 diff**（unified diff 文本 + 结构化变更摘要） |
| PUT | `/documents/{doc_id}` | 人工编辑：创建 `created_by='human'` 的新版本，置 `manual_edited=true` |
| POST | `/documents/{doc_id}/render` | 触发 PlantUML 编译，返回 `{render_ok, svg_url, error}` |
| POST | `/documents/{doc_id}/accept-ai` | 人审通过某条 AI 候选更新（ADR-006 的冲突消解入口） |
| POST | `/documents/{doc_id}/publish` | body: `{target, message}`；发布到知识库并写 publish_records |
| POST | `/documents/{doc_id}/archive` | 归档（实体已删除时由系统自动调用） |
| GET | `/documents/search?q=` | 全文检索（PG `tsvector`） |

### 2.6 Prompt 模板

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/prompt-templates` | 列表（含 active 标记与历史版本） |
| POST | `/prompt-templates` | 新建版本（不可修改已发布版本，只增不改） |
| POST | `/prompt-templates/{id}/activate` | 激活（同 name 自动下线旧版） |
| POST | `/prompt-templates/{id}/preview` | 用指定 run 的上下文试渲染，**不落库**，返回 prompt 全文与预估 token |

### 2.7 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针（进程在不在） |
| GET | `/readyz` | 就绪探针（DB / broker 通不通） |
| GET | `/metrics` | Prometheus 文本格式 |
| GET | `/me` | 当前 token 身份与 scope |
| POST | `/auth/token` | 换取 token（MVP 用静态管理员账号） |

### 2.8 当前实现（评审闭环，2026-11-06）

| 方法 | 实际路径 | 说明 |
|---|---|---|
| GET | `/api/v1/meta` | 版本、环境、可用投递目标、评审页 base URL |
| GET | `/api/v1/projects` | 项目列表 + 文档数 / 待确认数 / 最近 run |
| GET | `/api/v1/projects/{id}` | 项目详情 |
| GET | `/api/v1/projects/{id}/documents` | 支持 `doc_type / status / q` |
| GET | `/api/v1/projects/{id}/runs` | 项目 run 列表 |
| GET | `/api/v1/runs`、`/api/v1/runs/{id}` | run 列表/详情（含 invocations、publish_records） |
| GET | `/api/v1/documents/{id}` | 文档详情 + current + latest 候选 |
| GET | `/api/v1/documents/{id}/revisions` | 版本列表（默认不含正文） |
| GET | `/api/v1/documents/{id}/revisions/{version}` | 指定版本正文 |
| GET | `/api/v1/documents/{id}/diff?from=&to=` | unified diff + 增删行统计 |
| PUT | `/api/v1/documents/{id}` | 人工编辑；body `{content,summary?,force?}`；校验失败 422 + diagnostics |
| POST | `/api/v1/documents/{id}/accept-ai` | 接受 AI 候选 |
| POST | `/api/v1/documents/{id}/render?version=` | PlantUML 编译；jar 缺失时降级静态校验 |
| GET | `/api/v1/renders/{name}` | 内容寻址的 SVG 缓存 |
| POST | `/api/v1/documents/{id}/publish` | body 可空；`target` 缺省用配置 |
| GET | `/api/v1/publish-records` | 投递审计，支持 run/document/project/target 过滤 |
| GET | `/ui/` | 静态评审页（非 `/api/v1`） |

---

## 3. 错误码

统一体：

```json
{
  "type": "https://livingdoc.dev/errors/webhook-signature-invalid",
  "title": "Webhook signature verification failed",
  "status": 401,
  "detail": "X-Hub-Signature-256 mismatch for project demo/api-server",
  "instance": "/api/v1/webhooks/github",
  "trace_id": "01J9Z3K2M4"
}
```

| HTTP | code | 场景 | 客户端该做什么 |
|---|---|---|---|
| 400 | invalid_payload | 字段缺失/类型错 | 修请求，不重试 |
| 401 | unauthorized / webhook_signature_invalid | token 无效、签名不符 | 重新取 token / 检查 secret |
| 403 | scope_insufficient | token 权限不足 | 申请更高 scope |
| 404 | not_found | 资源不存在 | 不重试 |
| 409 | conflict_state | 对已 succeeded 的 run 调 cancel | 刷新状态 |
| 422 | parse_error | JSON 结构合法但语义非法（如 head_sha 不存在） | 校验输入 |
| 429 | rate_limited | 触发限流，带 `Retry-After` | 退避重试 |
| 502 | upstream_failed | Git 平台 / 模型供应商不可用 | 指数退避重试 |
| 500 | internal_error | 兜底 | 带 `trace_id` 反馈 |

**关键区分**：`409` 与 `422` 不要混用 —— 前者是"状态不允许"，后者是"输入不合法"，前端据此决定"刷新"还是"改表单"。

---

## 4. 两个典型交互

### 4.1 手动触发一次分析

```http
POST /api/v1/projects/8f2c.../runs
Authorization: Bearer lvd_xxx
Idempotency-Key: 7d1f9a
Content-Type: application/json

{ "branch": "main", "doc_types": ["class_diagram", "api_doc"] }
```

```http
HTTP/1.1 202 Accepted
Location: /api/v1/runs/3b9e4c1a-...
Retry-After: 2

{ "run_id": "3b9e4c1a-...", "status": "queued", "estimated_seconds": 45 }
```

### 4.2 SSE 进度流

```http
GET /api/v1/runs/3b9e4c1a-.../events
Accept: text/event-stream
```

```text
event: stage
data: {"stage":"cloning","progress":0.10}

event: stage
data: {"stage":"parsing","progress":0.35,"files":12}

event: stage
data: {"stage":"analyzing","progress":0.60,"calls":4,"tokens_in":18234}

event: done
data: {"status":"succeeded","documents":[{"id":"...","type":"class_diagram","url":"/api/v1/documents/..."}]}
```

---

## 5. 刻意不做的事（答辩可主动说明）

| 不做 | 原因 |
|---|---|
| GraphQL | 团队只有一人，REST + OpenAPI 的收益（自动文档、调试直观）远高于 GraphQL 的灵活性 |
| 完整 RBAC | `admin/member/viewer` 三级够用；细粒度权限是产品化阶段的事 |
| 文档内容的 `PUT` 全量覆盖 | 一律走"新建版本"，保证历史不可变；否则 diff 功能失去意义 |
| Webhook 回调鉴权白名单 | 靠 HMAC 验签 + 时间窗（±5 分钟），比 IP 白名单更可靠（平台 IP 会变） |
