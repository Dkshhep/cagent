import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cagent.config import load_project_env, provider_env  # noqa: E402
from cagent.models import _normalize_versioned_base_url  # noqa: E402


def _string_summary(value):
    if not isinstance(value, str):
        return {"type": type(value).__name__}
    return {
        "type": "str",
        "length": len(value),
        "preview": value[:80],
    }


def _content_shape(content):
    if not isinstance(content, list):
        return {"type": type(content).__name__}
    items = []
    for item in content:
        if not isinstance(item, dict):
            items.append({"type": type(item).__name__})
            continue
        items.append(
            {
                "type": item.get("type"),
                "keys": sorted(str(key) for key in item.keys()),
                "text": _string_summary(item.get("text")),
                "summary": _string_summary(item.get("summary")),
            }
        )
    return items


def response_shape(data):
    output = data.get("output")
    output_items = output if isinstance(output, list) else []
    return {
        "top_level_keys": sorted(str(key) for key in data.keys()),
        "id": data.get("id"),
        "model": data.get("model"),
        "status": data.get("status"),
        "incomplete_details": data.get("incomplete_details"),
        "output_text": _string_summary(data.get("output_text")),
        "usage": data.get("usage"),
        "output": [
            {
                "type": item.get("type") if isinstance(item, dict) else type(item).__name__,
                "status": item.get("status") if isinstance(item, dict) else None,
                "keys": sorted(str(key) for key in item.keys()) if isinstance(item, dict) else [],
                "content": _content_shape(item.get("content")) if isinstance(item, dict) else None,
                "summary": _content_shape(item.get("summary")) if isinstance(item, dict) else None,
            }
            for item in output_items
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print a redacted OpenAI Responses API shape.")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--endpoint", choices=("responses", "chat"), default="responses")
    parser.add_argument("--input-mode", choices=("content-list", "string"), default="content-list")
    parser.add_argument("--max-output-tokens", type=int, default=256)
    args = parser.parse_args(argv)

    load_project_env(Path.cwd())
    api_key = provider_env("PICO_OPENAI_API_KEY", ("OPENAI_API_KEY",))
    if not api_key:
        raise SystemExit("PICO_OPENAI_API_KEY or OPENAI_API_KEY missing")
    model = args.model or provider_env("PICO_OPENAI_MODEL", ("OPENAI_MODEL",), "gpt-5.4")
    base_url = _normalize_versioned_base_url(
        args.base_url or provider_env("PICO_OPENAI_API_BASE", ("OPENAI_API_BASE",), "https://www.right.codes/codex/v1")
    )
    if args.endpoint == "chat":
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Return exactly: <final>ok</final>"}],
            "max_tokens": args.max_output_tokens,
            "stream": False,
        }
        url = base_url + "/chat/completions"
    else:
        input_value = "Return exactly: <final>ok</final>"
        if args.input_mode == "content-list":
            input_value = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Return exactly: <final>ok</final>",
                        }
                    ],
                }
            ]
        payload = {
            "model": model,
            "input": input_value,
            "max_output_tokens": args.max_output_tokens,
            "stream": False,
        }
        url = base_url + "/responses"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "cagent/response-shape-inspector",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    shape = response_shape(data)
    if args.endpoint == "chat":
        choices = data.get("choices") if isinstance(data, dict) else None
        shape["choices"] = [
            {
                "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
                "message_keys": sorted(str(key) for key in (choice.get("message") or {}).keys())
                if isinstance(choice, dict)
                else [],
                "content": _string_summary((choice.get("message") or {}).get("content"))
                if isinstance(choice, dict)
                else {"type": "unknown"},
            }
            for choice in (choices if isinstance(choices, list) else [])
        ]
    print(json.dumps(shape, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
