# MewCode 第七阶段功能规格：MCP 协议与开放工具生态

## 1. 背景

第六章为 MewCode 建立了五层权限防御，但工具系统仍是封闭的：ReadFile、WriteFile、Bash、Grep、Glob 等工具全部编译进 MewCode，新增能力必须修改代码、重新发布。用户想接入 GitHub、数据库、Slack 或云服务时，Agent 核心与第三方 API 产生强耦合。

本阶段引入 MCP（Model Context Protocol），让 MewCode 作为 Host，通过标准化协议动态连接外部 MCP Server。第三方开发者只需实现一次 MCP Server，MewCode 无需修改代码即可接入；工具在 Agent 看来与内置工具完全一致。

MCP 分为两层：Data Layer 定义 JSON-RPC 消息、初始化握手、工具发现与调用；Transport Layer 定义消息传输方式。本阶段完整实现 Tools 主流程，Transport 支持 stdio 与 Streamable HTTP。

## 2. 目标与设计原则

1. **协议标准化**：实现 JSON-RPC 2.0 请求、响应、通知三种消息类型及编解码。
2. **Transport 解耦**：Transport 只负责消息收发，上层 Client 流程不关心底层是子进程管道还是 HTTP。
3. **Host/Client/Server 分层**：MewCode 是 Host，内部每个 MCP Server 对应一个独立 MCP Client，连接状态、请求 ID、能力协商彼此隔离。
4. **启动时发现**：启动阶段连接配置的 Server，执行 initialize、tools/list，保证 Agent 第一轮就能知道外部工具；单个 Server 失败不阻塞整体启动。
5. **适配器统一工具**：MCPToolWrapper 将外部工具转换为 MewCode Tool 接口，Agent 无需区分内置工具与 MCP 工具。
6. **安全边界继承**：MCP 工具同样进入第六章权限系统；默认按 command 级别管控，MCP Server 子进程采用最小环境变量集。
7. **上下文节省**：MCP 工具数量不可控，全部采用延迟加载；内置工具始终可见，MCP 工具先只列名字，模型通过 ToolSearch 拉取完整 schema。

## 3. 能力清单

### 3.1 JSON-RPC 2.0（`mcp/jsonrpc.py`）

- `JSONRPCRequest`：有 id、method、params，期待响应。
- `JSONRPCResponse`：有 id 及 result 或 error。
- `JSONRPCNotification`：有 method、无 id，不需要响应。
- 支持 id=0 与「无 id」的区分。
- 支持 JSON 字符串、字典、stdio 行格式之间的编解码。

### 3.2 Transport 层（`mcp/transport.py`）

- `Transport` 抽象：connect、send、receive、close、is_connected。
- `StdioTransport`：通过 `asyncio.create_subprocess_exec` 启动 Server，stdin 写入、stdout 读取，stderr 不参与协议。
- `HTTPTransport`：POST JSON-RPC 到 Streamable HTTP endpoint，发送 Accept `application/json, text/event-stream`，支持自定义认证 headers 与 MCP-Protocol-Version。
- stdio 消息使用 UTF-8 JSON 行；对 Windows 非规范 Server 提供 locale 编码兜底。
- 子进程环境隔离：仅保留 PATH 等必要系统变量与显式声明的 env，支持 `${VAR}` 展开。

### 3.3 MCP Client（`mcp/client.py`）

- initialize 握手：声明协议版本、roots 能力、客户端名称与版本。
- 握手完成后发送 `notifications/initialized` 通知。
- `tools/list` 发现 Server 工具，缓存工具定义。
- `tools/call` 调用外部工具，提取 content 文本块。
- pending 字典 + Future + reader loop 实现请求-响应异步 ID 匹配。
- 连接断开、超时、Server error 转换为 MCPClientError/MCPError。

### 3.4 MCPToolWrapper（`mcp/wrapper.py`）

- 工具名格式：`mcp_{server_name}_{tool_name}`，避免不同 Server 同名工具冲突。
- description 与 inputSchema 透传；默认 safety=`command`，受第六章权限系统管控。
- `should_defer()` 返回 True，工具注册后默认不把完整 schema 放进模型上下文。
- 执行时转发 `tools/call`，从 content 数组提取 text 块；`isError` 转为 ToolError。

### 3.5 MCP 配置与 Manager（`mcp/config.py`、`mcp/manager.py`）

- 项目级配置：`{project}/.mewcode/config.yaml`。
- 用户级配置：`~/.mewcode/config.yaml`。
- 项目级同名 Server 覆盖用户级配置。
- 同时支持 `mcp_servers` map 形式与带 `name` 字段的 list 形式。
- stdio 配置：command、args、env、timeout；HTTP 配置：url、headers、timeout。
- `MCPManager` 连接缓存、批量注册工具、查看 Server 状态、统一关闭生命周期。
- 部分 Server 连接失败只告警，不阻止其他 Server 与 MewCode 启动。

### 3.6 工具延迟加载（`ToolRegistry`、`tools/tool_search.py`）

- Tool 基类增加 `should_defer()`，内置工具默认 False，MCP 工具 True。
- ToolRegistry 维护 discovered 集合：`deferred_names`、`mark_discovered`、`is_discovered`。
- `api_specs()` 只返回非延迟工具与已发现延迟工具的完整 schema。
- `ToolSearch` 始终可见，支持 `select:name1,name2` 精确查找与关键词搜索。
- 搜索结果标记为已发现，从下一轮开始进入完整工具列表。
- Agent 每轮在 system-reminder 中列出尚未发现的延迟工具名字。

### 3.7 应用集成（`app.py`、`tui.py`、`commands.py`）

- App/TUI 启动时读取配置、连接 MCP Server、注册工具并显示状态。
- `/mcp` 命令查看已配置/已连接 Server、工具数量与失败原因。
- TUI 每轮请求前刷新工具描述，ToolSearch 发现的 MCP 工具下一轮可见。
- MCP 工具继续经过第六章黑名单、沙箱、规则、模式、HITL 权限链。

## 4. Out of Scope

- SSE 流式推送内容消费（HTTP 仅处理 application/json 响应）。
- Resources/Prompts 的完整消费 API（保留 Client 基础接口）。
- Sampling、Elicitation 等 Client 反向能力。
- MCP Server 命令白名单与远程 Server OAuth 自动刷新。
- MCP Server 的自定义 WebSocket transport。

## 5. 非功能要求

1. **测试完整性**：新增 MCP 协议、Transport、Client、配置、Wrapper、Manager、延迟加载与 App 集成测试；原有测试保持通过。
2. **跨平台**：stdio 在 Windows、macOS、Linux 可启动；Windows 子进程环境与编码处理不泄露敏感变量。
3. **故障隔离**：单个 MCP Server 失败不阻塞应用与其他 Server；调用错误通过 ToolResult 返回 Agent。
4. **安全继承**：MCP 工具默认 command 级别，统一接入第六章权限系统。
5. **上下文效率**：未发现 MCP 工具完整 schema 不进入每轮请求。

## 6. 完成定义

1. `mewcode/mcp/` 包完成 JSON-RPC、stdio/HTTP Transport、Client、配置、Wrapper、Manager。
2. 配置 MCP Server 后，启动可完成握手、tools/list 并注册外部工具。
3. Agent/TUI 能通过 ToolSearch 发现并调用 MCP 工具，结果回流对话。
4. 单个 Server 连接失败不阻塞应用；`/mcp` 可查看状态。
5. MCP 子进程只继承最小环境变量集。
6. 执行 `pytest` 全部通过。