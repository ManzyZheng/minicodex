MiniCodex 是一个用于项目考核的简化版 Claude Code/Codex。它采用单 Agent 和 OpenAI-compatible Tool Calling，提供 list_files、search_text、read_file、write_file、edit_file、run_command 六个工具。

核心工程约束：所有文件操作限制在指定 Workspace；已有文件必须先读后改；edit_file 的旧文本必须唯一匹配并返回 unified diff；命令使用 argv 和 shell=False，执行前要求 y/N 确认；工具统一返回 ToolResult，错误会回灌给模型而不会使 Agent 崩溃；支持最大轮数、三次重复失败调用检测和 Ctrl+C；使用工具输出截断与历史压缩两层上下文控制；会话写入 JSONL Trace；Key 只从 MINICODEX_API_KEY 读取；代码修改后以 test/build/lint 结果标记 VERIFIED、FAILED 或 NOT_RUN。

安装：python -m pip install -e ".[dev]"
配置：MINICODEX_API_KEY、MINICODEX_MODEL，可选 MINICODEX_BASE_URL。
运行：minicodex "修复测试失败并重新验证" --workspace demo/buggy_expense_tracker
测试：python -m pytest -q --basetemp=.pytest-tmp

演示项目预置退款计算和分类归一化两个 Bug。两分钟视频可依次展示：初始 2 failed、Agent 读文件、批准测试命令、精确编辑与 Diff、复跑得到 2 passed 和 VERIFIED、最后查看 JSONL Trace。初版暂不做 SHA-256 文件版本保护、多 Agent、MCP 和 IDE UI。
