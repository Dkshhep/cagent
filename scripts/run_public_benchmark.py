import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cagent.config import load_project_env, provider_env  # noqa: E402
from cagent.models import AnthropicCompatibleModelClient, DeepSeekCompatibleModelClient, OpenAICompatibleModelClient  # noqa: E402
from cagent.public_benchmarks import (  # noqa: E402
    DEFAULT_PUBLIC_MAX_NEW_TOKENS,
    DEFAULT_PUBLIC_REPO_CACHE_ROOT,
    run_public_benchmark,
)

DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"


def _provider_profile(provider, model_override=None, base_url_override=None):
    load_project_env(Path.cwd())
    if provider == "openai":
        api_key = provider_env("PICO_OPENAI_API_KEY", ("OPENAI_API_KEY",))
        if not api_key:
            raise RuntimeError("PICO_OPENAI_API_KEY or OPENAI_API_KEY missing")
        return {
            "provider": "openai",
            "model": model_override or provider_env("PICO_OPENAI_MODEL", ("OPENAI_MODEL",), DEFAULT_OPENAI_MODEL),
            "base_url": base_url_override or provider_env("PICO_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL),
            "api_key": api_key,
        }
    if provider == "anthropic":
        api_key = provider_env(
            "PICO_ANTHROPIC_API_KEY",
            ("ANTHROPIC_API_KEY", "PICO_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "PICO_OPENAI_API_KEY", "OPENAI_API_KEY"),
        )
        if not api_key:
            raise RuntimeError("PICO_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY missing")
        return {
            "provider": "anthropic",
            "model": model_override or provider_env("PICO_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",), DEFAULT_ANTHROPIC_MODEL),
            "base_url": base_url_override or provider_env("PICO_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), DEFAULT_ANTHROPIC_BASE_URL),
            "api_key": api_key,
        }
    api_key = provider_env("PICO_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
    if not api_key:
        raise RuntimeError("PICO_DEEPSEEK_API_KEY or DEEPSEEK_API_KEY missing")
    return {
        "provider": "deepseek",
        "model": model_override or provider_env("PICO_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",), DEFAULT_DEEPSEEK_MODEL),
        "base_url": base_url_override or provider_env("PICO_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), DEFAULT_DEEPSEEK_BASE_URL),
        "api_key": api_key,
    }


def _client_factory(profile, temperature, timeout):
    def factory(task, workspace):
        del task, workspace
        if profile["provider"] == "openai":
            return OpenAICompatibleModelClient(
                model=profile["model"],
                base_url=profile["base_url"],
                api_key=profile["api_key"],
                temperature=temperature,
                timeout=timeout,
            )
        if profile["provider"] == "anthropic":
            return AnthropicCompatibleModelClient(
                model=profile["model"],
                base_url=profile["base_url"],
                api_key=profile["api_key"],
                temperature=temperature,
                timeout=timeout,
            )
        return DeepSeekCompatibleModelClient(
            model=profile["model"],
            base_url=profile["base_url"],
            api_key=profile["api_key"],
            temperature=temperature,
            timeout=timeout,
        )

    return factory


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run materialized public benchmark tasks with CAgent.")
    parser.add_argument("--benchmark-path", default="benchmarks/swebench_lite_sample.jsonl")
    parser.add_argument("--artifact-path", default="artifacts/swebench-lite-sample.json")
    parser.add_argument("--workspace-root", default="artifacts/public-benchmark-workspaces")
    parser.add_argument("--repo-cache-root", default=str(DEFAULT_PUBLIC_REPO_CACHE_ROOT))
    parser.add_argument("--provider", choices=("openai", "anthropic", "deepseek"), default="openai")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_PUBLIC_MAX_NEW_TOKENS)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--offline-cache", action="store_true")
    parser.add_argument(
        "--verifier-python",
        default=None,
        help="Python interpreter used to run the verifier test command (e.g. a conda env with the target repo's deps installed). Defaults to the benchmark runner's own interpreter.",
    )
    parser.add_argument(
        "--agent-shell-python",
        default=None,
        help="Give the agent's run_shell a working `python` by prepending this interpreter's directory (and its Scripts/ on Windows) to PATH, so the agent can run the target repo's tests itself and self-correct. Pass the same env as --verifier-python to let the agent test django patches.",
    )
    args = parser.parse_args(argv)

    shell_path_prepend = None
    if args.agent_shell_python:
        interpreter = Path(args.agent_shell_python).resolve()
        env_dir = interpreter.parent
        # conda 环境的可执行脚本在 Scripts/（Windows）或 bin/（POSIX）；把解释器
        # 所在目录和脚本目录都 prepend，让裸 `python` 及 pip 等命令都可用。
        candidates = [env_dir, env_dir / "Scripts", env_dir / "bin"]
        shell_path_prepend = [str(p) for p in candidates if p.exists()]

    profile = _provider_profile(args.provider, model_override=args.model, base_url_override=args.base_url)
    artifact = run_public_benchmark(
        benchmark_path=args.benchmark_path,
        artifact_path=args.artifact_path,
        workspace_root=args.workspace_root,
        repo_cache_root=args.repo_cache_root,
        model_name=profile["provider"],
        model_version=profile["model"],
        max_new_tokens=args.max_new_tokens,
        model_client_factory=_client_factory(profile, args.temperature, args.timeout),
        limit=args.limit,
        offline_cache=args.offline_cache,
        verifier_python=args.verifier_python,
        shell_path_prepend=shell_path_prepend,
    )
    summary = artifact["summary"]
    print(
        f"completed {summary['total_tasks']} tasks: "
        f"pass_rate={summary['pass_rate']:.2%}, verifier_pass_rate={summary['verifier_pass_rate']:.2%}"
    )


if __name__ == "__main__":
    main()
