"""阶段三单测：tree-sitter 提取 + 新旧版本比对。

这些用例存在的意义：tree-sitter 的 Query 语法、节点类型、行号口径在不同语言包
之间并不一致，靠"看起来对"是不行的，必须用真实源码钉死行为。
"""

from __future__ import annotations

import textwrap

import pytest

from app.parsers import (
    ChangeKind,
    ChangedFunctionService,
    FilePayload,
    TreeSitterExtractor,
    UnsupportedLanguage,
    build_prompt_payload,
    diff_symbols,
    profile_for_path,
)


@pytest.fixture(scope="module")
def extractor() -> TreeSitterExtractor:
    return TreeSitterExtractor()


PY_SOURCE = textwrap.dedent(
    '''
    import os


    class UserService:
        """users"""

        @staticmethod
        def helper(x):
            return x + 1

        def __init__(self, repo):
            self.repo = repo

        def load(self, user_id: int) -> dict:
            # lookup cache first
            return {"id": user_id}


    def top_level(a, b=2):
        def inner():
            return a

        return inner()
    '''
).lstrip("\n")


def _parse(extractor: TreeSitterExtractor, path: str, text: str):
    return extractor.parse(path, text.encode("utf-8"))


# ---------------------------------------------------------------- 提取


def test_python_extracts_all_functions(extractor):
    fs = _parse(extractor, "app/service.py", PY_SOURCE)
    names = [s.qualified_name for s in fs.symbols]
    assert names == [
        "UserService.helper",
        "UserService.__init__",
        "UserService.load",
        "top_level",
        "top_level.inner",
    ]
    assert fs.language == "python"
    assert fs.has_error is False


def test_python_start_end_lines_are_inclusive(extractor):
    fs = _parse(extractor, "a.py", PY_SOURCE)
    load = fs.by_key()["UserService.load"]
    lines = PY_SOURCE.splitlines()
    expected_start = next(i for i, line in enumerate(lines, 1) if "def load" in line)
    assert load.start_line == expected_start
    # 行号是 1-based 闭区间：start/end 都必须是真实存在的行
    assert lines[load.start_line - 1].lstrip().startswith("def load")
    assert lines[load.end_line - 1].strip() == 'return {"id": user_id}'
    assert load.end_line - load.start_line + 1 == 3


def test_python_decorator_included_in_span_and_signature(extractor):
    fs = _parse(extractor, "a.py", PY_SOURCE)
    helper = fs.by_key()["UserService.helper"]
    assert helper.decorators == ("@staticmethod",)
    # 装饰器行也算函数起点，否则 diff 里改装饰器就定位不到函数
    assert helper.signature.startswith("@staticmethod")
    assert "def helper(x):" in helper.signature
    assert helper.signature.rstrip().endswith(":")


def test_python_signature_excludes_leading_comment_of_body(extractor):
    fs = _parse(extractor, "a.py", PY_SOURCE)
    load = fs.by_key()["UserService.load"]
    assert "lookup cache" not in load.signature
    assert "lookup cache" in load.body


def test_python_kinds(extractor):
    fs = _parse(extractor, "a.py", PY_SOURCE)
    assert fs.by_key()["UserService.__init__"].kind.value == "constructor"
    assert fs.by_key()["UserService.load"].kind.value == "method"
    assert fs.by_key()["top_level"].kind.value == "function"


def test_python_params_normalized(extractor):
    fs = _parse(extractor, "a.py", PY_SOURCE)
    assert fs.by_key()["UserService.load"].params == "self, user_id: int"
    assert fs.by_key()["top_level"].params == "a, b=2"


def test_python_broken_source_still_yields_symbols(extractor):
    broken = "def ok():\n    return 1\n\ndef bad(:\n    pass\n"
    fs = _parse(extractor, "a.py", broken)
    assert fs.has_error is True
    assert "ok" in [s.name for s in fs.symbols]


def test_unsupported_extension_raises(extractor):
    with pytest.raises(UnsupportedLanguage):
        extractor.parse("README.md", b"# hi\n")
    assert extractor.supports("x.py") is True
    assert extractor.supports("x.java") is True
    assert extractor.supports("x.go") is False


def test_profile_lookup():
    assert profile_for_path("a/b/c.py").name == "python"
    assert profile_for_path("A.JAVA").name == "java"
    assert profile_for_path("a.ts") is None


# ---------------------------------------------------------------- 语义指纹


def test_whitespace_and_comment_changes_are_formatting_only(extractor):
    reformatted = PY_SOURCE.replace(
        '        return {"id": user_id}',
        '            # 换行 + 多一层缩进 + 新注释\n            return {"id": user_id}',
    )
    assert reformatted != PY_SOURCE, "替换没命中，用例会变成空测"
    old = _parse(extractor, "a.py", PY_SOURCE)
    new = _parse(extractor, "a.py", reformatted)
    diff = diff_symbols("a.py", old, new)
    load = [c for c in diff.changes if c.key == "UserService.load"]
    assert len(load) == 1
    assert load[0].kind is ChangeKind.MODIFIED
    assert load[0].formatting_only is True
    assert load[0].needs_redoc is False


def test_real_code_change_is_not_formatting_only(extractor):
    changed = PY_SOURCE.replace('return {"id": user_id}', 'return {"uid": user_id, "v": 2}')
    old = _parse(extractor, "a.py", PY_SOURCE)
    new = _parse(extractor, "a.py", changed)
    diff = diff_symbols("a.py", old, new)
    change = [c for c in diff.changes if c.key == "UserService.load"][0]
    assert change.formatting_only is False
    assert change.needs_redoc is True


def test_long_line_does_not_crash(extractor):
    """回归用例：tree-sitter 0.26.0 的 Windows wheel 在行内列号 > 255 时会 access
    violation（进程直接退出，退出码 -1073741819，Python 层没有 traceback）。

    修复方式是不碰 Node.start_point / Node.end_point，改用字节偏移 + _LineIndex。
    这条用例保证以后没人把 Point 加回来：只要有 JRE 之外的编译产物回归，它就会
    让 pytest 直接崩掉，而不是悄悄读错行号。
    """
    body = "x" * 600  # 结束列约 615，远超 255
    source = f'def f():\n    return "{body}"\n\n\ndef g():\n    return 2\n'
    parsed = extractor.parse("long.py", source.encode("utf-8"))
    first, second = parsed.symbols
    assert (first.name, first.start_line, first.end_line) == ("f", 1, 2)
    assert (second.name, second.start_line, second.end_line) == ("g", 5, 6)
    assert first.raw_hash != second.raw_hash


def test_identical_versions_produce_no_changes(extractor):
    old = _parse(extractor, "a.py", PY_SOURCE)
    new = _parse(extractor, "a.py", PY_SOURCE)
    assert diff_symbols("a.py", old, new).changes == ()


# ---------------------------------------------------------------- 变更分类


def test_added_file_marks_every_symbol_added(extractor):
    new = _parse(extractor, "a.py", PY_SOURCE)
    diff = diff_symbols("a.py", None, new)
    assert {c.kind for c in diff.changes} == {ChangeKind.ADDED}
    assert len(diff.changes) == len(new.symbols)
    assert all(c.old is None for c in diff.changes)


def test_deleted_file_marks_every_symbol_removed(extractor):
    old = _parse(extractor, "a.py", PY_SOURCE)
    diff = diff_symbols("a.py", old, None)
    assert {c.kind for c in diff.changes} == {ChangeKind.REMOVED}
    assert all(c.new is None for c in diff.changes)


def test_rename_detected_by_identical_body(extractor):
    renamed = PY_SOURCE.replace("def top_level(a, b=2):", "def entry_point(a, b=2):")
    old = _parse(extractor, "a.py", PY_SOURCE)
    new = _parse(extractor, "a.py", renamed)
    changes = diff_symbols("a.py", old, new).changes
    assert {c.kind for c in changes} == {ChangeKind.RENAMED}, [c.kind for c in changes]
    # 一次改名会连带产生两条：顶层函数本身，以及它里面 qualified_name 变了的嵌套函数。
    # 所以这里按集合断言——如果只取 changes[0]，用例的成败就取决于排序，是假绿。
    renamed = {c.old.qualified_name: c.new.qualified_name for c in changes}
    assert renamed == {
        "top_level": "entry_point",
        "top_level.inner": "entry_point.inner",
    }


def test_ambiguous_rename_not_guessed(extractor):
    # 两个函数体完全相同，改名后无法确定谁是谁 -> 不配对，报成删+增
    base = "def a():\n    return 1\n\n\ndef b():\n    return 1\n"
    after = "def c():\n    return 1\n\n\ndef d():\n    return 1\n"
    diff = diff_symbols("x.py", _parse(extractor, "x.py", base), _parse(extractor, "x.py", after))
    kinds = {c.kind for c in diff.changes}
    assert ChangeKind.RENAMED not in kinds
    assert kinds == {ChangeKind.ADDED, ChangeKind.REMOVED}


def test_new_method_in_existing_class_is_added(extractor):
    after = PY_SOURCE.replace(
        "    def load(self, user_id: int) -> dict:",
        "    def delete(self, user_id: int) -> None:\n        pass\n\n    def load(self, user_id: int) -> dict:",
    )
    diff = diff_symbols("a.py", _parse(extractor, "a.py", PY_SOURCE), _parse(extractor, "a.py", after))
    added = [c for c in diff.changes if c.kind is ChangeKind.ADDED]
    assert [c.name for c in added] == ["UserService.delete"]


# ---------------------------------------------------------------- Java


JAVA_SOURCE = textwrap.dedent(
    """
    package com.example;

    import java.util.List;

    // user service
    public class UserService implements Service {
        private final Repo repo;

        public UserService(Repo repo) {
            this.repo = repo;
        }

        @Override
        public List<User> load(int id) throws IOException {
            return repo.find(id);
        }

        public List<User> load(long id) {
            return repo.find((int) id);
        }

        static int add(int a, int b) { return a + b; }
    }
    """
).lstrip("\n")


def test_java_extracts_methods_and_constructor(extractor):
    fs = _parse(extractor, "src/main/java/UserService.java", JAVA_SOURCE)
    by_name = {}
    for s in fs.symbols:
        by_name.setdefault(s.name, []).append(s)
    assert len(by_name["UserService"]) == 1
    assert by_name["UserService"][0].kind.value == "constructor"
    assert {s.key for s in by_name["load"]} == {
        "UserService.load(int id)",
        "UserService.load(long id)",
    }
    add = fs.by_key()["UserService.add(int a, int b)"]
    # 单行方法：start 与 end 必须是同一行
    assert add.start_line == add.end_line


def test_java_overloads_have_distinct_keys(extractor):
    fs = _parse(extractor, "U.java", JAVA_SOURCE)
    loads = [s for s in fs.symbols if s.name == "load"]
    assert len(loads) == 2
    assert loads[0].key != loads[1].key


def test_java_annotation_captured(extractor):
    fs = _parse(extractor, "U.java", JAVA_SOURCE)
    assert fs.by_key()["UserService.load(int id)"].decorators == ("@Override",)


def test_java_overload_signature_change_is_add_remove(extractor):
    after = JAVA_SOURCE.replace("load(long id)", "load(String id)")
    diff = diff_symbols(
        "U.java", _parse(extractor, "U.java", JAVA_SOURCE), _parse(extractor, "U.java", after)
    )
    kinds = sorted(c.kind.value for c in diff.changes)
    assert kinds == ["added", "removed"]


# ---------------------------------------------------------------- 服务层


def test_service_skips_unsupported_files():
    change_set = ChangedFunctionService().analyze(
        [
            FilePayload("a.py", None, b"def f():\n    pass\n"),
            FilePayload("README.md", None, b"# hi\n"),
            FilePayload("b.go", None, b"package main\n"),
        ]
    )
    assert [f.path for f in change_set.files] == ["a.py"]
    assert change_set.skipped_files == [
        ("README.md", "unsupported_language"),
        ("b.go", "unsupported_language"),
    ]


def test_service_summary_counts():
    old = b"def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    new = b"def a():\n    return 1\n\n\ndef c():\n    return 3\n"
    change_set = ChangedFunctionService().analyze([FilePayload("x.py", old, new)])
    summary = change_set.to_summary()
    assert summary["files_analyzed"] == 1
    assert summary["symbols_added"] >= 1
    assert summary["symbols_removed"] >= 1
    assert summary["files_skipped"] == 0


def test_build_prompt_payload_respects_budget():
    old = b"def a():\n    return 1\n"
    new = "def a():\n    return 1\n\ndef big():\n    return " + repr("x" * 500) + "\n"
    change_set = ChangedFunctionService().analyze(
        [FilePayload("x.py", old, new.encode())]
    )
    payload = build_prompt_payload(change_set, max_chars=120)
    assert "truncated" in payload
    assert len(payload) < 400


def test_payload_requires_at_least_one_side():
    with pytest.raises(ValueError):
        FilePayload("x.py", None, None)
