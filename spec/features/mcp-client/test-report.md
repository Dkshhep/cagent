# MCP 客户端接入 — 测试报告

> 日期:2026-06-17
> 分支:`feat/mcp-client`
> 运行环境:conda 环境 `cagent`(Python 3.12.13,pytest 9.0.3)
> 运行命令:`python -m pytest tests/test_mcp.py -q`
> 关联文档:[完成总结](summary.md) · [设计 Spec](design.md)

## 总览

| 指标 | 结果 |
|------|------|
| MCP 新增测试 | **28 个,全部通过** |
| 全量套件 | 126 passed / 7 failed |
| 7 个失败 | **开工前基线即存在,与 MCP 无关**(见下"基线说明") |
| 测试方式 | 真实子进程 fixture(非 mock),仅 model client 用 `FakeModelClient` |

测试策略遵循 TDD:每个功能单元都"先写失败测试 → 实现 → 转绿 → 提交"。

## 基线说明(为什么有 7 个失败)

开工前先跑了一次全量套件作基线:**7 failed, 98 passed**。这 7 个失败在
任何 MCP 代码写入之前就已存在,与本特性无关:

- `tests/test_evaluator.py` ×3
- `tests/test_cagent.py::test_trace_and_report_redact_secret_env_values`
- `tests/test_cagent.py::test_reviewer_skeleton_docs_exist`
- `tests/test_safety_invariants.py::test_symlink_path_traversal_is_rejected`
- `tests/test_safety_invariants.py::test_run_shell_uses_allowlisted_environment_only`

收工时全量套件为 **126 passed / 7 failed**:失败集合与基线**逐一相同**,
没有新增失败;98 个原本通过的测试仍全部通过(98 + 28 MCP = 126)。
即:MCP 改动没有破坏任何既有行为。

## 28 个 MCP 测试明细

### 1. 配置加载 `load_mcp_config`(6 个)
| 测试 | 验证内容 |
|------|----------|
| `test_load_mcp_config_parses_servers` | 正常解析多个 server(含无 args/env 的精简条目) |
| `test_load_mcp_config_missing_returns_empty` | 文件缺失返回 `[]`,不抛 |
| `test_load_mcp_config_corrupt_returns_empty` | JSON 损坏返回 `[]`,不抛 |
| `test_load_mcp_config_skips_entry_without_command` | 跳过缺 `command` 的条目 |
| `test_load_mcp_config_tolerates_null_args_and_env` | `args`/`env` 为 null 时降级为空(缺陷#1 回归) |
| `test_load_mcp_config_non_dict_top_level_returns_empty` | 顶层非 dict 返回 `[]` |

### 2. 客户端 `McpClient`(4 个,真实子进程)
| 测试 | 验证内容 |
|------|----------|
| `test_mcp_client_handshake_and_list` | `initialize` 握手 + `tools/list` 解析 |
| `test_mcp_client_call_tool_returns_text` | `tools/call` 返回 content 拼接文本 |
| `test_mcp_client_unknown_method_raises` | server 回 JSON-RPC error → 抛 `McpError` |
| `test_mcp_client_times_out_on_silent_server` | 静默 server 在超时后抛 `McpError`,不永久阻塞(缺陷#3 回归,~1s 完成) |

### 3. 管理器 `McpManager`(4 个)
| 测试 | 验证内容 |
|------|----------|
| `test_manager_registers_namespaced_tool_specs` | 工具命名空间化 `mcp__echo__echo`,`risky=True`,schema 正确 |
| `test_manager_routes_call_to_client` | 调用按命名空间路由到正确 client |
| `test_manager_skips_broken_server` | 坏 server 降级跳过,其余可用,不抛(降级容错) |
| `test_manager_call_unknown_returns_error_text` | 未知工具返回 `mcp_error:` 文本 |

### 4. Schema 转换 `_schema_to_display`(5 个)
| 测试 | 验证内容 |
|------|----------|
| `test_schema_to_display_required_and_optional` | 必填裸类型 / 可选加 `?` 后缀 |
| `test_schema_to_display_type_mappings` | 6 种 JSON 类型 → cagent 展示类型映射 |
| `test_schema_to_display_handles_empty_and_missing_type` | None / 空 / 缺 type 的容错 |
| `test_schema_to_display_tolerates_union_type` | union 类型 `["string","null"]` 降级为 str,不崩(缺陷#2 回归) |
| `test_schema_to_display_non_dict_property_spec` | 非 dict 的属性定义容错 |

### 5. 注册表接入(2 个,真实 CAgent)
| 测试 | 验证内容 |
|------|----------|
| `test_top_level_agent_registers_mcp_tools` | 顶层 agent 注册 MCP 工具,且渲染进 prefix 工作手册 |
| `test_delegate_child_has_no_mcp_tools` | depth=1 read_only 子 agent 不含任何 `mcp__` 工具 |

### 6. run_tool 护栏端到端(5 个,真实 CAgent + 真实子进程)
| 测试 | 验证内容 |
|------|----------|
| `test_run_tool_mcp_executes_when_approved` | `--approval auto` 下执行成功,返回 `echo:hi` |
| `test_run_tool_mcp_blocked_when_approval_never` | `--approval never` 下被拦:`error: approval denied` |
| `test_run_tool_mcp_blocked_in_read_only` | `read_only` 下被拦:`error: approval denied` |
| `test_run_tool_mcp_missing_required_arg_is_rejected` | 缺必填参数被校验拦截 |
| `test_run_tool_mcp_unknown_tool_rejected` | 未注册工具名被拒:`error: unknown tool` |

### 7. CLI 装配(2 个,真实 build_agent)
| 测试 | 验证内容 |
|------|----------|
| `test_build_agent_wires_mcp_from_config` | `build_agent` 从 `.mcp.json` 装配 manager,工具注册成功(新建路径) |
| `test_build_agent_wires_mcp_on_resume_path` | `--resume` 恢复路径同样装配 manager(resume 路径) |

## 测试质量要点

- **真实子进程,非 mock**:`McpClient` / `McpManager` / run_tool / build_agent
  相关测试都通过 `tests/fixtures/echo_mcp_server.py`(一个真实的 stdio JSON-RPC
  echo server,经 `sys.executable` 启动)走完整协议握手。超时测试还动态
  生成一个"只睡觉不回复"的 server 来真实触发超时分支。唯一被替换的是
  model client(`FakeModelClient`),这是 cagent 既有的标准测试手段。
- **回归敏感**:若 MCP 工具被误设为 `risky=False`,审批测试会立刻失败;
  若 `validate_tool` 的 `mcp__` 分支被删,缺参测试会失败——测试能真正
  捕获护栏退化。
- **资源清理**:每个测试都在 `finally` 里 `close_all()`,不残留子进程。

## 已知非阻塞观察

- 静默 server 超时测试在 teardown 阶段会产生一条
  `PytestUnhandledThreadExceptionWarning`——这是后台守护读线程在管道关闭后
  命中异常,属**警告而非失败**,不影响正确性。
- resume 恢复旧 session 时,因新增了 MCP 工具,`tool_signature` 变化会让
  改动前创建的 session 被分类为 `workspace-mismatch`——这是设计内的降级
  提示路径,非缺陷。

## 结论

MCP 客户端特性的 28 个测试全部通过,覆盖配置加载、协议握手、超时、降级
容错、命名空间化、schema 转换、注册表接入、审批/只读护栏、CLI 装配
(新建 + 恢复)等全部关键路径。全量套件无新增失败。特性已通过全特性
终审,**READY TO MERGE**。

