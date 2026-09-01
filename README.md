# MiniCodex

一个用于项目考核与教学演示的简化版 Claude Code / Codex：单 Agent、OpenAI-compatible Tool Calling、六个核心工具，并把文件边界、修改约束、权限确认、循环保护、可追踪性和验证驱动完成做成可测试的工程能力。

它不是“套一个模型 API 的聊天脚本”。核心展示点是：模型可以自主查看代码、调用工具、接收结构化错误、修复真实 Bug，并用测试结果证明完成；同时宿主程序始终控制文件边界和命令执行权限。

## 功能

- 单 Agent 的 `model → tool calls → tool results → model` 循环。
- 多 Project、多 Session：项目注册表和会话状态写入应用数据目录；每个项目可创建、切换和恢复多个 Session，但全局同一时刻只允许一个 Agent 任务运行。
- Global + Project 双 Scope 记忆：显式记住/忘记、每个用户任务完成后一次保守自动提取、证据校验、敏感信息过滤和重复抑制；两类有效记忆都会自动保存。
- Codex 式本机 Web Console：以最终回答为主，过程默认折叠；文件变更卡片可打开右侧累计 Diff 审查，终端同步保留工具输出。
- OpenAI-compatible Chat Completions Tool Calling；支持自定义 `base_url`。
- Qwen thinking：非流式读取独立 `reasoning_content`，保留在终端和 JSONL Trace；Web 只显示模型主动给出的短进展与确定性工具摘要，不倾倒原始思维链。
- 六个工具：`list_files`、`search_text`、`read_file`、`write_file`、`edit_file`、`run_shell`。
- Workspace Boundary：内置文件工具的路径 resolve 后必须仍位于指定项目目录，阻止 `..`、绝对路径和符号链接逃逸。
- Session 只读参考：用户可用 `@api.md` 或 `@{D:\Docs\API Spec.md}` 显式引用一个文本文件；工作区外只授予该精确文件的只读快照，不扩大编辑、搜索、目录或 Shell 权限。
- 分层 Prompt 注入：静态行为规则、Session 环境/Git 快照、运行时权限状态和不可信参考资料分开构造；外部正文不能授权工具或覆盖安全规则。
- Read-before-edit：已有文件必须先读再写；新文件可以直接创建。
- 唯一匹配编辑：单次替换与原子 `edits[]` 批量替换中的每一步都必须唯一匹配；任一步失败时整批不落盘。
- 修改返回 unified diff；同一文件一轮内多次编辑时，Web 汇总为“本轮开始内容 → 最终内容”的累计 Diff。
- 两种持久执行权限：`ACT` 对文件变化和 Shell 逐次确认，`AUTO-ACT` 自动批准普通本地开发操作，并把灰区命令交给独立 Permission Reviewer；`PLAN` 是 Agent 可自主进入的临时只读阶段，不能自行提升回写权限。
- `run_shell` 以结构化 `commands[]` 批量提交命令字符串；每一步独立判权、执行和记录 stdout、stderr、真实 exit code。Windows 优先使用 PowerShell 7，其他平台使用 `/bin/sh`。
- 敏感路径保护：`.env`、私钥、`.git` 与 `.minicodex` 不允许被 Agent 读取、搜索、枚举或修改；`.env.example` 作为脱敏模板例外。
- 统一 `ToolResult`：工具错误被结构化回灌给模型，不会让 Agent 因一次工具失败而崩溃。
- 循环保护：最大模型轮数、连续三次相同失败调用检测、`Ctrl+C` 与 Web 停止按钮协作式中断。
- 分层上下文控制：单结果保护、预算缩减、过时结果裁剪与磁盘 Checkpoint，避免拆散 tool call / result，也避免压缩后反复读取文件。
- JSONL Session Trace：保存模型回复、工具调用结果、终止原因与验证状态，便于复盘。
- 分层 Session 持久化：`transcript.jsonl` 永久保存经过脱敏的前端历史，`state.json` 只保存可压缩的 Agent 工作快照，`trace.jsonl` 记录完整调试审计；上下文压缩不会删除用户看到的历史。
- 验证驱动完成：发生修改后，`test/build/lint` 命令成功才标记 `VERIFIED`；失败为 `FAILED`，未运行是 `NOT_RUN`。
- Mock Model 单元测试，不依赖真实 API 即可验证 Agent 状态机。

当前初版按考核范围暂不实现 SHA-256/mtime 文件版本保护；read-before-edit 记录的是本次会话中已读取的规范化路径。

如果想从一次 Prompt 的真实调用链开始逐文件阅读实现，请看 [CODE_WALKTHROUGH.md](CODE_WALKTHROUGH.md)。其中单独解释了 `VERIFIED` 的证据状态机、每项安全机制的代码位置、对应测试和常见答辩问题。

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
ToolRuntime ── WorkspaceGuard ── PermissionPolicy
  ├── list / search / read
  ├── write / exact edit / diff ── allow / ask / deny
  └── shell command batch ── allow/review/ask/deny ── verification state
                              └── Permission Reviewer ── allow/escalate

PromptContextBuilder ── static / environment / runtime / memory
ExternalReferenceRegistry ── exact-file read-only snapshots

WebWorkspaceManager ── ProjectRegistry / SessionRepository / MemoryStore
        │
        └── WebSession ── EventBus ── SSE ── Browser Timeline
     └────── ApprovalGate ◀── HTTP approval response
```

主要模块：

- `agent.py`：Agent 状态机、终止条件、验证提醒和最终状态。
- `permissions.py`：`PLAN/ACT/AUTO-ACT` 决策、敏感路径和高风险命令规则。
- `shell_analysis.py`：轻量拆分复合 Shell，识别常见操作、删除目标和 Workspace 路径关系；不能可靠分析时保守回退。
- `reviewer.py`：灰区 Shell 命令的独立结构化审核；异常或不确定时升级给用户。
- `tools.py`：六工具、统一异常转换、read-before-edit、Diff、审批与批量命令执行。
- `workspace.py`：项目目录边界和规范化路径。
- `model_adapter.py`：OpenAI-compatible 请求、tool call 解析及瞬时错误重试。
- `context.py`：工具输出截断和历史压缩。
- `prompting.py`：静态、Session、运行时和参考文件四层 Prompt 构造。
- `references.py`：解析用户 `@` 引用、校验敏感/类型/大小限制并保存 Session 快照。
- `session.py`：可逐行读取、可回放的 JSONL Trace。
- `transcript.py`：追加式前端历史、事件白名单、Diff Artifact 与历史投影。
- `persistence.py`、`projects.py`、`project_sessions.py`：应用数据路径、原子 JSON、Project 注册表和 Session 状态仓库。
- `memory/`：统一 Global/Project Memory、保守提取、证据验证、去重和 Prompt 索引。
- `web/manager.py`：管理当前活动 Project/Session，强制全局单运行任务，并在每轮结束后持久化和提取记忆。
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
│   ├── permissions.py            # 三种模式、路径保护与命令风险规则
│   ├── tools.py                  # 六工具、Diff、批量命令和 read-before-edit
│   ├── workspace.py              # Workspace Boundary 与路径规范化
│   ├── prompting.py              # 分层 System/Session/Runtime/Reference Prompt
│   ├── references.py             # 精确文件、只读、Session 级参考快照
│   ├── context.py                # 工具输出截断和历史摘要
│   ├── session.py                # JSONL Session Trace
│   ├── persistence.py            # 应用数据目录与原子 JSON
│   ├── projects.py               # 多 Project 注册表
│   ├── project_sessions.py       # 多 Session 元数据与状态
│   ├── memory/                   # 双 Scope 记忆、提取、验证与存储
│   └── web/
│       ├── app.py                # Prompt/审批 API、SSE 与静态文件路由
│       ├── session.py            # 连续会话与单 Worker 编排
│       ├── manager.py            # Project/Session 切换与单运行约束
│       ├── events.py             # 可重放内存事件总线
│       ├── approval.py           # 浏览器通用副作用审批门
│       └── static/               # 原生 HTML/CSS/JS 执行时间线
├── tests/
│   ├── test_core.py              # 配置、ToolResult、工作区和 Trace 测试
│   ├── test_tools.py             # 六工具、安全边界与命令隔离测试
│   ├── test_agent.py             # Mock Model、循环终止与上下文测试
│   ├── test_prompting.py         # 分层 Prompt、环境和不可信数据边界
│   ├── test_references.py        # @ 语法、快照、敏感文件和容量限制
│   ├── test_greenfield_multiturn_demo.py # 从零创建、多轮、压缩与 Trace 验收
│   ├── test_model_adapter.py     # Tool Calling 解析、重试和 Qwen 参数测试
│   └── test_cli.py               # CLI 参数与 Ctrl+C 行为测试
├── demo/buggy_expense_tracker/
│   ├── TASK.md                   # 推荐直接交给 Agent 的演示任务
│   ├── MULTI_TURN_DEMO.md        # 修 Bug→加功能→改旧功能→回归的四轮脚本
│   ├── expense_tracker.py        # 带两个预置 Bug 的小型项目
│   ├── sample.csv                # CLI 冒烟输入
│   └── tests/test_expense_tracker.py
├── demo/booknest_demo/
│   ├── reset_demo.py             # 固定路径安全重置并生成外部规范
│   └── MULTI_TURN_DEMO.md        # 从空 Workspace 开始的九轮演进脚本
├── demo/generated_booknest/      # 近空 Workspace，由 Agent 创建项目
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
| `MINICODEX_MODELS` | 否 | Web Composer 可选模型白名单，逗号分隔；默认只有 `MINICODEX_MODEL` |
| `MINICODEX_BASE_URL` | 否 | OpenAI-compatible `/v1` 端点 |
| `MINICODEX_ENABLE_THINKING` | 否 | `true/false`，控制 Qwen thinking 参数 |
| `MINICODEX_REVIEWER_ENABLED` | 否 | 默认 `true`；是否为 AUTO-ACT 灰区命令启用独立审核请求 |
| `MINICODEX_REVIEWER_MODEL` | 否 | Reviewer 模型；默认继承 `MINICODEX_MODEL`，审核请求始终关闭 thinking |
| `MINICODEX_DATA_DIR` | 否 | Project、Session、Trace 和记忆的应用数据根目录；Windows 默认 `%LOCALAPPDATA%/MiniCodex` |

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

适配器使用 Chat Completions Tool Calling 格式，把 SDK 返回的函数名、调用 ID 和 JSON 参数转换成内部 `ToolCall`。对 429、5xx、连接失败和超时最多尝试三次，并使用短指数退避。启用 Qwen thinking 时加入 `extra_body={"enable_thinking": true, "preserve_thinking": false}`；当前采用非流式请求，一次取得完整 `reasoning_content`、最终正文和 Tool Calls。关闭 preserved thinking 可以避免历史思考迅速扩大上下文，也不要求压缩后的历史完整回传供应商特有字段。

### `agent.py`：单 Agent 状态机

每个模型请求计为一轮。一次回复可以包含多个工具调用，Agent 会顺序执行并把每个 `ToolResult` 通过原始 `tool_call_id` 送回模型。循环可能以以下状态结束：

| 状态 | 含义 |
|---|---|
| `COMPLETED` | 模型给出最终文本 |
| `MAX_TURNS` | 达到最大模型轮数 |
| `REPEATED_CALL` | 连续三次相同失败调用且没有进展 |
| `INTERRUPTED` | 用户按下 Ctrl+C，或在 Web 页面点击停止按钮 |
| `MODEL_ERROR` | 模型适配器发生不可恢复错误 |
| `CONTEXT_ERROR` | 用户显式引用的文件不存在、敏感、不支持或超过限制；模型不会被调用 |

文件被修改但还没有验证时，Agent 会额外提醒模型运行测试、构建或 lint；若模型仍选择结束，则最终状态诚实显示 `NOT_RUN`。

### `workspace.py`：Workspace Boundary

所有用户路径都会通过 `Path.resolve()` 规范化，并检查解析后的路径仍位于工作区根目录。该策略同时阻止：

- `../secret.txt` 等父目录逃逸；
- 指向工作区外的绝对路径；
- 文件或目录符号链接逃逸；
- Trace 路径借助 `.minicodex` 链接写出工作区。

Workspace Boundary 继续约束所有普通文件工具和代码修改。外部参考不是第二个 Workspace：它不能列目录、搜索相邻文件、修改来源文件或扩大 Shell 权限。

### `references.py` 与 `prompting.py`：Context Boundary

用户消息中的 `@api.md` 解析为工作区相对文件；带空格的绝对路径使用 `@{D:\API Docs\api.md}`。普通路径文字不会自动读取。显式引用成功后，程序只保存该精确文件的 UTF-8 内容快照，并在当前 Agent Session 的后续轮次继续提供；重新 `@` 同一路径会刷新快照，前端可移除它。移除会清理本地后续模型请求中的历史快照，但无法撤回供应商此前已经收到的 API 请求。引用文件中的其他 `@` 不递归执行。

外部参考只支持受控文本扩展名，单文件最多 64 KiB、最多 8 个、Session 总计最多 128 KiB；`.env`、私钥、证书和 `credentials.json` 直接拒绝。参考正文会发送给配置的模型供应商，但不会写入 JSONL Trace 或在前端展开。`prompting.py` 将其标记为 `untrusted-data + read-only-session-snapshot`；真正的权限仍由 Python 工具层强制。

System Prompt 现在分为稳定规则、Session 环境快照和可更新的 Runtime 模式三条 system message；当前用户文本与参考资料保持 user data。环境层包含 Workspace、平台、Shell、最大轮数和受限的初始 Git 分支/提交/status，不注入完整 Diff。稳定规则要求模型优先做最小完整实现、批量提交彼此独立的工具调用、尽早验证第一条完整功能链，并严格使用环境层报告的 Shell，减少无依赖的模型往返和跨 Shell 语法错误。

### `context.py`：96K Token 三层上下文控制

`ContextManager` 由轻到重处理历史：单个 ToolResult 最多保留 16K 字符；约 60K estimated tokens 后缩短旧的大型结果，76K 后裁剪同一路径的重复读取，96K 后生成唯一滚动 Checkpoint，并从磁盘恢复最近文件，目标约 64K tokens。估算器按 ASCII 约 4 字符/token、非 ASCII 约 1 字符/token 计算，不增加 tokenizer 依赖；前端仍显示压缩前后的字符数，Trace 额外记录 token 估算。完整工具保护区覆盖最近 2 个模型工具轮次、最多约 30K 字符，历史 Tool Call 参数从不改写。

Agent 还使用软 Turn 收敛提示：Turn 8 提醒合并改动并进入目标验证，Turn 12 要求收尾，Turn 16 要求只完成最终验证；一旦状态为 `VERIFIED`，下一轮优先要求直接给出最终答案。若仍有用户明确要求但尚未完成的交付项，模型可以继续；普通小修复不会自行扩展到版本升级、打包或部署调查。

### 多 Project、多 Session 与双 Scope 记忆

Web 启动时把 `--workspace` 注册为一个 Project；左侧栏可继续添加本地项目，并在每个项目下创建多个 Session。`WebWorkspaceManager` 只允许一个活动 Session 执行：运行、等待审批或停止过程中切换会返回 HTTP 409；空闲时切换会重新创建 `AgentSession`、`ToolRuntime`、审批门和中断标记，再从应用数据目录恢复对话、Prompt 计数、模型/模式、验证状态和累计 Diff。Read-before-edit 缓存、正在运行的进程、待审批请求和外部参考注册表不会跨进程恢复，避免把过期运行时能力带入新进程。

记忆采用统一结构：`scope = global | project`，`kind = preference | decision | reference`。Global Memory 对所有项目可见，Project Memory 只注入当前项目；当前用户消息优先于记忆，项目记忆只在当前项目内覆盖全局偏好，任何记忆都不能授予权限、放宽 Workspace Boundary 或绕过 Shell 审核。Prompt 只注入有上限的记忆索引，总字符数默认不超过 12K。

每个用户任务结束后，独立的 `MemoryExtractor` 使用同一供应商模型但关闭 thinking、不给工具，最多返回两条候选且允许空数组。候选必须引用最近用户消息中的连续原文；程序再验证 Scope 证据、长度、敏感信息和重复内容。有效的 Global 与 Project 候选都会自动保存；失败或坏 JSON 只产生后台事件，不改变主任务的 `COMPLETED/VERIFIED/FAILED` 状态。记忆正文会在后续请求中发送给配置的模型供应商，因此不应手动保存密钥、Token 或其他敏感内容。

默认存储结构位于 `%LOCALAPPDATA%/MiniCodex`，可以通过 `MINICODEX_DATA_DIR` 覆盖：

```text
MiniCodex/
├── registry.json
├── memory/items/                 # Global Memory
└── projects/<project-id>/
    ├── project.json
    ├── memory/items/             # Project Memory
    └── sessions/<session-id>/
        ├── metadata.json
        ├── transcript.jsonl       # 用户可见历史；只追加，不参与上下文压缩
        ├── state.json             # Agent 下一轮恢复快照；原子覆盖，可被压缩
        ├── trace.jsonl            # 模型、工具、权限与错误的调试审计
        └── artifacts/diffs/*.patch # 历史文件卡片引用的完整 Diff
```

三种 Session 数据有严格边界：浏览器从 Transcript 恢复对话，Agent 只从 State 恢复工作上下文，Trace 不作为普通 UI 数据源。公开事件会同时通过 SSE 实时推送并追加到 Transcript；原始 reasoning、完整 ToolResult、Shell 长输出和文件 before/after 不进入 Transcript。文件卡片只保存路径、增删行数与 `diff_ref`，完整 Diff 在 Artifact 中保存一次。旧 Session 第一次打开时优先根据追加式 Trace 迁移历史，再用旧 State 补充仍可获得的文件变更；迁移不会改写原 Trace。

### `session.py`：JSONL Trace

每行是一个独立 JSON 事件，包含 UTC 时间、事件类型和 payload。主要事件包括 `session_start`、`model_reply`、`tool_result`、`model_error` 和 `final`。终端单次运行仍可把 Trace 放在目标工作区；Web 多 Session 模式把每个 Trace 写入应用数据目录对应的 Session，避免污染或提交用户项目。Trace 可能包含本机路径和完整代码上下文，不应进入远程仓库。

## 六个工具

| 工具 | 主要参数 | 功能与约束 |
|---|---|---|
| `list_files` | `path` | 列出工作区内文件，跳过 Git、Trace、敏感路径和缓存目录 |
| `search_text` | `query`, `path` | 搜索 UTF-8 文本；跳过敏感路径，每个候选文件重新检查边界 |
| `read_file` | `path`, `start_line`, `end_line` | 读取非敏感 UTF-8 文件；大文件可读取最多 1000 行的带行号片段，片段读取同样满足 Read-before-edit |
| `write_file` | `path`, `content` | 新建文件或覆盖已读文件；成功后返回 unified diff |
| `edit_file` | `path` + `old_text/new_text` 或 `edits[]` | 单点替换保持兼容；最多 12 个顺序替换可一次原子执行，任一步不唯一则不写文件；累计 unified diff 仍由变更状态记录并展示 |
| `run_shell` | `commands`, `stop_on_failure` | 顺序执行 1–8 个命令；普通程序优先结构化 `argv`，管道/重定向才用 `command`；逐条判权，1–120 秒超时 |

每个 command 的 `purpose` 为 `test`、`build`、`lint` 或 `other`。前三种命令作用于验证状态：最近一次相关命令退出码为 0 时是 `VERIFIED`，非 0 时是 `FAILED`；之后再次修改文件会重置为 `NOT_RUN`。子进程不会继承 MiniCodex、DashScope、OpenAI 或 Anthropic API Key。

批量调用示例：

```json
{
  "commands": [
    {"argv": ["python", "-m", "pytest", "-q"], "purpose": "test"},
    {"argv": ["python", "expense_tracker.py"], "purpose": "other"}
  ],
  "stop_on_failure": true
}
```

`argv` 通过 `subprocess.run(..., shell=False)` 直接执行，避免 PowerShell 引号、编码和 exit code 差异；只有需要管道、重定向、变量等语法时才使用 `command`。两种形式必须二选一，都会经过相同权限分析、Reviewer、输出捕获和验证状态逻辑。子进程的 `TMP/TEMP/TMPDIR` 指向工作区 `.minicodex/tmp`，避免系统 Temp 不可写导致 pytest 反复失败；API Key 仍会被移除。失败且 `stop_on_failure=true` 时，后续步骤标记为 skipped。

## 执行权限与临时 PLAN

| 状态/权限 | 读文件 | 修改/新建文件 | 普通本地 Shell | 灰区/边界命令 | 用途 |
|---|---|---|---|---|---|
| `PLAN` | 自动 | 禁止 | 禁止 | 禁止 | 只读探索并给出实施方案 |
| `ACT` | 自动 | 展示 Diff 后确认 | 确认 | 确认 | 默认的逐次审阅模式 |
| `AUTO-ACT` | 自动 | 普通工作区文件自动 | 自动 | Reviewer 或用户确认 | 类似“帮我批准”的连续开发 |

Composer 中只选择 `ACT / AUTO-ACT`，它们是持续生效的执行权限。Agent 遇到解释、诊断、设计或明确“先规划”的任务时，可调用 `enter_plan_mode` 降权进入 PLAN；此时只暴露读取工具和 `exit_plan_mode`。完成计划后页面显示“执行方案”，但 Agent 仍处于只读状态。只有用户点击执行或明确回复“执行”，才按当前 Composer 权限继续；普通反馈会保持 PLAN 并修订方案。

`AUTO-ACT` 先用轻量 Command Analyzer 把复合 Shell 识别为测试、安装、Git、网络、系统配置、删除或普通进程等操作，并解析可确定的删除目标。普通本地开发命令和 Workspace 内已知缓存清理为 `ALLOW`；安装依赖、下载、嵌套 Shell、rebase、普通源码删除等灰区进入 `REVIEW`；工作区外普通访问、普通 push 和系统配置进入 `ASK`；Workspace 根目录/受保护或动态删除目标、`git reset --hard`、强制 clean/push、编码 PowerShell、关机/格式化等进入 `DENY`。Analyzer 不是完整 Shell 解释器，不能可靠分析时会回退 Reviewer 或用户。Reviewer 只返回 `allow/escalate`，不能覆盖硬拒绝；无效输出、API 失败或 `escalate` 都回退到人工审批。`.env`、私钥、`.git` 和 `.minicodex` 仍受保护。

这是一层应用内受控 Shell，不是操作系统沙箱。Reviewer 是审批自动化而不是安全边界；获准程序仍拥有当前用户的系统权限，因此 AUTO-ACT 只适合用户明确选择并信任的本机工作区。

## 一次任务的数据流

```text
1. CLI 解析任务、工作区、模型和最大轮数
2. Config 从环境变量或当前目录 .env 加载配置
3. Agent 根据当前模式把 system prompt、用户任务和允许的工具 schema 发给模型
4. 模型返回一个或多个 Tool Calls
5. ToolRuntime 验证整批参数，并让 PermissionPolicy 返回 allow/review/ask/deny
6. 灰区命令由独立 Reviewer 返回 allow/escalate；其余需要时由 CLI/Web 展示 Diff 或 Shell 审批
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
MINICODEX_REVIEWER_ENABLED=true
MINICODEX_REVIEWER_MODEL=qwen3.8-flash
```

复制 `.env.example` 为 `.env` 并替换 Key 即可。`.env` 已被 Git 忽略；程序优先读取 `MINICODEX_API_KEY`，未配置时回退到 `DASHSCOPE_API_KEY`。Qwen 思考模式通过非流式请求开启：适配器从 `message.reasoning_content` 读取完整思考字段，通过独立 `model_reasoning` 事件显示；最终答案仍来自 `message.content`。Reviewer 复用同一 API Key，但使用独立上下文和关闭 thinking 的请求。当前显式设置 `preserve_thinking=false`，不把历史思考回传到后续模型请求。

## 使用

### 终端单轮模式

```powershell
minicodex "检查测试失败，定位并修复 Bug，然后重新运行测试" --workspace .\demo\buggy_expense_tracker --mode act
```

也可以直接使用模块入口：

```powershell
python -m minicodex "给项目增加输入校验并运行测试" --workspace D:\path\to\project --max-turns 50
```

`--mode` 可选 `plan`、`act`、`auto-act`，默认 `act`。ACT 中，文件变化会先展示 Diff，命令则展示完整 Shell 字符串，并询问：

```text
[permission] run shell command 1 of 1
  purpose: test
  python -m pytest -q
Allow? [y/N]
```

直接回车或输入 `n` 都会拒绝执行，拒绝结果同样会回灌给模型。获批的项目命令也不会继承 `MINICODEX_API_KEY` 等宿主 API Key，避免测试或构建脚本读取凭据。会话记录保存在目标工作区的 `.minicodex/sessions/*.jsonl`，路径同样经过 Workspace Boundary 校验；该目录默认被 Git 忽略。

### 本机连续会话模式

```powershell
minicodex-web
```

不传 `--workspace` 时先进入项目主页，不创建 Agent，也不会把启动目录隐式授权为 Workspace。点击左侧“＋”会打开 Windows 原生文件夹选择窗口；Project 名称默认取文件夹名。左侧使用扁平树展示 Project、Session 和项目记忆，Workspace 绝对路径只在悬浮 Project 名称时提示。Project 右侧的“＋”创建 Session，“⋯”菜单可重命名或删除 Project/Session；重命名只改显示名称，删除只清理 MiniCodex 应用数据中的会话、Trace、状态和项目记忆，**绝不删除或重命名 Workspace 文件夹及其中的代码**。各 Session 保存独立对话、Trace 和验证状态，但串行操作同一个 Workspace。

录制 Demo 或已经知道目标目录时，仍可快速打开：

```powershell
minicodex-web --workspace .\demo\buggy_expense_tracker --mode auto-act --port 8000
```

启动后终端会打印形如 `http://127.0.0.1:8000/?token=...` 的随机会话 URL，请使用这一整条地址。带 `--workspace` 时注册并恢复该 Project；不带时只加载 Project Registry，直到用户选择 Session 或添加 Project 后才创建 `AgentSession`、工具运行时和 Workspace Boundary。服务端固定绑定 loopback，不提供 `--host` 参数，因此不会直接暴露给局域网或公网。页面关闭不会清空服务端 Session；只要进程未退出，重新打开页面仍能继续使用同一个 Agent 和 Workspace。事件总线保留最近 2,000 个事件，刷新或断线重连后可以重新渲染保留窗口内的执行卡片。

本机服务仍按不可信 HTTP 接口防护：每次启动生成 256-bit 级随机令牌，所有 `/api/*` 与 SSE 请求都必须携带；服务同时拒绝非 loopback `Host`、跨站 `Origin`，并设置 CSP、`no-referrer` 和 `nosniff`。这能阻断普通恶意网页与 DNS rebinding 直接读取事件或替用户批准命令。令牌只应保留在本机终端和地址栏，不要复制到截图、日志或他人可访问的位置；拥有该 URL 的本机进程或浏览器扩展仍应视为拥有本次 Agent 会话权限。

Web 模式继续复用同一个 `AgentSession` 和 `ToolRuntime`。`ACT / AUTO-ACT` 与模型白名单选择器位于底部 Composer；PLAN 是对话中的临时状态，不是第二套工作方式菜单。计划完成后只需点击“执行方案”，系统使用当前执行权限继续同一消息历史、已读集合和 Workspace。一次只接受一个 Prompt，后台单 Worker 串行运行。运行时发送箭头原位变成停止方块；点击后进入 `STOPPING`，拒绝待审批操作，并在模型返回后或 Tool Call 之间以 `INTERRUPTED` 安全结束。同步模型请求或已经启动的 Shell 不会被强杀。Web 事件投影会过滤原始 reasoning，只展示模型主动给出的公开短进展；模型没有公开正文时不补写说明，仅保留确定性的工具记录。执行过程中每秒更新已用时间，结束后冻结最终耗时；过程默认只显示最近 2 个阶段，可展开较早记录。阶段数只表示前端可展示过程，最终标签中的 `Model Turn` 才是后端真实模型调用数。SSE 携带后端 UTC 时间，因此刷新和历史重放仍显示真实耗时。对话左侧的 Prompt 导航轨道以每轮用户输入为刻度，悬浮可预览内容，点击可平滑跳转到对应对话。最终回答和文件变更卡片保持展开，点击文件在右侧查看累计 Diff。

### SSE 如何工作

浏览器先携带启动 URL 中的 token 请求 `GET /api/session`，获取 Workspace、模型、状态和验证结果，再用原生 `EventSource` 长连接 `GET /api/events`。服务端把每个事件编码为：

```text
id: 17
event: file_changed
data: {"prompt_index":1,"path":"expense_tracker.py","diff":"...","additions":2,"deletions":2,"event_timestamp":"...Z"}
```

SSE 是服务器到浏览器的单向流，适合持续推送 Agent 事件；用户 Prompt 和审批决定则用普通 HTTP POST 反向发送。每个事件有递增 ID，浏览器断线重连时会发送 `Last-Event-ID`，服务端从内存事件总线补发之后的事件。15 秒没有新事件时发送 heartbeat，避免空闲连接被中间层回收。相比 WebSocket，这里无需双向帧协议、连接状态机或额外前端库，代码量更小，也足够支持本项目的实时输出。

Web API 很小：

| 路径 | 用途 |
|---|---|
| `GET /`、`GET /static/*` | 本机控制台与静态资源 |
| `GET /api/session?token=...` | 会话、权限/模型选项、累计文件变化与待审批状态 |
| `GET /api/transcript?token=...&limit=100&before_seq=...` | 分页读取当前 Session 的永久前端历史；每页最多 200 条 |
| `GET /api/events?token=...` | SSE 事件流与断线补发 |
| `POST /api/system/folder-picker?token=...` | 打开本机 Windows 文件夹选择窗口；取消返回 `selected=false` |
| `POST /api/projects?token=...` | 注册选择的 Workspace 并创建或恢复初始 Session |
| `PATCH /api/projects/{id}?token=...` | 修改 Project 显示名称，不重命名磁盘目录 |
| `DELETE /api/projects/{id}?token=...` | 删除 MiniCodex 所有该 Project 应用数据，保留 Workspace |
| `POST /api/projects/{id}/sessions?token=...` | 新建并激活 Session |
| `PATCH /api/projects/{id}/sessions/{session_id}?token=...` | 修改 Session 标题 |
| `DELETE /api/projects/{id}/sessions/{session_id}?token=...` | 删除 Session 应用数据并安全切换到可用 Session |
| `POST /api/prompts?token=...` | 提交 Prompt、ACT/AUTO-ACT 与白名单模型；忙碌时返回 409 |
| `POST /api/interrupt?token=...` | 请求停止当前 Prompt；空闲时返回 409 |
| `POST /api/mode?token=...` | 兼容接口；新页面只用它更新 ACT/AUTO-ACT |
| `POST /api/plans/{id}/resolve?token=...` | 执行、修订或取消待审批计划 |
| `POST /api/approvals/{id}?token=...` | 允许或拒绝当前文件变化/命令 |
| `DELETE /api/references/{id}?token=...` | 从后续轮次移除一个 Session 参考；Agent 忙碌时返回 409 |

### 具体运行限制

| 限制 | 当前值 | 目的 |
|---|---:|---|
| 每个 Prompt 最大模型轮数 | 50（可用 `--max-turns` 调整） | 给从零创建项目留出修复和验证空间；重复失败检测仍会提前终止循环 |
| 连续相同失败 Tool Call | 3 次 | 检测无进展重复调用 |
| Prompt 长度 | 20,000 字符 | 控制请求规模 |
| Session 参考文件 | 8 个；单个 64 KiB；总计 128 KiB | 防止外部资料无限扩大模型上下文 |
| 单个 ToolResult 进入上下文 | 16,000 字符，保留约 70% 头部与 30% 尾部 | 保留错误起因和结尾摘要 |
| 上下文分层水位 | 60K / 76K / 96K estimated tokens；目标约 64K | Budget Trim、Stale Snip、Auto Checkpoint；事件展示字符数并记录 token 估算 |
| 命令超时 | 默认 30 秒，允许 1–120 秒 | 限制子进程运行时间 |
| 单次命令批量 | 1–8 步；默认失败即停止 | 减少 Tool Call，同时保留逐步权限和证据 |
| Web 审批等待 | 300 秒，超时拒绝 | 防止 Worker 永久阻塞 |
| SSE heartbeat | 15 秒 | 保持连接并及时发现断线 |
| 并发 Prompt | 1 | 避免同一 Workspace 并发修改 |
| Web 事件保留 | 最近 2,000 个；单条事件 JSON 最多约 32,000 字符 | 限制长会话内存与刷新重放成本 |
| 浏览器时间线 | 最近 500 张事件卡片 | 防止长会话 DOM 持续增长 |

短演示使用 [Expense Tracker 四轮脚本](demo/buggy_expense_tracker/MULTI_TURN_DEMO.md)。完整能力演示使用 [BookNest 九轮脚本](demo/booknest_demo/MULTI_TURN_DEMO.md)：Agent 从近空 Workspace 创建项目，再持续增改、修 Bug、引用外部规范、触发上下文压缩并做批量回归。

## 测试

```powershell
python -m pytest -q --basetemp=.pytest-tmp
```

主测试集覆盖三种权限模式、Shell 风险分类、Reviewer 结构化决策与人工回退、敏感路径、高风险命令、批量 Shell、工作区逃逸、read-before-edit、唯一匹配、空文件创建、Diff、失败诊断回灌、验证状态、重复调用、最大轮数、JSONL Trace、上下文截断与压缩事件、分层 Prompt、Session 外部参考去重/刷新/移除、从空 Workspace 开始的多轮 Mock Model 场景、Web PLAN→执行流程和 OpenAI-compatible 适配器。

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
3. `0:35–0:55`：Agent 自主进入 PLAN 只读定位两个 Bug；页面明确“批准后使用 AUTO-ACT”，点击“执行方案”。
4. `0:55–1:30`：Agent 精确编辑两个函数；最终回答下方出现文件卡片，点击后在右侧展示累计 Diff，识别出的 pytest 自动执行。
5. `1:30–1:50`：看到 `2 passed`，再用一个批量命令完成完整测试与 CLI 冒烟，最终状态显示 `VERIFIED`。
6. `1:50–2:00`：打开 `.minicodex/sessions/*.jsonl`，点出每个模型回复、工具结果和最终验证状态均可追踪。

录屏前先在演示目录手动确认基线：

```powershell
cd demo\buggy_expense_tracker
python -m pytest -q
```

演示后若需恢复预置 Bug，使用 Git 恢复演示目录即可；录制过程中不要把真实 API Key 显示在终端。

## 初版边界

- `run_shell` 同时提供结构化 argv 与完整 Shell 字符串，但权限规则与 Reviewer 都是应用层保护，不是 OS 沙箱；获准进程仍可能以当前用户权限访问工作区外资源。
- 不实现多 Agent、MCP、IDE 插件、技能系统或代码索引；Plan Mode 是同一 Session 中的只读/执行状态切换。
- 历史压缩仍采用确定性 Checkpoint；长期记忆只保存经过证据与安全校验的短偏好/决策/参考，不尝试把完整会话总结成知识库，也不使用向量检索。
- 文件并发修改检测尚未加入，后续可用 SHA-256/mtime 版本令牌升级。
- 服务关闭会拒绝新 Prompt、取消待审批命令并最多等待当前 Worker 2 秒；第三方模型 SDK 的同步请求和已启动子进程当前无法强制协作取消，超时后状态会明确保留为 `CLOSING`。正式版可进一步加入模型取消令牌和 Windows Job Object 终止子进程树。
