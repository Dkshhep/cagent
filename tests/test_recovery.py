import json
from pathlib import Path

import pytest

from cagent import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from cagent.recovery import RecoveryCheckpointStore, capture_file_state
from cagent.storage import write_json_atomic


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".cagent" / "sessions"),
        approval_policy="auto",
    )


def seed_file_recovery(agent, tool="patch_file", changed=False):
    target = agent.root / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    path, before = capture_file_state("target.txt", agent.root)
    payload = {
        "schema_version": 1,
        "checkpoint_id": "recovery_test",
        "session_id": agent.session["id"],
        "run_id": "run_interrupted",
        "task_id": "task_interrupted",
        "tool": tool,
        "args_summary": {"path": path},
        "target": {"path": path, "before": before},
        "created_at": "2026-09-10T00:00:00+08:00",
    }
    agent.recovery_store.prepare(agent.session["id"], payload)
    if changed:
        target.write_text("after\n", encoding="utf-8")
    return payload


def resume(agent, outputs):
    return MiniAgent.from_session(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(agent.root),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )


def test_atomic_json_write_and_recovery_store_round_trip(tmp_path):
    path = tmp_path / "state.json"
    write_json_atomic(path, {"value": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"value": 1}

    store = RecoveryCheckpointStore(tmp_path)
    store.prepare("session", {"schema_version": 1, "tool": "run_shell"})
    store.prepare("other", {"schema_version": 1, "tool": "patch_file"})
    assert store.load("session")["tool"] == "run_shell"
    store.clear("session")
    assert store.load("session") is None
    assert store.load("other")["tool"] == "patch_file"


def test_atomic_json_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write_json_atomic(path, {"value": "old"})

    def fail_replace(_source, _target):
        raise OSError("injected replace failure")

    monkeypatch.setattr("cagent.storage.os.replace", fail_replace)
    with pytest.raises(OSError):
        write_json_atomic(path, {"value": "new"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"value": "old"}
    assert list(tmp_path.glob("state.json.*.tmp")) == []


def test_session_latest_ignores_recovery_sidecar(tmp_path):
    store = SessionStore(tmp_path)
    store.save({"id": "session-a", "history": []})
    RecoveryCheckpointStore(tmp_path).prepare("session-a", {"schema_version": 1})

    assert store.latest() == "session-a"


@pytest.mark.parametrize(
    ("tool_call", "expected"),
    [
        ('<tool name="write_file" path="new.txt"><content>new</content></tool>', "new"),
        ('<tool name="patch_file" path="target.txt"><old_text>old</old_text><new_text>new</new_text></tool>', "new"),
    ],
)
def test_normal_file_mutation_clears_recovery_after_history_is_saved(tmp_path, tool_call, expected):
    agent = build_agent(tmp_path, [tool_call, "<final>done</final>"])
    (tmp_path / "target.txt").write_text("old", encoding="utf-8")

    assert agent.ask("change a file") == "done"
    assert not agent.recovery_store.path(agent.session["id"]).exists()
    assert any(item.get("role") == "tool" for item in agent.session["history"])
    assert expected in (tmp_path / ("new.txt" if "write_file" in tool_call else "target.txt")).read_text(encoding="utf-8")

    events = [json.loads(line) for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()]
    names = [event["event"] for event in events]
    assert "recovery_checkpoint_prepared" in names
    assert "recovery_checkpoint_cleared" in names


def test_normal_run_shell_clears_recovery_after_history_is_saved(tmp_path):
    agent = build_agent(
        tmp_path,
        ['<tool>{"name":"run_shell","args":{"command":"echo ok","timeout":20}}</tool>', "<final>done</final>"],
    )

    assert agent.ask("run a command") == "done"
    assert not agent.recovery_store.path(agent.session["id"]).exists()
    assert any(item.get("role") == "tool" and item.get("name") == "run_shell" for item in agent.session["history"])


def test_changed_file_recovery_blocks_mutation_and_final_until_exact_read(tmp_path):
    agent = build_agent(tmp_path, [])
    seed_file_recovery(agent, changed=True)
    resumed = resume(
        agent,
        [
            '<tool name="patch_file" path="target.txt"><old_text>after</old_text><new_text>duplicated</new_text></tool>',
            "<final>too early</final>",
            '<tool>{"name":"read_file","args":{"path":"target.txt","start":1,"end":20}}</tool>',
            "<final>inspected</final>",
        ],
    )

    assert resumed.recovery_state["target_changed"] is True
    assert "may have produced partial or complete changes" in resumed.prompt("continue")
    assert resumed.ask("continue") == "inspected"
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "after\n"
    assert not resumed.recovery_store.path(resumed.session["id"]).exists()
    assert any("recovery_inspection_required" in item.get("content", "") for item in resumed.session["history"])


def test_unchanged_file_recovery_notice_requires_read(tmp_path):
    agent = build_agent(tmp_path, [])
    seed_file_recovery(agent, changed=False)
    resumed = resume(
        agent,
        [
            '<tool>{"name":"read_file","args":{"path":"target.txt"}}</tool>',
            "<final>safe</final>",
        ],
    )

    assert resumed.recovery_state["target_changed"] is False
    assert "still matches its pre-operation state" in resumed.prompt("continue")
    assert resumed.ask("continue") == "safe"


def test_file_recovery_search_and_wrong_file_read_do_not_clear_lock(tmp_path):
    agent = build_agent(tmp_path, [])
    seed_file_recovery(agent, changed=False)
    (tmp_path / "other.txt").write_text("other\n", encoding="utf-8")
    resumed = resume(
        agent,
        [
            '<tool>{"name":"search","args":{"pattern":"before","path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"other.txt"}}</tool>',
            "<final>too early</final>",
            '<tool>{"name":"read_file","args":{"path":"target.txt"}}</tool>',
            "<final>safe</final>",
        ],
    )

    assert resumed.ask("continue") == "safe"
    assert any("recovery_inspection_required" in item.get("content", "") for item in resumed.session["history"])


def test_run_shell_recovery_cannot_rerun_and_clears_after_read_only_inspection(tmp_path):
    agent = build_agent(tmp_path, [])
    payload = {
        "schema_version": 1,
        "checkpoint_id": "recovery_shell",
        "session_id": agent.session["id"],
        "run_id": "run_interrupted",
        "task_id": "task_interrupted",
        "tool": "run_shell",
        "args_summary": {"command": "echo secret", "timeout": 20},
        "target": None,
        "created_at": "2026-09-10T00:00:00+08:00",
    }
    agent.recovery_store.prepare(agent.session["id"], payload)
    resumed = resume(
        agent,
        [
            '<tool>{"name":"run_shell","args":{"command":"echo secret"}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>inspected</final>",
        ],
    )

    assert "outcome and side effects are unknown" in resumed.prompt("continue")
    assert resumed.ask("continue") == "inspected"
    assert not resumed.recovery_store.path(resumed.session["id"]).exists()


def test_old_session_checkpoint_is_ignored(tmp_path):
    agent = build_agent(tmp_path, ["<final>clean</final>"])
    agent.session["checkpoints"] = {"current_id": "old", "items": {"old": {"current_goal": "legacy"}}}
    agent.session_store.save(agent.session)
    resumed = resume(agent, ["<final>clean</final>"])

    assert resumed.recovery_state["status"] == "clean"
    assert "Task checkpoint:" not in resumed.prompt("continue")


def test_fault_after_checkpoint_before_tool_keeps_marker(tmp_path):
    agent = build_agent(tmp_path, ['<tool name="write_file" path="crash.txt"><content>x</content></tool>'])

    def crash(_args):
        raise SystemExit("injected before execution")

    agent.tools["write_file"]["run"] = crash
    with pytest.raises(SystemExit):
        agent.ask("write")

    checkpoint = agent.recovery_store.load(agent.session["id"])
    assert checkpoint["tool"] == "write_file"
    assert not (tmp_path / "crash.txt").exists()


def test_fault_after_file_change_before_tool_return_is_detected(tmp_path):
    agent = build_agent(tmp_path, ['<tool name="write_file" path="crash.txt"><content>x</content></tool>'])

    def change_then_crash(args):
        (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
        raise SystemExit("injected after side effect")

    agent.tools["write_file"]["run"] = change_then_crash
    with pytest.raises(SystemExit):
        agent.ask("write")

    resumed = resume(agent, ['<tool>{"name":"read_file","args":{"path":"crash.txt"}}</tool>', "<final>checked</final>"])
    assert resumed.recovery_state["target_changed"] is True
    assert resumed.ask("continue") == "checked"


def test_fault_before_history_save_keeps_marker(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, ['<tool name="write_file" path="crash.txt"><content>x</content></tool>'])
    original_save = agent.session_store.save

    def fail_when_recovery_is_active(session):
        if agent.recovery_store.path(session["id"]).exists():
            raise SystemExit("injected before history save")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_when_recovery_is_active)
    with pytest.raises(SystemExit):
        agent.ask("write")

    assert agent.recovery_store.load(agent.session["id"])["tool"] == "write_file"
    persisted = original_save.__self__.load(agent.session["id"])
    assert not any(item.get("role") == "tool" and item.get("name") == "write_file" for item in persisted["history"])


def test_fault_after_history_save_before_clear_keeps_safe_false_positive(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, ['<tool name="write_file" path="crash.txt"><content>x</content></tool>'])

    def fail_clear(_session_id):
        raise SystemExit("injected before checkpoint clear")

    monkeypatch.setattr(agent.recovery_store, "clear", fail_clear)
    with pytest.raises(SystemExit):
        agent.ask("write")

    assert agent.recovery_store.load(agent.session["id"])["tool"] == "write_file"
    persisted = agent.session_store.load(agent.session["id"])
    assert any(item.get("role") == "tool" and item.get("name") == "write_file" for item in persisted["history"])


def test_run_shell_interruption_keeps_unknown_outcome_marker(tmp_path):
    agent = build_agent(tmp_path, ['<tool>{"name":"run_shell","args":{"command":"do something"}}</tool>'])

    def crash(_args):
        raise SystemExit("injected during shell")

    agent.tools["run_shell"]["run"] = crash
    with pytest.raises(SystemExit):
        agent.ask("run")

    checkpoint = agent.recovery_store.load(agent.session["id"])
    assert checkpoint["tool"] == "run_shell"
    assert checkpoint["target"] is None
