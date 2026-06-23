# 测试报告:上下文压缩 V2(history 中间轮蒸馏)

> 日期:2026-06-22
> 状态:✅ 通过

## 测试命令

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -Command "& 'C:\Users\Dksheep\anaconda3\shell\condabin\conda-hook.ps1'; conda activate cagent; python -m pytest tests/test_context_manager.py tests/test_cagent.py::test_context_distillation_success_replaces_middle_history_and_rebuilds_prompt tests/test_cagent.py::test_context_distillation_retries_invalid_json_once tests/test_cagent.py::test_context_distillation_double_failure_keeps_history_and_uses_v1_prompt tests/test_cagent.py::test_context_distillation_process_note_can_be_recalled_later tests/test_cagent.py::test_resume_prompt_uses_checkpoint_state_not_just_history tests/test_cagent.py::test_agent_creates_checkpoint_when_context_reduction_happens_and_artifacts_only_reference_it -q"
```

结果:

```text
20 passed in 5.57s
```

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -Command "& 'C:\Users\Dksheep\anaconda3\shell\condabin\conda-hook.ps1'; conda activate cagent; python -m pytest tests/test_cagent.py -q -k 'checkpoint or prompt_cache or context_reduction or context_distillation'"
```

结果:

```text
13 passed, 53 deselected in 5.26s
```

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -Command "& 'C:\Users\Dksheep\anaconda3\shell\condabin\conda-hook.ps1'; conda activate cagent; python -m pytest tests/test_context_manager.py tests/test_cagent.py -q -k 'context or compression or reduction or distillation'"
```

结果:

```text
19 passed, 61 deselected in 5.20s
```

## 覆盖场景

- v1 history/tool 压缩链路仍通过 `tests/test_context_manager.py`。
- 压缩整体筛选覆盖 v1 context manager、context reduction 和 v2 context distillation 的组合链路。
- 蒸馏成功路径:
  - FakeModelClient 返回合法 JSON。
  - 新建 context distillation checkpoint。
  - checkpoint 写入 `completed` / `failed` / `excluded`。
  - history 中间区替换为 `[distilled-history]` marker。
  - process note 写入 memory,并在重建 prompt 中通过 `Relevant memory` 召回。
- JSON 解析失败重试:
  - 第一次非法 JSON,第二次合法 JSON。
  - trace 记录 `retry_count == 1`。
- 双失败降级:
  - 两次非法 JSON 后不替换 history。
  - 不写 checkpoint/memory 蒸馏结果。
  - 当前轮继续使用 v1 压缩 prompt。
- checkpoint 兼容:
  - 旧 checkpoint 无 `failed` 字段时仍可 resume/render。
  - `Task checkpoint` 支持渲染 `Failed`。
- prompt/cache/checkpoint 相关回归:
  - checkpoint 创建、context reduction、prompt cache 相关用例通过。

## 真实模型压缩实验

本轮额外跑了一次非 mock 的真实模型实验。实验构造 36 条 history,其中包含长 tool
输出、重复 tool 输出、已完成工作、失败测试和排除方案;同时把 context budget 调小到
小预算,强制触发 history 压缩和 `history[3:-27]` 蒸馏候选区。

实验配置:

```text
provider: openai-compatible
model: gpt-5.4
base_url: https://www.right.codes/codex/v1
history budget: 260 token
total budget: 1400 token
```

关键结果:

```text
pre_prompt_tokens: 283
pre_history_count: 36
pre_distillation_candidate: history[3:9], count=6
answer: real compression experiment complete
post_history_count: 32
marker_count: 1
distill_checkpoint_count: 1
distill_trace_status: success
distill_retry_count: 0
distill_process_note_count: 2
```

真实蒸馏输出写回:

```text
checkpoint_id: ckpt_61d3d0fd
completed: Edited cagent/runtime.py to add a context distillation helper and a trace event.
failed: pytest tests/test_api.py failed because the test expected route /old but the implementation returned /new. |
        An initial pytest-related marker format missed a details line.
excluded: Implementing distillation as a subagent was rejected because it may run tools.
```

实验结论:

- 真实模型调用能按蒸馏 prompt 返回合法 JSON。
- 小预算下 v1 history/tool 压缩正常触发。
- v2 蒸馏成功写入 checkpoint、process notes 和 `[distilled-history]` marker。
- 蒸馏成功后主 prompt 立刻重建,主模型返回 `<final>`。
- 观察到一个后续可讨论边界:蒸馏后 history 仍可能超过 30 条,metadata 可能再次出现很小的
  distillation candidate;当前轮不会重复蒸馏,后续可考虑对已有 distilled marker 加跳过保护。

## 未覆盖/后续

- 未跑全量 `tests -q`。
- 已接一次真实 provider 验证 JSON 路径;仍未做多 provider / 多轮稳定性验证。
- `completed` / `failed` / `excluded` 暂未做预算限制;如 checkpoint 膨胀,后续补限长策略。
