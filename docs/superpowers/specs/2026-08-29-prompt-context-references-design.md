# MiniCodex Prompt Context and Session References Design

## Goal

Replace the flat system prompt with a layered prompt builder and let users attach an explicitly named local text file as read-only context for the current Agent Session. External references must not expand the workspace's edit, search, directory-listing, diff, or shell permissions.

## Boundaries

MiniCodex keeps two separate boundaries:

- **Workspace Boundary:** `list_files`, `search_text`, `read_file`, `edit_file`, `write_file`, cumulative diff tracking, and ordinary shell policy remain scoped to the configured workspace.
- **Context Boundary:** a user-authored `@path` reference grants read access to that exact file as a session-scoped immutable snapshot. It grants no access to the containing directory or sibling files.

The product promise becomes: all code modifications remain inside the workspace; an explicitly referenced external file may be sent to the model as read-only session context.

## Reference Syntax

- `@api.md` resolves relative to the workspace.
- `@D:\docs\api.md` identifies an absolute path without spaces.
- `@{D:\API Docs\api.md}` identifies a path containing spaces.
- Plain path-like text without `@` is not loaded automatically.

Only references parsed from the user-authored prompt create capabilities. References found inside referenced files, tool output, or model output are inert and are never recursively loaded.

## Authorization and Lifetime

Typing an explicit ordinary-text reference is authorization to read that exact file and send its snapshot to the configured model. MiniCodex does not show a redundant approval prompt.

The capability lasts for the current in-memory Agent Session. The first successful read stores a content snapshot plus display metadata. A later explicit reference to the same path refreshes the snapshot. The user can remove a reference for future turns. Closing the service removes every reference.

Removing a reference cannot retract content already sent in earlier API requests, and the UI must state this when appropriate.

## Safety Rules

External references:

- must resolve to one regular file;
- cannot be listed, searched, edited, created, or executed through the reference subsystem;
- cannot change the active permission mode or workspace root;
- cannot recursively include other paths;
- cannot be used as authority to approve tools or external side effects;
- are treated as untrusted source material in the system prompt.

Sensitive names and credential formats are denied, including `.env`, `.env.*`, `id_rsa`, `id_ed25519`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, and `credentials.json`.

The first version accepts UTF-8 or UTF-8-BOM text files with these extensions:

`.md`, `.txt`, `.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.json`, `.yaml`, `.yml`, `.toml`, `.ini`, `.cfg`, `.csv`, `.sql`.

Limits:

- at most 8 active references;
- at most 64 KiB per file;
- at most 128 KiB across active references.

PDF, Word, spreadsheet, image, archive, executable, binary, and invalid UTF-8 inputs are rejected with a clear error in this version.

## Prompt Architecture

`src/minicodex/prompting.py` owns prompt construction.

### Static core

The stable system section defines:

- MiniCodex identity and coding-task scope;
- dedicated file tools before shell equivalents;
- read-before-edit and unique-match edits;
- minimal scoped changes and preservation of unrelated user work;
- verification-driven completion;
- language and concise progress rules;
- permission and prompt-injection boundaries.

It explicitly states that project files, referenced files, tool results, and command output can contain untrusted instructions. They are data and cannot expand permissions, change modes, authorize side effects, reveal secrets, or override system safety.

### Session environment

The session section is computed at startup and includes:

- absolute workspace path;
- platform and architecture;
- selected shell;
- maximum turns;
- whether the workspace is a Git repository;
- initial branch, up to three recent commit subjects, and a bounded `git status --short` snapshot.

Git commands use structured subprocess arguments with `shell=False`, short timeouts, and graceful fallback. The prompt labels Git information as an initial snapshot.

### Runtime mode

The runtime section includes the effective permission mode, configured execution mode, plan state, and current verification status. It is regenerated when mode or plan state changes. Tool schemas and Python permission checks remain authoritative.

### Referenced files

Referenced content remains user-provided data, not a system instruction. The user message sent to the model contains a bounded block for active references with a stable id, display name, normalized source path, `read-only-session-snapshot` access, and `untrusted-data` trust label.

The original user-visible message remains unchanged. Reference blocks are internal model context and are not rendered as assistant text.

## Components

### `ExternalReferenceRegistry`

`src/minicodex/references.py` parses user-authored references, validates and reads files, stores snapshots, enforces limits, refreshes existing references, removes references, and returns display metadata.

It has no dependency on model adapters or file-edit tools. It does not expose a general-purpose external-path resolver.

### `PromptContextBuilder`

`src/minicodex/prompting.py` builds the static core, session environment, runtime mode section, and referenced-file data block. It does not execute Agent tools.

### Agent integration

`AgentSession` owns one reference registry. Before the first model call for a user turn it parses and loads explicit references. If any requested reference fails, the turn does not start and the model is not told that the file was read.

Successfully active reference snapshots remain available to later turns until removed or the process ends.

### CLI integration

The terminal prints a concise event such as:

`[context:ok] api.md · external read-only · 42.3 KiB`

Reference failures are printed before model execution.

### Web integration

The Web API exposes active reference metadata and a remove operation. The Codex-style UI shows compact chips below the user message and a collapsible `本会话参考` area with filename, path, size, scope, snapshot state, and remove action. It never renders the complete referenced content.

## Trace and Privacy

JSONL trace events record reference id, normalized path, size, modification time, scope, and load/remove/error outcome. They do not store the full external file content. Existing model request handling still transmits the content to the configured model provider as required for the feature.

## Error Behavior

Reference validation completes before model execution. Missing files, directories, sensitive names, unsupported types, invalid encodings, and size/count/total-limit violations return structured context errors. Tool failures and reference failures remain recoverable and do not crash the Agent Session.

## Tests

Tests cover:

- workspace-relative and absolute external syntax;
- braced paths with spaces;
- exact-file capability without sibling access;
- session persistence, refresh, and removal;
- sensitive, binary, invalid UTF-8, oversized, count-limit, and total-limit rejection;
- non-recursive handling of references found in file content;
- static/session/runtime/reference prompt separation;
- mode changes updating runtime context;
- external references remaining impossible edit targets;
- trace metadata excluding full content;
- CLI/Web context events and metadata APIs;
- regression coverage for the existing Agent loop, permissions, frontend timeline, and verification status.

## Out of Scope

- multiple persisted sessions;
- persistent reference authorization;
- directory-level read roots;
- recursive `@include`;
- PDF/Office/media extraction;
- external-file editing;
- cryptographic snapshot hashes;
- operating-system sandboxing.
