# 设计 Spec: Prompt 预算分配

> 日期: 2026-06-21
> 状态: 设计已确认, 待实现
> 关联: `spec/features/context-compression/design.md`

## 1. 概述

本 spec 记录 cagent prompt 预算分配方案。它补充现有 `context-compression` spec:

- `context-compression` 负责压缩、裁剪、收敛链和中间轮蒸馏。
- `context-budget` 负责 token 估算、模型窗口阀门、section 预算分配和默认参数。

核心决策: prompt 预算主单位从字符切换为估算 token。字符统计仍可保留在 metadata 中用于调试和兼容旧报告, 但不再作为裁剪触发和 section 分配的主判断依据。

本期边界: 先完成预算 spec, 不要求同步完善压缩逻辑。压缩模块后续只需要消费这里产出的 token budget、trigger 和 target, 不在本 spec 中决定具体压缩算法。

## 2. 目标与非目标

目标:

- 定义 prompt 预算的主单位、估算方法和默认窗口参数。
- 定义每个 section 的 target/max 预算和动态 history 预算规则。
- 定义 soft/hard trigger 与 compression target 的数值来源。
- 定义预算相关 metadata, 便于后续 trace、report、metrics 使用同一套口径。

非目标:

- 不实现真实 tokenizer, 本期只定义 `estimate_tokens(text)`。
- 不设计完整压缩链, 不处理中间轮蒸馏和历史消息删除策略。
- 不改变 prompt section 顺序; 继续沿用 `prefix -> history -> memory -> relevant_memory -> current_request`。
- 不把旧字符预算立刻删除; 字符统计作为兼容字段保留。

## 3. Token 估算

短期不接真实 tokenizer, 先使用 `estimate_tokens(text)` 做跨 provider 的保守估算。后续可以替换为 OpenAI、Anthropic、DeepSeek 或 Ollama 对应 tokenizer, 但预算逻辑不随 tokenizer 替换而变化。

估算规则:

| 文本类型 | 估算方式 |
| --- | --- |
| ASCII / 英文 / 代码 | `chars / 4` |
| CJK 中文、日文、韩文统一表意字符 | `chars / 1.5` |
| 符号、JSON、空白密集文本 | `chars / 3` |

最终结果乘以 `1.15` 安全系数, 用于吸收中英文混合、路径、JSON、日志等内容带来的漂移。

实现要求:

- `estimate_tokens(text)` 必须是独立函数, 方便日后替换为真实 tokenizer。
- 所有 prompt 阀门、section budget、压缩目标都使用估算 token。
- metadata 同时记录估算 token 和字符数, 但字符数只用于可观测性。

## 4. 默认窗口参数

默认按 128k token 窗口设计, 输出和安全余量先从输入窗口中扣除。

```python
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_OUTPUT_RESERVE_TOKENS = 4_096
DEFAULT_SAFETY_MARGIN_TOKENS = 2_048

input_hard_budget = (
    DEFAULT_CONTEXT_WINDOW_TOKENS
    - DEFAULT_OUTPUT_RESERVE_TOKENS
    - DEFAULT_SAFETY_MARGIN_TOKENS
)

soft_trigger = input_hard_budget * 0.60
hard_trigger = input_hard_budget * 0.90
compression_target = input_hard_budget * 0.30
```

在默认参数下:

| 名称 | 值 |
| --- | ---: |
| `input_hard_budget` | `121_856` |
| `soft_trigger` | 约 `73_114` |
| `hard_trigger` | 约 `109_670` |
| `compression_target` | 约 `36_557` |

设计含义:

- 未超过 `soft_trigger` 时, 不主动压缩 history, 保持 raw append-only, 优先保护 prompt cache。
- 超过 `soft_trigger` 时, 压缩到 `compression_target` 附近, 而不是只压到刚好低于阈值。
- 超过 `hard_trigger` 时, 进入强制压缩路径, 先压 history, 仍不收敛时再压 `relevant_memory` 和 `memory`。

## 5. Section 预算分配

固定 section 使用 target/max, `history` 不写死, 动态吃剩余预算。

| Section | Target | Max | 策略 |
| --- | ---: | ---: | --- |
| `prefix` | `8_000` | `16_000` | 工具协议、系统规则、workspace 摘要、checkpoint。稳定且可缓存, 不应频繁裁剪。 |
| `memory` | `4_000` | `8_000` | 工作记忆、文件摘要、任务摘要。必须有硬上限, 避免挤占 history。 |
| `relevant_memory` | `2_000` | `4_000` | 检索得到的少量高相关笔记。宁可少而准, 不要长。 |
| `current_request` | soft max `1600` | hard max `2000` | 当前用户请求优先级最高, 但单条请求过大时必须显式处理。 |
| `history` | 动态 | 动态 | 主上下文弹性池, 通常占输入预算的 `55%-75%`。 |
| `output_reserve` | `4_096` | `8_192` | 给模型输出 tool call、patch、final 留空间。 |

`history_budget` 按实际 section 体积动态计算:

```python
input_hard_budget = context_window_tokens - output_reserve_tokens - safety_margin_tokens
fixed_tokens = prefix_tokens + memory_tokens + relevant_memory_tokens + current_request_tokens
history_budget = max(min_history_tokens, input_hard_budget - fixed_tokens)
```

约束:

- `current_request` 不参与静默裁剪。
- 如果 `current_request` 单独超过 hard max, runtime 应返回明确错误, 要求用户转成文件、附件或拆分输入。
- `prefix` 的工具调用协议和安全规则是最后兜底才可裁剪的内容; 正常实现应优先缩短 workspace/checkpoint 摘要, 不动工具协议。
- `history` 是主要弹性池, 旧工具输出、旧日志、重复读取结果应先被压缩或替换为摘要。

## 6. 保留与裁剪优先级

保留优先级从高到低:

```text
current_request
> prefix/tool contract
> recent history
> checkpoint/process memory
> relevant_memory
> old tool output
> old dialogue
```

裁剪优先级从高到低:

```text
duplicate or stale tool output
> old read_file raw content
> old shell/test logs
> old assistant chatter
> relevant_memory
> memory
> non-contract prefix content
```

实现时应优先保留最近交互和当前任务决策, 不应为了保留旧工具原文而裁掉当前请求、工具协议或最近失败反馈。

## 7. 触发与收敛行为

Prompt 组装流程:

1. 渲染 `prefix`, `memory`, `relevant_memory`, `current_request` 的初始文本, 并估算 token。
2. 从 `input_hard_budget` 中扣除这些固定 section, 得到动态 `history_budget`。
3. 渲染 history:
   - 如果总 prompt 未超过 `soft_trigger`, history 保持 raw append-only。
   - 如果总 prompt 超过 `soft_trigger`, history 压缩到 `compression_target` 附近的剩余空间。
   - 如果总 prompt 超过 `hard_trigger`, 强制继续压缩 history。
4. 如果强制压缩 history 后仍不收敛, 按裁剪优先级压 `relevant_memory`, 再压 `memory`。
5. 如果仍然超过 `input_hard_budget`, 返回明确错误或进入极端兜底压缩; 不允许静默发送 over-budget prompt。

压缩目标不是"刚好低于 soft trigger", 而是压到更低的 `compression_target`。这样可以拉长压缩周期, 避免每轮都轻微压缩导致 prompt cache 频繁失效。

## 8. Metadata

Prompt metadata 必须同时保留 token 与字符维度:

```python
{
    "prompt_chars": int,
    "prompt_tokens_estimated": int,
    "prompt_token_budget": int,
    "section_token_budgets": {
        "prefix": int,
        "history": int,
        "memory": int,
        "relevant_memory": int,
        "current_request": None,
    },
    "sections": {
        "prefix": {
            "raw_chars": int,
            "rendered_chars": int,
            "estimated_tokens": int,
        },
        "...": {},
    },
    "budget_trigger": "none" | "soft" | "hard" | "request_too_large",
    "compression_target_tokens": int,
}
```

兼容要求:

- 旧字段 `prompt_chars`, `prompt_budget_chars`, `sections[*].rendered_chars` 可继续保留。
- 新实现必须新增 token 字段, 并让报告、trace、metrics 优先使用 token 字段判断是否 over budget。
- 如果 provider 返回真实 usage token, 可额外记录真实 token, 但不覆盖本地估算字段。

## 9. 测试计划

新增或更新 context manager 测试:

- `estimate_tokens` 对英文、中文、混合文本、JSON 的估算落在预期区间。
- `history_budget` 会随 `current_request` 和固定 section 实际大小动态变化。
- 低于 `soft_trigger` 时 history 不压缩, 保持 raw append-only。
- 超过 `soft_trigger` 时压到 `compression_target` 附近。
- 超过 `hard_trigger` 时仍保证 prompt 低于 `input_hard_budget`。
- `current_request` 超过 hard max 时不被静默截断。
- metadata 同时保留 token 预算和字符统计, 兼容旧报告阅读。

## 10. 决策记录

| 决策 | 选择 |
| --- | --- |
| 主预算单位 | 估算 token, 不再使用字符作为主判断单位 |
| token 估算 | `estimate_tokens(text)` 加权估算 + `1.15` 安全系数 |
| 默认窗口 | `128_000` token |
| 输出预留 | 默认 `4_096` token, 复杂任务可升到 `8_192` |
| 安全余量 | `2_048` token |
| 阀门 | soft `60%`, hard `90%`, compression target `30%` |
| history 预算 | 动态剩余预算, 不写死 |
| current request | 保留原文; 超 hard max 时显式失败, 不静默裁剪 |
| metadata | token 与字符双轨记录 |
