# Project, Session, and Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persisted multi-project/multi-session operation and conservative global/project memory to MiniCodex without session concurrency.

**Architecture:** Application-owned repositories persist projects, sessions, and memory with atomic JSON writes. A Web workspace manager delegates existing session operations to one active `WebSession`, restores Agent messages on switches, saves state after prompts, and runs an isolated post-turn memory service. The prompt builder injects bounded memory context, while the existing frontend gains a project/session sidebar and memory manager.

**Tech Stack:** Python 3.11, dataclasses, pathlib, JSON/JSONL, FastAPI, vanilla JavaScript/CSS, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-project-sessions-memory-design.md`

## Global Constraints

- One running session globally; no concurrent Agent prompts.
- No vector database, embeddings, cloud sync, or new runtime dependency.
- Both valid global and project memories auto-save; global scope has stricter evidence validation.
- Memory extraction never changes the main task outcome and never enters main conversation history.
- Existing single-session API behavior remains backward compatible.

---

### Task 1: Atomic persistence, projects, and sessions

**Files:**
- Create: `src/minicodex/persistence.py`
- Create: `src/minicodex/projects.py`
- Create: `src/minicodex/project_sessions.py`
- Test: `tests/test_project_persistence.py`

**Interfaces:**
- Produces: `ApplicationPaths`, `ProjectRegistry`, `ProjectRecord`, `SessionRepository`, `SessionRecord`.

- [ ] Write tests proving project path de-duplication, session creation/listing, atomic state round-trip, and non-destructive project removal.
- [ ] Run the focused tests and confirm they fail because the modules do not exist.
- [ ] Implement the minimal repositories with validated IDs, resolved workspace paths, timestamps, and atomic JSON replacement.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Unified scoped memory and conservative extraction

**Files:**
- Create: `src/minicodex/memory/__init__.py`
- Create: `src/minicodex/memory/models.py`
- Create: `src/minicodex/memory/store.py`
- Create: `src/minicodex/memory/extractor.py`
- Create: `src/minicodex/memory/service.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `ApplicationPaths`, `ProjectRecord`.
- Produces: `MemoryItem`, `MemoryStore`, `MemoryExtractor`, `MemoryService`, `MemoryProcessResult`.

- [ ] Write tests for global/project storage, logical forget, explicit remember, empty extraction, evidence enforcement, secret rejection, de-duplication, strict global scope evidence, and non-fatal malformed model output.
- [ ] Run the focused tests and confirm the expected missing-module failure.
- [ ] Implement strict JSON candidate parsing, deterministic validation, atomic storage, and automatic create/no-op behavior.
- [ ] Run the focused tests and confirm they pass.

### Task 3: Agent state recovery and memory prompt layer

**Files:**
- Modify: `src/minicodex/agent.py`
- Modify: `src/minicodex/prompting.py`
- Test: `tests/test_agent_state.py`
- Modify: `tests/test_prompting.py`

**Interfaces:**
- Consumes: a callable memory prompt provider.
- Produces: `AgentSession.export_state()`, `AgentSession.restore_state(state)`, and a refreshed memory system layer.

- [ ] Write tests proving messages/prompt count restore, transient runtime state is not restored, and global/project memories appear in the bounded prompt layer.
- [ ] Run focused tests and confirm failure for missing state APIs.
- [ ] Add a dedicated memory system message, state export/restore, and prompt refresh without changing existing tool/runtime message indexes accidentally.
- [ ] Run focused tests and confirm they pass.

### Task 4: Single-run Web workspace manager and API

**Files:**
- Create: `src/minicodex/web/manager.py`
- Modify: `src/minicodex/web/session.py`
- Modify: `src/minicodex/web/app.py`
- Modify: `src/minicodex/web_cli.py`
- Test: `tests/test_web_manager.py`
- Modify: `tests/test_web_api.py`

**Interfaces:**
- Consumes: project/session repositories, `MemoryService`, and a `WebSession` factory.
- Produces: project/session switch/create APIs, active-session delegation, memory CRUD APIs, and post-turn persistence/extraction.

- [ ] Write tests for initial project registration, session creation/switching, busy-switch rejection, restored conversation state, single extraction per prompt index, explicit remember/forget, and legacy endpoint delegation.
- [ ] Run focused tests and confirm missing manager/API behavior.
- [ ] Implement `WebWorkspaceManager`, prompt-completion callbacks, new Pydantic request types/routes, and CLI factory wiring with a shared event bus.
- [ ] Run focused Web tests and confirm they pass.

### Task 5: Project/session and memory frontend

**Files:**
- Modify: `src/minicodex/web/static/index.html`
- Modify: `src/minicodex/web/static/codex-app.css`
- Modify: `src/minicodex/web/static/codex-app.js`
- Modify: `tests/js/turn_timeline_test.cjs`
- Modify: `tests/test_web_static.py`

**Interfaces:**
- Consumes: `/api/projects`, session routes, memory routes, and session reset/history snapshots.
- Produces: a left project/session sidebar, new-session control, global/project memory views, switch handling, and undoable memory notices.

- [ ] Add failing static/JavaScript assertions for sidebar controls, session switching/reset, memory rendering, remember/forget actions, and busy-state disabling.
- [ ] Run the focused frontend tests and confirm failure.
- [ ] Implement the minimal Codex-style sidebar and memory panel while preserving the conversation, navigation rail, composer, and right diff review.
- [ ] Run focused frontend tests and confirm they pass.

### Task 6: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `CODE_WALKTHROUGH.md`
- Modify: `.env.example`

**Interfaces:**
- Documents: data location, APIs, recovery semantics, memory safety/order, automatic extraction, and non-concurrency.

- [ ] Update concise user and code documentation without adding unrelated features.
- [ ] Run `python -m pytest -q` with a workspace-local basetemp and confirm zero failures.
- [ ] Run `git diff --check` and inspect status to ensure local Demo files remain uncommitted and no secret/session data is included.
