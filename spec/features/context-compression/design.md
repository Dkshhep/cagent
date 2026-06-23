# 设计 Spec:上下文压缩(history 裁剪 + 蒸馏占位)

> 日期:2026-06-21
> 状态:📝 设计已拍板,待实现
> 遵循:[working-norms.md](../../conventions/working-norms.md)(Spec 先行 + 测试随产出积累)

## 1. 概述

把 cagent 的上下文压缩从"小窗口 + 字符预算 + 每轮动态压缩"升级为
**"消费 token 预算 + 缓存友好的分档裁剪 + 中间轮蒸馏占位"**。

核心目标有两个,且互相牵制:
- **不溢出**:无论单轮塞进多大的工具结果,组装出的 prompt 必然 ≤
  `context-budget` 给出的输入硬预算。
- **保缓存**:尽量少触发压缩、压缩时压狠,且未触发的轮次 history 字节稳定,
  让 prefix + history 这段大前缀能命中 provider 的前缀缓存。

本特性是对 [`context_manager.py`](../../../cagent/context_manager.py) 的演进,
不改变"5 段 prompt"的基本结构。预算单位、窗口阀门、section target/max 由
[`context-budget`](../context-budget/design.md) 统一定义;本 spec 只定义**触发后的
history 渲染策略、分档收敛链以及中间轮蒸馏占位**。

## 2. 背景与问题(为什么做)

现状(改造前)的问题是"字符预算 + 静态 section 上限 + 压缩口径分散":

- 早期总预算曾是 `DEFAULT_TOTAL_BUDGET = 12000` **字符**,history 段仅 5200 字符
  (~1300 token 英文)。当前代码虽已提高到更大的字符预算,但仍然不是 token 口径。
- 超限判断用 `len(prompt)`(**字符**),而模型按 **token** 计费/限长。
  中英文混合时字符/token 比例漂移 2~3 倍,字符预算只是粗代理。
- section 预算仍是静态绝对值,没有消费 `context-budget` 定义的动态 `history_budget`
  和 `compression_target`。
- `_render_history_section` 曾依赖 `file_summaries` 重写旧 read 条目。摘要一变,
  history 中段字节就变 → 前缀缓存被自己击穿。本期 v1 不再依赖
  `file_summary`,旧工具结果只做机械折叠和头尾裁剪。
- 易变的 `memory`(写操作时变)/ `relevant_memory`(每轮按 query 变)排在
  `history` **之前**,缓存切点落在它们前面 → 大块 history 永远进不了缓存。

要把 cagent 当真实生产力工具用,必须贴近真实窗口(几万~十几万 token),
而一旦"贴着窗口百分比用满",上述字符近似与缓存击穿问题就从"无所谓"变成"会出事"。

## 3. 目标与非目标

### 目标
- 消费 `context-budget` 产出的 `soft_trigger` / `hard_trigger` /
  `compression_target` / `history_budget`。
- history 裁剪走**必然终止的分档链**,火力集中在工具结果。
- 为后续中间轮**蒸馏(LLM)** 预留顺序和边界:v1 不实际调用模型蒸馏,
  不写 checkpoint / memory 回填。
- 未触发压缩的轮次,history 渲染**确定性、append-only**(缓存友好)。

### 非目标(YAGNI,本期不做)
- 不定义 token 估算、默认窗口、section target/max 或输出预留;这些属于
  `context-budget`。
- 不接真实 tokenizer(tiktoken / provider SDK)。
- 不重新设计 prompt section 顺序。
- 不在 v1 实现 LLM 蒸馏、checkpoint 回填或 memory 回填。
- 不常规裁剪 `memory` / `relevant_memory`;它们只在预算 spec 指定的极端兜底阶段
  作为 history 之后的后备压缩对象。

## 4. 已完成的前置改动(M0/M1/M2/M3,铺路)

下列四项**已实现或已完成设计确认**,是本特性的地基,记录在此以便追溯:

| 编号 | 改动 | 文件 | 目的 |
|------|------|------|------|
| M0 | section 顺序改为 `prefix → history → memory → relevant_memory → current_request` | `context_manager.py`(`SECTION_ORDER` / `_assemble_prompt`) | 把易变短期记忆挪到 history 之后,缓存切点落到 history 之后 |
| M1 | history 预算内直接渲染 raw(append-only),仅超预算才压缩 | `context_manager.py`(`_render_history_section` 新增短路) | 未触发压缩的轮次 history 字节稳定,可被缓存 |
| M2 | 移除 workspace `git status` 字段 | `workspace.py`(`__init__`/`build`/`text`/`fingerprint`) | 改动文件列表每次写操作都变,放进稳定前缀会反复击穿缓存 |
| M3 | 完成 `context-budget` 预算分配 spec | `spec/features/context-budget/design.md` | 定义 `estimate_tokens`、`soft_trigger`、`hard_trigger`、`compression_target`、动态 `history_budget` 和 metadata 口径,供压缩链消费 |

## 5. 触发输入:消费预算 spec 的阀门

```
soft_trigger       ← 来自 context-budget
hard_trigger       ← 来自 context-budget
compression_target ← 来自 context-budget
history_budget     ← 来自 context-budget 动态计算
```

设计要点:
- 压缩模块不重新计算窗口百分比,只消费预算模块给出的 token budget。
- 当总 prompt 未超过 `soft_trigger` 时,history 必须保持 raw append-only。
- 当超过 `soft_trigger` 时,压缩目标是预算模块给出的 `compression_target`,不是
  "刚好低于软阀"。
- 当超过 `hard_trigger` 时,进入强制收敛路径,优先压 history。

## 6. 量纲:token 体积

压缩判断使用 `context-budget` 定义的 `estimate_tokens(text)` 或其后续替代实现。
本 spec 不维护独立的 token 估算表,避免预算口径分叉。

压缩代码只关心两个问题:

- 当前渲染体积是否超过预算模块给出的触发线。
- 每一档裁剪后,history 和总 prompt 的估算 token 是否已经收敛到目标以内。

## 7. 收敛链:必然终止的 4 档裁剪

```
不可裁:头 3 条消息(任务定义/早期约束)+ current_request  → 记为 FLOOR
触发:if tokens(prompt) <= soft_trigger: 直接返回(绝大多数轮在此结束)

档1  旧 tool 结果机械压缩      ← 重复 tool 折叠;非重复旧 tool 头尾裁剪
                              不使用 file_summary,不做语义替换
档2  中间区蒸馏占位           ← v1 只标记候选区:
                              history > 30 时保留头 3 + 尾 27,
                              history[3:-27] 作为 future distillation candidate
档3  最近 8 条 tool 结果截断  ← 只有前两档仍不够时才触发;
                              只压最近 8 条中的 tool content,保头尾
档4  history 层最终兜底       ← 保护头 3,从第 4 条开始旧到新头尾裁剪,
                              直到总 prompt 收敛到预算内
```

**终止性证明思路**:前 3 档"尽量低损",任一档压够即提前返回;档 4 在 history
层按旧到新不断缩短可裁条目,并保护头 3 与当前请求。只要 fixed sections
(`prefix` / `memory` / `relevant_memory` / `current_request`)本身没有超过输入硬预算,
档 4 必然能把 history 收敛到剩余空间内。若 fixed sections 本身已超限,应返回明确错误
或交由 `context-budget` 的固定 section 兜底策略处理,不静默发送 over-budget prompt。

**裁剪优先级**:旧 tool 结果 > 中间区蒸馏候选 > 最近 8 条 tool 结果 > 普通历史兜底。
旧工具结果不只占地方,还可能因文件/测试状态过期而误导模型,所以最先处理。

本期前提:
- `current_request` 已有 hard max,用户粘贴大量内容时直接给出提示,不静默裁剪。
- 模型输出 `max_tokens` 已有限制。
- 因此 v1 的上下文膨胀主因是工具结果,压缩范围集中在 history,尤其是 tool result。

## 8. 单位:消息条数定位,token 体积收敛

- **消息条数**(`session["history"]` 的 item,与现有数据结构 1:1):负责"裁哪些、
  划头 N / 尾 M 边界"。用户输入 / 模型输出 / 工具结果各算一条。
- **token 体积**:负责"裁完是否收敛"的判断。
- 裁剪动作按消息 `role` / `name` 分流。v1 不直接复用旧
  `_compressed_history_entries` 主逻辑,而是新建 history compression pipeline:
  旧 tool 结果折叠/头尾裁剪 → 蒸馏候选区边界标记 → 最近 8 条 tool result
  延后裁剪 → history 层最终兜底裁剪。

## 9. 头尾裁剪 Helper 策略

现有 `_tail_clip` / `_token_clip` 都是"保开头、丢尾部":前者按字符截断,
后者按估算 token 截断,但二者都不保留尾部。因此它们不作为 tool result
头尾裁剪主逻辑复用;也不改变它们的现有语义,避免影响 `memory` /
`relevant_memory` 等既有 section 裁剪逻辑。

v1 新增 token-aware 头尾裁剪 helper,形如:

```python
_token_head_tail_clip(text, head_tokens=60, tail_tokens=80)
```

设计要求:
- 只作用于 tool `content`;tool 名和 args 单独保留,不计入 content 的头尾预算。
- 默认保留头部约 `60 token`、尾部约 `80 token`。
- 中间插入明确占位,格式包含被裁剪 token 估算,例如:
  `[... 中间内容已裁剪，约 N token ...]`。
- helper 使用 `estimate_tokens` 判断收敛,不使用字符长度作为主判断。
- 如果单条 tool content 可用预算不足以容纳头 60 + 尾 80 + 占位,
  优先保留尾部和占位,再用剩余预算保留头部。`run_shell`、测试日志、
  traceback 等输出的错误和 summary 往往在尾部,所以尾部优先级高于头部。
- v1 只对 tool content 使用该 helper,不对用户消息和 assistant 决策文本使用。

## 10. 中间轮蒸馏占位(v2)

### 为什么不能直接删中间
中间区(头 3 与尾 27 之间)是"有效与无效交错":既有可丢的(已完成子任务、重复读取、
冗长成功输出),也有不可丢的(决策与否决、已做的修改、当前卡点由来、用户中途追加的
约束)。按位置一刀切会误伤后者,导致模型重复劳动 / 推翻已做改动 / 违反早先约束。

v1 不实际删除中间区原文,也不调用 LLM。它只预留顺序和边界:

```
if len(history) > 30:
    keep history[0:3]
    mark history[3:-27] as future distillation candidate
    keep history[-27:]
else:
    no distillation candidate
```

单位按 `session["history"]` 的 item 计算:用户输入 / 模型输出 / 工具结果各算一条。
v2 实现时,必须先把中间区蒸馏进 checkpoint / memory,再移除原文,不可颠倒。

### 现有容器(为 v2 预留)
| 要蒸馏的东西 | 现成容器 | 状态 |
|--------------|----------|------|
| 进度 / 目标 | checkpoint `current_goal` | ✅ |
| 当前卡点 | checkpoint `current_blocker` | ✅ |
| 已完成的事 | checkpoint `completed` | ⚠️ 字段在,但只塞过 final_answer |
| 否决的方案 | checkpoint `excluded` | ⚠️ 字段在,但永远是空 `[]` |
| 改了哪些文件 | episodic note / 后续结构化文件记忆 | ⚠️ write/patch 现状只失效不记录 |
| 决策 / 约束 / shell 结论 | `append_note(text, kind=...)`(支持 episodic/process/durable) | ✅ |
| 项目级约定 | `DurableMemoryStore` 四个固定 topic | ✅ |

两个缺口都是**字段已存在、只是没人写**(`checkpoint["excluded"]` /
`checkpoint["completed"]` 的中途填充),无需改数据结构。

### v2 蒸馏方式:LLM(B)
- 把中间 N 条原文喂给模型,产出一段**结构化摘要**:进度 / 已改文件 / 决策 / 否决 /
  约束。能抓住启发式拼接抓不住的"为什么"语义。
- 每次蒸馏花一次模型调用 → **做成 feature flag**(与现有 `memory` /
  `relevant_memory` / `context_reduction` 风格一致),可在 ablation 实验里对比开/关。
- 蒸馏产出**回填**:决策/否决 → checkpoint `excluded` + 可选 durable `key-decisions`;
  改动/shell 结论 → `append_note(kind="process")`;中途完成项 → checkpoint
  `completed`;用户约束 → durable `project-conventions` / `user-preferences`。
- **顺序铁律**:先回填成功,再移除原文,不可颠倒。

## 11. 决策记录

| 决策 | 选择 |
|------|------|
| 预算来源 | `context-budget` 提供 token 估算、阀门、section budget 和 compression target |
| 触发输入 | `soft_trigger` / `hard_trigger` / `history_budget` / `compression_target` |
| 软阀触发后目标 | 压到 `context-budget` 定义的 `compression_target` |
| 收敛链 | 档1 旧 tool 机械压缩 → 档2 蒸馏占位 → 档3 最近 8 条 tool 截断 → 档4 history 层兜底,必然终止 |
| 裁剪单位 | 消息条数定位 + token 体积收敛 |
| 旧 tool 结果 | 重复旧 tool 折叠;非重复旧 tool 头尾裁剪;不使用 `file_summary` |
| 蒸馏占位 | `history > 30` 时保留头 3 + 尾 27,中间区标记为 future distillation candidate |
| 最近现场 | 最近 8 条只在前序压缩后仍不够时裁剪,且只裁 tool content |
| 最终兜底 | 保护头 3,从第 4 条开始旧到新头尾裁剪 history |
| memory/relevant_memory | v1 常规阶段不裁剪;fixed sections 自身超限时返回明确错误或交给 `context-budget` 固定 section 兜底 |
| prefix | v1 不裁剪工具定义和安全规则 |

## 12. 影响的文件

| 文件 | 改动 |
|------|------|
| `cagent/context_manager.py` | 消费预算模块产出的阀门与 history_budget,实现 v1 history/tool-result 收敛链 |
| `cagent/runtime.py` | v1 不改;v2 蒸馏时再接 LLM 调用与 checkpoint/memory 回填 |
| `cagent/memory.py` | v1 不依赖 `file_summary`;v2 若需再设计结构化文件记忆 |
| `cagent/models.py` | (后续)确认 prefix+history 作为可缓存前缀发出 |
| `tests/test_context_manager.py` | 预算触发接入、旧 tool 折叠/头尾裁剪、蒸馏占位 metadata、最近 8 条与最终兜底 |
| `tests/test_cagent.py` | v1 不新增蒸馏端到端;保留 current request 过大显式失败等链路测试 |
| `CLAUDE.md` | 更新上下文预算/压缩说明 |

## 13. 测试积累(对应规范 2)

- **预算接入**:体积 < `soft_trigger` 不压缩(确定性 append-only);超软阀触发并压到
  `compression_target`;单轮塞入超大工具结果时 hard trigger 强制收敛。
- **旧 tool 结果优先压缩**:重复旧 `read_file` / tool 结果折叠,不引用 `file_summary`;
  非重复旧 tool 输出按头尾裁剪。
- **头尾裁剪 helper**:长 tool content 被裁成"头部 + 含 token 估算的裁剪占位 +
  尾部";尾部错误/summary 在裁剪后仍保留;小预算场景优先保留尾部和占位,
  头部按剩余预算缩短;现有 `_token_clip` / `_tail_clip` 行为不变。
- **蒸馏占位**:`history > 30` 时 metadata 标记保留头 3、尾 27 和中间
  future distillation candidate;`history <= 30` 不进入候选逻辑。
- **最近 8 条保护**:只有旧 tool 压缩和蒸馏占位后仍未达到目标时,才裁剪最近 8 条中的
  tool result;用户消息和 assistant 决策文本不裁剪。
- **最终兜底 + 终止性**:构造极端用例,断言保护头 3,从第 4 条开始按旧到新头尾裁剪,
  最终 prompt ≤ 预算且循环终止。
- **缓存友好**:未触发压缩时,连续两轮(仅尾部追加)的 history 渲染字节前缀一致。
- **当前请求保护**:`current_request` 过大仍走显式报错,不参与静默压缩。
