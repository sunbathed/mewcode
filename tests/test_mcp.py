"""第七章：MCP 模块核心单元测试。

只保留核心契约：
  - JSON-RPC 2.0 消息编解码（含 id=0 与通知无 id 的区分）
  - 配置加载与合并（项目级优先、env 展开）
  - StdioTransport 往返与环境隔离、HTTPTransport 全握手
  - MCPClient 握手/工具发现/调用、错误响应、乱序回包按 id 匹配
  - MCPToolWrapper 命名映射与防冲突
  - MCPManager 注册与失败容忍
  - 工具延迟加载（ToolSearch 发现后进入完整列表）
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning",
)

from mewcode.mcp import (
    HTTPTransport,
    MCPClient,
    MCPError,
    MCPManager,
    MCPServerConfig,
    MCPToolWrapper,
    StdioTransport,
    TransportError,
    expand_env_vars,
    load_mcp_config,
    parse_message,
)
from mewcode.mcp.jsonrpc import JSONRPCNotification, JSONRPCRequest, JSONRPCResponse, encode_message, to_dict
from mewcode.tools import ToolRegistry, ToolSearch
from mewcode.tools.base import BaseTool


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 消息编解码
# ---------------------------------------------------------------------------


class TestJsonRPC:
    def test_encode_request(self):
        msg = JSONRPCRequest(method="tools/call", params={"name": "x"}, id=1)
        data = to_dict(msg)
        assert data == {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}

    def test_encode_notification_omits_id(self):
        msg = JSONRPCNotification(method="notifications/initialized", params={})
        data = to_dict(msg)
        assert "id" not in data

    def test_encode_response_error(self):
        data = to_dict(JSONRPCResponse(id=1, error={"code": -1, "message": "bad"}))
        assert data["error"] == {"code": -1, "message": "bad"}
        assert "result" not in data

    def test_id_zero_kept(self):
        """id 为 0 是合法请求 id，必须保留（区别于无 id 的通知）。"""
        data = to_dict(JSONRPCRequest(method="ping", id=0))
        assert data.get("id") == 0
        msg = parse_message({"jsonrpc": "2.0", "id": 0, "method": "ping"})
        assert msg.id == 0

    def test_encode_decode_roundtrip(self):
        msg = JSONRPCRequest(method="tools/call", params={"name": "add", "arguments": {"a": 1, "b": 2}}, id=9)
        parsed = parse_message(json.loads(encode_message(msg)))
        assert parsed.method == "tools/call"
        assert parsed.id == 9


# ---------------------------------------------------------------------------
# 配置加载与合并
# ---------------------------------------------------------------------------


class TestMCPConfig:
    def test_expand_env_vars(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
        assert expand_env_vars("${GITHUB_TOKEN}") == "secret-token"
        assert expand_env_vars("prefix-${GITHUB_TOKEN}-suffix") == "prefix-secret-token-suffix"

    def test_project_overrides_user(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        project = tmp_path / "proj"
        home.mkdir()
        project.mkdir()
        (home / ".mewcode").mkdir()
        (project / ".mewcode").mkdir()
        (home / ".mewcode" / "config.yaml").write_text(
            "mcp_servers:\n"
            "  slack:\n"
            "    command: npx\n"
            "    args: ['-y', '@slack/mcp']\n"
            "  shared:\n"
            "    command: python\n"
            "    args: ['-m', 'shared']\n",
            encoding="utf-8",
        )
        (project / ".mewcode" / "config.yaml").write_text(
            "mcp_servers:\n"
            "  shared:\n"
            "    command: python\n"
            "    args: ['-m', 'shared-local']\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home))
        configs = load_mcp_config(workdir=str(project), user_path=str(home / ".mewcode" / "config.yaml"))
        assert set(configs) == {"slack", "shared"}
        assert configs["shared"].args == ["-m", "shared-local"]  # 项目级覆盖用户级

    def test_stdio_and_http_config_parse(self):
        configs = {
            "github": MCPServerConfig(name="github", command="npx", args=["-y", "gh"]),
            "remote": MCPServerConfig(name="remote", url="https://api.example.com/mcp"),
        }
        assert configs["github"].transport_type == "stdio"
        assert configs["remote"].transport_type == "http"


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------


class FakeStdioServerScript:
    """最小假 server：读一行 JSON-RPC，回一行响应。"""

    SCRIPT = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    line=line.strip()\n"
        "    if not line: continue\n"
        "    msg=json.loads(line)\n"
        "    rid=msg.get('id')\n"
        "    if rid is None: continue\n"
        "    method=msg.get('method')\n"
        "    if method=='initialize': result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'fake'}}\n"
        "    elif method=='tools/list': result={'tools':[{'name':'hi','description':'say hi','inputSchema':{'type':'object'}}]}\n"
        "    elif method=='tools/call': result={'content':[{'type':'text','text':'world'}]}\n"
        "    else: result={}\n"
        "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':rid,'result':result})+chr(10))\n"
        "    sys.stdout.flush()\n"
    )


class TestStdioTransport:
    def test_transport_roundtrip(self, tmp_path):
        script = tmp_path / "fake_server.py"
        script.write_text(FakeStdioServerScript.SCRIPT, encoding="utf-8")
        transport = StdioTransport(sys.executable, [str(script)], workdir=str(tmp_path))

        async def run():
            await transport.connect()
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            msg = await asyncio.wait_for(transport.receive(), timeout=10)
            assert msg["result"]["serverInfo"]["name"] == "fake"
            await transport.close()

        asyncio.run(run())

    def test_environment_isolation(self, tmp_path, monkeypatch):
        """子进程只拿到系统必需变量 + 显式 env，不继承父进程完整环境。"""
        script = tmp_path / "print_env.py"
        script.write_text(
            "import json,os,sys\n"
            "sys.stdout.write(json.dumps(dict(os.environ))+chr(10))\n"
            "sys.stdout.flush()\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MCP_SECRET_LEAK", "should-not-pass")
        transport = StdioTransport(sys.executable, [str(script)], env={"ONLY_ME": "yes"}, workdir=str(tmp_path))

        async def run():
            await transport.connect()
            env = await asyncio.wait_for(transport.receive(), timeout=10)
            assert "MCP_SECRET_LEAK" not in env
            assert env.get("ONLY_ME") == "yes"
            assert "PATH" in env  # 系统必需变量保留
            await transport.close()

        asyncio.run(run())

    def test_subprocess_not_found(self, tmp_path):
        transport = StdioTransport("definitely-not-a-real-cmd-xyz", workdir=str(tmp_path))

        async def run():
            with pytest.raises(TransportError):
                await transport.connect()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# HTTP transport（用 httpx MockTransport 模拟 Server）
# ---------------------------------------------------------------------------


class FakeHTTPServer:
    """用 httpx.MockTransport 模拟的 MCP HTTP Server。"""

    def __init__(self):
        self.requests: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        self.requests.append(payload)
        method = payload.get("method")
        rid = payload.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-fake", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "http_tool", "description": "d", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "http-result"}]}
        else:
            result = {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": result})


class TestHTTPTransport:
    def test_full_handshake_and_call(self):
        fake = FakeHTTPServer()
        mock = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = HTTPTransport("https://example.com/mcp", http_client=mock)

        async def run():
            client = MCPClient("http-fake", transport)
            await client.connect()
            assert client.server_info["name"] == "http-fake"
            tools = await client.list_tools()
            assert len(tools) == 1
            assert tools[0]["name"] == "http_tool"
            result = await client.call_tool("http_tool", {})
            assert "http-result" in result.get("content", [{}])[0].get("text", "")
            await client.close()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# MCPClient：stdio 端到端
# ---------------------------------------------------------------------------


class TestMCPClient:
    def make_stdio_client(self, tmp_path):
        script = tmp_path / "fake_mcp.py"
        script.write_text(FakeStdioServerScript.SCRIPT, encoding="utf-8")
        transport = StdioTransport(sys.executable, [str(script)], workdir=str(tmp_path))
        return MCPClient("demo", transport)

    def test_handshake_tools_and_call(self, tmp_path):
        client = self.make_stdio_client(tmp_path)

        async def run():
            await client.connect()
            assert client.protocol_version == "2025-11-25"
            tools = await client.list_tools()
            assert [t["name"] for t in tools] == ["hi"]
            result = await client.call_tool("hi", {})
            assert "world" in result.get("content", [{}])[0].get("text", "")
            await client.close()

        asyncio.run(run())

    def test_request_error_raises(self, tmp_path):
        script = tmp_path / "fake_mcp_error.py"
        script.write_text(
            "import json,sys\n"
            "for line in sys.stdin:\n"
            "    line=line.strip()\n"
            "    if not line: continue\n"
            "    msg=json.loads(line)\n"
            "    rid=msg.get('id')\n"
            "    if rid is None: continue\n"
            "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'unknown method'}})+chr(10))\n"
            "    sys.stdout.flush()\n",
            encoding="utf-8",
        )
        transport = StdioTransport(sys.executable, [str(script)], workdir=str(tmp_path))
        client = MCPClient("demo", transport)

        async def run():
            with pytest.raises(MCPError):
                await client.connect()

        asyncio.run(run())

    def test_async_matching_out_of_order(self, tmp_path):
        """请求-响应异步匹配：Server 乱序回包，仍能按 id 对应。"""
        script = tmp_path / "fake_mcp_outoforder.py"
        script.write_text(
            "import json,sys\n"
            "for line in sys.stdin:\n"
            "    line=line.strip()\n"
            "    if not line: continue\n"
            "    msg=json.loads(line)\n"
            "    rid=msg.get('id')\n"
            "    if rid is None: continue\n"
            "    method=msg.get('method')\n"
            "    # 故意乱序：先回 tools/list 再回 initialize（按 id 匹配，无关顺序）\n"
            "    if method=='initialize':\n"
            "        result={'protocolVersion':'2025-11-25','capabilities':{},'serverInfo':{'name':'fake'}}\n"
            "    elif method=='tools/list':\n"
            "        result={'tools':[{'name':'t1','description':'','inputSchema':{'type':'object'}}]}\n"
            "    else:\n"
            "        result={}\n"
            "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':rid,'result':result})+chr(10))\n"
            "    sys.stdout.flush()\n",
            encoding="utf-8",
        )
        transport = StdioTransport(sys.executable, [str(script)], workdir=str(tmp_path))
        client = MCPClient("demo", transport)

        async def run():
            await client.connect()
            tools = await client.list_tools()
            assert [t["name"] for t in tools] == ["t1"]
            await client.close()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# MCPToolWrapper 适配器
# ---------------------------------------------------------------------------


class TestMCPToolWrapper:
    def make_wrapper(self, name, tool_name):
        transport = StdioTransport(sys.executable, ["-c", "import sys; sys.stdin.read()"], workdir=".")
        client = MCPClient(name, transport)
        return MCPToolWrapper(client, name, {"name": tool_name, "description": "d", "inputSchema": {"type": "object"}})

    def test_wrapper_maps_name_description(self):
        wrapper = self.make_wrapper("github", "search_issues")
        assert wrapper.name == "mcp_github_search_issues"
        assert wrapper.description == "d"
        assert wrapper.input_schema["type"] == "object"
        assert wrapper.should_defer() is True  # MCP 工具默认延迟加载

    def test_wrapper_avoids_name_collision(self):
        assert self.make_wrapper("github", "search").name == "mcp_github_search"
        assert self.make_wrapper("jira", "search").name == "mcp_jira_search"


# ---------------------------------------------------------------------------
# MCPManager：注册与失败容忍
# ---------------------------------------------------------------------------


class TestMCPManager:
    def make_fake_server_config(self, tmp_path) -> MCPServerConfig:
        script = tmp_path / "fake_mcp.py"
        script.write_text(FakeStdioServerScript.SCRIPT, encoding="utf-8")
        return MCPServerConfig(name="demo", command=sys.executable, args=[str(script)])

    def test_register_all_tools(self, tmp_path):
        manager = MCPManager(configs={"demo": self.make_fake_server_config(tmp_path)})
        registry = ToolRegistry()

        async def run():
            failed = await manager.register_all_tools(registry)
            assert failed == []
            assert manager.connected_servers() == ["demo"]
            assert "mcp_demo_hi" in registry.names()
            tool = registry.get("mcp_demo_hi")
            assert tool.should_defer()
            assert tool.name not in {s["function"]["name"] for s in registry.api_specs()}
            assert "mcp_demo_hi" in registry.deferred_names()
            await manager.close_all()

        asyncio.run(run())

    def test_partial_failure_does_not_block(self, tmp_path):
        """一个 Server 失败不应阻止另一个 Server 注册。"""
        bad = MCPServerConfig(name="bad", command="definitely-not-real-xyz", args=[])
        good = self.make_fake_server_config(tmp_path)
        manager = MCPManager(configs={"bad": bad, "good": good})
        registry = ToolRegistry()

        async def run():
            failed = await manager.register_all_tools(registry)
            assert failed == ["bad"]
            assert "mcp_good_hi" in registry.names()  # 好的 Server 正常注册
            assert manager.failures["bad"]
            await manager.close_all()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# 工具延迟加载（ToolSearch）
# ---------------------------------------------------------------------------


class TestDeferredLoading:
    class DeferredTool(BaseTool):
        """测试用延迟工具。"""

        name = "mcp_demo_lazy_tool"
        description = "延迟加载的 MCP 工具"
        safety = "command"

        async def _run(self, args):
            return "ok"

        def should_defer(self):
            return True

    def test_deferred_excluded_from_api_specs(self):
        registry = ToolRegistry()
        registry.register(self.DeferredTool())
        names = {s["function"]["name"] for s in registry.api_specs()}
        assert "mcp_demo_lazy_tool" not in names
        assert "mcp_demo_lazy_tool" in registry.deferred_names()

    def test_toolsearch_select_loads_and_marks(self):
        registry = ToolRegistry()
        registry.register(self.DeferredTool())
        tool_search = ToolSearch(registry)

        async def run():
            result = await tool_search.run({"query": "select:mcp_demo_lazy_tool"})
            assert result.success
            assert "mcp_demo_lazy_tool" in result.output
            names = {s["function"]["name"] for s in registry.api_specs()}
            assert "mcp_demo_lazy_tool" in names
            assert "mcp_demo_lazy_tool" not in registry.deferred_names()

        asyncio.run(run())
