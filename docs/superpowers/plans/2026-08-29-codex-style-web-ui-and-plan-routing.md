# MiniCodex Codex 式 Web UI 与自主 Plan 路由 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MiniCodex 重构为结果优先的 Codex 式本机对话界面，支持右侧累计 Diff 审查，以及由 Agent 自主进入、用户批准后按当前 ACT/AUTO-ACT 执行的临时 PLAN 阶段。

**Architecture:** 保留单 Agent、六个工作区工具、FastAPI、SSE 和原生前端。`execution_mode` 持久保存 ACT/AUTO-ACT，`plan_state` 独立表示 INACTIVE/PLANNING/WAITING_APPROVAL；ToolRuntime 记录每轮文件首版与最新版生成累计 Diff；Web sink 把底层事件投影成精炼产品事件，前端只渲染对话、折叠过程、最终答案和变更审查。

**Tech Stack:** Python 3.11、OpenAI-compatible Chat Completions、FastAPI、SSE、原生 HTML/CSS/JavaScript、pytest、Node.js 行为测试。

**Spec:** `docs/superpowers/specs/2026-08-29-codex-style-web-ui-and-plan-routing-design.md`

## Global Constraints

- 继续使用原生 HTML/CSS/JavaScript，不增加 React、Vue、Node 构建链或前端依赖。
- 保留六个核心工作区工具；`enter_plan_mode`/`exit_plan_mode` 是 Agent 控制工具。
- 用户只选择 ACT/AUTO-ACT；PLAN 不出现在持久权限菜单。
- PLAN 中禁止文件修改和命令；批准执行后按未被覆盖的当前 execution mode 继续。
- 不直接向 Web 发布完整 `reasoning_content`；JSONL Trace 继续记录。
- 中文会话中的普通说明与进展使用中文；技术状态、代码、路径、argv、stdout/stderr 和错误原文不强制翻译。
- `.env`、Workspace Boundary、read-before-edit、唯一匹配、批量 argv、API Key 隔离和 deny 优先规则不得回归。
- 保留终端输出；Web 仅重构展示层和产品事件。
- 用户现有 `demo/buggy_expense_tracker/expense_tracker.py` 与其测试修改不得暂存或覆盖。

---

### Task 1: 临时 PLAN 状态机与 Agent 控制工具

**Files:**
- Modify: `src/minicodex/permissions.py`
- Modify: `src/minicodex/agent.py`
- Test: `tests/test_permissions.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Produces: `PlanState(str, Enum)` with `INACTIVE`, `PLANNING`, `WAITING_APPROVAL`.
- Produces: `AgentSession.execution_mode: AgentMode` restricted to ACT/AUTO_ACT.
- Produces: `AgentSession.plan_state: PlanState`.
- Produces: `AgentSession.enter_plan_mode(call_id: str) -> ToolResult` and `AgentSession.request_plan_approval(call_id: str, plan_text: str) -> ToolResult`.
- Produces: `AgentSession.resume_plan(execute: bool, feedback: str | None = None) -> None` for WebSession.
- Consumes: existing `ToolRuntime.set_mode()` as the effective permission setter.

- [ ] **Step 1: Write failing permission and Agent tests**

Add tests proving the production breaks if PLAN overwrites the selected mode or exposes mutation tools:

```python
def test_plan_overlay_keeps_selected_auto_act_mode(tmp_path: Path) -> None:
    tools = ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT)
    session = AgentSession(MockModel([]), tools)

    session.enter_plan_mode("enter")

    assert session.execution_mode is AgentMode.AUTO_ACT
    assert session.plan_state is PlanState.PLANNING
    assert tools.mode is AgentMode.PLAN


def test_plan_tools_allow_read_and_exit_control_only(tmp_path: Path) -> None:
    model = MockModel([ModelReply(content="方案", tool_calls=[])])
    session = AgentSession(model, ToolRuntime(tmp_path, approver=lambda _: True, mode=AgentMode.ACT))
    session.enter_plan_mode("enter")

    names = {schema["function"]["name"] for schema in session._tool_schemas()}

    assert names == {"list_files", "search_text", "read_file", "exit_plan_mode"}
```

Add a model-driven test where `enter_plan_mode` is returned as a ToolCall and verify no ToolRuntime unknown-tool failure occurs.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_permissions.py tests/test_agent.py -q
```

Expected: fail because `PlanState`, control schemas and overlay state do not exist.

- [ ] **Step 3: Implement the minimal state model**

Add:

```python
class PlanState(str, Enum):
    INACTIVE = "inactive"
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"
```

Keep `AgentMode.PLAN` as the effective ToolRuntime permission for compatibility, but prevent callers from treating it as the persistent execution selection. Add control schemas separate from `TOOL_SCHEMAS`:

```python
PLAN_CONTROL_SCHEMAS = [
    function_schema("enter_plan_mode", "Enter a temporary read-only planning phase."),
    function_schema(
        "exit_plan_mode",
        "Submit the completed plan for user approval.",
        required_string="plan",
    ),
]
```

`AgentSession._tool_schemas()` must return normal tools plus `enter_plan_mode` while inactive, and read tools plus `exit_plan_mode` while planning. Intercept both names before `ToolRuntime.execute()`. `exit_plan_mode` must carry the complete Markdown plan in its required `plan` argument so WebSession never has to infer it from conversational text.

- [ ] **Step 4: Add intent and language contract to the System Prompt**

State once:

```text
Follow the user's language for progress, plans, and final answers. For Chinese requests, use Chinese except for code, paths, commands, API names, errors, and stable status labels.
For answer, explanation, review, diagnosis, design, or explicit plan-only requests, call enter_plan_mode before exploring. For clear fix/build/change requests, execute directly unless the user asks to plan first.
Before tool calls, expose at most one short progress sentence; do not expose a full chain of thought.
```

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the same focused pytest command. Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add src/minicodex/permissions.py src/minicodex/agent.py tests/test_permissions.py tests/test_agent.py
git commit -m "feat: add temporary agent plan state"
```

### Task 2: Plan 完成、执行、反馈与取消

**Files:**
- Modify: `src/minicodex/web/session.py`
- Modify: `src/minicodex/web/app.py`
- Modify: `tests/test_web_session.py`
- Modify: `tests/test_web_api.py`

**Interfaces:**
- Produces: `PendingPlan(id: str, text: str, execution_mode: AgentMode)`.
- Produces: `WebSession.resolve_plan(plan_id: str, action: Literal["execute", "revise", "cancel"], feedback: str | None) -> None`.
- Produces: `POST /api/plans/{plan_id}/resolve` body `{action, feedback?}`.
- Preserves: `/api/mode` and `/api/plans/approve` compatibility routes.

- [ ] **Step 1: Write failing WebSession tests**

Cover three observable branches with real WebSession state:

```python
def test_execute_pending_plan_uses_existing_auto_act_mode(tmp_path) -> None:
    web = make_web_session(tmp_path, ReplyModel([]), mode=AgentMode.AUTO_ACT)
    plan = web.mark_plan_ready("先修改实现，再运行测试")

    web.resolve_plan(plan.id, "execute")

    assert web.agent.execution_mode is AgentMode.AUTO_ACT
    assert web.agent.plan_state is PlanState.INACTIVE


def test_plan_feedback_stays_read_only_and_reenters_agent(tmp_path) -> None:
    ...
    web.resolve_plan(plan.id, "revise", "保持旧 API 兼容")
    assert web.agent.plan_state is PlanState.PLANNING


def test_cancel_plan_returns_idle_without_starting_implementation(tmp_path) -> None:
    ...
```

- [ ] **Step 2: Write failing API tests**

Assert execute/revise/cancel status codes, invalid action 422, stale ID 404/409, and snapshot fields:

```json
{
  "execution_mode": "auto-act",
  "plan_state": "waiting_approval",
  "pending_plan": {"id": "...", "text": "...", "execution_mode": "auto-act"}
}
```

- [ ] **Step 3: Run Web tests and verify RED**

```powershell
python -m pytest tests/test_web_session.py tests/test_web_api.py -q
```

Expected: missing PendingPlan/resolve endpoint failures.

- [ ] **Step 4: Implement PendingPlan and resolver**

Use one pending plan per WebSession. `execute` appends a deterministic implementation instruction and starts the worker under the unchanged execution mode. `revise` appends user feedback to the same Agent history and resumes read-only planning. `cancel` clears pending state and leaves the session IDLE.

Typed user text while a plan is pending is handled as revise feedback unless it normalizes to an explicit execution acknowledgement (`执行`, `执行方案`, `开始执行`, case-insensitive `execute`). Keep the button/API as the deterministic primary path.

- [ ] **Step 5: Run Web tests and verify GREEN**

Run the focused command. Expected: pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add src/minicodex/web/session.py src/minicodex/web/app.py tests/test_web_session.py tests/test_web_api.py
git commit -m "feat: add plan approval handoff"
```

### Task 3: 每轮累计文件变更

**Files:**
- Modify: `src/minicodex/tools.py`
- Modify: `src/minicodex/agent.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Produces: `FileChange(path, before, after, additions, deletions, diff, first_change_seq, last_change_seq)` serialization.
- Produces: `ToolRuntime.begin_prompt(prompt_index: int) -> None`.
- Produces: `ToolRuntime.changes_snapshot(prompt_index: int | None = None) -> list[dict[str, Any]]`.
- Emits: `file_changed` after every successful write/edit.

- [ ] **Step 1: Write failing cumulative Diff tests**

Use a real temporary file, read it, edit it twice and hand-check the final literals:

```python
def test_multiple_edits_report_one_diff_from_prompt_start_to_latest_content(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")
    tools = runtime(tmp_path)
    tools.begin_prompt(1)
    tools.read_file("r", "app.py")
    tools.edit_file("e1", "app.py", "1", "2")
    tools.edit_file("e2", "app.py", "2", "3")

    change = tools.changes_snapshot(1)[0]
    assert "-value = 1" in change["diff"]
    assert "+value = 3" in change["diff"]
    assert "+value = 2" not in change["diff"]
    assert (change["additions"], change["deletions"]) == (1, 1)
```

Add a second test proving prompt 2 uses prompt 1 final content as its baseline.

- [ ] **Step 2: Run Tool tests and verify RED**

```powershell
python -m pytest tests/test_tools.py tests/test_agent.py -q
```

Expected: missing begin_prompt/changes_snapshot.

- [ ] **Step 3: Implement FileChange tracking**

Store per-prompt records keyed by resolved path. Capture `before` before the first approved write in that prompt and update `after` after each successful write. Count only lines beginning `+`/`-` excluding unified diff headers. Include `path`, `prompt_index`, `change_seq`, `diff`, `additions`, `deletions` in ToolResult data.

- [ ] **Step 4: Emit and snapshot cumulative changes**

AgentSession calls `tools.begin_prompt(self.prompt_count)` after incrementing prompt count. Replace the old local `diff` event with `file_changed` carrying the cumulative record; optionally keep `diff` as a compatibility alias until the new frontend lands.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the focused command. Expected: pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/minicodex/tools.py src/minicodex/agent.py tests/test_tools.py tests/test_agent.py
git commit -m "feat: track cumulative prompt diffs"
```

### Task 4: 产品事件、中文进展与 reasoning 隔离

**Files:**
- Modify: `src/minicodex/agent.py`
- Modify: `src/minicodex/web_cli.py`
- Modify: `src/minicodex/web/session.py`
- Modify: `tests/test_agent.py`
- Modify: `tests/test_web_cli.py`
- Modify: `tests/test_web_session.py`

**Interfaces:**
- Produces Web events: `progress`, `tool_summary`, `command_summary`, `file_changed`, `final_answer`, `plan_started`, `plan_ready`, `plan_resolved`.
- Preserves Trace events: `model_reply.reasoning_content`, tool_result, final.
- Produces: `summarize_tool_result(result: ToolResult) -> dict[str, Any]` deterministic payload.

- [ ] **Step 1: Write failing event projection tests**

Prove that a reply containing reasoning, short content and a tool call writes reasoning to Trace but publishes only the short content as progress. Assert tool summaries from actual ToolResult fields rather than hardcoded source strings.

```python
assert not any(kind == "model_reasoning" for kind, _ in events)
assert ("progress", {"text": "正在检查失败测试", "turn": 1}) in events
assert any(kind == "tool_summary" and payload["text"].startswith("已读取") for kind, payload in events)
```

- [ ] **Step 2: Run event tests and verify RED**

```powershell
python -m pytest tests/test_agent.py tests/test_web_cli.py tests/test_web_session.py -q
```

Expected: old `model_reasoning` publication and missing product events.

- [ ] **Step 3: Implement deterministic summaries**

Map tool payloads without translating technical content. Examples: `已读取 {path}`, `已修改 {path}`, `测试通过 · {last non-empty stdout line}`, `命令失败 · exit code {code}`. Full result remains in Trace and nested command detail payload.

- [ ] **Step 4: Separate terminal and Web sinks**

Terminal may continue showing `[thinking:turn N]` for developer evidence. `publish_agent_event()` filters raw reasoning from EventBus and projects supported event types. Do not remove reasoning from ModelReply or SessionTrace.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the focused command. Expected: pass.

- [ ] **Step 6: Commit Task 4**

```powershell
git add src/minicodex/agent.py src/minicodex/web_cli.py src/minicodex/web/session.py tests/test_agent.py tests/test_web_cli.py tests/test_web_session.py
git commit -m "feat: publish concise web progress events"
```

### Task 5: 模型允许列表与 Composer 会话参数

**Files:**
- Modify: `src/minicodex/config.py`
- Modify: `src/minicodex/model_adapter.py`
- Modify: `src/minicodex/web/session.py`
- Modify: `src/minicodex/web/app.py`
- Modify: `tests/test_core.py`
- Modify: `tests/test_model_adapter.py`
- Modify: `tests/test_web_api.py`

**Interfaces:**
- Produces: `Config.allowed_models: tuple[str, ...]`.
- Produces: `OpenAIChatModel.set_model(model: str) -> None` validating against allowed values at WebSession boundary.
- Changes `PromptRequest` to `{text: str, permission?: AgentMode, model?: str}` where permission excludes PLAN.

- [ ] **Step 1: Write failing config and API tests**

Cover missing `MINICODEX_MODELS`, comma trimming/deduplication, configured default inclusion, allowed selection and rejected unknown model. Assert PLAN is rejected as a persistent permission selection.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
python -m pytest tests/test_core.py tests/test_model_adapter.py tests/test_web_api.py -q
```

- [ ] **Step 3: Implement allowlist and prompt-scoped selection**

Parse `MINICODEX_MODELS`; default to `(config.model,)`; reject configuration where the default is absent only by inserting the default first, avoiding startup breakage. Snapshot returns `model`, `allowed_models`, `execution_mode`, `plan_state`.

- [ ] **Step 4: Run focused tests and verify GREEN**

- [ ] **Step 5: Commit Task 5**

```powershell
git add src/minicodex/config.py src/minicodex/model_adapter.py src/minicodex/web/session.py src/minicodex/web/app.py tests/test_core.py tests/test_model_adapter.py tests/test_web_api.py
git commit -m "feat: add composer model selection"
```

### Task 6: Codex 式对话与右侧 Diff 审查

**Files:**
- Replace structure in: `src/minicodex/web/static/index.html`
- Rewrite behavior in: `src/minicodex/web/static/app.js`
- Rewrite presentation in: `src/minicodex/web/static/app.css`
- Modify: `tests/js/turn_timeline_test.cjs`
- Modify: `tests/test_frontend_timeline.py`
- Modify: `tests/test_web_static.py`

**Interfaces:**
- Frontend state: `turns`, `currentTurn`, `changedFilesByPrompt`, `selectedDiffPath`, `reviewOpen`, `pendingPlan`.
- DOM regions: `#conversation`, `#review-panel`, `#review-file-list`, `#review-diff`, `#prompt-input`, `#permission-select`, `#model-select`.
- Consumes product events from Task 4 and snapshot fields from Tasks 2/3/5.

- [ ] **Step 1: Extend the fake DOM only as needed by behavior tests**

Do not assert source strings. Drive the real `app.js` event handlers through exported test hooks and inspect user-visible DOM state.

- [ ] **Step 2: Write failing frontend behavior tests**

Required behaviors:

```javascript
emit("user_prompt", {prompt_index: 1, text: "修复测试"});
emit("progress", {text: "正在定位失败原因"});
emit("file_changed", {prompt_index: 1, path: "app.py", diff: "...", additions: 2, deletions: 1});
emit("final_answer", {text: "已修复。", turns: 4, verification_status: "VERIFIED"});

assert.equal(text("#final-answer-1"), "已修复。");
assert.equal(isOpen("#process-1"), false);
clickFile("app.py");
assert.equal(isVisible("#review-panel"), true);
assert.match(text("#review-diff"), /\+2/);
```

Also assert raw `model_reasoning` events create no card, Plan card uses current AUTO-ACT label and one primary “执行方案” button, and mode/model selectors are disabled while busy.

- [ ] **Step 3: Run JS tests and verify RED**

```powershell
python -m pytest tests/test_frontend_timeline.py tests/test_web_static.py -q
```

Expected: missing new DOM and behavior.

- [ ] **Step 4: Build the semantic HTML shell**

Use one compact topbar, `main.app-layout`, `section.conversation-pane`, `aside.review-panel`, and fixed Composer. No project sidebar, tabs, staging, commit or undo controls. Use Chinese labels except stable technical states.

- [ ] **Step 5: Implement event reducer and rendering**

Keep one state update path per event. Completed turns show user text, collapsed process, final Markdown and change summary. Clicking a file renders its cumulative diff in the right panel. On narrow screens add `.review-panel[data-open="true"]` as a full-height drawer.

- [ ] **Step 6: Implement the visual token system**

Use CSS variables from the spec (`#F7F7F5`, `#FFFFFF`, `#202124`, `#737373`, `#E4E4E0`, `#2F6FED`, add/delete colors). Use system Chinese UI fonts and a monospace stack for code. The signature interaction is the answer-adjacent changed-files card opening the review pane. Respect visible focus and `prefers-reduced-motion`.

- [ ] **Step 7: Run frontend tests and verify GREEN**

Run the focused command plus:

```powershell
node --check src/minicodex/web/static/app.js
```

- [ ] **Step 8: Commit Task 6**

```powershell
git add src/minicodex/web/static/index.html src/minicodex/web/static/app.js src/minicodex/web/static/app.css tests/js/turn_timeline_test.cjs tests/test_frontend_timeline.py tests/test_web_static.py
git commit -m "feat: redesign web console around code review"
```

### Task 7: 文档、集成回归与本机视觉验证

**Files:**
- Modify: `README.md`
- Modify: `CODE_WALKTHROUGH.md`
- Modify: `demo/buggy_expense_tracker/MULTI_TURN_DEMO.md`
- Modify other source/tests only for integration failures with a reproducing test first.

**Interfaces:**
- Documents final user workflow and truthful security limits.

- [ ] **Step 1: Run the complete automated suite**

```powershell
python -m pytest -p no:cacheprovider --basetemp "$env:TEMP\minicodex-codex-ui"
node --check src/minicodex/web/static/app.js
git diff --check
```

Expected: all Python/Node tests pass; no whitespace errors.

- [ ] **Step 2: Fix any integration failure through RED/GREEN**

For each failure, add or narrow a test that reproduces the user-visible regression, observe it fail, then make the smallest production correction.

- [ ] **Step 3: Update user documentation**

Document:

- ACT/AUTO-ACT only in Composer;
- Agent may temporarily enter PLAN;
- “执行方案” uses the current execution mode;
- plan feedback remains read-only;
- final answer/process/Diff information hierarchy;
- `MINICODEX_MODELS`;
- Web hides raw reasoning while Trace retains it;
- cumulative Diff is per Prompt;
- AUTO-ACT remains application policy, not an OS sandbox.

- [ ] **Step 4: Start the local Web app and perform visual QA**

Run against the demo Workspace. Verify desktop single-pane, review split-pane, narrow drawer, Composer selectors, Plan card, collapsed process, final answer, changed-files card, keyboard focus and no console errors. Take local screenshots for inspection; do not commit screenshots unless explicitly requested.

- [ ] **Step 5: Re-run the complete suite after docs and visual fixes**

Use the commands from Step 1 and record exact counts.

- [ ] **Step 6: Commit Task 7 without user demo modifications**

Stage exact feature, test and documentation paths. Confirm `git diff --cached --name-status` excludes:

```text
demo/buggy_expense_tracker/expense_tracker.py
demo/buggy_expense_tracker/tests/test_expense_tracker.py
```

Then commit:

```powershell
git commit -m "docs: explain codex-style web workflow"
```

- [ ] **Step 7: Report final evidence**

Report test count, JS syntax result, visual QA result, commits, unpushed state and preserved user modifications. Do not push unless the user explicitly asks.
