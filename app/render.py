"""评审界面的"渲染"能力：静态校验 + 可选的 PlantUML SVG 编译。

为什么不是简单的 `plantuml.jar -tsvg` 包装：
- 本机（Windows 开发环境）经常没有 JRE / jar，评审界面仍然要能打开、能看校验结果；
- 静态校验器（E001–E012 / W001–W004）是发布闸门的一部分，界面必须展示同一套诊断，
  否则会出现"界面说没问题、流水线却拒收"的分裂。

所以这里分成两层，`renderer` 字段告诉前端实际用了哪一层：
  static  —— 只做了结构校验，没有图
  plantuml —— 真的编译出了 SVG
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Any

from app.ai.validator import MarkdownValidator, validate_plantuml
from app.config import Settings

logger = logging.getLogger("livingdoc.render")

_PLANTUML_FORMATS = {"plantuml", "puml"}


def diagnostic_payload(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "code": d.code,
            "message": d.message,
            "severity": d.severity,
            "line": d.line,
            "hint": d.hint,
            "snippet": d.snippet,
        }
        for d in result.diagnostics
    ]


def kind_for(doc_type: str) -> str:
    if doc_type == "sequence_diagram":
        return "sequence"
    if doc_type == "api_doc":
        return "api"
    return "class"


def validate_document(
    content: str,
    content_format: str,
    doc_type: str = "class_diagram",
    *,
    required_members: set[str] | None = None,
) -> Any:
    if content_format == "markdown":
        return MarkdownValidator(required_members=required_members).validate(content, kind="api")
    if content_format in _PLANTUML_FORMATS:
        return validate_plantuml(content, kind=kind_for(doc_type), required_members=required_members)
    # mermaid / 纯文本：只查空内容，别假装能编译
    return MarkdownValidator().validate(content, kind="api")


def compile_svg(content: str, settings: Settings) -> tuple[bytes | None, str | None]:
    """调 plantuml.jar 生成 SVG。任何环境问题都返回 (None, reason)，不抛异常。"""
    jar = Path(settings.plantuml_jar)
    if not jar.exists():
        return None, "plantuml.jar 不存在；已降级为静态校验（容器内会编译 SVG）"
    cmd = [
        settings.java_bin,
        "-Djava.awt.headless=true",
        "-jar",
        str(jar),
        "-tsvg",
        "-pipe",
        "-failfast2",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=content.encode("utf-8"),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return None, f"找不到 Java 可执行文件 {settings.java_bin!r}"
    except subprocess.TimeoutExpired:
        return None, "PlantUML 编译超时（60s）"
    except OSError as exc:  # pragma: no cover - 平台差异
        return None, f"无法启动 PlantUML: {exc}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        return None, f"PlantUML 编译失败（exit {proc.returncode}）: {detail[:500]}"
    svg = proc.stdout
    if b"<svg" not in svg[:2000]:
        return None, "PlantUML 没有返回 SVG"
    return svg, None


def render_svg(content: str, settings: Settings) -> tuple[str | None, str | None]:
    """编译并缓存 SVG，返回 (文件名, 错误原因)。文件名即 URL 的最后一段。"""
    settings.renders_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
    target = settings.renders_dir / f"{digest}.svg"
    if target.exists():
        return target.name, None
    svg, error = compile_svg(content, settings)
    if svg is None:
        return None, error
    tmp = target.with_suffix(".svg.tmp")
    tmp.write_bytes(svg)
    tmp.replace(target)
    return target.name, None