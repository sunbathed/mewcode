# MewCode 第五阶段实现任务

> 任务按依赖顺序排列；本阶段在 `mewcode/engine/` 下新增 `builder.py` 与 `sections.py`，改造 `plan_mode.py`、`agent.py`、`llm_client.py`，更新 `tools/` 工具描述，并补充对应测试与评估套件。

## 1. 建立 Section 契约与 PromptBuilder 组装器

- **影响文件：** `src/mewcode/engine/builder.py`
- **依赖任务：** 无
- **参考资料定位：** `docs/spec.md:29-37`（拼装契约与组装器）
- **交付内容：**
  - 定义 `Section` 数据结构（`name: str`, `priority: int`, `content: str`）。
  - 定义 `PromptBuilder` 类，提供 `add(section: Section) -> PromptBuilder` 支持链式添加，以及 `build() -> str` 方法。
  - `build()` 逻辑：过滤 `content` 为空或纯空白的 Section，按照 `priority` 升序排列，并使用 `\n\n` 进行合并拼接。

## 2. 编写七大固定提示词核心模块

- **影响文件：** `src/mewcode/engine/sections.py`
- **依赖任务：** 1
- **参考资料定位：** `docs/spec.md:39-81`（七大核心指令模块）
- **交付内容：**
  - `identity_section() -> Section`（Priority 0，Name: `"Identity"`）
  - `system_section() -> Section`（Priority 10，Name: `"System"`）
  - `doing_tasks_section() -> Section`（Priority 20，Name: `"DoingTasks"`）
  - `executing_actions_section() -> Section`（Priority 30，Name: `"ExecutingActions"`）
  - `using_tools_section() -> Section`（Priority 40，Name: `"UsingTools"`）
  - `tone_style_section() -> Section`（Priority 50，Name: `"ToneStyle"`）
  - `output_efficiency_section() -> Section`（Priority 60，Name: `"TextOutput"`）
  - 核心提示词文本包含精确指令（终端编程助手定位、安全漏洞防范、专用工具优先、探索性问题先建议不妄动、三行相似代码比过早抽象好、高风险操作清单、无 emoji、代码引用格式等）。

## 3. 实现环境上下文抽象与探测

- **影响文件：** `src/mewcode/engine/builder.py`、`src/mewcode/engine/sections.py`
- **依赖任务：** 1、2
- **参考资料定位：** `docs/spec.md:83-103`（环境上下文动态拼装）
- **交付内容：**
  - 定义 `EnvironmentContext` 数据类（`work_dir`, `os_name`, `arch`, `shell`, `is_git_repo`, `git_branch`, `date`, `model`）。
  - 实现 `detect_environment(workdir: str | None = None, model: str = "") -> EnvironmentContext` 探测函数：通过系统接口获取 OS、Arch、Shell、Date；通过 `git -C workdir rev-parse` 探测 Git 仓库状态与分支名（探测失败不抛异常，优雅降级）。
  - 在 `sections.py` 实现 `environment_section(env: EnvironmentContext) -> Section`（Priority 70，Name: `"Environment"`）。

## 4. 建立条件注入入口与主编排函数

- **影响文件：** `src/mewcode/engine/builder.py`、`src/mewcode/engine/sections.py`
- **依赖任务：** 3
- **参考资料定位：** `docs/spec.md:105-117`（条件注入与主编排函数）
- **交付内容：**
  - 定义 `BuildOptions` 数据类（`custom_instructions: str = ""`, `skill_instructions: str = ""`, `memory_instructions: str = ""`）。
  - 在 `sections.py` 实现 `custom_instructions_section(content: str) -> Section` (80)、`skill_section(content: str) -> Section` (90)、`memory_section(content: str) -> Section` (95)。
  - 实现主编排入口 `build_system_prompt(env: EnvironmentContext, options: BuildOptions | None = None) -> str`：装配 7 核心模块 + 环境模块 + 可选扩展模块，产出完整稳定的 System Prompt。

## 5. 重构 Plan Mode：三段模板与频率控制

- **影响文件：** `src/mewcode/engine/plan_mode.py`
- **依赖任务：** 1
- **参考资料定位：** `docs/spec.md:119-136`（Plan Mode 三模板与频率控制）
- **交付内容：**
  - 定义三段模板：`PLAN_MODE_FULL_REMINDER`（五阶段工作流与只读约束）、`PLAN_MODE_SPARSE_REMINDER`（精简提醒）、`PLAN_MODE_EXIT_REMINDER`。
  - 实现 `build_plan_mode_reminder(plan_file_path: str, plan_exists: bool, iteration: int) -> str`：第 1 轮完整版，之后每 5 轮（`iteration % 5 == 1`）发完整版，其余轮次精简版；按 `plan_exists` 提示“读现有计划”还是“起草新计划”。
  - 明确 Plan Mode 指令不再硬拼接进 System Prompt。

## 6. 引入 system-reminder 消息封装与 API 组装管线

- **影响文件：** `src/mewcode/engine/prompt_pipeline.py`（或在 `builder.py`/`agent.py` 中）
- **依赖任务：** 4、5
- **参考资料定位：** `docs/spec.md:133-144`（system-reminder 与 assemble_api_payload）
- **交付内容：**
  - 实现 `system_reminder_message(content: str) -> dict[str, Any]`：构建 `role="user"`，包裹 `<system-reminder>` XML 标签的补充指令消息。
  - 实现 `assemble_api_payload(...)`：支持将稳定的 System Prompt（可附加缓存标签）、对话历史、动态 system-reminder 及工具列表完整装配。

## 7. 强化六大工具描述（双重强化）

- **影响文件：** `src/mewcode/tools/files.py`、`src/mewcode/tools/bash.py`、`src/mewcode/tools/search.py`
- **依赖任务：** 无
- **参考资料定位：** `docs/spec.md:138-146`（工具描述强化）
- **交付内容：**
  - 更新 `ReadFile.description`：指出优先于 Bash cat/head，大文件处理与编辑前必读。
  - 更新 `WriteFile.description`：指出自动建目录，优先于 echo/cat heredoc。
  - 更新 `EditFile.description`：指出编辑前必须先用 ReadFile 读取，精确替换与整体原子性。
  - 更新 `Bash.description`：指出专用于系统命令与必须 shell 执行的操作，读写搜索绝不用 Bash。
  - 更新 `Glob.description` 与 `Grep.description`：指出优先于 find/ls 与 grep/rg。

## 8. 扩展 LLM Client 与 Agent：Prompt Cache 标记与 Usage 解析

- **影响文件：** `src/mewcode/llm_client.py`、`src/mewcode/engine/events.py`、`src/mewcode/engine/agent.py`
- **依赖任务：** 6、7
- **参考资料定位：** `docs/spec.md:148-154`（Prompt Cache 支持与用量捕获）
- **交付内容：**
  - `llm_client.py` 流式处理支持捕获 chunk 中的 `usage` 字典（提取 `prompt_tokens_details.cached_tokens` 或 `cache_read_input_tokens`）。
  - `UsageEvent` 增加 `cached_tokens` 字段（向后兼容默认 0）。
  - `Agent` 使用 `build_system_prompt` 生成固定 system prompt，在 Plan Mode 下通过 `system-reminder` 轮次注入提醒。

## 9. 编写模块单元测试与集成测试

- **影响文件：** `tests/test_prompt_builder.py`、`tests/test_prompt_sections.py`、`tests/test_plan_mode_reminders.py`、`tests/test_engine_agent.py`
- **依赖任务：** 1、2、3、4、5、6、7、8
- **参考资料定位：** `docs/spec.md:162-171`（非功能要求与完成定义）
- **交付内容：**
  - 测试 Builder 排序、过滤空 Section、链式调用。
  - 测试环境探测与假数据注入。
  - 测试 Plan Mode 轮次频率控制（第 1 轮、第 2-4 轮、第 5 轮、第 6 轮...）。
  - 测试现有全部测试用例保持 100% 通过。

## 10. 编写典型场景评估脚本

- **影响文件：** `tests/test_prompt_eval.py` 或 `scripts/eval_scenarios.py`
- **依赖任务：** 9
- **参考资料定位：** `docs/spec.md:156-160`（定性评估场景）
- **交付内容：**
  - 覆盖 5 大典型场景验证：身份认知、读文件工具选型、代码抽象态度、高风险操作警惕、风格限制（无 emoji）。
