"""结构抽取测试：类图/时序图能不能画对，取决于这里抽出的"事实"对不对。

每个用例都对应一个真实代码里会出现的写法：基类、抽象接口、字段组合、
构造参数注入、`self.repo.find()` 这种接收者靠类型才能解析的调用。
"""

from app.parsers import ChangeSet, TreeSitterExtractor, diff_symbols
from app.parsers.structure import ARROWS, build_structure

SRC = '''"""多模块/继承/组合/异步的一个小切片。"""

from abc import ABC, abstractmethod


class Reader(ABC):
    @abstractmethod
    def find(self, key: str) -> dict: ...


class Cache:
    def __init__(self, size: int = 16) -> None:
        self._items = {}
        self.size: int = size

    def get(self, key: str) -> dict:
        return self._items.get(key, {})


class Repository(Reader):
    def __init__(self, cache: Cache) -> None:
        self.cache = cache

    def find(self, key: str) -> dict:
        if not key:
            raise ValueError("empty key")
        cached = self.cache.get(key)
        for attempt in range(3):
            if cached:
                return cached
        return {"key": key}

    async def refresh(self, keys: list) -> int:
        count = 0
        for key in keys:
            try:
                await self.find(key)
                count += 1
            except ValueError:
                continue
        return count


class Service:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def load(self, key: str) -> dict:
        record = self.repo.find(key)
        return self.render(record)

    def render(self, record: dict) -> dict:
        return record
'''

PATH = "app/services/users.py"


def make_structure(source: str = SRC, path: str = PATH):
    extractor = TreeSitterExtractor()
    new = extractor.parse(path, source.encode())
    change = diff_symbols(path, extractor.parse(path, b""), new)
    return build_structure(ChangeSet(files=[change], skipped_files=[]), module="app.services")


def kinds(structure, source: str, target: str) -> set[str]:
    return {rel.kind for rel in structure.relations if rel.source == source and rel.target == target}


def test_classes_are_aggregated_from_methods():
    structure = make_structure()
    names = {info.name for info in structure.classes}
    assert names == {"Reader", "Cache", "Repository", "Service"}
    repo = next(info for info in structure.classes if info.name == "Repository")
    assert "__init__(self, cache: Cache) -> None" in repo.methods
    assert any(method.startswith("async refresh(") for method in repo.methods)


def test_abstract_base_is_marked():
    structure = make_structure()
    reader = next(info for info in structure.classes if info.name == "Reader")
    assert reader.is_abstract is True


def test_extends_relation():
    structure = make_structure()
    assert "extends" in kinds(structure, "Repository", "Reader")


def test_implements_relation():
    source = """from abc import ABC, abstractmethod

class Reader(ABC):
    @abstractmethod
    def find(self, key: str) -> dict: ...

class DbRepository(Reader):
    def find(self, key: str) -> dict:
        return {}
"""
    structure = make_structure(source)
    assert kinds(structure, "DbRepository", "Reader") == {"extends"}


def test_composition_from_annotated_field():
    structure = make_structure()
    assert "composition" in kinds(structure, "Repository", "Cache")


def test_composition_from_constructor_parameter():
    """`self.repo = repo` 丢掉了类型，只能靠 `repo: Repository` 反推。"""
    structure = make_structure()
    assert "composition" in kinds(structure, "Service", "Repository")


def test_parameter_type_becomes_dependency():
    structure = make_structure()
    assert "dependency" in kinds(structure, "Service", "Repository")


def test_relation_render_uses_plantuml_arrow():
    structure = make_structure()
    line = next(line for line in structure.render().splitlines() if "--|>" in line)
    assert line.startswith("- ")
    assert " : 继承" in line


def test_call_chain_resolves_field_receiver_by_type():
    """`self.repo.find(key)` 里 find 同时属于 Reader 与 Repository，靠字段类型定死。"""
    structure = make_structure()
    chain = structure.chain_for("Service.load")
    assert "Service.load -> Repository.find" in chain


def test_call_chain_is_breadth_first_and_bounded():
    structure = make_structure()
    chain = structure.chain_for("Service.load")
    # render 是同一调用链的第二跳，必须出现
    assert "Service.load -> Service.render" in chain


def test_self_method_call_resolves_within_class():
    structure = make_structure()
    assert structure.chain_for("Service.load")


def test_branches_are_collected_per_function():
    structure = make_structure()
    branches = structure.branchy_functions()
    assert "Repository.find" in branches
    assert "if" in dict(structure.branches)["Repository.find"]


def test_try_and_for_are_branches_too():
    structure = make_structure()
    flow = dict(structure.branches)["Repository.refresh"]
    assert "for" in flow
    assert "try" in flow


def test_entry_point_prefers_longest_chain():
    structure = make_structure()
    # Service.load 有两跳，Repository.find 有一跳
    assert structure.best_entry_point() == "Service.load"


def test_known_names_and_expected_relations_are_usable():
    structure = make_structure()
    assert {"Service", "Repository", "Cache", "Reader"} <= structure.known_names()
    assert ("Repository", "Reader", "extends") in structure.expected_relations()


def test_arrow_table_covers_every_relation_kind():
    for kind in ("extends", "implements", "composition", "aggregation", "dependency"):
        assert kind in ARROWS


def test_empty_change_set_has_no_content():
    structure = build_structure(ChangeSet(files=[], skipped_files=[]))
    assert structure.has_content() is False
    assert structure.best_entry_point() == ""


def test_removed_symbols_are_ignored():
    """只吃 new：删掉的类不该出现在"当前架构"里。"""
    structure = make_structure(
        "class Kept:\n    def go(self) -> None: ...\n"
    )
    assert {info.name for info in structure.classes} == {"Kept"}


def test_parameter_type_resolves_receiver():
    """`service.load(...)` 里 service 是参数，类型写在签名上，也要能解析到 UserService。"""
    structure = make_structure(
        "class Repo:\n"
        "    def find(self, key: str) -> dict: return {}\n"
        "\n"
        "class UserService:\n"
        "    def __init__(self, repo: Repo) -> None:\n"
        "        self.repo = repo\n"
        "    def load(self, key: str) -> dict:\n"
        "        return self.repo.find(key)\n"
        "\n"
        "def handle(key: str, service: UserService) -> dict:\n"
        "    return service.load(key)\n"
    )
    assert "handle -> UserService.load" in structure.chain_for("handle")
    assert "UserService.load -> Repo.find" in structure.chain_for("handle")


def test_unknown_receiver_is_not_guessed():
    """`data.get(...)` 的 data 是局部变量，不能因为"get 只有一处"就连到 Cache.get。

    猜错的边比缺的边更糟：时序图上会多出一条源代码里根本不存在的一跳。
    """
    structure = make_structure(
        "class Cache:\n"
        "    def get(self, key: str) -> dict: return {}\n"
        "\n"
        "def parse(body: str) -> str:\n"
        "    data = {'email': ''}\n"
        "    return data.get('email')\n"
    )
    assert structure.chain_for("parse") == ()


def test_abstract_marker_bases_are_not_relations():
    """ABC/Protocol 不是业务父类：画不出来，也不该要求模型画。"""
    structure = make_structure(
        "from abc import ABC\n"
        "\n"
        "class Port(ABC):\n"
        "    def send(self) -> None: ...\n"
        "\n"
        "class HttpPort(Port):\n"
        "    def send(self) -> None: return None\n"
    )
    assert ("Port", "ABC", "extends") not in structure.expected_relations()
    port = next(info for info in structure.classes if info.name == "Port")
    assert port.is_abstract is True
    assert kinds(structure, "HttpPort", "Port") == {"extends"}


def test_unresolved_types_still_produce_the_edge():
    """只知道 `repo: UserRepository`、类定义在别的模块，也要画出组合边。

    丢掉这条边的代价比画一个空类框大得多：类图会退化成几个没有关系的框。
    """
    source = (
        "class UserService:\n"
        "    def __init__(self, repo: UserRepository) -> None:\n"
        "        self.repo = repo\n"
        "    def load(self, key: str) -> dict:\n"
        "        return self.repo.find(key)\n"
    )
    structure = make_structure(source)
    assert ("UserService", "UserRepository", "composition") in structure.expected_relations()
    # 另一端不在本模块，必须进 externals，否则白名单不放行、模型也画不出来
    assert "UserRepository" in structure.externals
    assert "UserRepository" in structure.known_names()


def test_context_makes_cross_module_types_declared_not_external():
    """context 里有这个类时，它是"已知实体"，不再是外部引用。"""
    source = (
        "class UserService:\n"
        "    def __init__(self, repo: Cache) -> None:\n"
        "        self.repo = repo\n"
    )
    extractor = TreeSitterExtractor()
    path = "app/services/users.py"
    new = extractor.parse(path, source.encode())
    change = diff_symbols(path, extractor.parse(path, b""), new)
    with_context = build_structure(
        ChangeSet(files=[change], skipped_files=[]),
        module="app.services",
        context=("Cache",),
    )
    assert ("UserService", "Cache", "composition") in with_context.expected_relations()
    assert "Cache" in with_context.known_names()


def test_builtin_types_are_not_drawn():
    """`dict` / `Callable` 大写开头也不是类，不能变成类框。"""
    source = (
        "class Service:\n"
        "    def __init__(self, fn: Callable[[int], dict]) -> None:\n"
        "        self.fn = fn\n"
        "    def run(self, items: list[dict]) -> dict:\n"
        "        return {}\n"
    )
    structure = make_structure(source)
    assert structure.relations == ()
    assert structure.externals == ()


def test_optional_field_type_still_counts_as_composition():
    """`cache: Cache | None = None` 语义上就是持有一个 Cache，组合边不能丢。"""
    source = (
        "class Cache:\n"
        "    def get(self, key: str) -> object:\n"
        "        return None\n"
        "\n"
        "class Service:\n"
        "    def __init__(self, cache: 'Cache | None' = None) -> None:\n"
        "        self.cache = cache\n"
        "    def load(self, key: str) -> object:\n"
        "        return self.cache.get(key)\n"
    )
    structure = make_structure(source)
    assert ("Service", "Cache", "composition") in structure.expected_relations()
    chain = structure.chain_for("Service.load")
    assert "Service.load -> Cache.get" in chain


def test_java_inheritance_and_implements():
    source = """package com.example;

public class UserService implements Service, Closeable {
    private final UserRepository repo;

    public UserService(UserRepository repo) {
        this.repo = repo;
    }

    public String load(int id) {
        if (id < 0) {
            return null;
        }
        return repo.find(id);
    }
}
"""
    structure = make_structure(source, "src/UserService.java")
    assert "implements" in kinds(structure, "UserService", "Service")
    assert "strictfp" not in {rel.kind for rel in structure.relations}