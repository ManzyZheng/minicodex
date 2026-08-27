from __future__ import annotations

import difflib
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .models import ToolResult
from .workspace import WorkspaceError, WorkspaceGuard


CommandApprover = Callable[[list[str], str], bool]


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "list_files", "description": "List files inside the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}}},
    {"type": "function", "function": {"name": "search_text", "description": "Search UTF-8 text files.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string", "default": "."}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 file before editing it.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a UTF-8 file. Existing files must be read first.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace exactly one literal text occurrence and return a unified diff. Read the file first.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run an argv command after user approval; no shell is used.", "parameters": {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}}, "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30}, "purpose": {"type": "string", "enum": ["test", "build", "lint", "other"]}}, "required": ["argv", "purpose"]}}},
]


class ToolRuntime:
    def __init__(self, workspace: str | Path, *, command_approver: CommandApprover) -> None:
        self.guard = WorkspaceGuard(workspace)
        self.command_approver = command_approver
        self.read_paths: set[Path] = set()
        self.change_seq = 0
        self.last_verification: dict[str, Any] | None = None

    def _failure(self, tool: str, call_id: str, code: str, message: str) -> ToolResult:
        return ToolResult.failure(tool=tool, call_id=call_id, code=code, message=message)

    def _path(self, tool: str, call_id: str, path: str) -> Path | ToolResult:
        try:
            return self.guard.resolve(path)
        except WorkspaceError as exc:
            return self._failure(tool, call_id, "WORKSPACE_VIOLATION", str(exc))

    def list_files(self, call_id: str, path: str = ".") -> ToolResult:
        target = self._path("list_files", call_id, path)
        if isinstance(target, ToolResult): return target
        if not target.exists(): return self._failure("list_files", call_id, "PATH_NOT_FOUND", f"path not found: {path}")
        candidates = [target] if target.is_file() else target.rglob("*")
        files = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if not resolved.is_relative_to(self.guard.root) or not resolved.is_file(): continue
            except OSError:
                continue
            if any(x in {".git", ".minicodex", ".pytest_cache"} for x in candidate.parts): continue
            files.append(self.guard.relative(resolved))
        files = sorted(set(files))
        return ToolResult.success(tool="list_files", call_id=call_id, summary=f"listed {len(files)} files", data={"files": files})

    def search_text(self, call_id: str, query: str, path: str = ".") -> ToolResult:
        if not query: return self._failure("search_text", call_id, "INVALID_ARGUMENT", "query must not be empty")
        target = self._path("search_text", call_id, path)
        if isinstance(target, ToolResult): return target
        if not target.exists(): return self._failure("search_text", call_id, "PATH_NOT_FOUND", f"path not found: {path}")
        candidates = [target] if target.is_file() else target.rglob("*")
        matches: list[dict[str, Any]] = []
        for file in candidates:
            try:
                resolved = file.resolve()
                if not resolved.is_relative_to(self.guard.root) or not resolved.is_file(): continue
            except OSError:
                continue
            if any(x in {".git", ".minicodex"} for x in file.parts): continue
            try: lines = resolved.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError): continue
            for number, line in enumerate(lines, 1):
                if query in line: matches.append({"path": self.guard.relative(resolved), "line": number, "text": line})
        return ToolResult.success(tool="search_text", call_id=call_id, summary=f"found {len(matches)} matches", data={"matches": matches})

    def read_file(self, call_id: str, path: str) -> ToolResult:
        target = self._path("read_file", call_id, path)
        if isinstance(target, ToolResult): return target
        try: content = target.read_text(encoding="utf-8")
        except FileNotFoundError: return self._failure("read_file", call_id, "FILE_NOT_FOUND", f"file not found: {path}")
        except (OSError, UnicodeDecodeError) as exc: return self._failure("read_file", call_id, "READ_ERROR", str(exc))
        self.read_paths.add(target)
        return ToolResult.success(tool="read_file", call_id=call_id, summary=f"read {len(content)} characters", data={"path": self.guard.relative(target), "content": content})

    def _require_read(self, tool: str, call_id: str, target: Path) -> ToolResult | None:
        if target.exists() and target not in self.read_paths:
            return self._failure(tool, call_id, "READ_REQUIRED", "existing file must be read before editing")
        return None

    def _changed(self, target: Path) -> None:
        self.read_paths.add(target)
        self.change_seq += 1
        self.last_verification = None

    def write_file(self, call_id: str, path: str, content: str) -> ToolResult:
        target = self._path("write_file", call_id, path)
        if isinstance(target, ToolResult): return target
        denied = self._require_read("write_file", call_id, target)
        if denied: return denied
        old = target.read_text(encoding="utf-8") if target.exists() else ""
        if old == content: return self._failure("write_file", call_id, "NO_CHANGE", "new content is identical")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}"))
        self._changed(target)
        return ToolResult.success(tool="write_file", call_id=call_id, summary=f"wrote {path}", data={"diff": diff})

    def edit_file(self, call_id: str, path: str, old_text: str, new_text: str) -> ToolResult:
        target = self._path("edit_file", call_id, path)
        if isinstance(target, ToolResult): return target
        denied = self._require_read("edit_file", call_id, target)
        if denied: return denied
        try: old = target.read_text(encoding="utf-8")
        except FileNotFoundError: return self._failure("edit_file", call_id, "FILE_NOT_FOUND", f"file not found: {path}")
        count = old.count(old_text)
        if count == 0: return self._failure("edit_file", call_id, "OLD_TEXT_NOT_FOUND", "old_text was not found")
        if count > 1: return self._failure("edit_file", call_id, "AMBIGUOUS_MATCH", f"old_text matched {count} times")
        if old_text == new_text: return self._failure("edit_file", call_id, "NO_CHANGE", "old_text and new_text are identical")
        new = old.replace(old_text, new_text, 1)
        target.write_text(new, encoding="utf-8")
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}"))
        self._changed(target)
        return ToolResult.success(tool="edit_file", call_id=call_id, summary=f"edited {path}", data={"diff": diff})

    def run_command(self, call_id: str, argv: list[str], *, purpose: str, timeout_sec: int = 30) -> ToolResult:
        if not argv or not all(isinstance(x, str) and x for x in argv): return self._failure("run_command", call_id, "INVALID_ARGUMENT", "argv must be a non-empty string array")
        if not 1 <= timeout_sec <= 120: return self._failure("run_command", call_id, "INVALID_ARGUMENT", "timeout_sec must be between 1 and 120")
        if purpose not in {"test", "build", "lint", "other"}: return self._failure("run_command", call_id, "INVALID_ARGUMENT", "purpose must be test, build, lint, or other")
        if not self.command_approver(argv, purpose): return self._failure("run_command", call_id, "COMMAND_REJECTED", "user rejected command")
        child_env = os.environ.copy()
        for secret_name in ("MINICODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            child_env.pop(secret_name, None)
        try:
            completed = subprocess.run(argv, cwd=self.guard.root, shell=False, capture_output=True, text=True, timeout=timeout_sec, env=child_env)
        except subprocess.TimeoutExpired as exc:
            return self._failure("run_command", call_id, "COMMAND_TIMEOUT", f"command timed out after {timeout_sec}s")
        except OSError as exc:
            return self._failure("run_command", call_id, "COMMAND_ERROR", str(exc))
        data = {"argv": argv, "purpose": purpose, "exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
        if purpose in {"test", "build", "lint"} and self.change_seq:
            self.last_verification = {"status": "VERIFIED" if completed.returncode == 0 else "FAILED", "change_seq": self.change_seq, **data}
        return ToolResult.success(tool="run_command", call_id=call_id, summary=f"command exited {completed.returncode}", data=data) if completed.returncode == 0 else ToolResult.failure(tool="run_command", call_id=call_id, code="COMMAND_FAILED", message=f"command exited {completed.returncode}", data=data)

    def execute(self, name: str, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        handler = getattr(self, name, None)
        if name not in {schema["function"]["name"] for schema in TOOL_SCHEMAS} or not callable(handler):
            return self._failure(name, call_id, "UNKNOWN_TOOL", f"unknown tool: {name}")
        started = time.monotonic()
        try: result = handler(call_id, **arguments)
        except TypeError as exc: result = self._failure(name, call_id, "INVALID_ARGUMENT", str(exc))
        except Exception as exc: result = self._failure(name, call_id, "TOOL_INTERNAL_ERROR", str(exc))
        result.meta.duration_ms = int((time.monotonic() - started) * 1000)
        return result
