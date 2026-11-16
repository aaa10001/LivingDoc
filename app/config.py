"""集中式配置：所有可调参数走环境变量 / .env，代码里不出现魔法值。

为什么单独一个文件：Celery worker 和 FastAPI 进程必须读到同一份配置
（尤其是 repo 缓存目录），否则 worker 找不到 API 进程克隆的仓库。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录：app/config.py -> app/ -> 根
BASE_DIR = Path(__file__).resolve().parent.parent
VAR_DIR = BASE_DIR / "var"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ---------- 应用 ----------
    app_name: str = "LivingDoc"
    env: str = "dev"
    timezone: str = "Asia/Shanghai"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # ---------- GitHub ----------
    github_webhook_secret: str = "dev-secret-change-me"
    github_token: str = ""  # 只读 PAT；留空则只能访问公开仓库
    payload_max_bytes: int = 25 * 1024 * 1024  # GitHub 的 payload 上限是 25MB

    # ---------- Celery ----------
    broker_url: str = f"sqla+sqlite:///{(VAR_DIR / 'celery-broker.sqlite').as_posix()}"
    result_backend: str = f"db+sqlite:///{(VAR_DIR / 'celery-results.sqlite').as_posix()}"
    # 兼容两种写法：pydantic 字段名 TASK_ALWAYS_EAGER，以及历史文档/
    # .env.example 里的 CELERY_TASK_ALWAYS_EAGER。之前只认字段名，导致照
    # .env.example 配的人以为开了 eager，实际仍然在等 worker（真实踩过）。
    task_always_eager: bool = Field(
        False,
        validation_alias=AliasChoices("TASK_ALWAYS_EAGER", "CELERY_TASK_ALWAYS_EAGER"),
    )
    analyze_max_retries: int = 3

    # ---------- Git ----------
    work_dir: Path = VAR_DIR / "work"
    repo_cache_dir: Path = VAR_DIR / "repos"
    git_timeout_seconds: int = 300
    git_clone_depth: int = 0  # 0 = 完整历史；>0 = 浅克隆（省带宽，但拿不到 base commit）
    diff_max_files: int = 3000

    # ---------- 大模型 ----------
    llm_provider: str = "deepseek"  # deepseek | zhipu；决定默认 base_url/model
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 3
    llm_max_tokens: int = 2048
    llm_temperature: float = 0.2
    self_correct_enabled: bool = True
    self_correct_max_attempts: int = 3
    plantuml_jar: Path = BASE_DIR / "tools" / "plantuml.jar"
    java_bin: str = "java"

    # ---------- 持久化 ----------
    # 默认 SQLite：这门课要在没有 Docker/PostgreSQL 的机器上跑起来。
    # 换 PG 只改这一个串，store.py 的表定义两边通用。
    database_url: str = f"sqlite:///{(VAR_DIR / 'livingdoc.sqlite').as_posix()}"
    # False 时任务只做本段、不自动入队下一段，方便单步调试与复现
    pipeline_enabled: bool = True

    # ---------- 发布（P1：PR 评论 / 知识库推送 / 仓库回写）----------
    # 逗号分隔，例如 "local_wiki,github_pr_comment,github_repo"。
    # local_wiki 永远可用（写本地目录，离线演示靠它）；另外三个缺配置就自动跳过，
    # 绝不能因为没配 token 让一次成功的生成跑成失败。
    publish_targets: str = "local_wiki"
    publish_timeout_seconds: float = 15.0
    kb_dir: Path = VAR_DIR / "kb"
    # 渲染出的 SVG 落这里，评审界面通过 /api/v1/renders/<sha>.svg 取
    renders_dir: Path = VAR_DIR / "renders"
    # PR 评论 / 评审页面里回链用的站点根地址（不填就不生成链接）
    public_base_url: str = "http://127.0.0.1:8000"
    # GitHub API 根；测试可指向 MockTransport，企业版也可换域名
    github_api_base: str = "https://api.github.com"
    # 文档回写仓库：默认写到独立分支并发 PR；要直接同步 main 就把分支改成 main、
    # 同时把 GITHUB_DOCS_OPEN_PR 设为 false。目录固定，避免和人工文档混在一起。
    github_docs_branch: str = "livingdoc/docs"
    github_docs_dir: str = "docs/livingdoc"
    github_docs_open_pr: bool = True
    # 通用知识库 webhook（飞书/Notion/Confluence 的中间层都可以接）
    kb_webhook_url: str = ""
    kb_webhook_secret: str = ""

    @property
    def publish_target_names(self) -> list[str]:
        """把逗号分隔的配置切成去重后的列表（保持声明顺序）。"""
        seen: list[str] = []
        for item in self.publish_targets.replace(";", ",").split(","):
            name = item.strip()
            if name and name not in seen:
                seen.append(name)
        return seen

    # ---------- Webhook 去重（内存态占位，阶段三换成 DB 唯一索引）----------
    dedupe_ttl_seconds: int = 3600
    dedupe_max_items: int = 10000

    @field_validator("work_dir", "repo_cache_dir", "plantuml_jar", "kb_dir", "renders_dir", mode="after")
    @classmethod
    def _absolute_paths(cls, value: Path) -> Path:
        """相对路径统一按仓库根解析。

        为什么不在各处 `Path(...).resolve()`：`.env` 里写的是 `var/kb`，
        如果 API 从别的目录启动，相对路径会落到进程 cwd，worker 和 API 看到
        两个不同的知识库。这里一次解析，所有进程拿到的都是同一个绝对路径。
        真实 GitHub 测试里 `local_wiki` 的 `.as_uri()` 就是因为相对路径炸的。
        """
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path.resolve()

    def ensure_dirs(self) -> None:
        for path in (
            VAR_DIR,
            self.work_dir,
            self.repo_cache_dir,
            self.work_dir / "runs",
            self.kb_dir,
            self.renders_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
