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
    agent.memory.append_note("deploy key is red", tags=("deploy",), created_at="2026-04-07T10:00:00+00:00")
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.record({"role": "assistant", "content": "old answer", "created_at": "2026-04-07T10:00:30+00:00"})

    prompt, metadata = ContextManager(agent).build("Where is the deploy key?")

    assert prompt.index("You are cagent") < prompt.index("Transcript:")
    assert prompt.index("Transcript:") < prompt.index("Memory:")
    assert prompt.index("Memory:") < prompt.index("Relevant memory:")
    assert prompt.index("Relevant memory:") < prompt.index("Current user request:")
    assert prompt.rstrip().endswith("Current user request:\nWhere is the deploy key?")
    assert metadata["section_order"] == ["prefix", "history", "checkpoint", "memory", "relevant_memory", "current_request"]


def test_context_manager_places_checkpoint_after_history_for_cache_prefix(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.render_checkpoint_text = lambda: "Task checkpoint:\n- Next step: keep history prefix stable"

    prompt, metadata = ContextManager(agent).build("Continue")

    assert prompt.index("Transcript:") < prompt.index("Task checkpoint:")
    assert prompt.index("Task checkpoint:") < prompt.index("Memory:")
    assert metadata["sections"]["prefix"]["rendered_chars"] == len(agent.prefix)
    assert metadata["sections"]["checkpoint"]["rendered_chars"] > 0


def test_context_manager_reduces_relevant_memory_before_history_and_preserves_newer_context(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    agent.memory.append_note("keep episodic note one " + ("C" * 220), tags=("keep",), created_at="2026-04-07T10:00:00+00:00")
    agent.memory.append_note("keep episodic note two " + ("D" * 220), tags=("keep",), created_at="2026-04-07T10:01:00+00:00")
    agent.memory.append_note("keep episodic note three " + ("E" * 220), tags=("keep",), created_at="2026-04-07T10:02:00+00:00")
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
            "memory": 120,
            "relevant_memory": 120,
            "history": 400,
        },
    )

    prompt, metadata = manager.build("keep this request verbatim")

    for section in ("prefix", "memory", "relevant_memory", "history"):
        assert metadata["sections"][section]["rendered_chars"] <= metadata["sections"][section]["budget_chars"]

    reduction_sections = [entry["section"] for entry in metadata["budget_reductions"]]
    assert reduction_sections[0] == "history"
    assert reduction_sections
    assert "keep this request verbatim" in prompt
    assert metadata["prompt_tokens_estimated"] <= metadata["prompt_token_budget"]
    assert metadata["sections"]["history"]["estimated_tokens"] <= metadata["sections"]["history"]["budget_tokens"]


def test_context_manager_history_budget_tracks_current_request_size(tmp_path):
    agent = build_agent(tmp_path, [])
    manager = ContextManager(
        agent,
        total_budget=500,
        section_budgets={"prefix": 80, "memory": 40, "relevant_memory": 30},
        min_history_tokens=20,
    )

    _, short_metadata = manager.build("short")
    _, long_metadata = manager.build("long request " + ("detail " * 120))

    assert short_metadata["section_token_budgets"]["history"] > long_metadata["section_token_budgets"]["history"]
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


def test_context_manager_renders_top_three_episodic_notes_per_note_under_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.memory.append_note("alpha episodic note " + ("A" * 120), tags=("recall",), created_at="2026-04-07T10:00:00+00:00")
    agent.memory.append_note("beta episodic recall note " + ("B" * 120), created_at="2026-04-07T10:01:00+00:00")
    agent.memory.append_note("gamma episodic note " + ("C" * 120), tags=("recall",), created_at="2026-04-07T10:02:00+00:00")
    agent.memory.append_note("older unmatched note", created_at="2026-04-07T09:59:00+00:00")
    agent.memory.append_note("Unrelated note", created_at="2026-04-07T11:00:00+00:00")

    prompt, metadata = ContextManager(
        agent,
        total_budget=250,
        section_budgets={
            "prefix": 60,
            "memory": 60,
            "relevant_memory": 80,
            "history": 60,
        },
    ).build("recall")

    assert metadata["relevant_memory"]["selected_count"] == 3
    assert metadata["relevant_memory"]["limit"] == 3
    assert metadata["relevant_memory"]["selected_notes"] == [
        "gamma episodic note " + ("C" * 120),
        "alpha episodic note " + ("A" * 120),
        "beta episodic recall note " + ("B" * 120),
    ]
    assert len(metadata["relevant_memory"]["rendered_notes"]) == 3
    assert metadata["relevant_memory"]["rendered_count"] == 3
    assert metadata["relevant_memory"]["rendered_notes"][0].startswith("gamma episodi")
    assert metadata["relevant_memory"]["rendered_notes"][1].startswith("alpha episodi")
    assert metadata["relevant_memory"]["rendered_notes"][2].startswith("beta episodi")
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]
    assert len([line for line in relevant_section.splitlines() if line.startswith("- ")]) == 3
    assert "alpha episodi" in relevant_section
    assert "beta episodic" in relevant_section
    assert "gamma episodi" in relevant_section
    assert "older unmatched note" not in relevant_section


def test_context_manager_preserves_current_request_when_over_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    agent.memory.retrieval_view = lambda query, limit=3: "Relevant memory:\n" + "\n".join(f"- {i} " + ("C" * 220) for i in range(5))
    agent.history_text = lambda: "Transcript:\n" + "\n".join(f"[user] {i} " + ("D" * 220) for i in range(5))

    request = "please preserve this request exactly"
    prompt, metadata = ContextManager(
        agent,
        total_budget=250,
        section_budgets={
            "prefix": 80,
            "memory": 80,
            "relevant_memory": 80,
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
    agent.memory.set_file_summary("sample.txt", "alpha | beta")
    agent.memory.remember_file("sample.txt")

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
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nMemory:", 1)[0]

    assert "sample.txt -> alpha | beta" not in transcript
    assert metadata["history"]["collapsed_duplicate_reads"] == 1
    assert metadata["history"]["reused_file_summary_count"] == 0


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
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nMemory:", 1)[0]

    assert "START pytest" in transcript
    assert "FINAL ERROR summary" in transcript
    assert "中间内容已裁剪" in transcript
    assert metadata["history"]["summarized_tool_count"] == 1
    assert metadata["history"]["head_tail_clipped_tool_count"] == 1
    assert metadata["history"]["reused_file_summary_count"] == 0


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
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nMemory:", 1)[0]

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
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nMemory:", 1)[0]

    assert "KEEP-HEAD-0" in transcript
    assert "KEEP-HEAD-1" in transcript
    assert "KEEP-HEAD-2" in transcript
    assert metadata["sections"]["history"]["estimated_tokens"] <= metadata["sections"]["history"]["budget_tokens"]
    assert metadata["history"]["final_fallback_clipped_count"] > 0


def test_context_manager_relevant_memory_can_mix_durable_notes(tmp_path):
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
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]

    assert "Use constrained tools instead of guessing." in relevant_section
    assert any("Use constrained tools instead of guessing." in item for item in metadata["relevant_memory"]["selected_notes"])
    assert metadata["relevant_memory"]["selected_durable_count"] == 1
    assert metadata["relevant_memory"]["selected_sources"] == ["project-conventions"]
    assert metadata["relevant_memory"]["selected_kinds"] == ["durable"]
