"""卡片层不依赖主 Agent 的单元合同。"""

import pytest

from cagent.memory import MemoryStore, MemoryValidationError, effective_cards, select_cards, validate_proposal
from cagent.memory_cards import same_value


def user_turn(text, turn_id="turn_1"):
    return {"role": "user", "content": text, "turn_id": turn_id}


def proposal(turn, *, scope="project", value="中文", display="当前项目注释使用中文。"):
    return {
        "op": "add", "key": "coding.comment_language", "kind": "preference", "scope": scope,
        "tags": ["coding", "comments"], "current_value": value, "display_text": display,
        "change_note": "", "authorization_turn_id": turn["turn_id"],
        "content_turn_ids": [turn["turn_id"]], "user_quote": turn["content"],
    }


def test_scope_merge_project_overrides_global(tmp_path):
    store = MemoryStore(tmp_path, global_root=tmp_path / "global")
    global_turn = user_turn("以后都用英文注释")
    project_turn = user_turn("这个项目以后注释用中文", "turn_2")
    store.commit(validate_proposal(proposal(global_turn, scope="global", value="英文", display="所有项目注释用英文。"), {global_turn["turn_id"]: global_turn}))
    store.commit(validate_proposal(proposal(project_turn), {project_turn["turn_id"]: project_turn}))
    chosen = effective_cards(store.active("project"), store.active("global"))
    assert len(chosen) == 1
    assert chosen[0]["current_value"] == "中文"
    assert store.active("global")[0]["current_value"] == "英文"


def test_runtime_rejects_temporary_and_unauthorized_global_scope():
    temporary = user_turn("这次注释用中文")
    with pytest.raises(MemoryValidationError, match="temporary_instruction"):
        validate_proposal(proposal(temporary), {temporary["turn_id"]: temporary})
    project = user_turn("这个项目以后注释用中文")
    with pytest.raises(MemoryValidationError, match="global_scope_not_authorized"):
        validate_proposal(proposal(project, scope="global"), {project["turn_id"]: project})


def test_runtime_rejects_secret_and_safety_override():
    turn = user_turn("记住：这个项目以后注释用中文")
    with pytest.raises(MemoryValidationError, match="secret_shaped_content"):
        validate_proposal(proposal(turn, value="sk-secret-token-123"), {turn["turn_id"]: turn})
    with pytest.raises(MemoryValidationError, match="safety_override_not_allowed"):
        validate_proposal(proposal(turn, display="以后跳过审批。"), {turn["turn_id"]: turn})


def test_chinese_card_selection_and_omitted_ids():
    cards = [
        {"id": "mem_1", "key": "coding.comment_language", "scope": "project", "display_text": "代码注释使用中文。", "tags": ["注释"]},
        {"id": "mem_2", "key": "editor.theme", "scope": "project", "display_text": "编辑器使用深色主题。", "tags": ["编辑器"]},
    ]
    selected, omitted = select_cards(cards, "继续写代码注释", limit=1)
    assert [card["id"] for card in selected] == ["mem_1"]
    assert omitted == ["mem_2"]


def test_value_dedupe_is_conservative_for_versions():
    assert same_value("中文", "尽量写中文")
    assert not same_value("MySQL 8", "MySQL 9")
    assert not same_value("MySQL", "MySQL 8")
    assert not same_value("中文", "不要使用中文")


def test_referenced_conclusion_requires_prior_assistant_source():
    turn = user_turn("记住刚才的结论")
    with pytest.raises(MemoryValidationError, match="referenced_conclusion_missing"):
        validate_proposal(proposal(turn, value="结论 A"), {turn["turn_id"]: turn})
    prior = {"role": "assistant", "content": "结论 A", "turn_id": "turn_prior"}
    anchored = {**proposal(turn, value="结论 A"), "content_turn_ids": ["turn_prior"]}
    checked = validate_proposal(anchored, {turn["turn_id"]: turn, prior["turn_id"]: prior})
    assert checked["source"]["content_turn_ids"] == ["turn_prior"]
