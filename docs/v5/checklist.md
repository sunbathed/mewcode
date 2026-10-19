# MewCode 第五阶段验收清单

## 1. 拼装契约与 Builder（`builder.py`）

- [x] `Section` 包含 `name: str`、`priority: int`、`content: str` 三字段，不可变或语义清晰。
- [x] `PromptBuilder.add` 支持链式调用 `builder.add(s1).add(s2)`。
- [x] `PromptBuilder.build` 严格按 `priority` 升序排序拼接（例如 priority 0 在 10 之前）。
- [x] 无论调用方以何种顺序添加 Section，最终输出顺序均由 `priority` 决定。
- [x] 过滤掉 `content` 为空字符串或纯空白字符的 Section，不产生多余的分隔空行。
- [x] 各有效 Section 之间使用两个换行符 `\n\n` 分隔。

## 2. 七大核心提示词模块（`sections.py`）

- [x] `identity_section`：Priority 0，包含 MewCode 身份定位、终端环境、软件工程核心任务、安全漏洞防范、绝不猜测 URL 指令。
- [x] `system_section`：Priority 10，包含文本输出可见性、权限规则、`<system-reminder>` 说明、外部输入 prompt 注入警惕、hooks 机制、上下文压缩机制。
- [x] `doing_tasks_section`：Priority 20，包含探索性问题先建议不妄动、先读代码再提修改、优先编辑现有文件、三行相似代码比过早抽象好、只在系统边界校验、默认不写注释、必须测试验证且严禁谎报测试通过。
- [x] `executing_actions_section`：Priority 30，明确本地可逆操作与高风险操作边界，包含删除/force-push/reset/影响他人操作的高风险清单，严禁把破坏性操作当捷径。
- [x] `using_tools_section`：Priority 40，明确有专用工具绝不用 Bash（读写编辑搜索专用工具与 Bash 边界），声明独立工具并行调用、多步任务使用 TaskCreate、Agent 子代理与延迟加载 ToolSearch 预告。
- [x] `tone_style_section`：Priority 50，声明除非明确要求否则不使用 emoji，代码引用统一使用 `file_path:line_number` 格式，工具调用前文本不以冒号结尾。
- [x] `output_efficiency_section`：Priority 60，声明第一次工具调用前一句话说明意图，关键节点简短更新，不实况播报内部权衡，回合结尾一两句话总结，严禁产出未经要求的中间分析或计划文件。

## 3. 环境上下文动态拼装与探测

- [x] `EnvironmentContext` 数据结构覆盖 `work_dir`、`os_name`、`arch`、`shell`、`is_git_repo`、`git_branch`、`date`、`model`。
- [x] `detect_environment` 在不同操作系统下能正确获取 OS/Arch/Shell/Date。
- [x] 在 Git 仓库目录下能探测到 `is_git_repo = True` 及当前分支名。
- [x] 在非 Git 仓库或无 git 命令环境下，能优雅降级（`is_git_repo = False`, `git_branch = ""`），不抛错、不崩溃。
- [x] `environment_section`：Priority 70，格式化输出工作目录、平台架构、Shell、Git 状态、模型与日期。
- [x] 环境探测函数与内容生成解耦，单元测试可传入任意模拟 `EnvironmentContext` 验证输出。

## 4. 条件注入扩展点与主编排函数

- [x] `BuildOptions` 提供 `custom_instructions`、`skill_instructions`、`memory_instructions` 三个可选字段。
- [x] `custom_instructions_section` 优先级为 80，仅在有内容时加入 System Prompt。
- [x] `skill_section` 优先级为 90，仅在有内容时加入。
- [x] `memory_section` 优先级为 95，仅在有内容时加入。
- [x] `build_system_prompt(env, options)` 按升序正确组合 7 大核心模块、环境模块及非空可选模块。
- [x] `build_system_prompt` 为无状态纯函数，多次调用结果稳定可复现。

## 5. Plan Mode 三模板与频率控制（`plan_mode.py`）

- [x] `build_system_prompt` 不再拼接 Plan Mode 指令，保证主 System Prompt 全局稳定。
- [x] 提供完整版 `PLAN_MODE_FULL_REMINDER`、精简版 `PLAN_MODE_SPARSE_REMINDER` 与退出提示 `PLAN_MODE_EXIT_REMINDER`。
- [x] `build_plan_mode_reminder(plan_file_path, plan_exists, iteration)` 按轮次控制：
  - 第 1 轮（`iteration == 1`）返回完整版。
  - 第 2~5 轮返回精简版。
  - 第 6 轮（`iteration == 6`）再次返回完整版（即每 5 轮循环一次完整版）。
- [x] 当 `plan_exists == True` 时提示模型阅读已有 plan 文件；当 `plan_exists == False` 时提示模型创建新 plan 文件。
- [x] Plan Mode 提醒作为 `<system-reminder>` 消息注入对话通道，而非追加在系统提示词末尾。

## 6. system-reminder 机制与 API 组装

- [x] `system_reminder_message(content)` 生成标准消息：`role: "user"`，`content: "<system-reminder>\n...\n</system-reminder>"`。
- [x] 模型能够识别 `<system-reminder>` 为系统指令而非用户输入，不直接回复该提醒。
- [x] `assemble_api_payload` 实现 system、messages（含 system-reminder）、tools 的统一装配。

## 7. 工具描述强化（双重强化）

- [x] `ReadFile` 描述中包含：路径参数规范、优先于 Bash cat/head、大文件处理、以及“编辑文件前必须先用本工具读取”。
- [x] `WriteFile` 描述中包含：自动创建父目录、创建文件优先于 echo / 重定向。
- [x] `EditFile` 描述中包含：编辑前必须先用 ReadFile 读取、精确查找替换、多处编辑原子性。
- [x] `Bash` 描述中包含：专用于系统命令与必须 shell 执行的操作，读写与搜索绝不要使用 Bash。
- [x] `Glob` 描述中包含：优先于 find / ls。
- [x] `Grep` 描述中包含：优先于 grep / rg。

## 8. Prompt Cache 控制与用量解析

- [x] API 请求 payload 中 System 消息支持缓存标记配置（`cache_control: {"type": "ephemeral"}`）。
- [x] `llm_client.py` 流式 chunk 解析能够提取 API 返回的 `usage` 信息中的 `cached_tokens`（兼容 OpenAI `prompt_tokens_details.cached_tokens` 与 Anthropic `cache_read_input_tokens`）。
- [x] `UsageEvent` 包含 `cached_tokens` 字段，并在 TUI 或日志中可见。

## 9. 回归测试与质量保证

- [x] 新增 `test_prompt_builder.py` 覆盖 Builder 契约与排序。
- [x] 新增 `test_prompt_sections.py` 覆盖七核心模块与环境模块生成。
- [x] 新增 `test_plan_mode_reminders.py` 覆盖三模板与轮次频率控制。
- [x] 原有全部 141+ 测试无缝通过，无破坏性变更。
- [x] 运行 `pytest` 100% 通过。

## 10. 五大典型场景定性评估（`tests/test_prompt_eval.py`）

- [x] 场景 1（身份问答）：输入「你是谁，什么名字」，验证提示词包含 MewCode 身份与终端工程助手定位。
- [x] 场景 2（工具偏好）：输入「查看 auth.py 的内容」，验证提示词与工具描述明确引导优先使用 `ReadFile` 而非 `cat`。
- [x] 场景 3（抽象倾向）：输入「你对于代码的抽象会怎么做」，验证提示词明确「三行相似代码比过早抽象好，不为未来假想需求设计」。
- [x] 场景 4（谨慎操作）：输入「哪些执行操作需要谨慎」，验证提示词包含破坏性操作、不可撤销操作、影响他人操作的三大分类。
- [x] 场景 5（风格限制）：输入「什么情况才能回复 emoji」，验证提示词严格限制默认不使用 emoji。
