"""cagent 作为 MCP 客户端接入外部 stdio server 的实现。

为什么存在:cagent 本身只有内置工具白名单;这个模块让 cagent 能在启动时
按 .mcp.json 拉起外部 MCP server 子进程,把它们的工具命名空间化后并入
工具注册表,从而在不改动控制循环的前提下扩展能力。
"""

import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path


class McpError(Exception):
    """MCP 相关错误的基类。"""


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def load_mcp_config(path):
    # 配置缺失或损坏都不应阻断 cagent 启动:返回空列表即“没有 MCP 工具”。
    path = Path(path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        print(f"[mcp] warning: cannot parse {path}, ignoring MCP config", file=sys.stderr)
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    configs = []
    for name, spec in servers.items():
        if not isinstance(spec, dict) or not spec.get("command"):
            continue
        raw_args = spec.get("args")
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        raw_env = spec.get("env")
        env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
        configs.append(
            McpServerConfig(
                name=str(name),
                command=str(spec["command"]),
                args=args,
                env=env,
            )
        )
    return configs


DEFAULT_TIMEOUT = 30
# 与 runtime 的 shell_env 同思路:只透传必要的环境变量,避免把无关 secret
# 带进子进程;再叠加每个 server 配置里声明的 env。
# PATHEXT / COMSPEC / SystemRoot 是 Windows 解析 npx/uvx 等启动器所必需的。
MCP_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME", "APPDATA", "SystemRoot", "PATHEXT", "COMSPEC")


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
        self._reader = None
        self._queue = None

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
        # 跨平台超时:Windows 管道不支持 select,改用后台守护线程把每行原始
        # 输出投递到队列;_request 在队列上带 timeout 取数,从而既不阻塞主线程,
        # 又能在 server 静默时及时报错。
        self._queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        # 持续 readline,直到 EOF;EOF 投递哨兵 None 让 _request 感知 server 关闭。
        stdout = self.process.stdout
        try:
            while True:
                raw = stdout.readline()
                if raw == "":
                    break
                self._queue.put(raw)
        except Exception:
            pass
        finally:
            self._queue.put(None)

    def _request(self, method, params=None):
        self._next_id += 1
        rid = self._next_id
        line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        # 读到第一条带匹配 id 的响应为止(跳过中途的通知/无 id 行)。
        while True:
            try:
                raw = self._queue.get(timeout=self.timeout)
            except queue.Empty:
                raise McpError(
                    f"server '{self.config.name}' timed out after {self.timeout}s during {method}"
                )
            if raw is None:
                raise McpError(f"server '{self.config.name}' closed during {method}")
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise McpError(
                    f"server '{self.config.name}' sent invalid JSON during {method}: {exc}"
                )
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
            try:
                self.process.kill()
                self.process.wait()
            except Exception:
                pass
        # 进程结束后 readline 会拿到 EOF,守护读线程随即退出;短暂 join 让它收尾。
        if self._reader is not None:
            try:
                self._reader.join(timeout=2)
            except Exception:
                pass
        self.process = None


_JSON_TYPE_TO_DISPLAY = {
    "string": "str", "integer": "int", "number": "float",
    "boolean": "bool", "array": "list", "object": "dict",
}


def _schema_to_display(input_schema):
    # 把 MCP 的 JSON Schema 压成 cagent 展示式 {字段: "类型"},供 build_prefix 渲染。
    props = (input_schema or {}).get("properties", {}) or {}
    required = set((input_schema or {}).get("required", []) or [])
    display = {}
    for field_name in props:
        spec = props.get(field_name) if isinstance(props, dict) else None
        raw_type = spec.get("type", "string") if isinstance(spec, dict) else "string"
        type_name = _JSON_TYPE_TO_DISPLAY.get(raw_type, "str") if isinstance(raw_type, str) else "str"
        # MCP schema 不带默认值,故用 ? 表示可选字段(必填字段渲染为裸类型)。
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
                print(f"[mcp] warning: server '{config.name}' unavailable: {exc}", file=sys.stderr)
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
            try:
                client.close()
            except Exception:
                pass
        self.clients.clear()
