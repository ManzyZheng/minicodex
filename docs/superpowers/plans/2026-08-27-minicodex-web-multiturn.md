# MiniCodex Web UI and Multi-turn Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a localhost-only FastAPI UI that supports one persistent multi-turn Agent session, streams events with SSE, shows command output and unified diffs, and performs command approval in the browser while preserving the terminal CLI.

**Architecture:** Refactor the current one-shot loop into `AgentSession.run_turn()` with persistent messages and ToolRuntime state. A single `WebSession` owns one worker thread, in-memory `EventBus`, and blocking `ApprovalGate`; FastAPI exposes prompt/approval endpoints and an SSE stream. Static vanilla HTML/CSS/JS renders the event timeline.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, OpenAI-compatible Chat Completions, threading primitives, SSE, vanilla HTML/CSS/JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-minicodex-web-multiturn-design.md`

## Global Constraints

- Bind only to `127.0.0.1`; do not expose a host override.
- Maintain one Workspace, one Agent Session, and one Agent worker thread.
- Allow at most 20 model calls per user Prompt; reset the counter per Prompt.
- Keep the existing three-identical-failed-call stop and 80,000-character history compaction.
- Send an SSE heartbeat every 15 seconds and retain events in memory for `Last-Event-ID` replay.
- Wait at most 300 seconds for web approval; reject on timeout or shutdown.
- Keep command timeout validation at 1–120 seconds and continue stripping API Keys from child environments.
- Do not add React, Vue, Node.js, a database, WebSocket, auth, file upload, or browser-side source editing.
- Preserve the existing `minicodex` CLI and its terminal command approval.
- Render untrusted model and tool text with `textContent`, never raw `innerHTML`.

---

### Task 1: Persistent AgentSession

**Files:**
- Modify: `src/minicodex/agent.py`
- Modify: `src/minicodex/models.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Produces: `AgentSession(model, tools, *, max_turns_per_prompt=20, trace=None, on_event=None)`.
- Produces: `AgentSession.run_turn(prompt: str) -> AgentOutcome`.
- Preserves: `Agent(...).run(task)` as a one-shot compatibility wrapper.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_agent_session_keeps_messages_between_prompts(tmp_path):
    model = RecordingModel([
        ModelReply(content="first done"),
        ModelReply(content="second done"),
    ])
    session = AgentSession(model, ToolRuntime(tmp_path, command_approver=allow), max_turns_per_prompt=20)
    session.run_turn("first task")
    session.run_turn("second task")
    assert any(m.get("content") == "first task" for m in model.messages_seen[1])
    assert any(m.get("content") == "first done" for m in model.messages_seen[1])
    assert model.messages_seen[1][-1]["content"] == "second task"

def test_agent_session_resets_turn_limit_per_prompt(tmp_path):
    session = AgentSession(two_completed_replies(), runtime(tmp_path), max_turns_per_prompt=1)
    assert session.run_turn("one").stop_reason is StopReason.COMPLETED
    assert session.run_turn("two").stop_reason is StopReason.COMPLETED
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest tests/test_agent.py -q --basetemp=.pytest-tmp-web-red-1`

Expected: import or attribute failure because `AgentSession` does not exist.

- [ ] **Step 3: Implement persistent state and one-shot wrapper**

Move `messages`, compaction, tool-call fingerprints, verification nudge, and per-Prompt loop into `AgentSession`. Append each new user Prompt to the existing message list. Reset only turn count, repeated-call count, previous fingerprint, and verification-nudge flag at the start of `run_turn`. Implement `Agent.run(task)` by creating a temporary `AgentSession` and calling `run_turn(task)`.

- [ ] **Step 4: Run Agent and full regression tests**

Run: `python -m pytest tests/test_agent.py -q --basetemp=.pytest-tmp-web-green-1`

Expected: all Agent tests pass, including old one-shot cases.

- [ ] **Step 5: Commit**

```powershell
git add src/minicodex/agent.py src/minicodex/models.py tests/test_agent.py
git commit -m "feat: add persistent multi-turn agent sessions"
```

### Task 2: Typed Web Events and EventBus

**Files:**
- Create: `src/minicodex/web/__init__.py`
- Create: `src/minicodex/web/events.py`
- Create: `tests/test_web_events.py`

**Interfaces:**
- Produces: `WebEvent(id: int, type: str, timestamp: str, payload: dict[str, Any])`.
- Produces: `EventBus.publish(type: str, payload: dict) -> WebEvent`.
- Produces: `EventBus.after(last_id: int) -> list[WebEvent]`.
- Produces: `EventBus.wait_after(last_id: int, timeout: float) -> list[WebEvent]`.

- [ ] **Step 1: Write failing EventBus tests**

```python
def test_event_bus_assigns_ids_and_replays_after_cursor():
    bus = EventBus()
    bus.publish("status", {"value": "RUNNING"})
    second = bus.publish("diff", {"path": "a.py"})
    assert second.id == 2
    assert [e.type for e in bus.after(1)] == ["diff"]

def test_event_bus_wait_wakes_for_new_event():
    bus = EventBus()
    threading.Timer(0.02, lambda: bus.publish("status", {"value": "IDLE"})).start()
    assert bus.wait_after(0, timeout=0.2)[0].type == "status"
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest tests/test_web_events.py -q --basetemp=.pytest-tmp-web-red-2`

Expected: `minicodex.web.events` is missing.

- [ ] **Step 3: Implement EventBus with a Condition**

Keep an in-memory list, a monotonically increasing counter, and `threading.Condition`. Serialize events with `asdict`; timestamps use UTC ISO 8601. Return copies of matching event slices so consumers do not mutate storage.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_web_events.py -q --basetemp=.pytest-tmp-web-green-2`

Expected: all EventBus tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/minicodex/web tests/test_web_events.py
git commit -m "feat: add in-memory web event bus"
```

### Task 3: Browser ApprovalGate

**Files:**
- Create: `src/minicodex/web/approval.py`
- Create: `tests/test_web_approval.py`

**Interfaces:**
- Consumes: `EventBus.publish()`.
- Produces: `ApprovalGate.request(argv: list[str], purpose: str, timeout_sec: int) -> bool`.
- Produces: `ApprovalGate.resolve(request_id: str, allow: bool) -> bool`.
- Produces: `ApprovalGate.pending() -> ApprovalRequest | None`.
- Produces: `ApprovalGate.close() -> None`.

- [ ] **Step 1: Write allow, timeout, and stale-ID tests**

```python
def test_approval_gate_blocks_until_matching_resolution():
    gate = ApprovalGate(EventBus(), wait_timeout=0.5)
    result = []
    thread = Thread(target=lambda: result.append(gate.request(["pytest"], "test", 30)))
    thread.start()
    request = wait_for_pending(gate)
    assert gate.resolve(request.id, True)
    thread.join()
    assert result == [True]

def test_approval_gate_rejects_on_timeout():
    gate = ApprovalGate(EventBus(), wait_timeout=0.01)
    assert gate.request(["pytest"], "test", 30) is False
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest tests/test_web_approval.py -q --basetemp=.pytest-tmp-web-red-3`

Expected: approval module missing.

- [ ] **Step 3: Implement one-pending-request gate**

Use a lock and Condition. Generate opaque IDs with `uuid4().hex`; publish `approval_required` before waiting and `approval_resolved` for allow, reject, timeout, or shutdown. Reject attempts to open a second pending approval.

- [ ] **Step 4: Run approval tests**

Run: `python -m pytest tests/test_web_approval.py -q --basetemp=.pytest-tmp-web-green-3`

Expected: all tests pass without sleeps longer than 0.5 seconds.

- [ ] **Step 5: Commit**

```powershell
git add src/minicodex/web/approval.py tests/test_web_approval.py
git commit -m "feat: add browser command approval gate"
```

### Task 4: Agent Event Hooks and Console Fan-out

**Files:**
- Modify: `src/minicodex/agent.py`
- Modify: `src/minicodex/cli.py`
- Modify: `src/minicodex/tools.py`
- Modify: `tests/test_agent.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: event callback `Callable[[str, dict[str, Any]], None]` on AgentSession.
- Emits: `user_prompt`, `model_message`, `tool_call`, `tool_result`, `diff`, `command_output`, `verification`, `turn_completed`, and `error`.
- Preserves: existing `on_tool_result` behavior and terminal formatting.

- [ ] **Step 1: Write failing event-order test**

```python
def test_agent_session_emits_tool_diff_and_completion_events(tmp_path):
    events = []
    session = AgentSession(scripted_edit_model(), runtime(tmp_path), on_event=lambda t, p: events.append((t, p)))
    session.run_turn("change file")
    types = [event[0] for event in events]
    assert types.index("user_prompt") < types.index("tool_call")
    assert "tool_result" in types
    assert "diff" in types
    assert types[-1] == "turn_completed"
```

- [ ] **Step 2: Run test and confirm RED**

Run: `python -m pytest tests/test_agent.py -q --basetemp=.pytest-tmp-web-red-4`

Expected: `on_event` unsupported or required event types absent.

- [ ] **Step 3: Emit stable events without removing terminal output**

Emit tool-call events before execution, tool result afterward, a separate diff when `data.diff` exists, command output for `run_command`, and verification whenever the runtime state changes. Keep `print_tool_result()` unchanged for CLI; WebSession will attach both console and EventBus observers.

- [ ] **Step 4: Run Agent/CLI regression tests**

Run: `python -m pytest tests/test_agent.py tests/test_cli.py -q --basetemp=.pytest-tmp-web-green-4`

Expected: new event test and existing terminal compatibility tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/minicodex/agent.py src/minicodex/cli.py src/minicodex/tools.py tests/test_agent.py tests/test_cli.py
git commit -m "feat: emit agent lifecycle events"
```

### Task 5: WebSession Worker Orchestration

**Files:**
- Create: `src/minicodex/web/session.py`
- Create: `tests/test_web_session.py`

**Interfaces:**
- Consumes: `AgentSession`, `EventBus`, `ApprovalGate`, and console formatter.
- Produces: `WebSession.submit_prompt(text: str) -> None` raising `SessionBusyError` when active.
- Produces: `WebSession.snapshot() -> dict[str, Any]`.
- Produces: `WebSession.resolve_approval(id: str, allow: bool) -> bool`.
- Produces: `WebSession.close() -> None`.

- [ ] **Step 1: Write worker and busy-state tests**

```python
def test_web_session_runs_prompt_and_returns_to_idle(tmp_path):
    web = make_web_session(tmp_path, replies=[ModelReply(content="done")])
    web.submit_prompt("hello")
    wait_until(lambda: web.snapshot()["status"] == "IDLE")
    assert any(e.type == "turn_completed" for e in web.events.after(0))

def test_web_session_rejects_second_prompt_while_running(tmp_path):
    web = make_blocked_web_session(tmp_path)
    web.submit_prompt("first")
    with pytest.raises(SessionBusyError):
        web.submit_prompt("second")
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest tests/test_web_session.py -q --basetemp=.pytest-tmp-web-red-5`

Expected: WebSession missing.

- [ ] **Step 3: Implement one daemon worker per WebSession**

Validate non-empty Prompt length, set `RUNNING`, start a daemon thread, call persistent `AgentSession.run_turn`, publish status transitions, and return to `IDLE` in `finally`. Wire the runtime command approver to `ApprovalGate.request`. Console observer calls existing terminal formatting functions.

- [ ] **Step 4: Run WebSession tests**

Run: `python -m pytest tests/test_web_session.py -q --basetemp=.pytest-tmp-web-green-5`

Expected: state, busy rejection, multi-turn reuse, and close tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/minicodex/web/session.py tests/test_web_session.py
git commit -m "feat: orchestrate one local web agent session"
```

### Task 6: FastAPI API and SSE Stream

**Files:**
- Create: `src/minicodex/web/app.py`
- Create: `tests/test_web_api.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `create_app(web_session: WebSession) -> FastAPI`.
- Endpoints: `/`, `/assets/app.css`, `/assets/app.js`, `/api/session`, `/api/events`, `/api/prompts`, `/api/approvals/{id}`, `/api/interrupt`.

- [ ] **Step 1: Add FastAPI/Uvicorn dependencies and failing endpoint tests**

```python
def test_prompt_endpoint_accepts_idle_and_rejects_busy(client, web_session):
    assert client.post("/api/prompts", json={"text": "first"}).status_code == 202
    assert client.post("/api/prompts", json={"text": "second"}).status_code == 409

def test_sse_formats_event_id_type_and_json_data(client, web_session):
    web_session.events.publish("status", {"value": "IDLE"})
    chunk = read_first_sse_event(client, "/api/events")
    assert "id: 1" in chunk
    assert "event: status" in chunk
    assert 'data: {"value":"IDLE"}' in chunk
```

- [ ] **Step 2: Run API tests and confirm RED**

Run: `python -m pytest tests/test_web_api.py -q --basetemp=.pytest-tmp-web-red-6`

Expected: app module or endpoints missing.

- [ ] **Step 3: Implement API validation and StreamingResponse**

Use Pydantic request bodies, HTTP 202 for accepted Prompt, 409 for busy/stale approvals, and 404 for unknown approval ID. Parse `Last-Event-ID` safely. The SSE generator replays queued events, waits up to 15 seconds, emits heartbeat on timeout, and stops on client cancellation.

- [ ] **Step 4: Run API tests**

Run: `python -m pytest tests/test_web_api.py -q --basetemp=.pytest-tmp-web-green-6`

Expected: route, validation, SSE formatting, replay cursor, approval, and static-resource tests pass.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml src/minicodex/web/app.py tests/test_web_api.py
git commit -m "feat: expose local FastAPI and SSE endpoints"
```

### Task 7: Minimal Timeline Frontend

**Files:**
- Create: `src/minicodex/web/static/index.html`
- Create: `src/minicodex/web/static/app.css`
- Create: `src/minicodex/web/static/app.js`
- Create: `tests/test_web_static.py`

**Interfaces:**
- Consumes: the API and event types defined in Tasks 4 and 6.
- Produces: status header, timeline cards, diff renderer, approval dialog, and Prompt composer.

- [ ] **Step 1: Read `frontend-design` skill and define the visual direction**

Use an engineering-workbench visual language: quiet warm-gray canvas, ink typography, monospace technical details, restrained green/red/amber status colors, and a strong central event timeline. Avoid generic gradients, excessive cards, and dashboard clutter.

- [ ] **Step 2: Write failing static contract tests**

```python
def test_index_has_status_timeline_composer_and_approval_dialog(static_files):
    html = static_files.index
    assert 'id="timeline"' in html
    assert 'id="prompt-input"' in html
    assert 'id="approval-dialog"' in html

def test_javascript_uses_eventsource_and_safe_text_rendering(static_files):
    js = static_files.javascript
    assert "new EventSource(" in js
    assert ".textContent" in js
    assert ".innerHTML" not in js
```

- [ ] **Step 3: Run tests and confirm RED**

Run: `python -m pytest tests/test_web_static.py -q --basetemp=.pytest-tmp-web-red-7`

Expected: static files missing.

- [ ] **Step 4: Implement the minimal page**

Build semantic HTML, responsive CSS, and small event handlers. Render messages and tool cards with `document.createElement`; render diff lines by prefix with CSS classes; POST Prompt and approval actions; disable the composer while status is RUNNING or WAITING_APPROVAL; reconnect SSE automatically through EventSource.

- [ ] **Step 5: Run static and API tests**

Run: `python -m pytest tests/test_web_static.py tests/test_web_api.py -q --basetemp=.pytest-tmp-web-green-7`

Expected: static contracts and route serving pass.

- [ ] **Step 6: Start locally and inspect in browser**

Run: `minicodex-web --workspace demo/multi_turn_expense_tracker` after Task 8, then verify desktop layout, narrow layout, long command output, multiline Diff, approval dialog, and reconnect behavior.

- [ ] **Step 7: Commit**

```powershell
git add src/minicodex/web/static tests/test_web_static.py
git commit -m "feat: add minimal MiniCodex web timeline"
```

### Task 8: Web CLI Entry Point

**Files:**
- Create: `src/minicodex/web_cli.py`
- Modify: `pyproject.toml`
- Create: `tests/test_web_cli.py`

**Interfaces:**
- Produces executable: `minicodex-web --workspace PATH --model NAME --max-turns 20 --port 8000`.
- Binds: `127.0.0.1` only.

- [ ] **Step 1: Write failing parser and binding tests**

```python
def test_web_cli_has_no_host_override_and_defaults_to_localhost():
    parser = build_web_parser()
    assert "--host" not in parser.format_help()
    args = parser.parse_args(["--workspace", "."])
    assert args.port == 8000

def test_run_server_forces_loopback(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    main(["--workspace", str(tmp_path)])
    assert calls[0]["host"] == "127.0.0.1"
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest tests/test_web_cli.py -q --basetemp=.pytest-tmp-web-red-8`

Expected: `web_cli` missing.

- [ ] **Step 3: Implement startup composition**

Reuse `Config`, `OpenAIChatModel`, `ToolRuntime`, and `SessionTrace`; create EventBus, ApprovalGate, persistent AgentSession, WebSession, and FastAPI app. Validate port 1–65535 and max turns >=1. Register `minicodex-web = "minicodex.web_cli:main"` in `pyproject.toml`. Disable noisy Uvicorn access logs while keeping application terminal events.

- [ ] **Step 4: Run CLI tests and help smoke**

Run: `python -m pytest tests/test_web_cli.py tests/test_cli.py -q --basetemp=.pytest-tmp-web-green-8`

Run: `python -m minicodex.web_cli --help`

Expected: both CLI suites pass and help documents workspace/model/max-turns/port without a host option.

- [ ] **Step 5: Commit**

```powershell
git add src/minicodex/web_cli.py pyproject.toml tests/test_web_cli.py
git commit -m "feat: add localhost web CLI entry point"
```

### Task 9: Four-turn Demo Fixture

**Files:**
- Create: `demo/multi_turn_expense_tracker/expense_tracker.py`
- Create: `demo/multi_turn_expense_tracker/sample.csv`
- Create: `demo/multi_turn_expense_tracker/tests/test_expense_tracker.py`
- Create: `demo/multi_turn_expense_tracker/PROMPTS.md`
- Create: `demo/multi_turn_expense_tracker/README.md`

**Interfaces:**
- Initial contract: exactly two failing tests for refund handling and category normalization.
- Prompt 2 target: `monthly_totals(expenses) -> dict[str, float]`.
- Prompt 3 target: `category_totals(expenses, aliases: dict[str, str] | None = None) -> dict[str, float]`.

- [ ] **Step 1: Copy the small expense fixture and write four prompts**

Prompt 1 fixes two existing failures. Prompt 2 requests monthly totals plus tests. Prompt 3 requests optional aliases with backward compatibility plus regression tests. Prompt 4 requests full tests, CLI smoke, and a summary without further features.

- [ ] **Step 2: Verify the initial demo baseline**

Run from fixture: `python -m pytest -q`

Expected: exactly `2 failed`; do not add future-feature tests to the initial fixture.

- [ ] **Step 3: Document expected UI observations**

Explain which tool cards, Diff, authorization, verification changes, and persistent-context behaviors should appear in each round. Include reset instructions using Git, without destructive broad commands.

- [ ] **Step 4: Commit**

```powershell
git add demo/multi_turn_expense_tracker
git commit -m "demo: add four-turn coding workflow"
```

### Task 10: Documentation, Browser QA, and Release Verification

**Files:**
- Modify: `README.md`
- Create: `docs/WEB_UI.md`
- Modify: `README.txt`

**Interfaces:**
- Documents all public CLI, API, event, limit, safety, and demo behavior implemented above.

- [ ] **Step 1: Expand concise root README sections**

Add `minicodex-web` installation/start commands, the multi-turn lifecycle, SSE purpose, terminal fan-out, page features, and links to the demo and `docs/WEB_UI.md`. State exact limits: 20 model calls per Prompt, 80,000-character compaction, three repeated failures, 15-second heartbeat, 300-second approval timeout, and 120-second command limit.

- [ ] **Step 2: Write focused Web UI documentation**

Describe file responsibilities, routes, event types, threading model, ApprovalGate, SSE reconnect, security boundary, failure handling, and short answers to “why SSE instead of WebSocket?”, “why one worker?”, and “what survives a refresh/restart?”. Keep it concrete and avoid duplicating the entire root README.

- [ ] **Step 3: Run all automated verification**

Run: `python -m pytest -q --basetemp=.pytest-tmp-web-final`

Expected: all main tests pass with only the known Windows symlink skip.

Run initial fixtures separately and confirm each intentionally reports exactly two failures.

- [ ] **Step 4: Perform real local Web smoke**

Start `minicodex-web` on a free local port, open the page, submit at least two short Prompts with a fake or configured model, verify SSE timeline updates, approve/reject flow, terminal fan-out, and refresh replay. Do not expose or print the API Key.

- [ ] **Step 5: Inspect security and Git state**

Run `git diff --check`; scan candidate files for real Key-like literals and `shell=True`; confirm `.env`, `.minicodex`, `.venv`, pytest temp directories, and egg-info remain ignored.

- [ ] **Step 6: Commit documentation and final polish**

```powershell
git add README.md README.txt docs/WEB_UI.md
git commit -m "docs: explain web UI, SSE, limits, and demo"
```
