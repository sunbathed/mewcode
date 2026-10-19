# MewCode 第六阶段实现任务

> 任务按依赖顺序排列；本阶段在 `mewcode/permission/` 下新增五层权限模块，改造 `engine/agent.py`、`engine/events.py`、`app.py`、`tui.py`、`commands.py`，并补充对应测试。

## 1. 危险命令黑名单（第 1 层）

- **影响文件：** `src/mewcode/permission/dangerous.py`
- **依赖任务：** 无
- **参考资料定位：** `docs/spec.md:3.1`；理论篇「第一道防线：危险命令黑名单」
- **交付内容：**
  - `DEFAULT_DANGEROUS_PATTERNS`：文档表格 8 条默认黑名单正则。
  - `DangerousCommandDetector.match(command) -> reason | None`：编译正则并对命令做 search。
  - 支持自定义模式注入。

## 2. 路径沙箱（第 2 层）

- **影响文件：** `src/mewcode/permission/sandbox.py`
- **依赖任务：** 无
- **参考资料定位：** `docs/spec.md:3.2`；理论篇「第二道防线：路径沙箱」
- **交付内容：**
  - `PathSandbox(workdir, extra_dirs, allow_temp=True)`：默认允许项目根目录 + 系统临时目录。
  - `validate(requested_path) -> (ok, detail)`：绝对路径化 → 符号链接解析（失败退父目录）→ 前缀检查。
  - 比较大小写不敏感（os.path.normcase）。

## 3. 权限规则引擎（第 3 层）

- **影响文件：** `src/mewcode/permission/rules.py`
- **依赖任务：** 无
- **参考资料定位：** `docs/spec.md:3.3`；理论篇「第三道防线：权限规则」
- **交付内容：**
  - `PermissionRule`：`ToolName(pattern)` 解析，`matches` 用 `fnmatch.fnmatchcase` 做 glob 匹配。
  - `parse_rules` / `load_rules`：YAML 子集解析（两列 + 单行简写），文件不存在返回空列表。
  - `RuleEngine`：三层路径（本地/项目/用户），`evaluate` 先合并 deny（跨层不可翻转）再按高优先级找 allow；`append_local_rule` 持久化「始终允许」规则。
  - 规则文件路径：`{workdir}/.mewcode/permissions.local.yaml`、`{workdir}/.mewcode/permissions.yaml`、`~/.mewcode/permissions.yaml`。

## 4. 权限模式（第 4 层）

- **影响文件：** `src/mewcode/permission/modes.py`
- **依赖任务：** 无
- **参考资料定位：** `docs/spec.md:3.4`；理论篇「第四道防线：权限模式」（决策矩阵表）
- **交付内容：**
  - `PermissionMode`：default / acceptEdits / plan / bypassPermissions。
  - `MODE_ALIASES`：全小写键，别名 yolo/bypass → bypassPermissions。
  - `decide_mode(mode, safety) -> allow | ask`：按矩阵实现；safety 分 read/write/command。
  - `parse_mode` / `infer_safety`。

## 5. HITL 人机协同（第 5 层）

- **影响文件：** `src/mewcode/permission/hitl.py`
- **依赖任务：** 无
- **参考资料定位：** `docs/spec.md:3.5`；理论篇「第五道防线：HITL 人在回路」
- **交付内容：**
  - `PermissionResponse`：ALLOW / DENY / ALLOW_ALWAYS。
  - `build_permission_description`：确认对话框展示的工具调用描述。
  - `build_allow_rule_text`：Bash 取前两词 + `*`，文件工具取 path/pattern 精确匹配。

## 6. 权限检查器：五层决策链

- **影响文件：** `src/mewcode/permission/checker.py`
- **依赖任务：** 1、2、3、4
- **参考资料定位：** `docs/spec.md:3.6`；理论篇「把五层防线串成决策链」
- **交付内容：**
  - `PermissionDecision`：ALLOW / DENY / ASK。
  - `extract_content` / `extract_target_path`：按工具提取 command/path/pattern。
  - `PermissionChecker.check`：黑名单(仅 Bash) → 沙箱(文件工具) → 规则 → 模式，短路决策。
  - `remember_always`：生成规则并写入本地配置文件。
  - 安全分类内置映射：ReadFile/Glob/Grep=read，Write/Edit/Replace=write，Bash=command。

## 7. 事件系统与 Agent Loop 嵌入

- **影响文件：** `src/mewcode/engine/events.py`、`src/mewcode/engine/agent.py`、`src/mewcode/engine/__init__.py`
- **依赖任务：** 6
- **参考资料定位：** `docs/spec.md:3.5、3.7`；理论篇「嵌入 Agent Loop」
- **交付内容：**
  - `PermissionRequestEvent`（kind="permission_request"，携带 response_future）加入 AgentEvent 联合。
  - Agent 构造增加 `permission` 参数（None 保持向后兼容）。
  - 工具执行前逐工具决策：DENY → 拦截结果；ASK → HITL 阻塞等待；规划模式拦截位优先。
  - 权限拒绝不计入 `tool_error_turns`（理论篇「被拒绝不等于停下来」）。

## 8. App（终端版）集成

- **影响文件：** `src/mewcode/app.py`、`src/mewcode/commands.py`
- **依赖任务：** 7
- **参考资料定位：** `docs/spec.md:3.8`；实战篇验证流程
- **交付内容：**
  - App 构造接受 `permission_mode` / `permission`，默认 default 模式。
  - `_ask` 事件循环处理 `PermissionRequestEvent`，`_prompt_permission` 渲染确认对话框（y/n/a 与 1/2/3）。
  - `/permissions` 命令：查看模式与规则统计、切换模式；`KNOWN_COMMANDS` 与 help 更新。

## 9. TUI 全屏版集成

- **影响文件：** `src/mewcode/tui.py`
- **依赖任务：** 7
- **参考资料定位：** `docs/spec.md:3.8`；实战篇「弹出 Modal 对话框」
- **交付内容：**
  - `PermissionModal(ModalScreen)`：展示工具调用，按钮 + BINDINGS 快捷键。
  - `_permission_check`：五层检查，ASK 用 `push_screen(wait_for_dismiss=True)` 等待用户回复。
  - `_execute_tools` 逐工具前置权限检查；`/permissions` 命令支持。

## 10. 权限专项测试

- **影响文件：**
  - `tests/test_permission_unit.py`（五层单测）
  - `tests/test_permission_agent.py`（Agent 集成：DENY 不终止、HITL、始终允许循环）
  - `tests/test_app_permissions.py`（App 层：/permissions 命令、对话框）
  - `tests/test_tui_permissions.py`（TUI：Modal、按键、模式切换）
  - `tests/test_e2e.py`（组合链路测试适配 bypassPermissions 模式）
- **依赖任务：** 1-9
- **参考资料定位：** `docs/spec.md:4、5`
- **交付内容：**
  - 黑名单命中/放行、沙箱 symlink 逃逸拦截、规则三层优先级与 deny 合并、模式矩阵。
  - Agent 集成：危险命令 DENY 后循环继续 END_TURN；HITL 三种回复；acceptEdits/bypass 无弹窗；权限拒绝不触发 TOOL_ERROR_LIMIT。
  - 原有全部测试保持 100% 通过。

## 11. 文档与验收

- **影响文件：** `README.md`、`docs/spec.md`、`docs/tasks.md`、`docs/checklist.md`
- **依赖任务：** 10
- **参考资料定位：** `docs/spec.md:5`；MEWCODE.md 三份文档工作流
- **交付内容：**
  - 第六章 spec/tasks/checklist 三份文档写入 docs（第五章归档至 docs/v5/）。
  - README「当前范围」更新为第六章权限系统。
  - 执行 `pytest` 全部通过。