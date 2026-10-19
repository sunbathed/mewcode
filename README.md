# MewCode

MewCode 是一个使用 Python 编写的终端 LLM 对话助手。支持 OpenAI 兼容 Chat Completions 接口、流式输出、多轮上下文、JSONL 会话持久化、跨会话记忆（项目指令 + 自动记忆），以及一套基础工具系统：模型可以在对话中读取文件、写入文件、编辑文件、执行命令和搜索代码。

## 安装

```bash
python -m pip install -e ".[test]"
```

## 配置

默认复用 ZCode 的 `provider_config.json`。也可以复制 `.env.example` 为 `.env`，使用 `MEWCODE_API_KEY`、`MEWCODE_BASE_URL` 和 `MEWCODE_MODEL` 覆盖 provider 配置。API 密钥只从本地配置读取，不会保存进会话文件。

## 启动

```bash
python -m mewcode
# 或
mewcode
```

启动后进入全屏终端工作台（黑底 + 橙色强调）：顶部欢迎面板展示品牌、版本、模型、当前目录、使用提示与最近会话；下方是对话工作区，模型回答流式输出，工具调用以 `⎿ ⎿` 卡片展示，执行中有 spinner 动画，完成后显示 `✓`（成功）/ `✗`（失败）；底部为固定输入区（`>` 提示符）与状态栏。已有历史的会话启动后直接进入对话视图。

## 终端命令

- `/init`：在当前目录初始化创建 `MEWCODE.md` 指令文件
- `/new`：新建会话，回到欢迎面板
- `/sessions`：列出历史会话（`/session` 提供 list / delete / path 子命令）
- `/use <id>`：切换会话
- `/resume`：展示会话列表，输入编号恢复历史对话
- `/memory`：查看自动记忆（用户画像 + 记忆索引），支持 show / clear / path 子命令
- `/clear`：清空当前会话上下文
- `/compact`：手动触发上下文压缩（显示压缩前后 Token 变化）
- `/plan`：进入规划模式（只读探索，唯一可写的是 plan 文件）
- `/do`：退出规划模式，恢复正常工具
- `/permissions [模式]`：查看或切换权限模式（default / acceptEdits / plan / bypassPermissions / yolo）
- `/mcp`：查看已配置与已连接的 MCP Server、工具列表与失败原因
- `/help`：查看所有可用命令（输入 `?` 也可快速查看）
- `/exit`：保存并退出

会话以 JSONL 格式保存到 `~/.mewcode/sessions/`（每个会话一对 `.jsonl` + `.meta` 文件），可用 `MEWCODE_SESSION_DIR` 指定其他目录；欢迎面板的 Recent activity 会列出最近历史会话。输入「记住 X」可以显式让 Agent 把信息记入长期记忆。

## 工具系统

模型在对话中可调用以下七个内置工具：

- `ReadFile`：读取文件内容（超过 30000 字符截断）
- `WriteFile`：写入或覆盖文件，自动创建父目录
- `EditFile`：对同一文件做多处「查找→替换」编辑，任一段失败则整体不生效
- `Bash`：在项目工作目录执行 shell 命令，默认 10 秒超时
- `Glob`：按通配模式列出文件（默认忽略隐藏目录，最多 100 条）
- `Grep`：按正则搜索文件内容（默认忽略 `.git` 等目录，最多 200 条）
- `ToolSearch`：搜索并加载延迟工具的完整定义（`select:工具名` 精确拉取，或直接输入关键词）

通过 MCP 接入的外部工具以 `mcp_<server>_<tool>` 命名注册，对模型与内置工具无差别；MCP 工具默认延迟加载，先只列名字，经 `ToolSearch` 发现后进入完整工具列表。

工具调用走「模型发起 → 串行执行 → 结果回传 → 再次分析/最终回答」的 Agent 多轮调用循环（最多支持 10 轮连招，如先搜索项目文件，再读取目标文件内容，最后综合回答）。工具调用过程（工具名、参数、状态、结果摘要）会实时显示在对话区。

## 当前范围

本阶段实现了第九章的核心：**记忆层（跨会话记忆与会话持久化）**，让 Agent 从「每次开新会话都失忆」变成「越用越懂你」：

- **项目指令文件**（`mewcode/memory/instructions.py`）：
  - 三级优先级加载 MEWCODE.md：`<项目根>/MEWCODE.md` > `<项目根>/.mewcode/MEWCODE.md` > `~/.mewcode/MEWCODE.md`，全部拼接、项目级排前面（LLM 对 prompt 前置内容更重视，冲突时项目说了算）。
  - `@include` 模块化引用：逐行展开、递归处理，两层防环（visited 精确去重 + depth ≤ 5 兜底），路径越出项目目录直接拦截（`<!-- @include blocked -->` 标记），文件不存在插入 HTML 注释继续处理。
  - 加载结果作为 `custom_instructions` 注入 system prompt，Agent 第一轮对话前就读完「入职文档」。
- **JSONL 会话持久化**（`mewcode/memory/session.py`、`storage.py`）：
  - 每行一个 JSON 对象，追加写入 O(1)、崩溃只丢最后半行、恢复时坏行跳过继续。
  - 会话双文件：`.jsonl`（全量记录）+ `.meta`（id/标题/消息数/token 量/创建/最后活跃），列表只扫 meta。
  - 标题自动生成：用户第一条消息前 50 字符；会话 ID 为 `YYYYMMDD-HHMMSS-xxxx`（时间戳 + 随机后缀防同秒冲突）。
  - 恢复四步走：逐行解析（坏行跳过）→ 消息链校验（tool_use↔tool_result 配对，截断到最后一个完整位置）→ token 超限触发压缩提示；`/use`、`/resume` 恢复时距上次活跃 > 24 小时自动插入时间跨度提醒。
  - 过期清理：启动时清理 30 天没有活跃的会话；旧 `.json` 会话可无缝读取。
- **自动记忆**（`mewcode/memory/auto_memory.py`）：
  - 四类记忆分两级存储：用户偏好/纠正反馈 → 用户级 `~/.mewcode/memory/`；项目知识/参考资料 → 项目级 `<项目>/记忆目录`。
  - 每条记忆一个 Markdown 文件（YAML frontmatter 记 name/description/type），MEMORY.md 索引注入 messages（上限 200 行 / 25KB，超出截断并附加警告）。
  - 显式记忆：输入「记住 X」直接把内容写入记忆文件并更新索引，用户角色陈述（「我是…」）额外写入 user_role.md 用户画像。
  - 自动提取：每轮 Agent Loop 结束后（达到频率阈值）后台异步调 LLM 回顾对话，按四分类节解析、占位符过滤（.../暂无/N/A）、更新索引；去重交给提取 LLM 判断，不自己实现相似度算法。
  - `/memory` 展示用户画像与记忆索引，`/memory clear` 清空记忆。
- **Agent Loop 集成**：指令与记忆索引经 `BuildOptions(custom_instructions / memory_instructions)` 注入 system prompt，跨会话自动恢复项目知识与用户偏好。

至此的完整能力链：四~六章的 Agent Loop/权限，第七章 MCP 工具生态，第八章上下文管理（会话内记忆），第九章跨会话记忆——Agent 从「每次失忆」变成「越用越懂你」。

## MCP 配置

在项目级 `.mewcode/config.yaml`（或用户级 `~/.mewcode/config.yaml`）声明要连接的 MCP Server：

```yaml
mcp_servers:
  # stdio：有 command 字段 → 启动子进程，走管道
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"

  # Streamable HTTP：有 url 字段 → 发 HTTP 请求
  remote-tool:
    url: "https://api.example.com/mcp"
    headers:
      Authorization: "Bearer ${API_TOKEN}"
```

MCP 工具统一以 `mcp_<server>_<tool>` 命名注册进工具中心，受第六章权限系统管控，且默认延迟加载（经 `ToolSearch` 发现后进入模型工具列表）。

## 测试

```bash
pytest
```

当前共 292 个测试全部通过，覆盖事件流、分批调度、Prompt Builder、七核心模块、Plan Mode 模板与频率控制、API 装配管线、工具描述强化、五大定性评估场景、第六章五层权限防御、第七章 MCP 开放工具生态（JSON-RPC 编解码、stdio/HTTP transport、Client 握手与异步匹配、配置合并、Wrapper、Manager、延迟加载与 App 集成），第八章上下文管理（Token 估算、大结果存盘与决策冻结、摘要压缩、熔断、ForceCompact 与 /compact 命令），以及第九章记忆层（MEWCODE.md 三级优先级与 @include 安全展开、JSONL 记录模型、坏行跳过、消息链校验、meta 双文件、过期清理、时间跨度提示、显式记住与用户画像、LLM 自动提取与占位符过滤、/session /memory /resume 命令）。

## 权限规则文件

权限规则采用 `ToolName(pattern)` 语法 + glob 匹配，三层优先级（越靠近项目优先级越高）：

```bash
# 本地级（最高，个人覆盖，不入版本控制）——{project}/.mewcode/permissions.local.yaml
# 项目级（团队共享）——{project}/.mewcode/permissions.yaml
# 用户级（全局偏好）——~/.mewcode/permissions.yaml
```

```yaml
# 允许所有 git 命令
- rule: Bash(git *)
  effect: allow

# 禁止 force push
- rule: Bash(git push --force*)
  effect: deny

# 禁止读取环境变量文件
- rule: ReadFile(*.env*)
  effect: deny
```

任意层出现 deny 都不可被 allow 覆盖；「始终允许」会自动把规则写入本地文件。

