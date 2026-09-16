from cagent import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from cagent.context_manager import ContextManager, estimate_tokens


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".cagent" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def add_card(agent, key, value, *, scope="project", display=None):
    action, card = agent.memory_store.commit({
        "op": "add", "target_card_id": "", "key": key, "kind": "preference", "scope": scope,
        "tags": ["coding"], "current_value": value, "display_text": display or value,
        "change_note": "", "source": {"authorization_turn_id": "test", "content_turn_ids": ["test"], "user_quote": "test"},
    })
    assert action == "added"
    return card


def test_estimate_tokens_reflects_text_shape():
    english = estimate_tokens("hello world " * 40)
    chinese = estimate_tokens("预算分配" * 40)
    jsonish = estimate_tokens('{"path": "sample.py", "args": [1, 2, 3]}' * 10)

    assert 120 <= english <= 160
    assert 120 <= chinese <= 140
    assert 130 <= jsonish <= 180
    assert chinese > english * 0.8


def test_context_manager_assembles_sections_in_expected_order(tmp_path):
    agent = build_agent(tmp_path, [])
    add_card(agent, "coding.comment_language", "中文", display="当前项目注释用中文。")
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.record({"role": "assistant", "content": "old answer", "created_at": "2026-04-07T10:00:30+00:00"})

    prompt, metadata = ContextManager(agent).build("Where is the deploy key?")

    assert prompt.index("You are cagent") < prompt.index("Transcript:")
    assert prompt.index("Transcript:") < prompt.index("Saved user preferences")
    assert prompt.index("Saved user preferences") < prompt.index("Current user request:")
    assert prompt.rstrip().endswith("Current user request:\nWhere is the deploy key?")
    assert metadata["section_order"] == ["prefix", "history", "saved_memory", "recovery_notice", "current_request"]


def test_context_manager_places_recovery_notice_near_current_request(tmp_path):
    agent = build_agent(tmp_path, [])
    add_card(agent, "coding.comment_language", "中文", display="当前项目注释用中文。")
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.render_recovery_notice = lambda: "Recovery warning:\nInspect before continuing"

    prompt, metadata = ContextManager(agent).build("Continue")

    assert prompt.index("Saved user preferences") < prompt.index("Recovery warning:")
    assert prompt.index("Recovery warning:") < prompt.index("Current user request:")
    assert metadata["sections"]["prefix"]["rendered_chars"] == len(agent.prefix)
    assert metadata["sections"]["recovery_notice"]["rendered_chars"] > 0


def test_context_manager_reduces_saved_memory_and_history_and_preserves_newer_context(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    add_card(agent, "coding.first", "C" * 150)
    add_card(agent, "coding.second", "D" * 150)
    add_card(agent, "coding.third", "E" * 150)
    agent.record({"role": "user", "content": "OLD-CONTEXT " + ("D" * 260), "created_at": "2026-04-07T09:59:00+00:00"})
    for minute in range(1, 8):
        role = "assistant" if minute % 2 == 1 else "user"
        content = "RECENT-CONTEXT " + ("E" * 260) if minute == 7 else f"recent-{minute} " + ("E" * 180)
        agent.record({"role": role, "content": content, "created_at": f"2026-04-07T10:0{minute}:00+00:00"})

    manager = ContextManager(
        agent,
        total_budget=1200,
        section_budgets={
            "prefix": 120,
            "saved_memory": 120,
            "history": 400,
        },
    )

    prompt, metadata = manager.build("keep this request verbatim")

    for section in ("prefix", "saved_memory", "history"):
        assert metadata["sections"][section]["rendered_chars"] <= metadata["sections"][section]["budget_chars"]

    assert metadata["history"]["history_count"] == 8
    assert "keep this request verbatim" in prompt
    assert metadata["prompt_tokens_estimated"] <= metadata["prompt_token_budget"]
    assert metadata["sections"]["history"]["estimated_tokens"] <= metadata["sections"]["history"]["budget_tokens"]


def test_context_manager_history_budget_tracks_current_request_size(tmp_path):
    agent = build_agent(tmp_path, [])
    manager = ContextManager(
        agent,
        total_budget=500,
        section_budgets={"prefix": 80, "saved_memory": 40},
        min_history_tokens=20,
    )

    _, short_metadata = manager.build("short")
    _, long_metadata = manager.build("long request " + ("detail " * 120))
    short_budget = manager._dynamic_section_budgets({"current_request": "Current user request:\nshort", "recovery_notice": ""}, [])
    long_budget = manager._dynamic_section_budgets({"current_request": "Current user request:\n" + ("detail " * 120), "recovery_notice": ""}, [])
    assert short_budget["history"] > long_budget["history"]
    assert long_metadata["sections"]["current_request"]["estimated_tokens"] > short_metadata["sections"]["current_request"]["estimated_tokens"]


def test_context_manager_rejects_current_request_over_hard_max(tmp_path):
    agent = build_agent(tmp_path, [])
    manager = ContextManager(agent, section_max={"current_request": 10})

    try:
        manager.build("oversized " + ("request " * 80))
    except ValueError as exc:
        assert "current request exceeds prompt budget hard max" in str(exc)
    else:
        raise AssertionError("expected oversized current request to be rejected")


def test_context_manager_omits_whole_cards_under_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    cards = [add_card(agent, f"coding.rule_{index}", str(index), display=f"规则{index}：" + ("中文" * 50)) for index in range(4)]

    prompt, metadata = ContextManager(
        agent,
        total_budget=400,
        section_budgets={
            "prefix": 60,
            "saved_memory": 80,
            "history": 60,
        },
    ).build("规则")

    assert len(metadata["selected_card_ids"]) < 4
    assert set(metadata["selected_card_ids"] + metadata["omitted_card_ids"]) == {card["id"] for card in cards}
    for card in cards:
        if card["id"] not in metadata["selected_card_ids"]:
            assert card["display_text"] not in prompt


def test_context_manager_preserves_current_request_when_over_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    add_card(agent, "coding.comment_language", "中文", display="当前项目注释用中文。")
    for index in range(5):
        agent.record({"role": "user", "content": f"history-{index} " + ("D" * 220)})

    request = "please preserve this request exactly"
    prompt, metadata = ContextManager(
        agent,
        total_budget=250,
        section_budgets={
            "prefix": 80,
            "saved_memory": 80,
            "history": 80,
        },
    ).build(request)

    assert prompt.split("Current user request:\n", 1)[1] == request
    assert metadata["current_request"]["text"] == request
    assert metadata["current_request"]["rendered_chars"] == len(request)


def test_context_manager_collapses_older_duplicate_reads_without_file_summary(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    for created_at in ("2026-04-07T09:00:00+00:00", "2026-04-07T09:01:00+00:00"):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": "sample.txt", "start": 1, "end": 2},
                "content": "# sample.txt\n" + ("alpha\n" * 120) + "beta\n",
                "created_at": created_at,
            }
        )

    for minute in range(2, 13):
        role = "user" if minute % 2 == 0 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:{minute:02d}:00+00:00",
            }
        )

    # 用很小的 history 预算强制触发压缩；M1 之后，预算内的 history 会保持 raw 不压缩。
    prompt, metadata = ContextManager(
        agent,
        total_budget=100000,
        section_budgets={"history": 160},
    ).build("check the file")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert "sample.txt -> alpha | beta" not in transcript
    assert metadata["history"]["collapsed_duplicate_reads"] == 1
    assert "reused_file_summary_count" not in metadata["history"]


def test_context_manager_head_tail_clips_older_tool_output(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record(
        {
            "role": "tool",
            "name": "run_shell",
            "args": {"command": "pytest -q"},
            "content": "START pytest\n" + ("middle log line\n" * 220) + "FINAL ERROR summary\n",
            "created_at": "2026-04-07T09:00:00+00:00",
        }
    )

    for minute in range(1, 12):
        role = "user" if minute % 2 == 1 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:{minute:02d}:00+00:00",
            }
        )

    # 同样用小 history 预算强制触发压缩。
    prompt, metadata = ContextManager(
        agent,
        total_budget=100000,
        section_budgets={"history": 260},
    ).build("check failures")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert "START pytest" in transcript
    assert "FINAL ERROR summary" in transcript
    assert "中间内容已裁剪" in transcript
    assert metadata["history"]["summarized_tool_count"] == 1
    assert metadata["history"]["head_tail_clipped_tool_count"] == 1
    assert "reused_file_summary_count" not in metadata["history"]


def test_context_manager_marks_distillation_candidate_for_long_history(tmp_path):
    agent = build_agent(tmp_path, [])
    for index in range(35):
        role = "user" if index % 2 == 0 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"message-{index}",
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )

    _, metadata = ContextManager(
        agent,
        total_budget=100000,
        section_budgets={"history": 40},
    ).build("continue")

    assert metadata["history"]["history_count"] == 35
    assert metadata["history"]["distillation_candidate_start"] == 3
    assert metadata["history"]["distillation_candidate_end"] == 8
    assert metadata["history"]["distillation_candidate_count"] == 5
    assert metadata["history"]["recent_window"] == 8


def test_context_manager_recent_tool_is_clipped_only_when_needed(tmp_path):
    agent = build_agent(tmp_path, [])
    for index in range(10):
        agent.record(
            {
                "role": "user",
                "content": f"old-{index}",
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )
    agent.record(
        {
            "role": "tool",
            "name": "run_shell",
            "args": {"command": "pytest -q"},
            "content": "RECENT TOOL START\n" + ("recent middle\n" * 260) + "RECENT TOOL FINAL ERROR\n",
            "created_at": "2026-04-07T10:00:00+00:00",
        }
    )

    prompt, metadata = ContextManager(
        agent,
        total_budget=100000,
        section_budgets={"history": 180},
    ).build("continue")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert "RECENT TOOL START" in transcript
    assert "RECENT TOOL FINAL ERROR" in transcript
    assert "中间内容已裁剪" in transcript
    assert metadata["history"]["recent_tool_clipped_count"] == 1


def test_context_manager_final_fallback_protects_head_three_and_converges(tmp_path):
    agent = build_agent(tmp_path, [])
    for index in range(14):
        agent.record(
            {
                "role": "assistant" if index % 2 else "user",
                "content": f"KEEP-HEAD-{index} " + ("head " * 12) if index < 3 else f"BODY-{index} " + ("body " * 120),
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(
        agent,
        total_budget=100000,
        section_budgets={"history": 120},
    ).build("continue")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert "KEEP-HEAD-0" in transcript
    assert "KEEP-HEAD-1" in transcript
    assert "KEEP-HEAD-2" in transcript
    assert metadata["sections"]["history"]["estimated_tokens"] <= metadata["sections"]["history"]["budget_tokens"]
    assert metadata["history"]["final_fallback_clipped_count"] > 0


def test_context_manager_ignores_legacy_topics(tmp_path):
    memory_root = tmp_path / ".cagent" / "memory"
    topics_dir = memory_root / "topics"
    topics_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        "- [project-conventions](topics/project-conventions.md): Project Conventions\n"
        "  - summary: Stable repository conventions.\n"
        "  - tags: convention\n",
        encoding="utf-8",
    )
    (topics_dir / "project-conventions.md").write_text(
        "# Project Conventions\n\n"
        "- topic: project-conventions\n"
        "- summary: Stable repository conventions.\n"
        "- tags: convention\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n",
        encoding="utf-8",
    )

    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(agent).build("What conventions should I follow?")
    assert "Use constrained tools instead of guessing." not in prompt
    assert metadata["selected_card_ids"] == []
