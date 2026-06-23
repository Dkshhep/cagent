# 设计 Spec:cagent MCP 客户端接入

> 日期:2026-06-17
> 状态:已实现(见 [summary.md](summary.md) / [test-report.md](test-report.md))
> 遵循:[working-norms.md](../../conventions/working-norms.md)(Spec 先行 + 测试随产出积累)

## 1. 概述

让 cagent 作为 **MCP 客户端**接入外部 MCP server:启动时按 `.mcp.json`
拉起本地 MCP server 子进程,把它们暴露的工具**命名空间化后并入现有工具
注册表**,完全复用 cagent 既有的 prefix 渲染与 approval 护栏。

cagent 是消费方(client),不是 server 提供方。这是纯增量功能:无配置时
cagent 行为与现状完全一致。

## 2. 目标与非目标

### 目标
- cagent 能连接配置好的本地 MCP server,发现其工具并暴露给模型。
- MCP 工具复用现有 `run_tool` 护栏(校验 / 重复检测 / approval / 裁剪)。
- 单个 server 故障不影响 cagent 启动与其余 server。

### 非目标(YAGNI,本期不做)
- 远程 HTTP / SSE 传输(仅 stdio)。
- MCP 的 resources、prompts、sampling 能力(仅 tools)。
- 引入官方 `mcp` SDK(手写极小同步客户端)。
- delegate 子 agent 使用 MCP 工具。

## 3. 配置文件 `.mcp.json`(仓库根)

采用跨工具标准的 `mcpServers` 结构(与 Claude Code / Cursor 一致):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": {}
    }
  }
}
```

- 位置:`<repo_root>/.mcp.json`。
- 文件不存在 / 为空 → 无 MCP 工具,cagent 正常运行。
- 每条 server:`command`(必填)+ `args`(可选,默认 `[]`)+ `env`(可选,默认 `{}`)。
- JSON 损坏 → 打印警告并视为无 MCP 配置,**不中断** cagent 启动。

## 4. 数据流(启动 → 调用)

```
build_agent()
  └─ load_mcp_config(.mcp.json) → [McpServerConfig...]
  └─ McpManager(configs).start_all()
        └─ 每个 McpClient: 启动子进程 → initialize → tools/list
              成功 → 收集工具;失败 → 警告并跳过(降级容错)
  └─ 注入 CAgent(mcp_manager=...)

CAgent.build_tools()
  └─ 内置工具 + mcp_manager.tool_specs()(命名空间化)

模型输出 <tool>{"name":"mcp__filesystem__read_file",...}</tool>
  └─ run_tool 护栏流水线(校验→重复→approval→执行)
        └─ runner → McpManager.call_tool(namespaced_name, args)
              └─ 解析出 server + 原始工具名 → McpClient.call_tool(原始名, args)
                    → content blocks 拼成文本
```

## 5. 新模块 `cagent/mcp.py`

| 单元 | 职责 |
|------|------|
| `McpServerConfig` | dataclass:`name` / `command` / `args` / `env`。从 `.mcp.json` 单条解析 |
| `load_mcp_config(path)` | 读取并校验 `.mcp.json`,返回 `[McpServerConfig]`;缺失 / 损坏 → 空列表(+警告) |
| `McpClient` | 管理**一个** stdio 子进程:`start()` / `initialize()` / `list_tools()` / `call_tool(name, args)` / `close()`。手写按行 JSON-RPC,**全同步**,每次请求带超时 |
| `McpManager` | 加载并 `start_all()`;聚合工具;命名空间化;`tool_specs()` 产出注册表条目;`call_tool()` 路由;`close_all()` 退出清理 |
| `McpError` | MCP 相关异常基类 |

### McpClient 协议细节(JSON-RPC over stdio)
- 子进程:`subprocess.Popen([command, *args], stdin=PIPE, stdout=PIPE, env=...)`。
- 消息按行(`\n` 分隔的 JSON)读写;每条请求自增 `id`。
- 必需方法:`initialize`(握手 + `notifications/initialized`)、`tools/list`、`tools/call`。
- 超时:`initialize` 与 `call_tool` 各有独立超时;超时抛 `McpError`。

### 子进程环境
复用 cagent 现有 env 允许清单(`shell_env` 思路,保证能找到 `npx`/`uvx`/`PATH`),
再叠加配置中声明的 `env`。

## 6. 工具命名空间

MCP 工具名加前缀,防与内置工具冲突:**`mcp__<server>__<tool>`**
(如 `mcp__filesystem__read_file`)。`McpManager` 维护
`命名空间名 → (server, 原始工具名, inputSchema)` 的映射。

## 7. 接入工具注册表(改 `tools.py` + `runtime.py`)

- `build_tool_registry(agent)` 装配完内置工具后,若 `agent.mcp_manager` 存在
  **且 `agent.depth == 0`**(仅顶层 agent),合并 `mcp_manager.tool_specs()`。
- 每条 MCP 工具 spec:`{schema, risky: True, description, run}`,`run` 调
  `McpManager.call_tool`。
- **schema 转换**:MCP `inputSchema`(JSON Schema)→ cagent 展示式 `{字段: "类型"}`。
  这样 `build_prefix()` 现有渲染无需改动即可把 MCP 工具列给模型;
  `tool_signature()` 自动纳入,prefix 缓存正确失效。
- **校验**:`validate_tool` 顶部加分支——名字以 `mcp__` 开头者,按其
  `inputSchema.required` 做通用必填校验后 return;现有逐工具 if/elif 不动。

## 8. 调用与结果(复用现有护栏)

- MCP 工具走 `run_tool` **同一条护栏流水线**:存在性 → 校验 → 重复检测 →
  **approval(risky=True 必过)** → 执行 → 裁剪。零改动复用。
- runner 调 `McpManager.call_tool(namespaced_name, args)`,把 MCP 返回的
  content blocks(text 类型)拼成文本返回。
- 调用超时 / server 崩溃 → 返回 `mcp_error: ...` 文本,模型下一轮可消费,
  不崩主循环。

## 9. 风险与审批

**所有 MCP 工具一律 `risky=True`。**
理由:外部代码不可预测,可能写文件 / 调 API / 改数据。统一走 `--approval`
审批闸门;`read_only` 模式(含 delegate 子 agent)下被拦。

## 10. 生命周期与容错(改 `cli.py`)

- `build_agent()` 构造 `McpManager`(从 `repo_root/.mcp.json`),`start_all()`,
  注入 `CAgent`。`CAgent` 持有 `self.mcp_manager`。
- **降级容错**:某 server 起不来 / initialize 超时 → 跳过 + 打印警告,其余照常;
  cagent 不因单个坏 server 无法启动。
- one-shot 结束 / REPL `/exit` 时 `manager.close_all()` 终止所有子进程
  (含异常路径,确保不留僵尸进程)。

## 11. 与 delegate 子 agent 的关系

仅顶层 agent(`depth == 0`)注册 MCP 工具。delegate 子 agent 是 `read_only`,
risky 工具本就被拦,故不重复列入其 prefix——省 token 且语义清晰。

## 12. 测试积累(对应规范 2)

新增 `tests/test_mcp.py`,并扩展现有测试:

- **传输 double**:用极小 Python stdio 回环脚本作 fixture(`sys.executable` 启动),
  真实走一遍 `initialize` / `tools/list` / `tools/call`,不依赖 npx 等外部命令。
- **McpClient**:握手成功;`tools/list` 解析;`tools/call` 返回 content 拼接;
  超时抛 `McpError`;`close()` 终止子进程。
- **McpManager**:加载配置;命名空间注册(`mcp__server__tool`);路由调用;
  **坏 server 降级**(起不来时跳过,其余可用);调用错误返回 `mcp_error`。
- **schema 转换**:JSON Schema → 展示式;`required` 必填校验通过 / 失败路径。
- **load_mcp_config**:正常 / 缺失 / 损坏 JSON(返回空 + 不抛)。
- **集成(扩展 `tests/test_cagent.py` / 安全测试)**:`FakeModelClient` 脚本化一次
  MCP 工具调用,断言 approval 闸门生效、`read_only` 下被拦、结果正确并入历史;
  delegate 子 agent 的 prefix 不含 MCP 工具。

## 13. 决策记录

| 决策 | 选择 |
|------|------|
| cagent 角色 | MCP 客户端(消费外部 server) |
| 传输方式 | 仅本地 stdio 子进程(v1) |
| JSON-RPC 实现 | 手写极小同步客户端,零新依赖 |
| 配置位置 | 仓库根 `.mcp.json`(`mcpServers` 结构) |
| 工具命名 | `mcp__<server>__<tool>` |
| 风险标记 | 全部 `risky=True`,走 approval |
| 连接时机 | 启动时连接 + 降级容错 |
| 暴露能力 | 仅 tools(不含 resources/prompts) |
| delegate 子 agent | 不注册 MCP 工具 |

## 14. 影响的文件

| 文件 | 改动 |
|------|------|
| `cagent/mcp.py` | 新增:配置 / 客户端 / 管理器 |
| `cagent/tools.py` | `build_tool_registry` 合并 MCP 工具;`validate_tool` 加 `mcp__` 通用分支 |
| `cagent/runtime.py` | `CAgent` 持有 `mcp_manager`;`build_tools` 顶层合并 |
| `cagent/cli.py` | `build_agent` 构造 / 启动 / 退出清理 manager |
| `tests/test_mcp.py` | 新增测试 + fixture 回环脚本 |
| `.mcp.json.example` | 新增示例配置(可选) |
| `CLAUDE.md` | 补充 MCP 接入说明 |




