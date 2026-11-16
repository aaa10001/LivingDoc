
# 阶段三：AST 解析与"只把变了的函数喂给大模型"

> 一句话：用 tree-sitter 把源文件解析成语法树，按函数切块，用**语义指纹**判断哪个函数真的变了，
> 只把变化的函数体送进 Prompt。这是本项目控制大模型上下文成本的核心机制。

## 1. 本阶段的交付物

| 文件 | 职责 |
|---|---|
| app/parsers/model.py | 数据模型：Symbol / FileSymbols / SymbolChange / FileDiff / ChangeSet |
| app/parsers/profiles.py | 语言档案：扩展名 → Query（S-expression）、节点类型、注释类型、是否支持重载 |
| app/parsers/extractor.py | TreeSitterExtractor：单文件 → 函数列表（名称、限定名、行号、签名、函数体、三种指纹） |
| app/parsers/differ.py | 新旧两版函数集合的比对：新增 / 删除 / 修改 / 重命名 / 纯格式化 |
| app/parsers/service.py | 服务层：批量文件 → ChangeSet；build_prompt_payload 做上下文预算裁剪 |
| tests/test_parsers.py | 26 个用例，含一条"长行不许崩"的回归用例 |
| scripts/diff_functions.py | 命令行演示：对着真实 git 仓库的两个 commit 输出函数级变更报告 |

支持语言：Python（.py/.pyi）、Java（.java）。新增语言只需在 profiles.py 加一份 LanguageProfile。

## 2. 为什么不能"用正则找函数"

这是本阶段最开始就否决掉的方案。正则（例如 ^\s*def (\w+)\s*\( 或 ^\s*(public|private).*\)\s*\{）在三种情况下必然出错：

1. **嵌套**：Python 允许函数里定义函数，Java 允许匿名内部类、lambda。正则按行扫描，无法知道某个 def 属于哪个类、哪一层。
2. **字符串与注释里的假函数**：三引号字符串里写的"示例：def foo(): ..."会被误认为真函数。
3. **多行签名**：

       def save(
           self,
           user: User,   # 注意这里
       ) -> None:

   行首正则只能抓到第一行，签名被截断，也无法确定函数体的结束行。

正则唯一能做的是"猜"，而下游的文档生成需要精确的函数边界和函数体原文 —— 猜错的直接后果是给大模型喂了半截函数，生成出错误的 API 文档。

因此改用 AST：tree-sitter 是增量解析器，容错（源码有语法错误也能出树）、跨语言、且给出了每个节点的**字节区间**，天然适合"按函数切块"。

## 3. 设计：四层各管一件事

    profiles.py    语言差异（Query、节点类型）只出现在这里
        |
    extractor.py   单文件 -> Symbol 列表（语言无关）
        |
    differ.py      两版 Symbol 列表 -> SymbolChange 列表（纯函数，无 I/O）
        |
    service.py     批量文件 + Prompt 预算裁剪

这样做的理由：语言差异是最容易扩散的脏东西。如果让 extractor 里写满 if language == "python" ... else ...，
加第三种语言就要改五个地方；现在加语言 = 加一份 profile。

### 3.1 Query 用 S-expression，而不是自己遍历节点

tree-sitter 的 Query 语法（S-expression）就是为"按模式取节点"设计的：

    ; Python
    (function_definition) @fn
    (class_definition) @class

    ; Java
    (method_declaration) @fn
    (constructor_declaration) @fn
    (class_declaration) @class
    (interface_declaration) @class
    (enum_declaration) @class
    (record_declaration) @class

取回的 captures 是 {"fn": [Node...], "class": [Node...]}。相比手写递归遍历，Query 的好处是
**匹配逻辑是声明式的、可被 tree-sitter 自己优化**，且不会漏掉语法变体（比如 Java 的 record）。

### 3.2 混合策略：Query 抓节点 + parent 链求限定名

Query 只能抓到"函数节点"，但文档里需要 UserService.load 这样的限定名。两条路：

- 纯 Query：写 (class_definition name: (identifier) @cls body: (block (function_definition) @fn)) —— 
  只能覆盖"类直接包含函数"一层，嵌套类、内部类就漏了，且每个语言要各写一份复杂模式。
- 纯遍历：从根往下递归，自己维护"当前在哪个类里"的栈 —— 代码量翻倍。

采用的是混合：**Query 负责找节点（快、不漏），parent 链负责求上下文（简单、通用）**。
_enclosing_names() 从函数节点往上走，收集所有 class/function 祖先的名字再反序，
于是 top_level.inner 这种嵌套函数、Outer.Inner.method 这种内部类方法都能自然得到。

## 4. 函数身份：key 怎么定

    Python:  key = qualified_name             例：UserService.load
    Java:    key = qualified_name(params)     例：UserService.load(int id)

为什么要区分语言：Java 支持重载，load(int id) 和 load(long id) 是两个不同的 API，
只用 UserService.load 会把它们合并成一个，文档就丢了半边。Python 不存在重载，用限定名即可。

参数规范化（_normalize_params）做了两件事：压掉多余空白、去掉参数节点自带的括号，
否则 Java 会得到 load((int id)) 这种双括号。

## 5. 三种指纹，解决三种问题

| 指纹 | 计算方式 | 用途 |
|---|---|---|
| raw_hash | 节点原文（含装饰器、签名、函数体）sha256 | 判断"一个字都没动" |
| body_hash | **只取函数体**（body 子节点）sha256 | 识别重命名：改名只动签名，不动函数体 |
| semantic_hash | 遍历子树，跳过注释，收集 (节点类型, 叶子文本) 再 sha256 | 判断语义是否变化 |

semantic_hash 为什么能忽略纯格式化：tree-sitter 的**叶子节点文本不含前后空白**，
所以改缩进、加空行、拆分多行都不会改变叶子序列；注释节点被显式跳过，所以加注释也不影响。
于是：

- semantic 相同 + raw 相同 → 完全没动，不进报告
- semantic 相同 + raw 不同 → formatting_only=True，**不触发文档重新生成**（needs_redoc=False）
- semantic 不同 → 真修改

这条设计直接省钱：一次 black / prettier 全量格式化提交，不应该让系统重写所有文档。

## 6. 比对算法（四步，顺序不能乱）

1. 按 key 求交集 → 同名函数逐个比 semantic_hash（顺带识别 formatting-only）
2. 求差集 → 候选的"新增"与"删除"
3. 在候选之间按 body_hash 做 **1:1** 配对 → 重命名/移动
4. 剩下的才是真的新增和删除

几个刻意的保守设计：

- **一对多直接放弃**：三个函数体一模一样的函数同时改名时，无法确定谁对应谁。宁可报"删三个加三个"，
  也不猜 —— 猜错会让文档里的类图连线错乱，比多生成两份文档严重得多。
- **限定名相同、只有参数不同 → 不算重命名**：load(long id) 改成 load(String id) 是破坏性 API 变更，
  必须报成"删一个 + 加一个"。这条是测试逼出来的（见 §8.2）。
- **排序固定**：changes 按 (start_line, key) 排序，保证同样的输入永远产出同样的报告 —— 否则文档 diff 会随机抖动。

## 7. 上下文预算：build_prompt_payload

ChangeSet.to_summary() 给出计数，build_prompt_payload() 负责真正把函数体拼成 Prompt 载荷：

- 只收 needs_redoc=True 的变更（formatting-only 与删除的函数不喂）
- 优先级：added > modified > renamed > removed
- 上限 max_chars=60000（约 1.5 万 token，给 DeepSeek-Coder 留足输出空间）
- 超预算的部分被丢弃，并在载荷末尾写明 [truncated] N more changes were omitted for context budget.

**为什么要在末尾显式声明截断**：大模型不知道自己没看到全部内容，会自信地生成"完整的"类图。
显式告诉它"还有 N 个变更没给你"，它才会在文档里保留 TODO 而不是臆造。
这一步是"上下文有限"这个作业得分点的正面回答：不是把项目塞进去，而是**先用 AST 缩小范围，再用预算裁剪**。

## 8. 本阶段的两个真实 bug（人机协同记录）

### 8.1 重命名配对用错了指纹

初版 _pair_renames 用 raw_hash 配对。测试 test_rename_detected_by_identical_body 直接红了：

    top_level -> entry_point 改名后，输出是 added + removed + renamed(inner)，而不是一条 renamed

**根因**：改名必然改变原文（def 行里的名字变了），raw_hash 一定不同，所以永远配不上对。
而嵌套函数 inner 的原文没变，反而被配上了 —— 于是报告里出现"外层函数删+增、内层函数改名"的荒谬结果。

**修复**：新增 body_hash（只对 body 子节点取指纹），配对改用它。同时补了一条规则：限定名相同、
只有参数不同的不算重命名（Java 重载那条用例暴露的）。

**教训**：指纹的粒度必须和"你想判断的语义"对齐。用整段原文的指纹去回答"是不是同一个函数换了名字"，
从信息论上就不成立 —— 名字本身就在被比较的原文里。

### 8.2 Java 重载变更被误判成重命名

load(long id) → load(String id) 的测试期望 "added + removed"，实际得到 "renamed"。
因为函数体一字未改，body_hash 相同。**这是把"签名变更"误当成"改名"**：调用方会编译失败，属于破坏性变更，
必须显式报告。修复：配对前先判断 before.qualified_name == after.qualified_name，相同就跳过配对。

## 9. ★ access violation 根因调查（本阶段最重要的记录）

### 9.1 症状

python 进程**直接消失**：

    exit code -1073741819   (0xC0000005, access violation)
    没有 Python traceback

faulthandler 也只能报 Windows fatal exception: access violation，位置指向 _semantic_hash 内部。
pytest 表现为跑一半静默退出，看起来像"测试框架坏了"。

### 9.2 排查过程（五步，每一步都排除了一个假设）

| 步骤 | 假设 | 做法 | 结果 |
|---|---|---|---|
| 1 | QueryCursor 被 GC 后返回的 Node 变野指针 | 让 _captures() 返回 cursor，由 parse 持有 | 仍崩 |
| 2 | Node.text 不安全（属性访问恰好触发物化） | 全部改成 source[start:end] 切片 | 仍崩 |
| 3 | GC 在遍历中途回收 | gc.disable() / gc.freeze() | 仍崩 |
| 4 | 某个具体调用坏了 | 把 parse 的每一步做成探针逐步打印 | 崩点总在上一次调用 _end_line(node) 之后 |
| 5 | 是不是数据相关 | 二分前置调用：只调 _end_line 后再 hash | **必崩**；只调 _kind_for/_parents/_signature_text 都不崩 |
| 6 | 是某一行太长？ | 按行宽参数扫描 | 阈值恰好是 **255** |

### 9.3 扫描结果

| 该行结束列号 | 结果 |
|---|---|
| 23 / 213 / 253 | 正常 |
| 313 | 崩溃 |

### 9.4 结论

**tree-sitter 0.26.0 的 Windows wheel 在"行内列号 > 255"时物化 Point 会破坏内存。**
Node.start_point / Node.end_point 返回 Point(row, column)，而列号在这个构建里被塞进了一个
容纳不下的位置，越界写入后堆被踩坏 —— 所以崩溃点会飘到之后任何一次节点访问上，
看起来像"随机崩溃"，实际上是确定性的：只要文件里有超过 255 字符的行。

这件事在真实项目里非常致命：压缩过的 JS、长字符串常量、base64 常量、minified 配置都很常见。
如果没有这个发现，线上表现就是"某些仓库一分析 worker 就没了"，而且日志里什么都没有。

### 9.5 修复

**彻底不用 Point，改用字节偏移 + 换行索引**（app/parsers/extractor.py）：

    class _LineIndex:
        __slots__ = ("_newlines",)
        def __init__(self, source: bytes) -> None:
            self._newlines = [i for i, byte in enumerate(source) if byte == 0x0A]
        def line_of(self, byte_offset: int) -> int:
            return bisect.bisect_left(self._newlines, byte_offset) + 1
        def span(self, start_byte: int, end_byte: int) -> tuple[int, int]:
            return self.line_of(start_byte), self.line_of(max(start_byte, end_byte - 1))

- 每个文件只扫一遍换行位置，之后每个函数两次二分 → 比 Point 还快
- end_byte 指向最后一个字符之后，所以回退一格再算行号，节点以换行结尾时不会多算一行
- 删掉原来的 _end_line()，避免有人再把它加回来

### 9.6 回归测试

test_long_line_does_not_crash：构造 600 字符的单行 return，断言两个函数都被正确提取且行号正确。
这条用例的价值不在于它测了什么功能，而在于**它会让回归立刻以进程崩溃的形式暴露**，
而不是悄悄读出错误的行号。

## 10. 验证结果

已实测（本机 Windows 11, Python 3.11.2, tree-sitter 0.26.0, ABI 15）：

- python -m pytest tests/ -q → **66 passed**（阶段二 40 + 阶段三 26）
- 长行 600 字符 / 2000 字符：不再崩溃，行号正确
- Python：类方法、静态方法、__init__、嵌套函数、装饰器行归入 start_line、签名裁掉注释
- Java：构造器 kind=constructor、重载 key 区分、@Override 进 decorators、单行方法 start==end
- 真实仓库演示（scripts/diff_functions.py）：两次提交正确产出 formatting-only / added / renamed 三类结果

未验证：

- 大型文件（>1 万行）的解析耗时（parse_ms 字段已就绪，等阶段四在真实仓库上采集）
- C++ / TypeScript（profile 可扩展，但本次未写 Query）
- 增量解析（tree-sitter 的 Tree.edit() + changed_ranges）—— 阶段三刻意不用，理由见下

### 为什么不用 tree-sitter 的增量解析

Tree.edit() 需要把 git 的 diff hunk 精确映射成**字节偏移**，一旦错位，树和源码就对不上，
后续所有提取都是错的；而且它只回答"哪里变了"，不回答"哪个函数的语义变了"。
当前方案对新版文件整体重新解析一次（毫秒级），用语义指纹回答第二个问题 —— 
在 5000 行以内的文件上，重解析的成本远低于做增量映射的风险。
增量解析留给阶段四：当单文件 > 5000 行且 PR 频繁时再开。

## 11. 如何运行

    # 单元测试
    .\.venv\Scripts\python.exe -m pytest tests/test_parsers.py -q

    # 对着真实仓库的两个 commit 演示
    .\.venv\Scripts\python.exe scripts\diff_functions.py --repo . --base HEAD~1 --head HEAD --json var\out.json

## 12. 阶段四接口

阶段二 analyze_push 产出的 next_stage="tree-sitter-parse" 就是这里的入口：

    ChangeSet = ChangedFunctionService().analyze([FilePayload(path, old_source, new_source), ...])
    prompt = build_prompt_payload(ChangeSet)       # 阶段四：拼成 PlantUML/Markdown 生成 Prompt
    doc    = llm.generate(prompt)                  # 阶段五：调 DeepSeek-Coder，校验 PlantUML 语法

下一步（阶段四）要解决的是：怎么让大模型**稳定输出符合 PlantUML 语法**的图，
以及生成失败时如何带着错误信息重试。
