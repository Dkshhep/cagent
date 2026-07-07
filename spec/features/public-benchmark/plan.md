# Public Benchmark 接入计划：SWE-bench Lite

## 背景

当前 `cagent` 的测评以 mock / fixture benchmark 为主：`benchmarks/coding_tasks.json` 定义固定任务，`BenchmarkEvaluator` 复制本地 fixture、使用 `FakeModelClient` 或 provider client 执行、再通过 verifier 汇总 artifact。这一层非常适合验证控制流、工具协议、安全边界、checkpoint / resume、memory promotion 等平台不变量。

它不适合单独证明真实 coding 能力。mock 输出由 `SCRIPTED_MODEL_OUTPUTS` 驱动，任务空间较小，无法覆盖真实仓库里的跨文件理解、依赖安装、测试定位、issue 修复和模型泛化。因此公共数据集测评应作为慢速真实能力层补充，而不是替换现有 fixture benchmark。

## 目标

第一版接入 SWE-bench Lite，形成一个可复现、可小规模运行的 public benchmark 通道。

- 保留现有 `benchmarks/coding_tasks.json` 作为快速 mock 回归。
- 新增 SWE-bench Lite adapter，把公共数据集样本转换成 `cagent` 可执行的统一 task。
- 默认运行小子集，例如 5 到 20 个任务，先验证 runner、artifact 和失败分类。
- 默认使用 OpenAI provider 跑真实模型测评；其他 provider 作为显式参数覆盖。
- 测试和手动运行环境默认先执行 `conda activate pico`。
- 不在第一版强行完整复刻官方 SWE-bench harness；官方对齐放到后续阶段。

## 设计

### 数据流

```text
SWE-bench Lite dataset
  -> scripts/import_swebench_lite.py
  -> benchmarks/swebench_lite_sample.jsonl
  -> public benchmark adapter
  -> isolated workspace at base_commit
  -> CAgent.ask(problem_statement)
  -> apply test_patch / run verifier
  -> artifacts/swebench-lite-*.json
```

### Task normalization

公共 benchmark 任务需要先物化为本地 JSON/JSONL，避免 evaluator 运行时依赖网络。

每条 SWE-bench Lite task 规范化为：

```json
{
  "id": "django__django-12345",
  "dataset": "swebench_lite",
  "repo": "django/django",
  "base_commit": "abcdef123456",
  "problem_statement": "Issue text from SWE-bench Lite.",
  "test_patch": "diff --git ...",
  "fail_to_pass": ["tests/path/test_file.py::test_case"],
  "pass_to_pass": ["tests/path/test_file.py::test_existing"],
  "step_budget": 40,
  "category": "real-bugfix"
}
```

第一版 adapter 只要求字段存在并类型正确，不引入复杂 schema 版本迁移。后续如果需要兼容多个公共数据源，再提升为独立 `schema_version`。

### Workspace setup

每个 public task 必须使用独立 workspace：

- 依据 `repo` 和 `base_commit` 准备源码目录。
- checkout 到 `base_commit` 后再启动 `CAgent`。
- `.cagent/`、run artifact、trace、report 全部保存在该任务 workspace 内。
- verifier 在 agent 结束后运行，不混入 agent 的工具调用预算。

第一版实现可以假设 repo 已经提前 clone 到本地 cache；下载、clone、依赖安装作为脚本职责，不放进 evaluator 核心路径。

### Prompt

传给 agent 的 prompt 由 adapter 生成，包含：

- issue / problem statement。
- 明确要求修改代码并通过相关测试。
- 提醒 agent 可以读取文件、搜索、运行测试，但不要改无关文件。

不要把 `test_patch` 原文直接塞给 agent，避免泄露答案侧测试细节。`test_patch` 只给 verifier 使用。

### Verifier

第一版 verifier 采用最小可行策略：

- agent 修改完成后，应用 `test_patch`。
- 优先运行 `fail_to_pass` 中列出的测试。
- 如果测试命令不可推断，记录为 `setup_failed` 或 `verifier_unavailable`，不伪造 pass。

后续阶段再对齐官方 SWE-bench 容器化 harness，以降低不同项目依赖环境带来的噪声。

### Artifact

public benchmark artifact 继续沿用现有 benchmark 汇总口径：

- `pass_rate`
- `verifier_pass_rate`
- `within_budget_rate`
- `failure_category_counts`
- `rows`

每条 row 额外记录：

- `dataset`
- `repo`
- `base_commit`
- `patch_digest`
- `diff_stat`
- `test_command`
- `verifier_stdout`
- `verifier_stderr`
- `failure_category`

失败分类至少包含：

- `setup_failed`
- `agent_failed`
- `verifier_failed`
- `budget_exceeded`
- `missing_diff`
- `failure_stop_reason`

## 入口规划

### `scripts/import_swebench_lite.py`

职责：

- 从 SWE-bench Lite 数据源导出小型本地 JSON/JSONL 清单。
- 支持 `--limit`、`--output`、`--seed`。
- 可选使用 `datasets` 包，但不把它加入项目硬依赖。
- 输出路径建议为 `benchmarks/swebench_lite_sample.jsonl`。

预期命令：

```bash
python scripts/import_swebench_lite.py --limit 20 --output benchmarks/swebench_lite_sample.jsonl
```

### `scripts/run_public_benchmark.py`

职责：

- 读取物化后的 public benchmark task。
- 根据 `--provider` 构造真实 model client；未传时默认使用 `openai`。
- 调用 public benchmark runner。
- 写出 artifact。

预期命令：

```bash
conda activate pico
python scripts/run_public_benchmark.py \
  --benchmark-path benchmarks/swebench_lite_sample.jsonl \
  --artifact-path artifacts/swebench-lite-sample.json \
  --workspace-root artifacts/public-benchmark-workspaces \
  --provider openai \
  --limit 5
```

## 测试计划

文档阶段不新增代码测试。

实现阶段的本地测试与手动 benchmark 默认在 `pico` 环境中运行：

```bash
conda activate pico
```

实现阶段新增：

- `tests/test_public_benchmark.py`
- 本地伪 SWE-bench fixture repo，用于模拟 `repo`、`base_commit`、`problem_statement`、`test_patch`。
- adapter schema 校验测试：缺少 `repo`、`base_commit`、`problem_statement`、`test_patch` 时拒绝。
- workspace 隔离测试：每个 task 使用独立目录，不污染原始 fixture。
- verifier 测试：agent patch 后应用 `test_patch`，测试通过则 row pass。
- artifact 汇总测试：public rows 能被汇总为现有 pass / fail / failure category 口径。

真实 SWE-bench Lite 运行标记为 slow/manual，不进入默认 CI。

## 分阶段落地

### Phase 1：文档与本地 schema

- 增加本 spec。
- 定义 SWE-bench Lite 物化 task 字段。
- 明确 public benchmark 不替换现有 mock benchmark。

### Phase 2：伪 SWE-bench fixture

- 新增本地最小 fixture repo。
- 新增 public task loader 和 schema 校验。
- 用 `FakeModelClient` 跑通一条伪任务，证明 runner / verifier / artifact 通路。

### Phase 3：SWE-bench Lite 小子集

- 增加 import 脚本。
- 支持本地 repo cache。
- 默认使用 OpenAI provider 跑 5 到 20 条任务。
- 输出 `artifacts/swebench-lite-sample.json`。

### Phase 4：官方 harness 对齐

- 对齐官方 SWE-bench 的环境构建和测试运行方式。
- 分离 setup failure 与模型修复失败。
- 扩展到更大子集，并记录成本、耗时、工具步数、重试次数。

## Assumptions

- 第一公共数据集固定为 SWE-bench Lite。
- 第一版只沉淀计划，不修改 `cagent/evaluator.py`。
- 测试环境默认使用 `conda activate pico`。
- 真实模型测评默认使用 OpenAI provider。
- 后续实现优先复用现有 benchmark artifact 结构。
- 现有 `benchmarks/coding_tasks.json` 继续作为快速 mock 回归，不被公共数据集替换。
