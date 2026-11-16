# LivingDoc 数据库设计

> 目标库：**PostgreSQL 16**　｜　开发降级：SQLite 3（见 §5）　｜　迁移工具：Alembic

---

## 1. 设计原则

1. **枚举一律用 `varchar + CHECK`，不用 `CREATE TYPE`**。PG 的 enum 加值时需要 `ALTER TYPE`，迁移不可回滚；而本项目状态机会频繁加状态（比如后面加 `superseded`、`cost_exceeded`）。用 CHECK 改一行 DDL 就行，且 SQLite 完全兼容。
2. **主键类型混用是刻意的**：对外暴露的资源（project / run / document）用 `uuid`，防止 URL 遍历猜 ID；纯日志型、只做内部关联的（changed_files、ai_invocations、document_revisions）用 `bigserial`，省索引空间、写入更快。
3. **一张表只承担一种"变化速率"**。documents（元信息，低频更新）与 document_revisions（正文，高频追加）拆开，避免每次生成新版本都去 update 主表行，减少锁竞争。
4. **不变式写进数据库**。幂等、去重、唯一版本号全部由约束保证，应用层只负责"优雅地接住异常"，不负责"记得检查"。
5. **所有时间戳 `timestamptz`**，不用 `timestamp`。服务器在 Asia/Shanghai，但 Webhook 的 `X-Gitee-Timestamp` 是毫秒 UTC，混用会出跨时区 bug。

---

## 2. 实体关系总览

```text
projects ──┬── webhook_deliveries     （原始事件，去重与审计）
           ├── commits                （提交元信息）
           ├── analysis_runs ──┬── changed_files
           │                   └── ai_invocations ──┐
           ├── code_entities ──── entity_relations  │
           └── documents ──── document_revisions ───┴── publish_records

prompt_templates ──(name+version)──▶ ai_invocations
users ── api_tokens
```

**分层看**：

| 层 | 表 | 说明 |
|---|---|---|
| 配置域 | projects, users, api_tokens, prompt_templates | 变更极少 |
| 事件域 | webhook_deliveries, commits | 只追加，按月归档 |
| 执行域 | analysis_runs, changed_files, ai_invocations | 只追加，run 是聚合根 |
| 知识域 | code_entities, entity_relations | 增量 upsert，软删除 |
| 产物域 | documents, document_revisions, publish_records | 版本化，可回溯 |

---

## 3. P0 表 DDL（MVP 必须）

```sql
-- ============================================================
-- 0. 扩展
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
-- CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- 可选：实体名模糊搜索

-- ============================================================
-- 1. projects：仓库接入配置（聚合根）
-- ============================================================
CREATE TABLE projects (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                varchar(64)  NOT NULL UNIQUE,          -- URL 友好标识，如 livingdoc-demo
    name                varchar(128) NOT NULL,
    provider            varchar(16)  NOT NULL
                        CHECK (provider IN ('github', 'gitee', 'gitlab')),
    repo_full_name      varchar(255) NOT NULL,                 -- owner/repo
    repo_url            varchar(512) NOT NULL,
    default_branch      varchar(128) NOT NULL DEFAULT 'main',
    webhook_secret_enc  text         NOT NULL,                 -- Fernet 密文，严禁明文
    access_token_enc    text,                                  -- 私有库只读 token 密文
    clone_mode          varchar(16)  NOT NULL DEFAULT 'https'
                        CHECK (clone_mode IN ('https', 'ssh')),
    language_hint       varchar(32),                           -- python / typescript / java，用于选 parser
    is_active           boolean      NOT NULL DEFAULT true,
    settings            jsonb        NOT NULL DEFAULT '{}'::jsonb,
                        -- 例：{"ignore_globs":[".venv/**","*.lock"],"max_file_bytes":1048576,
                        --      "doc_types":["class_diagram","api_doc"],"model":"deepseek-coder"}
    last_run_at         timestamptz,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_project_repo UNIQUE (provider, repo_full_name)
);

COMMENT ON COLUMN projects.webhook_secret_enc IS 'Webhook 验签密钥密文；轮换时保留旧值 24h 以容忍在途请求';
COMMENT ON COLUMN projects.settings IS '项目级分析参数，避免改行为要发版';

-- ============================================================
-- 2. webhook_deliveries：原始事件（幂等 + 审计 + 重放入口）
-- ============================================================
CREATE TABLE webhook_deliveries (
    id            bigserial PRIMARY KEY,
    project_id    uuid         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider      varchar(16)  NOT NULL,
    delivery_id   varchar(128),          -- GitHub: X-GitHub-Delivery；Gitee 可能为空
    event_type    varchar(64)  NOT NULL, -- push / pull_request / ping
    action        varchar(32),           -- opened / synchronize / closed
    ref           varchar(255),          -- refs/heads/main
    before_sha    varchar(64),
    after_sha     varchar(64),
    signature_ok  boolean      NOT NULL,
    payload       jsonb        NOT NULL,
    dedupe_key    varchar(200) NOT NULL,
    received_at   timestamptz  NOT NULL DEFAULT now(),
    processed_at  timestamptz,
    status        varchar(16)  NOT NULL DEFAULT 'received'
                  CHECK (status IN ('received','ignored','enqueued','duplicate','failed')),
    note          text
);

-- 幂等的物理保证：同一 project 的同一次变更只能落一条
CREATE UNIQUE INDEX ux_delivery_dedupe ON webhook_deliveries (project_id, dedupe_key);
CREATE INDEX ix_delivery_recent  ON webhook_deliveries (received_at DESC);
CREATE INDEX ix_delivery_project ON webhook_deliveries (project_id, received_at DESC);

COMMENT ON COLUMN webhook_deliveries.dedupe_key IS
  'sha256(project_id | event_type | action | after_sha | pr_number)。用内容 hash 而非 delivery header，因为平台重发时 header 会变';

-- ============================================================
-- 3. analysis_runs：一次分析任务（状态机主体）
-- ============================================================
CREATE TABLE analysis_runs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id       uuid        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    delivery_id      bigint      REFERENCES webhook_deliveries(id) ON DELETE SET NULL,
    trigger_type     varchar(16) NOT NULL
                     CHECK (trigger_type IN ('push','pull_request','manual','backfill')),
    trigger_ref      varchar(255),
    pr_number        int,
    base_sha         varchar(64),
    head_sha         varchar(64) NOT NULL,
    status           varchar(16) NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','cloning','diffing','parsing','analyzing',
                                       'rendering','publishing','succeeded','failed',
                                       'canceled','superseded','skipped')),
    stage_detail     text,
    error_code       varchar(64),
    error_message    text,
    files_changed    int         NOT NULL DEFAULT 0,
    entities_changed int         NOT NULL DEFAULT 0,
    tokens_in        bigint      NOT NULL DEFAULT 0,
    tokens_out       bigint      NOT NULL DEFAULT 0,
    cost_usd         numeric(10,6) NOT NULL DEFAULT 0,
    idempotency_key  varchar(200) NOT NULL,
    attempt          int         NOT NULL DEFAULT 1,
    queued_at        timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    CONSTRAINT uq_run_idempotent UNIQUE (project_id, idempotency_key)
);

CREATE INDEX ix_runs_project_time ON analysis_runs (project_id, queued_at DESC);
-- 部分索引：只索引"未终结"的 run，队列查询走它，表再大也快
CREATE INDEX ix_runs_active ON analysis_runs (project_id, status)
    WHERE status NOT IN ('succeeded','failed','canceled','superseded','skipped');

COMMENT ON COLUMN analysis_runs.status IS
  '状态机：queued→cloning→diffing→parsing→analyzing→rendering→publishing→succeeded；任一阶段可转 failed';
COMMENT ON COLUMN analysis_runs.idempotency_key IS
  'sha256(project_id | base_sha | head_sha | trigger_type)，与 delivery 的 dedupe_key 解耦：一次投递可因重试产生多个 run';

-- ============================================================
-- 4. changed_files：本次 run 的文件级变更 + 解析状态
-- ============================================================
CREATE TABLE changed_files (
    id           bigserial PRIMARY KEY,
    run_id       uuid        NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    path         varchar(1024) NOT NULL,
    old_path     varchar(1024),                 -- rename 场景的源路径
    change_type  char(1)     NOT NULL CHECK (change_type IN ('A','M','D','R','C')),
    language     varchar(32),
    additions    int         NOT NULL DEFAULT 0,
    deletions    int         NOT NULL DEFAULT 0,
    patch        text,                          -- 单文件 unified diff
    patch_uri    varchar(512),                  -- patch 超过 256KB 时转文件存储，此处留指针
    parse_status varchar(16) NOT NULL DEFAULT 'pending'
                 CHECK (parse_status IN ('pending','parsed','skipped','parse_error')),
    parse_error  text,
    CONSTRAINT uq_file_per_run UNIQUE (run_id, path)
);

CREATE INDEX ix_files_run ON changed_files (run_id);

COMMENT ON COLUMN changed_files.patch IS '大文件不落 PG：> 256KB 写文件存储并置 patch_uri，避免行溢出拖慢查询';

-- ============================================================
-- 5. code_entities：AST 提取的代码实体（知识域核心）
-- ============================================================
CREATE TABLE code_entities (
    id             bigserial PRIMARY KEY,
    project_id     uuid        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_path      varchar(1024) NOT NULL,
    entity_type    varchar(32) NOT NULL
                   CHECK (entity_type IN ('module','class','function','method',
                                          'interface','enum','route','table')),
    qualified_name varchar(512) NOT NULL,       -- app.services.auth.AuthService.login
    signature      text,                        -- 参数与返回类型，供 Prompt 直接用
    start_line     int         NOT NULL CHECK (start_line > 0),
    end_line       int         NOT NULL CHECK (end_line >= start_line),
    source_hash    varchar(64) NOT NULL,        -- sha256(规范化源码片段) → 变更检测核心
    is_public      boolean     NOT NULL DEFAULT true,
    docstring      text,
    metadata       jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- 装饰器/注解/是否 async 等
    last_run_id    uuid        REFERENCES analysis_runs(id) ON DELETE SET NULL,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz,                 -- 软删除：保留历史，供"某类何时消失"查询
    CONSTRAINT uq_entity_identity UNIQUE (project_id, file_path, qualified_name, entity_type)
);

CREATE INDEX ix_entities_hash    ON code_entities (project_id, source_hash);
CREATE INDEX ix_entities_file    ON code_entities (project_id, file_path);
CREATE INDEX ix_entities_name    ON code_entities (project_id, qualified_name);
CREATE INDEX ix_entities_alive   ON code_entities (project_id) WHERE deleted_at IS NULL;

COMMENT ON COLUMN code_entities.source_hash IS
  '只对"去掉注释与空白后的 token 流"求 hash。改注释不该触发文档重生成，否则噪声太大';

-- ============================================================
-- 6. documents：逻辑文档（一个 target_key 一份）
-- ============================================================
CREATE TABLE documents (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id          uuid         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    doc_type            varchar(32)  NOT NULL
                        CHECK (doc_type IN ('class_diagram','sequence_diagram','api_doc',
                                            'module_summary','data_model','readme')),
    target_key          varchar(512) NOT NULL,   -- 稳定业务主键：module:app/services/auth
                                                 -- 或 api:POST /v1/users 或 class:AuthService
    title               varchar(255) NOT NULL,
    content_format      varchar(16)  NOT NULL
                        CHECK (content_format IN ('plantuml','mermaid','markdown','openapi')),
    current_revision_id bigint,                  -- FK 后置，见 §4
    status              varchar(20)  NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','validated','published','stale','archived','failed')),
    manual_edited       boolean      NOT NULL DEFAULT false,
    owner_run_id        uuid         REFERENCES analysis_runs(id) ON DELETE SET NULL,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_target UNIQUE (project_id, doc_type, target_key)
);

CREATE INDEX ix_documents_project ON documents (project_id, doc_type, status);

COMMENT ON COLUMN documents.target_key IS
  '文档的身份锚点。用"模块路径/接口签名"而非文件名，因为文件会重命名，业务实体不会';
COMMENT ON COLUMN documents.manual_edited IS
  '人改过的文档，后续自动更新必须走冲突流程（ADR-006），不得静默覆盖';

-- ============================================================
-- 7. document_revisions：不可变版本快照
-- ============================================================
CREATE TABLE document_revisions (
    id               bigserial PRIMARY KEY,
    document_id      uuid        NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version          int         NOT NULL CHECK (version > 0),
    content          text        NOT NULL,
    content_hash     varchar(64) NOT NULL,
    change_summary   text,                       -- AI 生成的一句话变更说明，直接用于 PR 评论
    render_ok        boolean,
    render_error     text,
    svg_uri          varchar(512),
    source_entity_ids bigint[],                  -- 证据链：这份文档由哪些实体推导而来
    model_name       varchar(64),
    prompt_version   varchar(32),
    ai_invocation_id bigint,
    created_by       varchar(16) NOT NULL DEFAULT 'ai'
                     CHECK (created_by IN ('ai','human','ai+human')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_revision_version UNIQUE (document_id, version)
);

CREATE INDEX ix_revisions_doc ON document_revisions (document_id, version DESC);

COMMENT ON COLUMN document_revisions.source_entity_ids IS
  '反向可追溯：从文档点回代码实体。缺失这条链，评审无法验证 AI 有没有编造';
COMMENT ON COLUMN document_revisions.render_ok IS
  'PlantUML 编译结果。false 的版本不进 current_revision，但保留下来供分析失败模式';

-- ============================================================
-- 8. ai_invocations：AI 调用审计（论文实验数据来源）
-- ============================================================
CREATE TABLE ai_invocations (
    id                bigserial PRIMARY KEY,
    run_id            uuid        REFERENCES analysis_runs(id) ON DELETE CASCADE,
    document_id       uuid        REFERENCES documents(id) ON DELETE SET NULL,
    purpose           varchar(48) NOT NULL,   -- class_diagram / api_extract / arch_summary / repair
    provider          varchar(32) NOT NULL,   -- deepseek / zhipu
    model             varchar(64) NOT NULL,
    prompt_template   varchar(64) NOT NULL,
    prompt_version    varchar(32) NOT NULL,
    prompt_hash       varchar(64) NOT NULL,
    request_payload   jsonb,                  -- 脱敏后的完整请求，用于复现实验
    response_raw      text,
    prompt_tokens     int,
    completion_tokens int,
    latency_ms        int,
    attempt           int         NOT NULL DEFAULT 1,
    status            varchar(16) NOT NULL
                      CHECK (status IN ('success','invalid_output','rate_limited','timeout','error')),
    error_message     text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_ai_run         ON ai_invocations (run_id);
CREATE INDEX ix_ai_prompt_hash ON ai_invocations (prompt_hash, status);
CREATE INDEX ix_ai_time        ON ai_invocations (created_at DESC);
```

---

## 4. P1 表 DDL（二期 / 加分项）

```sql
-- ============================================================
-- 9. commits：提交元信息（用于"某次变更由谁引入"的追溯）
-- ============================================================
CREATE TABLE commits (
    id           bigserial PRIMARY KEY,
    project_id   uuid        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sha          varchar(64) NOT NULL,
    parent_sha   varchar(64),
    branch       varchar(128),
    author_name  varchar(128),
    author_email varchar(255),
    message      text,
    committed_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_commit UNIQUE (project_id, sha)
);

-- ============================================================
-- 10. entity_relations：实体关系图（Neo4j 的替代，ADR-004）
-- ============================================================
CREATE TABLE entity_relations (
    id         bigserial PRIMARY KEY,
    project_id uuid        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    src_id     bigint      NOT NULL REFERENCES code_entities(id) ON DELETE CASCADE,
    dst_id     bigint      REFERENCES code_entities(id) ON DELETE CASCADE,
    dst_name   varchar(512),          -- dst 未解析到时保留原始名（跨仓库/动态调用）
    relation   varchar(32) NOT NULL
               CHECK (relation IN ('calls','inherits','implements','imports',
                                   'contains','decorates','reads','writes')),
    evidence   varchar(1200),         -- file.py:120
    confidence numeric(3,2) NOT NULL DEFAULT 1.00 CHECK (confidence BETWEEN 0 AND 1),
    run_id     uuid        REFERENCES analysis_runs(id) ON DELETE SET NULL,
    CONSTRAINT uq_relation UNIQUE (project_id, src_id, dst_id, relation)
);

CREATE INDEX ix_rel_src ON entity_relations (src_id, relation);
CREATE INDEX ix_rel_dst ON entity_relations (dst_id, relation);

-- 影响面分析示例：反向 3 跳，找出"改了这个函数会波及谁"
-- WITH RECURSIVE impact AS (
--     SELECT dst_id AS id, 1 AS depth FROM entity_relations WHERE src_id = $1
--     UNION
--     SELECT r.dst_id, i.depth + 1 FROM entity_relations r
--     JOIN impact i ON r.src_id = i.id
--     WHERE i.depth < 3
-- )
-- SELECT e.* FROM impact i JOIN code_entities e ON e.id = i.id;

-- ============================================================
-- 11. prompt_templates：Prompt 版本化（Prompt 工程的载体）
-- ============================================================
CREATE TABLE prompt_templates (
    id            bigserial PRIMARY KEY,
    name          varchar(64) NOT NULL,
    version       varchar(32) NOT NULL,        -- v1 / v2-strict-syntax
    purpose       varchar(48) NOT NULL,
    system_prompt text        NOT NULL,
    user_template text        NOT NULL,        -- Jinja2，变量：entities / relations / diff
    output_schema jsonb,                       -- JSON Schema，供 jsonschema 校验
    is_active     boolean     NOT NULL DEFAULT false,
    notes         text,                        -- 这一版为什么改，用于论文里的迭代记录
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_prompt_version UNIQUE (name, version)
);

-- 同一 name 只允许一个 active 版本
CREATE UNIQUE INDEX ux_prompt_active ON prompt_templates (name) WHERE is_active;

-- ============================================================
-- 12. publish_records：发布到外部知识库的投递记录
-- ============================================================
CREATE TABLE publish_records (
    id            bigserial PRIMARY KEY,
    document_id   uuid        NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision_id   bigint      NOT NULL REFERENCES document_revisions(id) ON DELETE CASCADE,
    target        varchar(32) NOT NULL
                  CHECK (target IN ('local_wiki','feishu','notion','confluence',
                                    'github_pr_comment','git_commit')),
    external_id   varchar(255),
    external_url  varchar(1024),
    status        varchar(16) NOT NULL CHECK (status IN ('pending','success','failed')),
    error_message text,
    published_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_publish_doc ON publish_records (document_id, created_at DESC);

> **实现说明（2026-11-06）**：SQLite/PG 实际建表见 `app/store.py` 的 `publish_records`。
> 与上面的产品化 DDL 有三处差异：`document_id` / `revision_id` 可空（run 级投递失败没有
> 具体文档）、增加 `run_id` / `project_id` / `detail JSON`、`status` 允许 `skipped`。
> 这是为了"投递失败也要可审计，但不能影响 run 状态"。

-- ============================================================
-- 13. users / api_tokens：最小鉴权
-- ============================================================
CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         varchar(255) NOT NULL UNIQUE,
    display_name  varchar(128),
    password_hash text,
    role          varchar(16) NOT NULL DEFAULT 'member'
                  CHECK (role IN ('admin','member','viewer')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_tokens (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         varchar(64) NOT NULL,
    token_hash   varchar(64) NOT NULL UNIQUE,  -- sha256(token)，明文只在创建时返回一次
    scopes       text[]      NOT NULL DEFAULT '{}',
    last_used_at timestamptz,
    expires_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- 14. 外键补全：documents → document_revisions（循环依赖后置）
-- ============================================================
ALTER TABLE documents
    ADD CONSTRAINT fk_doc_current_revision
    FOREIGN KEY (current_revision_id) REFERENCES document_revisions(id) ON DELETE SET NULL;
```

**为什么 current_revision_id 要后置**：documents 与 document_revisions 互相引用（文档指向当前版本，版本指向所属文档），建表顺序上必然有一个外键要延后。这里选择让"指向最新"的那条延后，因为它在业务上更弱（版本被删时文档应继续存在，故 `ON DELETE SET NULL`）。

---

## 5. SQLite 降级方案

开发机没有 Docker 时用 SQLite 跑通全链路。差异只有 6 处，用 SQLAlchemy 的方言分支 + Alembic 条件迁移处理：

| PG 特性 | SQLite 替代 | 影响 |
|---|---|---|
| `uuid` + `gen_random_uuid()` | `CHAR(32)`，主键由应用层 `uuid4().hex` 生成 | 无 |
| `jsonb` | `JSON`（存 TEXT） | 失去 JSON 路径索引；本项目查询不依赖 JSON 内部字段，可接受 |
| `text[]` / `bigint[]` | `JSON` 数组 | 失去数组包含查询，只做展示则无影响 |
| 部分索引 `WHERE` | 普通复合索引 | 队列扫描略慢，数据量 < 10 万行无感 |
| `numeric(10,6)` | `NUMERIC`（无精度约束） | 成本统计精度下降，可用整数微美元规避 |
| `COMMENT ON` | 不支持 | 注释只存在于 Alembic 与本文档 |

**结论**：开发用 SQLite，答辩/生产用 PostgreSQL；两者通过 `DATABASE_URL` 切换，代码零分支（除上述建表 DDL 由迁移脚本处理）。

---

## 6. 索引与约束清单（答辩速查）

| 索引 / 约束 | 服务的问题 | 不加会怎样 |
|---|---|---|
| ux_delivery_dedupe (project_id, dedupe_key) | 重复 Webhook 只分析一次 | 平台重发 → 同一提交生成两份文档，人工修改被覆盖 |
| uq_run_idempotent (project_id, idempotency_key) | 任务级幂等 | Worker 重复消费 → token 双倍消耗 |
| uq_file_per_run (run_id, path) | 文件不重复 | 大 PR 里 rename 会重复插入 |
| uq_entity_identity | 实体唯一 | AST 解析重跑产生重复实体，关系图出现重影 |
| ix_entities_hash | 快速判断"这个函数真的变了吗" | 每次全量重解析，token 成本翻倍 |
| uq_document_target | 一个业务实体只有一份文档 | 文档碎片化，读者找不到"最新的那份" |
| uq_revision_version | 版本号不跳号 | 并发写出现 version 3 与 4 同时存在，diff 失效 |
| ux_prompt_active (name) WHERE is_active | 同名模板只有一个生效版 | Prompt 灰度时行为不可预测，实验不可复现 |
| ix_runs_active (partial) | 队列扫描 | 表增长后 `WHERE status='queued'` 全表扫 |

---

## 7. 数据生命周期

| 表 | 增长速率（估算） | 策略 |
|---|---|---|
| webhook_deliveries | 每 push 1 行，含 payload（~10KB） | 保留 90 天，按月分区；payload 超过 1MB 转对象存储 |
| analysis_runs | 每 push 1 行（~1KB） | 永久保留（论文数据） |
| ai_invocations | 每 run 5–20 行，含 request/response（~20KB） | 保留原始 30 天，之后只留统计字段，原文转冷存储 |
| changed_files | 每 run 5–50 行 | 保留 90 天；patch 大字段优先清理 |
| code_entities | 稳定在项目实体规模（~10^3–10^4） | 软删除，不物理删；每季度 VACUUM |
| document_revisions | 每 run 1–10 行 | 永久保留，但超过 50 版后旧版 content 转冷存储（保留 hash 与摘要） |

**容量估算（中等规模项目，日均 20 次 push）**：一年后 webhook_deliveries ≈ 7300 行、analysis_runs ≈ 7300 行、ai_invocations ≈ 10 万行、revisions ≈ 3 万行。单机 PG 完全无压力，这也印证了"现阶段不需要 Neo4j / 不需要分库"的判断。

---

## 8. 迁移与演进

- **Alembic**：每个 revision 带 `downgrade()`，因为答辩时要演示"加一个字段"的完整流程。
- **零停机加字段**：一律 `ADD COLUMN ... NULL` 或带默认值的可空列，绝不在大表上 `ALTER TYPE`。
- **状态机演进**：新增状态只改 CHECK 约束（`DROP CONSTRAINT` + `ADD CONSTRAINT ... NOT VALID`），避免锁表。
- **预定演进方向**（写进 v2 backlog，答辩可主动提，显示想清楚了）：
  1. `documents.parent_id` —— 支持"总架构图 → 子系统图 → 类图"的层级文档树
  2. `document_revisions.review_status` —— 加人工评审流（AI 生成 → 人审 → 发布）
  3. `code_entities.embedding vector(1536)` —— pgvector 做语义检索，替代纯 qualified_name 匹配
