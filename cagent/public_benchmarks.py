"""公共数据集 benchmark 的轻量接入层。

为什么存在：现有 evaluator 主要证明工具协议、恢复和安全边界；公共数据集
需要额外处理外部 repo、issue 文本、测试补丁和真实模型 artifact。这个模块把
SWE-bench Lite 先物化成稳定 task，再复用 CAgent 的运行和报告能力。
"""

import hashlib
import json
import locale as locale_module
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .evaluator import DEFAULT_MAX_NEW_TOKENS, DEFAULT_TIMEZONE, _git_value, summarize_rows
from .models import FakeModelClient
from .runtime import CAgent, SessionStore
from .run_store import RunStore
from .task_state import STOP_REASON_FINAL_ANSWER_RETURNED
from .workspace import WorkspaceContext

PUBLIC_BENCHMARK_SCHEMA_VERSION = 1
DEFAULT_PUBLIC_ARTIFACT_PATH = Path("artifacts/swebench-lite-sample.json")
DEFAULT_PUBLIC_WORKSPACE_ROOT = Path("artifacts/public-benchmark-workspaces")
DEFAULT_PUBLIC_REPO_CACHE_ROOT = Path("artifacts/public-repo-cache")
DEFAULT_PUBLIC_MODEL_NAME = "openai"
DEFAULT_PUBLIC_MODEL_VERSION = "openai-default"
DEFAULT_PUBLIC_STEP_BUDGET = 40
DEFAULT_PUBLIC_MAX_NEW_TOKENS = 4096
DEFAULT_GIT_TIMEOUT_SECONDS = 300

PUBLIC_TASK_REQUIRED_KEYS = (
    "id",
    "dataset",
    "repo",
    "base_commit",
    "problem_statement",
    "test_patch",
    "fail_to_pass",
    "pass_to_pass",
    "step_budget",
    "category",
)


def _now_in_timezone(timezone_name):
    return datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _current_locale():
    try:
        return locale_module.setlocale(locale_module.LC_CTYPE)
    except Exception:
        return locale_module.getdefaultlocale()[0] or "C"


def _read_json_or_jsonl(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        return data["tasks"]
    return data


def _as_string_list(value, field_name, task_id):
    if not isinstance(value, list):
        raise ValueError(f"public benchmark task {task_id} {field_name} must be a list")
    return [str(item).strip() for item in value if str(item).strip()]


def validate_public_benchmark_tasks(tasks):
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("public benchmark tasks must be a non-empty list")

    seen_ids = set()
    normalized = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"public benchmark task at index {index} must be a mapping")
        missing = [key for key in PUBLIC_TASK_REQUIRED_KEYS if key not in task]
        if missing:
            raise ValueError(
                f"public benchmark task {task.get('id', index)!r} is missing required keys: {', '.join(missing)}"
            )

        task_id = str(task["id"]).strip()
        if not task_id:
            raise ValueError(f"public benchmark task at index {index} has an empty id")
        if task_id in seen_ids:
            raise ValueError(f"duplicate public benchmark task id: {task_id}")
        seen_ids.add(task_id)

        step_budget = int(task["step_budget"])
        if step_budget < 1:
            raise ValueError(f"public benchmark task {task_id} step_budget must be positive")

        normalized_task = dict(task)
        normalized_task["id"] = task_id
        normalized_task["dataset"] = str(task["dataset"]).strip()
        normalized_task["repo"] = str(task["repo"]).strip()
        normalized_task["base_commit"] = str(task["base_commit"]).strip()
        normalized_task["problem_statement"] = str(task["problem_statement"]).strip()
        normalized_task["test_patch"] = str(task["test_patch"])
        normalized_task["fail_to_pass"] = _as_string_list(task["fail_to_pass"], "fail_to_pass", task_id)
        normalized_task["pass_to_pass"] = _as_string_list(task["pass_to_pass"], "pass_to_pass", task_id)
        normalized_task["step_budget"] = step_budget
        normalized_task["category"] = str(task["category"]).strip()
        if "source_repo" in task:
            normalized_task["source_repo"] = str(task["source_repo"]).strip()
        if "test_command" in task:
            normalized_task["test_command"] = str(task["test_command"]).strip()
        normalized.append(normalized_task)
    return normalized


def load_public_benchmark(path, limit=None):
    tasks = validate_public_benchmark_tasks(_read_json_or_jsonl(path))
    if limit is not None:
        return tasks[: int(limit)]
    return tasks


def build_public_prompt(task):
    tests = ", ".join(task.get("fail_to_pass") or [])
    prompt = [
        "You are fixing a public benchmark issue.",
        "",
        "Problem statement:",
        task["problem_statement"],
        "",
        "Modify the repository code so the relevant tests pass.",
        "Read files, search, and run focused tests as needed.",
        "Keep the patch minimal and avoid unrelated file changes.",
    ]
    if tests:
        prompt.extend(["", f"Relevant failing tests: {tests}"])
    return "\n".join(prompt)


def _repo_cache_path(repo_cache_root, repo_name):
    return Path(repo_cache_root) / repo_name.replace("/", "__")


def _github_clone_url(repo_name):
    repo_name = str(repo_name).strip().strip("/")
    if repo_name.count("/") != 1:
        raise ValueError(f"public benchmark repo must look like owner/repo: {repo_name}")
    return f"https://github.com/{repo_name}.git"


def _run_git(args, cwd=None, timeout=DEFAULT_GIT_TIMEOUT_SECONDS):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_origin_exists(repo_path):
    result = _run_git(["remote", "get-url", "origin"], cwd=repo_path)
    return result.returncode == 0 and bool(result.stdout.strip())


def _git_commit_exists(repo_path, commit):
    if not commit:
        return False
    result = _run_git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_path)
    return result.returncode == 0


def _remove_tree(path):
    def handle_remove_error(func, failed_path, exc_info):
        del exc_info
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)

    shutil.rmtree(path, onerror=handle_remove_error)


def ensure_public_repo_cache(repo_name, repo_cache_root, base_commit=None, offline_cache=False):
    repo_cache_root = Path(repo_cache_root)
    cache_path = _repo_cache_path(repo_cache_root, repo_name)
    if cache_path.exists():
        if not (cache_path / ".git").is_dir():
            raise RuntimeError(f"repo cache path exists but is not a git repo: {cache_path}")
        if not offline_cache and _git_origin_exists(cache_path):
            result = _run_git(["fetch", "--all", "--tags", "--prune"], cwd=cache_path)
            if result.returncode != 0:
                if base_commit and _git_commit_exists(cache_path, base_commit):
                    return cache_path
                raise RuntimeError(f"git fetch failed for {repo_name}: {result.stderr.strip()}")
        if base_commit and not _git_commit_exists(cache_path, base_commit):
            raise RuntimeError(f"repo cache for {repo_name} is missing base_commit {base_commit}")
        return cache_path

    repo_cache_root.mkdir(parents=True, exist_ok=True)
    result = _run_git(["clone", _github_clone_url(repo_name), str(cache_path)])
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed for {repo_name}: {result.stderr.strip()}")
    return cache_path


def _source_repo_for_task(task, repo_cache_root, offline_cache=False):
    if task.get("source_repo"):
        return Path(task["source_repo"])
    if repo_cache_root is None:
        raise ValueError(f"public benchmark task {task['id']} needs source_repo or repo_cache_root")
    return ensure_public_repo_cache(
        task["repo"],
        repo_cache_root,
        base_commit=task.get("base_commit"),
        offline_cache=offline_cache,
    )


def _copy_public_workspace(task, workspace_root, repo_cache_root, offline_cache=False):
    source_repo = _source_repo_for_task(task, repo_cache_root, offline_cache=offline_cache).resolve()
    if not source_repo.is_dir():
        raise FileNotFoundError(f"public benchmark source repo does not exist: {source_repo}")

    destination = Path(workspace_root) / task["id"] / source_repo.name
    if destination.exists():
        _remove_tree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (source_repo / ".git").is_dir():
        result = _run_git(["clone", "--no-hardlinks", str(source_repo), str(destination)])
        if result.returncode != 0:
            raise RuntimeError(f"git clone workspace failed for {task['id']}: {result.stderr.strip()}")
    else:
        ignore = shutil.ignore_patterns(".cagent", "__pycache__", ".pytest_cache")
        shutil.copytree(source_repo, destination, ignore=ignore)

    if (destination / ".git").is_dir() and task.get("base_commit"):
        result = subprocess.run(
            ["git", "checkout", "--force", task["base_commit"]],
            cwd=destination,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git checkout failed for {task['id']}: {result.stderr.strip()}")
    return destination


def _test_files_from_patch(patch_text):
    files = []
    for line in str(patch_text).splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[len("+++ b/") :].strip()
        if path and path != "/dev/null":
            files.append(path)
    return files


def _django_app_label_from_test_path(path):
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "tests" and parts[-1] == "tests.py":
        return parts[1]
    return ""


def _django_test_label_from_human_name(test_name):
    match = re.fullmatch(r"(?P<method>test_[\w_]+)\s+\((?P<class_path>[\w.]+)\)", str(test_name).strip())
    if not match:
        return ""
    class_path = match.group("class_path")
    parts = class_path.split(".")
    # django 的 runtests.py 需要完整点分标签 ``模块路径.类.方法``。模块路径的形态有两种：
    #  - ``app.tests.Class``：测试写在 app 的 tests.py 里（如 proxy_models.tests.ProxyModelTests）
    #  - ``app...test_xxx.Class``：测试写在子包的 test_*.py 文件里
    #    （如 backends.sqlite.test_creation.TestDbSignatureTests）
    # 两种都要求倒数第二段是测试模块（``tests`` 或 ``test_*``），以此和早先的 bug
    # 场景 ``app.Class``（缺测试模块段）区分，避免拼出让 runtests 报 ModuleNotFoundError
    # 的错误标签，同时不再漏掉子包里的测试（否则会错误回退到 `python -m pytest`）。
    if len(parts) < 3:
        return ""
    test_module = parts[-2]
    if test_module != "tests" and not test_module.startswith("test_"):
        return ""
    return f"{class_path}.{match.group('method')}"


def _added_test_methods_from_patch(patch_text):
    methods = []
    for line in str(patch_text).splitlines():
        match = re.match(r"\+\s+def\s+(test_[\w_]+)\s*\(", line)
        if match:
            methods.append(match.group(1))
    return methods


def _class_for_method(path, method_name):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    method_line = None
    method_pattern = re.compile(rf"^\s+def\s+{re.escape(method_name)}\s*\(")
    for index, line in enumerate(lines):
        if method_pattern.match(line):
            method_line = index
            break
    if method_line is None:
        return ""
    class_pattern = re.compile(r"^class\s+(\w+)\s*\(")
    for line in reversed(lines[:method_line]):
        match = class_pattern.match(line)
        if match:
            return match.group(1)
    return ""


def _infer_django_test_command(task, workspace_root=None):
    labels = [_django_test_label_from_human_name(test) for test in task.get("fail_to_pass") or []]
    labels = [label for label in labels if label]
    if labels:
        return "python tests/runtests.py " + " ".join(labels)

    test_files = _test_files_from_patch(task.get("test_patch", ""))
    methods = _added_test_methods_from_patch(task.get("test_patch", ""))
    if workspace_root is None or len(test_files) != 1 or not methods:
        return ""

    app_label = _django_app_label_from_test_path(test_files[0])
    if not app_label:
        return ""
    # 测试文件位于 ``tests/<app_label>/tests.py``，对应的模块标签是
    # ``<app_label>.tests``。runtests.py 需要 ``模块.类.方法`` 的完整点分路径，
    # 少了中间的 ``tests`` 会让它去 import 不存在的 ``<app_label>.<类>`` 模块。
    labels = []
    test_path = Path(workspace_root) / test_files[0]
    for method in methods:
        class_name = _class_for_method(test_path, method)
        if class_name:
            labels.append(f"{app_label}.tests.{class_name}.{method}")
    if labels:
        return "python tests/runtests.py " + " ".join(labels)
    return ""


def _make_test_command(task, workspace_root=None):
    if task.get("test_command"):
        return task["test_command"]
    if task.get("repo") == "django/django":
        command = _infer_django_test_command(task, workspace_root=workspace_root)
        if command:
            return command
    tests = list(task.get("fail_to_pass") or [])
    if tests:
        return "python -m pytest " + " ".join(tests)
    return ""


def _resolve_python_in_command(test_command, verifier_python=None):
    """把测试命令开头的裸 ``python``/``python3`` 换成目标解释器。

    为什么存在：验证器用 ``shell=True`` 跑命令，但 conda 环境里 PATH 常常
    没有裸的 ``python`` 可执行文件（尤其 Windows 下会返回退出码 9009），
    导致本来正确的补丁被误判为验证失败。

    ``verifier_python`` 允许把测试跑在一个装好目标仓库依赖的独立解释器上
    （例如给 django 任务专门建的 conda 环境），从而把「agent 改得对不对」
    和「跑 benchmark 的环境缺依赖」这两层错误分开。为 None 时回退到当前
    运行 benchmark 的 ``sys.executable``。
    """
    if not test_command:
        return test_command
    python_exe = verifier_python or sys.executable
    stripped = test_command.lstrip()
    leading_ws = test_command[: len(test_command) - len(stripped)]
    for prefix in ("python3 ", "python "):
        if stripped.startswith(prefix):
            rest = stripped[len(prefix) :]
            return f'{leading_ws}"{python_exe}" {rest}'
    return test_command


def _apply_test_patch(workspace_root, patch_text):
    if not str(patch_text).strip():
        return subprocess.CompletedProcess(args=["git", "apply"], returncode=0, stdout="", stderr="")
    return subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=workspace_root,
        input=patch_text,
        capture_output=True,
        text=True,
    )


def _run_verifier(task, workspace_root, verifier_python=None):
    patch_result = _apply_test_patch(workspace_root, task["test_patch"])
    if patch_result.returncode != 0:
        return {
            "test_command": "",
            "verifier_exit_code": patch_result.returncode,
            "verifier_stdout": patch_result.stdout,
            "verifier_stderr": patch_result.stderr,
            "verifier_passed": False,
            "failure_category": "setup_failed",
        }

    test_command = _make_test_command(task, workspace_root=workspace_root)
    if not test_command:
        return {
            "test_command": "",
            "verifier_exit_code": 1,
            "verifier_stdout": "",
            "verifier_stderr": "no test command could be inferred",
            "verifier_passed": False,
            "failure_category": "verifier_unavailable",
        }

    env = os.environ.copy()
    pythonpath = str(Path(workspace_root).resolve())
    if env.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pythonpath

    resolved_command = _resolve_python_in_command(test_command, verifier_python=verifier_python)
    verifier = subprocess.run(
        resolved_command,
        cwd=workspace_root,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return {
        "test_command": test_command,
        "verifier_exit_code": verifier.returncode,
        "verifier_stdout": verifier.stdout,
        "verifier_stderr": verifier.stderr,
        "verifier_passed": verifier.returncode == 0,
        "failure_category": None if verifier.returncode == 0 else "verifier_failed",
    }


def _workspace_relpath(path, workspace_root):
    return str(Path(path).resolve().relative_to(Path(workspace_root).resolve()))


def _digest_text(text):
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _diff_stat(workspace_root):
    result = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=workspace_root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def _snapshot_workspace_files(workspace_root):
    ignored_dirs = {".cagent", "__pycache__", ".pytest_cache", ".git"}
    snapshot = {}
    for path in Path(workspace_root).rglob("*"):
        if not path.is_file():
            continue
        if any(part in ignored_dirs for part in path.relative_to(workspace_root).parts):
            continue
        relative = str(path.relative_to(workspace_root))
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _diff_stat_from_snapshot(before, workspace_root):
    after = _snapshot_workspace_files(workspace_root)
    changed = []
    for path, digest in after.items():
        if before.get(path) != digest:
            changed.append(path)
    for path in before:
        if path not in after:
            changed.append(path)
    if not changed:
        return ""
    return f"{len(sorted(set(changed)))} files changed: " + ", ".join(sorted(set(changed)))


def _workspace_diff_stat(before_snapshot, workspace_root):
    git_stat = _diff_stat(workspace_root)
    if git_stat:
        return git_stat
    return _diff_stat_from_snapshot(before_snapshot, workspace_root)


def _agent_error_type(exc):
    message = str(exc)
    if "could not extract text from response" in message:
        return "model_client_response_parse_failed"
    if "Could not reach" in message or "request failed" in message:
        return "model_client_request_failed"
    return "agent_failed"


class PublicBenchmarkEvaluator:
    def __init__(
        self,
        benchmark_path,
        artifact_path=DEFAULT_PUBLIC_ARTIFACT_PATH,
        workspace_root=DEFAULT_PUBLIC_WORKSPACE_ROOT,
        repo_cache_root=DEFAULT_PUBLIC_REPO_CACHE_ROOT,
        model_name=DEFAULT_PUBLIC_MODEL_NAME,
        model_version=DEFAULT_PUBLIC_MODEL_VERSION,
        max_new_tokens=DEFAULT_PUBLIC_MAX_NEW_TOKENS,
        timezone_name=DEFAULT_TIMEZONE,
        model_client_factory=None,
        limit=None,
        offline_cache=False,
        verifier_python=None,
        shell_path_prepend=None,
    ):
        self.benchmark_path = Path(benchmark_path)
        self.artifact_path = Path(artifact_path)
        self.workspace_root = Path(workspace_root) if workspace_root is not None else Path(
            tempfile.mkdtemp(prefix="cagent-public-benchmark-")
        )
        self.repo_cache_root = Path(repo_cache_root) if repo_cache_root is not None else None
        self.model_name = model_name
        self.model_version = model_version
        self.max_new_tokens = max_new_tokens
        self.timezone_name = timezone_name
        self.model_client_factory = model_client_factory
        self.limit = limit
        self.offline_cache = offline_cache
        self.verifier_python = verifier_python
        self.shell_path_prepend = tuple(shell_path_prepend or ())
        self.repo_root = self.benchmark_path.resolve().parent.parent

    def load(self):
        return load_public_benchmark(self.benchmark_path, limit=self.limit)

    def run(self):
        tasks = self.load()
        rows = [self.run_task(task) for task in tasks]
        summary = summarize_rows(rows)
        artifact = {
            "schema_version": PUBLIC_BENCHMARK_SCHEMA_VERSION,
            "captured_at": _now_in_timezone(self.timezone_name),
            "runtime": {
                "commit_sha": _git_value(["rev-parse", "HEAD"], cwd=self.repo_root),
                "branch": _git_value(["branch", "--show-current"], cwd=self.repo_root),
            },
            "benchmark": {
                "source": str(self.benchmark_path),
                "task_count": len(tasks),
                "kind": "public",
            },
            "reproducibility": {
                "model_name": self.model_name,
                "model_version": self.model_version,
                "decoding": {"max_new_tokens": self.max_new_tokens},
                "timezone": self.timezone_name,
                "locale": _current_locale(),
            },
            "summary": summary,
            "failure_category_counts": summary["failure_category_counts"],
            "rows": rows,
        }
        self._write_artifact(artifact)
        return artifact

    def run_task(self, task):
        task = dict(task)
        try:
            workspace_root = _copy_public_workspace(
                task,
                self.workspace_root,
                self.repo_cache_root,
                offline_cache=self.offline_cache,
            )
        except Exception as exc:
            return self._setup_failure_row(task, exc)

        workspace = WorkspaceContext.build(workspace_root, repo_root_override=workspace_root)
        session_store = SessionStore(workspace_root / ".cagent" / "sessions")
        run_store = RunStore(workspace_root / ".cagent" / "runs")
        before_agent_snapshot = _snapshot_workspace_files(workspace_root)
        if self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, workspace=workspace)
        else:
            model_client = FakeModelClient([])
        agent = CAgent(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
            shell_path_prepend=self.shell_path_prepend,
        )

        final_answer = ""
        task_state = None
        report = {}
        agent_error_type = ""
        agent_error_message = ""
        try:
            final_answer = agent.ask(build_public_prompt(task))
            task_state = agent.current_task_state
            report = agent.run_store.load_report(task_state.run_id)
            agent_diff_stat = _workspace_diff_stat(before_agent_snapshot, workspace_root)
            verifier = _run_verifier(task, workspace_root, verifier_python=self.verifier_python)
        except Exception as exc:
            task_state = agent.current_task_state
            if task_state is not None:
                try:
                    report = agent.run_store.load_report(task_state.run_id)
                except Exception:
                    report = {}
            agent_error_type = _agent_error_type(exc)
            agent_error_message = str(exc)
            agent_diff_stat = _workspace_diff_stat(before_agent_snapshot, workspace_root)
            verifier = {
                "test_command": "",
                "verifier_exit_code": 1,
                "verifier_stdout": "",
                "verifier_stderr": agent_error_message,
                "verifier_passed": False,
                "failure_category": "agent_failed",
            }

        tool_steps = int(task_state.tool_steps) if task_state is not None else 0
        attempts = int(task_state.attempts) if task_state is not None else 0
        stop_reason = task_state.stop_reason if task_state is not None else ""
        within_budget = tool_steps <= int(task["step_budget"])
        non_failure_stop_reason = stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
        has_diff = bool(agent_diff_stat)
        verifier_passed = bool(verifier["verifier_passed"])
        passed = within_budget and verifier_passed and non_failure_stop_reason and has_diff
        failure_category = None if passed else self._failure_category(
            within_budget=within_budget,
            verifier=verifier,
            non_failure_stop_reason=non_failure_stop_reason,
            has_diff=has_diff,
        )

        run_dir = Path(agent.current_run_dir) if task_state is not None and agent.current_run_dir else workspace_root / ".cagent" / "runs"
        return {
            "id": task["id"],
            "dataset": task["dataset"],
            "repo": task["repo"],
            "base_commit": task["base_commit"],
            "category": task["category"],
            "workspace_relpath": _workspace_relpath(workspace_root, self.workspace_root),
            "run_dir_relpath": _workspace_relpath(run_dir, self.workspace_root) if run_dir.exists() else "",
            "step_budget": int(task["step_budget"]),
            "status": "pass" if passed else "fail",
            "passed": passed,
            "failure_category": failure_category,
            "within_budget": within_budget,
            "verifier_passed": verifier_passed,
            "non_failure_stop_reason": non_failure_stop_reason,
            "tool_steps": tool_steps,
            "attempts": attempts,
            "final_answer": final_answer,
            "stop_reason": stop_reason,
            "patch_digest": _digest_text(task["test_patch"]),
            "diff_stat": agent_diff_stat,
            "test_command": verifier["test_command"],
            "verifier_exit_code": verifier["verifier_exit_code"],
            "verifier_stdout": verifier["verifier_stdout"],
            "verifier_stderr": verifier["verifier_stderr"],
            "agent_error_type": agent_error_type,
            "agent_error_message": agent_error_message,
            "fail_to_pass": list(task["fail_to_pass"]),
            "pass_to_pass": list(task["pass_to_pass"]),
            "report": report,
        }

    def _failure_category(self, within_budget, verifier, non_failure_stop_reason, has_diff):
        if not within_budget:
            return "budget_exceeded"
        if verifier.get("failure_category") == "agent_failed":
            return "agent_failed"
        if not non_failure_stop_reason:
            return "failure_stop_reason"
        if not has_diff:
            return "missing_diff"
        return verifier.get("failure_category") or "unknown"

    def _setup_failure_row(self, task, exc):
        return {
            "id": task["id"],
            "dataset": task.get("dataset", ""),
            "repo": task.get("repo", ""),
            "base_commit": task.get("base_commit", ""),
            "category": task.get("category", ""),
            "workspace_relpath": "",
            "run_dir_relpath": "",
            "step_budget": int(task.get("step_budget", DEFAULT_PUBLIC_STEP_BUDGET)),
            "status": "fail",
            "passed": False,
            "failure_category": "setup_failed",
            "within_budget": True,
            "verifier_passed": False,
            "non_failure_stop_reason": False,
            "tool_steps": 0,
            "attempts": 0,
            "final_answer": "",
            "stop_reason": "",
            "patch_digest": _digest_text(task.get("test_patch", "")),
            "diff_stat": "",
            "test_command": "",
            "verifier_exit_code": 1,
            "verifier_stdout": "",
            "verifier_stderr": str(exc),
            "agent_error_type": "",
            "agent_error_message": "",
            "fail_to_pass": list(task.get("fail_to_pass") or []),
            "pass_to_pass": list(task.get("pass_to_pass") or []),
            "report": {},
        }

    def _write_artifact(self, artifact):
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_public_benchmark(
    benchmark_path,
    artifact_path=DEFAULT_PUBLIC_ARTIFACT_PATH,
    workspace_root=DEFAULT_PUBLIC_WORKSPACE_ROOT,
    repo_cache_root=DEFAULT_PUBLIC_REPO_CACHE_ROOT,
    model_name=DEFAULT_PUBLIC_MODEL_NAME,
    model_version=DEFAULT_PUBLIC_MODEL_VERSION,
    max_new_tokens=DEFAULT_PUBLIC_MAX_NEW_TOKENS,
    timezone_name=DEFAULT_TIMEZONE,
    model_client_factory=None,
    limit=None,
    offline_cache=False,
    verifier_python=None,
    shell_path_prepend=None,
):
    evaluator = PublicBenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=workspace_root,
        repo_cache_root=repo_cache_root,
        model_name=model_name,
        model_version=model_version,
        max_new_tokens=max_new_tokens,
        timezone_name=timezone_name,
        model_client_factory=model_client_factory,
        limit=limit,
        offline_cache=offline_cache,
        verifier_python=verifier_python,
        shell_path_prepend=shell_path_prepend,
    )
    return evaluator.run()
