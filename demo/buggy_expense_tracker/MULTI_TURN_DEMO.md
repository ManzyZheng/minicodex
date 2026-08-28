# 四轮连续会话 Demo

先从仓库根目录启动只读计划模式：

```powershell
minicodex-web --workspace .\demo\buggy_expense_tracker --mode plan
```

在同一个页面依次发送下面四条 Prompt。不要重启服务；这样可以直接展示同一个 Agent Session 保留了历史对话、已读文件集合和工作区修改状态。第 1 轮先在 PLAN 中完成只读定位，再点击最终结果下方的“在 AUTO-ACT 中实施”，展示同一 Session 从计划切到受控自动执行。

## 第 1 轮：定位并修复真实 Bug

```text
先运行测试，定位两个失败用例的原因，用最小改动修复 Bug，然后重新运行测试验证。请不要修改现有测试。
```

预期看点：PLAN 只暴露读取工具；批准后 AUTO-ACT 自动应用普通工作区 Diff、自动运行识别出的 pytest，最后得到 `2 passed` 和 `VERIFIED`。若 Agent 提议未知命令，仍会弹出审批。

## 第 2 轮：添加新功能

```text
在当前实现上新增 monthly_totals(expenses) 函数：按日期中的 YYYY-MM 汇总金额，返回按月份升序排列的普通 dict，金额保留两位小数。请先阅读相关代码，添加覆盖多个自然月和退款的测试，并运行测试验证。
```

预期看点：Agent 记得上一轮已修复的退款语义，在同一工作区添加函数和测试，页面连续追加新的 Diff 与测试输出。

## 第 3 轮：修改旧功能并保持兼容

```text
修改已有 category_totals：增加可选参数 aliases: dict[str, str] | None = None。分类名仍先 strip + casefold；若规范化后的分类存在于 aliases，就归入别名对应的规范化分类。旧调用方式必须保持不变。添加向后兼容和别名合并测试并验证。
```

预期看点：对旧 API 做兼容演进，而不是只会新建文件；Diff 中能看到函数签名、规范化顺序与新测试。

## 第 4 轮：回归检查与交付总结

```text
不要再增加功能。检查当前 git diff，并用一次 run_command 批量调用依次运行完整测试和 sample.csv 的 CLI 冒烟测试，要求前一步失败就停止；如果失败就修复。最后按“修复 / 新功能 / 兼容修改 / 验证证据”四项简要总结。
```

预期看点：一次 Tool Call 中出现两个独立 argv 步骤，每步都有输出和 exit code；Agent 使用现有上下文完成回归，最终页面给出完整改动链和验证证据，终端仍同步保留工具输出。

## 两分钟录屏建议

- 0:00–0:15：展示页面顶栏中的本机 Workspace、模型、PLAN 和 IDLE 状态。
- 0:15–0:55：完整展示第 1 轮；只读计划、切换 AUTO-ACT、Diff、自动验证是核心镜头。
- 0:55–1:30：第 2、3 轮可在模型等待处加速，只保留 Prompt、关键 Diff 和测试通过画面。
- 1:30–1:50：发送第 4 轮，展示完整回归与 Agent 的四项总结。
- 1:50–2:00：快速切到终端输出和 `.minicodex/sessions/*.jsonl`，说明网页、终端、Trace 是同一次执行的三个视角。

录制前用 `git status --short demo/buggy_expense_tracker` 确认 fixture 没有被上一次演示修改；初始测试应为 `2 failed`。

## 重置 Demo

完成演示后，在 MiniCodex 仓库根目录执行下面一条命令，即可把 Demo 中所有已被 Git 跟踪的源码、测试、样例和说明恢复到当前提交的初始状态：

```powershell
git restore --source=HEAD --worktree -- demo/buggy_expense_tracker
```

该命令只重置 `demo/buggy_expense_tracker`，不会影响 MiniCodex 主程序或仓库中的其他项目，也不会删除被 Git 忽略的 `.minicodex/sessions/*.jsonl` 会话记录。它会丢弃 Demo 目录中尚未提交的已跟踪文件修改，因此应在录屏完成、确认不再需要当前修复结果后执行。

重置后可以检查初始基线：

```powershell
cd demo\buggy_expense_tracker
python -m pytest -q
```

正确的演示起点应再次显示 `2 failed`。
