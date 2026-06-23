# MCP 客户端接入 — 完成总结

> 日期:2026-06-17
> 分支:`feat/mcp-client`
> 状态:实现完成,通过全特性终审(READY TO MERGE)
> 关联文档:[设计 Spec](design.md) · [实现计划](plan.md) · [测试报告](test-report.md)

## 做了什么

让 cagent 作为 **MCP 客户端**接入外部 stdio MCP server:启动时按 `.mcp.json`
拉起 server 子进程,把它们暴露的工具命名空间化后并入现有工具注册表,
复用 cagent 既有的 prefix 渲染与 approval 护栏。纯增量功能——无配置时
cagent 行为与改动前完全一致。

## 已锁定的设计决策

| 决策 | 选择 |
|------|------|
| cagent 角色 | MCP 客户端(消费外部 server),不做 server 提供方 |
| 传输方式 | 仅本地 stdio 子进程(v1) |
| JSON-RPC 实现 | 手写极小同步客户端,零新依赖 |
| 配置位置 | 仓库根 `.mcp.json`(`mcpServers` 结构) |
| 工具命名 | `mcp__<server>__<tool>` |
| 风险标记 | 全部 `risky=True`,走 approval |
| 连接时机 | 启动时连接 + 降级容错 |
| 暴露能力 | 仅 tools(不含 resources/prompts) |
| delegate 子 agent | 不注册 MCP 工具(read_only 本就拦截) |

## 新增与改动的文件

| 文件 | 改动 |
|------|------|
| `cagent/mcp.py` | **新增(283 行)**:`McpServerConfig` / `load_mcp_config` / `McpClient`(同步 JSON-RPC + 后台读线程超时)/ `McpManager`(命名空间化、降级容错、调用路由)/ `_schema_to_display` |
| `cagent/runtime.py` | `CAgent.__init__` 接受 `mcp_manager`;`build_tools` 仅在顶层 agent(depth 0)合并 MCP 工具 |
| `cagent/tools.py` | `validate_tool` 增加 `mcp__` 通用必填校验分支 |
| `cagent/cli.py` | `build_agent` 从 `.mcp.json` 构造并启动 manager(失败时关闭防泄漏);`main()` 在 finally 里 `close_all()` |
| `tests/test_mcp.py` | **新增 28 个测试** |
| `tests/fixtures/echo_mcp_server.py` | **新增**:真实 stdio echo MCP server 测试 fixture |
| `.mcp.json.example` | **新增**:配置模板 |

> `CLAUDE.md` 也加了一段 MCP 说明,但因文件内混有用户既有的未提交编辑,**未纳入本特性的提交**,留待用户自行处理。

## 一条调用路径(端到端)

```
模型输出 <tool>{"name":"mcp__echo__echo","args":{"text":"hi"}}</tool>
  → CAgent.parse → run_tool
  → 存在性检查(tools 注册表含 mcp__echo__echo)
  → validate_tool 的 mcp__ 分支(按 inputSchema.required 校验)
  → 重复调用检测
  → approval 闸门(risky=True 必过;read_only / --approval never 时被拦)
  → tool["run"] = partial(McpManager._run, "mcp__echo__echo")
  → McpManager.call_tool 解析出 (server, 原始工具名)
  → McpClient.call_tool → tools/call → content blocks 拼成文本 "echo:hi"
```

## 评审中发现并修掉的真实缺陷

按 subagent 驱动流程,每个 task 都过 spec 合规 + 代码质量两段评审。
评审捕获并修复了以下真实缺陷(均非表面问题):

1. **`args: null` 崩溃** — 配置里 `"args": null` 会让 loader 抛 `TypeError`,违背"绝不因坏配置阻断启动"。改为对 null / 非 list 降级为 `[]`。
2. **union 类型 schema 崩溃** — JSON Schema 允许 `"type": ["string","null"]`(不可哈希),`_schema_to_display` 在 `CAgent.__init__` 阶段抛 `TypeError`,会拖垮整个 agent 构造。改为非 str 类型降级为 `str`。
3. **请求无超时** — 原 `_request` 是裸阻塞 `readline()`,server 挂起会永久阻塞整个 agent。改用后台读线程 + 队列实现跨平台超时,超时抛 `McpError`。
4. **构造失败子进程泄漏** — `start_all()` 后若 `CAgent` 构造抛异常,已启动的 server 子进程会泄漏。`build_agent` 增加 try/except 在失败时 `close_all()`。
5. **Windows 无法解析 npx/uvx** — `MCP_ENV_ALLOWLIST` 缺 `PATHEXT`/`COMSPEC`,Windows 下启动 `npx` 类 server 会失败。已补入允许清单。

## 提交记录(`feat/mcp-client`,共 13 个)

```
b379032 fix(mcp): allowlist PATHEXT/COMSPEC so Windows can resolve npx/uvx
35ae5b9 docs(mcp): add .mcp.json.example config template
4a222fb fix(mcp): close manager if agent construction fails; test resume-path wiring
09026a4 feat(mcp): wire McpManager into build_agent and close on exit
6d5b3e0 test(mcp): e2e run_tool guardrails for MCP tools (approval, read_only, validation)
e49f3a0 feat(mcp): register MCP tools on top-level agent with generic validation
a7092f5 fix(mcp): tolerate union/non-str schema types; harden close_all; add _schema_to_display tests
71b559b feat(mcp): add McpManager with namespacing, schema mapping, degradation
e573560 fix(mcp): enforce per-request timeout via reader thread; harden close; add negative tests
3366287 feat(mcp): add synchronous stdio McpClient + echo test server
5679ebc fix(mcp): harden load_mcp_config against null/non-list args and add branch tests
ec41380 feat(mcp): add McpServerConfig and load_mcp_config
(以及 spec 文档提交 aa1d036)
```

## 安全说明

MCP server 作为独立子进程运行,有各自的文件访问权——这是 MCP 协议固有的。
cagent 能强制的护栏是**"是否调用某工具"的决策**:所有 MCP 工具 `risky=True`,
统一走 approval 闸门,`read_only` 模式下被拦;子进程环境用允许清单隔离,
不透传无关 secret。delegate 子 agent(depth≥1)不注册 MCP 工具。

## 后续事项

1. **`CLAUDE.md` 待用户提交**(混有用户既有编辑,本特性未代为提交)。
2. 分支 `feat/mcp-client` 尚未合并到 `master`,可走 finishing-a-development-branch 收尾。
3. 已知非目标(未来可扩展):远程 HTTP/SSE 传输、MCP resources/prompts/sampling 能力。

