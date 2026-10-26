# MewCode 实现笔记（第 13~15 章）

> 这份笔记从 README 中拆出，保存第 13~15 章各阶段的核心实现细节与关键文件定位，供维护者回顾。用户手册请看根目录 `README.md`。

## 第十三章 SubAgent 子任务分发——把 Agent 包装成工具

让引擎层和工具层递归组合，主 Agent 可以把子任务委派给拥有独立上下文、工具集与权限边界的子 Agent：

- **统一 Agent 工具**（`mewcode/subagent/agent_tool.py`）：一个 `Agent` 工具通过 `subagent_type` 参数分流「定义式」（预定义角色、空白对话）与「Fork 式」（继承父对话、无条件后台）两条路径，工具列表始终稳定（不随定义文件增减变化）。参数含 prompt / description（必填）+ subagent_type / model / run_in_background / name / isolation（可选），模型选择按「调用时参数 > 定义文件 > 父 Agent」顺序（haiku / sonnet / opus 别名映射）。
- **Agent 定义与加载**（`subagent/types.py` + `parser.py` + `loader.py`）：`AgentDef` 蓝图（agent_type / when_to_use / system_prompt / tools / disallowed_tools / model / max_turns / permission_mode / background / isolation / source）。定义文件用 YAML frontmatter + Markdown body（body 是子 Agent 启动时的 system prompt，注入一次伴随整个生命周期）。三级加载（项目 `.mewcode/agents/` > 用户 `~/.mewcode/agents/` > 内置 `builtin/`），同名先到先得覆盖，每次 get 热重载、失败回退缓存。
- **Fork 路径**（`subagent/fork.py`）：继承父 Agent 完整对话历史（首次 API 请求命中 prompt cache），首部注入 `<fork_boilerplate>` 硬指令覆盖父 Agent 的交互式默认行为（不能再 Fork / 不要对话提问 / 直接用工具 / 报告 ≤500 字）；历史含 Fork 标记即拒绝嵌套；父会话末尾未完成的 tool_use 用 "interrupted" 占位符补全（否则 LLM API 报错）。
- **RunToCompletion**（`engine/agent.py` 新增）：子 Agent 的非交互式执行循环——任务直接注入不等用户输入，LLM 不再调用工具即视为完成，返回最后文本；Hook（pre/post_tool_use 等）在子 Agent 中仍然生效。
- **工具过滤多层防线**（`subagent/tool_filter.py`）：MCP 工具直通 → 全局禁止列表（Agent / AskUserQuestion / TaskOutput / ExitPlanMode / EnterPlanMode / TaskStop / Workflow，防嵌套与阻塞循环）→ 自定义 Agent 额外限制 → 后台白名单（文件读写/搜索/Bash/ToolSearch 等基础工具）→ 定义级 tools / disallowedTools。逐层递进，子 Agent 权限永远不会超过父 Agent。
- **后台任务管理**（`subagent/task_manager.py`）：`BackgroundTask`（状态 / 结果 / token 用量 / 起止时间 / cancel），launch 生成 UUID 8 位 ID 并持有 asyncio 任务句柄，`_run_background` 处理 completed / cancelled / failed 三终态，完成后 task_id 推入通知队列（push/pull 结合）；`TaskList` / `TaskGet` 工具让主 Agent 自己查后台任务，用自然语言答复用户。
- **父子链路追踪**（`subagent/trace.py`）：`TraceRegistry` 记录调用链（parent_id / agent_type / 工具调用次数 / token / 状态），`tree_text()` 渲染调用链视图（不跨会话持久化）。
- **三个内置 Agent**（`subagent/builtin/`）：`explore`（haiku 模型只读探索，disallowed 写类工具）、`plan`（只读规划、输出分步实现策略与关键文件）、`general-purpose`（全能力通用）。项目级定义可覆盖同名的内置定义。
- **Slash 命令**：`/tasks`（列出后台任务 + 子 Agent 调用链）、`/task info <ID>`、`/task cancel <ID>`；权限模式新增 `dontAsk` 别名（Fork 默认，子 Agent 无 UI 可弹确认框，工具过滤已锁死能力边界所以自动批准安全）。终端版与 Textual 版两条 UI 链路都已接入。

## 第十四章 Git Worktree 文件系统隔离

上一章的上下文隔离是逻辑层面的（消息、权限、缓存各自独立），但文件系统仍然共享；Worktree 提供物理层面的隔离：每个子 Agent 在自己的独立工作目录中工作，彻底消除并行场景下的文件冲突：

- **Slug 安全校验**（`mewcode/worktree/slug.py`）：LLM 生成的名称不可信——按 `/` 分段逐段验证（允许字母/数字/点/连字符/下划线），显式拒绝 `.`/`..` 段（防路径遍历 `../../etc/passwd`），总长 ≤64；`/` 作为嵌套分隔符，转分支名时平铺为 `+`（feature/login → worktree-feature+login，避免 Git D/F 冲突）。
- **WorktreeManager 生命周期**（`worktree/manager.py`）：Create（快速恢复——目录已存在时纯文件系统读 HEAD SHA 不启动 git 子进程；全新创建用 `git worktree add -B` 容忍残留分支）/ Enter（清缓存 → 记录现场 → 持久化 session）/ Exit（变更保护：有未提交修改或新增 commit 时须显式 discard_changes）/ Remove / List / RestoreSession（先加载再验证，目录被删则清掉持久化记录）。asyncio.Lock 保护并发，git 命令统一注入 `GIT_TERMINAL_PROMPT=0` 防挂起。
- **创建后四步初始化**（`worktree/setup.py`，best-effort）：复制本地配置（settings.local.json / .env）→ 按主仓库 hooks 路径（.husky 优先，回退 .git/hooks）配置 core.hooksPath → 软链接大型依赖目录（列表来自 `.mewcode/config.yaml` 的 `worktree.symlink_directories`）→ 按 `.worktreeinclude`（gitignore 语法）复制被忽略但运行需要的文件。
- **变更检测**（`worktree/changes.py`）：`git status --porcelain`（未提交修改）+ `git rev-list --count {创建时HEAD}..HEAD`（新增提交）双维度——子 Agent 可能已 commit 未 push，status 干净但 rev-list 不是；fail-closed（git 失败视为有变更，宁可误留不误删）。
- **自动清理**（`worktree/cleanup.py`）：子 Agent 完成后 autoCleanup——没修改就删、有修改就保留并把路径/分支追加到结果供主 Agent review；后台周期回收孤儿临时目录，五层过滤（命名模式 `agent-*`/`wf_*` → 使用中/未过期 → 未提交修改 → 新增提交 → 未推送 commit），手动命名的 worktree 永不被自动清理。
- **会话状态持久化**（`worktree/session.py`）：WorktreeSession 写入 `.mewcode/worktree_session.json`，进程崩溃后下次启动自动恢复（恢复工具根目录到 worktree 继续工作）；退出时清成空对象而不是删文件——区分「从未使用」与「已退出」，是 --resume 正确工作的前提。
- **SubAgent 集成**（`worktree/integration.py` + `subagent/agent_tool.py`）：`AgentDef.isolation` 字段（frontmatter `isolation: worktree`，解析时校验合法值）与 Agent 工具 `isolation` 参数（参数级覆盖定义级）。executeWithWorktree 完整生命周期：以 `agent-+8位hex` 命名创建 → 任务文本前注入 Worktree 上下文通知（告诉子 Agent 它在隔离副本中、父路径需要翻译、编辑前重新读取）→ 以 worktree 为根重建工具注册中心（显式 cwd 机制）并运行 → 完成后自动清理。后台模式整个生命周期（含清理）在同一协程中执行。
- **/worktree 斜杠命令**：list（列出全部与当前位置）/ create / enter（工具根目录切到 worktree）/ exit（keep 保留或 remove 删除，--discard 显式丢弃）/ remove。

## 第十五章 Agent Teams 多 Agent 团队协作

从「星型」SubAgent 拓扑升级到「网状」团队结构。队员有自己的上下文窗口，可以直接给其他队员发消息，通过共享任务板与异步邮箱自主协调，Lead 还能开启 Coordinator Mode 专注调度：

- **核心数据结构**（`mewcode/teams/models.py`）：`AgentTeam`（名称 / Lead / 队员花名册 / 持久化位置）+ `TeammateInfo`（名称 / agent_id / agent_type / model / worktree_path / backend_type / is_active）。`is_active` 三态（True 工作中 / False 空闲 / None 未开始），没有 terminated 墓碑——终止的队员直接从花名册移除。团队配置持久化到 `~/.mewcode/teams/{name}/config.json`（可用 `MEWCODE_TEAMS_DIR` 覆盖），`save/load` 成对出现让 pane 队员进程能读回团队身份；`unique_team_name` 同名自动加序号。
- **三种执行后端**（`teams/backend_detect.py` + `spawn_*.py`）：tmux（split-window 三级降级：当前 session 分屏 → 新建 window 再分 → 新建 session）、iTerm2（it2 CLI split-pane，复用 tmux 的 CLI 命令构建）、in-process（asyncio.Task 包装，生命周期绑定 Lead）。检测按环境优先级一次性决定（tmux 会话 > iTerm2+it2 > tmux 二进制 > in-process）；检测不到终端后端时显式回退 in-process 并在输出中说明，不静默退化。CLI 命令形如 `MEWCODE_TEAM_NAME=x MEWCODE_TEAMMATE_NAME=y mewcode --teammate --work-dir …`，prompt 单引号转义防 shell 解析破坏。
- **Mailbox 文件邮箱**（`teams/mailbox.py`）：每条消息一个独立 JSON 文件（时间戳前缀，`sorted` 即时间序），多进程并发写不同文件无需文件锁；`consume` 读取即删（消费语义），坏文件跳过不删；支持广播（可排除发件人）与结构化消息类型（`shutdown_request` / `shutdown_response` / `plan_approval_response`）。消息在收件人下一轮 Agent Loop 开头被消费并以 system reminder 注入（引擎新增 `pre_turn_inject` 回调）——队员之间可以直接寻址，通信本质上是异步的。
- **共享任务板**（`teams/shared_task.py` + `team_task_tools.py`）：`SharedTaskStore` 提供 CRUD 与 `blocks`/`blocked_by` 依赖字段（追加语义、去重），任务 ID 顺序编号；`TaskCreate` / `TaskGet` / `TaskList` / `TaskUpdate` 四个工具绑定 store provider——队员侧绑定固定任务板，Lead 侧按活跃团队动态解析。
- **TeamManager 生命周期**（`teams/manager.py`）：创建（唯一命名 → 初始化 config.json / tasks.json / mailbox 三件套）、注册成员（花名册持久化 + `AgentNameRegistry` 全局名称注册 + agent_id→team 反向映射）、空闲通知（推模型：队员完成主动写 Lead 邮箱）、删除（有活跃成员拒绝；逐个清理注册表 / asyncio task / tmux pane / worktree，最后删目录）。
- **队员构造与续写**（`teams/teammate.py`）：Agent 工具新增 `team_name` 参数——非空时子 Agent 成为队员：自动创建独立 Worktree（`team-{团队}/{队员}` 嵌套 slug）→ 注入协作工具（基础工作工具 + 共享任务四件套 + SendMessage；Agent 工具不在池中，队员不能 spawn 队员）→ 系统提示追加三行团队附录（纯文本回复队友不可见，通信必须用 SendMessage）→ 完成后存 transcript、标记空闲、通知 Lead。**续写**：Lead 给已停止的队员 SendMessage，系统自动从磁盘 transcript 恢复其完整对话历史继续工作——队员是暂停/恢复式的持续工作者，不是一次性临时工。
- **Coordinator Mode**（`teams/coordinator.py`）：双锁激活（config.yaml `features.coordinatorMode` + 环境变量 `MEWCODE_COORDINATOR_MODE`）。激活后 Lead 的工具集收窄到调度 + 读类（`Agent` / `SendMessage` / `Task*` / `TeamCreate` / `TeamDelete` / `ReadFile` / `Glob` / `Grep` / `Bash`），剥夺 WriteFile/EditFile；同时注入四阶段工作流系统提示词（Research → Synthesis → Implementation → Verification，验证必须由独立 worker 执行）与反模式说明（lazy delegation：Lead 必须消化研究结果后给出带文件路径和行号的具体指令）。
- **pane 队员入口**（`teams/pane_entry.py`）：tmux/iTerm2 后端在窗格里启动完整 CLI 实例，通过环境变量识别团队身份，进入「跑任务 → 存 transcript → 标记空闲通知 Lead → 500ms 轮询邮箱 → 收到新消息继续工作」的队员循环。
