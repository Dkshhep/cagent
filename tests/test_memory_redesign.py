"""新长期记忆合同：授权、去重、替换、取用和旧数据隔离。"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from cagent.cli import handle_memory_command
from cagent.memory_cards import MemoryValidationError, validate_proposal
from cagent.memory_store import MemoryStore
from cagent.models import FakeModelClient
from cagent.runtime import CAgent, SessionStore
from cagent.workspace import WorkspaceContext


class FakeMemoryDecider:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.last_metadata = {"duration_ms": 1, "completion": {}}
        self.calls = 0

    def review(self, **kwargs):
        self.calls += 1
        result = self.decisions.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(kwargs)
        return result


def agent_for(tmp_path, outputs, decider=None):
    workspace = WorkspaceContext.build(tmp_path, repo_root_override=tmp_path)
    store = SessionStore(tmp_path / ".cagent" / "sessions")
    memory_store = MemoryStore(tmp_path, global_root=tmp_path / "global-memory")
    return CAgent(
        model_client=FakeModelClient(outputs), workspace=workspace, session_store=store,
        memory_store=memory_store, memory_decider=decider, approval_policy="auto",
    )


def proposal(kwargs, *, op="add", target="", value="中文", key="coding.comment_language", scope="project", quote=None):
    user = kwargs["user_turn"]
    return {
        "op": op, "target_card_id": target, "key": key, "kind": "preference",
        "scope": scope, "tags": ["coding", "comments"], "current_value": value,
        "display_text": f"当前项目的代码注释使用{value}。", "change_note": "",
        "authorization_turn_id": user["turn_id"], "content_turn_ids": [user["turn_id"]],
        "user_quote": quote or user["content"],
    }


def change(fn):
    return lambda kwargs: {"decision": "change", "proposals": [fn(kwargs)]}


def test_add_dedupe_and_prompt_only_current_text(tmp_path):
    decider = FakeMemoryDecider(
        change(proposal),
        change(lambda kwargs: proposal(kwargs, value="尽量写中文")),
    )
    agent = agent_for(tmp_path, ["<final>收到</final>", "<final>好的</final>"], decider)
    assert "已记住" not in agent.ask("以后这个项目的注释都用中文")
    cards = agent.memory_store.active("project")
    assert len(cards) == 1
    assert cards[0]["current_value"] == "中文"
    agent.ask("以后这个项目代码注释还是尽量写中文")
    assert len(agent.memory_store.active("project")) == 1
    assert agent.memory_store.active("project")[0]["revisions"] == []
    prompt = agent.prompt("继续")
    assert "Saved user preferences" in prompt
    assert "当前项目的代码注释使用中文" in prompt
    assert "change_note" not in prompt


def test_explicit_update_keeps_revision_and_forget_hides_card(tmp_path):
    decider = FakeMemoryDecider()
    agent = agent_for(tmp_path, ["<final>已保存</final>"] * 3, decider)
    def first(kwargs):
        p = proposal(kwargs, value="PostgreSQL", key="project.database")
        p["display_text"] = "当前项目使用 PostgreSQL。"
        return {"decision": "change", "proposals": [p]}
    decider.decisions.append(first)
    assert "已记住：PostgreSQL" in agent.ask("记住：这个项目以后使用 PostgreSQL")
    card = agent.memory_store.active("project")[0]
    def second(kwargs):
        p = proposal(kwargs, op="update", target=card["id"], value="MySQL", key="project.database")
        p["display_text"] = "当前项目使用 MySQL。"
        p["change_note"] = "用户先前提出 PostgreSQL，随后明确改为 MySQL。"
        return {"decision": "change", "proposals": [p]}
    decider.decisions.append(second)
    answer = agent.ask("记住：这个项目以后改用 MySQL，不用 PostgreSQL 了")
    assert "原有记忆已更新为：MySQL" in answer
    updated = agent.memory_store.active("project")[0]
    assert updated["revisions"][0]["previous_value"] == "PostgreSQL"
    assert "PostgreSQL" not in agent.prompt("继续").split("Saved user preferences", 1)[1].split("Current user request", 1)[0]
    def third(kwargs):
        p = proposal(kwargs, op="forget", target=card["id"], key="project.database")
        return {"decision": "change", "proposals": [p]}
    decider.decisions.append(third)
    assert "已忘记" in agent.ask("忘记这个项目的数据库偏好")
    assert not agent.memory_store.active("project")


def test_temporary_instruction_and_bad_authorization_rejected(tmp_path):
    decider = FakeMemoryDecider(change(lambda kwargs: {**proposal(kwargs), "authorization_turn_id": "missing"}))
    agent = agent_for(tmp_path, ["<final>done</final>"] * 2, decider)
    agent.ask("这次注释用中文")
    assert decider.calls == 0
    assert not agent.memory_store.active("project")
    answer = agent.ask("记住：以后注释用中文")
    assert "未保存" in answer
    assert agent.last_memory_review["failure_reason"] == "authorization_turn_not_user"


def test_ambiguous_conflict_does_not_overwrite(tmp_path):
    agent = agent_for(tmp_path, ["<final>done</final>"] * 2, FakeMemoryDecider())
    user = {"role": "user", "turn_id": "turn_1", "content": "记住：这个项目以后使用 PostgreSQL"}
    p = proposal({"user_turn": user}, key="project.database", value="PostgreSQL")
    p["display_text"] = "当前项目使用 PostgreSQL。"
    agent.memory_store.commit(validate_proposal(p, {"turn_1": user}))
    def conflicting(kwargs):
        p = proposal(kwargs, key="project.database", value="MySQL")
        return {"decision": "change", "proposals": [p]}
    agent.memory_decider.decisions.append(conflicting)
    agent.ask("记住：这个项目也用 MySQL")
    assert agent.last_memory_review["failure_reason"] == "conflict_requires_explicit_update"
    assert agent.memory_store.active("project")[0]["current_value"] == "PostgreSQL"


def test_revision_guard_prevents_lost_update(tmp_path):
    store = MemoryStore(tmp_path, global_root=tmp_path / "global-memory")
    p = {
        "op": "add", "target_card_id": "", "key": "coding.comment_language", "kind": "preference",
        "scope": "project", "tags": [], "current_value": "中文", "display_text": "注释用中文。",
        "change_note": "", "source": {"authorization_turn_id": "turn_1", "content_turn_ids": ["turn_1"], "user_quote": "以后注释用中文"},
    }
    original_revision = store.read("project")["revision"]
    store.commit(p, expected_revision=original_revision)
    with pytest.raises(MemoryValidationError, match="store_revision_changed"):
        store.commit({**p, "key": "coding.other_language"}, expected_revision=original_revision)
    assert len(store.active("project")) == 1


def test_concurrent_writers_preserve_distinct_cards(tmp_path):
    def add(index):
        store = MemoryStore(tmp_path, global_root=tmp_path / "global-memory")
        return store.commit({
            "op": "add", "target_card_id": "", "key": f"coding.rule_{index}",
            "kind": "preference", "scope": "project", "tags": [],
            "current_value": str(index), "display_text": f"规则{index}", "change_note": "",
            "source": {"authorization_turn_id": f"turn_{index}", "content_turn_ids": [f"turn_{index}"], "user_quote": f"规则{index}"},
        })
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(8)))
    assert len(MemoryStore(tmp_path, global_root=tmp_path / "global-memory").active("project")) == 8


def test_legacy_is_detected_but_not_used_until_explicit_import(tmp_path):
    topic_dir = tmp_path / ".cagent" / "memory" / "topics"
    topic_dir.mkdir(parents=True)
    (topic_dir / "user-preferences.md").write_text("# User Preferences\n\n## Notes\n- Old instruction.\n", encoding="utf-8")
    agent = agent_for(tmp_path, ["<final>done</final>"])
    assert agent.legacy_memory_present
    assert "Old instruction" not in agent.prompt("继续")
    assert "user-preferences:4" in handle_memory_command(agent, "/memory migrate preview")
    assert not agent.memory_store.active("project")
    assert "Imported" in handle_memory_command(agent, "/memory migrate import user-preferences:4")
    assert "Old instruction" in agent.prompt("继续")


def test_legacy_import_preflights_conflicts_before_any_write(tmp_path):
    topic_dir = tmp_path / ".cagent" / "memory" / "topics"
    topic_dir.mkdir(parents=True)
    (topic_dir / "notes.md").write_text("## Notes\n- First.\n- Second.\n", encoding="utf-8")
    store = MemoryStore(tmp_path, global_root=tmp_path / "global-memory")
    store.commit({
        "op": "add", "target_card_id": "", "key": "legacy.notes.3", "kind": "explicit_note",
        "scope": "project", "tags": [], "current_value": "Existing.", "display_text": "Existing.",
        "change_note": "", "source": {"authorization_turn_id": "migration", "content_turn_ids": [], "user_quote": "manual"},
    })
    preview = store.migration_preview()
    assert preview[1]["conflict"] == "Existing."
    with pytest.raises(MemoryValidationError, match="legacy_candidate_conflicts"):
        store.import_legacy(["notes:2", "notes:3"])
    assert len(store.active("project")) == 1


def test_decider_invalid_json_does_not_break_main_task(tmp_path):
    agent = agent_for(tmp_path, ["<final>任务完成，我已经记住了。</final>"], FakeMemoryDecider(MemoryValidationError("memory_review_invalid_json")))
    answer = agent.ask("记住以后注释用中文")
    assert "任务完成" in answer
    assert "未保存" in answer
    assert "我已经记住" not in answer
    assert not agent.memory_store.active("project")
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["memory_review_status"] == "failed"


def test_decider_transport_error_does_not_break_main_task(tmp_path):
    agent = agent_for(tmp_path, ["<final>任务完成。</final>"], FakeMemoryDecider(RuntimeError("upstream down")))
    answer = agent.ask("记住：以后注释用中文")
    assert "任务完成" in answer and "未保存" in answer
    assert agent.last_memory_review["failure_reason"] == "RuntimeError"


def test_secret_request_is_rejected_before_review_call(tmp_path):
    decider = FakeMemoryDecider(change(proposal))
    agent = agent_for(tmp_path, ["<final>Done.</final>"], decider)
    answer = agent.ask("记住：我的 API key 是 sk-secret-token-123")
    assert "未保存" in answer
    assert decider.calls == 0
    assert agent.last_memory_review["failure_reason"] == "secret_shaped_request"


def test_configured_secret_value_is_not_sent_to_decider(tmp_path, monkeypatch):
    monkeypatch.setenv("PICO_DEEPSEEK_API_KEY", "private-odd-value-for-test")
    decider = FakeMemoryDecider(change(proposal))
    agent = agent_for(tmp_path, ["<final>Done.</final>"], decider)
    answer = agent.ask("记住：偏好 private-odd-value-for-test")
    assert "未保存" in answer
    assert decider.calls == 0
    assert agent.last_memory_review["failure_reason"] == "secret_shaped_request"


def test_missing_prior_conclusion_requests_clarification_without_review(tmp_path):
    decider = FakeMemoryDecider(change(proposal))
    agent = agent_for(tmp_path, ["<final>Done.</final>"], decider)
    answer = agent.ask("记住刚才的结论")
    assert "需要澄清" in answer
    assert decider.calls == 0
    assert not agent.memory_store.active("project")


def test_reset_keeps_saved_cards_and_cli_forget(tmp_path):
    agent = agent_for(tmp_path, ["<final>done</final>"], FakeMemoryDecider(change(proposal)))
    agent.ask("以后这个项目注释用中文")
    card = agent.memory_store.active("project")[0]
    assert card["id"] in handle_memory_command(agent, "/memory")
    assert card["id"] in handle_memory_command(agent, f"/memory show {card['id']}")
    agent.reset()
    assert agent.session["history"] == []
    assert len(agent.memory_store.active("project")) == 1
    assert "Forgot" in handle_memory_command(agent, f"/memory forget {card['id']}")
    assert not agent.memory_store.active("project")


def test_old_session_turn_ids_assigned_without_making_distilled_marker_source(tmp_path):
    agent = agent_for(tmp_path, [])
    agent.session["history"] = [
        {"role": "user", "content": "old", "created_at": "yesterday"},
        {"role": "assistant", "content": "summary", "metadata": {"kind": "distilled_history"}},
    ]
    agent.session_store.save(agent.session)
    resumed = CAgent.from_session(
        model_client=FakeModelClient([]), workspace=agent.workspace, session_store=agent.session_store,
        session_id=agent.session["id"], memory_store=agent.memory_store,
    )
    assert resumed.session["history"][0]["turn_id"]
    assert "turn_id" not in resumed.session["history"][1]
    saved = json.loads(resumed.session_path.read_text(encoding="utf-8"))
    assert saved["history"][0]["turn_id"] == resumed.session["history"][0]["turn_id"]
