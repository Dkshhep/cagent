import argparse
import json
from pathlib import Path
from random import Random


def _normalize_row(row, default_step_budget):
    def list_field(*names):
        for name in names:
            value = row.get(name)
            if value is None:
                continue
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    return [value]
                if isinstance(parsed, list):
                    return parsed
                return [value]
        return []

    return {
        "id": str(row.get("instance_id") or row.get("id") or "").strip(),
        "dataset": "swebench_lite",
        "repo": str(row.get("repo", "")).strip(),
        "base_commit": str(row.get("base_commit", "")).strip(),
        "problem_statement": str(row.get("problem_statement", "")).strip(),
        "test_patch": str(row.get("test_patch", "")),
        "fail_to_pass": list_field("FAIL_TO_PASS", "fail_to_pass"),
        "pass_to_pass": list_field("PASS_TO_PASS", "pass_to_pass"),
        "step_budget": int(default_step_budget),
        "category": "real-bugfix",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export a small materialized SWE-bench Lite task set.")
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite", help="Hugging Face dataset name.")
    parser.add_argument("--split", default="test", help="Dataset split to export.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of tasks to export.")
    parser.add_argument("--seed", type=int, default=0, help="Shuffle seed before applying --limit.")
    parser.add_argument("--step-budget", type=int, default=40, help="Default CAgent step budget for each task.")
    parser.add_argument("--output", default="benchmarks/swebench_lite_sample.jsonl", help="Output JSONL path.")
    args = parser.parse_args(argv)

    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit("The optional `datasets` package is required for import. Install it in the pico environment first.") from exc

    rows = list(load_dataset(args.dataset, split=args.split))
    Random(args.seed).shuffle(rows)
    tasks = [_normalize_row(row, args.step_budget) for row in rows[: args.limit]]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n" for task in tasks),
        encoding="utf-8",
    )
    print(f"wrote {len(tasks)} SWE-bench Lite tasks to {output}")


if __name__ == "__main__":
    main()
