# MewCode

> 用 Python 编写的终端 AI 编程助手——流式对话、工具调用、跨会话记忆，以及从 SubAgent 到多 Agent 团队的协作能力。

<!-- TODO: 补一张真实运行截图（建议 120 列终端、跑一次工具调用），替换掉下面的占位 -->
![运行界面](docs/screenshot.png)

## 特性

- **终端工作台**：Textual 全屏 TUI——welcome 面板、流式输出、工具调用卡片（`⎿ ⎿`）、执行 spinner 与成功/失败标记、底部输入区与状态栏
- **流式多轮对话**：OpenAI 兼容 Chat Completions 接口，Agent Loop 支持最多 10 轮「调用工具 → 分析结果」连招
- **内置工具系统**：读 / 写 / 编辑文件、执行命令（Bash）、Glob / Grep 搜索代码，外加 ToolSearch 延迟加载
- **上下文管理**：token 估算、接近上限自动压缩、超大工具结果落盘（熔断）、`/compact` 手动压缩
- **跨会话记忆**：`MEWCODE.md` 项目指令（三级优先级 + @include）+ 自动记忆提取 + 显式「记住 X」，会话 JSONL 持久化
- **命令框架**：所有 `/` 命令本地直执行（绕过 Agent Loop），Tab 补全，Skill 可自动注册为命令
- **Skill 技能包**：两阶段加载与渐进式披露，可带专属工具子进程执行
- **Hook 生命周期钩子**：15 个事件，`pre_tool_use` 可拒绝并改写工具调用
- **权限系统**：三层规则（用户 / 项目 / 本地）+ 五层防御 + 人工确认，deny 不可被 allow 覆盖
- **MCP 工具生态**：stdio / Streamable HTTP 两种 transport，工具延迟加载，与内置工具无差别
- **SubAgent 子任务分发**：把 Agent 包装成工具，定义式 / Fork 式两种路径，后台任务与调用链追踪
- **Git Worktree 文件系统隔离**：子任务在物理隔离的工作副本中运行，变更检测、自动清理、崩溃后会话恢复
- **Agent Teams 多 Agent 协作**：队员之间异步邮箱直连、共享任务板、Coordinator 调度模式、跨进程（tmux / iTerm2 / in-process）

## 快速上手

```bash
# 1. 安装（从项目源码目录）
python -m pip install -e ".[test]"

# 2. 配置模型
# 默认复用 ZCode 的 provider_config.json；也可以复制 .env.example 为 .env，
# 用 MEWCODE_API_KEY / MEWCODE_BASE_URL / MEWCODE_MODEL 覆盖 provider 配置。
# API 密钥只从本地配置读取，永远不会写进会话文件。

# 3. 启动
mewcode
# 或
python -m mewcode
```

启动后进入全屏终端工作台：顶部欢迎面板展示品牌、版本、模型、当前目录与最近会话；下方是对话工作区，模型回答流式输出，工具调用以卡片形式展示，执行中有 spinner 动画，完成后显示 `✓`（成功）/ `✗`（失败）。已有历史的会话启动后直接进入对话视图。

## 使用示例

```text
> 看看这个仓库的测试怎么跑，顺便告诉我总共多少个用例

  ⎿ Grep("pytest*")        ✓   3 处命中
  ⎿ Bash("python -m pytest --collect-only -q")   ✓  547 tests collected

MewCode 的测试用 pytest 组织（tests/ 目录，conftest.py 提供共享 fixture），
在仓库根目录执行 `pytest` 即可，当前共 547 个用例全部通过。
```

## 终端命令

所有以 `/` 开头的输入都会被命令框架拦截，绕过 Agent Loop 直接在本地执行——省 Token、响应快、行为确定。输入 `/` 后按 Tab 自动补全。命令按执行模式分三类：LOCAL（纯本地）、LOCAL_UI（本地 + 改变 UI 状态）、PROMPT（把预设 prompt 发给 Agent）。

- `/help`（`/h`、`/?`）：显示帮助；`/help <命令名>` 查看详细用法
- `/status`（`/s`）：显示综合状态（模式 / Token / 工具 / 记忆 / 目录 / 版本）
- `/compact`（`/c`）：手动压缩上下文（显示压缩前后 Token 变化）
- `/clear`：清除对话，创建新会话（LOCAL_UI）
- `/plan`（`/p`）：进入规划模式；带参数则同时把任务描述发给 Agent
- `/do`：退出规划模式，恢复正常工具
- `/new`：新建会话，回到欢迎面板
- `/session`：会话管理（list / resume <id> / new / delete）
- `/sessions`：列出历史会话
- `/use <id>`：切换会话
- `/resume`：展示会话列表，输入编号恢复历史对话
- `/memory`：记忆管理（show / list / add <内容> / clear / path）
- `/init`：在当前目录初始化创建 `MEWCODE.md` 指令文件
- `/permission`（`/perm`、`/permissions`）：权限管理（mode / rules / add / reset）
- `/skill`：Skill 管理（list / info <名称> / reload）
- `/review`：审查代码变更（Skill，fork 模式隔离上下文客观审查）
- `/tasks`：列出后台子 Agent 任务与调用链
- `/task`：后台任务详情（info / cancel）
- `/worktree`：Git Worktree 管理（list / create / enter / exit / remove，删除需显式 --discard）
- `/teams`：列出 Agent 团队与队员状态
- `/mcp`：查看已配置与已连接的 MCP Server、工具列表与失败原因
- `/exit`：保存并退出

Skill 加载后会自动注册为 Slash Command（如 `/commit`、`/test`，描述带 `[skill]` 标记），与内置命令无差别地出现在 `/help` 与 Tab 补全里。会话以 JSONL 格式保存到 `~/.mewcode/sessions/`（每个会话一对 `.jsonl` + `.meta` 文件），可用 `MEWCODE_SESSION_DIR` 指定其他目录。输入「记住 X」可以显式让 Agent 把信息记入长期记忆。命令框架的注册中心在 `mewcode/commands/`，未知命令统一引导到 `/help`。

## 工具系统

模型在对话中可调用以下七个内置工具：

| 工具 | 说明 |
| --- | --- |
| `ReadFile` | 读取文件内容（超过 30000 字符截断） |
| `WriteFile` | 写入或覆盖文件，自动创建父目录 |
| `EditFile` | 对同一文件做多处「查找→替换」编辑，任一段失败则整体不生效 |
| `Bash` | 在项目工作目录执行 shell 命令，默认 10 秒超时 |
| `Glob` | 按通配模式列出文件（默认忽略隐藏目录，最多 100 条） |
| `Grep` | 按正则搜索文件内容（默认忽略 `.git` 等目录，最多 200 条） |
| `ToolSearch` | 搜索并加载延迟工具的完整定义（`select:工具名` 精确拉取，或直接输入关键词） |

通过 MCP 接入的外部工具以 `mcp_<server>_<tool>` 命名注册，对模型与内置工具无差别；MCP 工具默认延迟加载，先只列名字，经 `ToolSearch` 发现后进入完整工具列表。团队协作工具（`TeamCreate` / `SendMessage` / `TaskCreate` / `TaskList` / 等）随 SubAgent 与 Teams 能力注入对应角色的工具池。工具调用过程（工具名、参数、状态、结果摘要）会实时显示在对话区。

## 项目结构

```text
src/mewcode/
├── app.py            # 应用装配与生命周期
├── tui.py            # Textual 全屏终端界面
├── terminal.py       # 精简终端入口
├── session.py        # 会话（JSONL + meta）管理
├── config.py         # 配置加载（复用 ZCode provider / .env）
├── llm_client.py     # LLM 客户端（OpenAI 兼容）
├── engine/           # Agent Loop 核心：事件流、Prompt 构建、规划模式
├── tools/            # 内置工具与注册中心
├── permission/       # 权限系统：模式、规则、危险命令、人工确认
├── context/          # 上下文管理：token 估算、自动压缩、预算熔断
├── memory/           # 跨会话记忆：指令、自动提取、会话存储
├── commands/         # 斜杠命令框架：解析、注册、补全
├── skills/           # Skill 技能包：加载、执行器、工具子进程
├── hooks/            # Hook 钩子系统：事件、条件解析、执行器
├── mcp/              # MCP 客户端：JSON-RPC、stdio/HTTP、工具包装
├── subagent/         # SubAgent：Agent 工具、Fork、后台任务、追踪
├── worktree/         # Git Worktree 隔离：变更检测、清理、恢复
└── teams/            # Agent Teams：邮箱、任务板、Coordinator
```

测试在根目录 `tests/`（与章节一一对应），构建与依赖见 `pyproject.toml`。

## 配置

默认复用 ZCode 的 `provider_config.json`（可用 `MEWCODE_ZCODE_CONFIG` 指定其他路径）。也可复制 `.env.example` 为 `.env`，常用变量：

| 变量 | 作用 |
| --- | --- |
| `MEWCODE_API_KEY` / `MEWCODE_BASE_URL` / `MEWCODE_MODEL` | 覆盖 provider 配置 |
| `MEWCODE_CONTEXT_WINDOW` | 上下文窗口 token 数，接近上限自动压缩（`MEWCODE_AUTO_COMPACT=0` 可关闭） |
| `MEWCODE_MEMORY_EVERY_N_MESSAGES` | 自动记忆提取频率（每 N 条新消息） |
| `MEWCODE_SESSION_DIR` | 会话文件目录（默认 `~/.mewcode/sessions/`） |

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

MCP 工具统一以 `mcp_<server>_<tool>` 命名注册进工具中心，受权限系统管控，且默认延迟加载（经 `ToolSearch` 发现后进入模型工具列表）。

## 权限规则

权限规则采用 `ToolName(pattern)` 语法 + glob 匹配，三层优先级（越靠近项目优先级越高）：

```text
本地级（最高，个人覆盖，不入版本控制）—— {project}/.mewcode/permissions.local.yaml
项目级（团队共享）                    —— {project}/.mewcode/permissions.yaml
用户级（全局偏好）                    —— ~/.mewcode/permissions.yaml
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

## 测试

```bash
pytest
```

全量 500+ 个用例（单元 + 集成）通过，覆盖事件流、Prompt 构建、上下文管理、记忆层、命令框架、Skill 系统、Hook 系统、权限五层防御、MCP 双 transport、SubAgent、Git Worktree 与 Agent Teams 各模块，配套端到端验收场景。

## 实现笔记

本项目按「从单 Agent 到多 Agent 团队」的章节路线迭代构建：

四~六章 Agent Loop / 权限 → 七 MCP 工具生态 → 八 上下文管理 → 九 跨会话记忆 → 十 命令框架 → 十一 Skill 技能包 → 十二 Hook 钩子 → 十三 SubAgent 子任务分发 → 十四 Git Worktree 隔离 → 十五 Agent Teams 协作。

各部分的核心实现细节（机制、演进取舍、关键文件定位）整理在 [docs/implementation-notes.md](docs/implementation-notes.md)。

## License

本项目目前未附带 LICENSE 文件，开源协议待定。