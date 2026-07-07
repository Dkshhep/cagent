import json
import subprocess
from pathlib import Path

import pytest

import cagent.public_benchmarks as public_benchmarks
from cagent.models import FakeModelClient
from cagent.public_benchmarks import load_public_benchmark, run_public_benchmark


TEST_PATCH = """diff --git a/test_calc.py b/test_calc.py
new file mode 100644
index 0000000..1f9f0b1
--- /dev/null
+++ b/test_calc.py
@@ -0,0 +1,5 @@
+from calc import add
+
+
+def test_adds_numbers():
+    assert add(2, 3) == 5
"""


def _public_task(source_repo):
    return {
        "id": "fixture__calc-1",
        "dataset": "swebench_lite",
        "repo": "fixture/calc",
        "source_repo": str(source_repo),
        "base_commit": "fixture-base",
        "problem_statement": "add() subtracts the right operand instead of adding it.",
        "test_patch": TEST_PATCH,
        "fail_to_pass": ["test_calc.py::test_adds_numbers"],
        "pass_to_pass": [],
        "step_budget": 4,
        "category": "real-bugfix",
    }


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(repo, message):
    result = subprocess.run(
        ["git", "-c", "user.name=CAgent Test", "-c", "user.email=cagent@example.test", "commit", "-m", message],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return _git(repo, "rev-parse", "HEAD")


def test_load_public_benchmark_validates_materialized_swebench_schema(tmp_path):
    source_repo = Path("tests/fixtures/public_benchmark_repo")
    benchmark_path = tmp_path / "public.jsonl"
    _write_jsonl(benchmark_path, [_public_task(source_repo)])

    tasks = load_public_benchmark(benchmark_path)

    assert len(tasks) == 1
    assert tasks[0]["dataset"] == "swebench_lite"
    assert tasks[0]["fail_to_pass"] == ["test_calc.py::test_adds_numbers"]
    assert tasks[0]["step_budget"] == 4


def test_load_public_benchmark_rejects_missing_required_task_fields(tmp_path):
    benchmark_path = tmp_path / "broken.jsonl"
    _write_jsonl(benchmark_path, [{"id": "broken", "dataset": "swebench_lite"}])

    with pytest.raises(ValueError, match="missing required keys"):
        load_public_benchmark(benchmark_path)


def test_run_public_benchmark_applies_test_patch_and_reports_pass(tmp_path):
    source_repo = Path("tests/fixtures/public_benchmark_repo")
    benchmark_path = tmp_path / "public.jsonl"
    artifact_path = tmp_path / "artifact.json"
    _write_jsonl(benchmark_path, [_public_task(source_repo)])

    artifact = run_public_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
        model_name="FakeModelClient",
        model_version="scripted-public-fixture",
        model_client_factory=lambda task, workspace: FakeModelClient(
            [
                '<tool name="patch_file" path="calc.py"><old_text>return left - right</old_text><new_text>return left + right</new_text></tool>',
                "<final>Done.</final>",
            ]
        ),
    )

    assert artifact_path.exists()
    assert artifact["summary"]["total_tasks"] == 1
    assert artifact["summary"]["pass_rate"] == 1.0
    row = artifact["rows"][0]
    assert row["status"] == "pass"
    assert row["dataset"] == "swebench_lite"
    assert row["repo"] == "fixture/calc"
    assert row["test_command"] == "python -m pytest test_calc.py::test_adds_numbers"
    assert row["patch_digest"].startswith("sha256:")
    assert "calc.py" in row["diff_stat"]


def test_run_public_benchmark_marks_missing_diff_as_failure(tmp_path):
    source_repo = Path("tests/fixtures/public_benchmark_repo")
    benchmark_path = tmp_path / "public.jsonl"
    _write_jsonl(benchmark_path, [_public_task(source_repo)])

    artifact = run_public_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=lambda task, workspace: FakeModelClient(["<final>Done.</final>"]),
    )

    row = artifact["rows"][0]
    assert row["status"] == "fail"
    assert row["failure_category"] == "missing_diff"
    assert artifact["summary"]["failure_category_counts"] == {"missing_diff": 1}


def test_run_public_benchmark_reports_agent_errors_with_task_state(tmp_path):
    class RaisingModelClient:
        supports_prompt_cache = False

        def __init__(self):
            self.last_completion_metadata = {}

        def complete(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            raise RuntimeError("DeepSeek-compatible error: could not extract text from response")

    source_repo = Path("tests/fixtures/public_benchmark_repo")
    benchmark_path = tmp_path / "public.jsonl"
    _write_jsonl(benchmark_path, [_public_task(source_repo)])

    artifact = run_public_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=lambda task, workspace: RaisingModelClient(),
    )

    row = artifact["rows"][0]
    assert row["status"] == "fail"
    assert row["failure_category"] == "agent_failed"
    assert row["agent_error_type"] == "model_client_response_parse_failed"
    assert "could not extract text" in row["agent_error_message"]
    assert row["attempts"] == 1
    assert row["tool_steps"] == 0
    assert artifact["summary"]["failure_category_counts"] == {"agent_failed": 1}


def test_make_test_command_maps_django_human_test_name_to_runtests_label():
    task = _public_task(source_repo="")
    task["repo"] = "django/django"
    task["fail_to_pass"] = ["test_select_related_only (proxy_models.tests.ProxyModelTests)"]

    command = public_benchmarks._make_test_command(task)

    assert command == "python tests/runtests.py proxy_models.tests.ProxyModelTests.test_select_related_only"


def test_django_test_label_keeps_tests_module_in_dotted_path():
    # 回归：runtests.py 需要完整的 ``app.tests.Class.method`` 标签。早先的实现
    # 丢掉了中间的 ``tests`` 模块名，产出 ``app.Class.method``，导致 runtests 去
    # import 不存在的 ``app.Class`` 模块并报 ModuleNotFoundError，把本可通过的
    # 补丁误判为验证失败。
    label = public_benchmarks._django_test_label_from_human_name(
        "test_select_related_only (proxy_models.tests.ProxyModelTests)"
    )

    assert label == "proxy_models.tests.ProxyModelTests.test_select_related_only"


def test_django_test_label_handles_subpackage_test_module():
    # 很多 django 测试写在子包的 test_*.py 文件里（不是 app 的 tests.py），
    # 人类可读名形如 ``test_x (backends.sqlite.test_creation.TestDbSignatureTests)``。
    # 标签解析要保留完整模块路径，否则会错误回退到 `python -m pytest`（跑不了
    # django 测试），把这些任务全判成失败。
    label = public_benchmarks._django_test_label_from_human_name(
        "test_custom_test_name (backends.sqlite.test_creation.TestDbSignatureTests)"
    )

    assert label == "backends.sqlite.test_creation.TestDbSignatureTests.test_custom_test_name"


def test_django_test_label_rejects_names_without_tests_module():
    # 类路径中间不是 ``tests`` 模块的，无法安全映射到 runtests 标签，应返回空串
    # 让上层回退到其它推断方式，而不是拼出一个错误标签。
    assert public_benchmarks._django_test_label_from_human_name("not a test name") == ""
    assert (
        public_benchmarks._django_test_label_from_human_name(
            "test_x (proxy_models.ProxyModelTests)"
        )
        == ""
    )


def test_make_test_command_infers_django_added_test_method_from_patch(tmp_path):
    tests_dir = tmp_path / "tests" / "model_fields"
    tests_dir.mkdir(parents=True)
    (tests_dir / "tests.py").write_text(
        "class BasicFieldTests(SimpleTestCase):\n"
        "    def test_field_name(self):\n"
        "        pass\n"
        "\n"
        "    def test_abstract_inherited_fields(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    task = _public_task(source_repo="")
    task["repo"] = "django/django"
    task["fail_to_pass"] = ["Field instances from abstract models are not equal."]
    task["test_patch"] = """diff --git a/tests/model_fields/tests.py b/tests/model_fields/tests.py
--- a/tests/model_fields/tests.py
+++ b/tests/model_fields/tests.py
@@ -1,3 +1,5 @@
 class BasicFieldTests(SimpleTestCase):
+    def test_abstract_inherited_fields(self):
+        pass
"""

    command = public_benchmarks._make_test_command(task, workspace_root=tmp_path)

    assert command == "python tests/runtests.py model_fields.tests.BasicFieldTests.test_abstract_inherited_fields"


def test_run_verifier_adds_workspace_root_to_pythonpath_for_django_runner(tmp_path):
    (tmp_path / "django").mkdir()
    (tmp_path / "django" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "runtests.py").write_text(
        "import django\n"
        "print('django import ok')\n",
        encoding="utf-8",
    )
    task = _public_task(source_repo="")
    task["test_patch"] = ""
    task["test_command"] = "python tests/runtests.py"

    result = public_benchmarks._run_verifier(task, tmp_path)

    assert result["verifier_passed"] is True
    assert "django import ok" in result["verifier_stdout"]


def test_resolve_python_in_command_uses_explicit_verifier_python():
    # verifier_python 允许把测试跑在装好目标仓库依赖的独立解释器上，
    # 把「补丁对不对」和「运行环境缺依赖」两层错误分开。
    command = public_benchmarks._resolve_python_in_command(
        "python tests/runtests.py proxy_models.tests.ProxyModelTests.test_x",
        verifier_python="X:/envs/swebench/python.exe",
    )

    assert command == (
        '"X:/envs/swebench/python.exe" tests/runtests.py '
        "proxy_models.tests.ProxyModelTests.test_x"
    )


def test_resolve_python_in_command_defaults_to_current_interpreter():
    # 不指定 verifier_python 时保持既有行为：回退到当前运行 benchmark 的解释器。
    import sys

    command = public_benchmarks._resolve_python_in_command("python -m pytest a::b")

    assert command == f'"{sys.executable}" -m pytest a::b'


def test_repo_cache_path_maps_owner_repo_to_stable_directory(tmp_path):
    path = public_benchmarks._repo_cache_path(tmp_path / "cache", "owner/repo")

    assert path == tmp_path / "cache" / "owner__repo"


def test_run_public_benchmark_uses_existing_git_repo_cache_and_checks_out_base_commit(tmp_path):
    cache_repo = tmp_path / "cache" / "fixture__calc"
    cache_repo.mkdir(parents=True)
    _git(cache_repo, "init")
    (cache_repo / "calc.py").write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    _git(cache_repo, "add", "calc.py")
    base_commit = _commit(cache_repo, "base buggy calc")
    (cache_repo / "calc.py").write_text("def add(left, right):\n    return 999\n", encoding="utf-8")
    _git(cache_repo, "add", "calc.py")
    _commit(cache_repo, "move cache head away from base")

    task = _public_task(source_repo="")
    task.pop("source_repo")
    task["base_commit"] = base_commit
    benchmark_path = tmp_path / "public.jsonl"
    _write_jsonl(benchmark_path, [task])

    artifact = run_public_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        repo_cache_root=tmp_path / "cache",
        model_client_factory=lambda task, workspace: FakeModelClient(
            [
                '<tool name="patch_file" path="calc.py"><old_text>return left - right</old_text><new_text>return left + right</new_text></tool>',
                "<final>Done.</final>",
            ]
        ),
    )

    row = artifact["rows"][0]
    workspace = tmp_path / "workspaces" / row["workspace_relpath"]
    assert row["status"] == "pass"
    assert "return left + right" in (workspace / "calc.py").read_text(encoding="utf-8")
    assert "return 999" in (cache_repo / "calc.py").read_text(encoding="utf-8")


def test_run_public_benchmark_continues_when_fetch_fails_but_base_commit_is_cached(tmp_path, monkeypatch):
    cache_repo = tmp_path / "cache" / "fixture__calc"
    cache_repo.mkdir(parents=True)
    _git(cache_repo, "init")
    _git(cache_repo, "remote", "add", "origin", "https://example.invalid/fixture/calc.git")
    (cache_repo / "calc.py").write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    _git(cache_repo, "add", "calc.py")
    base_commit = _commit(cache_repo, "base buggy calc")

    real_run_git = public_benchmarks._run_git

    def fake_run_git(args, cwd=None, timeout=public_benchmarks.DEFAULT_GIT_TIMEOUT_SECONDS):
        if args[0] == "fetch":
            return subprocess.CompletedProcess(["git", *args], 1, "", "network unavailable")
        return real_run_git(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(public_benchmarks, "_run_git", fake_run_git)
    task = _public_task(source_repo="")
    task.pop("source_repo")
    task["base_commit"] = base_commit
    benchmark_path = tmp_path / "public.jsonl"
    _write_jsonl(benchmark_path, [task])

    artifact = run_public_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        repo_cache_root=tmp_path / "cache",
        model_client_factory=lambda task, workspace: FakeModelClient(
            [
                '<tool name="patch_file" path="calc.py"><old_text>return left - right</old_text><new_text>return left + right</new_text></tool>',
                "<final>Done.</final>",
            ]
        ),
    )

    assert artifact["rows"][0]["status"] == "pass"


def test_run_public_benchmark_offline_cache_skips_fetch(tmp_path, monkeypatch):
    cache_repo = tmp_path / "cache" / "fixture__calc"
    cache_repo.mkdir(parents=True)
    _git(cache_repo, "init")
    _git(cache_repo, "remote", "add", "origin", "https://example.invalid/fixture/calc.git")
    (cache_repo / "calc.py").write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    _git(cache_repo, "add", "calc.py")
    base_commit = _commit(cache_repo, "base buggy calc")

    real_run_git = public_benchmarks._run_git

    def fake_run_git(args, cwd=None, timeout=public_benchmarks.DEFAULT_GIT_TIMEOUT_SECONDS):
        if args[0] == "fetch":
            raise AssertionError("offline cache should not fetch")
        return real_run_git(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(public_benchmarks, "_run_git", fake_run_git)
    task = _public_task(source_repo="")
    task.pop("source_repo")
    task["base_commit"] = base_commit
    benchmark_path = tmp_path / "public.jsonl"
    _write_jsonl(benchmark_path, [task])

    artifact = run_public_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        repo_cache_root=tmp_path / "cache",
        offline_cache=True,
        model_client_factory=lambda task, workspace: FakeModelClient(
            [
                '<tool name="patch_file" path="calc.py"><old_text>return left - right</old_text><new_text>return left + right</new_text></tool>',
                "<final>Done.</final>",
            ]
        ),
    )

    assert artifact["rows"][0]["status"] == "pass"


def test_run_public_benchmark_reports_setup_failed_when_fetch_fails_and_base_commit_is_missing(tmp_path, monkeypatch):
    cache_repo = tmp_path / "cache" / "fixture__calc"
    cache_repo.mkdir(parents=True)
    _git(cache_repo, "init")
    _git(cache_repo, "remote", "add", "origin", "https://example.invalid/fixture/calc.git")
    (cache_repo / "calc.py").write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    _git(cache_repo, "add", "calc.py")
    _commit(cache_repo, "base buggy calc")

    real_run_git = public_benchmarks._run_git

    def fake_run_git(args, cwd=None, timeout=public_benchmarks.DEFAULT_GIT_TIMEOUT_SECONDS):
        if args[0] == "fetch":
            return subprocess.CompletedProcess(["git", *args], 1, "", "network unavailable")
        return real_run_git(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(public_benchmarks, "_run_git", fake_run_git)
    task = _public_task(source_repo="")
    task.pop("source_repo")
    task["base_commit"] = "0" * 40
    benchmark_path = tmp_path / "public.jsonl"
    _write_jsonl(benchmark_path, [task])

    artifact = run_public_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        repo_cache_root=tmp_path / "cache",
        model_client_factory=lambda task, workspace: FakeModelClient(["<final>Done.</final>"]),
    )

    row = artifact["rows"][0]
    assert row["status"] == "fail"
    assert row["failure_category"] == "setup_failed"
    assert "network unavailable" in row["verifier_stderr"]


def test_run_public_benchmark_reports_setup_failed_when_clone_fails(tmp_path, monkeypatch):
    def fake_run_git(args, cwd=None, timeout=public_benchmarks.DEFAULT_GIT_TIMEOUT_SECONDS):
        del cwd, timeout
        if args[0] == "clone":
            return subprocess.CompletedProcess(["git", *args], 1, "", "network unavailable")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(public_benchmarks, "_run_git", fake_run_git)
    task = _public_task(source_repo="")
    task.pop("source_repo")
    benchmark_path = tmp_path / "public.jsonl"
    _write_jsonl(benchmark_path, [task])

    artifact = run_public_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        repo_cache_root=tmp_path / "cache",
        model_client_factory=lambda task, workspace: FakeModelClient(["<final>Done.</final>"]),
    )

    row = artifact["rows"][0]
    assert row["status"] == "fail"
    assert row["failure_category"] == "setup_failed"
    assert "network unavailable" in row["verifier_stderr"]
