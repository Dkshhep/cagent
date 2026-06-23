import json
import sys
from pathlib import Path

import pytest

from cagent.mcp import McpClient, McpManager, McpServerConfig, _schema_to_display, load_mcp_config
from cagent.runtime import CAgent, SessionStore
from cagent.models import FakeModelClient
from cagent.workspace import WorkspaceContext


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


def test_load_mcp_config_skips_entry_without_command(tmp_path):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"bad": {"args": ["x"]}, "ok": {"command": "npx"}}}), encoding="utf-8")
    names = [c.name for c in load_mcp_config(cfg)]
    assert names == ["ok"]


def test_load_mcp_config_tolerates_null_args_and_env(tmp_path):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"s": {"command": "npx", "args": None, "env": None}}}), encoding="utf-8")
    (config,) = load_mcp_config(cfg)
    assert config.args == [] and config.env == {}


def test_load_mcp_config_non_dict_top_level_returns_empty(tmp_path):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_mcp_config(cfg) == []


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


def test_mcp_client_unknown_method_raises():
    # 直接调用一个不存在的工具方法,fixture 会回 JSON-RPC error,客户端应抛 McpError
    from cagent.mcp import McpError
    client = McpClient(_echo_config())
    try:
        client.start()
        client.initialize()
        with pytest.raises(McpError):
            client._request("nonexistent/method")
    finally:
        client.close()


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


def test_mcp_client_times_out_on_silent_server(tmp_path):
    # 一个只睡觉、从不回复的假 server:客户端应在 timeout 后抛 McpError 而不是永久阻塞
    from cagent.mcp import McpError
    script = tmp_path / "silent_server.py"
    script.write_text("import time\nwhile True:\n    time.sleep(1)\n", encoding="utf-8")
    cfg = McpServerConfig(name="silent", command=sys.executable, args=[str(script)])
    client = McpClient(cfg, timeout=1)
    try:
        client.start()
        with pytest.raises(McpError):
            client.initialize()
    finally:
        client.close()


def test_schema_to_display_required_and_optional():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }
    assert _schema_to_display(schema) == {"a": "str", "b": "int?"}


def test_schema_to_display_type_mappings():
    schema = {
        "properties": {
            "s": {"type": "string"}, "i": {"type": "integer"}, "f": {"type": "number"},
            "b": {"type": "boolean"}, "arr": {"type": "array"}, "obj": {"type": "object"},
        },
        "required": ["s", "i", "f", "b", "arr", "obj"],
    }
    assert _schema_to_display(schema) == {"s": "str", "i": "int", "f": "float", "b": "bool", "arr": "list", "obj": "dict"}


def test_schema_to_display_handles_empty_and_missing_type():
    assert _schema_to_display(None) == {}
    assert _schema_to_display({}) == {}
    assert _schema_to_display({"properties": {"x": {}}, "required": ["x"]}) == {"x": "str"}


def test_schema_to_display_tolerates_union_type():
    # JSON Schema 允许 "type": ["string","null"];不可哈希,必须降级为 str 而非崩溃
    schema = {"properties": {"x": {"type": ["string", "null"]}}, "required": ["x"]}
    assert _schema_to_display(schema) == {"x": "str"}


def test_schema_to_display_non_dict_property_spec():
    schema = {"properties": {"x": "not-a-dict"}, "required": ["x"]}
    assert _schema_to_display(schema) == {"x": "str"}


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


def test_run_tool_mcp_executes_when_approved(tmp_path):
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        agent = _agent_with_mcp(tmp_path, ["<final>done</final>"], mcp_manager=mgr, approval_policy="auto")
        result = agent.run_tool("mcp__echo__echo", {"text": "hi"})
        assert result == "echo:hi"
    finally:
        mgr.close_all()


def test_run_tool_mcp_blocked_when_approval_never(tmp_path):
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        agent = _agent_with_mcp(tmp_path, ["<final>done</final>"], mcp_manager=mgr, approval_policy="never")
        result = agent.run_tool("mcp__echo__echo", {"text": "hi"})
        assert result == "error: approval denied for mcp__echo__echo"
    finally:
        mgr.close_all()


def test_run_tool_mcp_blocked_in_read_only(tmp_path):
    # read_only 顶层 agent 仍会注册 MCP 工具(depth 0),但 approve() 因 read_only 直接拒绝
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        agent = _agent_with_mcp(tmp_path, ["<final>done</final>"], mcp_manager=mgr, approval_policy="auto", read_only=True)
        result = agent.run_tool("mcp__echo__echo", {"text": "hi"})
        assert result == "error: approval denied for mcp__echo__echo"
    finally:
        mgr.close_all()


def test_run_tool_mcp_missing_required_arg_is_rejected(tmp_path):
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        agent = _agent_with_mcp(tmp_path, ["<final>done</final>"], mcp_manager=mgr, approval_policy="auto")
        result = agent.run_tool("mcp__echo__echo", {})  # 缺 required 'text'
        assert "invalid arguments" in result and "text" in result
    finally:
        mgr.close_all()


def test_run_tool_mcp_unknown_tool_rejected(tmp_path):
    mgr = McpManager([_echo_config()])
    mgr.start_all()
    try:
        agent = _agent_with_mcp(tmp_path, ["<final>done</final>"], mcp_manager=mgr, approval_policy="auto")
        result = agent.run_tool("mcp__echo__nope", {})
        assert "unknown tool" in result
    finally:
        mgr.close_all()


def test_build_agent_wires_mcp_from_config(tmp_path, monkeypatch):
    import json as _json
    from types import SimpleNamespace
    from cagent import cli

    # 在临时仓库放一个 .mcp.json,指向 echo fixture server
    (tmp_path / ".mcp.json").write_text(_json.dumps({
        "mcpServers": {"echo": {"command": sys.executable, "args": [FIXTURE]}}
    }), encoding="utf-8")

    # 用假 model client,避免真实联网
    monkeypatch.setattr(cli, "_build_model_client", lambda args: FakeModelClient(["<final>ok</final>"]))

    args = SimpleNamespace(
        cwd=str(tmp_path), resume=None, approval="auto",
        max_steps=3, max_new_tokens=128, secret_env_names=[],
    )
    agent = cli.build_agent(args)
    try:
        assert agent.mcp_manager is not None
        assert "mcp__echo__echo" in agent.tools
    finally:
        if agent.mcp_manager:
            agent.mcp_manager.close_all()


def test_build_agent_wires_mcp_on_resume_path(tmp_path, monkeypatch):
    import json as _json
    from types import SimpleNamespace
    from cagent import cli

    (tmp_path / ".mcp.json").write_text(_json.dumps({
        "mcpServers": {"echo": {"command": sys.executable, "args": [FIXTURE]}}
    }), encoding="utf-8")
    monkeypatch.setattr(cli, "_build_model_client", lambda args: FakeModelClient(["<final>ok</final>"]))

    def _args(resume):
        return SimpleNamespace(cwd=str(tmp_path), resume=resume, approval="auto",
                               max_steps=3, max_new_tokens=128, secret_env_names=[])

    # 先建一个全新 agent;构造时就会把 session 落盘,latest() 因此能找到它。
    first = cli.build_agent(_args(None))
    try:
        first.ask("hi")  # 触发一次 ask,确保 session 写盘
    finally:
        if first.mcp_manager:
            first.mcp_manager.close_all()

    # 用 latest 恢复,走 CAgent.from_session 分支,验证 resume 路径也接上了 mcp_manager。
    resumed = cli.build_agent(_args("latest"))
    try:
        assert resumed.mcp_manager is not None
        assert "mcp__echo__echo" in resumed.tools
    finally:
        if resumed.mcp_manager:
            resumed.mcp_manager.close_all()
