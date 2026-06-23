import os
from unittest.mock import patch

from cagent.metrics import (
    _provider_profile,
    run_context_ablation_v2,
    run_context_compression_v3,
    run_memory_ablation_v2,
    run_prompt_cache_layout_experiment,
    run_recovery_ablation_v2,
    write_benchmark_core_report,
    write_context_compression_v3_report,
)


def test_run_context_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "context-ablation-v2.json"

    artifact = run_context_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "context-ablation-v2"
    assert artifact["config_count"] == 12
    assert len(artifact["configs"]) == 12
    assert "current_request_preserved_rate" in artifact["summary"]


def test_run_context_compression_v3_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "context-compression-v3.json"

    artifact = run_context_compression_v3(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "context-compression-v3"
    assert artifact["distillation_mode"] == "deterministic_stub"
    assert artifact["real_model_calls"] is False
    assert artifact["config_count"] == 12
    assert len(artifact["configs"]) == 12
    assert set(artifact["variants"]) == {"raw_no_compression", "compressed_v1", "compressed_v2_stub_distill"}
    assert artifact["summary"]["current_request_preserved_rate"] == 1.0
    assert "avg_v2_compression_ratio" in artifact["summary"]
    assert "max_v2_compression_ratio" in artifact["summary"]
    assert "avg_v2_incremental_ratio_vs_v1" in artifact["summary"]

    marker_configs = 0
    high_tool_configs = 0
    for config in artifact["configs"]:
        variants = config["variants"]
        assert set(variants) == {"raw_no_compression", "compressed_v1", "compressed_v2_stub_distill"}
        raw = variants["raw_no_compression"]
        v2 = variants["compressed_v2_stub_distill"]
        assert v2["prompt_tokens"] <= raw["prompt_tokens"]
        assert "compression_ratio_vs_raw" in v2
        assert "history_rendered_tokens" in v2
        assert v2["current_request_preserved"] is True
        if config["history_size"] in {"medium", "long"} and v2["distilled_marker_count"] > 0:
            marker_configs += 1
        if config["tool_density"] == "high":
            high_tool_configs += 1
            assert v2["collapsed_duplicate_tools"] > 0 or v2["head_tail_clipped_tool_count"] > 0

    assert marker_configs > 0
    assert high_tool_configs == 6


def test_write_context_compression_v3_report_contains_resume_metrics(tmp_path):
    artifact_path = tmp_path / "artifacts" / "context-compression-v3.json"
    report_path = tmp_path / "docs" / "metrics" / "context-compression-v3-report.md"
    run_context_compression_v3(artifact_path=artifact_path, repetitions=1)

    report_text = write_context_compression_v3_report(
        report_path=report_path,
        artifact_path=artifact_path,
    )

    assert report_path.exists()
    assert "deterministic distillation stub" in report_text
    assert "Average v2 compression ratio" in report_text
    assert "Max v2 compression ratio" in report_text
    assert "Average v2 incremental compression vs v1" in report_text
    assert "Current request preserved rate" in report_text
    assert "does not call a real model" in report_text


def test_run_prompt_cache_layout_experiment_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "prompt-cache-layout-v1.json"

    artifact = run_prompt_cache_layout_experiment(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "prompt-cache-layout-v1"
    assert artifact["context_window_chars"] == 100000
    assert artifact["section_budgets_disabled"] is True
    assert artifact["size_group_targets"] == {"short": 3000, "medium": 15000, "long": 50000}
    assert artifact["base_scenario_count"] == 10
    assert artifact["scenario_count"] == 30
    assert set(artifact["variants"]) == {
        "current",
        "volatile_before_history",
        "request_before_memory",
        "prefix_only",
    }
    current = artifact["variants"]["current"]
    volatile = artifact["variants"]["volatile_before_history"]
    assert current["cache_hit_rate"] > volatile["cache_hit_rate"]
    assert current["cache_hit_rate"] < 1.0
    assert volatile["cache_hit_rate"] < 1.0
    assert current["exact_reuse_rate"] < 1.0
    assert current["avg_cached_tokens"] > volatile["avg_cached_tokens"]
    assert current["avg_common_prefix_chars"] > volatile["avg_common_prefix_chars"]
    assert current["current_request_preserved_rate"] == 1.0
    assert 2500 <= artifact["size_groups"]["short"]["variants"]["current"]["avg_prompt_chars"] <= 4500
    assert 12000 <= artifact["size_groups"]["medium"]["variants"]["current"]["avg_prompt_chars"] <= 18000
    assert 45000 <= artifact["size_groups"]["long"]["variants"]["current"]["avg_prompt_chars"] <= 55000
    assert artifact["size_groups"]["medium"]["variants"]["current"]["cache_hit_rate"] < 1.0
    assert artifact["size_groups"]["long"]["variants"]["current"]["cache_hit_rate"] < 1.0


def test_prompt_cache_layout_scenarios_keep_current_request_and_expected_order(tmp_path):
    artifact = run_prompt_cache_layout_experiment(
        artifact_path=tmp_path / "artifacts" / "prompt-cache-layout-v1.json",
        repetitions=1,
    )

    assert artifact["variants"]["current"]["section_order"] == [
        "prefix",
        "history",
        "memory",
        "relevant_memory",
        "current_request",
    ]
    for scenario in artifact["scenarios"]:
        current = scenario["variants"]["current"]
        volatile = scenario["variants"]["volatile_before_history"]
        assert current["current_request_preserved_rate"] == 1.0
        assert current["avg_cached_tokens"] > volatile["avg_cached_tokens"]
    changed_sections = {}
    for scenario in artifact["scenarios"]:
        for section, count in scenario["variants"]["current"]["changed_section_counts"].items():
            changed_sections[section] = changed_sections.get(section, 0) + count
    assert changed_sections["history"] > 0
    assert changed_sections["memory"] > 0
    assert changed_sections["relevant_memory"] > 0


def test_provider_profile_loads_project_env_before_reading_deepseek_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PICO_DEEPSEEK_API_KEY=sk-project-deepseek",
                "PICO_DEEPSEEK_MODEL=deepseek-v4-pro",
                "PICO_DEEPSEEK_API_BASE=https://api.deepseek.com/anthropic",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_KEY": "sk-legacy-deepseek",
            "DEEPSEEK_MODEL": "legacy-deepseek-model",
            "DEEPSEEK_API_BASE": "https://legacy.deepseek.example/anthropic",
        },
        clear=True,
    ):
        profile = _provider_profile("deepseek")

    assert profile["status"] == "ready"
    assert profile["api_key"] == "sk-project-deepseek"
    assert profile["model"] == "deepseek-v4-pro"
    assert profile["base_url"] == "https://api.deepseek.com/anthropic"


def test_run_memory_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "memory-ablation-v2.json"

    artifact = run_memory_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "memory-ablation-v2"
    assert artifact["task_count"] == 12
    assert set(artifact["variants"]) == {"memory_on", "memory_off", "memory_irrelevant"}
    assert "memory_hit_rate" in artifact["variants"]["memory_on"]


def test_run_recovery_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "recovery-ablation-v2.json"

    artifact = run_recovery_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "recovery-ablation-v2"
    assert artifact["task_count"] == 10
    assert set(artifact["variants"]) == {"resume_enabled", "resume_disabled"}
    assert set(artifact["variants"]["resume_enabled"]["summary"]) >= {
        "resume_success_rate",
        "stale_reanchor_rate",
        "workspace_drift_detection_rate",
        "resume_false_accept_rate",
    }


def test_write_benchmark_core_report_marks_resume_safe_metrics(tmp_path):
    run_context_ablation_v2(tmp_path / "artifacts" / "context-ablation-v2.json", repetitions=1)
    run_memory_ablation_v2(tmp_path / "artifacts" / "memory-ablation-v2.json", repetitions=1)
    run_recovery_ablation_v2(tmp_path / "artifacts" / "recovery-ablation-v2.json", repetitions=1)
    harness_artifact_path = tmp_path / "artifacts" / "harness-regression-v2.json"
    harness_artifact_path.write_text(
        '{"summary":{"total_tasks":12,"pass_rate":1.0,"within_budget_rate":1.0,"verifier_pass_rate":1.0},"failure_category_counts":{}}',
        encoding="utf-8",
    )

    report_path = tmp_path / "docs" / "metrics" / "cagent-benchmark-core-report.md"
    report_text = write_benchmark_core_report(
        report_path=report_path,
        harness_artifact_path=harness_artifact_path,
        context_artifact_path=tmp_path / "artifacts" / "context-ablation-v2.json",
        memory_artifact_path=tmp_path / "artifacts" / "memory-ablation-v2.json",
        recovery_artifact_path=tmp_path / "artifacts" / "recovery-ablation-v2.json",
    )

    assert report_path.exists()
    assert "可以安全写进简历的指标" in report_text
    assert "只适合放文档/面试展开的指标" in report_text
    assert "resume_success_rate" in report_text
    assert "memory_hit_rate" in report_text
