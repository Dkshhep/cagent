import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from cagent.public_benchmarks import load_public_benchmark


SCRIPT_PATH = Path("scripts/import_quixbugs.py")
SPEC = importlib.util.spec_from_file_location("import_quixbugs", SCRIPT_PATH)
import_quixbugs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(import_quixbugs)


def _fake_quixbugs_root(tmp_path):
    root = tmp_path / "QuixBugs"
    (root / "python_programs").mkdir(parents=True)
    (root / "python_testcases").mkdir()
    (root / "python_programs" / "gcd.py").write_text("def gcd(a, b):\n    return a\n", encoding="utf-8")
    (root / "python_programs" / "bitcount.py").write_text("def bitcount(x):\n    return 0\n", encoding="utf-8")
    (root / "python_testcases" / "test_gcd.py").write_text("from python_programs.gcd import gcd\n", encoding="utf-8")
    (root / "python_testcases" / "test_bitcount.py").write_text(
        "from python_programs.bitcount import bitcount\n",
        encoding="utf-8",
    )
    (root / "python_testcases" / "test_missing_program.py").write_text("def test_missing(): pass\n", encoding="utf-8")
    return root


def test_discover_python_programs_matches_tests_to_program_files(tmp_path):
    root = _fake_quixbugs_root(tmp_path)

    programs = import_quixbugs.discover_python_programs(root)

    assert programs == ["bitcount", "gcd"]


def test_build_quixbugs_tasks_writes_public_benchmark_jsonl(tmp_path):
    root = _fake_quixbugs_root(tmp_path)
    output = tmp_path / "quixbugs.jsonl"

    tasks = import_quixbugs.build_quixbugs_tasks(
        quixbugs_root=root,
        programs=["gcd"],
        step_budget=7,
    )
    import_quixbugs.write_jsonl(tasks, output)
    loaded = load_public_benchmark(output)

    assert len(loaded) == 1
    task = loaded[0]
    assert task["id"] == "quixbugs__gcd"
    assert task["dataset"] == "quixbugs_python"
    assert task["source_repo"] == str(root)
    assert task["base_commit"] == "local-quixbugs"
    assert task["test_patch"] == ""
    assert task["test_command"] == "python -m pytest python_testcases/test_gcd.py -q"
    assert task["fail_to_pass"] == ["python -m pytest python_testcases/test_gcd.py -q"]
    assert task["step_budget"] == 7
    assert json.loads(output.read_text(encoding="utf-8").splitlines()[0])["category"] == "quixbugs-python-repair"


def test_build_quixbugs_tasks_rejects_unknown_program(tmp_path):
    root = _fake_quixbugs_root(tmp_path)

    with pytest.raises(ValueError, match="unknown QuixBugs Python program"):
        import_quixbugs.build_quixbugs_tasks(root, programs=["does_not_exist"])


def test_ensure_quixbugs_root_can_clone_when_requested(tmp_path, monkeypatch):
    clone_target = tmp_path / "cache" / "QuixBugs"

    def fake_run_git(args, cwd=None):
        del cwd
        assert args == ["clone", import_quixbugs.QUIXBUGS_REPO, str(clone_target)]
        clone_target.mkdir(parents=True)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(import_quixbugs, "_run_git", fake_run_git)

    assert import_quixbugs.ensure_quixbugs_root(clone_target, clone_if_missing=True) == clone_target


def test_ensure_quixbugs_root_requires_explicit_clone(tmp_path):
    with pytest.raises(FileNotFoundError, match="Clone it there or pass --clone-if-missing"):
        import_quixbugs.ensure_quixbugs_root(tmp_path / "missing", clone_if_missing=False)
