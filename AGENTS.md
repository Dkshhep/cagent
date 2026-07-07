# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Running environment

```bash
conda activate cagent
```

## Build & Development Commands

```bash
# Install dependencies (preferred)
uv sync

# Alternative: editable install
pip install -e .

# Lint / format
uv run ruff check .
uv run ruff format .

# Run all tests
uv run python -m pytest -q

# Run a single test file / single test
uv run python -m pytest tests/test_pico.py -q
uv run python -m pytest tests/test_pico.py::test_name -q

# Run the agent (interactive REPL) / one-shot task
uv run cagent --provider deepseek
uv run cagent --provider deepseek "inspect the test failures and propose a fix"
```

Note: the `cagent` console script (`pyproject.toml` `[project.scripts]`) maps to `cagent.cli:main`. `python -m cagent` is equivalent.

## Architecture

**`cagent` is a local terminal coding agent** — it reads your repo, uses tools (`read_file`, `write_file`, `run_shell`, etc.) via a model control loop, and persists session state to `.cagent/`. The CLI entry point is `main()` (`cli.py:303`), which builds a `CAgent` runtime via `build_agent()` (`cli.py:207`). `CAgent` is the primary class in `runtime.py:89`; `MiniAgent`/`SessionStore` are also exported from `cagent/__init__.py`.

### `.cagent/` directory layout

```
.cagent/
├── sessions/{id}.json     # Full session: history, memory, checkpoints, runtime_identity
├── runs/{run_id}/         # Per-ask() artifacts: task_state.json, trace.jsonl, report.json
└── memory/                # Durable persistent memory
    ├── MEMORY.md          # Index of durable topics
    └── topics/*.md        # Per-topic notes (project-conventions, key-decisions, etc.)
```

### Request flow (the core loop)

1. **`CAgent.ask(user_message)`** (`runtime.py:1002`) is the main entry — it runs the perception→decision→action→record loop until a final answer or stop condition.
2. **`ContextManager.build()`** (`context_manager.py:239`) assembles the prompt from 5 sections: prefix (tools + workspace), memory, relevant_memory (notes matching query), history, and current_request. It enforces a char budget and reduces sections in priority order; the current request is never trimmed.
3. **`model_client.complete(prompt, max_new_tokens, prompt_cache_key?, prompt_cache_retention?)`** sends the prompt to the provider and returns raw text.
4. **`CAgent.parse(raw)`** (`runtime.py:1543`, plus `parse_xml_tool` at `1607`) parses model output into `(kind, payload)` — `tool`, `final`, or `retry` (malformed).
5. If tool: **`CAgent.run_tool(name, args)`** (`runtime.py:1318`) executes with guardrails: existence check → validate args → repeated-call detection → approval gate → execute → capture workspace diff → update memory.
6. Loop repeats until `<final>` or step/retry limit hit (`max_steps * 3` malformed attempts before stopping).

### Model output format

The model must output exactly one of these per response:

- **JSON-style tool call**: `<tool>{"name":"tool_name","args":{...}}</tool>` — for short/structured arguments.
- **XML-style tool call**: `<tool name="write_file" path="file.py"><content>multi-line text</content></tool>` — for `write_file`/`patch_file`/`delegate` with multi-line content. Named children extracted: `<content>`, `<old_text>`, `<new_text>`, `<command>`, `<task>`, `<pattern>`, `<path>`. Body text is accepted as the primary argument.
- **Final answer**: `<final>your answer text</final>` — signals task completion.

Canonical per-tool examples live in `TOOL_EXAMPLES` (`tools.py:53`).

### Key modules

| Module | Role |
|--------|------|
| `runtime.py` | `CAgent` — control loop, tool execution, session management, checkpoint/resume |
| `cli.py` | Argument parsing, model client factory, `build_welcome()` (`cli.py:162`), REPL loop |
| `models.py` | `FakeModelClient`, `OllamaModelClient`, `OpenAICompatibleModelClient`, `AnthropicCompatibleModelClient`, `DeepSeekCompatibleModelClient` — all expose `complete(...)` |
| `tools.py` | 7 tools defined in `BASE_TOOL_SPECS` + `DELEGATE_TOOL_SPEC`: `list_files`, `read_file`, `search`, `run_shell`, `write_file`, `patch_file`, `delegate` (spawns a read-only child `CAgent`). Runners wired in `_TOOL_RUNNERS`. |
| `context_manager.py` | Budget-constrained prompt assembly with configurable section budgets and reduction order |
| `memory.py` | `LayeredMemory` (working memory + episodic notes) and `DurableMemoryStore` (persists to `.cagent/memory/`) |
| `workspace.py` | `WorkspaceContext.build()` — git repo snapshot (branch, status, commits, key docs) as a fingerprintable prefix |
| `config.py` | `.env` loading via `find_project_env()` (walks up from cwd); `provider_env()` for var lookup with legacy fallbacks |
| `run_store.py` | `RunStore` — writes `task_state.json`, `trace.jsonl`, `report.json` per run |
| `task_state.py` | `TaskState` dataclass — state machine per `ask()` (tool_steps, attempts, stop_reason, final_answer) |
| `mcp.py` | `McpManager` / `McpClient` — loads `.mcp.json`, starts servers, registers `mcp__*` tools to the top-level agent only |
| `evaluator.py` | `BenchmarkEvaluator` — runs `benchmarks/coding_tasks.json` against scripted `FakeModelClient` outputs with verifier scripts |
| `metrics.py` | Aggregation + ablation experiments (context/memory/recovery), security scenario suites, provider experiments |

### MCP integration

`build_agent()` connects external MCP servers from `<repo>/.mcp.json` on startup (`McpManager.from_path().start_all()`); failed servers degrade and are skipped. MCP tools register as `mcp__...` and are exposed **only to the top-level agent**, not to `delegate` child agents.

### Checkpoints & resume

Checkpoints (`create_checkpoint()` `runtime.py:844`, distillation variant `696`) are created at tool executions, run completion, and anomaly triggers (freshness mismatch, workspace mismatch, context reduction). On resume, `evaluate_resume_state()` (`runtime.py:233`) classifies into one of 5 statuses (`runtime.py:37-41`): `no-checkpoint`, `full-valid`, `partial-stale` (files changed externally → `[Stale paths: ...]` shown), `workspace-mismatch` (runtime identity changed), `schema-mismatch` (old checkpoint format).

### Durable memory promotion

When the user's message signals intent ("remember"/"save"/"persist" or Chinese equivalents) and the model's final answer contains tagged lines, they promote to `.cagent/memory/topics/*.md`:

```
Project convention: ...
Decision: ...
Dependency: ...
Preference: ...
```

Chinese forms (`项目约定：`, `决策：`, `依赖：`, `偏好：`) are also supported. `DurableMemoryStore` deduplicates by subject key and replaces outdated notes on the same topic.

### Feature flags

`feature_flags` dict on `CAgent` toggles: `memory` (tool results → working-memory summaries), `relevant_memory` (query-relevant note retrieval), `context_reduction` (budget-based trimming), `prompt_cache` (cache key sent to backend).

### Configuration priority

```
explicit CLI args > .env PICO_* vars > legacy env vars > code defaults
```

`provider_env()` in `config.py` implements this chain. `--provider` accepts `ollama`, `openai`, `anthropic`, `deepseek` (default `openai`). Copy `.env.example` → `.env` and fill only the provider you use.

| Variable | Purpose |
|----------|---------|
| `PICO_OPENAI_API_KEY` / `_API_BASE` / `_MODEL` | OpenAI-compatible backend |
| `PICO_ANTHROPIC_API_KEY` / `_API_BASE` / `_MODEL` | Anthropic-compatible backend (key falls back through legacy var names) |
| `PICO_DEEPSEEK_API_KEY` / `_API_BASE` / `_MODEL` | DeepSeek backend (uses dedicated `DeepSeekCompatibleModelClient`, Anthropic Messages API shape) |
| `PICO_SECRET_ENV_NAMES` | Comma-separated extra env var names to redact from traces |

### Metrics & experiments

`metrics.py` holds experiment harnesses: context stress matrix, large-scale memory ablation, recovery ablation, security suite (guardrail enforcement), and live-provider benchmark runs. Orchestrated by `scripts/run_large_scale_experiments.py`, `scripts/run_provider_experiments.py`, `scripts/collect_resume_metrics.py`.

### Tests

Tests drive the agent with `FakeModelClient` scripted outputs for determinism; fixture repos live in `tests/fixtures/` and are cloned into temp dirs by the benchmark harness. Core loop/parsing/output-format tests are in `tests/test_pico.py`. Other files: `test_context_manager.py`, `test_memory.py`, `test_metrics.py`, `test_run_store.py`, `test_task_state.py`, `test_safety_invariants.py`, `test_evaluator.py`, `test_mcp.py`, `test-compact.py`.

### Encoding convention

Several modules (`runtime.py`, `cli.py`, `models.py`, `tools.py`, `memory.py`, `context_manager.py`, `workspace.py`) use Chinese docstrings/comments explaining *why* a function exists and its role in the pipeline. Follow this convention in new code.
