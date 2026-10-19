# MewCode 第七阶段验收清单

> 每一项必须可勾选、可观测。验收方式：`pytest` 全量 + 针对性命令，或阅读对应源码/测试断言。

## 1. JSON-RPC 2.0 协议层（`mcp/jsonrpc.py`）

- [x] `to_dict(JSONRPCRequest(method=..., id=1))` 输出包含 jsonrpc/id/method/params。
- [x] 通知（无 id）序列化时不输出 id 字段。
- [x] `to_dict(JSONRPCResponse(id=1, result=...))` 输出 result；error 响应不含 result 字段。
- [x] `parse_message` 有 method 且有 id → 请求；有 method 无 id → 通知；无 method → 响应。
- [x] id=0 是合法请求 id，序列化与解析均保留（区别于无 id 的通知）。
- [x] `parse_line` 从 stdio 行 JSON 解析消息；`encode_message` 序列化可 roundtrip。
- [x] `MCPError(code, message, data)` 异常可用。

## 2. Transport（`mcp/transport.py`）

- [x] `StdioTransport` 通过 stdin 发消息（JSON 行）、stdout 收消息。
- [x] 子进程环境隔离：显式设置 `MCP_SECRET_LEAK` 后子进程 env 中不存在；显式声明的 env 存在；`PATH` 保留。
- [x] 不存在的子进程命令触发 `TransportError`（不崩溃）。
- [x] `HTTPTransport` 与 httpx MockTransport 完成 initialize/tools/list/tools/call 全链路。
- [x] HTTP 请求带 `Accept: application/json, text/event-stream`，支持自定义认证 headers 与 MCP-Protocol-Version。
- [x] stdio 消息编解码 UTF-8，中文内容（GBK 环境）能正确解析。

## 3. MCP Client（`mcp/client.py`）

- [x] `connect()` 完成 initialize 握手并解析 serverInfo/protocolVersion/capabilities。
- [x] 握手成功后发送 notifications/initialized 通知。
- [x] `list_tools()` 发现并缓存工具定义。
- [x] `call_tool()` 调用外部工具并返回 content 数组。
- [x] 乱序响应按 id 正确匹配（异步匹配测试通过）。
- [x] Server 返回 error → 抛 `MCPError`。
- [x] 连接中断 → pending 请求抛 `MCPClientError`（不悬挂）。
- [x] `read_resource(uri)` 基础接口可用。
- [x] `extract_text_content` 从 content 提取 text 块，`isError` 场景可识别。

## 4. 配置加载与合并（`mcp/config.py`）

- [x] 项目级 `{project}/.mewcode/config.yaml` 同名 Server 覆盖用户级 `~/.mewcode/config.yaml`。
- [x] 支持 `mcp_servers` map 形式（键为 Server 名）。
- [x] 支持带 `name` 字段的 list 形式（实战篇语法）。
- [x] `${GITHUB_TOKEN}` 从当前环境展开；未定义变量保留原样。
- [x] stdio 配置（command/args/env）与 HTTP 配置（url/headers）可解析，`transport_type` 按字段推断。
- [x] `disabled: true` 的 Server 被跳过。
- [x] 配置文件缺失不报错，返回空配置。

## 5. MCPToolWrapper（`mcp/wrapper.py`）

- [x] 包装后工具名 `mcp_{server}_{tool}`（如 `mcp_github_search_issues`）。
- [x] 两个不同 Server 的同名工具不冲突（`mcp_github_search` ≠ `mcp_jira_search`）。
- [x] description / inputSchema 透传 Server 原始值。
- [x] `should_defer()` 返回 True。
- [x] 执行时调用远端工具并提取文本；`isError` 转 ToolError。

## 6. MCPManager（`mcp/manager.py`）

- [x] `register_all_tools` 连接假 Server 并注册 `mcp_fake_echo`、`mcp_fake_add` 到 ToolRegistry。
- [x] MCP 工具注册后是延迟工具（`should_defer()` True，不出现在 `api_specs()`，出现在 `deferred_names()`）。
- [x] 一个 Server 命令不存在时，`register_all_tools` 返回失败列表但不阻塞另一个 Server 注册。
- [x] `connected_servers()` / `registered_count()` / `close_all()` 正确。
- [x] 无配置时 `register_all_tools` 无副作用返回空失败列表。

## 7. 工具延迟加载（ToolSearch）

- [x] 延迟工具未发现前不出现在 `api_specs()`，名字出现在 `deferred_names()`。
- [x] `ToolSearch(query="select:mcp_demo_lazy_tool")` 精确拉取并标记为 discovered，之后完整 schema 进入 `api_specs()`。
- [x] 关键词搜索（中文描述）命中延迟工具。
- [x] 无匹配时返回「未找到匹配的延迟工具」。
- [x] ToolSearch 本身始终可见（默认注册，不延迟）。
- [x] Agent 请求中，未发现的延迟工具名出现于 `<system-reminder>`，其完整 schema 不在消息工具列表中。

## 8. App / TUI 集成

- [x] `App.setup_mcp` 启动连接假 Server 并输出「MCP 已连接 1 个 Server，注册 2 个工具」。
- [x] `/mcp` 命令显示已配置/已连接 Server、工具名（echo、add）。
- [x] 部分 Server 失败：输出「MCP 连接失败」告警，同时正常 Server 继续注册，应用不阻塞。
- [x] 未配置 MCP 时 `/mcp` 提示「未配置任何 MCP Server」。
- [x] `/help` 列出 `/mcp`。
- [x] TUI 构造持有 MCPManager，`on_mount` 启动后台连接，每轮请求前刷新 client.tools。

## 9. 回归与文档

- [x] `pytest tests/test_mcp.py`：32 passed。
- [x] `pytest tests/test_mcp_integration.py`：6 passed。
- [x] `pytest` 全量：348 passed（含原有 310 个，无回归）。
- [x] `docs/spec.md`、`docs/tasks.md`、`docs/checklist.md` 为第七章内容（第六章归档 `docs/v6/`）。
- [x] `README.md`「当前范围」更新为第七章 MCP 开放工具生态。
- [x] `pyproject.toml` 增加 PyYAML 依赖与 Windows 子进程 warning 过滤。

## 10. 端到端验收（实战篇验证流程映射）

- [x] 配置 MCP Server（stdio 或 HTTP），启动后 UI 显示「连接正常」与注册工具。
- [x] 与模型对话可触发 MCP 工具调用：MewCode → MCP 协议 → 外部 Server → 返回结果 → 模型回答。
- [x] 对模型而言 MCP 工具与内置工具无差别（走统一 Tool 接口与权限链）。
- [x] 延迟加载生效：MCP 工具先只列名字，ToolSearch 加载后完整 schema 进入下一轮。