# 设计 Spec:上下文压缩 V2(history 中间轮蒸馏)

> 日期:2026-06-22
> 状态:✅ 已实现
> 关联:[design.md](design.md) / [context-budget](../context-budget/design.md)

## 1. 概述

本 spec 定义 `context-compression` 的 v2:history 中间轮蒸馏。

v1 已完成机械压缩链路:预算内 history raw append-only;超预算后优先压旧 tool result,
保留头 3 条和尾 27 条,并在 metadata 中标记 `history[3:-27]` 作为 future
distillation candidate。v2 在这个候选区上补上真正的 LLM 蒸馏。

核心目标:
- **释放上下文**:把 `history[3:-27]` 永久替换成单条 distilled marker。
- **保留事实**:把中间区里的已完成工作、失败工作、排除方案写进新 checkpoint。
- **可召回细节**:把更细的原因、文件、命令、测试结果写入 process notes,后续由
  `Relevant memory` 按当前请求召回。
- **失败安全**:蒸馏失败时不修改 history / checkpoint / memory,继续使用 v1 压缩结果。

## 2. 触发与时机

蒸馏不是后台任务,也不是每轮都做。它只在主模型调用前、压缩链路走到中间区时触发。

触发条件:
- 本轮 prompt build 发生 `context_reduction`。
- `history` 条目数 `> 30`。
- metadata 中存在 `distillation_candidate_count > 0`,也就是 `history[3:-27]` 非空。

执行时机:
1. `MiniAgent.ask()` 调用 `_build_prompt_and_metadata(user_message)`。
2. 如果 metadata 满足蒸馏触发条件,在主模型 `complete()` 之前调用 distillation helper。
3. 蒸馏成功后保存 session,并立刻重新 build prompt。
4. 主模型使用重建后的 prompt。

这样当前轮可以马上使用 marker、最新 checkpoint 和 relevant memory,不必等下一轮。

## 3. 蒸馏输入

蒸馏输入使用**压缩后的中间区内容**,不是完整原始中间区。

理由:
- 旧 tool result 已经过 v1 机械压缩,能显著降低额外模型调用成本。
- 蒸馏目标是抽取事实和过程结论,不需要完整 stdout / 文件原文。
- 若需要细节,应写成 process note 供 relevant memory 召回,而不是把大块原文塞回 prompt。

输入范围仍按 `session["history"]` 条目定位:

```python
head = history[0:3]
candidate = history[3:-27]
tail = history[-27:]
```

蒸馏 prompt 应包含:
- 蒸馏任务说明:只抽取中间区事实,不要重新规划当前任务。
- 压缩后的 candidate transcript。
- 严格 JSON 输出 schema。

## 4. 蒸馏输出 Schema

模型必须输出严格 JSON:

```json
{
  "checkpoint_updates": {
    "completed": ["A", "B"],
    "failed": ["C"],
    "excluded": ["D"]
  },
  "process_notes": [
    {
      "text": "pytest failed because tests/test_api.py still expects the old route.",
      "tags": ["process", "pytest", "failure", "tests/test_api.py"]
    }
  ]
}
```

字段语义:
- `completed`:已完成且后续可以依赖的工作。
- `failed`:做过但失败的尝试、失败原因、失败命令或失败测试。
- `excluded`:明确否决、不可行、不要再走的方案。
- `process_notes`:按需召回的细节,每条只包含 `text` 和 `tags`。

非目标:
- 不让蒸馏输出 `current_blocker` / `next_step`。这些属于当前控制状态,应由尾 27 条
  history 和 runtime checkpoint 逻辑决定。
- 不把完整摘要替换进 history。

## 5. 蒸馏模型调用与提示词

蒸馏不使用 `delegate` / 子 agent。现有 delegate 会启动完整只读 child agent,
可能进入工具循环、产生额外 history 和 checkpoint,对于蒸馏过重。蒸馏只是一次
"给定 transcript → 严格 JSON"的结构化转换,应直接调用同一个 `model_client`:

```python
distill_prompt = build_history_distillation_prompt(candidate_transcript)
raw = model_client.complete(distill_prompt, max_new_tokens=800)
data = parse_strict_json(raw)
```

约束:
- 不暴露工具,不进入 agent loop。
- 不记录为普通 assistant/user history。
- 不使用 prompt cache key;这是一次短生命周期结构化调用。
- JSON 解析或字段校验失败时重试一次。
- 第二次仍失败则放弃蒸馏,不修改 session。

提示词模板:

```text
You are a context distillation worker for a coding agent.

This is not the main agent turn. Do not continue the user's task.
Do not propose new next steps. Do not call tools.

Task:
Distill the provided middle history segment into structured facts.
Only extract facts that are explicitly supported by the transcript.

Keep:
- completed work
- failed work or failed attempts
- excluded, impossible, or rejected approaches
- process notes useful for later retrieval

Ignore:
- duplicated tool output
- verbose logs unless they explain a failure or decision
- chit-chat
- content not supported by the transcript

Return ONLY valid JSON with this exact shape:
{
  "checkpoint_updates": {
    "completed": [],
    "failed": [],
    "excluded": []
  },
  "process_notes": [
    {"text": "", "tags": []}
  ]
}

Rules:
- completed/failed/excluded items must be short factual strings.
- process_notes[*].text must be one concise factual sentence.
- process_notes[*].tags must be short lowercase tags.
- If nothing fits a field, return an empty array.
- Do not include markdown fences.
- Do not include commentary.

Middle history segment:
<<<HISTORY
{candidate_transcript}
HISTORY
>>>
```

## 6. 写回策略

蒸馏成功必须按顺序写回,不可颠倒:

1. 新建 checkpoint。
2. 写入 process notes。
3. 用 marker 永久替换 `session["history"][3:-27]`。
4. 保存 session。
5. 重建 prompt。

### Checkpoint

每次蒸馏成功都新建 checkpoint,不覆盖旧 checkpoint。

checkpoint 字段:
- `completed`:来自 `checkpoint_updates.completed`。
- `failed`:来自 `checkpoint_updates.failed`。
- `excluded`:来自 `checkpoint_updates.excluded`。

初版策略:三类数组全部保留,不去重、不限长。若后续 checkpoint 膨胀,再增加预算和限长策略。

兼容要求:
- checkpoint schema 增加 `failed: []`。
- `render_checkpoint_text()` 渲染 `- Failed: ...`。
- 旧 checkpoint 缺少 `failed` 时按空列表处理。

### Process Notes

`process_notes` 写入:

```python
memory.append_note(text, tags=tags, source="context_distillation", kind="process")
```

这些 notes 不每轮全量进入 prompt,而是由 `Relevant memory` 根据当前 user request 召回。

### History Marker

蒸馏成功后,把中间区永久替换为一条 assistant marker:

```text
[distilled-history]
range=history[3:-27], items=42
completed: A | B
failed: C
excluded: D
details: see Relevant memory
```

最终 history 形态:

```python
history = history[0:3] + [marker_item] + history[-27:]
```

marker item 建议结构:

```python
{
  "role": "assistant",
  "content": "[distilled-history]\nrange=history[3:-27], items=42\ncompleted: A | B\nfailed: C\nexcluded: D\ndetails: see Relevant memory",
  "created_at": now(),
  "metadata": {
    "kind": "distilled_history",
    "items": 42
  }
}
```

## 7. 失败降级

蒸馏模型调用:
- 使用同一个 `model_client`。
- `max_new_tokens=800`。
- 严格解析 JSON。
- 解析失败或字段不合格时重试一次。

以下情况视为失败:
- 两次输出都不是合法 JSON。
- JSON 缺少 `checkpoint_updates` 或 `process_notes`。
- `completed` / `failed` / `excluded` 不是数组。
- `process_notes` 不是 `{text, tags}` 数组。
- 模型调用异常。
- checkpoint / memory / history 写回任一步失败。

失败时:
- 不修改 `session["history"]`。
- 不创建 checkpoint。
- 不写 memory note。
- 当前轮继续使用 v1 压缩结果。
- 写 trace event 记录失败原因和 retry count。

## 8. Trace 与 Metadata

蒸馏成功/失败都写 trace event。

成功事件建议:

```json
{
  "event": "context_distillation",
  "status": "success",
  "candidate_count": 42,
  "retry_count": 0,
  "checkpoint_id": "ckpt_ab12cd34",
  "process_note_count": 3,
  "history_replaced": true
}
```

失败事件建议:

```json
{
  "event": "context_distillation",
  "status": "failed",
  "candidate_count": 42,
  "retry_count": 1,
  "reason": "invalid_json",
  "history_replaced": false
}
```

重建后的 prompt metadata 应能反映:
- history 条目数已减少。
- marker 已进入 history。
- latest checkpoint 包含蒸馏三类事实。
- relevant memory 可召回 process notes。

## 9. 测试计划

- **成功路径**:FakeModelClient 返回合法 JSON;断言新 checkpoint 创建、`failed` 渲染、
  process notes 写入、history 中间区替换为 marker,并重建 prompt。
- **蒸馏调用隔离**:断言蒸馏不创建 child agent、不调用工具、不写普通 history,只调用
  `model_client.complete(distill_prompt, 800)`。
- **提示词约束**:断言 distill prompt 包含禁止继续任务、禁止工具调用、严格 JSON schema 和
  candidate transcript。
- **解析失败重试**:第一次返回非法 JSON,第二次合法;断言重试一次且最终成功。
- **双失败降级**:两次非法 JSON;断言 history / checkpoint / memory 不变,当前轮继续
  使用 v1 压缩结果。
- **marker 格式**:断言包含 `range=history[3:-27]`、`items=N`、completed / failed /
  excluded 三类状态和 `details: see Relevant memory`。
- **relevant memory 召回**:蒸馏写入的 process note 能在后续相关请求中出现在
  `Relevant memory`。
- **checkpoint 兼容**:旧 checkpoint 缺少 `failed` 时按空列表处理,不影响 resume 或
  `render_checkpoint_text()`。

## 10. 决策记录

| 决策 | 选择 |
|------|------|
| 文件位置 | 同一特性目录下新增 `design-v2-distillation.md` |
| 触发条件 | context reduction + `history > 30` + candidate 非空 |
| 调用时机 | 主模型调用前,蒸馏成功后立刻重建 prompt |
| 蒸馏输入 | 压缩后的中间区内容 |
| 蒸馏执行 | 不用子 agent;直接无工具调用 `model_client.complete()` |
| 输出格式 | 严格 JSON |
| checkpoint 字段 | `completed` / `failed` / `excluded` |
| checkpoint 写回 | 每次成功都新建 checkpoint |
| process notes | 写入 `kind="process"`,由 relevant memory 召回 |
| history 写回 | 永久替换 `history[3:-27]` 为单条 marker |
| 失败降级 | 不改 session,继续 v1 压缩结果 |
| 重试 | JSON 解析/字段校验失败时重试一次 |
| 蒸馏输出预算 | `max_new_tokens=800` |
