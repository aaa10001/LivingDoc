"""AST 解析与函数级变更提取（阶段三）。

对外只暴露这几样东西，其余是实现细节：

    from app.parsers import (
        TreeSitterExtractor,   # 单文件 -> FileSymbols
        ChangedFunctionService, # 一批文件新旧版本 -> ChangeSet
        FilePayload,            # 输入载体
        diff_symbols,           # 只关心两个 FileSymbols 的差异时
    )
"""

from .differ import diff_symbols
from .extractor import TreeSitterExtractor, UnsupportedLanguage
from .model import (
    ChangeKind,
    ChangeSet,
    FileDiff,
    FileSymbols,
    Symbol,
    SymbolChange,
    SymbolKind,
)
from .profiles import JAVA_PROFILE, PYTHON_PROFILE, SUPPORTED_EXTENSIONS, profile_for_path
from .service import ChangedFunctionService, FilePayload, build_prompt_payload
from .structure import (
    ARROWS,
    ClassInfo,
    FunctionInfo,
    ModuleStructure,
    Relation,
    build_structure,
)

__all__ = [
    "ARROWS",
    "ChangeKind",
    "ChangeSet",
    "ChangedFunctionService",
    "ClassInfo",
    "FileDiff",
    "FilePayload",
    "FileSymbols",
    "FunctionInfo",
    "JAVA_PROFILE",
    "ModuleStructure",
    "PYTHON_PROFILE",
    "Relation",
    "SUPPORTED_EXTENSIONS",
    "Symbol",
    "SymbolChange",
    "SymbolKind",
    "TreeSitterExtractor",
    "UnsupportedLanguage",
    "build_prompt_payload",
    "build_structure",
    "diff_symbols",
    "profile_for_path",
]
