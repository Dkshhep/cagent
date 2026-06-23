"""极小 MCP stdio server,仅供测试。按行读 JSON-RPC 请求,按行回 JSON。"""
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the text argument.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "echo"}}
        elif method == "notifications/initialized":
            continue  # 通知无需回复
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            text = req.get("params", {}).get("arguments", {}).get("text", "")
            result = {"content": [{"type": "text", "text": f"echo:{text}"}]}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "method not found"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
