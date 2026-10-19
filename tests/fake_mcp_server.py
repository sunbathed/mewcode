"""测试用假 MCP Server（stdio 传输）。

通过 stdin/stdout 管道与 MCP Client 通信，实现最小可用的
initialize / tools/list / tools/call / notifications/initialized。
用来离线验证 MCP Client 的握手、工具发现与工具调用流程。
"""

import json
import sys

# MCP stdio 规范要求消息为 UTF-8 编码。Windows 上子进程 stdout 默认用
# locale 编码（GBK），必须显式切换到 UTF-8，否则中文字符会被 Client 解析失败。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

PROTOCOL_VERSION = "2025-11-25"

TOOLS = [
    {
        "name": "echo",
        "description": "回显输入文本",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "add",
        "description": "两个整数相加",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
]


def handle_request(request_id: int, method: str, params: dict) -> dict:
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp-server", "version": "1.0.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            text = str(args.get("text", ""))
            result = {"content": [{"type": "text", "text": f"echo: {text}"}]}
        elif name == "add":
            total = int(args.get("a", 0)) + int(args.get("b", 0))
            result = {"content": [{"type": "text", "text": str(total)}]}
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"未知工具: {name}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method == "resources/read":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"contents": [{"uri": params.get("uri", ""), "mimeType": "text/plain", "text": "fake resource"}]},
        }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"未知方法: {method}"},
    }


def main() -> None:
    initialized = False
    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method == "notifications/initialized":
            initialized = True
            continue
        if request_id is None:
            continue
        response = handle_request(request_id, method, params)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()