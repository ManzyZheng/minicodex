# Project, Session, and Memory Design

## Scope

MiniCodex supports multiple registered local projects and multiple persisted sessions per project while allowing only one running Agent prompt at a time. It also supports a unified memory system with `global` and `project` scopes, explicit remember/forget operations, conservative post-turn extraction, prompt injection, and undoable automatic writes.

## Constraints

- Project registration never deletes or moves a workspace.
- Application data lives outside workspaces under a configurable MiniCodex data directory.
- Session recovery restores conversation and metadata but not live approvals, processes, read-before-edit cache, or cancellation state.
- Only one session may run globally; other sessions remain readable.
- Memory uses `scope = global | project` and `kind = preference | decision | reference`.
- Automatic extraction runs once after a completed user prompt, uses no tools and no thinking, accepts at most two candidates, and may validly return none.
- Candidate evidence must be a continuous substring of recent user text. Secrets, large code, invalid scope evidence, and duplicates are rejected.
- Both valid project and global memories are saved automatically. Global scope requires explicit cross-project or stable personal-preference evidence.
- Memory cannot grant permissions, override workspace boundaries, or outrank the current user request.
- No vector database, embedding service, session concurrency, cloud sync, or third-party runtime dependency is added.

## Persistence

The default application root is `%LOCALAPPDATA%/MiniCodex`, overridable for tests and local deployments. Atomic JSON files store the project registry, session metadata/state, and memory items. Session trace remains JSONL. The canonical hierarchy is:

```text
MiniCodex/
  registry.json
  memory/items/*.json
  projects/<project-id>/
    project.json
    memory/items/*.json
    sessions/<session-id>/metadata.json
    sessions/<session-id>/state.json
    sessions/<session-id>/trace.jsonl
```

## Prompt Ordering

The main Agent prompt uses static policy, session environment, memory context, runtime state, then conversation messages. The memory layer contains bounded global and project indexes plus a small number of recalled bodies. Current user instructions override memory; project memory overrides global memory only inside that project; system safety rules override all memory.

## Web Model

The Web application exposes project/session/memory APIs while retaining existing single-session endpoints as delegates to the active session. Switching or creating a session is rejected while the active prompt is running. The left sidebar lists projects, sessions, global memory, and project memory. Session history is restored from persisted Agent messages; automatic memory writes appear as non-blocking, undoable events.
