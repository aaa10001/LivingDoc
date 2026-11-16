# 阶段四：让大模型吐出能渲染的图

> 输入：阶段三的 ChangeSet（只含发生变更的函数代码块）
> 输出：模块级 PlantUML 类图、时序图、Markdown API 文档
> 结论先行：**提示词决定"大概率对"，校验器决定"错了能被发现"，自我纠错决定"发现后能自愈"。三者缺一不可，而且顺序不能颠倒。**

## 1. 交付清单

| 文件 | 职责 | 关键设计 |
|---|---|---|
| app/ai/prompts.py | 三代提示词、输出契约、few-shot、修复提示词 | 契约写成"首字符必须是 @"这种可被程序检查的形式 |
| app/ai/validator.py | PlantUML 与 Markdown 的结构校验 | 纯 Python，零外网依赖，无 JRE 也能用 |
| app/ai/client.py | DeepSeek 客户端 + RetryPolicy | 429/5xx/超时退避，尊重 Retry-After |
| app/ai/self_correct.py | 生成 → 校验 → 带诊断修复的循环 | 失败 fail-closed，坏图不出门 |
| app/ai/service.py | ChangeSet → 模块级三种文档的编排 | 单份文档失败不影响同模块其它文档 |
| scripts/demo_phase4.py | 离线演示（剧本模型） | 零成本复现"坏输出 → 诊断 → 修好" |
| tests/test_validator.py 等 5 个文件 | 73 项新单测 | 全套 139 项全绿 |

一次模块级生成的数据流：

```text
ChangeSet
  └─ group_by_module()              按目录切成模块
       └─ build_class_prompt()      契约 + 实体清单 + changed_functions + 代码块
            └─ SelfCorrectingGenerator
                 ├─ client.complete()          第 1 次，temperature=0.2
                 ├─ extract_plantuml()         剥围栏、去废话
                 ├─ PlantUMLValidator          不合格？
                 └─ build_repair_prompt()      诊断 + 上次原文，temperature=0.0
       └─ DocumentDraft(ok, needs_review, diagnostics, attempts)
```

## 2. Prompt 工程：三代演化

这一节是作业记录任务「你如何设计 Prompt」的原始材料，三代都保留原文，失败与修正都写清楚。

### 2.1 v1：直接请求，拿到一堆废话

第一版只有一句话：

```text
请根据以下代码生成 PlantUML 类图。
```

模型的典型回复（当时记录的现象）：

```text
好的，我为您生成了这个模块的类图。这个模块包含 UserService 和
UserRepository 两个类，UserService 依赖 UserRepository 完成数据访问：

[围栏开始] plantuml
@startuml
class UserService {
  +load(user_id)
}
@enduml
[围栏结束]

如果你需要我补充字段类型或者调整布局，请告诉我。
```

程序拿到的是"一段话里夹着代码"，必须先抠出来才能用。当时的做法是写正则去抠 @startuml…@enduml，
但这掩盖了真正的问题：**"生成一段代码"和"只生成一段代码"在概率分布上不是同一件事**，
前者是绝大多数训练数据的形态（文档里代码总是被解释包裹）。

### 2.2 v2：加"只输出代码"，再加 OK 握手

第二版加了否定指令和握手：

```text
你是一个 PlantUML 生成器。只输出代码，不要任何解释。
如果你明白了，请只回复 OK，然后等待我发代码。
```

效果与代价：

| 观察 | 结果 |
|---|---|
| 开场白 | 显著减少，从"每次都有"变成"偶尔出现" |
| 图后补一句 | 仍有，约三成（凭当时记录，非严格统计） |
| 代价 1 | 每次生成多一次 API 调用，延迟与成本约 +30%~100%（取决于输出长度） |
| 代价 2 | HTTP API 是无状态的，握手不是"记住状态"，而是每轮都要重放系统提示词——省下的 token 又被重放吃回去 |
| 代价 3 | 握手本身也可能不被遵守（模型回 "OK！我已经准备好了"），于是还要校验握手回复 |

**关键认识**：握手改善的是"模型此刻的注意力"，不是"输出的可验证性"。
它能降低废话说出口的概率，但不能把概率变成 0。

### 2.3 v3：四层约束（当前实现）

握手没有扔掉，而是降级为可选项 warmup（见 build_warmup），默认关闭。现在的四层是：

| 层 | 内容 | 解决什么 |
|---|---|---|
| 1. 输出契约 | 7 条硬规则：整段回复是一份 PlantUML；**第一个字符必须是 @**；**最后一行必须正好是 @enduml**；禁止围栏；@enduml 前后不许有任何句子；不许用中文全角标点当语法；信息不足也要出图，把疑问写进 note | 把"不要有废话"这种形容词，换成程序能逐条判定的命题 |
| 2. 结构化输入 + few-shot | 输入含 module 名、known entities、changed_functions、函数体；示例与任务同构（一个 Service 依赖一个 Repository） | 让"格式"从抽象要求变成可模仿的对象 |
| 3. 负面清单 | 只画输入里出现过的类/方法；不许发明字段；推断出的关系要标 stereotype 或 note；changed_functions 必须作为成员出现 | 幻觉出的类名比语法错误更难发现 |
| 4. 生成后机器校验 | 校验器 + 自我纠错（第 3、4 节） | 唯一能保证"发出去的文档渲染得出来"的一层 |

契约条款的设计原则：**可执行性**。

- "不要输出解释"——无法判定，模型说"这不是解释，这是说明"就绕过去了。
- "第一个字符是 @"——strip 之后取第一个字符就能判定，修复提示词也能精确指出。

同样地，契约里写"最后一行必须正好是 @enduml"，而不是"记得闭合"，是因为前者能被
rstrip().endswith("@enduml") 这样的表达式检查。
**一条无法被机器检查的约束，在工程上等于没有约束。**

### 2.4 few-shot 为什么不给"大图"

示例刻意用小模块：两个类、四个方法、一条依赖箭头。
如果示例是十几个类的复杂图，模型会模仿规模，输出变长，撞上 max_tokens 截断（E002 的头号来源）。
示例的作用是定"形态"，不是定"体量"。

## 3. 机器校验：三档校验器，主判据不依赖外网

| 层级 | 实现 | 依赖 | 用途 |
|---|---|---|---|
| 结构校验 | PlantUMLValidator / MarkdownValidator | 纯 Python | 永远可用，抓 90% 的低级错误 |
| 语法校验 | java -jar tools/plantuml.jar -checkonly | JRE 17 | 本机未装；CI 与生产启用 |
| 远程复核 | plantuml.com 官方服务 | 外网 | 可选加强，**永不作为唯一判据** |

为什么主判据不能用 plantuml.com：闸门一旦依赖外部服务，对方限流、断网、改接口，
我们的"质量闸门"就静默失效——而且失效时没有任何报错，只是坏图开始流进知识库。
这条与 ADR-007（本机无 Java）是同一条思路：**校验能力必须自持**。

### 3.1 PlantUML 结构规则

| 码 | 判定 | 对应的真实模型错误 |
|---|---|---|
| E001 | 没有 @startuml | 模型直接开始写 class A |
| E002 | 没有 @enduml | 输出被 max_tokens 截断（最常见） |
| E003 | @enduml 之后还有内容 | 图后补一句"以上是类图" |
| E004 | 图是空的 | 只回了 @startuml @enduml |
| E005 | 花括号未闭合 | class A { 之后忘了 } |
| E006 | 多余的 } | 嵌套块写坏 |
| E007 | 引号未配平 | 中英文引号混用 |
| E008 | 未知关系箭头 | 自创箭头 A <-|> B |
| E009 | 时序消息缺冒号 | A -> B 请求数据 |
| E010 | note 块未闭合 | note right 之后没有 end note |
| E011 | 成员行在类块外 | 缩进写坏 |
| E012 | 方法括号不配平 | load(a, b 少了右括号 |
| W001 | 声明了输入里没有的类 | 幻觉（ok 仍为 True，进报告） |
| W002 | 输入里被改的函数没出现 | 漏项 |
| W003 | 泛型类名未加引号 | class Repo<T> 是语法错误 |
| W004 | 块不以已知关键字开头 | 生造语法 |

**error 与 warning 的划分标准**是"能否渲染"：

- error：这张图渲染不出来，必须修；
- warning：能渲染，但内容不可信（幻觉、漏项），记录并交人审。

### 3.2 Markdown 规则与"容忍策略"

| 码 | 判定 | 处理 |
|---|---|---|
| M001 | 首行不是标题，或以寒暄开头 | error |
| M002 | 代码围栏单数（不配平） | error，会污染整篇渲染 |
| M003 | 空文档 | error |
| M004 | 出现 TBD / TODO / your-code 占位符 | error，等于交了半成品 |
| W005 | changed_functions 里的函数没被写到 | warning |

这里有一个刻意的不对称：**文档开头一句"好的，以下是 API 文档"被容忍并剥掉，标记 had_prose_before，不触发修复。**

理由：修复一轮要一次完整的 API 调用（数秒、数千 token），
而这句话对渲染结果零影响，抽取器一句 strip 就解决了。
反之，占位符与围栏不配平必须修，因为它们直接破坏最终 Markdown 的结构。

判据是**后果**，不是**礼节**。

## 4. Retry 与 Self-Correction：传输层与语义层

两者都叫"失败后重来"，但根因完全不同，混在一起会同时做错两件事。

| 维度 | Retry（app/ai/client.py） | Self-Correction（app/ai/self_correct.py） |
|---|---|---|
| 触发 | 429 / 5xx / 408 / 409 / 425 / 超时 / 连接错误 | 输出未通过校验 |
| 提示词 | 完全不变 | 追加诊断 + 上次原文 |
| 温度 | 不变 | 降到 0.0 |
| 次数 | max_retries=3（共 4 次尝试） | max_attempts=3 |
| 等待 | 指数退避 + 抖动，尊重 Retry-After | 不等待（本地校验，无网络） |
| 失败后 | 抛 LLMError，任务标 failed 可重试 | 返回 ok=False，文档标 stale，不发布 |

**为什么不能合并**：传输失败的根因在网络，重发同一请求是对的；
语义失败的根因在输出，重发同一个请求只会拿到同一个错误——温度 0.2 时甚至是几乎相同的错误。
所以语义层必须**改输入**（把诊断塞回去），而传输层必须**不改输入**（改了就不叫重试了）。

退避公式（RetryPolicy.delay_for）：

```text
Retry-After 存在  -> min(retry_after, backoff_max)
否则              -> min(backoff_max, base * 2**attempt) * (0.5 + 0.5 * jitter())
```

抖动（全抖动，不是固定比例）的作用：多个 worker 同时被限流时，避免它们齐步重试形成二次洪峰。
测试里用 jitter=lambda: 0.0 固定序列断言 [0.5, 1.0]，用 jitter 返回极值断言上下界（2.0 / 4.0）。

### 4.1 自我纠错的对话结构

修复轮不是"再问一次"，而是把上一轮的完整上下文重放，共 4 条消息：

```text
[system]    输出契约 + 实体规则 + PlantUML 方言白名单
[user]      module、known entities、changed_functions、代码块
[assistant] 模型上一次的**原始输出**（含废话和围栏，不做任何修剪）
[user]      校验诊断 + 原文 + "Fix ONLY the problems listed above"
```

两个细节值得写进论文：

1. **保留原始输出而不是只给抠出来的代码**：让模型看见自己写了什么，修复就变成"局部编辑"；
   如果只给干净代码，模型会当成新任务，整篇重写，反而引入新错误。
2. **修复轮温度降到 0**：修复是确定性问题，不需要创造性。实测里温度序列是 [0.2, 0.0, 0.2, 0.2]——
   每个文档首轮 0.2，只有修复轮是 0。

截断的处理：如果 finish_reason == "length"，说明不是模型写错而是空间不够，
此时把 max_tokens 乘 1.5 再进修复轮（2048 → 3072），而不是让它"更简洁"。

### 4.2 回灌给模型的诊断长什么样

demo 实跑时，第二次调用最后一条 user 消息的开头（原文）：

```text
Your previous PlantUML output failed machine validation.

Validation errors (each item is: code, line number, offending line, how to fix):
[E002] line 5: missing @enduml (output is probably truncated or the model did not finish)
    offending line: }
    fix: close the diagram with @enduml; keep the diagram shorter if it does not fit

Previous output:
@startuml
class UserService {
  +load(user_id)
  +delete(user_id)
}

Fix ONLY the problems listed above and keep everything else unchanged.
```

每个诊断都带：错误码（可被程序统计）、行号（模型能定位）、原文行（模型能对齐）、修法（模型的提示）。
这四样缺一，模型就会开始猜。

### 4.3 fail-closed

三轮（或两轮，视配置）仍不合格，返回 ok=False，document 标 stale / needs_review，
**不写知识库、不发 PR 评论**。

原则：宁可少一篇文档，不可多一篇错文档。
自动文档最坏的失败模式不是"没生成"，而是"生成了一份看起来正确、实际与代码不符的文档"——
后者比没有文档更有害，因为读者会信任它。

## 5. 编排：模块级、固定顺序、失败隔离

- 粒度：阶段一 TBD 已定稿为**模块级**，target_key = module:app.services。
  理由：类图单独看没有上下文，模块内多个类的关系才是架构信息。
- 顺序：类图 → API 文档 → 时序图。
  固定顺序让输出可预测，也让缓存键（module + prompt_version）有意义。
- 时序图只在存在非删除变更时生成：全是删除时没有调用链可画。
- 失败隔离：单份文档抛任何异常都包装成 X001 诊断，同模块其它文档继续。
  「一次 API 配额耗尽」不应该变成「整个模块一篇文档都没有」。

ok 与 needs_review 是两个正交维度：

| 组合 | 含义 | 行为 |
|---|---|---|
| ok=True, review=False | 无 error 无 warning | 直接发布 |
| ok=True, review=True | 无 error，但有 warning（幻觉/漏项） | 可发布，同时标记待审 |
| ok=False | 有 error，修复用尽或调用失败 | 标 stale，不发布，进人工队列 |

demo 里 api_doc 就是第二种：ok=True 但 review=True，
原因是剧本里没写 UserRepository.remove，触发 W005。
这不是 bug，正是设计意图——**能发，但提醒人看一眼**。

## 6. 实测记录（离线，剧本模型）

命令：python scripts/demo_phase4.py

输入是阶段三真实解析出来的三个变更：

```text
modified UserService.load          L5-6
added    UserService.delete        L8-9
added    UserRepository.remove     L16-17
```

剧本第 1 次故意返回"好的，我为您生成了类图："+ 围栏 + 缺 @enduml，观察管线是否自愈：

```text
[class_diagram]    ok=True  attempts=2 review=True
[api_doc]          ok=True  attempts=1 review=True   [W005] changed function 'UserRepository.remove' is not documented
[sequence_diagram] ok=True  attempts=1 review=False
summary: docs_total=3 docs_ok=3 docs_needing_review=2 repair_rounds=1
temperatures: [0.2, 0.0, 0.2, 0.2]
```

产物：var/phase4/app.services.class_diagram.puml、app.services.api_doc.md、app.services.sequence_diagram.puml。

这段实测证明的是**管线**：坏输出被抽取器剥掉围栏、被校验器判定 E002、诊断被回灌、第二次修好、温度按设计下降。
它**不能**证明"真实模型一定会这样"——剧本模型是我写的，它当然会按剧本走。这条界限必须写清楚。

## 7. 尚未验证的部分（如实声明）

| 项 | 状态 | 原因 |
|---|---|---|
| 真实 DeepSeek API 调用 | **未执行** | 本机无 API key，且调用会产生费用 |
| plantuml.jar -checkonly | **未执行** | 本机无 JRE 17（见 ADR-007）；代码路径已实现，未跑通 |
| GLM-4 长文本通道 | 未接入 | 接口留了 base_url / model 切换，未实测 |
| 真实模型的修复成功率 | 未知 | 需要跑 N 次统计（建议每个文档类型 50 次），这是下一阶段的实验设计 |
| 成本 | 仅估算 | 每模块约 3 次调用、输入约 2k tokens、输出约 600 tokens；按 deepseek-chat 价格粗估每模块不到 0.01 元，但**未按官方最新价目核对** |

论文里不能写"实测通过 DeepSeek 生成"，只能写"管线在剧本模型下通过；真实模型验证待补"。
这是我自己给自己划的线：**演示能替代验证流程，不能替代验证结论**。

## 8. 人机协同记录（作业要求的"记录你如何设计 Prompt"）

| 轮次 | 我（AI）给的方案 | 人类的质疑 | 最终结果 |
|---|---|---|---|
| 1 | "请生成 PlantUML 类图" | "输出带解释，程序没法直接用" | 加"只输出代码" |
| 2 | 加 OK 握手 | "每次多一轮 API 调用，这个成本值吗？而且 HTTP 是无状态的" | 握手降级为可选 warmup，不作为正确性依赖 |
| 3 | 我提出"在提示词里强调格式，让模型自己保证" | "语言模型没有输出约束能力，能保证格式的只有生成之后的校验" | 引入 validator + self-correction |
| 4 | 校验交给 plantuml.com | "对方断网/限流，你的质量闸门就静默失效了" | 纯 Python 结构校验为主判据，JRE 为加强，远程仅复核 |
| 5 | Markdown 前言也当 error 触发修复 | "为一句'好的'花一次 API 调用，划算吗？" | 改为容忍 + 记录 had_prose_before；只对破坏结构的错误修复 |

第 3 轮是这一阶段最重要的转折：我原本的思维是"把提示词写得更好"，
而人类的判断是"提示词只能提高概率，正确性要靠事后校验"。这句话直接决定了整个模块的结构。

## 9. 复现命令

```powershell
# 离线（零成本，默认）
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe scripts\demo_phase4.py

# 真实 API（需要 key，会产生费用）
$env:LLM_API_KEY='sk-...'
.\.venv\Scripts\python.exe scripts\demo_phase4.py --live

# 单测
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

## 10. 与作业评分点的对应

| 评分点 | 本阶段的回应 |
|---|---|
| 上下文有限怎么办 | 只把**变更函数**送进去（阶段三），再按模块切分、按 60000 字符预算截断，超出写 [truncated] 提示 |
| Prompt 工程 | 输出契约可执行化（首字符/末行）、few-shot 定形态、负面清单防幻觉、修复提示词带行号与修法 |
| 国产工具 | 默认 deepseek-chat，base_url 与 model 可切 GLM-4；未真调，见 §7 |
| 工程判断力 | Retry 与 Self-Correction 分层、校验不依赖外网、fail-closed、Markdown 容忍策略 |
