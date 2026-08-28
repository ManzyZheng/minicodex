# 四轮连续会话 Demo

先从仓库根目录以 AUTO-ACT 执行权限启动：

```powershell
minicodex-web --workspace .\demo\buggy_expense_tracker --mode auto-act
```

在同一个页面依次发送下面四条 Prompt。不要重启服务；这样可以展示同一个 Agent Session 保留历史对话、已读文件集合和工作区修改状态。Composer 中的 AUTO-ACT 是持续执行权限；Agent 判断需要先分析时会临时进入只读 PLAN，完成后点击“执行方案”，仍按 AUTO-ACT 继续。

## 第 1 轮：定位并修复真实 Bug

```text
先只读分析两个失败用例，给出最小修复方案，不要修改文件。方案确认后再实现并运行测试验证，不要修改现有测试。
```

预期看点：Agent 自主进入 PLAN 后只暴露读取工具；计划卡片提示“批准后使用 AUTO-ACT”，点击“执行方案”才恢复写权限。完成后最终回答下方出现文件卡片，点击在右侧查看累计 Diff，并得到 `2 passed` 与 `VERIFIED`。

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

- 0:00–0:15：展示本机 Workspace、Composer 中的 AUTO-ACT 与模型选择器。
- 0:15–0:55：完整展示第 1 轮；Agent 自主进入 PLAN、用户批准、右侧累计 Diff 和自动验证是核心镜头。
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
