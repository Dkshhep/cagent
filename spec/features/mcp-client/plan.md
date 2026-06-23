# cagent MCP 客户端接入 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 cagent 作为 MCP 客户端,按 `.mcp.json` 启动本地 MCP server 子进程,把其工具命名空间化并入工具注册表,复用现有 approval 护栏。

**Architecture:** 新增 `cagent/mcp.py`(配置 / 同步 JSON-RPC 客户端 / 管理器);`runtime.py` 让 `CAgent` 持有 `mcp_manager` 并在顶层合并 MCP 工具;`tools.py` 的 `validate_tool` 加 `mcp__` 通用校验;`cli.py` 在 `build_agent` 装配与清理。

**Tech Stack:** Python 3.12(conda 环境 `cagent`),标准库 `subprocess` / `json`(零新依赖),pytest + `FakeModelClient`。

**测试命令:** 运行环境是 conda 的 `cagent`。在 bash 里用绝对路径调用解释器:
`"/c/Users/Dksheep/anaconda3/envs/cagent/python.exe" -m pytest ...`
(下文各 Task 写的 `uv run python` 一律替换为此命令)。基线:改动前 7 个
既有失败(test_evaluator / test_cagent / test_safety_invariants,与 MCP 无关)、
98 通过;MCP 改动不得破坏这 98 个,也不得新增失败。

**遵循:** [working-norms.md](../../conventions/working-norms.md)(Spec 先行 + 测试随产出积累),设计见 [design.md](design.md)。

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `cagent/mcp.py`(新增) | `McpServerConfig` / `load_mcp_config` / `McpClient` / `McpManager` / `McpError` |
| `cagent/tools.py`(改) | `build_tool_registry` 顶层合并 MCP 工具;`validate_tool` 加 `mcp__` 分支 |
| `cagent/runtime.py`(改) | `CAgent.__init__` 接 `mcp_manager`;`build_tools` 合并;`runtime_identity` 含 mcp |
| `cagent/cli.py`(改) | `build_agent` 构造 / 启动 / 注入 / 退出清理 manager |
| `tests/fixtures/echo_mcp_server.py`(新增) | 极小 stdio JSON-RPC 回环 server,供测试 |
| `tests/test_mcp.py`(新增) | mcp 模块单元 + 集成测试 |
| `.mcp.json.example`(新增) | 示例配置 |

---

## Task 1: 配置加载 `McpServerConfig` + `load_mcp_config`

**Files:**
- Create: `cagent/mcp.py`
- Test: `tests/test_mcp.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mcp.py
import json
from pathlib import Path

from cagent.mcp import McpServerConfig, load_mcp_config


def test_load_mcp_config_parses_servers(tmp_path):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "srv"], "env": {"A": "1"}},
            "bare": {"command": "uvx"},
        }
    }), encoding="utf-8")
    configs = load_mcp_config(cfg)
    by_name = {c.name: c for c in configs}
    assert by_name["fs"] == McpServerConfig(name="fs", command="npx", args=["-y", "srv"], env={"A": "1"})
    assert by_name["bare"] == McpServerConfig(name="bare", command="uvx", args=[], env={})


def test_load_mcp_config_missing_returns_empty(tmp_path):
    assert load_mcp_config(tmp_path / ".mcp.json") == []


def test_load_mcp_config_corrupt_returns_empty(tmp_path):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text("{not json", encoding="utf-8")
    assert load_mcp_config(cfg) == []
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run python -m pytest tests/test_mcp.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'cagent.mcp'`

- [ ] **Step 3: 写最小实现**

```python
# cagent/mcp.py
"""cagent 作为 MCP 客户端接入外部 stdio server 的实现。

为什么存在:cagent 本身只有内置工具白名单;这个模块让 cagent 能在启动时
按 .mcp.json 拉起外部 MCP server 子进程,把它们的工具命名空间化后并入
工具注册表,从而在不改动控制循环的前提下扩展能力。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


class McpError(Exception):
    """MCP 相关错误的基类。"""


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)


def load_mcp_config(path):
    # 配置缺失或损坏都不应阻断 cagent 启动:返回空列表即“没有 MCP 工具”。
    path = Path(path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        print(f"[mcp] warning: cannot parse {path}, ignoring MCP config")
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    configs = []
    for name, spec in servers.items():
        if not isinstance(spec, dict) or not spec.get("command"):
            continue
        configs.append(
            McpServerConfig(
                name=str(name),
                command=str(spec["command"]),
                args=[str(a) for a in spec.get("args", [])],
                env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
            )
        )
    return configs
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run python -m pytest tests/test_mcp.py -q`
Expected: PASS(3 passed)

- [ ] **Step 5: 提交**

```bash
git add cagent/mcp.py tests/test_mcp.py
git commit -m "feat(mcp): add McpServerConfig and load_mcp_config"
```

---

## Task 2: 测试用回环 server + `McpClient`

**Files:**
- Create: `tests/fixtures/echo_mcp_server.py`
- Modify: `cagent/mcp.py`(追加 `McpClient`)
- Test: `tests/test_mcp.py`(追加)

- [ ] **Step 1: 写回环 server fixture**

这是一个真实的 stdio JSON-RPC server,实现 `initialize` / `tools/list` /
`tools/call`,供测试真实走一遍协议,不依赖 npx。

```python
# tests/fixtures/echo_mcp_server.py
"""极小 MCP stdio server,仅供测试。按行读 JSON-RPC 请求,按行回 JSON。"""
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the text argument.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "echo"}}
        elif method == "notifications/initialized":
            continue  # 通知无需回复
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            text = req.get("params", {}).get("arguments", {}).get("text", "")
            result = {"content": [{"type": "text", "text": f"echo:{text}"}]}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "method not found"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 写失败测试**

```python
# tests/test_mcp.py (追加)
import sys
from cagent.mcp import McpClient, McpServerConfig

FIXTURE = str(Path(__file__).parent / "fixtures" / "echo_mcp_server.py")


def _echo_config():
    return McpServerConfig(name="echo", command=sys.executable, args=[FIXTURE])


def test_mcp_client_handshake_and_list():
    client = McpClient(_echo_config())
    try:
        client.start()
        client.initialize()
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["echo"]
    finally:
        client.close()


def test_mcp_client_call_tool_returns_text():
    client = McpClient(_echo_config())
    try:
        client.start()
        client.initialize()
        client.list_tools()
        out = client.call_tool("echo", {"text": "hi"})
        assert out == "echo:hi"
    finally:
        client.close()
```

- [ ] **Step 3: 运行,确认失败**

Run: `uv run python -m pytest tests/test_mcp.py -k client -q`
Expected: FAIL,`ImportError: cannot import name 'McpClient'`

- [ ] **Step 4: 实现 `McpClient`(追加到 `cagent/mcp.py`)**

```python
# cagent/mcp.py 追加
import os
import subprocess

DEFAULT_TIMEOUT = 30
# 与 runtime 的 shell_env 同思路:只透传必要的环境变量,避免把无关 secret
# 带进子进程;再叠加每个 server 配置里声明的 env。
MCP_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME", "APPDATA", "SystemRoot")


def build_subprocess_env(config):
    env = {name: os.environ[name] for name in MCP_ENV_ALLOWLIST if name in os.environ}
    env.update(config.env or {})
    return env


class McpClient:
    """管理单个 stdio MCP server 子进程,手写同步 JSON-RPC。"""

    def __init__(self, config, timeout=DEFAULT_TIMEOUT):
        self.config = config
        self.timeout = timeout
        self.process = None
        self._next_id = 0
        self.tools = []

    def start(self):
        self.process = subprocess.Popen(
            [self.config.command, *self.config.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=build_subprocess_env(self.config),
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def _request(self, method, params=None):
        self._next_id += 1
        rid = self._next_id
        line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        # 读到第一条带匹配 id 的响应为止(跳过中途的通知/无 id 行)。
        while True:
            raw = self.process.stdout.readline()
            if raw == "":
                raise McpError(f"server '{self.config.name}' closed during {method}")
            raw = raw.strip()
            if not raw:
                continue
            msg = json.loads(raw)
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                raise McpError(f"{method} failed: {msg['error']}")
            return msg.get("result", {})

    def _notify(self, method):
        line = json.dumps({"jsonrpc": "2.0", "method": method})
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def initialize(self):
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "cagent", "version": "0"},
        })
        self._notify("notifications/initialized")
        return result

    def list_tools(self):
        self.tools = self._request("tools/list").get("tools", [])
        return self.tools

    def call_tool(self, name, args):
        result = self._request("tools/call", {"name": name, "arguments": args or {}})
        blocks = result.get("content", [])
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts)

    def close(self):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()
        self.process = None
```

- [ ] **Step 5: 运行,确认通过**

Run: `uv run python -m pytest tests/test_mcp.py -k client -q`
Expected: PASS(2 passed)

- [ ] **Step 6: 提交**

```bash
git add cagent/mcp.py tests/test_mcp.py tests/fixtures/echo_mcp_server.py
git commit -m "feat(mcp): add synchronous stdio McpClient + echo test server"
```

---

## Task 3: `McpManager`(命名空间 / schema 转换 / 降级 / 路由)

**Files:**
- Modify: `cagent/mcp.py`(追加 `McpManager` + `_schema_to_display`)
- Test: `tests/test_mcp.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mcp.py (追加)
from cagent.mcp import McpManager


def test_manager_registers_namespaced_tool_specs():
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        specs = mgr.tool_specs()
        assert "mcp__echo__echo" in specs
        spec = specs["mcp__echo__echo"]
        assert spec["risky"] is True
        assert spec["schema"] == {"text": "str"}
    finally:
        mgr.close_all()


def test_manager_routes_call_to_client():
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        out = mgr.call_tool("mcp__echo__echo", {"text": "yo"})
        assert out == "echo:yo"
    finally:
        mgr.close_all()


def test_manager_skips_broken_server():
    bad = McpServerConfig(name="bad", command="this_command_does_not_exist_xyz")
    mgr = McpManager([bad, _echo_config()])
    mgr.start_all()  # 不应抛异常
    try:
        specs = mgr.tool_specs()
        assert "mcp__echo__echo" in specs
        assert not any(k.startswith("mcp__bad__") for k in specs)
    finally:
        mgr.close_all()


def test_manager_call_unknown_returns_error_text():
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        out = mgr.call_tool("mcp__echo__missing", {})
        assert out.startswith("mcp_error:")
    finally:
        mgr.close_all()
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run python -m pytest tests/test_mcp.py -k manager -q`
Expected: FAIL,`ImportError: cannot import name 'McpManager'`

- [ ] **Step 3: 实现 `McpManager`(追加到 `cagent/mcp.py`)**

```python
# cagent/mcp.py 追加
from functools import partial

_JSON_TYPE_TO_DISPLAY = {
    "string": "str", "integer": "int", "number": "float",
    "boolean": "bool", "array": "list", "object": "dict",
}


def _schema_to_display(input_schema):
    # 把 MCP 的 JSON Schema 压成 cagent 展示式 {字段: "类型"},供 build_prefix 渲染。
    props = (input_schema or {}).get("properties", {}) or {}
    required = set((input_schema or {}).get("required", []) or [])
    display = {}
    for field_name, spec in props.items():
        json_type = spec.get("type", "string") if isinstance(spec, dict) else "string"
        type_name = _JSON_TYPE_TO_DISPLAY.get(json_type, "str")
        display[field_name] = type_name if field_name in required else f"{type_name}?"
    return display


class McpManager:
    """加载配置、启动所有 server、聚合工具、路由调用、退出清理。"""

    def __init__(self, configs):
        self.configs = list(configs)
        self.clients = {}                 # server_name -> McpClient
        self.routes = {}                  # namespaced -> (server_name, raw_tool, input_schema)

    @classmethod
    def from_path(cls, path):
        return cls(load_mcp_config(path))

    def start_all(self):
        for config in self.configs:
            client = McpClient(config)
            try:
                client.start()
                client.initialize()
                tools = client.list_tools()
            except (McpError, OSError, ValueError) as exc:
                print(f"[mcp] warning: server '{config.name}' unavailable: {exc}")
                client.close()
                continue
            self.clients[config.name] = client
            for tool in tools:
                namespaced = f"mcp__{config.name}__{tool['name']}"
                self.routes[namespaced] = (config.name, tool["name"], tool.get("inputSchema", {}))

    def tool_specs(self):
        specs = {}
        for namespaced, (server, raw, schema) in self.routes.items():
            specs[namespaced] = {
                "schema": _schema_to_display(schema),
                "risky": True,
                "description": f"[mcp:{server}] {raw}",
                "run": partial(self._run, namespaced),
                "mcp_input_schema": schema,
            }
        return specs

    def _run(self, namespaced, args):
        return self.call_tool(namespaced, args)

    def call_tool(self, namespaced, args):
        route = self.routes.get(namespaced)
        if route is None:
            return f"mcp_error: unknown mcp tool '{namespaced}'"
        server, raw, _ = route
        client = self.clients.get(server)
        if client is None:
            return f"mcp_error: server '{server}' not available"
        try:
            return client.call_tool(raw, args or {})
        except (McpError, OSError, ValueError) as exc:
            return f"mcp_error: {exc}"

    def close_all(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run python -m pytest tests/test_mcp.py -k manager -q`
Expected: PASS(4 passed)

- [ ] **Step 5: 提交**

```bash
git add cagent/mcp.py tests/test_mcp.py
git commit -m "feat(mcp): add McpManager with namespacing, schema mapping, degradation"
```

---

## Task 4: 接入注册表与校验(`runtime.py` + `tools.py`)

**Files:**
- Modify: `cagent/runtime.py`(`__init__` 加 `mcp_manager=None`;`build_tools` 合并)
- Modify: `cagent/tools.py`(`validate_tool` 顶部加 `mcp__` 分支)
- Test: `tests/test_mcp.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mcp.py (追加)
from cagent.runtime import CAgent, SessionStore
from cagent.models import FakeModelClient
from cagent.workspace import WorkspaceContext


def _agent_with_mcp(tmp_path, outputs, **kwargs):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(str(tmp_path / ".cagent" / "sessions"))
    return CAgent(
        model_client=FakeModelClient(outputs),
        workspace=ws,
        session_store=store,
        **kwargs,
    )


def test_top_level_agent_registers_mcp_tools(tmp_path):
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        agent = _agent_with_mcp(tmp_path, ["<final>ok</final>"], mcp_manager=mgr, approval_policy="auto")
        assert "mcp__echo__echo" in agent.tools
        assert "mcp__echo__echo" in agent.prefix  # 渲染进了工作手册
    finally:
        mgr.close_all()


def test_delegate_child_has_no_mcp_tools(tmp_path):
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        child = _agent_with_mcp(tmp_path, ["<final>x</final>"], mcp_manager=mgr, depth=1, read_only=True)
        assert not any(k.startswith("mcp__") for k in child.tools)
    finally:
        mgr.close_all()
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run python -m pytest tests/test_mcp.py -k "register or delegate_child" -q`
Expected: FAIL,`TypeError: __init__() got an unexpected keyword argument 'mcp_manager'`

- [ ] **Step 3a: `runtime.py` — `__init__` 增加参数**

在 `cagent/runtime.py:103` 的 `feature_flags=None,` 之后、参数列表末尾加一行
`mcp_manager=None,`,并在 `self.feature_flags` 赋值块之后(约 `runtime.py:119` 后)加:

```python
        self.mcp_manager = mcp_manager
```

注意:这行必须在 `self.tools = self.build_tools()`(`runtime.py:134`)**之前**,
因为 `build_tools` 会读取 `self.mcp_manager`。放在 `self.feature_flags` 块之后即可。

- [ ] **Step 3b: `runtime.py` — `build_tools` 合并 MCP 工具**

把 `cagent/runtime.py:323-324` 的 `build_tools` 改为:

```python
    def build_tools(self):
        tools = toolkit.build_tool_registry(self)
        # 仅顶层 agent 暴露 MCP 工具:子 agent 是 read_only,risky 工具本就被拦,
        # 不重复列入其 prefix,省 token。
        if self.depth == 0 and getattr(self, "mcp_manager", None) is not None:
            tools.update(self.mcp_manager.tool_specs())
        return tools
```

- [ ] **Step 3c: `tools.py` — `validate_tool` 加 `mcp__` 通用分支**

在 `cagent/tools.py` 的 `validate_tool` 函数体开头(`args = args or {}` 之后、
`if name == "list_files":` 之前)插入:

```python
    if name.startswith("mcp__"):
        # MCP 工具没有硬编码 schema,按其 inputSchema.required 做通用必填校验。
        tool = agent.tools.get(name, {})
        required = (tool.get("mcp_input_schema") or {}).get("required", []) or []
        missing = [key for key in required if key not in args]
        if missing:
            raise ValueError(f"missing required args: {', '.join(missing)}")
        return
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run python -m pytest tests/test_mcp.py -k "register or delegate_child" -q`
Expected: PASS(2 passed)

- [ ] **Step 5: 全量回归**

Run: `uv run python -m pytest -q`
Expected: PASS(原有测试不受影响)

- [ ] **Step 6: 提交**

```bash
git add cagent/runtime.py cagent/tools.py tests/test_mcp.py
git commit -m "feat(mcp): register MCP tools on top-level agent with generic validation"
```

---

## Task 5: 端到端经 `run_tool`(approval 闸门 + read_only 拦截)

验证 MCP 工具复用现有护栏:risky=True 时审批生效,read_only 时被拦,
结果正确返回。

**Files:**
- Test: `tests/test_mcp.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mcp.py (追加)
def test_run_tool_executes_mcp_with_auto_approval(tmp_path):
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        agent = _agent_with_mcp(tmp_path, ["<final>done</final>"],
                                mcp_manager=mgr, approval_policy="auto")
        result = agent.run_tool("mcp__echo__echo", {"text": "hello"})
        assert result == "echo:hello"
    finally:
        mgr.close_all()


def test_run_tool_blocks_mcp_when_approval_never(tmp_path):
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        agent = _agent_with_mcp(tmp_path, ["<final>done</final>"],
                                mcp_manager=mgr, approval_policy="never")
        result = agent.run_tool("mcp__echo__echo", {"text": "hi"})
        assert "approval denied" in result
    finally:
        mgr.close_all()


def test_run_tool_rejects_mcp_missing_required_arg(tmp_path):
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        agent = _agent_with_mcp(tmp_path, ["<final>done</final>"],
                                mcp_manager=mgr, approval_policy="auto")
        result = agent.run_tool("mcp__echo__echo", {})
        assert "missing required args: text" in result
    finally:
        mgr.close_all()
```

- [ ] **Step 2: 运行,确认通过(护栏已在 Task 4 接好,这里是验收)**

Run: `uv run python -m pytest tests/test_mcp.py -k run_tool -q`
Expected: PASS(3 passed)。若 `blocks` 用例失败,检查 `validate_tool` 的
`mcp__` 分支是否在执行前(approval 之前)正确返回。

- [ ] **Step 3: 提交**

```bash
git add tests/test_mcp.py
git commit -m "test(mcp): verify MCP tools flow through run_tool guardrails"
```

---

## Task 6: CLI 装配与清理(`cli.py`)

**Files:**
- Modify: `cagent/cli.py`(`build_agent` 构造 manager 并注入;`main` 退出清理)
- Test: `tests/test_mcp.py`(追加 build_agent 冒烟)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mcp.py (追加)
import argparse
from cagent.cli import build_agent


def test_build_agent_wires_mcp_manager(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"echo": {"command": sys.executable, "args": [FIXTURE]}}
    }), encoding="utf-8")
    args = argparse.Namespace(
        cwd=str(tmp_path), provider="ollama", model=None, host="http://x",
        base_url=None, ollama_timeout=300, openai_timeout=300, resume=None,
        approval="auto", secret_env_names=[], max_steps=6, max_new_tokens=512,
        temperature=0.2, top_p=0.9,
    )
    agent = build_agent(args)
    try:
        assert agent.mcp_manager is not None
        assert "mcp__echo__echo" in agent.tools
    finally:
        agent.mcp_manager.close_all()
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run python -m pytest tests/test_mcp.py -k build_agent -q`
Expected: FAIL,`AttributeError`(agent 无 `mcp_manager`)或工具缺失。

- [ ] **Step 3a: `cli.py` — 顶部导入**

在 `cagent/cli.py:17` 的 import 区加:

```python
from .mcp import McpManager
```

- [ ] **Step 3b: `cli.py` — `build_agent` 构造并注入**

在 `cagent/cli.py` 的 `build_agent` 内,`store = SessionStore(...)`(约 `cli.py:226`)
之后构造 manager,并把 `mcp_manager=mcp_manager` 传给两个 `CAgent` 构造点
(`from_session(...)` 与 `CAgent(...)`):

```python
    mcp_manager = McpManager.from_path(workspace.repo_root + "/.mcp.json")
    mcp_manager.start_all()
```

`from_session` 走的是 `**kwargs`,直接加 `mcp_manager=mcp_manager` 即可;
`CAgent(...)` 直接构造的那处,在参数末尾加 `mcp_manager=mcp_manager,`。

- [ ] **Step 3c: `cli.py` — `main` 退出清理**

`main()` 里 one-shot 分支与 REPL 分支都要在结束时清理。最稳妥是在
`agent = build_agent(args)`(`cli.py:288`)之后用 try/finally 包住后续逻辑:

```python
    agent = build_agent(args)
    try:
        # ... 原有 welcome / one-shot / REPL 逻辑保持不变 ...
    finally:
        if getattr(agent, "mcp_manager", None) is not None:
            agent.mcp_manager.close_all()
```

注意:原 `main` 中多处 `return 0/1` 仍然有效——`finally` 会在 return 前执行清理。

- [ ] **Step 4: 运行,确认通过**

Run: `uv run python -m pytest tests/test_mcp.py -k build_agent -q`
Expected: PASS(1 passed)

- [ ] **Step 5: 全量回归**

Run: `uv run python -m pytest -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add cagent/cli.py tests/test_mcp.py
git commit -m "feat(mcp): wire McpManager into build_agent with cleanup on exit"
```

---

## Task 7: 示例配置与文档

**Files:**
- Create: `.mcp.json.example`
- Modify: `CLAUDE.md`

- [ ] **Step 1: 写示例配置**

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

- [ ] **Step 2: 在 CLAUDE.md 增补一节**

在 CLAUDE.md 的 Architecture 区追加(放在 "Tools reference" 之后):

```markdown
### MCP client integration

cagent can act as an MCP client. On startup, `build_agent()` reads `<repo>/.mcp.json`
(`mcpServers` map), launches each server as a local stdio subprocess via `McpManager`
(`cagent/mcp.py`), and merges discovered tools into the registry — only on the
top-level agent (`depth == 0`). Tool names are namespaced `mcp__<server>__<tool>`,
all marked `risky=True` (approval-gated), and validated generically against their
`inputSchema.required`. A server that fails to start is skipped with a warning
(graceful degradation). Subprocesses are closed on exit. Transport is stdio only;
the JSON-RPC client is hand-written and synchronous (no `mcp` SDK dependency).
```

- [ ] **Step 3: 运行全量测试**

Run: `uv run python -m pytest -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add .mcp.json.example CLAUDE.md
git commit -m "docs(mcp): add .mcp.json example and CLAUDE.md section"
```

---

## 验收清单

- [ ] `uv run python -m pytest -q` 全绿
- [ ] `uv run ruff check .` 无新增告警
- [ ] 无 `.mcp.json` 时 cagent 行为与改动前完全一致
- [ ] 坏 server 不阻断启动(警告 + 跳过)
- [ ] MCP 工具在 `--approval ask/never` 下受审批闸门约束

