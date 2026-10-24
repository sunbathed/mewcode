---
name: test
description: 运行测试并分析结果，区分代码 bug 与测试本身的问题
allowedTools:
  - Bash
  - ReadFile
  - Grep
  - Glob
mode: inline
---

# 任务

运行当前项目的测试并分析结果。

## 步骤

1. **检测项目类型**：查看项目配置文件（pyproject.toml / go.mod / package.json / pom.xml 等），决定测试命令：
   - Python：`python -m pytest`
   - Go：`go test ./...`
   - Node：`npm test`
2. **运行测试**：执行对应测试命令
3. **分析失败**：如果测试失败，区分两种失败原因：
   - **代码 bug**：断言期望与实际行为不一致，需要修源码
   - **测试本身的问题**：测试用例写错、断言错误、测试环境问题，需要改测试
   通过对比断言期望、实际输出，并翻看相关代码上下文来判断。

## 输出

- 测试通过的用例数与失败数
- 每个失败的用例：失败原因分类（代码 bug / 测试问题）+ 修复方向
- 若全部通过：报告覆盖率（如果可用），并指出可能缺少的测试场景
  （边界值、错误路径等）——全绿不等于测试充分。

$ARGUMENTS