#!/bin/sh
# 容器内自检：把"镜像能不能用"拆成几条可判定的事实，每条都打印结论与退出码。
# 用法：docker build --target test -t livingdoc:test . && docker run --rm livingdoc:test sh scripts/container_smoke.sh
set -u

fail=0
say() { printf '\n== %s ==\n' "$1"; }

say "python"
python -VV

say "pytest（SQLite 路径，应为 215 passed）"
python -m pytest tests -q 2>&1 | tail -2
# 注意：这里用 PIPESTATUS 拿不到 sh 的语义，所以直接重跑一次判定退出码
if python -m pytest tests -q >/tmp/pytest.log 2>&1; then
  echo "pytest OK: $(tail -1 /tmp/pytest.log)"
else
  echo "pytest FAILED:"; tail -25 /tmp/pytest.log; fail=1
fi

say "tree-sitter 原生扩展"
python - <<'PY'
import tree_sitter, tree_sitter_python, tree_sitter_java
from app.parsers import TreeSitterExtractor
print("tree_sitter", getattr(tree_sitter, "__version__", "?"))
src = b"class A:\n    def m(self, x):\n        return x\n"
parsed = TreeSitterExtractor().parse("a.py", src)
print("parsed symbols:", [s.qualified_name for s in parsed.symbols])
PY
[ $? -eq 0 ] || fail=1

say "JRE + PlantUML 版本（ADR-007 的前提）"
java -version 2>&1 | head -1
java -jar "$PLANTUML_JAR" -version 2>&1 | head -2

say "plantuml -checkonly：合法图（多行成员声明）"
cat > /tmp/good.puml <<'UML'
@startuml
class Order {
  +id: String
  +total(): double
}
class Item
Order --> Item
@enduml
UML
java -jar "$PLANTUML_JAR" -checkonly -failfast2 /tmp/good.puml
rc=$?
echo "GOOD rc=$rc"
[ "$rc" -eq 0 ] || fail=1

say "plantuml -checkonly：坏图（花括号未闭合）"
printf '@startuml\nclass Broken {\n@enduml\n' > /tmp/bad.puml
java -jar "$PLANTUML_JAR" -checkonly -failfast2 /tmp/bad.puml 2>&1 | tail -2
rc=$?
echo "BAD rc=$rc（PlantUML 的 rc 不总是可靠，故同时看输出里是否含 errors）"
if java -jar "$PLANTUML_JAR" -checkonly -failfast2 /tmp/bad.puml > /tmp/bad.out 2>&1; then
  if grep -qi "error" /tmp/bad.out; then echo "BAD detected via output"; else echo "BAD not detected (unexpected)"; fail=1; fi
else
  echo "BAD rejected via exit code"
fi

say "结论"
if [ "$fail" -eq 0 ]; then echo "SMOKE PASSED"; else echo "SMOKE FAILED"; fi
exit "$fail"
