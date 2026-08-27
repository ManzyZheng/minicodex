# MiniCodex 本机 Web UI 与多轮会话设计

日期：2026-08-27
状态：待用户最终审阅

## 1. 目标

在保留现有终端 CLI 的基础上，新增一个只监听 `127.0.0.1` 的本机 Web UI。用户可以在同一页面连续提交多个 Prompt；同一 Agent Session 持续保留消息历史、Workspace、已读文件、修改序号、验证状态和 JSONL Trace。页面实时展示模型回复、工具调用、命令输出、unified diff 和验证状态，并在网页内批准或拒绝命令。

第一版优先满足考核演示，不建设通用 IDE 或多用户平台。

## 2. 范围

### 2.1 必须实现

- FastAPI 本机服务，固定绑定 `127.0.0.1`。
- 原生 HTML、CSS、JavaScript，不引入 Node.js、React 或 Vue。
- SSE 单向推送 Agent 事件；Prompt 和授权使用普通 POST。
- 单 Workspace、单 Agent Session、单后台工作线程。
- 真正的多轮消息历史；每个 Prompt 最多 20 次模型调用。
- 页面展示消息、工具结果、测试日志、Diff、状态和验证证据。
- 网页阻塞式命令确认；5 分钟未处理自动拒绝。
- 终端继续打印与网页一致的关键工具输出和 Diff。
- 原有 `minicodex` CLI 保持兼容；新增 `minicodex-web`。
- 新增四轮演示项目和简洁但具体的说明文档。

### 2.2 不实现

- 登录、远程访问、多用户、多 Workspace 或并行 Agent。
- 数据库、服务器重启后的会话恢复、历史会话列表。
- 浏览器内代码编辑器、文件树、文件上传。
- WebSocket、Redis、分布式事件队列。
- token 级模型流式输出；SSE 粒度为 Agent/工具事件。
- 自动批准或永久批准命令。

## 3. 技术选择

采用 FastAPI + Uvicorn + 原生前端 + SSE。

SSE 适合本项目的原因：数据主要由后端 Agent 持续流向浏览器，浏览器只偶尔发送 Prompt 或授权；单向长连接比 WebSocket 更小、更容易解释。SSE 使用浏览器原生 `EventSource`，支持自动重连和 `Last-Event-ID`。FastAPI 只负责本机 API 和静态资源，不直接承担 Agent 的长耗时工作。

新增运行依赖：

- `fastapi`
- `uvicorn`

前端不增加构建依赖。

## 4. 总体架构

```text
Browser
  ├── POST /api/prompts
  ├── POST /api/approvals/{request_id}
  ├── GET  /api/session
  └── GET  /api/events  (SSE)
             ▲
             │
FastAPI WebApp
  └── WebSession
      ├── EventBus（内存事件列表 + Condition）
      ├── ApprovalGate（单个待处理授权 + Condition）
      ├── AgentSession（持久消息历史）
      └── 单 Agent Worker Thread
             │
             ▼
OpenAIChatModel + ToolRuntime + WorkspaceGuard
             │
             ├── ConsoleSink
             ├── WebEventSink
             └── SessionTrace
```

整个服务生命周期只维护一个 `WebSession`。前一轮 Prompt 未结束时，新的 Prompt 返回 HTTP 409。所有文件和命令操作仍由现有 `ToolRuntime` 和 `WorkspaceGuard` 执行。

## 5. 多轮 Agent Session

现有 `Agent.run(task)` 每次创建新消息列表。新设计将循环状态拆为持久会话和单轮执行：

```python
session = AgentSession(model, tools, max_turns_per_prompt=20)
outcome1 = session.run_turn("修复现有 Bug")
outcome2 = session.run_turn("新增 monthly_totals 并添加测试")
```

会话持续保存：

- system prompt；
- 每轮 user、assistant、tool 消息；
- `ToolRuntime.read_paths`；
- `change_seq` 和最近验证证据；
- 历史压缩结果；
- JSONL Trace；
- Web 事件时间线。

每个新 Prompt 的模型调用计数从 1 重新开始，默认上限为 20。重复失败工具调用计数和“未验证提醒”也按 Prompt 重置。多轮 Prompt 数量不设固定上限；消息总字符数超过约 80,000 时使用现有结构化压缩。旧的 `Agent.run()` 保留为单轮便捷接口，内部可委托给新会话对象，避免破坏 CLI 和现有测试。

## 6. 事件模型

所有 Web 事件使用统一信封：

```json
{
  "id": 24,
  "type": "diff",
  "timestamp": "2026-08-27T09:06:31Z",
  "payload": {}
}
```

事件 ID 在单个服务生命周期内单调递增。第一版事件类型：

| 类型 | 主要数据 | 页面用途 |
|---|---|---|
| `session_started` | model、workspace、limits | 初始化顶部状态 |
| `user_prompt` | text、prompt_index | 用户消息 |
| `status` | IDLE/RUNNING/WAITING_APPROVAL | 状态徽标 |
| `model_message` | content、turn | Agent 消息 |
| `tool_call` | name、arguments、call_id | 工具调用卡片 |
| `tool_result` | ToolResult | 成功/失败摘要 |
| `diff` | path、diff | 红绿 Diff 面板 |
| `command_output` | argv、stdout、stderr、exit_code | 测试/程序输出 |
| `approval_required` | request_id、argv、purpose、timeout | 授权卡片 |
| `approval_resolved` | allow、reason | 授权结果 |
| `verification` | status、command、exit_code | 验证徽标和证据 |
| `turn_completed` | AgentOutcome | 当前 Prompt 总结 |
| `error` | code、message | 错误提示 |
| `heartbeat` | 空对象 | 维持 SSE 连接 |

Agent 当前不是 token 流式模型调用，因此 `model_message` 在一次模型回复完成后发送；工具调用、命令输出和 Diff 按发生顺序实时发送。

## 7. SSE 设计

`GET /api/events` 返回 `text/event-stream`。EventBus 保存服务启动后的全部事件，并用 `threading.Condition` 通知所有 SSE 生成器。每条消息格式为：

```text
id: 24
event: diff
data: {"path":"expense_tracker.py","diff":"--- a/..."}

```

浏览器初次连接从事件 1 重放；重连时读取 `Last-Event-ID`，只发送之后的事件。无新事件时最多等待 15 秒，然后发送 heartbeat。响应设置 `Cache-Control: no-cache` 和禁用代理缓冲所需的头。页面关闭不会立即终止 Agent；重新打开同一服务页面可重放内存事件。服务器重启后事件清空，但 JSONL Trace 仍保留在 Workspace。

## 8. 命令授权

Web 模式使用 `ApprovalGate` 替代终端 `input()`：

1. `run_command` 请求授权。
2. Gate 创建唯一 `request_id`，发布 `approval_required`，把 WebSession 状态改为 `WAITING_APPROVAL`。
3. Agent 工作线程在 Condition 上等待，最长 300 秒。
4. 浏览器 POST `{ "allow": true|false }` 到 `/api/approvals/{request_id}`。
5. Gate 校验 ID、解决授权并唤醒 Agent；超时或服务停止时返回拒绝。

页面必须展示 argv、purpose、timeout，以及 test/build/lint 成功后会验证当前修改的提示。只支持“允许一次”和“拒绝”。重复、过期或错误 ID 返回 HTTP 409/404。

## 9. API

| 方法与路径 | 行为 |
|---|---|
| `GET /` | 返回单页 UI |
| `GET /assets/app.css` | 返回样式 |
| `GET /assets/app.js` | 返回前端逻辑 |
| `GET /api/session` | 返回模型、Workspace、状态、验证状态和限制 |
| `GET /api/events` | SSE 事件流 |
| `POST /api/prompts` | 提交下一轮 Prompt；忙时 409 |
| `POST /api/approvals/{id}` | 允许或拒绝当前命令 |
| `POST /api/interrupt` | 请求当前 Prompt 尽快停止 |

Prompt 必须是非空字符串并设置合理长度上限。服务不接受客户端传入 Workspace 路径，Workspace 只在启动命令中确定。

## 10. 页面设计

第一版是单页时间线，不做 IDE 布局：

- 顶栏：MiniCodex、模型、Workspace、服务状态、验证状态、每 Prompt 最大 20 轮。
- 主区：用户消息、Agent 消息、工具卡片、命令输出和 Diff。
- 工具成功卡片默认折叠；失败、命令输出和 Diff 默认展开。
- Diff 按行着色：`+` 绿色、`-` 红色、`@@` 蓝灰色；保留等宽字体和横向滚动。
- 底部：多行输入框与发送按钮；Enter 发送、Shift+Enter 换行。
- 授权：居中模态卡片；等待时输入区禁用。
- 错误：时间线内显示，不使用浏览器 alert。

前端只通过 `textContent` 渲染模型/工具文本，不使用未净化的 `innerHTML`，避免本地项目内容触发 XSS。页面风格偏工程控制台：浅色背景、深色文本、克制的状态色，重点突出时间线、Diff 和验证证据。

## 11. 终端兼容

现有 `minicodex` CLI 和命令确认逻辑保持不变。新增 `minicodex-web` 入口。Web 模式中的同一事件同时送到：

- Console sink：打印工具结果、命令输出和 Diff；
- Web sink：发布 SSE；
- SessionTrace：写 JSONL。

终端无需显示网页布局事件，但必须保留现有 `[tool:ok]`、`[tool:error]`、命令输出、Diff 和最终状态。Uvicorn 访问日志默认降噪，避免淹没 Agent 输出。

## 12. 多轮演示项目

新增 `demo/multi_turn_expense_tracker`，初始仍包含退款计算和分类归一化两个 Bug。`PROMPTS.md` 提供四轮：

1. 修复现有两个 Bug并重新测试。
2. 新增 `monthly_totals(expenses)` 并添加测试。
3. 修改 `category_totals`，支持可选 `aliases` 且保持旧行为兼容。
4. 运行全部测试与 CLI 冒烟，只总结、不添加功能。

每轮都在同一页面、Session 和 Workspace 中执行。演示 README 说明预期观察点，不把后续轮次的目标实现预先放进 fixture。

## 13. 错误与安全

- Web 服务强制绑定 `127.0.0.1`，CLI 不提供 host 覆盖参数。
- Workspace 启动时解析一次，API 请求不能更换。
- 保留现有路径边界、read-before-edit、唯一匹配和子进程 Key 隔离。
- 同一时刻只运行一个 Prompt，避免并发编辑。
- 所有工具错误继续结构化回灌模型并发布到页面。
- SSE 客户端断开不影响 Agent；服务停止会拒绝等待中的授权。
- 前端不显示 `.env`，不提供文件任意读取 API，不把 Key发给浏览器。
- Web/Trace 大输出使用现有截断策略；页面命令输出设置可滚动区域。

## 14. 测试策略

- `AgentSession`：两轮 Prompt 共享消息历史、read_paths 和验证状态；每轮轮数独立；忙碌/中断行为。
- `EventBus`：ID 递增、按 last ID 重放、等待通知和 heartbeat。
- `ApprovalGate`：允许、拒绝、超时、错误 ID 和服务停止。
- Web API：本机 Session 信息、非空 Prompt、忙时 409、授权端点、SSE 格式。
- UI 静态契约：入口资源可加载；事件使用安全文本渲染；Diff 分类函数。
- 回归：现有 CLI 和 29 项主测试继续通过。
- 演示 fixture：初始测试稳定为两个失败；多轮预期写入文档而不是主测试套件。

## 15. 文档

根 README 增加 Web 启动方式、SSE 数据流、具体限制、API、事件类型、命令授权和多轮 Demo，但保持摘要式表达。新增 `docs/WEB_UI.md` 专门说明文件职责、最大轮数、80,000 字符压缩阈值、15 秒 heartbeat、300 秒授权超时、120 秒命令上限、终端兼容和常见答辩问题。

## 16. 验收标准

- `minicodex-web --workspace <path>` 能启动并只监听本机。
- 浏览器可连续完成至少四轮 Prompt，消息历史确实进入下一轮模型请求。
- 工具、测试输出和 Diff 同时显示在网页和终端。
- 网页批准或拒绝命令后 Agent 正确继续。
- SSE 断开重连可按事件 ID 重放。
- 页面正确展示 `NOT_RUN/FAILED/VERIFIED`。
- 原 CLI 行为和测试不回归。
- 根 README 与 Web 专项说明完整但不过度展开。
