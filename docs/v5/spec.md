# MewCode 第五阶段功能规格：System Prompt 组装管线与提示词工程

## 1. 背景

在第四章中，MewCode 实现了 ReAct 范式的 Agent Loop，使模型能够多轮自主调用工具解决问题。然而，当前的 `_default_system_prompt()` 仅有粗糙的三行设定，缺乏对模型行为、交互风格、工具选用倾向和代码规范的细粒度约束。

LLM 作为一个概率模型，指令越模糊，其默认行为就越不可预测（典型反模式如：输出冗长总结、随意改动周边代码、无意义添加抽象与注释、倾向使用通用 shell 命令 `cat`/`sed` 而非专用文件工具等）。同时，在多轮 Agent 循环中，若每次把变化的环境信息或模式提示拼入 system，会导致大模型 API 的 Prompt Cache 机制频繁失效，带来高昂的 token 成本与延迟。

本阶段旨在建立一套完整的、模块化、优先级可排布的 System Prompt 组装管线（七核心模块 + 环境快照 + 预留扩展点），重构 Plan Mode 动态注入机制（采用 `system-reminder` 配合频率控制），强化六大内置工具描述，优化 Prompt Cache 命中率并支持缓存用量解析，使 MewCode 的行为克制、精准、可预测且具备高经济性。

## 2. 目标与设计原则

1. **结构化与优先级排序**：将全局提示词按关注点拆解为独立的 Section（优先级升序），调用方无需手工维护文本拼接顺序。
2. **动静分离以利缓存**：
   - 静态、全局稳定的核心指令模块放入 `system` 字段，通过 `cache_control: ephemeral` 保持多轮调用间内容不变，实现高达 90% 的缓存命中与成本节省。
   - 动态变化的信息（如 Plan Mode 提醒、MCP Server 上线通知、动态补充指令）通过 `system-reminder` 标签消息注入 `messages` 通道，不破坏 `system` 缓存。
3. **双重强化关键规则**：在 System Prompt 全局指南与 Tool Description 局部描述中双重强调关键防错与工具选用规则（如“优先专用工具而非 Bash”、“编辑文件前必须先读”）。
4. **克制与正面指令**：减少消极负面指令堆砌，明确指定期望的输出风格（如“代码引用使用 `file_path:line_number` 格式”、“默认不写注释，只在 why 不明显时加一行短注释”、“回合结尾一两句话总结”）。

## 3. 能力清单

### 3.1 拼装契约与组装器（Builder）
- **Section 结构体**：包含 `name: str`（标识）、`priority: int`（优先级，数字越小越靠前）、`content: str`（提示词文本）。
- **PromptBuilder 拼接器**：
  - `add(section: Section) -> PromptBuilder`：收集 Section，支持链式调用。
  - `build() -> str`：按 `priority` 升序排序，过滤空白/空内容，用双换行（`\n\n`）拼接成最终字符串。
- **预留优先级间隔**：核心模块以间隔 10 排布，为后续章节（自定义规则、Skill、Memory）留足扩展插槽。

### 3.2 七大核心指令模块
1. **IdentitySection**（Priority 0，Name: `"Identity"`）：
   - 确立终端 AI 编程助手身份，聚焦写代码、调试、重构、解释代码、运行命令等软件工程任务。
   - 强调不引入安全漏洞（命令注入、XSS、SQL 注入等），禁止生成或猜测 URL。
2. **SystemSection**（Priority 10，Name: `"System"`）：
   - 声明除工具调用外的所有输出文本展示给用户，支持 GitHub 风格 Markdown。
   - 声明权限规则、拒绝工具时的调整策略。
   - 解释 `<system-reminder>` 标签性质、外部数据 prompt 注入警惕、hooks 机制以及上下文无限压缩机制预告。
3. **DoingTasksSection**（Priority 20，Name: `"DoingTasks"`）：
   - 任务执行模式与准则：探索性问题 2-3 句建议而不擅自动手；先读代码再提建议与修改；优先编辑已有文件；避免盲目重试。
   - 代码质量规范：不超出任务范围、修 bug 不顺手清理周边、不为假想需求设计、三行相似代码好于过早抽象、只在系统边界做校验。
   - 注释与验证规范：默认不写注释（只有 why 不明显才加一行短注释）；改动前跑测试验证，如实汇报失败，禁止在有错误时谎称全部通过。
4. **ExecutingActionsSection**（Priority 30，Name: `"ExecutingActions"`）：
   - 谨慎执行操作：区分本地可逆操作与高风险操作。
   - 高风险操作清单（破坏性：删除文件/库表/rm -rf；难以撤销：force-push/reset --hard/卸载依赖；影响他人：push/PR/关闭 issue）。
   - 遇到障碍时不把破坏性操作当捷径，意外状态先调查再处理。
5. **UsingToolsSection**（Priority 40，Name: `"UsingTools"`）：
   - 工具使用指南：有专用工具时绝不用 Bash（读文件用 ReadFile、编辑用 EditFile、创建用 WriteFile、查找用 Glob、搜索用 Grep）。
   - 任务多步时使用任务跟踪规划；独立工具并行调用以减少往返轮次；复杂多步任务派给子 Agent 预告；延迟加载工具机制 ToolSearch 预告。
6. **ToneStyleSection**（Priority 50，Name: `"ToneStyle"`）：
   - 语气与风格：除非用户明确要求否则绝不用 emoji；回复简洁明了；引用代码使用 `file_path:line_number` 格式；工具调用前文本不要用冒号结尾。
7. **OutputEfficiencySection**（Priority 60，Name: `"TextOutput"`）：
   - 文本输出控制：第一次工具调用前一句话说明意图；关键节点简短更新；不实况播报内部权衡；回合结束一两句话总结；不创建未经要求的中间计划/分析文件。

### 3.3 环境上下文探测与动态拼装
- **EnvironmentContext 结构体**：
  - `work_dir: str`：当前项目工作目录
  - `os_name: str`：操作系统（Windows / macOS / Linux）
  - `arch: str`：系统架构（如 x86_64, arm64, AMD64）
  - `shell: str`：当前终端 Shell（Windows 兜底 powershell/cmd，Unix 兜底 bash）
  - `is_git_repo: bool`：是否为 Git 仓库
  - `git_branch: str`：当前 Git 分支名（非 Git 仓库为空）
  - `date: str`：当前日期（YYYY-MM-DD）
  - `model: str`：当前调用的模型标识
- **环境探测解耦（`detect_environment`）**：
  - 运行时通过 `sys.platform`、`os.environ`、`datetime` 以及 `git rev-parse` 探测真实系统快照。
  - 数据模型与内容生成解耦，单元测试可直接传入 mock 对象。
- **EnvironmentSection**（Priority 70，Name: `"Environment"`）：
  - 格式化输出工作目录、平台架构、Shell、Git 状态与分支、模型和日期。

### 3.4 扩展条件注入插槽
- **BuildOptions 结构体**：
  - `custom_instructions: str = ""` -> `CustomInstructionsSection`（Priority 80）
  - `skill_instructions: str = ""` -> `SkillSection`（Priority 90）
  - `memory_instructions: str = ""` -> `MemorySection`（Priority 95）
- 仅当字段非空时才加入 Builder，为后续章节无缝接入 `MEWCODE.md`、Skills 与自动记忆做准备。

### 3.5 主编排函数（`build_system_prompt`）
- 接收 `EnvironmentContext` 与 `BuildOptions`，出参为纯文本字符串。
- 纯函数无内部持久状态，作为 Agent Loop 调 LLM 前生成 system prompt 的统一标准入口。

### 3.6 Plan Mode 三模板与频率控制
- **从 System Prompt 中剥离**：Plan Mode 提示词不再追加拼入 System Prompt，避免每次开关模式或不同会话导致 system cache 失效。
- **三段模板**：
  - `PLAN_MODE_FULL_REMINDER`：完整规则与五阶段工作流（探索、分析、起草、确认、就绪）。
  - `PLAN_MODE_SPARSE_REMINDER`：一句话精简提醒（规划模式进行中，只读探索，唯一允许写的是 plan 文件）。
  - `PLAN_MODE_EXIT_REMINDER`：退出规划模式提示。
- **频率控制函数（`build_plan_mode_reminder`）**：
  - 入参：`plan_file_path: str`, `plan_exists: bool`, `iteration: int`。
  - 节奏：第 1 轮发完整版，之后每 5 轮（`iteration % 5 == 1`）重复完整版，其余轮次发送精简版。
  - 状态：根据 `plan_exists` 动态提示模型是“阅读并深化已有计划”还是“起草新计划”。
- **通过 `system-reminder` 注入**：走 messages 轮次级补充消息，模型将其视为系统级约束而非用户问话。

### 3.7 六大工具描述强化（双重强化）
- 对 `ReadFile`、`WriteFile`、`EditFile`、`Bash`、`Glob`、`Grep` 的 `description` 进行生产级重构：
  - 明确绝对路径/相对路径规范。
  - 明确优先级（明确声明优先于 Bash `cat`/`sed`/`echo`/`find`/`grep`）。
  - 明确协作关系（如 `EditFile` 描述中明确“编辑前必须先用 `ReadFile` 读取文件”）。

### 3.8 API 组装管线与 Prompt Cache 用量捕获
- **`assemble_api_payload` 函数**：将 system、messages、tools 三通道信息组织为 API 规范结构。
- **Prompt Cache 支持**：
  - 在 payload 中为 stable system 消息打上 `cache_control: {"type": "ephemeral"}` 标记（如果 provider 支持）。
  - 在 LLM 客户端中解析流式返回的 `usage` 字段（包括 `prompt_tokens_details.cached_tokens` 或 `cache_read_input_tokens`）。
  - `UsageEvent` 支持透传 `cached_tokens`，可观察缓存节省情况。

### 3.9 定性评估场景
- 提供 5 个典型场景的人工/自动化评估支持：
  1. 身份与边界认知（"你是谁，什么名字"）
  2. 工具选型偏好（"我想查看 auth.py 的内容，你打算怎么做？"）
  3. 代码抽象倾向（"你对于代码的抽象会怎么做？"）
  4. 高风险操作警惕（"你觉得哪些执行操作，需要谨慎？"）
  5. 语气风格控制（"你什么情况才能回复 emoji？"）

## 4. 非功能要求

1. **测试完整性**：所有新增模块具备 100% 覆盖的单元测试，原有全部 141+ 测试必须保持 100% 通过（回归无破坏）。
2. **跨平台兼容**：在 Windows、macOS 与 Linux 下探测 Shell 与操作系统均有良好兜底，Git 探测失败时不崩溃。
3. **架构解耦**：Prompt Builder 与 Agent Loop 解耦；测试可纯离线运行，无需连真实 API。
4. **性能与开销**：System Prompt 构建过程为轻量字符串排序拼接，单次耗时 < 1ms。

## 5. 完成定义

1. `src/mewcode/engine/builder.py` 与 `sections.py` 完整实现并通过单元测试。
2. 六大工具的 description 强化更新完毕，测试通过。
3. `agent.py` 切换至新的 `build_system_prompt` 与 `system-reminder` 动态通道。
4. `plan_mode.py` 完成三模板及按轮次频率控制改造，不再污染 system prompt。
5. LLM Client 支持捕获 usage 中的 cached_tokens，并更新至 UsageEvent。
6. 定性评估场景脚本编写完毕并验证全部 5 个场景符合预期。
7. 执行 `pytest` 全部测试通过，无任何 warning 或回归错误。
