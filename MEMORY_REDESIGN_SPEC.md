# CAgent 记忆系统替换实施 Spec

- 状态：Implemented（真实模型小样本评测不作为收益结论）
- 日期：2026-09-13
- 范围：替换现有 working / episodic / file summary / durable topic 记忆实现；不修改 history、context distillation 和 recovery checkpoint 的职责。

## 1. 一句话目标

将当前“工具输出自动摘要 + 最终回答标签提升 + 关键词召回”的记忆系统，替换为**仅保存用户长期偏好或用户明确要求记住的内容**的卡片系统。LLM 负责理解近几轮对话并提出结构化变更；Runtime 负责授权、范围、去重、冲突和持久化校验。

记忆不是会话摘要，也不是仓库事实缓存：

| 信息 | 新的事实来源 |
|---|---|
| 本次任务、工具结果、测试输出 | Session history / trace |
| 被压缩的旧会话内容 | `distilled-history` marker |
| 当前代码、依赖版本和文件内容 | 工作区文件，按需重新读取 |
| 未确认副作用 | Recovery checkpoint |
| 用户长期偏好、明确要求记住的事项 | 新记忆卡片 |

## 2. 设计边界

### 2.1 允许保存

1. 用户明确表达长期适用的偏好，例如“以后注释都用中文”“默认用 `uv`”。无需另问一次权限。
2. 用户明确说“记住 / 保存为长期记忆”的具体内容。
3. 用户明确要求记住 Agent 前几轮已经确认的结论；记忆需同时保留用户授权消息和被引用结论的来源。
4. 用户明确要求更改或忘记已有记忆。

### 2.2 不允许自动保存

- 一次性要求，例如“这次用 MySQL”“先用中文解释”；
- `read_file` 前三行、文件摘要、工具输出、测试日志；
- Agent 自己推断但用户未要求保存的仓库事实；
- 从 assistant 最终回答中的固定标签行碰巧提取出的内容；
- 密钥、令牌、密码或明显敏感的凭据；
- 没有可核实来源的“用户偏好”推断。

### 2.3 第一版非目标

- 不使用向量数据库或 embedding；
- 不定时扫描完整历史；
- 不要求用户确认每一次清楚的保存或覆盖；
- 不把旧 Markdown 记忆无条件自动导入新卡片；
- 不让记忆覆盖当前用户要求、工具护栏或仓库当前事实。

## 3. 卡片数据模型

项目级卡片保存在 `.cagent/memory/cards.json`；全局卡片保存在 `Path.home() / ".cagent" / "memory" / "cards.json"`。两处使用同一 schema。项目根路径仅作为匹配范围的元数据，不作为可由模型任意指定的写入路径。

```json
{
  "schema_version": 1,
  "cards": [
    {
      "id": "mem_0e72a9",
      "key": "coding.comment_language",
      "kind": "preference",
      "scope": "project",
      "tags": ["coding", "comments"],
      "current_value": "中文",
      "display_text": "当前项目的代码注释使用中文。",
      "change_note": "",
      "status": "active",
      "source": {
        "authorization_turn_id": "turn_18",
        "content_turn_ids": ["turn_18"],
        "user_quote": "以后这个项目的注释都用中文"
      },
      "created_at": "2026-09-13T10:00:00+08:00",
      "updated_at": "2026-09-13T10:00:00+08:00",
      "revisions": []
    }
  ]
}
```

字段规则：

| 字段 | 含义和生成方 |
|---|---|
| `id`、时间戳 | Runtime 生成，不接受模型提供的值 |
| `key` | 同一偏好维度的稳定键；LLM 提议，Runtime 规范化并查重 |
| `kind` | `preference` 或 `explicit_note`，禁止泛化的自动 `episodic` 类别 |
| `scope` | `project` 或 `global`；LLM 提议，Runtime 根据用户原话和当前工作区校验 |
| `tags` | 用于按任务相关性取用；LLM 提议，Runtime 规范化和限制数量 |
| `current_value` | 当前有效值；更新时覆盖此字段，不将新旧值混在一起 |
| `display_text` | 给主 Agent 使用的简洁当前规则，不包含已废弃值 |
| `change_note` | 面向用户的变更说明，可记录“先前用 PostgreSQL，后来改用 MySQL” |
| `status` | `active` 或 `forgotten`；只有 active 卡片可进入 prompt |
| `source` | 授权和内容来源的对话锚点，Runtime 验证 turn id 存在 |
| `revisions` | 更新前的值、来源和时间；只作审计，不进入日常 prompt |

同一存储范围内，`(scope, key)` 对 active 卡片必须唯一。`change_note` 和 `revisions` 可以保留历史，但模型日常取用只能看到 `current_value` / `display_text`。

数据库变更示例：

```json
{
  "key": "project.database",
  "scope": "project",
  "current_value": "MySQL",
  "display_text": "当前项目使用 MySQL。",
  "change_note": "用户先前提出使用 PostgreSQL，随后明确改为 MySQL。",
  "revisions": [
    {"previous_value": "PostgreSQL", "replaced_by_turn_id": "turn_29"}
  ]
}
```

## 4. 何时进行记忆判断

每次 `ask()` 产生主模型的最终回答候选后、将该回答写入 history 和返回给用户前，最多执行一次独立的 memory review。不是“每隔 N 轮”才处理，以免明确的保存请求延迟生效。

新产生的用户消息和最终回答应有稳定 `turn_id`。现有 history 没有 ID；加载旧 session 时，仅为仍在 history 中的用户/assistant 消息分配 ID 并原子保存。已被 distillation 替换、无法定位原文的旧消息不能伪造为新记忆来源。

Review 输入只包含：

1. 本轮用户消息；
2. 最近 3–5 个用户/assistant 对话轮次（保留稳定 turn id）；
3. 当前项目级和全局 active 卡片的精简内容；
4. 必要时，用户明确引用的上一轮 assistant 结论。

默认不发送完整工具日志、完整 session history、旧的 forgotten/revisions 内容或 secret。若本轮用户要求“记住刚才确认的结论”，但最近对话中找不到可引用的结论，输出 `needs_clarification`，不得编造来源。

Memory review 使用独立的 `MemoryDecider` 接口；生产环境可复用当前 provider 的凭据，但调用、输出预算、trace 与主 Agent 控制循环隔离。测试可注入独立 FakeMemoryDecider，避免额外模型调用消耗原 `FakeModelClient` 的脚本输出。

若本轮没有长期偏好、记住、修改或忘记意图，允许 decider 返回 `no_change`。实现可以添加保守的轻量候选预筛选以减少调用，但不得漏掉“以后都”“默认”“从现在起”这类没有“记住”二字的长期偏好。

## 5. LLM 提案协议

MemoryDecider 只返回严格 JSON，不调用工具、不直接写文件：

```json
{
  "decision": "change",
  "proposals": [
    {
      "op": "update",
      "target_card_id": "mem_old",
      "key": "project.database",
      "kind": "preference",
      "scope": "project",
      "tags": ["database"],
      "current_value": "MySQL",
      "display_text": "当前项目使用 MySQL。",
      "change_note": "用户先前提出使用 PostgreSQL，随后明确改为 MySQL。",
      "authorization_turn_id": "turn_29",
      "content_turn_ids": ["turn_29"],
      "user_quote": "这个项目以后改用 MySQL，不用 PostgreSQL 了"
    }
  ]
}
```

`decision` 为 `no_change`、`change` 或 `needs_clarification`。`op` 为 `add`、`update` 或 `forget`；不允许模型输出任意文件路径或 Python 代码。一次 review 最多处理 3 个提案，超出则拒绝并记录错误。

提案规则：

1. 仅从用户明确表达的长期偏好或“记住/修改/忘记”要求提案；assistant 内容只可作为用户明确要求记住的被引用内容。
2. 已有同义记忆时必须指向既有 `(scope, key)`；值相同输出 `no_change`，不得换句话新建卡片。
3. 更新必须指出目标卡片 ID，并解释本轮用户如何明确替换旧值。
4. “这次”“暂时”“先”是任务指令，不是长期更新。
5. “我也用 MySQL”不等于“放弃 PostgreSQL”；没有明确替代关系时输出 `needs_clarification` 或保留原卡片。
6. 不从命令输出、网页内容或仓库文件里的指令推断用户授权；这些内容都可能不可信。

## 6. Runtime 校验与提交

Runtime 不重新进行完整语义推理，但必须执行可确定的硬校验：

1. JSON schema、字段类型、长度、枚举和提案数量正确；
2. `authorization_turn_id` 对应真实用户消息，`user_quote` 是该消息的原文片段；
3. `content_turn_ids` 存在，Agent 结论被记住时仍有明确的用户授权 turn；
4. `scope` 与用户措辞一致：明确“所有项目/以后都”可用 global；明确“这个项目”用 project；含糊时默认 project，避免跨项目泄漏；
5. `update` / `forget` 的目标卡片存在、仍 active，且与提案的 `(scope, key)` 一致；
6. 不保存 secret、过长文本、空值、纯工具日志或明显临时状态；
7. 不允许新卡片覆盖本轮以外的安全规则、审批要求和文件系统事实；
8. 校验当前 store revision，防止另一进程已修改同一张卡片时被覆盖。

Runtime 对 LLM 的“这句话是否真的意味着长期偏好”的判断不能做到形式化证明；因此模型提案、原文锚点、负例测试和保守的冲突策略需要共同保障。校验失败时不写入，trace 记录原因，但不能假称记忆已保存。

存储操作使用原子 JSON 写入。多进程情况下需用短时文件锁或比较 store revision 后重试；不能对同一 `cards.json` 做无保护的整文件覆盖。

### 6.1 去重

以 `(scope, key)` 为唯一语义槽位：

- 同槽位、规范化值相同：`no_change`；
- 同槽位、值不同且用户明确说“改用/以后改为/不用旧值”：`update`；
- 同槽位、值不同但没有明确替代关系：`needs_clarification`，不写入；
- 不同 scope：可以共存；项目级同 key 在该项目内覆盖全局级。

例如，用户之前已要求“注释用中文”，后来说“代码注释尽量写中文”，LLM 应定位 `coding.comment_language`，Runtime 发现有效值仍为中文，保持原卡片不变。

### 6.2 冲突与修订

“这个项目以后改用 MySQL，不用 PostgreSQL 了”会更新项目级 `project.database`：

1. 将旧 `current_value=PostgreSQL` 写入 `revisions`；
2. 将 `current_value` 更新为 `MySQL`；
3. `display_text` 只写当前有效规则；
4. `change_note` 写“先前 PostgreSQL，随后明确改为 MySQL”；
5. 更新 `source` 和 `updated_at`。

“这次先用 MySQL”只影响当前任务，不修改卡片。“另一个项目用 MySQL”必须写入另一个项目的 scope，不能覆盖当前项目。若用户同时提出相互矛盾的新要求且没有明确取舍，输出 `needs_clarification`。

### 6.3 忘记

用户说“忘记以后都用中文注释”时，匹配相应卡片并将其标记为 `forgotten`。该卡片不再检索或注入 prompt；保留最少的修订审计。若用户明确要求彻底删除内容，提供实际删除路径而不是只做软删除。

## 7. 对主 Agent 的取用

每轮构建主 prompt 时读取 project + global 的 active 卡片。先按 `(scope, key)` 合并：同 key 的 project 卡片只在当前项目覆盖 global 卡片。只把当前有效 `display_text` 注入模型，不注入 `change_note`、`revisions` 或 forgotten 卡片。

取用上下文不能只依赖最新用户消息“继续”；至少合并本轮请求、最近一个非“继续”的用户目标，以及当前任务相关的文件/主题。第一版不依赖英文正则分词：对中文可使用规范化关键词/标签和简单字符片段匹配。卡片规模较小时可直接过滤作用范围后按类别选择，无需向量检索。

Prompt 建议放在 history 之后、recovery notice 和 current request 之前：

```text
Saved user preferences and explicit notes (lower priority than this request):
- [project] 当前项目的代码注释使用中文。
- [project] 当前项目使用 MySQL。
```

设置固定 token 上限并在 prompt metadata 记录 selected/omitted card IDs。不得因为卡片过多而静默把 current request、recovery notice 或最新 history 挤出预算。主 Agent 的本轮明确指令优先于旧记忆；代码和配置文件是当前仓库事实的权威来源；工具护栏与审批规则不受记忆覆盖。

## 8. 用户可见行为

更新 CLI：

- `/memory list`：列出 active 卡片的 ID、scope、key、当前值；
- `/memory show <id>`：查看来源、变更说明与修订；
- `/memory forget <id>`：明确忘记一张卡片；
- `/memory`：等同 `/memory list`，不再显示 task/recent_files/file_summaries 仪表盘；
- `/reset`：仍只清空当前 session，不删除长期卡片；如需清空长期记忆，必须单独明确操作。

最终回答的提交顺序必须是：

```text
主模型产出 final 候选
→ memory review（可将候选回答作为本轮上下文）
→ Runtime 校验并尝试落盘
→ 组合任务回答与准确的记忆结果
→ 将组合后的回答一次性写入 history
→ 写 task state / report 并返回用户
```

主 Agent 不得在持久化成功前声称“我已经记住”；若候选回答错误宣称已保存，Runtime 必须修正该表述。用户明确要求保存时，最终回复追加准确结果，例如“已记住：当前项目注释使用中文”“原有记忆已更新为 MySQL”或“未保存：需要确认是否替换 PostgreSQL”。普通任务发生 `no_change` 时保持安静。

Decider 超时、返回非法 JSON、校验失败或磁盘写入失败时，主任务结果仍可返回；但明确的记忆请求必须告知“未保存”，不得假装成功。错误写入 trace/report，不把原始敏感对话写进错误日志。

## 9. 现有实现替换清单

### 9.1 `cagent/memory.py`

用卡片模型和 `MemoryStore` 替换当前 `LayeredMemory` 的以下运行路径：

- `working.task_summary`、`working.recent_files`；
- `file_summaries`、`summarize_read_result()`、文件 freshness 缓存；
- `episodic_notes`、`append_note()`、旧关键词召回；
- `DurableMemoryStore` 的 topic Markdown 写入、`_subject_key()` 句式覆盖和 topic 级时间戳；
- 兼容字段 `task`、`files`、`notes`、`next_note_index`；
- 每轮渲染的 `Memory:` 仪表盘。

新模块建议拆为：

```text
cagent/memory_cards.py      # Card schema、规范化、范围合并、去重和更新
cagent/memory_store.py      # 项目/全局 cards.json 原子读写与并发控制
cagent/memory_decider.py    # LLM review prompt、JSON 解析和独立调用接口
```

不要求机械保留旧类名或旧数据形状。对于使用旧 `LayeredMemory` API 的测试和 metrics，要改测新行为，而不是仅为了旧测试继续维护已废弃的路径。

### 9.2 `cagent/runtime.py`

删除 `update_memory_after_tool()` 对 `read_file` / `write_file` / `patch_file` 的自动记忆写入。工具结果仍照常进入 history、trace；Recovery checkpoint 的 before-state 仍由 recovery 模块独立采集，不依赖 file summary。

删除旧的：

- `DURABLE_MEMORY_INTENT_PATTERN` 与中文触发正则；
- `DURABLE_MEMORY_LINE_PATTERNS`；
- `extract_durable_promotions()`；
- `promote_durable_memory()`；
- `reject_durable_reason()` 中仅服务于标签行提升的路径；
- 根据最终回答中 `Project convention:`、`Decision:` 等标签自动保存的行为。

新增 `review_memory_after_turn()`，在主模型给出 final 候选后、history 记录最终回答前运行 decider、校验并提交提案。新增稳定 `turn_id` 的创建和旧 history 兼容处理。明确保存请求的结果由 Runtime 附加到最终回复。Review 不得计入主 Agent 的工具步数，不得调用 `run_tool()`，但单独记录模型调用耗时和 token 用量。

### 9.3 `cagent/context_manager.py`

删除旧的 `memory` 与 `relevant_memory` 两个 section，合并为一个小的 `saved_memory` section。移除 file summary 相关的旧历史压缩分支和 `reused_file_summary_count` 指标；history 自身继续负责普通裁剪与 distillation，不从 memory 重建文件读取结果。

新的 section 顺序建议为：

```text
prefix → history → saved_memory → recovery_notice → current_request
```

`saved_memory` 放在易变后缀，不破坏稳定 prefix。即使 memory store 损坏，也只能降级为“不注入记忆并报告错误”，不能丢弃 history 或阻止正常读代码。

### 9.4 `cagent/cli.py`、feature flags、report

- `/memory` 改为卡片列表，新增 show/forget 子命令；
- `/reset` 的帮助文本明确说明长期记忆不会被清除；
- 用 `saved_memory` / `memory_review` 等清晰 flag 替换旧 `memory` / `relevant_memory` 语义；
- report 使用 `memory_review_status`、`memory_changes`、`selected_card_ids` 和失败原因，不再使用 `durable_promotions` / `durable_superseded`；
- README 与架构文档删除“自动工作记忆、文件摘要缓存、topic 召回”描述。

### 9.5 与其他模块的边界

- Context distillation 仍只写 history marker，不写记忆卡片；
- Recovery checkpoint 只处理不确定副作用，不读取或修改记忆；
- Workspace prefix 仍从当前仓库生成，不用记忆卡片替代仓库文件；
- Agent 输出的普通 `final` 文本不再被当作记忆写入协议。

## 10. 已落盘旧记忆的迁移

旧数据包括 session JSON 中的 `memory`，以及 `.cagent/memory/MEMORY.md` 和 `topics/*.md`。新 runtime **不得静默删除或覆盖**这些文件，也不得在 prompt 中继续混用旧记忆。

迁移策略：

1. 新版本首次启动时检测旧数据，标记 `legacy_memory_present`；
2. 不自动把旧 topic 文本变成 active 卡片，因为旧记录缺少可靠的用户授权、scope 和单条来源；
3. 提供 `/memory migrate preview`：将可解析旧条目列为候选，显示原文、建议 key/scope 和可能冲突；
4. 用户一次性选择导入候选后，再写新 `cards.json`；不要求对日后每次清楚的保存重新确认；
5. 保留旧文件作为只读备份，迁移完成后不再由 runtime 读取；
6. 旧 session 仍可加载，旧 `session["memory"]` 仅为兼容字段，不再影响 prompt。

如果旧 topic 和新卡片 `(scope, key)` 冲突，预览必须显示双方当前值，不能自动让旧数据覆盖新卡片。迁移工具不得从旧 `file_summaries` 或 episodic notes 生成长期卡片。

## 11. 测试矩阵

### 11.1 新增与去重

- “以后注释用中文”新增一张 `coding.comment_language` 卡；
- “代码注释还是写中文”不新增第二张，也不产生修订；
- 同一用户 turn 的 review 重试具有幂等性；
- “这次注释用中文”不新增长期卡；
- 一段普通工具输出包含“记住”也不触发记忆。

### 11.2 更新与冲突

- “这个项目改用 MySQL，不用 PostgreSQL”更新原 `project.database` 卡；
- 更新后主 prompt 只出现当前 MySQL 规则，卡片的 `change_note` / `revisions` 保留变更经过；
- “另一个项目用 MySQL”不覆盖当前项目 PostgreSQL；
- “这次先用 MySQL”不更新旧卡；
- “我也用 MySQL”不能仅据此判定旧 PostgreSQL 已废弃；
- 项目级同 key 偏好在当前项目覆盖全局偏好，不影响其他项目。

### 11.3 授权与安全

- 模型提案引用不存在或非用户的 `authorization_turn_id` 时拒绝；
- 用户只说“记住刚才结论”但没有可定位的结论时请求澄清；
- 模型提案包含 secret、空值、过长文本、未知 op/scope 或任意文件路径时拒绝；
- 模型返回非法 JSON、超时或写盘失败时不产生半张卡片，并准确报告“未保存”；
- 两个进程同时更新同一卡片时不得丢失较新写入。

### 11.4 取用与兼容

- 用户只说“继续”时，相关项目偏好仍能通过活动任务上下文取用；
- 中文偏好检索不依赖 `[A-Za-z0-9_]` 分词；
- forgotten 卡片和旧值不进入主 prompt；
- 记忆预算超限时保留 current request 和 recovery notice，metadata 记录被省略的卡片；
- `/memory list/show/forget` 与 `/reset` 行为符合定义；
- 旧 session 和旧 topic 文件不丢失，迁移预览不自动导入 file summaries。

### 11.5 真实任务评测

旧 memory ablation 主要验证预设模型是否在 prompt 中看到特定文本，应降级为合同测试。新增真实模型场景，比较新记忆 on/off：

- 长期偏好遵循率；
- 同义重复卡片率；
- 明确更新后的旧值误用率；
- 临时指令被误存率；
- 每轮额外 memory-review token、耗时和失败率；
- 普通代码任务的最终测试通过率，确保记忆改造没有干扰控制循环。

## 12. 分阶段实施与验收

### 阶段 A：新卡片基础

实现 schema、项目/全局 store、原子写、并发防覆盖、CLI list/show/forget，以及旧数据只读检测。此时尚不删除旧代码，但新卡片不与旧 memory 混合注入 prompt。

### 阶段 B：LLM 提案与提交

实现独立 MemoryDecider、结构化输出校验、去重、明确覆盖、澄清、失败回报和 trace。先以测试注入的 FakeMemoryDecider 验证所有正负例。

### 阶段 C：主 Agent 取用

实现 project/global 合并与 `saved_memory` section；更新 Prompt Cache 与预算测试。确认“继续”场景和当前请求优先级正确。

### 阶段 D：移除旧实现

删除自动 file summary、working/episodic memory、标签行提升、旧 topic 召回、无消费者的压缩分支和对应旧指标。保留旧数据文件可读，但不再参与运行。

### 阶段 E：迁移与评测

提供 `/memory migrate preview` 和显式导入；重写 memory 测试与指标，运行全量测试和真实任务对照。最后更新 README。

完成条件：

1. 新增、同义去重、明确覆盖和忘记均正确；
2. 不明确冲突不覆盖旧记忆；
3. 旧 `file_summary` 与最终回答标签提升不再参与运行；
4. 用户能查看、纠正、忘记所有 active 卡片；
5. 旧落盘记忆未丢失，也未被静默当作新记忆使用；
6. 记忆失败不损坏 session/history，明确保存请求不会被误报成功；
7. 全量测试通过，并有真实任务指标证明新机制的收益和额外成本。
