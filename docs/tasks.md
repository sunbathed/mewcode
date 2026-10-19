# MewCode 第七阶段实现任务

> 任务按依赖顺序排列；本阶段在 `mewcode/mcp/` 下新增协议/传输/客户端/配置/适配器/管理器，
> 扩展 `tools/` 支持工具延迟加载，改造 `engine/agent.py`、`app.py`、`tui.py`、`commands.py`，
> 并补充对应测试。

## 1. JSON-RPC 2.0 协议层

- **影响文件：** `src/mewcode/mcp/jsonrpc.py`
- **依赖任务：** 无
- **参考资料定位：** `docs/spec.md:3.1`；理论篇「JSON-RPC 2.0：消息格式」
- **交付内容：**
  - `JSONRPCRequest`（id/method/params）、`JSONRPCNotification`（无 id）、`JSONRPCResponse`（result/error）。
  - `to_dict` / `parse_message` / `encode_message` / `parse_line`：字典、JSON 字符串、stdio 行格式互转。
  - id=0 与无 id 的区分（通知无 id 字段，序列化时省略）。
  - `MCPError` 异常（code/message/data）。

## 2. Transport 抽象 + 两种标准实现

- **影响文件：** `src/mewcode/mcp/transport.py`
- **依赖任务：** 1
- **参考资料定位：** `docs/spec.md:3.2`；理论篇「传输层：stdio 和 Streamable HTTP」「stdio 传输」
- **交付内容：**
  - `Transport` ABC：connect / send / receive / close / is_connected。
  - `StdioTransport`：`asyncio.create_subprocess_exec` 启动子进程，stdin 写入、stdout 读取、stderr 隔离。
  - 子进程环境隔离：只保留系统必需变量 + 显式 env，支持 `${VAR}` 展开。
  - `HTTPTransport`：POST JSON-RPC 到端点，Accept `application/json, text/event-stream`，支持认证 headers 与 MCP-Protocol-Version 头。
  - stdio 行解码 UTF-8，失败回退 locale 编码（Windows 兼容）。

## 3. MCP Client

- **影响文件：** `src/mewcode/mcp/client.py`
- **依赖任务：** 2
- **参考资料定位：** `docs/spec.md:3.3`；理论篇「一次完整的 MCP 会话长什么样」「请求-响应的异步匹配」
- **交付内容：**
  - `connect()`：initialize 握手 + notifications/initialized。
  - `list_tools()` / `call_tool()` / `read_resource()`。
  - pending dict + Future + reader loop 实现请求-响应异步 ID 匹配。
  - 超时、连接中断、Server error 转为 MCPClientError / MCPError。
  - `extract_text_content`：从 content 数组提取 text 块。

## 4. MCP 配置加载与合并

- **影响文件：** `src/mewcode/mcp/config.py`
- **依赖任务：** 无
- **参考资料定位：** `docs/spec.md:3.5`；理论篇「配置：让用户告诉 MewCode 该连哪些 Server」
- **交付内容：**
  - `MCPServerConfig`：command/args/env（stdio）与 url/headers（HTTP）、timeout、disabled。
  - 项目级 `{project}/.mewcode/config.yaml` 优先于用户级 `~/.mewcode/config.yaml`。
  - 同时支持 `mcp_servers` map 形式与带 `name` 字段的 list 形式。
  - `expand_env_vars`：`${VAR}` 从当前进程环境展开。

## 5. MCPToolWrapper 适配器

- **影响文件：** `src/mewcode/mcp/wrapper.py`
- **依赖任务：** 3
- **参考资料定位：** `docs/spec.md:3.4`；理论篇「工具包装器：让 MCP 工具融入 MewCode」
- **交付内容：**
  - 工具名 `mcp_{server_name}_{tool_name}`，避免跨 Server 同名冲突。
  - description / inputSchema 透传；safety 默认 `command`（受权限系统管控）。
  - 执行转发 `tools/call`；`isError` 转 ToolError。
  - `should_defer()` 返回 True（MCP 工具延迟加载）。

## 6. MCPManager

- **影响文件：** `src/mewcode/mcp/manager.py`
- **依赖任务：** 4、5
- **参考资料定位：** `docs/spec.md:3.5`；理论篇「完整流程：从配置到调用」
- **交付内容：**
  - `register_all_tools(registry)`：连接所有 Server → tools/list → 包装注册，返回失败列表。
  - 连接缓存：`connect()` / `get_client()` / `connected_servers()`。
  - 生命周期：`close_all()`。
  - 单个 Server 失败只告警，不阻止整体启动。

## 7. 工具延迟加载（ToolSearch）

- **影响文件：** `src/mewcode/tools/base.py`、`src/mewcode/tools/registry.py`、`src/mewcode/tools/tool_search.py`、`src/mewcode/tools/__init__.py`
- **依赖任务：** 5
- **参考资料定位：** `docs/spec.md:3.6`；理论篇「工具延迟加载：80 个工具塞不进上下文」
- **交付内容：**
  - Tool 基类 `should_defer()`（默认 False）。
  - ToolRegistry：`deferred_names` / `mark_discovered` / `is_discovered` / `search_deferred`；`api_specs()` 过滤未发现延迟工具。
  - `ToolSearch` 工具：`select:name1,name2` 精确拉取或关键词搜索；命中标记为已发现。
  - Agent 每轮在 system-reminder 列出未发现的延迟工具名。
  - 默认注册 ToolSearch（始终可见，不延迟）。

## 8. Agent / App / TUI 集成

- **影响文件：** `src/mewcode/engine/agent.py`、`src/mewcode/app.py`、`src/mewcode/tui.py`、`src/mewcode/commands.py`
- **依赖任务：** 6、7
- **参考资料定位：** `docs/spec.md:3.7`；实战篇验证流程
- **交付内容：**
  - App/TUI 构造持有 MCPManager；启动时 `setup_mcp` / `_setup_mcp` 连接并注册工具。
  - `_messages_for_request`：延迟工具名进入 system-reminder。
  - `/mcp` 命令：查看已配置/已连接 Server、工具与失败原因；help 更新。
  - TUI 每轮请求前刷新 client.tools，ToolSearch 发现的工具下一轮可见。
  - MCP 工具继续走第六章权限链。

## 9. 测试

- **影响文件：**
  - `tests/fake_mcp_server.py`（stdio 假 Server）
  - `tests/test_mcp.py`（协议/transport/client/config/wrapper/manager/延迟加载）
  - `tests/test_mcp_integration.py`（App/Agent 集成）
- **依赖任务：** 1-8
- **参考资料定位：** `docs/spec.md:5`
- **交付内容：**
  - JSON-RPC 编解码、id=0 与无 id 区分。
  - stdio 管道往返、环境隔离、子进程失败、HTTP 握手与 headers。
  - Client 握手/工具发现/调用/乱序响应匹配/错误。
  - 配置合并（项目级优先）、env 展开。
  - Manager 注册与部分失败容忍。
  - 延迟加载：未发现不出现、ToolSearch 发现后可见、关键词/select 搜索。
  - App 集成：启动连接、`/mcp` 命令、失败不阻塞。
  - 原有全部测试保持通过。

## 10. 文档与验收

- **影响文件：** `README.md`、`docs/spec.md`、`docs/tasks.md`、`docs/checklist.md`、`pyproject.toml`
- **依赖任务：** 9
- **参考资料定位：** `docs/spec.md:6`；MEWCODE.md 三份文档工作流
- **交付内容：**
  - 第七章三份文档写入 docs（第六章归档 docs/v6/）。
  - README「当前范围」更新为 MCP 开放工具生态。
  - pyproject.toml 增加 PyYAML 依赖与测试 warning 过滤。
  - 执行 `pytest` 全部通过。