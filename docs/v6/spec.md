# MewCode 第六阶段功能规格：五层纵深权限防御

## 1. 背景

在第五阶段，MewCode 具备了完整的 Agent Loop 与 System Prompt 组装管线，能够自主读代码、写代码、跑命令、改 bug。但能力越大，风险也越大：Agent 理论上能在终端里做任何事，包括毁掉整个系统。

Agent 面临三类主要安全威胁：

1. **Prompt 注入**：攻击者在 Agent 可能读到的文件（README、配置、错误信息）里植入指令，诱导模型执行恶意操作。模型无法 100% 区分「用户的真实意图」和「文件里伪装的指令」，必须从系统层面防御。
2. **越权操作**：用户只授权修 bug，Agent 却「顺手」重构项目、更新 CI 配置。自主性超出预期。
3. **数据泄露**：Agent 读了 `.env`、`~/.ssh/id_rsa` 等敏感文件并在回复中引用，对话日志或第三方服务暴露后造成泄露。

单一防御机制总存在可绕过的边界情况。本阶段采用安全领域经典原则「多层防御」（Defense in Depth），为 MewCode 构建五层权限防线，在安全与效率之间找到平衡点。

## 2. 目标与设计原则

1. **五层纵深**：黑名单（硬拦截）→ 路径沙箱（硬拦截）→ 权限规则（细粒度）→ 权限模式（整体策略）→ HITL（人在回路兜底）。前两层任何配置都无法绕过，后三层可灵活配置。
2. **逐层短路决策**：上一层能决策就直接返回，不能决策才往下走。决策只有三种：ALLOW / DENY / ASK。
3. **拒绝不等于终止**：权限被拒绝时把「权限拒绝」作为错误类型的工具结果返回给模型，Agent Loop 继续运行，让模型有机会调整策略。
4. **权限学习循环**：「始终允许」自动生成 allow 规则追加到本地配置，越用越顺畅而不损失安全基线。

## 3. 能力清单

### 3.1 第一道防线：危险命令黑名单（`dangerous.py`）
- 仅对 Bash 工具生效，正则匹配命令字符串，命中直接 DENY。
- 默认黑名单模式（对应理论篇表格）：
  - `rm -rf /` 变体（递归强制删除根目录）
  - `mkfs.`（格式化磁盘）
  - `dd ... of=/dev/`（直接写磁盘设备）
  - `chmod -R 777 /`（递归修改根目录权限）
  - fork bomb 进程炸弹
  - `curl/wget ... | bash/sh`（管道执行远程脚本）
  - `> /dev/sd`（覆盖磁盘设备）
- 支持注入自定义模式（`(pattern, reason)` 列表）。

### 3.2 第二道防线：路径沙箱（`sandbox.py`）
- 文件类工具（ReadFile/WriteFile/EditFile/ReplaceInFile）的路径操作限制在允许根目录内。
- 校验三步：解析为绝对路径 → 解析符号链接（防 symlink 逃逸）→ 前缀检查。
- 文件不存在时（WriteFile 创建新文件）退而求其次解析父目录，父目录在沙箱内则放行。
- 默认允许两个目录：项目根目录 + 系统临时目录；支持 `extra_dirs` 扩展白名单。

### 3.3 第三道防线：权限规则（`rules.py`）
- 规则语法：`ToolName(pattern)`，pattern 支持 glob 通配符。
- 内容提取映射（对应理论篇表格）：
  - Bash → `command` 参数；ReadFile/WriteFile/EditFile → `path` 参数；Glob/Grep → `pattern` 参数。
- **三层优先级**（越具体、越靠近当前项目，优先级越高，与 Git/ESLint/Claude Code 一致）：
  1. 本地级（最高）：`{project}/.mewcode/permissions.local.yaml`
  2. 项目级：`{project}/.mewcode/permissions.yaml`
  3. 用户级（最低）：`~/.mewcode/permissions.yaml`
- **deny 跨层合并，不可翻转**：任何一层 deny 都压过其他层的 allow。
- 同一层级内后定义规则优先级更高。
- 规则文件不存在时不报错，返回空规则集。
- 规则文件为 YAML 子集，支持两列格式与单行简写。

### 3.4 第四道防线：权限模式（`modes.py`）
- 四种模式覆盖从完全不信任到完全信任的光谱（决策矩阵对应理论篇表格）：
  - `default`：只读 Allow，写文件 Ask，Bash Ask
  - `acceptEdits`：只读 Allow，写文件 Allow，Bash Ask
  - `plan`：与 default 矩阵一致（只读约束由 System Prompt + 拦截位保证）
  - `bypassPermissions`（别名 `yolo`）：全部 Allow，但黑名单与沙箱仍生效
- 模式支持运行时切换（`/permissions` 命令）。

### 3.5 第五道防线：HITL 人在回路（`hitl.py`、`PermissionRequestEvent`）
- 前四层无法决策（ASK）时，Agent 发送 `PermissionRequestEvent` 到事件流，阻塞等待 `response_future`。
- UI 层渲染确认对话框，用户回复三种决策：ALLOW / DENY / ALLOW_ALWAYS。
- **始终允许**：自动生成 allow 规则追加到本地配置文件（`permissions.local.yaml`），下次同类操作自动放行。

### 3.6 权限检查器（`checker.py`）
- 串联五层防线输出最终决策：`check(tool_name, tool_input) -> (decision, reason)`。
- 决策：ALLOW / DENY / ASK。ASK 由调用方（Agent Loop / TUI）触发 HITL。

### 3.7 嵌入 Agent Loop（`agent.py`）
- 权限检查发生在工具执行之前；DENY 与 HITL 拒绝产生 `is_error=True` 的工具结果回传模型，循环继续。
- 权限拒绝不计入连续工具失败计数（`tool_error_turns`），模型保有调整策略的窗口。
- 规划模式拦截位（`intercept_write`）优先于权限检查；plan 文件写入始终放行。

### 3.8 UI 层集成（`app.py` / `tui.py`）
- 终端版：`_prompt_permission` 渲染确认对话框，支持 `y/n/a` 与 `1/2/3` 输入。
- TUI 版：`PermissionModal` 弹窗，支持按键与按钮。
- `/permissions` 命令：无参数查看当前模式与规则统计；带参数切换模式。

## 4. 非功能要求

1. **测试完整性**：五层防线与 Agent 集成、App/TUI 层均有专项测试；原有全部测试保持 100% 通过（回归无破坏）。
2. **跨平台兼容**：路径沙箱在 Windows 大小写不敏感（normcase）；黑名单正则跨平台一致。
3. **架构解耦**：权限模块与 Agent Loop 通过事件解耦；测试可纯离线运行。
4. **性能开销**：权限检查为轻量正则/路径/glob 匹配，单次 < 1ms。
5. **向后兼容**：`permission=None` 时 Agent 行为与第五章完全一致。

## 5. 完成定义

1. `permission/` 包五层模块实现完毕，单测覆盖各层决策。
2. Agent Loop 嵌入权限检查：DENY 返回错误结果、ASK 触发 HITL、拒绝不终止循环。
3. App 与 TUI 均支持 HITL 确认对话框与 `/permissions` 命令。
4. 「始终允许」自动追加规则并持久化。
5. 黑名单与沙箱在 bypassPermissions 模式下仍然生效。
6. 执行 `pytest` 全部测试通过，无回归。
