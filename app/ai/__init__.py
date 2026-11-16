"""AI 生成层：提示词、客户端、校验器、自我纠错、文档编排。

导入顺序无关，但依赖方向是单向的：
    prompts  -> 无依赖（只拼字符串）
    validator -> 无依赖（纯 Python 结构检查）
    client   -> httpx
    self_correct -> client + prompts + validator
    service  -> self_correct + parsers
"""

from .client import (
    Completion,
    DeepSeekClient,
    LLMClient,
    LLMError,
    RetryPolicy,
    ScriptedLLM,
)
from .prompts import (
    API_SYSTEM_PROMPT,
    CLASS_SYSTEM_PROMPT,
    SEQUENCE_SYSTEM_PROMPT,
    build_api_prompt,
    build_class_prompt,
    build_repair_prompt,
    build_sequence_prompt,
    changed_members,
    entity_names,
)
from .self_correct import (
    Attempt,
    GenerationResult,
    SelfCorrectingGenerator,
    generate_with_repair,
)
from .service import (
    DocType,
    DocumentationService,
    DocumentDraft,
    ModuleDocumentation,
    group_by_module,
    module_key_for,
)
from .validator import (
    Diagnostic,
    Extraction,
    MarkdownValidator,
    PlantUMLValidator,
    ValidationResult,
    extract_markdown,
    extract_plantuml,
    validate_markdown,
    validate_plantuml,
)

__all__ = [
    "API_SYSTEM_PROMPT",
    "Attempt",
    "CLASS_SYSTEM_PROMPT",
    "Completion",
    "DeepSeekClient",
    "Diagnostic",
    "DocType",
    "DocumentDraft",
    "DocumentationService",
    "Extraction",
    "GenerationResult",
    "LLMClient",
    "LLMError",
    "MarkdownValidator",
    "ModuleDocumentation",
    "PlantUMLValidator",
    "RetryPolicy",
    "SEQUENCE_SYSTEM_PROMPT",
    "ScriptedLLM",
    "SelfCorrectingGenerator",
    "ValidationResult",
    "build_api_prompt",
    "build_class_prompt",
    "build_repair_prompt",
    "build_sequence_prompt",
    "changed_members",
    "entity_names",
    "extract_markdown",
    "extract_plantuml",
    "generate_with_repair",
    "group_by_module",
    "module_key_for",
    "validate_markdown",
    "validate_plantuml",
]
