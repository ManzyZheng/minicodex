# MiniCodex

一个用于项目考核与教学演示的简化版 Claude Code / Codex：单 Agent、OpenAI-compatible Tool Calling、六个核心工具，并把文件边界、修改约束、权限确认、循环保护、可追踪性和验证驱动完成做成可测试的工程能力。

它不是“套一个模型 API 的聊天脚本”。核心展示点是：模型可以自主查看代码、调用工具、接收结构化错误、修复真实 Bug，并用测试结果证明完成；同时宿主程序始终控制文件边界和命令执行权限。

## 功能

- 单 Agent 的 `model → tool calls → tool results → model` 循环。
- 同一浏览器页面中的连续多轮会话：复用消息历史、已读文件集合、工作区修改状态和验证状态。
- 本机 Web Console：通过 SSE 实时展示模型回复、工具调用、命令输出、彩色 Diff 与命令审批；终端同步保留工具输出。
- OpenAI-compatible Chat Completions Tool Calling；支持自定义 `base_url`。
- 六个工具：`list_files`、`search_text`、`read_file`、`write_file`、`edit_file`、`run_command`。
- Workspace Boundary：所有路径 resolve 后必须仍位于指定项目目录，阻止 `..`、绝对路径和符号链接逃逸。
- Read-before-edit：已有文件必须先读再写；新文件可以直接创建。
- 唯一匹配编辑：`old_text` 必须恰好出现一次，否则返回 `OLD_TEXT_NOT_FOUND` 或 `AMBIGUOUS_MATCH`。
- 修改返回 unified diff，方便模型和用户检查实际变化。
- 命令以 `argv: list[str]`、`shell=False` 执行；每次运行前默认 `y/N` 人工确认。
- 统一 `ToolResult`：工具错误被结构化回灌给模型，不会让 Agent 因一次工具失败而崩溃。
- 循环保护：最大模型轮数、连续三次相同失败调用检测、`Ctrl+C` 中断。
- 两层上下文控制：单次工具输出头尾截断，以及按完整消息组压缩旧历史，避免拆散 tool call / result。
- JSONL Session Trace：保存模型回复、工具调用结果、终止原因与验证状态，便于复盘。
- 验证驱动完成：发生修改后，`test/build/lint` 命令成功才标记 `VERIFIED`；失败为 `FAILED`，未运行是 `NOT_RUN`。
- Mock Model 单元测试，不依赖真实 API 即可验证 Agent 状态机。

当前初版按考核范围暂不实现 SHA-256/mtime 文件版本保护；read-before-edit 记录的是本次会话中已读取的规范化路径。

## 架构

```text
CLI 或本机 Web 页面
      │
      ▼
Agent Loop ──────── JSONL Session Trace
  │     ▲
  │     └── ModelReply / ToolResult / 错误回灌
  ▼
OpenAI-compatible Model Adapter
  │
  ▼ tool_calls
ToolRuntime ── WorkspaceGuard
  ├── list / search / read
  ├── write / exact edit / diff
  └── argv command ── y/N approval ── verification state

WebSession ── EventBus ── SSE ── Browser Timeline
     └────── ApprovalGate ◀── HTTP approval response
```

主要模块：

- `agent.py`：Agent 状态机、终止条件、验证提醒和最终状态。
- `tools.py`：六工具、统一异常转换、read-before-edit、Diff 与命令执行。
- `workspace.py`：项目目录边界和规范化路径。
- `model_adapter.py`：OpenAI-compatible 请求、tool call 解析及瞬时错误重试。
- `context.py`：工具输出截断和历史压缩。
- `session.py`：可逐行读取、可回放的 JSONL Trace。
- `web/session.py`：一个 Worker 串行执行多个 Prompt，保证同一时间只有一轮任务在修改工作区。
- `web/events.py`：带递增 ID 的内存事件总线，支持 SSE 断线后通过 `Last-Event-ID` 补发事件。
- `web/approval.py`：把阻塞式命令确认桥接为浏览器审批，超时默认拒绝。
- `web/app.py` 与 `web/static/`：FastAPI API、SSE 流和无框架前端。

## 项目目录与文件职责

```text
minicodex/
├── pyproject.toml                 # 包信息、依赖、CLI 入口和 pytest 配置
├── .env.example                  # Qwen/百炼配置模板；不包含真实 Key
├── .gitignore                    # 排除 Key、虚拟环境、Trace、缓存和构建产物
├── README.md                     # 完整项目说明
├── README.txt                    # 适合考核材料提交的千字内摘要
├── src/minicodex/
│   ├── __init__.py               # 包版本
│   ├── __main__.py               # python -m minicodex 入口
│   ├── cli.py                    # 参数解析、权限确认、终端输出和退出码
│   ├── web_cli.py                # 仅绑定 127.0.0.1 的 Web 服务入口
│   ├── config.py                 # 环境变量/.env 读取与配置校验
│   ├── models.py                 # ToolCall、ModelReply、ToolResult 等数据模型
│   ├── model_adapter.py          # OpenAI-compatible 模型适配与重试
│   ├── agent.py                  # 单 Agent 主循环与终止/验证状态
│   ├── tools.py                  # 六工具、Diff、命令执行和 read-before-edit
│   ├── workspace.py              # Workspace Boundary 与路径规范化
│   ├── context.py                # 工具输出截断和历史摘要
│   └── session.py                # JSONL Session Trace
│   └── web/
│       ├── app.py                # Prompt/审批 API、SSE 与静态文件路由
│       ├── session.py            # 连续会话与单 Worker 编排
│       ├── events.py             # 可重放内存事件总线
│       ├── approval.py           # 浏览器命令审批门
│       └── static/               # 原生 HTML/CSS/JS 执行时间线
├── tests/
│   ├── test_core.py              # 配置、ToolResult、工作区和 Trace 测试
│   ├── test_tools.py             # 六工具、安全边界与命令隔离测试
│   ├── test_agent.py             # Mock Model、循环终止与上下文测试
│   ├── test_model_adapter.py     # Tool Calling 解析、重试和 Qwen 参数测试
│   └── test_cli.py               # CLI 参数与 Ctrl+C 行为测试
├── demo/buggy_expense_tracker/
│   ├── TASK.md                   # 推荐直接交给 Agent 的演示任务
│   ├── MULTI_TURN_DEMO.md        # 修 Bug→加功能→改旧功能→回归的四轮脚本
│   ├── expense_tracker.py        # 带两个预置 Bug 的小型项目
│   ├── sample.csv                # CLI 冒烟输入
│   └── tests/test_expense_tracker.py
└── docs/baselines/
    ├── v0.1.0-qwen3.8-flash-demo.md
    └── v0.1.0-qwen3.8-flash-demo.json
```

### `config.py`：配置与密钥

`Config.from_env()` 显式读取当前运行目录的 `.env`，不会搜索父目录，也不会把文件内容写回全局环境。配置优先级为：系统环境变量优先，本地 `.env` 作为回退。API Key 使用 `repr=False`，避免打印配置对象时意外暴露凭据。

支持的配置项：

| 变量 | 必需 | 说明 |
|---|---:|---|
| `MINICODEX_API_KEY` | 二选一 | 通用 OpenAI-compatible Key，优先级最高 |
| `DASHSCOPE_API_KEY` | 二选一 | 阿里云百炼 Key |
| `MINICODEX_MODEL` | 是 | 模型名称，如 `qwen3.8-flash` |
| `MINICODEX_BASE_URL` | 否 | OpenAI-compatible `/v1` 端点 |
| `MINICODEX_ENABLE_THINKING` | 否 | `true/false`，控制 Qwen thinking 参数 |

### `models.py`：统一消息模型

- `ToolCall`：模型给出的调用 ID、工具名和参数对象。
- `ModelReply`：一次模型回复的文本与零到多个 Tool Calls。
- `ToolError`：稳定错误码、说明和是否可重试。
- `ToolMeta`：耗时、是否截断及可选 artifact 路径。
- `ToolResult`：所有工具共用的成功/失败信封，保证错误也能作为普通消息回灌给模型。

典型失败结果：

```json
{
  "ok": false,
  "tool": "edit_file",
  "call_id": "call-123",
  "summary": "old_text matched 2 times",
  "data": null,
  "error": {
    "code": "AMBIGUOUS_MATCH",
    "message": "old_text matched 2 times",
    "retryable": false
  },
  "meta": {
    "duration_ms": 1,
    "truncated": false,
    "artifact_path": null
  }
}
```

### `model_adapter.py`：OpenAI-compatible 适配

适配器使用 Chat Completions Tool Calling 格式，把 SDK 返回的函数名、调用 ID 和 JSON 参数转换成内部 `ToolCall`。对 429、5xx、连接失败和超时最多尝试三次，并使用短指数退避。启用 Qwen thinking 时加入 `extra_body={"enable_thinking": true}`；当前采用非流式请求，确保一次取得完整 Tool Calls。

### `agent.py`：单 Agent 状态机

每个模型请求计为一轮。一次回复可以包含多个工具调用，Agent 会顺序执行并把每个 `ToolResult` 通过原始 `tool_call_id` 送回模型。循环可能以以下状态结束：

| 状态 | 含义 |
|---|---|
| `COMPLETED` | 模型给出最终文本 |
| `MAX_TURNS` | 达到最大模型轮数 |
| `REPEATED_CALL` | 连续三次相同失败调用且没有进展 |
| `INTERRUPTED` | 用户按下 Ctrl+C |
| `MODEL_ERROR` | 模型适配器发生不可恢复错误 |

文件被修改但还没有验证时，Agent 会额外提醒模型运行测试、构建或 lint；若模型仍选择结束，则最终状态诚实显示 `NOT_RUN`。

### `workspace.py`：Workspace Boundary

所有用户路径都会通过 `Path.resolve()` 规范化，并检查解析后的路径仍位于工作区根目录。该策略同时阻止：

- `../secret.txt` 等父目录逃逸；
- 指向工作区外的绝对路径；
- 文件或目录符号链接逃逸；
- Trace 路径借助 `.minicodex` 链接写出工作区。

### `context.py`：两层上下文控制

第一层限制单次工具消息：大型内容保留头部与尾部，在合法 JSON 信封中标记 `meta.truncated=true`。第二层在历史过长时按完整消息组压缩旧上下文，保留任务、路径、工具调用、错误和验证事实，不拆散 assistant tool call 与对应 tool result。

### `session.py`：JSONL Trace

每行是一个独立 JSON 事件，包含 UTC 时间、事件类型和 payload。主要事件包括 `session_start`、`model_reply`、`tool_result`、`model_error` 和 `final`。Trace 默认写入目标工作区的 `.minicodex/sessions/`，便于逐行查看和脚本分析；目录被 Git 忽略，因为记录可能包含本机路径和完整代码上下文。

## 六个工具

| 工具 | 主要参数 | 功能与约束 |
|---|---|---|
| `list_files` | `path` | 列出工作区内文件，跳过 Git、Trace 和缓存目录 |
| `search_text` | `query`, `path` | 搜索 UTF-8 文本；每个候选文件都重新进行边界检查 |
| `read_file` | `path` | 读取 UTF-8 文件，并把规范化路径记入本会话已读集合 |
| `write_file` | `path`, `content` | 新建文件或覆盖已读文件；成功后返回 unified diff |
| `edit_file` | `path`, `old_text`, `new_text` | 仅在 `old_text` 唯一匹配时替换，并返回 unified diff |
| `run_command` | `argv`, `purpose`, `timeout_sec` | `shell=False` 执行 argv；1–120 秒超时；执行前人工确认 |

`run_command` 的 `purpose` 为 `test`、`build`、`lint` 或 `other`。前三种命令作用于验证状态：最近一次相关命令退出码为 0 时是 `VERIFIED`，非 0 时是 `FAILED`；之后再次修改文件会重置为 `NOT_RUN`。获批子进程不会继承 MiniCodex、OpenAI 或 Anthropic API Key。

## 一次任务的数据流

```text
1. CLI 解析任务、工作区、模型和最大轮数
2. Config 从环境变量或当前目录 .env 加载配置
3. Agent 把 system prompt、用户任务和六工具 schema 发给模型
4. 模型返回一个或多个 Tool Calls
5. ToolRuntime 验证参数和工作区边界后执行工具
6. CLI 展示错误、命令授权请求、测试输出或 unified diff
7. Agent 将结构化 ToolResult 回灌模型
8. 重复 3–7，直到完成、中断、重复调用或达到轮数上限
9. 输出最终文本、stop reason、验证状态和验证命令证据
```

## 安装

要求 Python 3.11+。

```powershell
cd D:\Master\projects\minicodex
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

API Key 只从环境变量或本地 `.env` 读取，不提供 `--api-key` 参数，也不会写进 Trace。项目已提供面向阿里云百炼 Qwen 的 `.env.example`：

```dotenv
DASHSCOPE_API_KEY=sk-xxx
MINICODEX_MODEL=qwen3.8-flash
MINICODEX_BASE_URL=https://ws-2r6gaasmu4dyhxq0.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
MINICODEX_ENABLE_THINKING=true
```

复制 `.env.example` 为 `.env` 并替换 Key 即可。`.env` 已被 Git 忽略；程序优先读取 `MINICODEX_API_KEY`，未配置时回退到 `DASHSCOPE_API_KEY`。Qwen 思考模式通过非流式请求的 `extra_body={"enable_thinking": true}` 开启，以便一次取得完整 Tool Calls。

## 使用

### 终端单轮模式

```powershell
minicodex "检查测试失败，定位并修复 Bug，然后重新运行测试" --workspace .\demo\buggy_expense_tracker
```

也可以直接使用模块入口：

```powershell
python -m minicodex "给项目增加输入校验并运行测试" --workspace D:\path\to\project --max-turns 20
```

Agent 请求执行命令时会显示完整 argv 并询问：

```text
[permission] The agent wants to run this argv command:
  purpose: test (success will verify current changes)
  ['python', '-m', 'pytest', '-q']
Allow? [y/N]
```

直接回车或输入 `n` 都会拒绝执行，拒绝结果同样会回灌给模型。获批的项目命令也不会继承 `MINICODEX_API_KEY` 等宿主 API Key，避免测试或构建脚本读取凭据。会话记录保存在目标工作区的 `.minicodex/sessions/*.jsonl`，路径同样经过 Workspace Boundary 校验；该目录默认被 Git 忽略。

### 本机连续会话模式

```powershell
minicodex-web --workspace .\demo\buggy_expense_tracker --port 8000
```

启动后终端会打印形如 `http://127.0.0.1:8000/?token=...` 的随机会话 URL，请使用这一整条地址。服务端固定绑定 loopback，不提供 `--host` 参数，因此不会直接暴露给局域网或公网。页面关闭不会清空服务端 Session；只要进程未退出，重新打开页面仍能继续使用同一个 Agent 和 Workspace。事件总线保留最近 2,000 个事件，刷新或断线重连后可以重新渲染保留窗口内的执行卡片。

本机服务仍按不可信 HTTP 接口防护：每次启动生成 256-bit 级随机令牌，所有 `/api/*` 与 SSE 请求都必须携带；服务同时拒绝非 loopback `Host`、跨站 `Origin`，并设置 CSP、`no-referrer` 和 `nosniff`。这能阻断普通恶意网页与 DNS rebinding 直接读取事件或替用户批准命令。令牌只应保留在本机终端和地址栏，不要复制到截图、日志或他人可访问的位置；拥有该 URL 的本机进程或浏览器扩展仍应视为拥有本次 Agent 会话权限。

Web 模式依然使用原来的 `AgentSession` 和 `ToolRuntime`。一次只接受一个 Prompt，后台单 Worker 串行运行；结束后可以继续发送下一条 Prompt，历史消息、read-before-edit 已读集合、文件变化序号和验证状态都会保留。工具结果同时交给浏览器事件总线和 `print_tool_result()`，所以页面与启动服务的终端都能看到执行证据。

### SSE 如何工作

浏览器先携带启动 URL 中的 token 请求 `GET /api/session`，获取 Workspace、模型、状态和验证结果，再用原生 `EventSource` 长连接 `GET /api/events`。服务端把每个事件编码为：

```text
id: 17
event: diff
data: {"path":"expense_tracker.py","diff":"..."}
```

SSE 是服务器到浏览器的单向流，适合持续推送 Agent 事件；用户 Prompt 和审批决定则用普通 HTTP POST 反向发送。每个事件有递增 ID，浏览器断线重连时会发送 `Last-Event-ID`，服务端从内存事件总线补发之后的事件。15 秒没有新事件时发送 heartbeat，避免空闲连接被中间层回收。相比 WebSocket，这里无需双向帧协议、连接状态机或额外前端库，代码量更小，也足够支持本项目的实时输出。

Web API 很小：

| 路径 | 用途 |
|---|---|
| `GET /`、`GET /static/*` | 本机控制台与静态资源 |
| `GET /api/session?token=...` | 当前会话快照与待审批命令 |
| `GET /api/events?token=...` | SSE 事件流与断线补发 |
| `POST /api/prompts?token=...` | 提交下一轮 Prompt；忙碌时返回 409 |
| `POST /api/approvals/{id}?token=...` | 允许或拒绝当前命令 |

### 具体运行限制

| 限制 | 当前值 | 目的 |
|---|---:|---|
| 每个 Prompt 最大模型轮数 | 20（可用 `--max-turns` 调整） | 防止无限 Agent 循环 |
| 连续相同失败 Tool Call | 3 次 | 检测无进展重复调用 |
| Prompt 长度 | 20,000 字符 | 控制请求规模 |
| 单个 ToolResult 进入上下文 | 16,000 字符，保留约 70% 头部与 30% 尾部 | 保留错误起因和结尾摘要 |
| 历史压缩阈值 | 约 80,000 字符 | 压缩旧的完整消息组，不拆 tool call/result |
| 命令超时 | 默认 30 秒，允许 1–120 秒 | 限制子进程运行时间 |
| Web 审批等待 | 300 秒，超时拒绝 | 防止 Worker 永久阻塞 |
| SSE heartbeat | 15 秒 | 保持连接并及时发现断线 |
| 并发 Prompt | 1 | 避免同一 Workspace 并发修改 |
| Web 事件保留 | 最近 2,000 个；单条事件 JSON 最多约 32,000 字符 | 限制长会话内存与刷新重放成本 |
| 浏览器时间线 | 最近 500 张事件卡片 | 防止长会话 DOM 持续增长 |

四轮连续演示的可直接复制 Prompt 见 [MULTI_TURN_DEMO.md](demo/buggy_expense_tracker/MULTI_TURN_DEMO.md)。

## 测试

```powershell
python -m pytest -q --basetemp=.pytest-tmp
```

主测试集覆盖工作区逃逸、read-before-edit、唯一匹配、Diff、命令确认、失败诊断回灌、验证状态、重复调用、最大轮数、JSONL Trace、上下文截断、Mock Model Agent 循环和 OpenAI-compatible 适配器。

## 版本基线

真实 Agent 演示的脱敏结果保存在 `docs/baselines/`。首个基线使用 `qwen3.8-flash`，在约 59.4 秒内完成 8 次模型轮次和 10 次工具调用，将两个失败测试修复为两个通过测试，并达到 `COMPLETED + VERIFIED`：

- [人类可读报告](docs/baselines/v0.1.0-qwen3.8-flash-demo.md)
- [机器可读指标](docs/baselines/v0.1.0-qwen3.8-flash-demo.json)

后续版本应复用相同演示任务，比较工具调用数、模型轮次、端到端耗时、编辑次数、最终验证状态以及是否出现安全边界错误。仓库中的演示源码始终保留预置 Bug；成功修复结果记录在基线文档，而不是固化到 fixture。

## 两分钟演示方案

推荐使用 `demo/buggy_expense_tracker`。它足够小，观众能立即理解，又预置了两个真实且不同类型的 Bug：退款被错误计入绝对消费额，以及大小写/空格不同的分类未归并。初始测试应当是 **2 failed**。

建议视频节奏：

1. `0:00–0:15`：一句话介绍 MiniCodex 和安全边界，展示项目目录与两个失败测试。
2. `0:15–0:35`：输入 `TASK.md` 中的任务；Agent 先列文件、搜索并读取源码，突出 read-before-edit。
3. `0:35–1:05`：Agent 第一次运行 pytest；画面停留在 argv 和 `y/N` 确认，输入 `y` 后出现两个失败详情。
4. `1:05–1:30`：Agent 精确编辑两个函数；终端展示 unified diff。可以故意让一次编辑文本不唯一，快速展示结构化错误回灌与自我纠正。
5. `1:30–1:50`：再次批准 pytest，看到 `2 passed`，最终状态显示 `VERIFIED`。
6. `1:50–2:00`：打开 `.minicodex/sessions/*.jsonl`，点出每个模型回复、工具结果和最终验证状态均可追踪。

录屏前先在演示目录手动确认基线：

```powershell
cd demo\buggy_expense_tracker
python -m pytest -q
```

演示后若需恢复预置 Bug，使用 Git 恢复演示目录即可；录制过程中不要把真实 API Key 显示在终端。

## 初版边界

- 不提供 shell 字符串执行、自动批准命令或工作区外文件访问。
- 不实现多 Agent、MCP、IDE UI、Plan Mode、技能系统或代码索引。
- 历史压缩采用确定性摘要提示，不追求完整语义记忆；目标是清楚展示上下文治理机制。
- 文件并发修改检测尚未加入，后续可用 SHA-256/mtime 版本令牌升级。
- 服务关闭会拒绝新 Prompt、取消待审批命令并最多等待当前 Worker 2 秒；第三方模型 SDK 的同步请求和已启动子进程当前无法强制协作取消，超时后状态会明确保留为 `CLOSING`。正式版可进一步加入模型取消令牌和 Windows Job Object 终止子进程树。
