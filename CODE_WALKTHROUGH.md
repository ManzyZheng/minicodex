# MiniCodex 源码导读

这份文档回答三个问题：MiniCodex 的每个功能解决什么问题、代码在哪里、代码怎样强制执行。建议先读“一个 Prompt 如何跑完”，再按“推荐阅读顺序”打开源码。

> 一句话理解：MiniCodex 是一个单 Agent 状态机。模型只负责提出下一步，Python 代码负责限制路径、判定权限、执行工具、记录证据和终止循环。

## 1. 先看全景

```text
minicodex / minicodex-web
        │
        ├── Config：从环境变量或当前目录 .env 读取模型配置
        ├── OpenAIChatModel：把内部消息转换成 OpenAI-compatible 请求
        │
        ▼
AgentSession.run_turn()
        │  model → tool calls → tool results → model
        │
        ├── ToolRuntime：六个工具、Diff、批量命令、验证状态
        │       ├── WorkspaceGuard：所有文件路径必须留在工作区
        │       └── PermissionPolicy：PLAN / ACT / AUTO-ACT
        ├── SessionTrace：追加 JSONL 审计记录
        └── EventBus：把执行事件推到 Web 页面
                ├── SSE：服务器单向推送
                └── ApprovalGate：浏览器确认文件变化或命令
```

系统中有两类约束，答辩时要明确区分：

- **Prompt 约束**：告诉模型应该怎么做，例如“先读后改”“修改后运行测试”。模型可能不遵守。
- **代码约束**：由 Python 直接检查，例如工作区边界、敏感路径、read-before-edit 和权限策略。模型无法绕过正常工具接口。

## 2. 一个 Prompt 如何跑完

以 Web 模式输入“修复 Bug 并运行测试”为例：

1. `web_cli.main()` 创建配置、Trace、事件总线、审批门、工具运行时、模型适配器和 `AgentSession`。
2. 浏览器 `POST /api/prompts`，`WebSession.submit_prompt()` 检查当前是否空闲。
3. Web Session 启动一个后台 Worker，调用 `AgentSession.run_turn()`。
4. Agent 把 system message、历史对话、当前 Prompt 和当前模式允许的 Tool Schema 发给模型；PLAN 不暴露修改与命令工具。
5. 模型返回 `ModelReply`，其中可能有文本和多个 `ToolCall`。
6. Agent 逐个调用 `ToolRuntime.execute()`。
7. ToolRuntime 检查整批参数、工作区路径和 read-before-edit，并让 PermissionPolicy 返回 `ALLOW/ASK/DENY` 后再执行真实操作。
8. 工具无论成功失败都返回统一 `ToolResult`，Agent 用原始 `tool_call_id` 回灌模型。
9. 修改工具在写入前生成 unified diff；命令工具逐条返回 argv、权限结论、stdout、stderr、exit code 和状态。
10. 如果修改后没有验证，Agent 最多追加一次验证提醒，再让模型决定是否运行测试。
11. 模型不再调用工具时，本轮以 `COMPLETED` 结束；也可能因轮数、重复失败、中断或模型错误结束。
12. 最终文本、终止原因、轮数和验证状态写入 JSONL，并通过 SSE 推到浏览器。

对应的核心调用链：

```text
WebSession.submit_prompt
└── AgentSession.run_turn
    ├── compact_messages
    ├── OpenAIChatModel.complete
    ├── ToolRuntime.execute
    │   ├── WorkspaceGuard.resolve
    │   ├── PermissionPolicy.decide_*
    │   └── list/search/read/write/edit/run_command
    ├── serialize_tool_result
    └── AgentSession._outcome
```

## 3. VERIFIED 到底在哪里实现

### 3.1 不是模型自报

[`agent.py`](src/minicodex/agent.py) 的 `SYSTEM_PROMPT` 有两句提示：

```text
After changing code, run a relevant test, build, or lint command when possible.
Be honest in the final answer about what was verified and what was not.
```

这只会影响模型行为，不直接产生 `VERIFIED`。模型在最终回答中写“测试通过”，也不会改变程序状态。

### 3.2 状态的事实来源

事实来源在 [`tools.py`](src/minicodex/tools.py) 的 `ToolRuntime`：

```python
self.change_seq = 0
self.last_verification = None
```

- `change_seq`：当前会话中文件成功修改的序号。
- `last_verification`：最近一次验证命令的证据，包括状态、版本序号、argv、purpose、exit code、stdout 和 stderr。

`write_file()` 或 `edit_file()` 成功后都会调用 `_changed()`：

```python
def _changed(self, target: Path) -> None:
    self.read_paths.add(target)
    self.change_seq += 1
    self.last_verification = None
```

因此任何新修改都会让旧验证立即失效。

### 3.3 命令如何产生验证证据

`ToolRuntime.run_command()` 只有在以下条件同时成立时才更新验证状态：

1. 命令已经通过权限策略（自动允许或用户批准）；
2. `purpose` 是 `test`、`build` 或 `lint`；
3. 当前会话至少成功修改过一次文件。

```python
if purpose in {"test", "build", "lint"} and self.change_seq:
    self.last_verification = {
        "status": "VERIFIED" if completed.returncode == 0 else "FAILED",
        "change_seq": self.change_seq,
        **data,
    }
```

状态转移如下：

| 当前动作 | 结果 |
|---|---|
| 尚未修改文件 | `NOT_RUN` |
| 成功写入或编辑文件 | `change_seq + 1`，状态回到 `NOT_RUN` |
| 运行 `purpose=other` | 不改变验证状态 |
| test/build/lint 退出码为 0 | 当前 `change_seq` 标记为 `VERIFIED` |
| test/build/lint 退出码非 0 | 当前 `change_seq` 标记为 `FAILED` |
| 验证后再次修改 | 旧证据清空，重新变成 `NOT_RUN` |
| 命令被拒绝或超时 | 本次不产生新验证证据 |

### 3.4 Agent 如何读取状态

`AgentSession._verification_status()` 不只看 `last_verification` 是否存在，还核对证据中的 `change_seq` 是否等于当前版本：

```python
if verification and verification.get("change_seq") == self.tools.change_seq:
    return str(verification["status"])
return "NOT_RUN"
```

这样不能拿旧版本的测试结果证明新版本。

### 3.5 为什么有验证提醒

模型准备结束、但文件已经修改且状态仍是 `NOT_RUN` 时，`run_turn()` 会追加一次普通 user message：

```text
You changed files but have not verified them. If possible, run a test,
build, or lint command now; otherwise explain why verification cannot be run.
```

`verification_nudged` 保证每个 Prompt 最多提醒一次，防止提醒本身形成循环。提醒之后仍由模型决定是否调用 `run_command`；最终状态仍由代码证据决定。

### 3.6 Web 页面如何显示

数据链路是：

```text
ToolRuntime.last_verification
→ AgentSession._verification_status()
→ verification / turn_completed 事件
→ EventBus
→ SSE
→ app.js 更新 #verification-status
```

Web 快照也会在 [`web/session.py`](src/minicodex/web/session.py) 中重新计算一次，避免刷新后只依赖前端旧状态。

### 3.7 VERIFIED 的准确语义和局限

准确表述是：

> 当前文件修改序号执行过一条由 Agent 标记为 test/build/lint、通过权限策略且退出码为 0 的命令。

它不代表代码被形式化证明正确。AUTO-ACT 会识别 pytest/npm/cargo/go 等常见验证 argv，未知命令仍需人工确认；但 ACT 中获批的命令仍信任模型填写的 `purpose`，也没有衡量测试覆盖价值。未来可加入项目级验证配置、测试发现和覆盖率门槛。

对应测试：

- [`tests/test_tools.py`](tests/test_tools.py)：失败验证保留 stderr、exit code，并得到 `FAILED`。
- [`tests/test_agent.py`](tests/test_agent.py)：Mock Model 完成读、改、pytest 和最终回复，结果为 `VERIFIED`。

## 4. 功能总表

| 功能 | 主要实现 | 代码强制点 | 失败时 |
|---|---|---|---|
| 单 Agent Loop | `agent.py::AgentSession.run_turn` | 每轮一次模型请求，顺序执行 Tool Calls | 返回明确 StopReason |
| OpenAI-compatible Tool Calling | `model_adapter.py::complete` | 解析函数名、call ID 和 JSON 对象参数 | 非瞬时错误上抛给 Agent |
| 六个核心工具 | `tools.py::TOOL_SCHEMAS/ToolRuntime` | 只允许 Schema 中注册的工具 | `UNKNOWN_TOOL` |
| Workspace Boundary | `workspace.py::WorkspaceGuard.resolve` | resolve 后必须是 root 的子路径 | `WORKSPACE_VIOLATION` |
| 三种权限模式 | `permissions.py::PermissionPolicy` | 每个文件变化/命令返回 ALLOW、ASK 或 DENY | 拒绝或进入审批 |
| 敏感路径保护 | `permissions.py::is_protected_path` | `.env`、Key、`.git`、Trace 不可枚举/读写 | `PROTECTED_PATH` |
| Read-before-edit | `tools.py::_require_read` | 已存在文件必须在 `read_paths` | `READ_REQUIRED` |
| 唯一匹配编辑 | `tools.py::edit_file` | `old_text` 必须恰好出现一次 | `OLD_TEXT_NOT_FOUND` / `AMBIGUOUS_MATCH` |
| Unified Diff | `write_file/edit_file` | 修改成功后用 `difflib.unified_diff` 计算 | 随 ToolResult 回传 |
| argv 批量执行 | `tools.py::run_command` | 整批先校验；每步 `list[str]`、`shell=False`、固定 cwd | 可失败即停止 |
| 通用副作用确认 | `cli.confirm_action` / `web.ApprovalGate` | ACT 审 Diff/命令；AUTO-ACT 只询问未知风险 | 默认拒绝、超时拒绝 |
| 统一错误回灌 | `models.py::ToolResult`、`tools.py::execute` | 普通工具异常转换成结构化失败 | Agent 继续下一轮 |
| 最大轮数 | `AgentSession.max_turns_per_prompt` | while 条件，默认每 Prompt 20 | `MAX_TURNS` |
| 重复失败检测 | `agent.py::run_turn` | 同名同参连续失败 3 次 | `REPEATED_CALL` |
| Ctrl+C | `agent.py`、`cli.py` | 捕获 `KeyboardInterrupt` | `INTERRUPTED` / 退出码 130 |
| 两层上下文控制 | `context.py` | 单结果 16k；总历史约 80k | 截断或摘要旧组 |
| JSONL Trace | `session.py::SessionTrace` | 每事件追加一行 UTC JSON | Trace 不影响工具执行 |
| API Key 配置 | `config.py` | 环境变量优先，当前目录 `.env` 回退 | 缺配置直接退出 |
| 验证驱动完成 | `tools.py` + `agent.py` | change_seq 与验证证据绑定 | `NOT_RUN/FAILED` |
| Mock Model 测试 | `tests/test_agent.py::MockModel` | 固定 ModelReply 序列，无真实 API | 可重复测试状态机 |
| 连续会话 | `AgentSession.messages` + `WebSession` | 同一对象跨 Prompt 保留消息和工具状态 | 并发 Prompt 返回 409 |
| SSE 事件流 | `web/events.py` + `web/app.py` | 单调事件 ID、重放、heartbeat | 浏览器自动重连 |
| PLAN→执行 | `agent.py` + `web/session.py` | 同一 Session 切模式并追加实施 Prompt | 非空闲或非法迁移返回冲突 |
| Web 通用审批 | `web/approval.py` | Condition 等待最多 300 秒 | 拒绝、超时或关闭均返回 False |
| 本机 Web 安全 | `web/app.py`、`web_cli.py` | 127.0.0.1、随机 token、Host/Origin/CSP | HTTP 401/400/403 |
| 安全 Markdown | `web/static/markdown.js` | DOM API 与 textContent，不用 innerHTML | 非法实体保留为文本 |
| 轮次折叠 UI | `web/static/app.js` | 当前轮展开，完成后过程折叠 | 历史仍可点击查看 |

## 5. 六个工具逐个看

所有 Schema 定义在 [`tools.py`](src/minicodex/tools.py) 的 `TOOL_SCHEMAS`，会原样传给模型。实际执行统一经过 `ToolRuntime.execute()`，这里负责白名单、参数错误转换、兜底异常和耗时统计。

### 5.1 `list_files`

- 输入：`path`，默认 `.`。
- 先经过 WorkspaceGuard。
- 递归列出文件，跳过 `.git`、`.minicodex`、`.pytest_cache` 和 PermissionPolicy 判定的敏感路径。
- 每个候选路径再次 resolve，防止枚举过程中遇到逃逸符号链接。
- 返回相对工作区的 POSIX 风格路径。

### 5.2 `search_text`

- 输入：非空 `query` 和可选 `path`。
- 只读取非敏感 UTF-8 文本；二进制、非 UTF-8、不可读文件或 `.env`/私钥直接跳过。
- 返回路径、行号和整行文本。
- 当前是朴素 Python 逐文件搜索，适合小型演示项目，不是大型代码索引。

### 5.3 `read_file`

- 读取 UTF-8 全文。
- 成功后把规范化绝对路径加入 `read_paths`。
- 这个集合就是 read-before-edit 的会话状态。

### 5.4 `write_file`

- 新文件可以直接创建。
- 已存在文件必须先 `read_file`。
- 内容完全相同返回 `NO_CHANGE`。
- 写入前生成 Diff 并经过模式策略：PLAN 拒绝，ACT 询问，AUTO-ACT 对普通路径允许。
- 获准后创建父目录、写 UTF-8、增加 `change_seq`。
- 它可以整体覆盖文件，因此模型应优先使用更小的 `edit_file`。

### 5.5 `edit_file`

- 已存在文件必须先读。
- 统计 `old_text` 出现次数：0 次拒绝，多于 1 次也拒绝。
- 恰好一次才执行 literal replace，不使用正则表达式。
- 唯一替换后先生成 Diff 并审批，获准才落盘；成功后返回 Diff，并让旧验证失效。

### 5.6 `run_command`

- `commands` 必须包含 1–8 个对象；整批先校验，避免前一步已产生副作用后才发现后一步参数非法。
- 每步 `argv` 必须是非空字符串数组，`purpose` 只能是 `test/build/lint/other`，timeout 为 1–120 秒。
- 每一步独立经过 PermissionPolicy；PLAN 拒绝，ACT 询问，AUTO-ACT 自动允许识别出的验证/只读 Git 命令，未知命令询问，高风险命令拒绝。
- 逐条使用 `subprocess.run(argv, cwd=workspace, shell=False, ...)`。
- 每一步的 stdout/stderr/exit code/status 都被捕获；`stop_on_failure=true` 时失败后的命令标记为 skipped。
- 子进程环境移除 `MINICODEX_API_KEY`、`DASHSCOPE_API_KEY`、`OPENAI_API_KEY` 和 `ANTHROPIC_API_KEY`。
- 当前超时只终止直接子进程，未使用 Windows Job Object 管理完整进程树。

## 6. 安全机制与权限模式逐个看

### 6.1 Workspace Boundary

[`workspace.py`](src/minicodex/workspace.py) 先把根目录和用户路径都规范化，再用 `Path.is_relative_to()` 判断包含关系。它阻止：

- `../secret.txt`；
- 指向外部的绝对路径；
- 解析后落到外部的符号链接。

边界检查不仅用于六工具，也用于 Trace 路径，避免 `.minicodex` 被符号链接到工作区外。

### 6.2 Read-before-edit

这是“本会话读过该规范化路径”，不是内容版本锁。它能阻止模型没看文件就编辑，但不能发现人类在读取后、写入前修改了文件。项目明确暂不实现 SHA-256/mtime 版本令牌。

### 6.3 唯一匹配

精确唯一匹配让编辑意图可审计：模型必须给出足够上下文，使旧文本只命中一处。相比“替换所有”更不容易误改相似代码。

### 6.4 argv 与 `shell=False`

模型提交的是：

```json
{
  "commands": [
    {"argv":["python","-m","pytest","-q"],"purpose":"test"},
    {"argv":["python","expense_tracker.py"],"purpose":"other"}
  ],
  "stop_on_failure": true
}
```

不是：

```text
python -m pytest -q && dangerous-command
```

因为没有 shell 解析，`;`、`&&`、重定向和命令替换不会自动获得特殊语义。批量语义由 Python 循环实现，所以仍能一次 Tool Call 完成“测试成功后再冒烟”。但用户仍需检查未知 argv，因为获准的可执行文件本身可以做任意工作区内外操作；当前权限模式不是 OS 沙箱。

### 6.5 `PLAN / ACT / AUTO-ACT`

[`permissions.py`](src/minicodex/permissions.py) 把模式与风险判断集中到 `PermissionPolicy`，而不是散落在 Prompt 或 UI：

| 模式 | 文件变化 | 命令 |
|---|---|---|
| PLAN | 拒绝；模型也看不到 write/edit/run schema | 全部拒绝 |
| ACT | 先把 Diff 交给 approver | 每一步把 argv 交给 approver |
| AUTO-ACT | 普通工作区文件允许，敏感路径拒绝 | 验证/只读 Git 允许，未知询问，危险操作拒绝 |

敏感路径规则同时用于直接读写和递归枚举，避免模型通过 `list_files` 或 `search_text` 间接看到 `.env`。`.env.example` 只作为公开配置模板例外。危险命令的显式 deny 优先于模式便利性。

AUTO-ACT 的安全含义是“应用层替用户批准规则明确的操作”，不是系统账户降权。它没有容器、seccomp、Windows Job Object 或文件系统虚拟化。

### 6.6 统一 ToolResult

[`models.py`](src/minicodex/models.py) 的 `ToolResult` 同时承载成功和失败：

```text
ok / tool / call_id / summary / data / error / meta
```

`ToolRuntime.execute()` 捕获参数 `TypeError` 和普通异常，转换为 `INVALID_ARGUMENT` 或 `TOOL_INTERNAL_ERROR`。所以“工具失败”是 Agent 的可观察状态，不等于 Python 进程崩溃。

## 7. Agent Loop 逐段读

打开 [`agent.py`](src/minicodex/agent.py)，按下面顺序阅读。

### 7.1 `SYSTEM_PROMPT`

只放高层行为约束，不承担安全判定。PLAN 会追加只读提示，并且 `_tool_schemas()` 只返回 list/search/read；真正安全边界仍在 ToolRuntime、WorkspaceGuard 和 PermissionPolicy，即使模型伪造调用也会被拒绝。

### 7.2 `Model` Protocol、`ToolCall`、`ModelReply`

Agent 不依赖具体 SDK，只要求模型对象实现：

```python
complete(messages, tools) -> ModelReply
```

这让测试可以替换为 Mock Model。

### 7.3 `AgentSession.__init__`

长期状态包括：

- `messages`：system message 加所有历史对话；
- `tools`：其中包含 read 集合、修改序号和验证证据；
- `prompt_count`：连续会话的用户轮次；
- `mode`：与 ToolRuntime 同步的当前权限模式；
- Trace、终端回调和 Web 事件回调。

### 7.4 `run_turn()`

这是核心状态机：

1. 加入 user message；
2. 每轮先压缩上下文；
3. 请求模型；
4. 没有 Tool Call 时决定提醒验证或完成；
5. 有 Tool Call 时顺序执行；
6. 把 ToolResult 按 call ID 加入消息历史；
7. 检查连续失败次数；
8. 达到上限或捕获异常后生成 Outcome。

### 7.5 为什么要补齐未回答 Tool Calls

OpenAI Tool Calling 协议要求 assistant 发出的每个 tool call 都有对应 tool message。如果一轮包含多个调用，但 Agent 在中途因重复失败或中断退出，`_cancel_unanswered_tool_calls()` 会为剩余调用补 `TOOL_CALL_CANCELLED`，保证下一 Prompt 的消息历史仍合法。

### 7.6 五种终止原因

| StopReason | 触发条件 |
|---|---|
| `COMPLETED` | 模型返回无 Tool Call 的最终文本 |
| `MAX_TURNS` | 当前 Prompt 达到默认 20 个模型轮次 |
| `REPEATED_CALL` | 连续三次同名同参调用失败且没有成功进展 |
| `INTERRUPTED` | 捕获 `KeyboardInterrupt` |
| `MODEL_ERROR` | 模型或循环中出现不可恢复异常 |

`_outcome()` 统一写 Trace、发 `turn_completed` 事件并返回 `AgentOutcome`。

## 8. 模型适配与 Qwen

[`model_adapter.py`](src/minicodex/model_adapter.py) 做三件事：

1. 用 `Config` 创建 OpenAI SDK Client；
2. 发送 `messages`、`tools` 和 `tool_choice="auto"`；
3. 把 SDK 对象转换为内部 `ModelReply`。

当 `MINICODEX_ENABLE_THINKING=true` 时发送：

```python
extra_body={"enable_thinking": True, "preserve_thinking": False}
```

当前请求是非流式，适配器分别读取 `message.reasoning_content`、`message.content` 和 `message.tool_calls`，转换成内部 `ModelReply`。reasoning 通过独立 `model_reasoning` 事件进入终端、Web 执行过程和 JSONL Trace，不会拼接进最终 `content`。因此最终答案仍保持干净；任务结束后，前端会把 thinking 连同工具、Diff 和测试输出一起折叠。

这里显式关闭 `preserve_thinking`：当前轮仍会思考并返回 reasoning，但后续请求不需要完整回传历史 reasoning。这样与 MiniCodex 的确定性历史压缩兼容，也避免思考内容快速增加上下文成本。如果未来开启 preserved thinking，就必须完整、原样、按顺序保存并回传供应商特有字段，不能把它拼到 `content`。

瞬时错误包括 429、5xx、连接错误和超时，最多尝试三次，等待约 0.5 秒、1 秒。解析错误等非瞬时错误不会盲目重试。

## 9. 两层上下文控制

[`context.py`](src/minicodex/context.py) 有两层限制。

### 第一层：单个工具结果

`serialize_tool_result()` 默认最多 16,000 字符。超长时仍保持合法 JSON：

- `meta.truncated=true`；
- summary 和 error message 单独缩短；
- data 变成带头尾内容的 preview；
- 约保留 70% 头部和 30% 尾部。

### 第二层：完整历史

`compact_messages()` 默认阈值约 80,000 字符。超过时压缩较老的一半消息，保留：

- 用户任务；
- assistant 文本；
- 工具名和参数；
- ToolResult 和错误事实。

它会向后移动切点，避免新历史以孤立的 tool message 开始。摘要是确定性文本拼接，不调用另一个模型，因此便宜、可测试，但语义保真度有限。

## 10. JSONL Session Trace

[`session.py`](src/minicodex/session.py) 每次写入一行：

```json
{"timestamp":"...Z","event":"tool_result","payload":{...}}
```

主要事件：

- `session_start`；
- `prompt_start`；
- `model_reply`；
- `tool_result`；
- `model_error`；
- `final`。

`model_reply` 会分别记录 `reasoning_content` 和 `content`。Trace 因此可能包含完整思考、源码和 Prompt，只适合本地复盘，不应提交到 Git 或直接公开分享。

逐行 JSON 的优点是进程中途退出时，之前的完整行仍可读取，也方便用脚本流式统计。Trace 可能包含源码、Prompt 和本机路径，因此 `.minicodex/` 必须保持在 `.gitignore` 中。

## 11. CLI 与 Web 是怎样组装的

### 11.1 终端入口

[`pyproject.toml`](pyproject.toml) 注册：

```toml
minicodex = "minicodex.cli:main"
```

[`cli.py`](src/minicodex/cli.py) 负责 `--mode` 参数、创建依赖、终端通用 `y/N` 审批、打印批量 ToolResult 和根据 StopReason 返回进程退出码。文件审批显示待应用 Diff，命令审批显示当前批次中的单步 argv。`Agent` 只是 `AgentSession` 的单次运行兼容包装。

### 11.2 Web 入口

```toml
minicodex-web = "minicodex.web_cli:main"
```

[`web_cli.py`](src/minicodex/web_cli.py) 固定绑定 `127.0.0.1`，生成随机 URL token，并组装：

```text
EventBus
ApprovalGate（文件变化和命令）
ToolRuntime
OpenAIChatModel
AgentSession
WebSession
FastAPI app
```

终端输出仍通过 `on_tool_result=print_tool_result` 保留；同一个结果还通过 `on_event=events.publish` 进入前端。

## 12. Web 连续会话、SSE 与审批

### 12.1 `WebSession`

[`web/session.py`](src/minicodex/web/session.py) 是并发边界：

- 状态不是 `IDLE` 时拒绝新 Prompt；
- 每个 Prompt 使用一个后台线程；
- 线程执行完成后回到 `IDLE`；
- 同一个 `AgentSession`、messages、ToolRuntime 和 Workspace 一直复用。
- 只允许在 IDLE 时切换模式；PLAN 结果可以在同一 Session 中批准到 ACT 或 AUTO-ACT 执行。

所以“多轮”不是多个 Agent，而是同一个 Agent Session 连续接收多个用户指令。

### 12.2 `EventBus`

[`web/events.py`](src/minicodex/web/events.py) 给每个事件递增 ID，保留最近 2,000 个事件，单条 JSON 约最多 32,000 字符。订阅者队列满时丢掉队列中最旧项；断线后可根据事件 ID 从保留窗口重放。

### 12.3 SSE

[`web/app.py`](src/minicodex/web/app.py) 把事件编码为：

```text
id: 17
event: diff
data: {"path":"app.py","diff":"..."}
```

SSE 只负责服务器到浏览器；Prompt 和审批决定仍用 HTTP POST。15 秒无事件时发送 heartbeat。浏览器自动带 `Last-Event-ID` 重连，服务端从下一个事件继续。

### 12.4 `ApprovalGate`

[`web/approval.py`](src/minicodex/web/approval.py) 把 ToolRuntime 同步需要的通用 bool 回调桥接为异步页面操作：

1. 创建唯一 request ID；
2. 发布 `approval_required`；
3. Worker 在 `threading.Condition` 上等待；
4. 浏览器 POST allow/reject；
5. `resolve()` 唤醒 Worker；
6. 300 秒超时默认拒绝。

`ApprovalPrompt.kind` 区分 `command` 和 `file_change`；payload 同时带 summary、reason、risk、rule ID，文件变化还带 diff，命令带 argv/purpose。任一时刻只允许一个 pending approval。

### 12.5 本机 HTTP 防护

FastAPI 中间件要求：

- Host 是 `127.0.0.1` 或 `localhost`；
- 跨站 Origin 被拒绝；
- 所有 `/api/*` 必须携带随机 token；
- 返回 CSP、no-referrer 和 nosniff。

这是本机 Web 控制台防护，不等于系统级沙箱。掌握 token 的本机进程或扩展仍可操作会话。

## 13. 前端代码怎么读

前端没有框架，位于 [`web/static/`](src/minicodex/web/static)。

- `index.html`：顶部 Session 状态、Workspace、执行区域、Prompt 输入框和审批 Dialog。
- `app.css`：终端式视觉、固定顶部栏、轮次折叠、Diff 和 Markdown 样式。
- `markdown.js`：小型安全 Markdown 渲染器。
- `app.js`：HTTP、EventSource、事件分组、审批和 DOM 更新。

`app.js` 的阅读顺序：

1. `setStatus()/setModeUI()`：控制输入框、当前模式和 PLAN 只读状态；
2. `beginTurn()`：新 Prompt 创建轮次组并折叠上一轮；
3. `addCard()/addCompactLine()`：渲染 Diff、命令、错误和简洁工具行；
4. `completeTurn()`：折叠执行过程，只展开最终 Markdown；
5. `handlers`：把 `model_reasoning` 渲染为 `THINKING · TURN N`，并映射其他 SSE 事件；
6. `loadSnapshot()`：刷新时恢复模式、状态和 pending approval；
7. `connectEvents()`：建立 SSE；
8. `submitPrompt()/decideApproval()/changeMode()/approvePlan()`：浏览器到服务器的 POST 方向。

Markdown 渲染只用 `createElement`、`createTextNode`、`textContent` 和 `replaceChildren`，不把模型输出交给 `innerHTML`。因此 `<script>` 只会显示成文本。

## 14. 配置和密钥

[`config.py`](src/minicodex/config.py) 的优先级是：

```text
进程环境变量 > 当前运行目录的 .env
```

支持：

- `MINICODEX_API_KEY`；
- `DASHSCOPE_API_KEY`；
- `MINICODEX_MODEL`；
- `MINICODEX_BASE_URL`；
- `MINICODEX_ENABLE_THINKING`。

`api_key` 字段设置 `repr=False`，避免打印 Config 时直接泄漏。CLI 没有 `--api-key` 参数，Trace 也不会主动记录 Key。不过 `.env` 是便利性回退，并非硬件密钥库；需要依赖 `.gitignore` 和本机文件权限。

## 15. 测试如何证明这些功能

| 测试文件 | 主要证明内容 |
|---|---|
| `test_core.py` | 配置、ToolResult、WorkspaceGuard、Trace |
| `test_tools.py` | 六工具、边界逃逸、read-before-edit、唯一匹配、命令确认、FAILED 证据 |
| `test_permissions.py` | PLAN/ACT/AUTO-ACT、敏感路径、验证命令和危险命令分类 |
| `test_agent.py` | Mock Model Loop、错误回灌、VERIFIED、reasoning 事件与 Trace、终止条件、连续会话 |
| `test_model_adapter.py` | Tool Call 与 reasoning 解析、Qwen thinking/preserve 参数、瞬时错误重试 |
| `test_cli.py` | 参数校验、Ctrl+C、退出码和有限长 thinking 输出 |
| `test_web_events.py` | 事件 ID、保留窗口、订阅和重放 |
| `test_web_approval.py` | allow/reject/timeout/close |
| `test_web_session.py` | 单 Worker、连续 Prompt、模式切换、PLAN→执行和状态快照 |
| `test_web_api.py` | token、Host、Origin、Prompt、模式、计划批准和 SSE API |
| `test_web_static.py` | 静态资源与禁止 innerHTML |
| `test_frontend_timeline.py` | 当前轮 thinking、历史折叠、最终答案隔离、最终 TURN、刷新重建 |
| `test_markdown_renderer.py` | Markdown、安全文本和异常输入不死循环 |

Mock Model 的核心思想是预先给出固定 `ModelReply` 列表：第一次故意违反 read-before-edit，第二次读取，第三次编辑，之后运行 pytest。测试不仅断言最终文件，还检查模型下一轮是否真的收到 `READ_REQUIRED` ToolResult。

## 16. 推荐阅读顺序

第一次读不要从前端开始。按以下顺序最容易形成完整模型：

1. [`models.py`](src/minicodex/models.py)：先认识数据结构。
2. [`workspace.py`](src/minicodex/workspace.py)：理解硬边界。
3. [`permissions.py`](src/minicodex/permissions.py)：看三种模式如何输出 allow/ask/deny。
4. [`tools.py`](src/minicodex/tools.py)：看六工具、批量 argv 和 VERIFIED。
5. [`agent.py`](src/minicodex/agent.py)：看状态机怎样按模式暴露和调用工具。
6. [`model_adapter.py`](src/minicodex/model_adapter.py)：看模型协议怎样接入。
7. [`context.py`](src/minicodex/context.py)：看上下文为什么不会无限增长。
8. [`session.py`](src/minicodex/session.py)：看审计记录。
9. [`cli.py`](src/minicodex/cli.py)：看单轮组装。
10. `web/events.py` → `web/approval.py` → `web/session.py` → `web/app.py`：看 Web 编排。
11. `web/static/app.js`：最后看展示层如何消费事件。
12. 对照 `tests/`，确认每项约束是否真的由测试保护。

## 17. 老师可能追问

### `VERIFIED` 是否保证程序正确？

不保证。它证明当前修改版本有一条获批验证命令成功退出。测试是否充分仍由测试内容决定。

### 为什么既有 Prompt 约束又有代码约束？

Prompt 适合表达策略，代码适合强制安全边界。只写 Prompt 无法可靠阻止路径逃逸或未授权命令。

### 为什么不用 shell 命令字符串？

argv 加 `shell=False` 消除 shell 元字符解析，行为更可预测，也更容易逐条展示和审批。需要 `&&` 式工作流时由 `commands[] + stop_on_failure` 表达，不需要引入 shell parser。

### AUTO-ACT 是否等于“完全信任”？

不是。普通工作区编辑与规则明确的验证/只读命令自动允许，未知操作仍询问，显式危险操作和敏感路径仍拒绝。它也不是 OS 沙箱，获准程序仍继承当前系统用户权限。

### Read-before-edit 是否防止并发覆盖？

不能。它只证明会话读过路径。要防止读取后被外部修改，需要 SHA-256、mtime 或文件版本令牌。

### 为什么编辑必须唯一匹配？

为了把模糊意图变成确定操作；如果匹配多处，让模型补充上下文比猜测哪一处更安全。

### 为什么用 SSE 而不是 WebSocket？

主要实时方向是服务器到浏览器；用户 Prompt 和审批可用普通 POST。SSE 自带事件 ID 和自动重连，代码更少。

### 为什么 Web 只允许一个并发 Prompt？

同一个 Workspace 并发编辑会导致读状态、Diff、验证版本和文件内容互相覆盖。初版用单 Worker换取可解释性。

### 当前最值得优先补的安全点是什么？

1. 用 SHA-256/mtime 保护 read-before-edit 的内容版本；
2. 用 Windows Job Object 或进程组终止完整命令树；
3. 把 AUTO-ACT 规则变成可审计的项目级配置；
4. 为获准子进程增加真正的系统级隔离或低权限账户。

## 18. 用一句话向老师总结每层

- **模型层**：OpenAI-compatible 模型负责规划下一步。
- **Agent 层**：单循环负责消息、Tool Calls、终止和验证提醒。
- **工具层**：六工具把模型意图变成确定操作和结构化结果。
- **安全层**：Workspace、敏感路径、先读后改、唯一匹配、三模式策略和 argv 限制副作用。
- **证据层**：Diff、stdout/stderr、change_seq、VERIFIED 和 JSONL 让结果可追踪。
- **Web 层**：同一 Session 通过 SSE 实时展示，并用 HTTP 完成 Prompt 和审批。
