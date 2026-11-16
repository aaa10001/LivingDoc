"""配置回归：真实 GitHub 测试里踩到的两个坑。

1. `.env.example` 写的是 CELERY_TASK_ALWAYS_EAGER，字段名却是 task_always_eager，
   于是"开了 eager"实际没开，任务停在 diffing 等 worker。
2. KB_DIR=var/kb 是相对路径，local_wiki 用 Path.as_uri() 时直接 ValueError。
   路径必须在配置层统一按仓库根解析成绝对路径。
"""

from __future__ import annotations

from pathlib import Path

from app.config import BASE_DIR, Settings


def test_celery_env_alias_enables_eager(monkeypatch):
    monkeypatch.delenv("TASK_ALWAYS_EAGER", raising=False)
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "true")

    settings = Settings(_env_file=None)

    assert settings.task_always_eager is True


def test_field_name_still_works(monkeypatch):
    monkeypatch.delenv("CELERY_TASK_ALWAYS_EAGER", raising=False)
    monkeypatch.setenv("TASK_ALWAYS_EAGER", "true")

    settings = Settings(_env_file=None)

    assert settings.task_always_eager is True


def test_relative_paths_are_resolved_against_repo_root():
    settings = Settings(_env_file=None, kb_dir="var/test-kb", work_dir="var/test-work")

    assert settings.kb_dir == (BASE_DIR / "var" / "test-kb").resolve()
    assert settings.work_dir == (BASE_DIR / "var" / "test-work").resolve()
    assert settings.kb_dir.is_absolute()
    assert settings.work_dir.is_absolute()


def test_absolute_paths_are_left_alone(tmp_path: Path):
    target = tmp_path / "kb"
    settings = Settings(_env_file=None, kb_dir=target)

    assert settings.kb_dir == target.resolve()