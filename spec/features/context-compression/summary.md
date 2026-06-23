# 完成总结:上下文压缩 V2(history 中间轮蒸馏)

> 日期:2026-06-22
> 状态:✅ 已实现
> 对应 spec:[design-v2-distillation.md](design-v2-distillation.md)

## 做了什么

- 实现 history 中间轮蒸馏:当压缩链路产生 `history[3:-27]` candidate 时,在主模型调用前执行一次无工具蒸馏模型调用。
- 蒸馏成功后永久将中间 history 替换为 `[distilled-history]` marker,保留头 3 条和尾 27 条。
- 每次蒸馏成功都会新建 checkpoint,写入 `completed` / `failed` / `excluded`。
- `process_notes` 写入 `memory.append_note(..., kind="process")`,后续由 `Relevant memory` 召回。
- 蒸馏 JSON 解析或字段校验失败时重试一次;两次失败则不修改 history / checkpoint / memory,继续使用 v1 压缩结果。
- checkpoint schema 兼容新增 `failed: []`,并在 `Task checkpoint` 中渲染 `- Failed: ...`。
- 增加 trace event `context_distillation`,记录成功/失败、candidate 数量、retry count、checkpoint id、process note 数和 history 是否替换。

## 改动文件

- `cagent/runtime.py`
  - 新增 `context_distillation` feature flag。
  - 新增蒸馏 prompt 构造、严格 JSON 解析、写回 checkpoint/memory/history、失败回滚和 trace 记录。
  - 在主模型请求前触发蒸馏,成功后立刻重建 prompt。
  - checkpoint 创建和渲染支持 `failed` 字段。
- `tests/test_cagent.py`
  - 增加蒸馏成功、JSON 重试、双失败降级、process note 召回等回归测试。
- `spec/features/context-compression/design-v2-distillation.md`
  - 新增蒸馏 v2 设计 spec。

## 行为边界

- 蒸馏不是子 agent,不会调用工具,不会进入 agent loop。
- 蒸馏调用暂用同一个 `model_client`,固定 `max_new_tokens=800`。
- 蒸馏输入使用压缩后的中间区 transcript。
- `completed` / `failed` / `excluded` 初版全部保留,暂不去重、不限长。
- 如果蒸馏失败,当前轮仍走 v1 压缩结果。
