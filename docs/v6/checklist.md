# MewCode 第六阶段验收清单

> 每一项必须可勾选、可观测。验收方式：`pytest` 全量 + 针对性命令，或阅读对应源码/测试断言。

## 1. 危险命令黑名单（第 1 层，`permission/dangerous.py`）

- [x] `DangerousCommandDetector.match("rm -rf /")` 返回「递归强制删除根目录」。
- [x] `rm -fr /`、`rm -rfv /` 等变体同样被拦截。
- [x] `curl ... | bash`、`wget ... | sh` 返回「管道执行远程脚本」。
- [x] `mkfs.`、`dd ... of=/dev/`、`chmod -R 777 /`、`> /dev/sd` 均被拦截。
- [x] 安全命令（`ls -la`、`git commit -m 'fix'`、`rm -rf build/`、`curl --version`）不被误伤。
- [x] 支持自定义模式注入（`DangerousCommandDetector([(regex, reason)])`）。

## 2. 路径沙箱（第 2 层，`permission/sandbox.py`）

- [x] 项目目录内文件放行（绝对/相对路径均可）。
- [x] 沙箱外绝对路径（如 `tmp_path.parent / "secret" / "id_rsa"`）被拒绝，提示「超出沙箱范围」。
- [x] `.env`、`/etc/passwd` 等路径被拒绝。
- [x] 系统临时目录默认放行；`allow_temp=False` 时拒绝。
- [x] `extra_dirs` 扩展白名单生效。
- [x] 符号链接逃逸被拦截：项目内 `link.txt -> /etc/passwd` 读取被拒。
- [x] 新建文件（文件不存在）通过父目录真实路径校验，沙箱内创建放行。
- [x] 空路径被拒绝。

## 3. 权限规则引擎（第 3 层，`permission/rules.py`）

- [x] `ToolName(pattern)` 解析正确：`Bash(git *)` → tool=Bash, pattern=`git *`。
- [x] YAML 两列格式（`- rule:` + `effect:`）与单行简写（`- Bash(git *): allow`）均可解析。
- [x] 注释行与空行被忽略；无括号的非法规则被过滤。
- [x] glob 匹配正确：`ReadFile(*.env*)` 命中 `.env`、`.env.local`，不命中 `main.py`、其他工具。
- [x] 无规则文件时 `evaluate` 返回 unknown，不报错。
- [x] allow 规则放行（如 `Bash(git *)` 放行 `git commit -m 'x'`）。
- [x] deny 规则拒绝（如 `ReadFile(*.env*)` 拒绝读 `.env`）。
- [x] **deny 跨层合并不可翻转**：本地级 allow `Bash(rm *)` + 用户级 deny `Bash(rm *)` → 最终 DENY。
- [x] 本地级（最高）规则优先于项目级/用户级；用户级 deny 压过项目级 allow。
- [x] 同层内后定义规则优先：`git push --force*` deny 覆盖前面 `git *` allow。
- [x] `append_local_rule` 追加规则到 `permissions.local.yaml` 并立即生效。
- [x] 规则文件路径符合三层约定：本地 `.mewcode/permissions.local.yaml`、项目 `.mewcode/permissions.yaml`、用户 `~/.mewcode/permissions.yaml`。

## 4. 权限模式（第 4 层，`permission/modes.py`）

- [x] `default`：read→allow、write→ask、command→ask。
- [x] `acceptEdits`：read→allow、write→allow、command→ask。
- [x] `plan`：矩阵与 default 一致（read→allow、write→ask、command→ask）。
- [x] `bypassPermissions`：read/write/command 全部 allow。
- [x] 别名解析：`yolo`、`bypass`、`bypassPermissions` → BYPASS_PERMISSIONS；未知值 → default。

## 5. HITL 人与回路（第 5 层，`permission/hitl.py`）

- [x] `PermissionResponse` 三值：ALLOW / DENY / ALLOW_ALWAYS。
- [x] `build_permission_description` 优先展示 command/path/pattern，超长内容截断。
- [x] `build_allow_rule_text("Bash", {"command": "git commit -m 'fix'"})` == `Bash(git commit *)`（取前两词）。
- [x] 文件工具用 path 作为精确匹配模式（`WriteFile(hello.txt)`）。

## 6. 权限检查器（`permission/checker.py`）

- [x] 第 1 层优先：bypassPermissions 模式下 `rm -rf /` 仍 DENY。
- [x] 第 2 层优先：bypassPermissions 模式下读 `/etc/passwd` 仍 DENY。
- [x] default：ReadFile/Glob 自动放行（无弹窗）；WriteFile/Bash 返回 ASK。
- [x] acceptEdits：WriteFile/EditFile 放行、Bash ASK。
- [x] 第 3 层 deny 规则覆盖第 4 层模式（bypass 也拦 `.env`）。
- [x] 第 3 层 allow 规则覆盖第 4 层模式（default 下 `Bash(pytest *)` 放行）。
- [x] `extract_content` 字段提取正确：Bash→command、文件工具→path、Glob/Grep→pattern。
- [x] `set_mode` 运行时切换模式生效。

## 7. Agent Loop 嵌入（`engine/agent.py`、`engine/events.py`）

- [x] 危险命令被 DENY 后返回 `is_error=True` 的工具结果，Agent Loop 继续运行至 END_TURN（`LoopCompleteEvent` stop_reason=end_turn）。
- [x] bypassPermissions 下黑名单 `rm -rf /` 仍拦截。
- [x] 沙箱外读取被拦截但循环继续。
- [x] deny 规则拦截 `.env` 读取，模型仍可收尾。
- [x] ASK 触发 `PermissionRequestEvent`（kind="permission_request"），Agent 阻塞等待 future。
- [x] HITL ALLOW → 工具执行成功；HITL DENY → 工具不执行、返回「用户拒绝」错误、循环继续。
- [x] HITL ALLOW_ALWAYS → 自动追加 allow 规则到本地配置，第二条同类命令自动放行（仅 1 次弹窗）。
- [x] acceptEdits 写文件无 ASK 弹窗。
- [x] bypass 模式写文件 + 危险 Bash 混合：写放行、Bash 拦截，全程无弹窗。
- [x] `permission=None` 时行为与第五章完全一致（向后兼容）。
- [x] 连续权限拒绝不触发 TOOL_ERROR_LIMIT（`max_tool_error_turns=1` 下三轮正常 END_TURN）。
- [x] 规划模式：plan 文件写入放行、其他写拦截（intercept_write 优先于权限检查）。

## 8. App（终端版）集成（`app.py`、`commands.py`）

- [x] `/permissions` 无参数显示当前模式与本地/项目/用户规则条数。
- [x] `/permissions acceptEdits` 切换模式；`/permissions yolo` 走别名；重复切换提示「已是 default」。
- [x] `/help` 列出 `/permissions`。
- [x] default 模式 WriteFile 弹确认对话框（终端出现 `[WriteFile]`），输入 `y` 放行并写入文件。
- [x] default 模式输入 `n` 拒绝：文件不写入、显示 `✗`。
- [x] default 模式输入 `a`（始终允许）：文件写入且 `permissions.local.yaml` 出现 `WriteFile(hello.txt)`。
- [x] default 模式 Bash 弹确认，数字 `1` 放行。
- [x] 无效输入（如 `x`）重新询问，直到合法输入。
- [x] default 模式只读操作（ReadFile）自动放行，无弹窗无 `✗`。
- [x] bypassPermissions 模式危险命令直接拒绝、无弹窗。
- [x] acceptEdits 写操作自动放行（无弹窗）、Bash 仍弹窗。

## 9. TUI 全屏版集成（`tui.py`）

- [x] default 模式 WriteFile 弹出 `PermissionModal`（含「想要执行以下操作」），按键 `2` 放行并写入文件。
- [x] Modal 按键 `3` 拒绝：文件不写入、卡片显示 `✗`。
- [x] bypassPermissions 模式危险 Bash 无 Modal 弹出、卡片显示 `✗`。
- [x] `/permissions yolo` 在 TUI 切换模式成功。

## 10. 回归与文档

- [x] `pytest tests/test_permission_unit.py`：50 passed。
- [x] `pytest tests/test_permission_agent.py`：13 passed。
- [x] `pytest tests/test_app_permissions.py`：14 passed。
- [x] `pytest tests/test_tui_permissions.py`：4 passed。
- [x] `pytest` 全量：310 passed（含原有 229 个，无回归）。
- [x] `docs/spec.md`、`docs/tasks.md`、`docs/checklist.md` 三份文档为第六章内容（第五章归档 `docs/v5/`）。
- [x] `README.md`「当前范围」更新为第六章权限系统。

## 11. 端到端验收（实战篇验证流程映射）

- [x] 黑名单硬拦截：`rm -rf /` → 拒绝（无任何弹窗，bypass 也一样）。
- [x] 规则引擎：配置 `ReadFile(*.env*)` deny → 读取 `.env` 被拦截。
- [x] default 模式：只读自动放行；写文件/Bash 弹确认（1 放行 / 2 始终允许 / 3 拒绝）。
- [x] acceptEdits 模式：写文件/编辑自动放行；Bash 弹窗确认。
- [x] plan 模式：只读正常；写操作被拦截（计划写入 plan 文件除外）。
- [x] yolo（bypassPermissions）模式：全部直接执行无弹窗，但 `rm -rf /` 仍被硬拦截。
- [x] 「始终允许」形成权限学习循环：本地 `permissions.local.yaml` 自动生成规则，后续同类操作不再询问。