import argparse
import json
import subprocess
from pathlib import Path
from random import Random


QUIXBUGS_REPO = "https://github.com/jkoppel/QuixBugs.git"
DEFAULT_QUIXBUGS_ROOT = Path("artifacts/public-repo-cache/jkoppel__QuixBugs")
DEFAULT_OUTPUT = Path("benchmarks/quixbugs_python_sample.jsonl")


def _run_git(args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _git_head(path):
    if not (Path(path) / ".git").is_dir():
        return "local-quixbugs"
    result = _run_git(["rev-parse", "HEAD"], cwd=path)
    if result.returncode != 0:
        raise RuntimeError(f"could not read QuixBugs git HEAD: {result.stderr.strip()}")
    return result.stdout.strip()


def ensure_quixbugs_root(path, clone_if_missing=False):
    path = Path(path)
    if path.is_dir():
        return path
    if not clone_if_missing:
        raise FileNotFoundError(
            f"QuixBugs repo not found at {path}. Clone it there or pass --clone-if-missing."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(["clone", QUIXBUGS_REPO, str(path)])
    if result.returncode != 0:
        raise RuntimeError(f"git clone QuixBugs failed: {result.stderr.strip()}")
    return path


def discover_python_programs(quixbugs_root):
    root = Path(quixbugs_root)
    tests_dir = root / "python_testcases"
    programs_dir = root / "python_programs"
    if not tests_dir.is_dir() or not programs_dir.is_dir():
        raise ValueError(
            "QuixBugs root must contain python_testcases/ and python_programs/ directories"
        )

    programs = []
    for test_path in sorted(tests_dir.glob("test_*.py")):
        program = test_path.stem.removeprefix("test_")
        if (programs_dir / f"{program}.py").is_file():
            programs.append(program)
    if not programs:
        raise ValueError(f"no QuixBugs Python programs discovered under {root}")
    return programs


def build_quixbugs_task(program, quixbugs_root, base_commit, step_budget):
    test_path = f"python_testcases/test_{program}.py"
    program_path = f"python_programs/{program}.py"
    test_command = f"python -m pytest {test_path} -q"
    return {
        "id": f"quixbugs__{program}",
        "dataset": "quixbugs_python",
        "repo": "jkoppel/QuixBugs",
        "source_repo": str(Path(quixbugs_root)),
        "base_commit": base_commit,
        "problem_statement": (
            f"Fix the buggy QuixBugs Python implementation for `{program}`.\n"
            f"The implementation file is `{program_path}`.\n"
            f"The focused regression test is `{test_path}`.\n"
            "Keep the patch minimal and do not edit tests."
        ),
        "test_patch": "",
        "fail_to_pass": [test_command],
        "pass_to_pass": [],
        "test_command": test_command,
        "step_budget": int(step_budget),
        "category": "quixbugs-python-repair",
    }


def build_quixbugs_tasks(
    quixbugs_root,
    programs=None,
    limit=None,
    seed=0,
    step_budget=12,
):
    quixbugs_root = Path(quixbugs_root)
    discovered = discover_python_programs(quixbugs_root)
    if programs:
        requested = [str(program).strip() for program in programs if str(program).strip()]
        missing = [program for program in requested if program not in discovered]
        if missing:
            raise ValueError(f"unknown QuixBugs Python program(s): {', '.join(missing)}")
        selected = requested
    else:
        selected = list(discovered)
        Random(seed).shuffle(selected)
        if limit is not None:
            selected = selected[: int(limit)]

    base_commit = _git_head(quixbugs_root)
    return [
        build_quixbugs_task(
            program=program,
            quixbugs_root=quixbugs_root,
            base_commit=base_commit,
            step_budget=step_budget,
        )
        for program in selected
    ]


def write_jsonl(tasks, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n" for task in tasks),
        encoding="utf-8",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Materialize QuixBugs Python tasks into the public benchmark JSONL format."
    )
    parser.add_argument("--quixbugs-root", default=str(DEFAULT_QUIXBUGS_ROOT))
    parser.add_argument("--clone-if-missing", action="store_true")
    parser.add_argument("--program", action="append", default=None, help="Program name to include; repeatable.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum discovered programs to export.")
    parser.add_argument("--seed", type=int, default=0, help="Shuffle seed when --program is not used.")
    parser.add_argument("--step-budget", type=int, default=12)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)

    quixbugs_root = ensure_quixbugs_root(args.quixbugs_root, clone_if_missing=args.clone_if_missing)
    tasks = build_quixbugs_tasks(
        quixbugs_root=quixbugs_root,
        programs=args.program,
        limit=args.limit,
        seed=args.seed,
        step_budget=args.step_budget,
    )
    write_jsonl(tasks, args.output)
    print(f"wrote {len(tasks)} QuixBugs Python tasks to {args.output}")


if __name__ == "__main__":
    main()
