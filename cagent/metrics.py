import json
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import load_project_env, provider_env
from .context_manager import CHECKPOINT_SECTION, CURRENT_REQUEST_SECTION, ContextManager, estimate_tokens
from .evaluator import run_fixed_benchmark
from .models import AnthropicCompatibleModelClient, FakeModelClient, OpenAICompatibleModelClient
from .runtime import CAgent, SessionStore
from .workspace import WorkspaceContext

METRICS_SCHEMA_VERSION = 2
DEFAULT_HARNESS_REGRESSION_V2_PATH = Path("artifacts/harness-regression-v2.json")
DEFAULT_CONTEXT_ABLATION_V2_PATH = Path("artifacts/context-ablation-v2.json")
DEFAULT_CONTEXT_COMPRESSION_V3_PATH = Path("artifacts/context-compression-v3.json")
DEFAULT_MEMORY_ABLATION_V2_PATH = Path("artifacts/memory-ablation-v2.json")
DEFAULT_RECOVERY_ABLATION_V2_PATH = Path("artifacts/recovery-ablation-v2.json")
DEFAULT_PROMPT_CACHE_LAYOUT_PATH = Path("artifacts/prompt-cache-layout-v1.json")
DEFAULT_CORE_REPORT_PATH = Path("docs/metrics/cagent-benchmark-core-report.md")
DEFAULT_CONTEXT_COMPRESSION_V3_REPORT_PATH = Path("docs/metrics/context-compression-v3-report.md")
PROMPT_CACHE_CONTEXT_WINDOW_CHARS = 100_000
PROMPT_CACHE_TOKEN_CHARS = 4
PROMPT_CACHE_HIT_TOKEN_THRESHOLD = 256
PROMPT_CACHE_HIT_COVERAGE_THRESHOLD = 0.75
PROMPT_CACHE_SIZE_GROUPS = {
    "short": 3_000,
    "medium": 15_000,
    "long": 50_000,
}
PROMPT_CACHE_SECTION_ORDERS = {
    "current": ("prefix", "history", "memory", "relevant_memory", CURRENT_REQUEST_SECTION),
    "volatile_before_history": ("prefix", "memory", "relevant_memory", "history", CURRENT_REQUEST_SECTION),
    "request_before_memory": ("prefix", "history", CURRENT_REQUEST_SECTION, "memory", "relevant_memory"),
    "prefix_only": ("prefix", CURRENT_REQUEST_SECTION),
}


def _safe_mean(values):
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def _safe_ratio(numerator, denominator):
    if not denominator:
        return 0.0
    return numerator / denominator


def _parse_iso8601(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def aggregate_benchmark_artifact(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = list(payload.get("rows", []))
    summary = dict(payload.get("summary", {}))
    task_count = int(summary.get("total_tasks", len(rows) or 0))
    tool_steps = [int(row.get("tool_steps", 0)) for row in rows]
    attempts = [int(row.get("attempts", 0)) for row in rows]
    categories = {}
    for row in rows:
        category = str(row.get("category", "")).strip()
        if not category:
            continue
        categories[category] = categories.get(category, 0) + 1
    return {
        "task_count": task_count,
        "passed": int(summary.get("passed", 0)),
        "failed": int(summary.get("failed", 0)),
        "pass_rate": float(summary.get("pass_rate", 0.0)),
        "within_budget": int(summary.get("within_budget", 0)),
        "verifier_passes": int(summary.get("verifier_passes", 0)),
        "failure_category_counts": dict(summary.get("failure_category_counts", {})),
        "avg_tool_steps": _safe_mean(tool_steps),
        "avg_attempts": _safe_mean(attempts),
        "category_counts": categories,
        "rows": rows,
    }


def _infer_run_duration_ms(events):
    finished = next((event for event in reversed(events) if event.get("event") == "run_finished"), None)
    if finished and finished.get("run_duration_ms") is not None:
        return float(finished["run_duration_ms"])
    started = next((event for event in events if event.get("event") == "run_started"), None)
    if not started or not finished:
        return 0.0
    start_dt = _parse_iso8601(started.get("created_at"))
    end_dt = _parse_iso8601(finished.get("created_at"))
    if start_dt is None or end_dt is None:
        return 0.0
    return max(0.0, (end_dt - start_dt).total_seconds() * 1000.0)


def aggregate_run_artifacts(runs_root):
    runs_root = Path(runs_root)
    run_dirs = sorted(path for path in runs_root.glob("*") if path.is_dir())
    reports = []
    tool_status_counts = {}
    tool_name_counts = {}
    security_event_counts = {}
    run_durations = []
    tool_durations = []
    prompt_durations = []
    stop_reasons = {}

    for run_dir in run_dirs:
        report_path = run_dir / "report.json"
        trace_path = run_dir / "trace.jsonl"
        if report_path.exists():
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
        events = []
        if trace_path.exists():
            events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        run_durations.append(_infer_run_duration_ms(events))
        for event in events:
            if event.get("event") == "prompt_built" and event.get("duration_ms") is not None:
                prompt_durations.append(float(event["duration_ms"]))
            if event.get("event") != "tool_executed":
                continue
            tool_name = str(event.get("name", "")).strip()
            if tool_name:
                tool_name_counts[tool_name] = tool_name_counts.get(tool_name, 0) + 1
            tool_status = str(event.get("tool_status", "")).strip()
            if tool_status:
                tool_status_counts[tool_status] = tool_status_counts.get(tool_status, 0) + 1
            security_event = str(event.get("security_event_type", "")).strip()
            if security_event:
                security_event_counts[security_event] = security_event_counts.get(security_event, 0) + 1
            if event.get("duration_ms") is not None:
                tool_durations.append(float(event["duration_ms"]))

    tool_steps = [int(report.get("tool_steps", 0)) for report in reports]
    attempts = [int(report.get("attempts", 0)) for report in reports]
    prompt_chars = [int((report.get("prompt_metadata") or {}).get("prompt_chars", 0)) for report in reports]
    cached_tokens = [int((report.get("prompt_metadata") or {}).get("cached_tokens", 0) or 0) for report in reports]
    cache_hits = [bool((report.get("prompt_metadata") or {}).get("cache_hit")) for report in reports]
    input_tokens = [int((report.get("prompt_metadata") or {}).get("input_tokens", 0) or 0) for report in reports]
    prefix_reused = [
        not bool((report.get("prompt_metadata") or {}).get("prefix_changed"))
        for report in reports
        if "prefix_changed" in (report.get("prompt_metadata") or {})
    ]
    for report in reports:
        stop_reason = str(report.get("stop_reason", "")).strip()
        if stop_reason:
            stop_reasons[stop_reason] = stop_reasons.get(stop_reason, 0) + 1

    return {
        "run_count": len(reports) if reports else len(run_dirs),
        "avg_tool_steps": _safe_mean(tool_steps),
        "avg_attempts": _safe_mean(attempts),
        "avg_prompt_chars": _safe_mean(prompt_chars),
        "cache_hit_rate": _safe_ratio(sum(1 for hit in cache_hits if hit), len(cache_hits)),
        "cached_token_ratio": _safe_ratio(sum(cached_tokens), sum(input_tokens)),
        "avg_cached_tokens": _safe_mean(cached_tokens),
        "prefix_reuse_rate": _safe_ratio(sum(1 for reused in prefix_reused if reused), len(prefix_reused)),
        "tool_status_counts": tool_status_counts,
        "tool_name_counts": tool_name_counts,
        "security_event_counts": security_event_counts,
        "stop_reason_counts": stop_reasons,
        "avg_run_duration_ms": _safe_mean(run_durations),
        "avg_tool_duration_ms": _safe_mean(tool_durations),
        "avg_prompt_build_duration_ms": _safe_mean(prompt_durations),
    }


@contextmanager
def _temporary_feature_flags(agent, updates):
    previous = dict(getattr(agent, "feature_flags", {}))
    merged = dict(previous)
    merged.update(updates)
    agent.feature_flags = merged
    try:
        yield
    finally:
        agent.feature_flags = previous


def measure_feature_ablation_metrics(agent, user_message):
    variants = {
        "full": {},
        "no_context_reduction": {"context_reduction": False},
        "no_memory": {"memory": False, "relevant_memory": False},
    }
    results = {}
    for name, updates in variants.items():
        with _temporary_feature_flags(agent, updates):
            prompt, metadata = agent._build_prompt_and_metadata(user_message)
        results[name] = {
            "prompt_chars": int(metadata.get("prompt_chars", 0)),
            "memory_chars": int(metadata.get("sections", {}).get("memory", {}).get("rendered_chars", 0)),
            "history_chars": int(metadata.get("sections", {}).get("history", {}).get("rendered_chars", 0)),
            "relevant_selected_count": int(metadata.get("relevant_memory", {}).get("selected_count", 0)),
            "budget_reduction_count": len(metadata.get("budget_reductions", [])),
            "current_request_preserved": prompt.endswith(f"Current user request:\n{user_message}"),
        }
    return results


def build_stress_agent_metrics():
    with tempfile.TemporaryDirectory(prefix="cagent-metrics-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        workspace = WorkspaceContext.build(workspace_root)
        store = SessionStore(workspace_root / ".cagent" / "sessions")
        agent = CAgent(
            model_client=FakeModelClient([]),
            workspace=workspace,
            session_store=store,
            approval_policy="auto",
        )
        for index in range(12):
            agent.memory.append_note(
                f"stress-note-{index}-" + ("A" * 180),
                tags=("recall",),
                created_at=f"2026-04-08T10:{index:02d}:00+00:00",
            )
            agent.record(
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"stress-history-{index}-" + ("B" * 220),
                    "created_at": f"2026-04-08T11:{index:02d}:00+00:00",
                }
            )
        return measure_feature_ablation_metrics(agent, "recall")


class _MemoryExperimentModelClient(FakeModelClient):
    def __init__(self, expected_fact, filename):
        super().__init__([])
        self.expected_fact = str(expected_fact).strip().lower()
        self.filename = str(filename).strip()
        self.phase = "bootstrap_tool"
        self.followup_reads = 0

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        self.last_completion_metadata = {}
        if self.phase == "bootstrap_tool":
            self.phase = "bootstrap_final"
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'
        if self.phase == "bootstrap_final":
            self.phase = "question"
            return "<final>Done.</final>"
        if self.phase == "question":
            prompt_lower = prompt.lower()
            memory_view = ""
            if "memory:" in prompt_lower and "\n\nrelevant memory:" in prompt_lower:
                memory_view = prompt_lower.split("memory:", 1)[1].split("\n\nrelevant memory:", 1)[0]
            relevant_view = ""
            if "relevant memory:" in prompt_lower and "\n\ntranscript:" in prompt_lower:
                relevant_view = prompt_lower.split("relevant memory:", 1)[1].split("\n\ntranscript:", 1)[0]
            if self.expected_fact in memory_view or self.expected_fact in relevant_view:
                return f"<final>{self.expected_fact.capitalize()}.</final>"
            self.phase = "question_after_read"
            self.followup_reads += 1
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'
        if self.phase == "question_after_read":
            self.phase = "done"
            return f"<final>{self.expected_fact.capitalize()}.</final>"
        return f"<final>{self.expected_fact.capitalize()}.</final>"


def _build_memory_experiment_agent(workspace_root, expected_fact, filename):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".cagent" / "sessions")
    return CAgent(
        model_client=_MemoryExperimentModelClient(expected_fact, filename),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def _set_irrelevant_memory(agent):
    state = agent.memory.to_dict()
    state["episodic_notes"] = [
        {
            "text": "team mascot is blue",
            "tags": ["unrelated"],
            "source": "other.txt",
            "created_at": "2026-04-08T10:00:00+00:00",
            "note_index": 0,
        }
    ]
    state["notes"] = ["team mascot is blue"]
    state["file_summaries"] = {}
    agent.memory.state = state
    agent.session["memory"] = agent.memory.to_dict()


def _run_memory_variant(mode):
    with tempfile.TemporaryDirectory(prefix="cagent-memory-experiment-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        (workspace_root / "facts.txt").write_text("deploy key is red\n", encoding="utf-8")
        agent = _build_memory_experiment_agent(workspace_root, "deploy key is red", "facts.txt")
        assert agent.ask("Read facts.txt and remember the key fact.") == "Done."

        if mode == "memory_off":
            agent.feature_flags["memory"] = False
            agent.feature_flags["relevant_memory"] = False
        elif mode == "memory_irrelevant":
            _set_irrelevant_memory(agent)

        result = agent.ask("What color is the deploy key?")
        task_state = agent.current_task_state
        model_client = agent.model_client
        return {
            "correct": result.strip().lower() == "deploy key is red.",
            "tool_steps": int(task_state.tool_steps),
            "attempts": int(task_state.attempts),
            "repeated_reads": int(getattr(model_client, "followup_reads", 0)),
        }


def run_memory_dependency_experiment(repetitions=3):
    variants = {
        "memory_on": [],
        "memory_off": [],
        "memory_irrelevant": [],
    }
    for _ in range(int(repetitions)):
        for variant in variants:
            variants[variant].append(_run_memory_variant(variant))

    results = {}
    for variant, rows in variants.items():
        results[variant] = {
            "repeated_reads": sum(row["repeated_reads"] for row in rows),
            "avg_tool_steps": _safe_mean(row["tool_steps"] for row in rows),
            "avg_attempts": _safe_mean(row["attempts"] for row in rows),
            "correct_rate": _safe_ratio(sum(1 for row in rows if row["correct"]), len(rows)),
        }
    return results


MEMORY_EXPERIMENT_TASKS = [
    {"id": "fact_color", "category": "fact_lookup", "filename": "facts.txt", "fact": "deploy key is red"},
    {"id": "fact_api", "category": "fact_lookup", "filename": "settings.txt", "fact": "api base path is /v1/internal"},
    {"id": "fact_budget", "category": "fact_lookup", "filename": "limits.txt", "fact": "default step budget is 6"},
    {"id": "fact_timeout", "category": "fact_lookup", "filename": "runtime.txt", "fact": "timeout ceiling is 120 seconds"},
    {"id": "edit_intro", "category": "edit_dependency", "filename": "README.md", "fact": "first bullet is the locked intro line"},
    {"id": "edit_token", "category": "edit_dependency", "filename": "sample.txt", "fact": "second token is placeholder"},
    {"id": "edit_field", "category": "edit_dependency", "filename": "config.txt", "fact": "fixed field name is benchmark_schema"},
    {"id": "edit_line", "category": "edit_dependency", "filename": "notes.txt", "fact": "locked marker is on line three"},
    {"id": "history_file", "category": "history_reference", "filename": "history.txt", "fact": "deploy fact came from facts.txt"},
    {"id": "history_line", "category": "history_reference", "filename": "history.txt", "fact": "benchmark note came from line two"},
    {"id": "history_token", "category": "history_reference", "filename": "history.txt", "fact": "placeholder token was beta"},
    {"id": "history_tool", "category": "history_reference", "filename": "history.txt", "fact": "inspection tool was read_file"},
]


def _write_memory_task_files(workspace_root, task):
    filename = task["filename"]
    payload = task["fact"]
    (workspace_root / filename).write_text(payload + "\n", encoding="utf-8")


def _bootstrap_prompt(task):
    return f"Read {task['filename']} and remember the key fact."


def _followup_prompt(task):
    if task["category"] == "fact_lookup":
        return f"What does {task['filename']} say?"
    if task["category"] == "edit_dependency":
        return f"Use the remembered constraint from {task['filename']} to continue without rereading."
    return f"What was the conclusion we already established from {task['filename']}?"


def _set_irrelevant_memory_for_task(agent):
    state = agent.memory.to_dict()
    state["episodic_notes"] = [
        {
            "text": "the team mascot is blue",
            "tags": ["unrelated"],
            "source": "other.txt",
            "created_at": "2026-04-08T10:00:00+00:00",
            "note_index": 0,
        }
    ]
    state["notes"] = ["the team mascot is blue"]
    state["file_summaries"] = {}
    agent.memory.state = state
    agent.session["memory"] = agent.memory.to_dict()


def _run_memory_task_variant(task, variant):
    with tempfile.TemporaryDirectory(prefix="cagent-memory-large-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        _write_memory_task_files(workspace_root, task)
        agent = _build_memory_experiment_agent(workspace_root, task["fact"], task["filename"])
        assert agent.ask(_bootstrap_prompt(task)) == "Done."
        if variant == "memory_off":
            agent.feature_flags["memory"] = False
            agent.feature_flags["relevant_memory"] = False
        elif variant == "memory_irrelevant":
            _set_irrelevant_memory_for_task(agent)
        result = agent.ask(_followup_prompt(task))
        task_state = agent.current_task_state
        return {
            "correct": result.strip().lower() == f"{task['fact']}.",
            "tool_steps": int(task_state.tool_steps),
            "attempts": int(task_state.attempts),
            "repeated_reads": int(getattr(agent.model_client, "followup_reads", 0)),
        }


def run_large_scale_memory_experiment(repetitions=5):
    repetitions = int(repetitions)
    variants = {
        "memory_on": [],
        "memory_off": [],
        "memory_irrelevant": [],
    }
    for task in MEMORY_EXPERIMENT_TASKS:
        for _ in range(repetitions):
            for variant in variants:
                row = _run_memory_task_variant(task, variant)
                row["task_id"] = task["id"]
                row["category"] = task["category"]
                variants[variant].append(row)
    category_counts = {}
    for task in MEMORY_EXPERIMENT_TASKS:
        category_counts[task["category"]] = category_counts.get(task["category"], 0) + 1
    return {
        "task_count": len(MEMORY_EXPERIMENT_TASKS),
        "runs_per_variant": len(MEMORY_EXPERIMENT_TASKS) * repetitions,
        "category_counts": category_counts,
        "variants": {
            variant: {
                "repeated_reads": sum(row["repeated_reads"] for row in rows),
                "avg_tool_steps": _safe_mean(row["tool_steps"] for row in rows),
                "avg_attempts": _safe_mean(row["attempts"] for row in rows),
                "correct_rate": _safe_ratio(sum(1 for row in rows if row["correct"]), len(rows)),
                "memory_hit_rate": _safe_ratio(sum(1 for row in rows if row["repeated_reads"] == 0), len(rows)),
            }
            for variant, rows in variants.items()
        },
        "rows": variants,
    }


def run_context_stress_matrix(repetitions=5):
    repetitions = int(repetitions)
    history_levels = [("short", 4), ("medium", 12), ("long", 24)]
    note_levels = [("low", 2), ("high", 10)]
    request_levels = [("short", "recall"), ("long", "recall the relevant benchmark fact without dropping the latest request details")]
    configs = []

    for history_label, history_count in history_levels:
        for note_label, note_count in note_levels:
            for request_label, request_text in request_levels:
                per_run = []
                for _ in range(repetitions):
                    with tempfile.TemporaryDirectory(prefix="cagent-context-matrix-") as temp_dir:
                        workspace_root = Path(temp_dir)
                        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                        workspace = WorkspaceContext.build(workspace_root)
                        store = SessionStore(workspace_root / ".cagent" / "sessions")
                        agent = CAgent(
                            model_client=FakeModelClient([]),
                            workspace=workspace,
                            session_store=store,
                            approval_policy="auto",
                        )
                        for index in range(note_count):
                            agent.memory.append_note(
                                f"matrix-note-{index}-" + ("A" * 180),
                                tags=("recall",),
                                created_at=f"2026-04-08T10:{index:02d}:00+00:00",
                            )
                        for index in range(history_count):
                            agent.record(
                                {
                                    "role": "user" if index % 2 == 0 else "assistant",
                                    "content": f"matrix-history-{index}-" + ("B" * 220),
                                    "created_at": f"2026-04-08T11:{index:02d}:00+00:00",
                                }
                            )
                        metrics = measure_feature_ablation_metrics(agent, request_text)
                        full_chars = metrics["full"]["prompt_chars"]
                        raw_chars = metrics["no_context_reduction"]["prompt_chars"]
                        ratio = _safe_ratio(raw_chars - full_chars, raw_chars)
                        per_run.append(
                            {
                                "full_prompt_chars": full_chars,
                                "raw_prompt_chars": raw_chars,
                                "compression_ratio": ratio,
                                "current_request_preserved": bool(metrics["full"]["current_request_preserved"]),
                            }
                        )
                configs.append(
                    {
                        "id": f"{history_label}-{note_label}-{request_label}",
                        "history_level": history_label,
                        "note_level": note_label,
                        "request_level": request_label,
                        "avg_prompt_compression_ratio": _safe_mean(item["compression_ratio"] for item in per_run),
                        "avg_full_prompt_chars": _safe_mean(item["full_prompt_chars"] for item in per_run),
                        "avg_raw_prompt_chars": _safe_mean(item["raw_prompt_chars"] for item in per_run),
                        "current_request_preserved_rate": _safe_ratio(
                            sum(1 for item in per_run if item["current_request_preserved"]),
                            len(per_run),
                        ),
                    }
                )
    ratios = [config["avg_prompt_compression_ratio"] for config in configs]
    full_chars = [config["avg_full_prompt_chars"] for config in configs]
    raw_chars = [config["avg_raw_prompt_chars"] for config in configs]
    return {
        "config_count": len(configs),
        "configs": configs,
        "summary": {
            "avg_full_prompt_chars": _safe_mean(full_chars),
            "avg_raw_prompt_chars": _safe_mean(raw_chars),
            "avg_prompt_compression_ratio": _safe_mean(ratios),
            "max_prompt_compression_ratio": max(ratios) if ratios else 0.0,
            "min_prompt_compression_ratio": min(ratios) if ratios else 0.0,
            "current_request_preserved_rate": _safe_ratio(
                sum(1 for config in configs if config["current_request_preserved_rate"] == 1.0),
                len(configs),
            ),
        },
    }


CONTEXT_COMPRESSION_V3_HISTORY_LEVELS = {
    "short": 16,
    "medium": 36,
    "long": 72,
}
CONTEXT_COMPRESSION_V3_TOOL_DENSITIES = ("low", "high")
CONTEXT_COMPRESSION_V3_REQUESTS = {
    "short": "Recall the compressed benchmark facts and keep the latest request intact.",
    "long": "Recall the compressed benchmark facts and keep the latest request intact. "
    + ("Do not drop this current request sentence. " * 42).strip(),
}


def _context_compression_v3_agent(workspace_root, history_count, tool_density):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".cagent" / "sessions")
    agent = CAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )
    agent.context_manager = ContextManager(
        agent,
        total_budget=20_000,
        context_window_tokens=24_000,
        output_reserve_tokens=2_000,
        safety_margin_tokens=2_000,
        section_budgets={"prefix": 800, "memory": 500, "relevant_memory": 500, "history": 12_000},
        section_floors={"prefix": 200, "memory": 120, "relevant_memory": 120, "history": 3_000},
        section_max={"current_request": 2_000},
        current_request_soft_max_tokens=1_600,
        min_history_tokens=80,
    )
    for index in range(4):
        agent.memory.append_note(
            f"context-compression-v3-note-{index}: benchmark fact {index}",
            tags=("compression", "benchmark"),
            created_at=f"2026-06-23T09:{index:02d}:00+00:00",
        )
    _seed_context_compression_v3_history(agent, history_count, tool_density)
    return agent


def _seed_context_compression_v3_history(agent, history_count, tool_density):
    high_density = str(tool_density) == "high"
    tool_every = 3 if high_density else 9
    long_tool = "\n".join(
        f"log line {line}: verbose test output and repeated diagnostic payload"
        for line in range(90 if high_density else 30)
    )
    for index in range(int(history_count)):
        if index % tool_every == 2:
            if high_density and index % 2 == 0:
                name = "read_file"
                args = {"path": "cagent/runtime.py"}
                content = (
                    "Completed: inspected cagent/runtime.py for context distillation behavior.\n"
                    + ("runtime source excerpt " * 260)
                    + "\nTAIL: runtime context marker details preserved"
                )
            else:
                name = "run_shell"
                args = {"command": "pytest tests/test_context_manager.py -q"}
                content = (
                    "pytest failed because marker format missed details line\n"
                    + long_tool
                    + "\nTAIL: AssertionError expected details: see Relevant memory"
                )
            agent.record({"role": "tool", "name": name, "args": args, "content": content, "created_at": f"2026-06-23T10:{index:02d}:00+00:00"})
            continue

        role = "user" if index % 2 == 0 else "assistant"
        if index == 4:
            content = "Completed: added token-aware head-tail clipping for old tool results. " + ("completed detail " * 90)
        elif index == 6:
            content = "Failed: pytest failed before the distilled marker included the details line. " + ("failed detail " * 90)
        elif index == 7:
            content = "Excluded: do not use a subagent for deterministic compression metrics. " + ("excluded detail " * 90)
        elif index == 10:
            content = "Completed: protected current_request from silent clipping in compression tests. " + ("request detail " * 90)
        else:
            content = f"context-v3-history-{index}: " + ("ordinary history filler " * (90 if high_density else 45))
        agent.record({"role": role, "content": content, "created_at": f"2026-06-23T10:{index:02d}:30+00:00"})


def _extract_stub_facts(candidate):
    buckets = {"completed": [], "failed": [], "excluded": []}
    for item in candidate:
        text = str(item.get("content", ""))
        lowered = text.lower()
        if "completed:" in lowered:
            buckets["completed"].append(_clip_fact(text.split(":", 1)[1]))
        if "failed:" in lowered or "pytest failed" in lowered:
            buckets["failed"].append(_clip_fact(text.split("\n", 1)[0]))
        if "excluded:" in lowered or "do not" in lowered:
            buckets["excluded"].append(_clip_fact(text.split(":", 1)[1] if ":" in text else text))
    return {key: values[:3] for key, values in buckets.items()}


def _clip_fact(text, limit=60):
    compact = " ".join(str(text).split())
    return compact[:limit].rstrip()


def _apply_deterministic_context_distillation(agent, prompt_metadata, user_message, config_id):
    history_meta = dict(prompt_metadata.get("history", {}) or {})
    start = history_meta.get("distillation_candidate_start")
    end = history_meta.get("distillation_candidate_end")
    count = int(history_meta.get("distillation_candidate_count", 0))
    if start is None or end is None or count <= 0:
        return {"applied": False, "checkpoint_created": False, "process_note_count": 0, "distilled_marker_count": 0}

    history = list(agent.session.get("history", []))
    candidate = history[int(start) : int(end)]
    updates = _extract_stub_facts(candidate)
    if not any(updates.values()):
        updates["completed"] = [f"Distilled {count} middle history items for {config_id}."]

    checkpoint_id = f"ckpt_stub_{config_id.replace('-', '_')}"
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": "",
        "schema_version": "phase1-v1",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "current_goal": str(user_message),
        "completed": list(updates.get("completed", [])),
        "failed": list(updates.get("failed", [])),
        "excluded": list(updates.get("excluded", [])),
        "current_blocker": "",
        "next_step": "No next step recorded.",
        "key_files": [],
        "freshness": {},
        "summary": f"context_compression_v3_stub: distilled {count} history items",
        "runtime_identity": agent.current_runtime_identity(),
    }
    checkpoints = agent.session.setdefault("checkpoints", {"current_id": "", "items": {}})
    checkpoints.setdefault("items", {})[checkpoint_id] = checkpoint
    checkpoints["current_id"] = checkpoint_id

    note_count = 0
    for key in ("completed", "failed", "excluded"):
        values = updates.get(key, [])
        if values:
            agent.memory.append_note(values[0], tags=("process", "context-compression-v3", key), source="context_compression_v3_stub", kind="process")
            note_count = 1
            break
    agent.session["memory"] = agent.memory.to_dict()

    marker = {
        "role": "assistant",
        "content": agent.build_distilled_history_marker(count, updates),
        "created_at": datetime.utcnow().isoformat() + "Z",
        "metadata": {"kind": "distilled_history", "items": count, "checkpoint_id": checkpoint_id},
    }
    agent.session["history"] = history[: int(start)] + [marker] + history[int(end) :]
    agent.session_path = agent.session_store.save(agent.session)
    return {"applied": True, "checkpoint_created": True, "process_note_count": note_count, "distilled_marker_count": 1}


def _context_compression_variant_metrics(agent, user_message, variant_name, raw_tokens=None, v1_tokens=None, config_id=""):
    if variant_name == "raw_no_compression":
        updates = {"context_reduction": False, "context_distillation": False}
    elif variant_name == "compressed_v1":
        updates = {"context_reduction": True, "context_distillation": False}
    else:
        updates = {"context_reduction": True, "context_distillation": True}

    with _temporary_feature_flags(agent, updates):
        prompt, metadata = agent._build_prompt_and_metadata(user_message)
        stub = {"applied": False, "checkpoint_created": False, "process_note_count": 0, "distilled_marker_count": 0}
        if variant_name == "compressed_v2_stub_distill":
            stub = _apply_deterministic_context_distillation(agent, metadata, user_message, config_id)
            if stub["applied"]:
                prompt, metadata = agent._build_prompt_and_metadata(user_message)

    prompt_tokens = int(metadata.get("prompt_tokens_estimated", estimate_tokens(prompt)))
    history_meta = dict(metadata.get("history", {}) or {})
    history_section = dict(metadata.get("sections", {}).get("history", {}) or {})
    denominator = int(raw_tokens or prompt_tokens)
    incremental_denominator = int(v1_tokens or prompt_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "prompt_chars": int(metadata.get("prompt_chars", len(prompt))),
        "history_rendered_tokens": int(history_section.get("estimated_tokens", 0)),
        "history_raw_tokens": int(history_section.get("raw_tokens_estimated", 0)),
        "compression_ratio_vs_raw": _safe_ratio(denominator - prompt_tokens, denominator),
        "v2_incremental_ratio_vs_v1": _safe_ratio(incremental_denominator - prompt_tokens, incremental_denominator)
        if variant_name == "compressed_v2_stub_distill"
        else 0.0,
        "current_request_preserved": prompt.endswith(f"Current user request:\n{user_message}"),
        "distillation_candidate_count": int(history_meta.get("distillation_candidate_count", 0)),
        "distilled_marker_count": int(stub["distilled_marker_count"]),
        "checkpoint_created": bool(stub["checkpoint_created"]),
        "process_note_count": int(stub["process_note_count"]),
        "collapsed_duplicate_tools": int(history_meta.get("collapsed_duplicate_tools", 0)) + int(history_meta.get("collapsed_duplicate_reads", 0)),
        "head_tail_clipped_tool_count": int(history_meta.get("head_tail_clipped_tool_count", 0)),
        "recent_tool_clipped_count": int(history_meta.get("recent_tool_clipped_count", 0)),
        "final_fallback_clipped_count": int(history_meta.get("final_fallback_clipped_count", 0)),
    }


def run_context_compression_v3_matrix(repetitions=3):
    repetitions = int(repetitions)
    configs = []
    for history_label, history_count in CONTEXT_COMPRESSION_V3_HISTORY_LEVELS.items():
        for tool_density in CONTEXT_COMPRESSION_V3_TOOL_DENSITIES:
            for request_label, user_message in CONTEXT_COMPRESSION_V3_REQUESTS.items():
                config_id = f"{history_label}-{tool_density}-{request_label}"
                rows = []
                for repetition in range(repetitions):
                    variant_results = {}
                    for variant_name in ("raw_no_compression", "compressed_v1", "compressed_v2_stub_distill"):
                        with tempfile.TemporaryDirectory(prefix="cagent-context-compression-v3-") as temp_dir:
                            workspace_root = Path(temp_dir)
                            (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                            agent = _context_compression_v3_agent(workspace_root, history_count, tool_density)
                            raw_tokens = variant_results.get("raw_no_compression", {}).get("prompt_tokens")
                            v1_tokens = variant_results.get("compressed_v1", {}).get("prompt_tokens")
                            variant_results[variant_name] = _context_compression_variant_metrics(
                                agent,
                                user_message,
                                variant_name,
                                raw_tokens=raw_tokens,
                                v1_tokens=v1_tokens,
                                config_id=f"{config_id}-{repetition}",
                            )
                    rows.append(variant_results)

                variants = {}
                for variant_name in ("raw_no_compression", "compressed_v1", "compressed_v2_stub_distill"):
                    variant_rows = [row[variant_name] for row in rows]
                    variants[variant_name] = {
                        key: (
                            all(item[key] for item in variant_rows)
                            if isinstance(variant_rows[0].get(key), bool)
                            else _safe_mean(item.get(key, 0) for item in variant_rows)
                        )
                        for key in variant_rows[0]
                    }
                configs.append(
                    {
                        "id": config_id,
                        "history_size": history_label,
                        "history_count": history_count,
                        "tool_density": tool_density,
                        "request_size": request_label,
                        "variants": variants,
                    }
                )
    v1 = [config["variants"]["compressed_v1"] for config in configs]
    v2 = [config["variants"]["compressed_v2_stub_distill"] for config in configs]
    raw = [config["variants"]["raw_no_compression"] for config in configs]
    return {
        "config_count": len(configs),
        "repetitions": repetitions,
        "variants": ["raw_no_compression", "compressed_v1", "compressed_v2_stub_distill"],
        "configs": configs,
        "summary": {
            "avg_raw_prompt_tokens": _safe_mean(item["prompt_tokens"] for item in raw),
            "avg_v1_prompt_tokens": _safe_mean(item["prompt_tokens"] for item in v1),
            "avg_v2_prompt_tokens": _safe_mean(item["prompt_tokens"] for item in v2),
            "avg_v1_compression_ratio": _safe_mean(item["compression_ratio_vs_raw"] for item in v1),
            "avg_v2_compression_ratio": _safe_mean(item["compression_ratio_vs_raw"] for item in v2),
            "max_v2_compression_ratio": max((item["compression_ratio_vs_raw"] for item in v2), default=0.0),
            "min_v2_compression_ratio": min((item["compression_ratio_vs_raw"] for item in v2), default=0.0),
            "avg_v2_incremental_ratio_vs_v1": _safe_mean(item["v2_incremental_ratio_vs_v1"] for item in v2),
            "current_request_preserved_rate": _safe_ratio(sum(1 for item in v2 if item["current_request_preserved"]), len(v2)),
            "distillation_success_rate": _safe_ratio(sum(1 for item in v2 if item["checkpoint_created"]), len(v2)),
            "configs_with_marker_rate": _safe_ratio(sum(1 for item in v2 if item["distilled_marker_count"] > 0), len(v2)),
        },
    }


def run_context_compression_v3(artifact_path=DEFAULT_CONTEXT_COMPRESSION_V3_PATH, repetitions=3):
    payload = run_context_compression_v3_matrix(repetitions=repetitions)
    artifact = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "context-compression-v3",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "metric_unit": "estimated_tokens",
        "distillation_mode": "deterministic_stub",
        "real_model_calls": False,
        "config_count": payload["config_count"],
        "repetitions": payload["repetitions"],
        "variants": payload["variants"],
        "configs": payload["configs"],
        "summary": payload["summary"],
    }
    return _write_json_artifact(artifact_path, artifact)


def write_context_compression_v3_report(
    report_path=DEFAULT_CONTEXT_COMPRESSION_V3_REPORT_PATH,
    artifact_path=DEFAULT_CONTEXT_COMPRESSION_V3_PATH,
):
    artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    summary = artifact["summary"]
    lines = [
        "# Context Compression V3 Report",
        "",
        "## Resume-safe metrics",
        "",
        "- Main benchmark uses deterministic distillation stub; it does not call a real model.",
        f"- Average prompt tokens: raw {summary['avg_raw_prompt_tokens']:.2f} -> v2 {summary['avg_v2_prompt_tokens']:.2f}.",
        f"- Average v2 compression ratio: {summary['avg_v2_compression_ratio']:.2%}.",
        f"- Max v2 compression ratio: {summary['max_v2_compression_ratio']:.2%}.",
        f"- Current request preserved rate: {summary['current_request_preserved_rate']:.2%}.",
        f"- Distillation success rate: {summary['distillation_success_rate']:.2%}.",
        "",
        "## Diagnostic metrics",
        "",
        f"- Average v2 incremental compression vs v1: {summary['avg_v2_incremental_ratio_vs_v1']:.2%}.",
        "- This value includes first-turn checkpoint/memory/marker refill overhead; resume-safe compression claims should use raw -> v2.",
        "",
        "## Notes",
        "",
        "- Ratios use estimated tokens as the primary unit; chars are kept in the JSON artifact for compatibility.",
        "- Real-provider distillation should be reported separately as validation, not as the main compression-rate source.",
    ]
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "\n".join(lines) + "\n"


def _longest_common_prefix_chars(left, right):
    count = 0
    for left_char, right_char in zip(str(left), str(right)):
        if left_char != right_char:
            break
        count += 1
    return count


def _prompt_cache_agent(workspace_root):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".cagent" / "sessions")
    agent = CAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )
    # Keep the synthetic stable prefix below the default cache-hit threshold so
    # the experiment measures whether history sits before volatile sections.
    agent.prefix = "PICO STATIC PREFIX\n" + ("P" * 600)
    return agent


def _set_prompt_cache_memory(agent, memory_epoch, relevant_epoch):
    memory_text = "Memory:\n" + "\n".join(
        [
            f"- working fact epoch={memory_epoch}",
            f"- cached file summary epoch={memory_epoch}: " + ("M" * 160),
        ]
    )
    relevant_notes = [
        {
            "text": f"relevant note epoch={relevant_epoch} item={index} " + ("R" * 120),
            "tags": ["cache"],
            "source": f"note-{index}",
            "kind": "episodic",
            "created_at": f"2026-04-20T10:0{index}:00+00:00",
        }
        for index in range(3)
    ]
    agent.memory.render_memory_text = lambda: memory_text
    agent.memory.retrieval_candidates = lambda query, limit=3: relevant_notes[:limit]


def _seed_prompt_cache_history(agent, target_prompt_chars=3_000, count=8, label="stable"):
    stable_overhead_chars = 1_250
    content_chars = max(120, (int(target_prompt_chars) - stable_overhead_chars) // max(1, int(count)))
    for index in range(count):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"{label}-history-{index}-" + ("H" * content_chars),
                "created_at": f"2026-04-20T11:{index:02d}:00+00:00",
            }
        )


def _render_prompt_cache_sections(agent, user_message):
    selected_notes = agent.memory.retrieval_candidates(user_message, limit=3)
    section_texts = {
        "prefix": str(agent.prefix),
        CHECKPOINT_SECTION: "",
        "memory": str(agent.memory_text()),
        "history": "",
        CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
    }
    rendered = agent.context_manager._render_sections_without_reduction(section_texts, selected_notes=selected_notes)
    return {section: rendered[section].rendered for section in PROMPT_CACHE_SECTION_ORDERS["current"]}


def _assemble_prompt_cache_variant(sections, order):
    spans = {}
    parts = []
    cursor = 0
    for index, section in enumerate(order):
        if index:
            parts.append("\n\n")
            cursor += 2
        text = str(sections[section])
        start = cursor
        parts.append(text)
        cursor += len(text)
        spans[section] = {"start": start, "end": cursor}
    return "".join(parts).strip(), spans


def _first_diff_section(spans, diff_index):
    if diff_index is None:
        return "none"
    ordered = sorted(spans.items(), key=lambda item: item[1]["start"])
    for section, span in ordered:
        if span["start"] <= diff_index < span["end"]:
            return section
        if diff_index < span["start"]:
            return section
    return "none"


def _changed_cache_sections(previous, current):
    changed = []
    previous_sections = previous.get("sections", {})
    current_sections = current.get("sections", {})
    for section in ("prefix", "history", "memory", "relevant_memory"):
        if previous_sections.get(section) != current_sections.get(section):
            changed.append(section)
    return changed


def _summarize_prompt_cache_sequence(sequence, cache_hit_token_threshold, cache_hit_coverage_threshold):
    rows = []
    for index in range(1, len(sequence)):
        previous = sequence[index - 1]
        current = sequence[index]
        common_prefix_chars = _longest_common_prefix_chars(previous["prompt"], current["prompt"])
        cached_tokens = common_prefix_chars // PROMPT_CACHE_TOKEN_CHARS
        input_tokens = max(1, len(current["prompt"]) // PROMPT_CACHE_TOKEN_CHARS)
        cached_token_ratio = _safe_ratio(cached_tokens, input_tokens)
        changed_sections = _changed_cache_sections(previous, current)
        threshold_hit = cached_tokens >= cache_hit_token_threshold and cached_token_ratio >= cache_hit_coverage_threshold
        exact_reuse = not changed_sections
        miss_reason = "hit"
        if not threshold_hit:
            if cached_tokens < cache_hit_token_threshold:
                miss_reason = "below_token_threshold"
            else:
                miss_reason = "below_coverage_threshold"
        rows.append(
            {
                "turn_index": index,
                "common_prefix_chars": common_prefix_chars,
                "cached_tokens": cached_tokens,
                "input_tokens": input_tokens,
                "cached_token_ratio": cached_token_ratio,
                "threshold_hit": threshold_hit,
                "exact_reuse": exact_reuse,
                "changed_sections": changed_sections,
                "miss_reason": miss_reason,
                "first_diff_section": _first_diff_section(current["spans"], common_prefix_chars),
                "current_request_preserved": current["current_request_marker"] in current["prompt"],
            }
        )

    cached_tokens = [row["cached_tokens"] for row in rows]
    common_prefix_chars = [row["common_prefix_chars"] for row in rows]
    prompt_chars = [len(item["prompt"]) for item in sequence]
    first_diff_counts = {}
    changed_section_counts = {}
    miss_reason_counts = {}
    for row in rows:
        section = row["first_diff_section"]
        first_diff_counts[section] = first_diff_counts.get(section, 0) + 1
        miss_reason = row["miss_reason"]
        miss_reason_counts[miss_reason] = miss_reason_counts.get(miss_reason, 0) + 1
        for changed_section in row["changed_sections"]:
            changed_section_counts[changed_section] = changed_section_counts.get(changed_section, 0) + 1
    return {
        "turn_count": len(sequence),
        "transition_count": len(rows),
        "cache_hit_rate": _safe_mean(row["cached_token_ratio"] for row in rows),
        "threshold_hit_rate": _safe_ratio(sum(1 for row in rows if row["threshold_hit"]), len(rows)),
        "exact_reuse_rate": _safe_ratio(sum(1 for row in rows if row["exact_reuse"]), len(rows)),
        "avg_cached_tokens": _safe_mean(cached_tokens),
        "min_cached_tokens": min(cached_tokens) if cached_tokens else 0,
        "cached_token_ratio": _safe_ratio(sum(row["cached_tokens"] for row in rows), sum(row["input_tokens"] for row in rows)),
        "avg_common_prefix_chars": _safe_mean(common_prefix_chars),
        "min_prompt_chars": min(prompt_chars) if prompt_chars else 0,
        "avg_prompt_chars": _safe_mean(prompt_chars),
        "max_prompt_chars": max(prompt_chars) if prompt_chars else 0,
        "first_diff_section": max(first_diff_counts, key=first_diff_counts.get) if first_diff_counts else "none",
        "first_diff_section_counts": first_diff_counts,
        "changed_section_counts": changed_section_counts,
        "cache_miss_reason_counts": miss_reason_counts,
        "current_request_preserved_rate": _safe_ratio(sum(1 for row in rows if row["current_request_preserved"]), len(rows)),
        "rows": rows,
    }


def _build_prompt_cache_sequences(scenario_id, target_prompt_chars=3_000, repetitions=1):
    sequences = {variant: [] for variant in PROMPT_CACHE_SECTION_ORDERS}
    total_turns = 20
    for repetition in range(int(repetitions)):
        with tempfile.TemporaryDirectory(prefix="cagent-prompt-cache-layout-") as temp_dir:
            workspace_root = Path(temp_dir)
            (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
            agent = _prompt_cache_agent(workspace_root)
            _seed_prompt_cache_history(
                agent,
                target_prompt_chars=target_prompt_chars,
                count=8,
                label=f"{scenario_id}-{repetition}",
            )

            for turn in range(total_turns):
                if scenario_id == "volatile_tail_stability":
                    memory_epoch = turn
                    relevant_epoch = turn
                    request = f"short cache request turn={turn}"
                elif scenario_id == "append_only_history_growth":
                    memory_epoch = turn // 3
                    relevant_epoch = turn // 2
                    request = f"append only request turn={turn}"
                    if turn:
                        agent.record(
                            {
                                "role": "user" if turn % 2 == 0 else "assistant",
                                "content": f"appended-history-{turn}-" + ("A" * 180),
                                "created_at": f"2026-04-20T12:{turn:02d}:00+00:00",
                            }
                        )
                else:
                    memory_rate = int(scenario_id.split("memory", 1)[1].split("_", 1)[0])
                    relevant_rate = int(scenario_id.split("relevant", 1)[1].split("_", 1)[0])
                    long_request = scenario_id.endswith("_long")
                    memory_epoch = turn if memory_rate == 1 else turn // memory_rate
                    relevant_epoch = turn if relevant_rate == 1 else turn // relevant_rate
                    request = f"matrix request turn={turn}"
                    if long_request:
                        request += " " + ("please preserve this long request detail " * 8).strip()

                _set_prompt_cache_memory(agent, memory_epoch, relevant_epoch)
                sections = _render_prompt_cache_sections(agent, request)
                marker = f"Current user request:\n{request}"
                for variant, order in PROMPT_CACHE_SECTION_ORDERS.items():
                    prompt, spans = _assemble_prompt_cache_variant(sections, order)
                    sequences[variant].append(
                        {
                            "prompt": prompt,
                            "spans": spans,
                            "sections": sections,
                            "current_request_marker": marker,
                        }
                    )
    return sequences


def _run_prompt_cache_scenario(
    scenario_id,
    size_group,
    target_prompt_chars,
    repetitions,
    cache_hit_token_threshold,
    cache_hit_coverage_threshold,
):
    sequences = _build_prompt_cache_sequences(
        scenario_id,
        target_prompt_chars=target_prompt_chars,
        repetitions=repetitions,
    )
    return {
        "scenario_id": scenario_id,
        "size_group": size_group,
        "target_prompt_chars": int(target_prompt_chars),
        "variants": {
            variant: {
                "section_order": list(PROMPT_CACHE_SECTION_ORDERS[variant]),
                **_summarize_prompt_cache_sequence(
                    sequence,
                    cache_hit_token_threshold,
                    cache_hit_coverage_threshold,
                ),
            }
            for variant, sequence in sequences.items()
        },
    }


def run_prompt_cache_layout_matrix(
    repetitions=1,
    cache_hit_token_threshold=PROMPT_CACHE_HIT_TOKEN_THRESHOLD,
    cache_hit_coverage_threshold=PROMPT_CACHE_HIT_COVERAGE_THRESHOLD,
):
    base_scenario_ids = [
        "volatile_tail_stability",
        "append_only_history_growth",
    ]
    for memory_rate in (1, 3):
        for relevant_rate in (1, 2):
            for request_length in ("short", "long"):
                base_scenario_ids.append(f"matrix_memory{memory_rate}_relevant{relevant_rate}_{request_length}")

    scenarios = [
        _run_prompt_cache_scenario(
            scenario_id,
            size_group=size_group,
            target_prompt_chars=target_prompt_chars,
            repetitions=repetitions,
            cache_hit_token_threshold=int(cache_hit_token_threshold),
            cache_hit_coverage_threshold=float(cache_hit_coverage_threshold),
        )
        for size_group, target_prompt_chars in PROMPT_CACHE_SIZE_GROUPS.items()
        for scenario_id in base_scenario_ids
    ]
    variants = {}
    for variant in PROMPT_CACHE_SECTION_ORDERS:
        scenario_summaries = [scenario["variants"][variant] for scenario in scenarios]
        variants[variant] = {
            "section_order": list(PROMPT_CACHE_SECTION_ORDERS[variant]),
            "cache_hit_rate": _safe_mean(item["cache_hit_rate"] for item in scenario_summaries),
            "threshold_hit_rate": _safe_mean(item["threshold_hit_rate"] for item in scenario_summaries),
            "exact_reuse_rate": _safe_mean(item["exact_reuse_rate"] for item in scenario_summaries),
            "avg_cached_tokens": _safe_mean(item["avg_cached_tokens"] for item in scenario_summaries),
            "min_cached_tokens": min((item["min_cached_tokens"] for item in scenario_summaries), default=0),
            "cached_token_ratio": _safe_mean(item["cached_token_ratio"] for item in scenario_summaries),
            "avg_common_prefix_chars": _safe_mean(item["avg_common_prefix_chars"] for item in scenario_summaries),
            "avg_prompt_chars": _safe_mean(item["avg_prompt_chars"] for item in scenario_summaries),
            "current_request_preserved_rate": _safe_mean(item["current_request_preserved_rate"] for item in scenario_summaries),
        }
    size_groups = {}
    for size_group in PROMPT_CACHE_SIZE_GROUPS:
        group_scenarios = [scenario for scenario in scenarios if scenario["size_group"] == size_group]
        size_groups[size_group] = {
            "target_prompt_chars": PROMPT_CACHE_SIZE_GROUPS[size_group],
            "scenario_count": len(group_scenarios),
            "variants": {
                variant: {
                    "cache_hit_rate": _safe_mean(scenario["variants"][variant]["cache_hit_rate"] for scenario in group_scenarios),
                    "threshold_hit_rate": _safe_mean(
                        scenario["variants"][variant]["threshold_hit_rate"] for scenario in group_scenarios
                    ),
                    "exact_reuse_rate": _safe_mean(
                        scenario["variants"][variant]["exact_reuse_rate"] for scenario in group_scenarios
                    ),
                    "avg_cached_tokens": _safe_mean(scenario["variants"][variant]["avg_cached_tokens"] for scenario in group_scenarios),
                    "min_cached_tokens": min((scenario["variants"][variant]["min_cached_tokens"] for scenario in group_scenarios), default=0),
                    "avg_common_prefix_chars": _safe_mean(
                        scenario["variants"][variant]["avg_common_prefix_chars"] for scenario in group_scenarios
                    ),
                    "avg_prompt_chars": _safe_mean(scenario["variants"][variant]["avg_prompt_chars"] for scenario in group_scenarios),
                }
                for variant in PROMPT_CACHE_SECTION_ORDERS
            },
        }
    return {
        "scenario_count": len(scenarios),
        "base_scenario_count": len(base_scenario_ids),
        "size_groups": size_groups,
        "scenarios": scenarios,
        "variants": variants,
    }


def _security_agent(workspace_root, approval_policy="auto", read_only=False):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".cagent" / "sessions")
    return CAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        read_only=read_only,
    )


def _scenario_invalid_patch_nonunique(workspace_root):
    (workspace_root / "sample.txt").write_text("beta\nbeta\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    agent.run_tool("patch_file", {"path": "sample.txt", "old_text": "beta", "new_text": "locked"})
    return dict(agent._last_tool_result_metadata)


def _scenario_invalid_patch_missing_field(workspace_root):
    (workspace_root / "sample.txt").write_text("beta\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    agent.run_tool("patch_file", {"path": "sample.txt", "old_text": "beta"})
    return dict(agent._last_tool_result_metadata)


def _scenario_timeout_out_of_range(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("run_shell", {"command": "echo hi", "timeout": 121})
    return dict(agent._last_tool_result_metadata)


def _scenario_empty_command(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("run_shell", {"command": "", "timeout": 20})
    return dict(agent._last_tool_result_metadata)


def _scenario_empty_delegate_task(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("delegate", {"task": "", "max_steps": 2})
    return dict(agent._last_tool_result_metadata)


def _scenario_path_escape_read(workspace_root):
    outside = workspace_root.parent / f"{workspace_root.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    agent.run_tool("read_file", {"path": "../outside.txt"})
    return dict(agent._last_tool_result_metadata)


def _scenario_symlink_escape(workspace_root):
    outside = workspace_root.parent / f"{workspace_root.name}-symlink-target.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (workspace_root / "linked.txt").symlink_to(outside)
    agent = _security_agent(workspace_root)
    agent.run_tool("read_file", {"path": "linked.txt"})
    return dict(agent._last_tool_result_metadata)


def _scenario_search_escape(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("search", {"pattern": "abc", "path": "../outside"})
    return dict(agent._last_tool_result_metadata)


def _scenario_approval_denied(workspace_root):
    agent = _security_agent(workspace_root, approval_policy="never")
    agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})
    return dict(agent._last_tool_result_metadata)


def _scenario_read_only_block(workspace_root):
    agent = _security_agent(workspace_root, read_only=True)
    agent.run_tool("write_file", {"path": "x.txt", "content": "nope"})
    return dict(agent._last_tool_result_metadata)


def _scenario_repeated_call(workspace_root):
    (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    args = {"path": "README.md", "start": 1, "end": 1}
    for _ in range(2):
        result = agent.run_tool("read_file", args)
        agent.record({"role": "tool", "name": "read_file", "args": args, "content": result, "created_at": "2026-04-09T00:00:00+00:00"})
    agent.run_tool("read_file", args)
    return dict(agent._last_tool_result_metadata)


SECURITY_SCENARIOS = [
    ("path_escape_read", _scenario_path_escape_read),
    ("symlink_escape", _scenario_symlink_escape),
    ("search_escape", _scenario_search_escape),
    ("approval_denied_shell", _scenario_approval_denied),
    ("read_only_write", _scenario_read_only_block),
    ("repeated_identical_call", _scenario_repeated_call),
    ("patch_nonunique", _scenario_invalid_patch_nonunique),
    ("patch_missing_new_text", _scenario_invalid_patch_missing_field),
    ("timeout_out_of_range", _scenario_timeout_out_of_range),
    ("empty_delegate_task", _scenario_empty_delegate_task),
]


def run_security_experiment_suite(repetitions=3):
    repetitions = int(repetitions)
    rows = []
    security_event_counts = {}
    tool_error_code_counts = {}
    for scenario_id, runner in SECURITY_SCENARIOS:
        for _ in range(repetitions):
            with tempfile.TemporaryDirectory(prefix="cagent-security-exp-") as temp_dir:
                workspace_root = Path(temp_dir)
                (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                metadata = runner(workspace_root)
                metadata["scenario_id"] = scenario_id
                rows.append(metadata)
                event = str(metadata.get("security_event_type", "")).strip()
                if event:
                    security_event_counts[event] = security_event_counts.get(event, 0) + 1
                error_code = str(metadata.get("tool_error_code", "")).strip()
                if error_code:
                    tool_error_code_counts[error_code] = tool_error_code_counts.get(error_code, 0) + 1
    return {
        "scenario_count": len(SECURITY_SCENARIOS),
        "runs": len(rows),
        "security_event_counts": security_event_counts,
        "tool_error_code_counts": tool_error_code_counts,
        "rows": rows,
    }


def _provider_summary_from_artifact(payload):
    rows = list(payload.get("rows", []))
    cached_tokens = []
    cache_hits = []
    tool_steps = []
    attempts = []
    for row in rows:
        report = row.get("report", {})
        prompt_metadata = report.get("prompt_metadata", {})
        cached_tokens.append(int(prompt_metadata.get("cached_tokens", 0) or 0))
        cache_hits.append(bool(prompt_metadata.get("cache_hit")))
        tool_steps.append(int(row.get("tool_steps", 0)))
        attempts.append(int(row.get("attempts", 0)))
    summary = payload.get("summary", {})
    return {
        "status": "completed",
        "task_count": int(summary.get("total_tasks", len(rows))),
        "pass_rate": float(summary.get("pass_rate", 0.0)),
        "avg_tool_steps": _safe_mean(tool_steps),
        "avg_attempts": _safe_mean(attempts),
        "cache_hit_rate": _safe_ratio(sum(1 for hit in cache_hits if hit), len(cache_hits)),
        "avg_cached_tokens": _safe_mean(cached_tokens),
        "artifact_path": payload.get("_artifact_path", ""),
    }


def _provider_profile(provider):
    load_project_env(Path.cwd())
    if provider == "gpt":
        api_key = provider_env("PICO_OPENAI_API_KEY", ("OPENAI_API_KEY",))
        if not api_key:
            return {"provider": provider, "status": "blocked", "reason": "PICO_OPENAI_API_KEY or OPENAI_API_KEY missing"}
        return {
            "provider": provider,
            "status": "ready",
            "model": provider_env("PICO_OPENAI_MODEL", ("OPENAI_MODEL",), "gpt-5.4"),
            "base_url": provider_env("PICO_OPENAI_API_BASE", ("OPENAI_API_BASE",), "https://api.openai.com/v1"),
            "api_key": api_key,
        }
    if provider == "deepseek":
        api_key = provider_env("PICO_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        if not api_key:
            return {"provider": provider, "status": "blocked", "reason": "PICO_DEEPSEEK_API_KEY or DEEPSEEK_API_KEY missing"}
        return {
            "provider": provider,
            "status": "ready",
            "model": provider_env("PICO_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",), "deepseek-v4-pro"),
            "base_url": provider_env("PICO_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), "https://api.deepseek.com/anthropic"),
            "api_key": api_key,
        }
    api_key = provider_env(
        "PICO_ANTHROPIC_API_KEY",
        ("ANTHROPIC_API_KEY", "PICO_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "PICO_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    if not api_key:
        return {"provider": "claude", "status": "blocked", "reason": "PICO_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY missing"}
    return {
        "provider": "claude",
        "status": "ready",
        "model": provider_env("PICO_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",), "claude-sonnet-4-6"),
        "base_url": provider_env("PICO_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), "https://www.right.codes/claude/v1"),
        "api_key": api_key,
    }


def _make_provider_client(provider):
    profile = _provider_profile(provider)
    if profile["status"] != "ready":
        raise RuntimeError(profile["reason"])
    timeout = 60
    if provider == "gpt":
        return OpenAICompatibleModelClient(
            model=profile["model"],
            base_url=profile["base_url"],
            api_key=profile["api_key"],
            temperature=0.0,
            timeout=timeout,
        )
    return AnthropicCompatibleModelClient(
        model=profile["model"],
        base_url=profile["base_url"],
        api_key=profile["api_key"],
        temperature=0.0,
        timeout=timeout,
    )


def _normalize_text(value):
    text = str(value).strip().lower()
    while text.endswith((".", "!", "?", "\"", "'")):
        text = text[:-1].strip()
    return text


def run_provider_experiments(benchmark_path, workspace_root, artifact_root, max_new_tokens=64):
    benchmark_path = Path(benchmark_path)
    workspace_root = Path(workspace_root)
    artifact_root = Path(artifact_root)
    providers = []
    for provider_name in ("gpt", "claude", "deepseek"):
        profile = _provider_profile(provider_name)
        if profile["status"] != "ready":
            providers.append(profile)
            continue
        if provider_name == "gpt":
            def factory(task, workspace, profile=profile):
                del task, workspace
                return OpenAICompatibleModelClient(
                    model=profile["model"],
                    base_url=profile["base_url"],
                    api_key=profile["api_key"],
                    temperature=0.0,
                    timeout=300,
                )
        else:
            def factory(task, workspace, profile=profile):
                del task, workspace
                return AnthropicCompatibleModelClient(
                    model=profile["model"],
                    base_url=profile["base_url"],
                    api_key=profile["api_key"],
                    temperature=0.0,
                    timeout=300,
                )
        artifact_path = artifact_root / f"{provider_name}-benchmark.json"
        try:
            payload = run_fixed_benchmark(
                benchmark_path=benchmark_path,
                artifact_path=artifact_path,
                workspace_root=workspace_root / provider_name,
                model_name=profile["provider"],
                model_version=profile["model"],
                max_new_tokens=max_new_tokens,
                model_client_factory=factory,
            )
            payload["_artifact_path"] = str(artifact_path)
            result = _provider_summary_from_artifact(payload)
            result["provider"] = provider_name
            result["model"] = profile["model"]
            providers.append(result)
        except Exception as exc:
            providers.append(
                {
                    "provider": provider_name,
                    "status": "error",
                    "model": profile["model"],
                    "reason": str(exc),
                }
            )
    return {"providers": providers}


def _followup_trace_metrics(agent):
    trace_path = agent.run_store.trace_path(agent.current_task_state)
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    repeated_reads = sum(1 for event in events if event.get("event") == "tool_executed" and event.get("name") == "read_file")
    return repeated_reads


def _inject_memory_noise(agent, rounds=8):
    for index in range(int(rounds)):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"filler-turn-{index}-" + ("context-noise-" * 40),
                "created_at": f"2026-04-09T12:{index:02d}:00+00:00",
            }
        )


def _truncate_read_history(agent):
    updated = []
    for item in agent.session["history"]:
        if item.get("role") == "tool" and item.get("name") == "read_file":
            replacement = dict(item)
            replacement["content"] = f"# {item.get('args', {}).get('path', 'file')}\n(truncated from transcript)"
            updated.append(replacement)
        else:
            updated.append(item)
    agent.session["history"] = updated
    agent.session_path = agent.session_store.save(agent.session)


def _build_real_agent(workspace_root, provider, approval_policy="auto", read_only=False):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".cagent" / "sessions")
    return CAgent(
        model_client=_make_provider_client(provider),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        read_only=read_only,
    )


def run_real_memory_experiment(provider="gpt", repetitions=1):
    repetitions = int(repetitions)
    provider = str(provider)
    variants = {"memory_on": [], "memory_off": [], "memory_irrelevant": []}
    category_counts = {}
    for task in MEMORY_EXPERIMENT_TASKS:
        category_counts[task["category"]] = category_counts.get(task["category"], 0) + 1
        for _ in range(repetitions):
            for variant in variants:
                with tempfile.TemporaryDirectory(prefix="cagent-real-memory-") as temp_dir:
                    workspace_root = Path(temp_dir)
                    (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                    _write_memory_task_files(workspace_root, task)
                    agent = _build_real_agent(workspace_root, provider)
                    agent.ask(f"Read {task['filename']} and remember the exact line. After you know it, reply with Done only.")
                    if variant == "memory_off":
                        agent.feature_flags["memory"] = False
                        agent.feature_flags["relevant_memory"] = False
                    elif variant == "memory_irrelevant":
                        _set_irrelevant_memory_for_task(agent)
                    _inject_memory_noise(agent)
                    _truncate_read_history(agent)
                    if task["category"] == "fact_lookup":
                        prompt = (
                            f"What exact line did you previously read from {task['filename']}? "
                            "Reply with the exact line only. If you are not certain, verify with tools instead of guessing."
                        )
                    elif task["category"] == "edit_dependency":
                        prompt = (
                            f"Before editing, what exact constraint line did you previously read from {task['filename']}? "
                            "Reply with the exact line only. If you are not certain, verify with tools instead of guessing."
                        )
                    else:
                        prompt = (
                            f"What exact conclusion did you already establish from {task['filename']}? "
                            "Reply with the exact line only. If you are not certain, verify with tools instead of guessing."
                        )
                    answer = agent.ask(prompt)
                    variants[variant].append(
                        {
                            "task_id": task["id"],
                            "category": task["category"],
                            "correct": _normalize_text(answer) == _normalize_text(task["fact"]),
                            "tool_steps": int(agent.current_task_state.tool_steps),
                            "attempts": int(agent.current_task_state.attempts),
                            "repeated_reads": _followup_trace_metrics(agent),
                        }
                    )
    return {
        "provider": provider,
        "task_count": len(MEMORY_EXPERIMENT_TASKS),
        "runs_per_variant": len(MEMORY_EXPERIMENT_TASKS) * repetitions,
        "category_counts": category_counts,
        "variants": {
            variant: {
                "repeated_reads": sum(row["repeated_reads"] for row in rows),
                "avg_tool_steps": _safe_mean(row["tool_steps"] for row in rows),
                "avg_attempts": _safe_mean(row["attempts"] for row in rows),
                "correct_rate": _safe_ratio(sum(1 for row in rows if row["correct"]), len(rows)),
            }
            for variant, rows in variants.items()
        },
        "rows": variants,
    }


def run_real_context_experiment(provider="gpt", repetitions=1):
    repetitions = int(repetitions)
    provider = str(provider)
    history_levels = [("short", 4), ("medium", 12), ("long", 24)]
    note_levels = [("low", 2), ("high", 10)]
    request_levels = [
        ("short", "Reply with the target token only."),
        ("long", "Reply with the target token only. Do not restate the prompt, and do not output any extra words."),
    ]
    configs = []
    for history_label, history_count in history_levels:
        for note_label, note_count in note_levels:
            for request_label, request_text in request_levels:
                token = f"TOKEN-{history_label}-{note_label}-{request_label}"
                per_run = []
                for _ in range(repetitions):
                    for variant_name, updates in (("full", {}), ("no_context_reduction", {"context_reduction": False})):
                        with tempfile.TemporaryDirectory(prefix="cagent-real-context-") as temp_dir:
                            workspace_root = Path(temp_dir)
                            (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                            agent = _build_real_agent(workspace_root, provider)
                            for index in range(note_count):
                                note_text = f"target token is {token}" if index == 0 else f"decoy token is DECOY-{index}"
                                agent.memory.append_note(note_text, tags=("token",), created_at=f"2026-04-09T10:{index:02d}:00+00:00")
                            for index in range(history_count):
                                agent.record(
                                    {
                                        "role": "user" if index % 2 == 0 else "assistant",
                                        "content": f"context-history-{index}-" + ("B" * 220),
                                        "created_at": f"2026-04-09T11:{index:02d}:00+00:00",
                                    }
                                )
                            with _temporary_feature_flags(agent, updates):
                                answer = agent.ask(f"What is the target token remembered in the notes? {request_text}")
                            per_run.append(
                                {
                                    "variant": variant_name,
                                    "prompt_chars": int(agent.last_prompt_metadata.get("prompt_chars", 0)),
                                    "correct": token.lower() in _normalize_text(answer),
                                }
                            )
                full_rows = [row for row in per_run if row["variant"] == "full"]
                raw_rows = [row for row in per_run if row["variant"] == "no_context_reduction"]
                avg_full = _safe_mean(row["prompt_chars"] for row in full_rows)
                avg_raw = _safe_mean(row["prompt_chars"] for row in raw_rows)
                configs.append(
                    {
                        "id": f"{history_label}-{note_label}-{request_label}",
                        "history_level": history_label,
                        "note_level": note_label,
                        "request_level": request_label,
                        "avg_full_prompt_chars": avg_full,
                        "avg_raw_prompt_chars": avg_raw,
                        "avg_prompt_compression_ratio": _safe_ratio(avg_raw - avg_full, avg_raw),
                        "full_correct_rate": _safe_ratio(sum(1 for row in full_rows if row["correct"]), len(full_rows)),
                        "raw_correct_rate": _safe_ratio(sum(1 for row in raw_rows if row["correct"]), len(raw_rows)),
                    }
                )
    ratios = [config["avg_prompt_compression_ratio"] for config in configs]
    full_chars = [config["avg_full_prompt_chars"] for config in configs]
    raw_chars = [config["avg_raw_prompt_chars"] for config in configs]
    return {
        "provider": provider,
        "config_count": len(configs),
        "configs": configs,
        "summary": {
            "avg_prompt_compression_ratio": _safe_mean(ratios),
            "max_prompt_compression_ratio": max(ratios) if ratios else 0.0,
            "min_prompt_compression_ratio": min(ratios) if ratios else 0.0,
            "avg_full_prompt_chars": _safe_mean(full_chars),
            "avg_raw_prompt_chars": _safe_mean(raw_chars),
        },
    }


REAL_SECURITY_SCENARIOS = [
    {"id": "approval_denied_shell", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"run_shell","args":{"command":"echo hi","timeout":20}}</tool>', "approval_policy": "never", "read_only": False},
    {"id": "read_only_write", "prompt": '<tool name="write_file" path="blocked.txt"><content>blocked</content></tool>', "approval_policy": "auto", "read_only": True},
    {"id": "read_only_patch", "prompt": '<tool name="patch_file" path="README.md"><old_text>demo</old_text><new_text>patched</new_text></tool>', "approval_policy": "auto", "read_only": True},
    {"id": "path_escape_read", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"read_file","args":{"path":"../outside.txt","start":1,"end":20}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "symlink_escape", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"read_file","args":{"path":"linked.txt","start":1,"end":20}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "search_escape", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"search","args":{"pattern":"abc","path":"../outside"}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "patch_nonunique", "prompt": '<tool name="patch_file" path="sample.txt"><old_text>beta</old_text><new_text>locked</new_text></tool>', "approval_policy": "auto", "read_only": False},
    {"id": "patch_missing_new_text", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"patch_file","args":{"path":"sample.txt","old_text":"beta"}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "timeout_out_of_range", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"run_shell","args":{"command":"echo hi","timeout":121}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "empty_delegate_task", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"delegate","args":{"task":"","max_steps":2}}</tool>', "approval_policy": "auto", "read_only": False},
]


def _setup_real_security_workspace(workspace_root, scenario_id):
    (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
    if scenario_id == "path_escape_read":
        outside = workspace_root.parent / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
    elif scenario_id == "symlink_escape":
        outside = workspace_root.parent / "symlink-target.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (workspace_root / "linked.txt").symlink_to(outside)
    elif scenario_id in {"patch_nonunique", "patch_missing_new_text"}:
        text = "beta\nbeta\n" if scenario_id == "patch_nonunique" else "beta\n"
        (workspace_root / "sample.txt").write_text(text, encoding="utf-8")


def _security_result_row(scenario_id, provider, metadata):
    row = dict(metadata)
    row["scenario_id"] = scenario_id
    row["provider"] = provider
    row.setdefault("tool_status", "")
    row.setdefault("tool_error_code", "")
    row.setdefault("security_event_type", "")
    return row


def _run_real_repeated_call_scenario(provider):
    with tempfile.TemporaryDirectory(prefix="cagent-real-security-repeat-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        agent = _build_real_agent(workspace_root, provider)
        prompt = 'Respond with exactly this tool call and nothing else: <tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>'
        for _ in range(3):
            agent.ask(prompt)
        return _security_result_row("repeated_identical_call", provider, dict(agent._last_tool_result_metadata))


def run_real_security_experiment_suite(provider="gpt", repetitions=1):
    repetitions = int(repetitions)
    provider = str(provider)
    rows = []
    security_event_counts = {}
    tool_error_code_counts = {}

    for _ in range(repetitions):
        rows.append(_run_real_repeated_call_scenario(provider))
        for scenario in REAL_SECURITY_SCENARIOS:
            with tempfile.TemporaryDirectory(prefix="cagent-real-security-") as temp_dir:
                workspace_root = Path(temp_dir)
                _setup_real_security_workspace(workspace_root, scenario["id"])
                agent = _build_real_agent(
                    workspace_root,
                    provider,
                    approval_policy=scenario["approval_policy"],
                    read_only=scenario["read_only"],
                )
                agent.ask(scenario["prompt"])
                rows.append(_security_result_row(scenario["id"], provider, dict(agent._last_tool_result_metadata)))

    for row in rows:
        event = str(row.get("security_event_type", "")).strip()
        if event:
            security_event_counts[event] = security_event_counts.get(event, 0) + 1
        error_code = str(row.get("tool_error_code", "")).strip()
        if error_code:
            tool_error_code_counts[error_code] = tool_error_code_counts.get(error_code, 0) + 1

    return {
        "provider": provider,
        "scenario_count": len(REAL_SECURITY_SCENARIOS) + 1,
        "runs": len(rows),
        "security_event_counts": security_event_counts,
        "tool_error_code_counts": tool_error_code_counts,
        "rows": rows,
    }


def collect_resume_metrics(
    benchmark_artifact_path,
    runs_root,
    provider_experiments=None,
    memory_repetitions=3,
    large_memory_repetitions=5,
    context_repetitions=5,
    security_repetitions=3,
    experiment_mode="synthetic",
    real_provider="gpt",
):
    benchmark = aggregate_benchmark_artifact(benchmark_artifact_path)
    runs = aggregate_run_artifacts(runs_root)
    experiment_mode = str(experiment_mode)
    real_provider = str(real_provider)
    if experiment_mode == "real":
        memory_large = run_real_memory_experiment(provider=real_provider, repetitions=large_memory_repetitions)
        memory = {name: dict(values) for name, values in memory_large["variants"].items()}
        context = run_real_context_experiment(provider=real_provider, repetitions=context_repetitions)
        security = run_real_security_experiment_suite(provider=real_provider, repetitions=security_repetitions)
        stress = {
            "full": {"prompt_chars": int(round(context["summary"].get("avg_full_prompt_chars", 0.0)))},
            "no_context_reduction": {"prompt_chars": int(round(context["summary"].get("avg_raw_prompt_chars", 0.0)))},
        }
    else:
        stress = build_stress_agent_metrics()
        memory = run_memory_dependency_experiment(repetitions=memory_repetitions)
        memory_large = run_large_scale_memory_experiment(repetitions=large_memory_repetitions)
        context = run_context_stress_matrix(repetitions=context_repetitions)
        security = run_security_experiment_suite(repetitions=security_repetitions)
    provider_payload = {"providers": []}
    if provider_experiments:
        provider_payload = json.loads(Path(provider_experiments).read_text(encoding="utf-8"))
    return {
        "experiment_mode": experiment_mode,
        "real_provider": real_provider if experiment_mode == "real" else "",
        "facts": {
            "model_backend_count": 3,
            "tool_count": 7,
            "run_artifact_count": 3,
        },
        "benchmark": benchmark,
        "runs": runs,
        "stress_ablation": stress,
        "memory_experiment": memory,
        "memory_large_experiment": memory_large,
        "context_experiment": context,
        "security_experiment": security,
        "provider_experiments": provider_payload,
        "resume_highlights": [
            f"Built a fixed benchmark harness with {benchmark['task_count']} tasks and automated pass/fail, verifier, and budget summaries.",
            f"Recorded 3 run artifacts per execution and structured runtime metadata across {runs['run_count']} aggregated runs.",
            f"Observed prompt-cache telemetry with average cached tokens of {runs['avg_cached_tokens']:.1f} and cache-hit rate of {runs['cache_hit_rate']:.2%} when available.",
            (
                f"In a real-model long-context experiment ({real_provider}), context reduction shrank average prompt size from "
                f"{stress['no_context_reduction']['prompt_chars']} to {stress['full']['prompt_chars']} chars."
                if experiment_mode == "real"
                else f"In a synthetic long-context stress scenario, context reduction shrank prompt size from {stress['no_context_reduction']['prompt_chars']} to {stress['full']['prompt_chars']} chars."
            ),
            f"In the memory dependency experiment, repeated follow-up reads dropped from {memory['memory_off']['repeated_reads']} to {memory['memory_on']['repeated_reads']}.",
            f"In the large-scale memory experiment, repeated reads dropped from {memory_large['variants']['memory_off']['repeated_reads']} to {memory_large['variants']['memory_on']['repeated_reads']} across {memory_large['task_count']} tasks.",
        ],
    }


def render_resume_metrics_markdown(metrics):
    benchmark = metrics["benchmark"]
    runs = metrics["runs"]
    stress = metrics["stress_ablation"]
    memory = metrics["memory_experiment"]
    memory_large = metrics["memory_large_experiment"]
    context = metrics["context_experiment"]
    security = metrics["security_experiment"]
    provider_payload = metrics.get("provider_experiments", {})
    lines = [
        "# CAgent Resume Metrics",
        "",
        "## Key Numbers",
        f"- Experiment mode: {metrics.get('experiment_mode', 'synthetic')}",
        f"- Model backends: {metrics['facts']['model_backend_count']}",
        f"- Tool types: {metrics['facts']['tool_count']}",
        f"- Fixed benchmark tasks: {benchmark['task_count']}",
        f"- Fixed benchmark pass rate: {benchmark['pass_rate']:.2%}",
        f"- Aggregated runs: {runs['run_count']}",
        f"- Average tool steps per run: {runs['avg_tool_steps']:.2f}",
        f"- Average attempts per run: {runs['avg_attempts']:.2f}",
        f"- Cache hit rate: {runs['cache_hit_rate']:.2%}",
        (
            f"- Real-model prompt chars (full vs no context reduction): {stress['full']['prompt_chars']} / {stress['no_context_reduction']['prompt_chars']}"
            if metrics.get("experiment_mode") == "real"
            else f"- Synthetic prompt chars (full vs no context reduction): {stress['full']['prompt_chars']} / {stress['no_context_reduction']['prompt_chars']}"
        ),
        f"- Memory repeated reads (on vs off): {memory['memory_on']['repeated_reads']} / {memory['memory_off']['repeated_reads']}",
        f"- Large-scale memory tasks: {memory_large['task_count']}",
        f"- Context matrix configs: {context['config_count']}",
        f"- Security scenarios: {security['scenario_count']}",
        "",
        "## Resume Highlights",
    ]
    lines.extend(f"- {line}" for line in metrics["resume_highlights"])
    providers = provider_payload.get("providers", [])
    if providers:
        lines.extend(["", "## Provider Experiments"])
        for provider in providers:
            if provider.get("status") == "completed":
                lines.append(
                    f"- {provider['provider']}: pass_rate={provider['pass_rate']:.2%}, avg_attempts={provider['avg_attempts']:.2f}, avg_tool_steps={provider['avg_tool_steps']:.2f}, cache_hit_rate={provider['cache_hit_rate']:.2%}"
                )
            else:
                lines.append(f"- {provider['provider']}: {provider['status']} ({provider.get('reason', 'unknown')})")
    lines.append("")
    return "\n".join(lines)


def render_large_scale_experiment_report(metrics):
    benchmark = metrics["benchmark"]
    memory_small = metrics["memory_experiment"]
    memory_large = metrics["memory_large_experiment"]
    context = metrics["context_experiment"]
    security = metrics["security_experiment"]
    providers = metrics.get("provider_experiments", {}).get("providers", [])
    report_provider = (
        metrics.get("real_provider")
        or context.get("provider")
        or memory_large.get("provider")
        or security.get("provider")
        or "unknown"
    )
    lines = [
        "# CAgent Large-Scale Experiment Report",
        "",
        "## Executive Summary",
        (
            f"- Experiment mode: real-model (provider: {report_provider})"
            if metrics.get("experiment_mode") == "real"
            else f"- Experiment mode: {metrics.get('experiment_mode', 'synthetic')}"
        ),
        f"- Fixed benchmark tasks: {benchmark['task_count']}",
        f"- Large-scale memory tasks: {memory_large['task_count']}",
        f"- Context stress configurations: {context['config_count']}",
        f"- Security scenarios: {security['scenario_count']}",
        "",
        "## Context Governance",
        (
            f"- Real-model prompt chars ({report_provider}): {metrics['stress_ablation']['full']['prompt_chars']} vs {metrics['stress_ablation']['no_context_reduction']['prompt_chars']}"
            if metrics.get("experiment_mode") == "real"
            else f"- Synthetic stress prompt chars: {metrics['stress_ablation']['full']['prompt_chars']} vs {metrics['stress_ablation']['no_context_reduction']['prompt_chars']}"
        ),
        f"- Average prompt compression ratio across context matrix: {context['summary']['avg_prompt_compression_ratio']:.2%}",
        f"- Max prompt compression ratio across context matrix: {context['summary']['max_prompt_compression_ratio']:.2%}",
        "",
        "## Memory Experiments",
        f"- Small memory experiment repeated reads: {memory_small['memory_on']['repeated_reads']} vs {memory_small['memory_off']['repeated_reads']}",
        f"- Large memory experiment repeated reads: {memory_large['variants']['memory_on']['repeated_reads']} vs {memory_large['variants']['memory_off']['repeated_reads']}",
        f"- Large memory experiment avg tool steps: {memory_large['variants']['memory_on']['avg_tool_steps']:.2f} vs {memory_large['variants']['memory_off']['avg_tool_steps']:.2f}",
        "",
        "## Security Experiments",
        f"- Security event counts: {json.dumps(security['security_event_counts'], sort_keys=True)}",
        f"- Tool error code counts: {json.dumps(security['tool_error_code_counts'], sort_keys=True)}",
        "",
        "## Provider Experiments",
    ]
    if providers:
        for provider in providers:
            if provider.get("status") == "completed":
                lines.append(
                    f"- {provider['provider']}: pass_rate={provider['pass_rate']:.2%}, avg_attempts={provider['avg_attempts']:.2f}, avg_tool_steps={provider['avg_tool_steps']:.2f}, cache_hit_rate={provider['cache_hit_rate']:.2%}"
                )
            else:
                lines.append(f"- {provider['provider']}: {provider['status']} ({provider.get('reason', 'unknown')})")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Resume-Safe Claims",
            f"- Long-context stress scenario: prompt length reduced from {metrics['stress_ablation']['no_context_reduction']['prompt_chars']} to {metrics['stress_ablation']['full']['prompt_chars']}.",
            f"- Large-scale memory experiment: repeated reads reduced from {memory_large['variants']['memory_off']['repeated_reads']} to {memory_large['variants']['memory_on']['repeated_reads']}.",
            f"- Platform facts: {benchmark['task_count']} benchmark tasks, {metrics['facts']['tool_count']} tool types, {metrics['facts']['run_artifact_count']} run artifacts.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json_artifact(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


class _RecoveryScenarioModelClient(FakeModelClient):
    def __init__(self, required_fragments, success_answer):
        super().__init__([])
        self.required_fragments = [str(fragment).lower() for fragment in required_fragments]
        self.success_answer = str(success_answer)

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        self.last_completion_metadata = {}
        prompt_lower = str(prompt).lower()
        if all(fragment in prompt_lower for fragment in self.required_fragments):
            return f"<final>{self.success_answer}</final>"
        return "<final>missing recovery state.</final>"


RECOVERY_ABLATION_TASKS = [
    {
        "id": "checkpoint_resume_goal",
        "category": "checkpoint_resume",
        "setup": "checkpoint_resume",
        "required_fragments": ["task checkpoint:", "current goal: resume the benchmark task", "next step: apply the locked change"],
    },
    {
        "id": "checkpoint_resume_files",
        "category": "checkpoint_resume",
        "setup": "checkpoint_resume",
        "required_fragments": ["task checkpoint:", "current goal: continue from the latest benchmark checkpoint", "key files: sample.txt"],
    },
    {
        "id": "partial_stale_single",
        "category": "partial_stale",
        "setup": "partial_stale_single",
        "required_fragments": ["resume status: partial-stale", "stale paths: sample.txt"],
    },
    {
        "id": "partial_stale_multi",
        "category": "partial_stale",
        "setup": "partial_stale_multi",
        "required_fragments": ["resume status: partial-stale", "stale paths: sample.txt, notes.txt"],
    },
    {
        "id": "workspace_mismatch_fingerprint",
        "category": "workspace_mismatch",
        "setup": "workspace_mismatch",
        "required_fragments": ["resume status: workspace-mismatch", "current goal: recover after workspace drift"],
    },
    {
        "id": "workspace_mismatch_runtime",
        "category": "workspace_mismatch",
        "setup": "workspace_mismatch",
        "required_fragments": ["resume status: workspace-mismatch", "next step: rebuild runtime state from a fresh checkpoint"],
    },
    {
        "id": "schema_mismatch_version",
        "category": "schema_mismatch",
        "setup": "schema_mismatch",
        "required_fragments": ["resume status: schema-mismatch"],
    },
    {
        "id": "schema_mismatch_missing",
        "category": "schema_mismatch",
        "setup": "no_checkpoint",
        "required_fragments": ["resume status: no-checkpoint"],
    },
    {
        "id": "partial_success_shell",
        "category": "partial_success_recovery",
        "setup": "partial_success_shell",
        "required_fragments": ["current blocker: tool_partial_success", "next step: inspect the diff before retry"],
    },
    {
        "id": "partial_success_tool",
        "category": "partial_success_recovery",
        "setup": "partial_success_tool",
        "required_fragments": ["current blocker: tool_failed", "next step: retry after checking the workspace state"],
    },
]


def _build_recovery_agent(workspace_root, required_fragments):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".cagent" / "sessions")
    return CAgent(
        model_client=_RecoveryScenarioModelClient(required_fragments, "recovery state restored."),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=4,
    )


def _apply_recovery_setup(agent, task, workspace_root):
    setup = task["setup"]
    workspace_root = Path(workspace_root)
    (workspace_root / "sample.txt").write_text("alpha\nbeta\ngamma\nplaceholder\n", encoding="utf-8")
    (workspace_root / "notes.txt").write_text("note-one\nnote-two\n", encoding="utf-8")
    agent.session["memory"] = agent.memory.to_dict()

    if setup == "checkpoint_resume":
        agent.memory.remember_file("sample.txt")
        agent.session["memory"] = agent.memory.to_dict()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_resume",
            "items": {
                "ckpt_resume": {
                    "checkpoint_id": "ckpt_resume",
                    "parent_checkpoint_id": "",
                    "schema_version": "phase1-v1",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Resume the benchmark task" if task["id"] == "checkpoint_resume_goal" else "Continue from the latest benchmark checkpoint",
                    "completed": ["Read sample.txt"],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Apply the locked change" if task["id"] == "checkpoint_resume_goal" else "Continue from remembered file anchors",
                    "key_files": [{"path": "sample.txt", "freshness": None}],
                    "freshness": {},
                    "summary": "checkpoint resume benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        if task["id"] == "checkpoint_resume_files":
            agent.session["checkpoints"]["items"]["ckpt_resume"]["key_files"] = [{"path": "sample.txt", "freshness": None}]
        agent.session_store.save(agent.session)
        return

    if setup in {"partial_stale_single", "partial_stale_multi"}:
        agent.memory.set_file_summary("sample.txt", "sample.txt: cached benchmark summary")
        agent.memory.remember_file("sample.txt")
        sample_freshness = agent.memory.to_dict()["file_summaries"]["sample.txt"]["freshness"]
        key_files = [{"path": "sample.txt", "freshness": sample_freshness}]
        freshness = {"sample.txt": sample_freshness}
        if setup == "partial_stale_multi":
            agent.memory.set_file_summary("notes.txt", "notes.txt: cached note summary")
            agent.memory.remember_file("notes.txt")
            notes_freshness = agent.memory.to_dict()["file_summaries"]["notes.txt"]["freshness"]
            key_files.append({"path": "notes.txt", "freshness": notes_freshness})
            freshness["notes.txt"] = notes_freshness
        agent.session["memory"] = agent.memory.to_dict()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_stale",
            "items": {
                "ckpt_stale": {
                    "checkpoint_id": "ckpt_stale",
                    "parent_checkpoint_id": "",
                    "schema_version": "phase1-v1",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover from stale benchmark summaries",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Re-anchor the stale summaries",
                    "key_files": key_files,
                    "freshness": freshness,
                    "summary": "partial stale benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        agent.session_store.save(agent.session)
        (workspace_root / "sample.txt").write_text("alpha\nbeta\nstale-shifted\nplaceholder\n", encoding="utf-8")
        if setup == "partial_stale_multi":
            (workspace_root / "notes.txt").write_text("note-one\nnote-two-shifted\n", encoding="utf-8")
        return

    if setup == "workspace_mismatch":
        agent.session["checkpoints"] = {
            "current_id": "ckpt_workspace",
            "items": {
                "ckpt_workspace": {
                    "checkpoint_id": "ckpt_workspace",
                    "parent_checkpoint_id": "",
                    "schema_version": "phase1-v1",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover after workspace drift",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Rebuild runtime state from a fresh checkpoint",
                    "key_files": [],
                    "freshness": {},
                    "summary": "workspace mismatch benchmark",
                    "runtime_identity": {"workspace_fingerprint": "outdated-workspace-fingerprint"},
                }
            },
        }
        agent.session_store.save(agent.session)
        return

    if setup == "schema_mismatch":
        agent.session["checkpoints"] = {
            "current_id": "ckpt_schema",
            "items": {
                "ckpt_schema": {
                    "checkpoint_id": "ckpt_schema",
                    "parent_checkpoint_id": "",
                    "schema_version": "legacy-v0",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover after schema mismatch",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Migrate the stale checkpoint",
                    "key_files": [],
                    "freshness": {},
                    "summary": "schema mismatch benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        agent.session_store.save(agent.session)
        return

    if setup == "no_checkpoint":
        agent.session.pop("checkpoints", None)
        agent.session_store.save(agent.session)
        return

    if setup in {"partial_success_shell", "partial_success_tool"}:
        blocker = "tool_partial_success" if setup == "partial_success_shell" else "tool_failed"
        next_step = "Inspect the diff before retry" if setup == "partial_success_shell" else "Retry after checking the workspace state"
        agent.session["checkpoints"] = {
            "current_id": "ckpt_partial",
            "items": {
                "ckpt_partial": {
                    "checkpoint_id": "ckpt_partial",
                    "parent_checkpoint_id": "",
                    "schema_version": "phase1-v1",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover after partial tool success",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": blocker,
                    "next_step": next_step,
                    "key_files": [{"path": "sample.txt", "freshness": None}],
                    "freshness": {},
                    "summary": "partial success benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        agent.session_store.save(agent.session)


def _run_recovery_task_variant(task, variant):
    with tempfile.TemporaryDirectory(prefix="cagent-recovery-ablation-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        agent = _build_recovery_agent(workspace_root, task["required_fragments"])
        _apply_recovery_setup(agent, task, workspace_root)
        if variant == "resume_disabled":
            agent.session.pop("checkpoints", None)
            agent.session_store.save(agent.session)
        final_answer = agent.ask("Continue the recovery task.")
        report = agent.run_store.load_report(agent.current_task_state.run_id)
        trace = [
            json.loads(line)
            for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
        ]
        resume_status = str(report.get("prompt_metadata", {}).get("resume_status", ""))
        stale_reanchored = any(
            event.get("event") == "checkpoint_created" and event.get("trigger") == "freshness_mismatch"
            for event in trace
        )
        workspace_drift_detected = any(event.get("event") == "runtime_identity_mismatch" for event in trace)
        invalid_resume = task["category"] in {"partial_stale", "workspace_mismatch", "schema_mismatch"}
        return {
            "task_id": task["id"],
            "category": task["category"],
            "variant": variant,
            "resume_status": resume_status,
            "resume_succeeded": final_answer == "recovery state restored.",
            "stale_reanchored": stale_reanchored,
            "workspace_drift_detected": workspace_drift_detected,
            "false_accept": invalid_resume and resume_status == "full-valid",
            "final_answer": final_answer,
        }


def _recovery_variant_summary(rows):
    rows = list(rows)
    stale_rows = [row for row in rows if row["category"] == "partial_stale"]
    drift_rows = [row for row in rows if row["category"] == "workspace_mismatch"]
    invalid_rows = [row for row in rows if row["category"] in {"partial_stale", "workspace_mismatch", "schema_mismatch"}]
    return {
        "resume_success_rate": _safe_ratio(sum(1 for row in rows if row["resume_succeeded"]), len(rows)),
        "stale_reanchor_rate": _safe_ratio(sum(1 for row in stale_rows if row["stale_reanchored"]), len(stale_rows)),
        "workspace_drift_detection_rate": _safe_ratio(sum(1 for row in drift_rows if row["workspace_drift_detected"]), len(drift_rows)),
        "resume_false_accept_rate": _safe_ratio(sum(1 for row in invalid_rows if row["false_accept"]), len(invalid_rows)),
    }


def run_context_ablation_v2(artifact_path=DEFAULT_CONTEXT_ABLATION_V2_PATH, repetitions=5):
    payload = run_context_stress_matrix(repetitions=repetitions)
    artifact = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "context-ablation-v2",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "config_count": payload["config_count"],
        "configs": payload["configs"],
        "summary": payload["summary"],
    }
    return _write_json_artifact(artifact_path, artifact)


def run_prompt_cache_layout_experiment(
    artifact_path=DEFAULT_PROMPT_CACHE_LAYOUT_PATH,
    repetitions=1,
    context_window_chars=PROMPT_CACHE_CONTEXT_WINDOW_CHARS,
    cache_hit_token_threshold=PROMPT_CACHE_HIT_TOKEN_THRESHOLD,
    cache_hit_coverage_threshold=PROMPT_CACHE_HIT_COVERAGE_THRESHOLD,
):
    payload = run_prompt_cache_layout_matrix(
        repetitions=repetitions,
        cache_hit_token_threshold=cache_hit_token_threshold,
        cache_hit_coverage_threshold=cache_hit_coverage_threshold,
    )
    artifact = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "prompt-cache-layout-v1",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "context_window_chars": int(context_window_chars),
        "section_budgets_disabled": True,
        "token_estimate_chars_per_token": PROMPT_CACHE_TOKEN_CHARS,
        "cache_hit_token_threshold": int(cache_hit_token_threshold),
        "cache_hit_coverage_threshold": float(cache_hit_coverage_threshold),
        "scenario_count": payload["scenario_count"],
        "base_scenario_count": payload["base_scenario_count"],
        "size_group_targets": dict(PROMPT_CACHE_SIZE_GROUPS),
        "size_groups": payload["size_groups"],
        "scenarios": payload["scenarios"],
        "variants": payload["variants"],
    }
    return _write_json_artifact(artifact_path, artifact)


def run_memory_ablation_v2(artifact_path=DEFAULT_MEMORY_ABLATION_V2_PATH, repetitions=5):
    payload = run_large_scale_memory_experiment(repetitions=repetitions)
    artifact = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "memory-ablation-v2",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "task_count": payload["task_count"],
        "runs_per_variant": payload["runs_per_variant"],
        "category_counts": payload["category_counts"],
        "variants": payload["variants"],
        "rows": payload["rows"],
    }
    return _write_json_artifact(artifact_path, artifact)


def run_recovery_ablation_v2(artifact_path=DEFAULT_RECOVERY_ABLATION_V2_PATH, repetitions=3):
    repetitions = int(repetitions)
    variants = {"resume_enabled": [], "resume_disabled": []}
    for task in RECOVERY_ABLATION_TASKS:
        for _ in range(repetitions):
            for variant in variants:
                variants[variant].append(_run_recovery_task_variant(task, variant))
    artifact = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "recovery-ablation-v2",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "task_count": len(RECOVERY_ABLATION_TASKS),
        "variants": {
            variant: {
                "summary": _recovery_variant_summary(rows),
                "rows": rows,
            }
            for variant, rows in variants.items()
        },
    }
    return _write_json_artifact(artifact_path, artifact)


def write_benchmark_core_report(
    report_path=DEFAULT_CORE_REPORT_PATH,
    harness_artifact_path=DEFAULT_HARNESS_REGRESSION_V2_PATH,
    context_artifact_path=DEFAULT_CONTEXT_ABLATION_V2_PATH,
    memory_artifact_path=DEFAULT_MEMORY_ABLATION_V2_PATH,
    recovery_artifact_path=DEFAULT_RECOVERY_ABLATION_V2_PATH,
):
    harness = json.loads(Path(harness_artifact_path).read_text(encoding="utf-8"))
    context = json.loads(Path(context_artifact_path).read_text(encoding="utf-8"))
    memory = json.loads(Path(memory_artifact_path).read_text(encoding="utf-8"))
    recovery = json.loads(Path(recovery_artifact_path).read_text(encoding="utf-8"))

    enabled_recovery = recovery["variants"]["resume_enabled"]["summary"]
    lines = [
        "# CAgent Benchmark Core Report",
        "",
        "这轮 benchmark 只收缩到 Harness regression、context ablation、working memory ablation 和 recovery ablation 四层，不把 provider、run aggregation 或 durable memory 的别的结论揉进来。",
        "",
        "## Harness Regression",
        f"- 固定 regression 任务数：{harness['summary']['total_tasks']}",
        f"- pass_rate：{harness['summary']['pass_rate']:.2%}",
        f"- within_budget_rate：{harness['summary']['within_budget_rate']:.2%}",
        f"- verifier_pass_rate：{harness['summary']['verifier_pass_rate']:.2%}",
        "",
        "## Context Ablation",
        f"- 配置数：{context['config_count']}",
        f"- avg_full_prompt_chars：{context['summary']['avg_full_prompt_chars']:.2f}",
        f"- avg_raw_prompt_chars：{context['summary']['avg_raw_prompt_chars']:.2f}",
        f"- avg_prompt_compression_ratio：{context['summary']['avg_prompt_compression_ratio']:.2%}",
        f"- max_prompt_compression_ratio：{context['summary']['max_prompt_compression_ratio']:.2%}",
        f"- current_request_preserved_rate：{context['summary']['current_request_preserved_rate']:.2%}",
        "",
        "## Working Memory Ablation",
        f"- memory_on repeated_reads：{memory['variants']['memory_on']['repeated_reads']}",
        f"- memory_off repeated_reads：{memory['variants']['memory_off']['repeated_reads']}",
        f"- memory_on avg_tool_steps：{memory['variants']['memory_on']['avg_tool_steps']:.2f}",
        f"- memory_on correct_rate：{memory['variants']['memory_on']['correct_rate']:.2%}",
        f"- memory_hit_rate：{memory['variants']['memory_on']['memory_hit_rate']:.2%}",
        "",
        "## Recovery / Resume Ablation",
        f"- resume_success_rate：{enabled_recovery['resume_success_rate']:.2%}",
        f"- stale_reanchor_rate：{enabled_recovery['stale_reanchor_rate']:.2%}",
        f"- workspace_drift_detection_rate：{enabled_recovery['workspace_drift_detection_rate']:.2%}",
        f"- resume_false_accept_rate：{enabled_recovery['resume_false_accept_rate']:.2%}",
        "",
        "## 可以安全写进简历的指标",
        "- avg_full_prompt_chars",
        "- avg_raw_prompt_chars",
        "- avg_prompt_compression_ratio",
        "- max_prompt_compression_ratio",
        "- repeated_reads",
        "- avg_tool_steps",
        "- correct_rate",
        "- resume_success_rate",
        "- workspace_drift_detection_rate",
        "- resume_false_accept_rate",
        "",
        "## 只适合放文档/面试展开的指标",
        "- current_request_preserved_rate",
        "- memory_hit_rate",
        "- stale_reanchor_rate",
        "- failure_category_counts",
        "",
        "## 口径边界",
        "- Harness regression 只证明 runtime 合同稳定，不证明 provider 上限。",
        "- Context、memory、recovery 这三层只证明模块收益，不和 provider benchmark 混写。",
    ]
    report_text = "\n".join(lines) + "\n"
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    return report_text
