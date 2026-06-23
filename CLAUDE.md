# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

##running environment
conda activate cagent


## Build & Development Commands

```bash
# Install dependencies (preferred)
uv sync

# Alternative: editable install
pip install -e .

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Run all tests
uv run python -m pytest -q

# Run a single test file
uv run python -m pytest tests/test_cagent.py -q

# Run a single test by name
uv run python -m pytest tests/test_cagent.py::test_agent_runs_tool_then_final -q

# Other test files
uv run python -m pytest tests/test_context_manager.py -q
uv run python -m pytest tests/test_memory.py -q
uv run python -m pytest tests/test_metrics.py -q
uv run python -m pytest tests/test_run_store.py -q
uv run python -m pytest tests/test_task_state.py -q
uv run python -m pytest tests/test_safety_invariants.py -q
uv run python -m pytest tests/test_evaluator.py -q

# Run the agent (interactive REPL)
uv run cagent --provider deepseek

# Run one-shot task
uv run cagent --provider deepseek "inspect the test failures and propose a fix"
```

## Architecture

**`cagent` is a local terminal coding agent** — it reads your repo, uses tools (read_file, write_file, run_shell, etc.) via a model control loop, and persists session state to `.cagent/`. The CLI entry point is `cagent/cli.py:main()`, which assembles a `CAgent` runtime instance via `build_agent()`.

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

1. **`CAgent.ask(user_message)`** (`runtime.py:773`) is the main entry — it runs the perception→decision→action→record loop until a final answer or stop condition.
2. **`ContextManager.build()`** (`context_manager.py:78`) assembles the prompt from 5 sections: prefix (tools + workspace), memory, relevant_memory (notes matching query), history, and current_request. It has a total char budget (default 12,000) and reduces sections in priority order: `relevant_memory → history → memory → prefix`. The current request is never trimmed.
3. **`model_client.complete(prompt, max_tokens)`** sends the prompt to the provider and returns raw text.
4. **`CAgent.parse(raw)`** (`runtime.py:1275`) parses the model output into `(kind, payload)` — `tool` (JSON tool call), `final` (answer), or `retry` (malformed).
5. If tool: **`CAgent.run_tool(name, args)`** (`runtime.py:1050`) executes with guardrails: existence check → validate args → repeated-call detection → approval gate → execute → capture workspace diff → update memory.
6. Loop repeats until `<final>` or step/retry limit hit.

### Model output format

The model must output exactly one of these per response:

- **JSON-style tool call**: `<tool>{"name":"tool_name","args":{...}}</tool>` — for tools with short/structured arguments.
- **XML-style tool call**: `<tool name="write_file" path="file.py"><content>multi-line text</content></tool>` — for write_file/patch_file/delegate with multi-line content. The body text is also accepted as the primary argument (e.g., body text → `content` for write_file, `task` for delegate).
- **Final answer**: `<final>your answer text</final>` — signals task completion.

The parser (`CAgent.parse()`) supports both styles, extracting named child elements (`<content>`, `<old_text>`, `<new_text>`, `<command>`, `<task>`, `<pattern>`, `<path>`) from XML-style calls. Malformed output returns `retry` with a notice, up to `max_steps * 3` attempts before the agent stops.

### Key modules

| Module | Role |
|--------|------|
| `runtime.py` | `CAgent` class — the agent control loop, tool execution, session management, checkpoint/resume logic |
| `cli.py` | Argument parsing, model client factory, welcome screen, REPL loop |
| `models.py` | `OllamaModelClient`, `OpenAICompatibleModelClient`, `AnthropicCompatibleModelClient`, `FakeModelClient` — all expose `complete(prompt, max_tokens, prompt_cache_key?, prompt_cache_retention?)` |
| `tools.py` | 7 tool definitions: `list_files`, `read_file`, `search`, `run_shell`, `write_file`, `patch_file`, `delegate` (spawns a read-only child `CAgent`). Each has a schema, risky flag, validator, and runner. |
| `context_manager.py` | Budget-constrained prompt assembly with configurable section budgets and reduction order |
| `memory.py` | `LayeredMemory` — working memory (task summary, recent files, file summaries), episodic notes, and `DurableMemoryStore` (persists to `.cagent/memory/` with `MEMORY.md` index + `topics/*.md`) |
| `workspace.py` | `WorkspaceContext.build()` — captures git repo snapshot (branch, status, recent commits, key doc contents) as a fingerprintable prefix |
| `config.py` | `.env` loading with `find_project_env()` — walks up from cwd; `provider_env()` for env var lookup with legacy fallbacks |
| `run_store.py` | `RunStore` — writes `task_state.json`, `trace.jsonl`, `report.json` per run into `.cagent/runs/<run_id>/` |
| `task_state.py` | `TaskState` dataclass — state machine tracking tool_steps, attempts, stop_reason, final_answer for one `ask()` invocation |
| `evaluator.py` | `BenchmarkEvaluator` — runs benchmark tasks from `benchmarks/coding_tasks.json` against scripted `FakeModelClient` outputs with verifier scripts |
| `metrics.py` | Aggregation, ablation experiments (context/memory/recovery), security scenario suites, provider experiments |

### Tools reference

| Tool | Args | Risky | Description |
|------|------|-------|-------------|
| `list_files` | `path="."` | No | List files in the workspace |
| `read_file` | `path`, `start=1`, `end=200` | No | Read a UTF-8 file by line range |
| `search` | `pattern`, `path="."` | No | Search with `rg` or simple fallback |
| `run_shell` | `command`, `timeout=20` | **Yes** | Run a shell command in repo root (timeout 1–120s) |
| `write_file` | `path`, `content` | **Yes** | Write a text file |
| `patch_file` | `path`, `old_text`, `new_text` | **Yes** | Replace one exact text block (must occur exactly once) |
| `delegate` | `task`, `max_steps=3` | No | Spawn a read-only child CAgent to investigate |

The `delegate` tool (`tools.py:261`) spawns a **read-only child CAgent** with `approval_policy="never"` and fewer steps. The child inherits the parent's model client and workspace but runs in a separate session. Its final answer is returned as `delegate_result:` to the parent. Depth is capped by `max_depth` (default 1) — once at max depth, the delegate tool is not even registered, so the model can't call it.

### MCP client (external tools)

cagent can act as an **MCP client** (`cagent/mcp.py`), connecting to external stdio MCP servers and exposing their tools to the model alongside the built-ins. This is opt-in: with no config, cagent behaves exactly as before.

- **Config**: declare servers in `<repo_root>/.mcp.json` using the standard `mcpServers` structure (copy from `.mcp.json.example`). Each entry has `command` (required), `args`, and `env`. Missing or corrupt config is ignored gracefully — cagent still starts.
- **Transport**: local stdio subprocesses only. `McpClient` speaks synchronous line-delimited JSON-RPC (`initialize` → `tools/list` → `tools/call`) over the subprocess pipes, with a per-request timeout enforced via a background reader thread.
- **Lifecycle**: `build_agent()` constructs an `McpManager` from `.mcp.json`, calls `start_all()` (connecting each server, degrading past any that fail to start), and passes it to `CAgent`. `main()` calls `close_all()` in a `finally` so subprocesses never leak. Only the **top-level agent** (`depth == 0`) registers MCP tools — delegate children are read-only and would have them blocked anyway.
- **Naming**: discovered tools are namespaced `mcp__<server>__<tool>` to avoid collision with built-ins. Their JSON-Schema `inputSchema` is rendered into the prompt as cagent's display schema (`{field: "type"}`, optional fields suffixed `?`).
- **Guardrails**: every MCP tool is `risky=True`, so it flows through the same `run_tool` pipeline as `run_shell`/`write_file` — generic required-arg validation, repeated-call detection, the `--approval` gate, and `read_only` blocking all apply unchanged. Call failures return an `mcp_error: ...` string the model can read and react to, rather than crashing the loop.

### Prompt cache (only on OpenAI-compatible to right.codes/openai.com)

The `supports_prompt_cache` flag is `True` only when the base URL contains `openai.com` or `right.codes`. When active, the stable prefix hash (tools + workspace sections) is sent as `prompt_cache_key` so the model can reuse cached prefix computation across turns.

### Tool guardrails

All tool calls go through `CAgent.run_tool()` which enforces:
- **Path sandboxing**: `CAgent.path()` (`runtime.py:1401`) resolves paths relative to repo root and rejects `../` escapes and symlink escapes.
- **Validation**: Each tool has specific validation in `tools.py:validate_tool()` (e.g., `patch_file` requires exactly one occurrence of `old_text`).
- **Repeated call detection**: Rejects if the last 2 tool history entries are identical name+args.
- **Approval gate**: `--approval ask|auto|never`; `read_only` mode blocks all risky tools.
- **Shell env isolation**: `run_shell` uses a minimal allowlisted env (`HOME`, `PATH`, `PWD`, etc.) rather than the full parent environment.

### Session persistence & checkpointing

Sessions are saved to `.cagent/sessions/{id}.json` containing `history`, `memory`, `checkpoints`, `runtime_identity`, and `resume_state`. Sessions can be resumed with `--resume <id|latest>`.

Checkpoints (`runtime.py:618`) are created at every tool execution, run completion, and on anomaly triggers (freshness mismatch, workspace mismatch, context reduction). They capture: current goal, completed/excluded items, current blocker, next step, key file paths with freshness hashes, and runtime identity snapshot.

On resume, `evaluate_resume_state()` (`runtime.py:228`) classifies the resume state into one of 5 statuses:
- **`no-checkpoint`**: No prior checkpoint exists — fresh start.
- **`full-valid`**: World state unchanged — safe to continue from where it left off.
- **`partial-stale`**: Some key files were modified externally — their summaries are invalidated, and the model sees `[Stale paths: ...]` in the prompt.
- **`workspace-mismatch`**: Runtime identity changed (different model, cwd, approval policy, etc.) — model sees the mismatch in the prompt.
- **`schema-mismatch`**: Checkpoint format is from an older version — cannot be used.

### Durable memory promotion

When the model's final answer contains specially formatted lines, they can be promoted to persistent memory (`.cagent/memory/topics/*.md`):

```
Project convention: Preserve benchmark artifacts under artifacts/.
Decision: Keep harness regression deterministic.
Dependency: Python 3.10+ required.
Preference: Use uv for package management.
```

Chinese equivalents (`项目约定：`, `决策：`, `依赖：`, `偏好：`) are also supported. Promotion is triggered when the user's message contains intent keywords like "remember", "save", "persist" (or Chinese equivalents). The `DurableMemoryStore` deduplicates by subject key and replaces outdated notes on the same topic.

### Feature flags

Runtime behavior is controlled by `feature_flags` dict on `CAgent`:
- `memory` — enables working memory updates (tool results → summaries)
- `relevant_memory` — enables query-relevant note retrieval in prompt
- `context_reduction` — enables budget-based prompt section trimming
- `prompt_cache` — enables sending cache key to model backend

### Metrics & ablation experiments (`metrics.py`)

The `metrics.py` module contains experiment harnesses for measuring the impact of design decisions:
- **Context ablation** (`run_context_stress_matrix`): Tests prompt compression across history/note/request size combinations.
- **Memory ablation** (`run_large_scale_memory_experiment`): Tests whether working memory reduces repeated file reads across 12 tasks.
- **Recovery ablation** (`run_recovery_ablation_v2`): Tests checkpoint resume success rates across stale, drift, and schema-mismatch scenarios.
- **Security suite** (`run_security_experiment_suite`): Validates guardrail enforcement (path escape, symlink escape, approval deny, read-only block, etc.).
- **Provider experiments** (`run_provider_experiments`): Runs the full benchmark against real model backends (GPT, Claude, DeepSeek).

Scripts in `scripts/` orchestrate these experiments:
- `scripts/run_large_scale_experiments.py` — full synthetic experiment suite
- `scripts/run_provider_experiments.py` — benchmark against live model backends

### Configuration priority

```
explicit CLI args > .env PICO_* vars > legacy env vars > code defaults
```

The `provider_env()` function in `config.py` implements this chain.

Key environment variables (set in `.env`, copy from `.env.example`):

| Variable | Purpose |
|----------|---------|
| `PICO_OPENAI_API_KEY` | OpenAI-compatible API key |
| `PICO_OPENAI_API_BASE` | OpenAI-compatible base URL |
| `PICO_OPENAI_MODEL` | OpenAI model name (default: `gpt-5.4`) |
| `PICO_ANTHROPIC_API_KEY` | Anthropic-compatible API key (falls back through 5 other var names) |
| `PICO_ANTHROPIC_API_BASE` | Anthropic-compatible base URL |
| `PICO_ANTHROPIC_MODEL` | Anthropic model name (default: `claude-sonnet-4-6`) |
| `PICO_DEEPSEEK_API_KEY` | DeepSeek API key |
| `PICO_DEEPSEEK_API_BASE` | DeepSeek base URL (default: `https://api.deepseek.com/anthropic`) |
| `PICO_DEEPSEEK_MODEL` | DeepSeek model name (default: `deepseek-v4-pro`) |
| `PICO_SECRET_ENV_NAMES` | Comma-separated extra env var names to redact from traces |

DeepSeek uses the `AnthropicCompatibleModelClient` under the hood — it speaks the Anthropic Messages API.

### Test structure

Tests use `FakeModelClient` with scripted outputs to deterministically test agent behavior. Fixture repos are in `tests/fixtures/`. The benchmark harness in `evaluator.py` clones fixture repos into temp directories for isolated deterministic runs.

Test files:
- `tests/test_cagent.py` — core agent loop, tool parsing, output format
- `tests/test_context_manager.py` — prompt assembly, budget reduction
- `tests/test_memory.py` — layered memory, durable store, note retrieval
- `tests/test_metrics.py` — ablation experiments, security scenarios
- `tests/test_run_store.py` — artifact persistence and atomic writes
- `tests/test_task_state.py` — state machine transitions
- `tests/test_safety_invariants.py` — guardrail enforcement
- `tests/test_evaluator.py` — benchmark harness, verifier scripts

### Encoding

The codebase has Chinese docstrings in several modules (`runtime.py`, `cli.py`, `models.py`, `tools.py`, `memory.py`, `context_manager.py`, `workspace.py`). These document the rationale for each function. New code should follow the existing docstring convention (Chinese explanations of "why" the function exists and its role in the agent pipeline).
