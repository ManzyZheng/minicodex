# MiniCodex Codex 式 Web UI 与自主 Plan 路由设计

日期：2026-08-29
状态：待用户文档审阅

## 1. 目标

把当前“内部事件时间线”重构为面向用户的本机 Coding Agent 工作台：中央区域像 Codex 一样以对话和最终结果为主，文件变更汇总位于回答下方，点击文件后在右侧审查累计 Diff。运行过程默认折叠，只展示少量有价值的中文进展，不直接倾倒供应商原始 `reasoning_content`。

同时升级 Plan Mode：Agent 可以根据用户意图自主进入只读规划状态，但不能自主从 PLAN 获得写权限。计划完成后必须由用户选择 `AUTO-ACT` 执行、`ACT` 审阅执行或继续规划。

本次仍保持单 Agent、单 Workspace、SSE、原生 HTML/CSS/JavaScript 和六个核心工作区工具，不扩展 MCP、多 Agent、复杂流式 Tool Calling 或浏览器代码编辑器。

## 2. 设计原则

1. **结果优先**：完成后默认只展开最终回答；过程、命令详情和原始证据按需展开。
2. **对话与审查分离**：聊天负责解释结果，右侧面板负责检查代码变化。
3. **权限升级必须显式**：自主进入 PLAN 是降低权限，可自动；离开 PLAN 并写文件是提升权限，必须由用户批准。
4. **中文是界面语言，不是机械翻译**：普通说明和进展使用中文；`VERIFIED`、`ACT`、`AUTO-ACT`、代码、文件名、argv、stdout/stderr 和错误原文可保留英文。
5. **不伪装原始推理**：页面中的“执行过程”是精炼进展与证据摘要，不把完整思维链当作产品输出。
6. **状态由代码产生**：Diff、验证、模式和权限来自运行时状态，不依赖模型在回答中自报。

## 3. 范围

### 3.1 必须实现

- Codex 式单页对话布局，底部固定输入框。
- `ACT / AUTO-ACT` 与模型选择器放入输入框底部工具栏。
- 顶栏只保留 MiniCodex、Workspace、运行状态和验证状态等轻量信息。
- 当前轮显示最新重要进展；完成后最终回答展开、执行过程折叠。
- 执行过程展示中文摘要，命令 stdout/stderr 再次展开后显示原文。
- 最终回答下方显示“已编辑 N 个文件”、总增删行数和文件列表。
- 点击文件打开右侧累计 Diff；关闭后恢复单栏。
- 同一文件多次修改时，审查区显示从本轮首次修改前到当前内容的累计 Diff。
- Agent 控制工具 `enter_plan_mode`、`exit_plan_mode`。
- Agent 可自主进入 PLAN；计划完成后由用户选择自动执行、审阅修改或继续规划。
- 手动“只规划 / 自动判断 / 直接执行”工作方式入口。
- 页面刷新和 SSE 重放后能恢复模式、变更集合、最终回答和待审批状态。
- 保留终端输出与 JSONL Trace；原始 reasoning 可继续进入 Trace，但不进入普通 Web UI。

### 3.2 不实现

- Codex 桌面应用的项目侧边栏、任务列表、标签页、分享、PR、提交和撤销 UI。
- 浏览器代码编辑器、行级评论、文件树或 Git staging。
- Plan 文件跨进程持久化、清空上下文再执行和历史 Plan 列表。
- 基于额外模型调用的独立意图分类器或 reasoning 摘要器。
- 原始 reasoning 的普通用户展示开关。
- 多 Workspace、多用户、远程访问或并行 Agent。

## 4. 信息架构

### 4.1 桌面布局

```text
┌──────────────────────────────────────────────────────────────────┐
│ MiniCodex   expense_tracker                    IDLE · VERIFIED    │
├───────────────────────────────────┬──────────────────────────────┤
│ 对话区                            │ 代码变更                     │
│                                   │ expense_tracker.py   +2 -2  │
│ 用户指令                          │ tests/test_...       +8 -0  │
│                                   ├──────────────────────────────┤
│ ▸ 执行过程 · 8 个操作 · 2.3 秒     │ 选中文件累计 Diff             │
│                                   │                              │
│ MiniCodex 最终回答                 │ - old line                   │
│                                   │ + new line                   │
│ 已编辑 2 个文件        +10 -2     │                              │
│ expense_tracker.py       +2 -2    │                              │
│ tests/test_...            +8 -0    │                              │
├───────────────────────────────────┴──────────────────────────────┤
│ 输入下一条指令……                                        发送    │
│ +  自动判断   ACT   qwen3.8-flash                                │
└──────────────────────────────────────────────────────────────────┘
```

右侧面板默认关闭。点击变更卡片或文件后，对话区缩窄并打开审查面板；关闭审查面板后恢复单栏。窗口宽度不足时，审查面板改为覆盖式抽屉。

### 4.2 顶栏

顶栏不再承担所有控制，仅显示：

- MiniCodex；
- 当前 Workspace 的短名称，悬停可见完整路径；
- `IDLE / RUNNING / WAITING_APPROVAL`；
- `NOT_RUN / FAILED / VERIFIED`；
- 打开或关闭代码审查的入口。

模型、工作方式和执行权限属于“下一条指令如何运行”，放在输入框内，不放在全局状态栏。

### 4.3 输入框

输入框参考 Codex 的组合式 Composer：正文输入位于上方，底部一行放控制项。

```text
+  [自动判断⌄]  [ACT⌄]  [qwen3.8-flash⌄]                 [发送]
```

- 工作方式：`自动判断 / 只规划 / 直接执行`；
- 执行权限：`ACT / AUTO-ACT`；
- 模型：当前配置模型；第一版只展示并选择后端已经允许的模型值，不在浏览器接触 API Key；
- 运行期间三个选择器禁用，避免中途改变语义；
- `PLAN` 是当前工作流状态，不再与 `ACT/AUTO-ACT` 放在同一个权限下拉框。

## 5. 对话展示

### 5.1 轮次结构

每个用户 Prompt 对应一个对话轮次，但不再使用大边框包住所有内部事件。轮次包含：

1. 用户消息；
2. 一条运行中状态或完成后的折叠过程摘要；
3. MiniCodex 最终 Markdown 回答；
4. 本轮文件变更卡片；
5. PLAN 完成时的计划审批操作。

历史轮次保留用户消息与最终回答；执行过程默认关闭。当前轮运行时只自动滚动到最新进展，不反复插入大卡片。

### 5.2 三级信息密度

```text
第一层：最终回答、VERIFIED、文件变更摘要
  ↓ 用户展开
第二层：重要进展、工具摘要、命令摘要
  ↓ 用户再次展开
第三层：完整 argv、stdout、stderr、退出码和错误原文
```

Diff 不属于这三级内容，统一进入右侧审查面板。

### 5.3 进展摘要

页面不直接渲染完整 `model_reasoning`。可见进展来自两类数据：

- 模型在 Tool Call 前返回的短 `content`；
- 程序根据工具和命令事件生成的确定性摘要。

System Prompt 要求：面向用户的进展使用用户语言；中文会话中每次最多一句、约 40 个汉字；说明当前目标、重要发现或下一步，不输出完整思维链，不重复用户指令。

工具摘要由前端或后端映射：

| 事件 | 默认展示 |
|---|---|
| `read_file` 成功 | `已读取 expense_tracker.py` |
| `search_text` 成功 | `已搜索 “total_spending”，找到 3 处` |
| `edit_file` 成功 | `已修改 expense_tracker.py` |
| pytest 退出码 0 | `测试通过 · 2 passed` |
| 命令退出码非 0 | `命令失败 · exit code 1` |
| 权限拒绝 | `操作已拒绝，Agent 将调整方案` |

文件名、搜索词、命令输出和错误原文保持原样。

### 5.4 语言规则

- 中文用户输入时，进展、计划、审批说明和最终回答使用中文。
- 英文用户输入时跟随英文，不把系统固定为只会中文。
- `ACT`、`AUTO-ACT`、`PLAN`、`VERIFIED` 等稳定产品状态保留英文。
- Python/API/库名、文件路径、代码、argv、stdout/stderr 和错误原文不翻译。
- 移除当前 `LOCAL AGENT CONSOLE`、`LIVE AGENT SESSION`、`COMMAND OUTPUT` 等无必要的英文装饰标签，改为中文或直接省略。

## 6. 文件变更与右侧审查

### 6.1 累计变更模型

当前单次 `edit_file` 返回的 Diff 只描述一次编辑，不能正确表示同一文件连续修改后的最终状态。ToolRuntime 增加会话内变更记录：

```python
file_changes[path] = {
    "before": first_content_before_change,
    "after": latest_content,
    "first_change_seq": int,
    "last_change_seq": int,
}
```

文件第一次修改时固定 `before`，之后只更新 `after`。每次成功写入后重新计算 unified diff、additions 和 deletions。新文件的 `before` 为空字符串。

第一版按 Prompt 展示本轮变化，同时保留 Session 累计变化供刷新恢复。若某文件在本轮开始前已经被前一轮修改，本轮 `before` 取本轮开始时内容，从而避免把上一轮修改重复算入本轮卡片。

### 6.2 事件与快照

新增产品事件：

```json
{
  "type": "file_changed",
  "payload": {
    "prompt_index": 2,
    "path": "expense_tracker.py",
    "diff": "--- a/...",
    "additions": 2,
    "deletions": 2,
    "change_seq": 4
  }
}
```

`GET /api/session` 增加当前 Session 的变更快照，避免 SSE 保留窗口截断后刷新丢失审查状态。

### 6.3 审查交互

- 文件列表展示路径、`+N` 和 `-N`；
- 当前文件有明确选中态；
- Diff 使用等宽字体、行号、红绿背景和横向滚动；
- 第一版显示完整 unified diff，不实现 `N 行未修改` 的 hunk 折叠；
- 审查面板只读，不实现编辑、撤销、暂存或提交；
- ACT 的写入前审批仍用模态框显示待应用 Diff，与写入后的审查面板职责不同。

## 7. 工作方式与权限状态机

### 7.1 两个维度

工作方式与权限分离：

- 工作方式：`AUTO / PLAN / EXECUTE`；
- 执行权限：`ACT / AUTO-ACT`。

`PLAN` 是只读工作流覆盖层。处于 PLAN 时，无论进入前是 ACT 还是 AUTO-ACT，写文件和命令均被拒绝。进入前权限保存在 `pre_plan_mode`，但用户批准计划时可以重新选择目标权限。

### 7.2 Agent 控制工具

新增两个 Agent 控制工具，不计入六个核心工作区工具：

```json
{"name": "enter_plan_mode", "parameters": {}}
{"name": "exit_plan_mode", "parameters": {}}
```

它们由 AgentSession 拦截，不进入 ToolRuntime 的普通文件/命令分发。

`enter_plan_mode`：

- 保存当前执行权限；
- 切换到只读 PLAN；
- 更新 System Prompt；
- 下一轮只暴露读工具和 `exit_plan_mode`；
- 发出 `mode_changed` / `plan_started`；
- 幂等：已在 PLAN 时返回普通 ToolResult。

`exit_plan_mode`：

- 不直接恢复写权限；
- 把当前计划作为 `plan_ready` 事件提交给 WebSession；
- Agent 进入 `WAITING_PLAN_APPROVAL`；
- 等待用户选择；
- 继续规划时把反馈作为工具结果回灌；
- 批准执行时才切到 ACT 或 AUTO-ACT 并继续同一 Session。

### 7.3 自动路由

工作方式为 AUTO 时，由模型根据语义选择：

- 回答、解释、审查、诊断、设计、给方案或明确“先不要改”时进入 PLAN；
- 修改、修复、添加、构建或实现时可直接按选定执行权限工作；
- 复杂或范围不明确的修改任务可以先进入 PLAN；
- 不使用关键词正则或额外分类模型。

进入 PLAN 是权限降低，不需弹窗。任何从 PLAN 到可写状态的迁移必须由用户动作触发。

工作方式为 PLAN 时，Session 在 Prompt 开始前直接进入 PLAN；工作方式为 EXECUTE 时，System Prompt 告诉模型不要主动进入 PLAN，除非遇到需要用户决策的实质性歧义。

### 7.4 计划审批

计划完成后在最终计划下显示：

1. `使用 AUTO-ACT 执行`：普通工作区编辑和已识别验证自动批准；未知命令仍询问；
2. `使用 ACT 执行`：文件 Diff 与命令按 ACT 规则审批；
3. `继续规划`：保持只读，用户输入反馈后继续同一计划轮次。

不实现“清空上下文再执行”。现有两层上下文压缩足以覆盖演示规模，省去 Plan artifact 持久化与上下文重建复杂度。

## 8. 模型选择

模型选择器位于 Composer，与权限选择并列。浏览器只提交模型名称，不接收或展示 API Key。允许值必须由服务端配置或白名单提供，不能把任意字符串直接当作远程模型调用参数。

服务端新增可选配置 `MINICODEX_MODELS`，格式为逗号分隔的模型名；未配置时允许值只有 `MINICODEX_MODEL`。当前模型必须包含在允许值中。只有一个允许值时选择器仍显示但禁用；多个允许值时用户可以选择。模型变更只影响下一 Prompt，并仅能在 `IDLE` 时修改。本设计采用 Prompt 级模型选择，Session 记录每轮实际模型，避免当前选择与历史回答来源混淆。

## 9. SSE 产品事件

保留底层 Trace 事件，Web 增加或调整以下产品事件：

| 事件 | 用途 |
|---|---|
| `user_prompt` | 创建用户消息 |
| `progress` | 一条精炼中文/用户语言进展 |
| `tool_summary` | 确定性工具摘要，可折叠 |
| `command_summary` | 命令状态和简短结果 |
| `file_changed` | 更新本轮累计文件变更 |
| `final_answer` | 渲染最终 Markdown |
| `plan_started` | 显示只读计划状态 |
| `plan_ready` | 显示计划和三个审批动作 |
| `plan_resolved` | 记录执行模式或继续规划 |
| `approval_required` | ACT 文件/命令审批 |
| `verification` | 更新 `NOT_RUN/FAILED/VERIFIED` |
| `turn_completed` | 结束当前 UI 轮次 |

`model_reasoning` 仍可写 JSONL，但 Web sink 默认不发布。`model_message` 中 Tool Call 前的短文本转换为 `progress`；无短文本时使用工具事件摘要，不额外调用模型生成总结。

## 10. 后端组件调整

### `agent.py`

- 中文/跟随用户语言的输出契约；
- Agent 控制工具 Schema 与拦截执行；
- `pre_plan_mode`、plan 状态和等待审批状态；
- 根据工作方式控制自主进入 PLAN；
- 不向 Web 发布原始 reasoning；Trace 继续记录。

### `permissions.py`

- 保持 deny 优先；
- PLAN 作为最高优先级只读覆盖；
- ACT/AUTO-ACT 继续决定执行阶段副作用权限；
- 不能因模型调用 `exit_plan_mode` 直接提升权限。

### `tools.py`

- 记录每 Prompt 首次修改前和最新内容；
- 生成累计 Diff 与增删行数；
- 提供变更快照；
- 保持 read-before-edit、唯一匹配与批量 argv 语义。

### `web/session.py`

- 工作方式、执行权限和模型选择；
- `WAITING_PLAN_APPROVAL` 状态；
- Plan 审批/反馈桥接；
- 快照包含轮次、变更集合、当前选择和待审批信息。

### `web/app.py`

- 工作方式、权限、模型的受限更新 API；
- Plan resolve API 支持 auto-act、act 和 revise；
- 继续保留 token、Host、Origin 和 CSP 防护。

### `web/static/`

- 对话状态存储、过程折叠和最终答案；
- Composer 内选择器；
- 变更汇总与右侧 Diff drawer；
- 响应式布局、键盘焦点和 reduced motion；
- 不使用 `innerHTML` 渲染模型或仓库内容。

## 11. API 调整

接口约定：

| 方法与路径 | 行为 |
|---|---|
| `GET /api/session` | 返回会话、选择器允许值、变更快照和待审批状态 |
| `POST /api/prompts` | 同时提交 text、workflow、permission、model |
| `POST /api/plans/{id}/resolve` | `auto-act / act / revise`；revise 携带反馈 |
| `POST /api/approvals/{id}` | 现有文件/命令审批 |
| `GET /api/events` | SSE 产品事件与断线重放 |

旧 `/api/mode` 和 `/api/plans/approve` 在本次重构中保留并委托给新的状态迁移逻辑，保证现有测试和旧页面调用不立即失效；新前端只调用本节的新请求格式和 `/api/plans/{id}/resolve`。

## 12. 错误与边界

- 模型请求进入 PLAN 后重复调用 `enter_plan_mode`：返回幂等成功摘要。
- 非 PLAN 调用 `exit_plan_mode`：返回结构化 `INVALID_STATE`。
- 等待 Plan 审批时拒绝新普通 Prompt，只接受 resolve 或 revise。
- 页面刷新时从 session snapshot 恢复待审批计划。
- 用户拒绝 ACT Diff：不写文件，错误回灌模型并保留对话状态。
- 累计 Diff 超过 Web 事件限制：快照和事件使用现有截断策略，右侧标明“Diff 已截断”；完整内容留在 Trace/文件系统。
- 二进制和非 UTF-8 文件不进入文本 Diff；展示“内容无法文本预览”。
- AUTO-ACT 仍是应用层权限策略，不宣称 OS 沙箱。

## 13. 视觉规范

外观参考 Codex 的信息层级和布局，不复制完整桌面壳或品牌视觉。

### 颜色

- 页面背景：`#F7F7F5`；
- 主面板：`#FFFFFF`；
- 主文字：`#202124`；
- 次级文字：`#737373`；
- 边框：`#E4E4E0`；
- 选中/焦点：`#2F6FED`；
- 新增文字/边线：`#18864B`；
- 新增背景：`#EAF6EE`；
- 删除文字/边线：`#C74646`；
- 删除背景：`#FBECEC`；
- 警告：`#A15C00`。

### 字体与密度

- 正文优先系统中文无衬线字体；
- 代码、路径、argv 使用等宽字体；
- 不使用大面积全大写 eyebrow 和强字距；
- 消息主体最大宽度约 760px；
- 右侧审查面板桌面宽度约 45%，最小 420px；
- 圆角、阴影和动画保持克制，主要靠留白、分隔线和字重建立层级。

### 可访问性

- 所有按钮和选择器有可见焦点；
- Diff 不只依赖红绿，还使用 `+/-` 和边线；
- `prefers-reduced-motion` 下关闭平滑滚动和抽屉动画；
- 键盘可打开文件、切换 Diff 和关闭面板；
- 移动端 Composer 不遮挡最后一条回答。

## 14. 测试策略

### Python

- Agent 在 AUTO 工作方式下能调用 `enter_plan_mode`；
- PLAN 中只暴露读取与退出控制工具；
- `exit_plan_mode` 不自动提升权限；
- auto-act/act/revise 三种 Plan 结果；
- 模型/权限/工作方式只能在 IDLE 时改变；
- 同一文件多次编辑得到正确累计 Diff；
- 新 Prompt 的本轮 Diff 基线正确重置；
- session snapshot 可恢复变更与待审批计划；
- 原始 reasoning 写 Trace 但不发布到 Web sink。

### JavaScript

- 默认只展示最终回答，执行过程折叠；
- progress 数量与长度受控，不渲染原始 reasoning；
- 文件列表汇总增删行数；
- 点击文件打开并切换右侧 Diff；
- 关闭审查恢复单栏；
- Plan 三个动作请求正确；
- Composer 选择器在忙碌时禁用；
- 所有仓库内容通过安全 DOM API 渲染；
- 刷新重放不会重复最终回答或文件条目。

### 验收演示

1. 输入中文“先分析失败原因，给我方案，不要修改”，Agent 自动进入 PLAN；
2. 页面只显示少量中文进展和最终计划；
3. 点击“使用 AUTO-ACT 执行”；
4. Agent 修改文件并自动运行 pytest；
5. 完成后只展开中文最终回答和 `VERIFIED`；
6. 点击“已编辑 N 个文件”，右侧打开累计 Diff；
7. 下一轮切换为 ACT，展示写入前 Diff 审批；
8. 终端和 JSONL Trace 仍保留完整执行证据。

## 15. 验收标准

- 页面第一视觉中心是对话和最终回答，不是工具日志。
- 完成轮次默认不展示 reasoning、完整命令输出或内联 Diff。
- 用户最多两次点击即可从最终答案进入任一文件 Diff。
- 页面普通说明在中文会话中为中文，允许稳定英文状态和技术原文自然保留。
- ACT/AUTO-ACT 与模型选择位于 Composer，交互方式接近 Codex。
- Agent 可自主进入 PLAN，但没有任何路径可绕过用户直接恢复写权限。
- 右侧展示累计 Diff，而非最后一次局部编辑 Diff。
- 当前安全边界、批量 argv、read-before-edit、验证状态、SSE 重放和终端输出不回归。
- 主测试套件和前端行为测试全部通过。

## 16. 明确不做的后续项

- Plan 文件持久化与 clear-and-execute；
- Git staging、撤销和 commit UI；
- 项目级模型列表管理界面；
- 原始 reasoning 浏览器查看器；
- AST Shell 分析和 OS 级沙箱；
- 多 Agent、MCP 和 IDE 插件。

这些功能只有在现有演示验证出明确价值后再考虑，不能挤占本轮“对话清晰、变更可审查、Plan 权限正确”三个核心目标。
