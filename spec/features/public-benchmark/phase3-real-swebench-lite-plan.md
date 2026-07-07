# SWE-bench Lite 真实小样本闭环计划

## Summary

本阶段把 public benchmark 从“伪 SWE-bench fixture + FakeModelClient”推进到“真实 SWE-bench Lite 小样本 + OpenAI provider + 本地 repo cache”。目标是让 `scripts/run_public_benchmark.py` 能实际跑 1 到 5 条真实 SWE-bench Lite 任务，并输出可诊断 artifact。

当前不替换 mock / fixture 测评；真实 SWE-bench Lite 作为 slow/manual benchmark。

## Key Changes

- 新增 repo cache 准备能力：
  - 默认 cache 根目录：`artifacts/public-repo-cache`
  - repo 路径规则：`owner/repo` -> `artifacts/public-repo-cache/owner__repo`
  - 如果 cache 不存在，clone `https://github.com/{owner}/{repo}.git`
  - 如果 cache 已存在且配置了 `origin` remote，执行 fetch 更新引用
  - 每个 task 仍复制到独立 workspace，再 checkout `base_commit`

- 更新 public benchmark runner 行为：
  - `source_repo` 继续支持本地伪 fixture 测试
  - 真实 SWE-bench Lite 默认通过 `repo_cache_root + repo` 找源码
  - setup 阶段失败明确记录为 `setup_failed`
  - 默认 provider 保持 `openai`
  - 默认运行环境记录为 `conda activate pico`

- 完善脚本入口：
  - `scripts/import_swebench_lite.py` 继续负责物化 JSONL
  - `scripts/run_public_benchmark.py` 默认使用 `--repo-cache-root artifacts/public-repo-cache`
  - 示例命令：

```bash
conda activate pico
python scripts/import_swebench_lite.py --limit 5 --output benchmarks/swebench_lite_sample.jsonl

python scripts/run_public_benchmark.py \
  --benchmark-path benchmarks/swebench_lite_sample.jsonl \
  --artifact-path artifacts/swebench-lite-sample.json \
  --workspace-root artifacts/public-benchmark-workspaces \
  --repo-cache-root artifacts/public-repo-cache \
  --provider openai \
  --limit 1
```

## Test Plan

- 扩展 `tests/test_public_benchmark.py`：
  - repo cache path 从 `owner/repo` 正确映射为 `owner__repo`
  - cache 已存在时不要求 `source_repo`
  - workspace 从 cache 复制，且不污染 cache
  - git repo workspace 能 checkout 到 `base_commit`
  - clone/fetch 失败时 row 标记为 `setup_failed`

- 保留现有伪 fixture 测试：
  - schema 校验
  - verifier 应用 `test_patch`
  - artifact 汇总
  - `missing_diff` 分类

- 验证命令：

```bash
conda activate pico
python -m pytest tests/test_public_benchmark.py tests/test_evaluator.py -q
python -m py_compile cagent/public_benchmarks.py scripts/import_swebench_lite.py scripts/run_public_benchmark.py
```

## Assumptions

- 真实网络 clone / dataset 下载属于手动或需授权操作。
- 第一轮真实运行只跑 `--limit 1`，成功后再扩大到 5 到 20 条。
- 不在本阶段接官方 SWE-bench Docker harness；先跑通小样本闭环。
