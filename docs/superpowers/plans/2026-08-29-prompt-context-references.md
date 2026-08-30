# Prompt Context and Session References Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add layered prompt construction and exact-file, read-only, session-scoped `@` references without expanding the workspace mutation boundary.

**Architecture:** A focused `ExternalReferenceRegistry` owns parsing, validation, snapshots, limits, and metadata. A separate prompt builder owns stable rules, environment/runtime sections, and safe user-data wrapping; `AgentSession` coordinates both while Web/CLI only expose metadata and events.

**Tech Stack:** Python 3.11+, dataclasses, pathlib, subprocess with `shell=False`, FastAPI/Pydantic, plain JavaScript/CSS, pytest, Node-based frontend tests.

**Spec:** `docs/superpowers/specs/2026-08-29-prompt-context-references-design.md`

## Global Constraints

- All edit, create, search, list, diff, and ordinary tool reads stay inside the configured workspace.
- External access is one exact regular-file snapshot explicitly named by a user-authored `@` token.
- References last only for the current in-memory Agent Session and never recurse.
- Deny sensitive names; accept only the listed UTF-8 text extensions.
- Limit 8 files, 64 KiB per file, and 128 KiB total.
- Do not store referenced content in JSONL trace events or render it in the UI.
- Do not create commits or push this implementation.

---

### Task 1: External reference registry

**Files:**
- Create: `src/minicodex/references.py`
- Create: `tests/test_references.py`

**Interfaces:**
- Produces `ReferenceErrorInfo(code: str, message: str, path: str | None)`.
- Produces immutable `ExternalReference(id, path, name, content, size, modified_at, scope)` with `metadata()` excluding content.
- Produces `ExternalReferenceRegistry(workspace)` with `parse(text)`, `load_from_prompt(text)`, `active()`, `metadata()`, and `remove(reference_id)`.

- [ ] Write parsing tests for `@api.md`, `@D:\docs\api.md`, and `@{D:\API Docs\api.md}`.
- [ ] Run `pytest tests/test_references.py -q -p no:cacheprovider` and confirm import/behavior failures.
- [ ] Implement the minimal parser and normalized exact-path lookup.
- [ ] Add failing validation tests for missing/directory/sensitive/unsupported/invalid UTF-8/64 KiB/count/128 KiB conditions.
- [ ] Implement validation, immutable snapshots, refresh-by-path, metadata, and removal.
- [ ] Add a test proving `@` inside referenced content is inert and sibling files are not exposed.
- [ ] Run the reference tests green.

### Task 2: Layered prompt builder

**Files:**
- Create: `src/minicodex/prompting.py`
- Create: `tests/test_prompting.py`

**Interfaces:**
- Produces `SessionEnvironment.capture(workspace, max_turns)`.
- Produces `build_static_prompt()`, `build_session_prompt(environment)`, `build_runtime_prompt(effective_mode, execution_mode, plan_state, verification_status)`, and `build_user_context(user_text, references)`.

- [ ] Write failing tests for stable rules, workspace/platform/shell/Git snapshot, mode state, and untrusted read-only reference tags.
- [ ] Implement static rules and bounded environment capture using structured Git subprocess calls.
- [ ] Implement runtime and referenced-data builders with escaped attributes and collision-safe content boundaries.
- [ ] Verify `tests/test_prompting.py` passes.

### Task 3: Agent integration and trace privacy

**Files:**
- Modify: `src/minicodex/agent.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- `AgentSession.references` is one session registry.
- `AgentSession.reference_metadata()` and `remove_reference(id)` expose safe state.
- `run_turn(prompt)` loads explicit references before model execution, keeps the visible prompt unchanged in events/trace, and sends internal contextualized content to the model.

- [ ] Add failing Agent tests for environment messages, multi-turn persistence, refresh/removal, failed-load-before-model-call, and trace payloads without content.
- [ ] Replace the flat prompt assembly with the prompt builder while preserving Plan Mode tool schemas.
- [ ] Emit `context_loaded`, `context_removed`, and `context_error` metadata-only events.
- [ ] Run Agent, Plan Mode, compaction, and verification tests green.

### Task 4: Web and CLI integration

**Files:**
- Modify: `src/minicodex/web/session.py`
- Modify: `src/minicodex/web/app.py`
- Modify: `src/minicodex/cli.py`
- Modify: `src/minicodex/web_cli.py`
- Modify: `tests/test_web_approval.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Session snapshot includes `references: list[metadata]`.
- `DELETE /api/references/{reference_id}` removes one idle-session reference.
- CLI events render one compact context status line.

- [ ] Add failing API/session tests for metadata and removal, including busy/stale behavior.
- [ ] Implement snapshot and delete endpoint without returning content.
- [ ] Add failing CLI formatting tests and implement Chinese compact messages.
- [ ] Run targeted Python integration tests green.

### Task 5: Codex-style reference UI

**Files:**
- Modify: `src/minicodex/web/static/codex-app.js`
- Modify: `src/minicodex/web/static/codex-app.css`
- Modify: `src/minicodex/web/static/index.html`
- Modify: `tests/js/turn_timeline_test.cjs`
- Modify: `tests/test_frontend_timeline.py`

**Interfaces:**
- Active references render as a collapsed `本会话参考` summary and metadata-only rows.
- User turns render compact reference chips from `context_loaded` events.
- Remove calls the DELETE endpoint and refreshes session state.

- [ ] Add failing frontend tests for Chinese labels, metadata-only rendering, and removal endpoint usage.
- [ ] Implement compact reference rendering that does not display content.
- [ ] Bump the static asset cache version.
- [ ] Run frontend tests green.

### Task 6: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `CODE_WALKTHROUGH.md`
- Modify: `.env.example` only if configuration changes are actually introduced.

- [ ] Document the two boundaries, syntax, limits, sensitive-file denial, session lifetime, provider transmission, and examples.
- [ ] Document prompt section construction and why external material is untrusted data.
- [ ] Search for contradictory claims that all reads are workspace-only.
- [ ] Run `pytest -q -p no:cacheprovider` and read the complete result.
- [ ] Run `git diff --check`, inspect status/diff scope, and report unrelated pre-existing changes separately.
