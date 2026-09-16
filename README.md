# CAgent

CAgent 是一个面向本地代码仓库的轻量级 Coding Agent Harness。它运行在终端中，能够读取当前工作区、调用受约束的本地工具、维护会话状态，并用最小 recovery checkpoint 保护未确认的副作用操作。

这个项目的重点不是做一个聊天窗口，而是实现一个完整的本地代码 agent runtime：统一模型接入、上下文管理、工具执行、审批控制、用户授权的长期记忆、副作用恢复和运行工件管理。

## 项目亮点

- **Agent Harness Runtime**：串联用户请求、prompt 组装、模型调用、工具执行、history 持久化和 report 落盘的完整控制循环。
- **多模型后端接入**：支持 Ollama、OpenAI-compatible Responses API、Anthropic-compatible Messages API，以及 DeepSeek 的 Anthropic-compatible endpoint。
- **受约束工具系统**：内置 7 类工具能力，包括 `list_files`、`read_file`、`search`、`run_shell`、`write_file`、`patch_file` 和只读 `delegate`。
- **上下文与记忆管理**：在 token 预算内组合 workspace prefix、历史对话、当前有效的用户偏好卡片、临时 recovery notice 和当前请求；压缩历史以自包含的 `distilled-history` marker 留在 history 中。
- **Recovery Checkpoint**：仅在 `write_file`、`patch_file` 或 `run_shell` 已进入执行但结果尚未可靠写入 history 时存在；恢复后强制先做只读调查，禁止盲目重放副作用。
- **可复盘运行工件**：每次运行都会在 `.cagent/runs/` 下写出 `task_state.json`、`trace.jsonl` 和 `report.json`。
- **MCP 扩展能力**：支持从 `.mcp.json` 加载外部 MCP server，并把 MCP 工具暴露给顶层 agent。

## 架构概览

CAgent 的核心模块如下：

- `cagent.runtime.CAgent`：主控制循环，负责组装 prompt、解析模型输出、校验并执行工具、记录 history、审查记忆变更、管理 recovery guard、写入 trace/report。
- `cagent.recovery.RecoveryCheckpointStore`：管理 session 级 active recovery checkpoint，并比较文件工具执行前后的目标状态。
- `cagent.storage.write_json_atomic`：为 session、recovery、task state 和 report 提供 fsync + replace 的原子 JSON 写入。
- `cagent.models`：模型适配层，OpenAI-compatible、Anthropic-compatible 和 DeepSeek-compatible 后端。
- `cagent.context_manager.ContextManager`：上下文组装与预算控制，负责按 section 渲染 prompt 并在超预算时压缩低优先级内容。
- `cagent.memory_cards` / `memory_store` / `memory_decider`：分别负责记忆卡片校验、原子持久化与独立 LLM 提案。仅保存用户长期偏好或明确要求记住的内容。
- `cagent.tools`：本地工具注册、参数校验和执行逻辑。
- `cagent.run_store.RunStore`：按 run 写入 `task_state.json`、`trace.jsonl` 和 `report.json`。
- `cagent.mcp.McpManager`：启动并注册 `.mcp.json` 中配置的 MCP 工具。

一次请求的主流程：

```text
用户请求
  -> 写入 session/history
  -> 按上下文预算组装 prompt
  -> 调用模型
  -> 解析 final answer 或 tool call
  -> 校验工具参数与审批策略
  -> 副作用工具执行前原子写 recovery checkpoint
  -> 执行工具并将结果原子写入 session/history
  -> 清理 recovery checkpoint，更新 task state / trace
  -> 循环直到 final answer 或命中停止条件
  -> 在 final 写入 history 前独立审查并提交用户授权的记忆变更
```

## 安装

需要 Python 3.10+。

或者在已有 Python 环境中以可编辑模式安装：

```bash
pip install -e .
```

## 快速开始

在当前仓库启动交互模式：

```bash
python -m cagent --provider openai
```

指定工作目录：

```bash
python -m cagent --cwd /path/to/repo --provider openai
```

执行一次性任务：

```bash
python -m cagent --provider openai "inspect the failing tests and propose a fix"
```

恢复最近一次 session：

```bash
python -m cagent --resume latest
```

如果已经安装到当前环境，也可以直接运行：

```bash
python -m cagent --provider openai
```

## 模型配置

CAgent 启动时会从项目目录向上查找并加载 `.env`。配置优先级为：

```text
显式 CLI 参数 > PICO_* 环境变量 > 兼容旧环境变量 > 代码默认值
```

复制示例配置：

```bash
cp .env.example .env
```

只需要填写实际使用的 provider。真实 API key 应保留在本地 `.env` 中，不要提交到仓库。

### OpenAI-Compatible

```bash
PICO_OPENAI_API_BASE=https://your-api.example/v1
PICO_OPENAI_API_KEY=your-api-key
PICO_OPENAI_MODEL=gpt-5.4
```

```bash
python -m cagent --provider openai
```

### Anthropic-Compatible

```bash
PICO_ANTHROPIC_API_BASE=https://www.right.codes/claude/v1
PICO_ANTHROPIC_API_KEY=your-api-key
PICO_ANTHROPIC_MODEL=claude-sonnet-4-6
```

```bash
python -m cagent --provider anthropic
```

### DeepSeek

```bash
PICO_DEEPSEEK_API_BASE=https://api.deepseek.com/anthropic
PICO_DEEPSEEK_API_KEY=your-api-key
PICO_DEEPSEEK_MODEL=deepseek-v4-pro
```

```bash
python -m cagent --provider deepseek
```

## 常用 CLI 参数

```bash
python -m cagent [prompt] \
  --cwd . \
  --provider openai \
  --model gpt-5.4 \
  --base-url https://your-api.example/v1 \
  --approval ask \
  --resume latest \
  --max-steps 6 \
  --max-new-tokens 4096
```

审批策略：

- `ask`：执行高风险工具前询问。
- `auto`：自动允许高风险工具。
- `never`：拒绝高风险工具。

交互模式内置命令：

- `/help`：查看帮助。
- `/memory` 或 `/memory list`：列出当前有效的记忆卡片。
- `/memory show <id>`：查看卡片来源与修订历史。
- `/memory forget <id>`：停止使用卡片；`/memory delete <id>`：彻底删除。
- `/memory migrate preview`：预览旧 topic 候选；`/memory migrate import <candidate-id>...`：显式选择导入。
- `/session`：查看当前 session 文件路径。
- `/reset`：只清空当前 session 历史，不删除长期记忆卡片。
- `/exit` 或 `/quit`：退出 REPL。

## 工具系统

内置工具保持显式、可审计和有边界：

| 工具 | 能力 | 风险等级 |
| --- | --- | --- |
| `list_files` | 列出工作区文件 | safe |
| `read_file` | 按行读取 UTF-8 文件 | safe |
| `search` | 使用 `rg` 或 fallback 搜索 | safe |
| `run_shell` | 在仓库根目录执行 shell 命令 | approval required |
| `write_file` | 写入文本文件 | approval required |
| `patch_file` | 精确替换一个文本块 | approval required |
| `delegate` | 启动受限只读子 agent 做调查 | safe |

额外 MCP 工具可以通过 `.mcp.json` 配置。MCP 工具会以 `mcp__...` 的形式注册，并且只暴露给顶层 agent，避免子 agent 重复消耗上下文。

## 状态与运行工件

CAgent 的本地状态默认写在仓库下的 `.cagent/`：

```text
.cagent/
  sessions/
    <session_id>.json
    <session_id>.recovery.json  # 仅在副作用结果未确认时存在
  runs/
    <run_id>/
      task_state.json
      trace.jsonl
      report.json
  memory/
    cards.json                  # 当前项目的记忆卡片
    MEMORY.md, topics/          # 旧记忆只读保留，不自动导入
```

- `<session_id>.json`：保存正常 resume 的事实源，包括会话 history；旧 session memory 字段只作兼容备份。
- `<session_id>.recovery.json`：只标记一个结果尚未确认的副作用工具；正常完成时不存在。
- `task_state.json`：保存当前任务的生命周期状态。
- `trace.jsonl`：逐条记录运行过程事件，适合排查 agent 为什么做了某个动作。
- `report.json`：保存最终运行摘要、当次 recovery 摘要、prompt metadata、工具统计和敏感信息脱敏结果。
- `memory/cards.json`：保存当前项目的用户授权卡片。全局卡片另存于用户主目录的 `.cagent/memory/cards.json`；项目级同 key 卡片在当前项目覆盖全局卡片。
