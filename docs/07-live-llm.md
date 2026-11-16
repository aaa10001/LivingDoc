# 阶段七：接上真实模型——从"剧本"到"有失败率的现实"

> 阶段四的演示用的是剧本模型：输出可控、结果好看，但它证明不了任何关于模型的事。
> 这一阶段接入了真实的 OpenAI 兼容端点（阿里云百炼 MaaS，模型 deepseek-v4.1-flash-1017），
> 跑了 7 轮共 21 份文档，得到一组**有失败率**的数据，并且靠这组数据抓出了两个真实的代码 bug。

---

## 1. 接入了什么

| 项 | 值 |
|---|---|
| 端点 | 阿里云百炼 MaaS 的 OpenAI 兼容地址（/compatible-mode/v1） |
| 模型 | deepseek-v4.1-flash-1017 |
| 协议 | OpenAI chat/completions；app/ai/client.py 的 DeepSeekClient 无需改动 |
| 密钥管理 | 只写入 .env（已在 .gitignore 中），不进入代码、文档、日志、测试夹具 |
| 超时/重试 | LLM_TIMEOUT_SECONDS=180，RetryPolicy(max_retries=3)，429/5xx 指数退避 |
| 容器内可达性 | 容器访问该端点返回 200（与 github 不同，国内地址可达） |

一个值得记录的细节：该模型返回体里带 reasoning_content。当 max_tokens 设得很小（如 8）时，
content 会是空字符串——**推理 token 与正文共享同一个 completion 预算**。这不是 bug，
但会直接影响"为什么偶尔只回一个标题"（见 §4）。

---

## 2. 评测方法

新增 scripts/live_eval.py（约 9.5KB）：

1. 在本机造一个两提交的 git 仓库，源码是一份有代表性的 Python 模块
   （DomainError / UserNotFound / User dataclass / UserRepository / UserService；
   head 增加 is_active、deactivate、remove、UserRepository.delete）；
2. 用真实 git show 取两个版本，交给 tree-sitter 得到 ChangeSet（不经过网络）；
3. 用真实 DeepSeekClient 调 DocumentationService.generate_all，反复跑 N 轮；
4. 每轮把三份文档落盘到 var/live/runNNN/，统计写入 var/live/report.json（追加式）：
   attempts、ok、needs_review、tokens、latency、错误码、警告码、字符数。

为什么不用 demo_phase4.py：那个脚本的目的是"演示流程"，模型是剧本；这里要的是
**可统计的量**，所以必须重复跑、必须落 JSON、必须把失败也记下来。

---

## 3. 七轮结果

| 文档类型 | 份数 | ok | 首次通过 | 总 attempts | tokens | 警告 |
|---|---|---|---|---|---|---|
| class_diagram | 7 | 7 | 7 | 7 | 11,991 | 0 |
| sequence_diagram | 7 | 7 | 7 | 7 | 11,361 | 0 |
| api_doc | 7 | **4** | 4 | **13** | 25,531 | 14 × W005 |
| 合计 | 21 | 18 | 18 | 27 | 48,883 | 14 |

- 总体一次通过率 18/21 = **86%**；
- 每轮三份文档耗时 18.7–38.1 s；
- 失败全部集中在 api_doc：2 次 M001（修复提示词 bug，见 §5.2）、1 次 M005（空壳文档，见 §5.1）；
- api_doc 的 14 个 W005 表示"变更函数没有被文档覆盖"——它是 warning，不阻断发布，
  但正好说明"结构合格"与"内容完整"是两件事。

对照一下阶段四的剧本演示：那份演示里三份文档全部 ok、repair_rounds 可控。
真实模型给出的第一组数字是 86%，而且失败 Mode 高度集中。这是"演示能替代验证流程、
不能替代验证结论"的一个具体例证。

---

## 4. 失败模式：为什么会只回一个标题

run7 的 api_doc 三次尝试都只返回了一个标题：

~~~
### app.UserRepository.delete
~~~

31 个字符。finish_reason 是 stop（不是 length），说明模型"主动"停在了这里。
结合 reasoning_content 的行为，最可能的解释是：模型把预算花在了推理上，
正文只起了个头就结束；也可能是它对"API 文档"这个任务选择了先给目录。

无论原因是什么，工程上的结论是明确的：**校验器必须能识别"结构合法但没有内容"的文档**。
在此之前，M001–M004 全部通过，这份 31 字符的"文档"会被发布进知识库。

---

## 5. 真实评测抓出的两个 bug

### 5.1 校验器漏判"空壳文档"（M005 / W006）

发现过程：run4/run5 的 api_doc 内容是 26 与 28 个字符，但 ok=True、needs_review 只有 W005。
打开文件只看到一行标题。

根因：MarkdownValidator 只检查四件事——不是寒暄开头（M001）、围栏配平（M002）、
非空（M003）、无占位符（M004）。一行标题完美满足这四条。

修复（app/ai/validator.py）：新增两条基于"正文体量"的规则。
正文 = 去掉标题行与围栏行之后的文本。

| 规则 | 判据 | 级别 | 含义 |
|---|---|---|---|
| M005 | 正文 < 40 字符 | error | 没有正文，等于没写 |
| W006 | 正文 < 120 字符 | warning | 太薄，需要人工看 |

配了 3 个新测试（空壳 → M005、短正文 → W006、正常正文 → 两条都不出现），
并把 tests/test_service.py 里的 API_MD 夹具正文扩到 120 字符以上——
**夹具自己也要经得起新规则**，否则测试会变成"用更低的门槛证明系统正确"。

### 5.2 修复提示词不区分文档类型（本次最有价值的发现）

现象：run4 与 run5 的 api_doc 连续 3 轮失败，错误码 M001（不是标题开头）。

根因追踪到阶段四写的 build_repair_prompt：

~~~
Your previous PlantUML output failed machine validation.
...
Return the complete corrected {kind} diagram.
{_OUTPUT_CONTRACT}
~~~

它对 kind 不做任何区分。Markdown 文档一旦进入修复轮，就被要求"返回修正后的 PlantUML 图"，
并追加了要求"第一个字符是 @"的 _OUTPUT_CONTRACT。模型照做，回了一段 @startuml，
校验器自然判 M001；下一轮再用同样的提示词修，还是错——三轮全废。

这个 bug 在阶段四的剧本演示里永远不会出现，因为剧本里没有"Markdown 文档需要修复"这一幕。

修复（app/ai/prompts.py）：按 kind 分支。

| kind | 提示词开头 | 目标 | 契约 |
|---|---|---|---|
| api | Your previous Markdown document failed machine validation. | Markdown document | 以 level-2 标题开头、保留原有小节、无前言/结尾/整篇围栏 |
| 其它 | Your previous PlantUML output failed machine validation. | class/sequence diagram | 原 _OUTPUT_CONTRACT（@startuml … @enduml） |

配了 2 个测试：api 的修复提示词不含 @startuml、class 的仍含。修复后 run6 的 api_doc 首次通过。

### 5.3 还没解决的：修复循环造不出内容

run7 的 api_doc 三轮都只回标题，M005 一直成立，最终 fail-closed（ok=False，不发布）。
这说明当前的自纠错只能修"格式"，修不了"没有内容"。诚实地写在这里：
**对空内容的修复，需要的是重新生成（换提示词/换温度/换模型），而不是在错误上迭代。**

---

## 6. 强制截断实验：验证自纠错的预算抬升

除了自然失败，还做了一次受控实验：把 max_tokens 压到 260，强制第一轮被截断。

~~~
attempt 0: finish='length' tokens=1321 codes=['E001']  (content 被截空)
           -> 日志：output truncated by max_tokens (length); raising budget for repair round
attempt 1: finish='stop'   tokens=1558 codes=[]       len=933, 末行 @enduml
ok = True, attempts = 2
~~~

这条链路证明了三件事：截断会被 finish_reason 识别、预算按 1.5 倍抬升、诊断会带着错误码回灌。
它是"自我纠错在真实模型上确实工作"的最小证据。

---

## 7. 独立校验：PlantUML 真的能渲染

把 7 轮里生成的 14 个 .puml（class + sequence）复制到 ASCII 临时目录，挂进容器，
用镜像里的 PlantUML 1.2024.7 逐个执行：

~~~
java -jar /opt/plantuml/plantuml.jar -checkonly -failfast2 <file>
plantuml: ok=14 bad=0
~~~

这是 ADR-007 第一次对**真实模型产物**闭环：模型输出 → 我们的结构校验 → PlantUML 自己的语法校验。
阶段四只能证明"我们的校验器认为它合法"，现在多了一道独立判据。

---

## 8. 全栈端到端：真实模型 + Redis + prefork + PostgreSQL

修好容器内仓库权限后，用 compose 起的全栈跑了一条真实推送：

~~~
POST /webhook/github            -> 202 accepted
analyze_push (prefork worker)   -> 克隆 file:///app/e2e.git，diff 1 个文件
parse_run                       -> tree-sitter，entities_changed=2
generate_run (真实 API)         -> 3 份文档，15.9 s，tokens_out=5722
publish                         -> 3 个 created v1
run                             -> succeeded
~~~

PostgreSQL 里的结果：

| doc_type | model | prompt_version | render_ok | chars |
|---|---|---|---|---|
| class_diagram | deepseek-v4.1-flash-1017 | v3-2025-11 | true | 309 |
| api_doc | deepseek-v4.1-flash-1017 | v3-2025-11 | true | 933 |
| sequence_diagram | deepseek-v4.1-flash-1017 | v3-2025-11 | true | 418 |

原始类图（从 PG 里取出的真实内容）里还有一段 note：

~~~
note right of UserService
  Input provides no parameter or return types
  for delete(uid) and load(uid); they are
  shown untyped rather than invented.
  Returned dict in load is not a known entity.
end note
~~~

这条 note 是 Prompt 里"信息不足也要出图，并把疑问写进 note"那条契约的直接产物——
它没有编造类型，而是把不确定性写在了图里。这是本阶段最像"工程可用"的一个细节。

---

## 9. 成本与规模

| 指标 | 实测 |
|---|---|
| 单份文档平均 tokens | 约 2,330（含输入） |
| 一轮三份 | 约 5,000–8,900 tokens |
| 七轮合计 | 48,883 tokens |
| 单轮墙钟 | 18.7–38.1 s |

按供应商单价换算即可得到单次分析的直接成本；这里不给具体金额，因为单价随套餐变动。
可复用的结论是：**一次提交生成三份文档的 token 量在 5k–9k 量级，属于可接受的量级。**

---

## 10. 边界与未做的事

1. 只测了一个模型（deepseek-v4.1-flash-1017），没有横向对比 GLM-4 / DeepSeek-Coder；
2. 只有一种 diff（一个文件、两个类、五个变更函数），没有大仓库、多模块的压力样本；
3. api_doc 的样本只有 7 份，4/7 的通过率置信区间很宽，不能当成稳定指标；
4. 没有人工对照评测（"人写的文档"与"AI 写的文档"的可读性/准确性对比）；
5. 没有并发调用模型，未测 429 限流下的表现（Retry 有单元测试，但没有真实限流场景）；
6. 密钥只在本机 .env，未做密钥轮换/审计。

---

## 11. 对论文的意义

这一阶段最重要的不是"接通了 API"，而是**第一次有了失败率**：

- 阶段四的结论是"我们的管线能产出合格文档"，证据是剧本演示；
- 阶段七的结论是"在 21 份真实产物里，18 份一次通过、3 份失败，失败集中在 api_doc，
  其中两次是我自己的提示词 bug"。

失败率的出现，才让下面这些设计有了依据：
M005 这条规则不是凭空加的，是被 31 字符的空壳文档逼出来的；
修复提示词的分支不是"更优雅"，是被 M001 连续三次失败逼出来的。

**可引用的结论**：真实模型不会按你的想象失败。它失败的方式，恰恰是你最没写校验的地方。
