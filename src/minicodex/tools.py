from __future__ import annotations

import difflib
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .models import ToolResult
from .permissions import AgentMode, ApprovalPrompt, PermissionAction, PermissionDecision, PermissionPolicy
from .workspace import WorkspaceError, WorkspaceGuard


Approver = Callable[[ApprovalPrompt], bool]


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "list_files", "description": "List files inside the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}}},
    {"type": "function", "function": {"name": "search_text", "description": "Search UTF-8 text files.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string", "default": "."}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 file before editing it.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a UTF-8 file. Existing files must be read first.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace exactly one literal text occurrence and return a unified diff. Read the file first.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run one or more structured argv commands sequentially without a shell. Each command is permission-checked independently.", "parameters": {"type": "object", "properties": {"commands": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object", "properties": {"argv": {"type": "array", "minItems": 1, "items": {"type": "string"}}, "purpose": {"type": "string", "enum": ["test", "build", "lint", "other"]}, "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30}}, "required": ["argv", "purpose"]}}, "stop_on_failure": {"type": "boolean", "default": True}}, "required": ["commands"]}}},
]


class ToolRuntime:
    def __init__(self, workspace: str | Path, *, approver: Approver, mode: AgentMode = AgentMode.ACT) -> None:
        self.guard = WorkspaceGuard(workspace)
        self.approver = approver
        self.mode = mode
        self.permissions = PermissionPolicy(self.guard.root)
        self.read_paths: set[Path] = set()
        self.change_seq = 0
        self.last_verification: dict[str, Any] | None = None

    def _failure(self, tool: str, call_id: str, code: str, message: str) -> ToolResult:
        return ToolResult.failure(tool=tool, call_id=call_id, code=code, message=message)

    def _path(self, tool: str, call_id: str, path: str) -> Path | ToolResult:
        decision = self.permissions.decide_tool(self.mode, tool, {"path": path})
        if decision.action is PermissionAction.DENY:
            return self._failure(tool, call_id, "PROTECTED_PATH" if decision.rule_id == "global.protected_path" else "PERMISSION_DENIED", decision.reason)
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
            if self.permissions.is_protected_path(self.guard.relative(resolved)): continue
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
            if self.permissions.is_protected_path(self.guard.relative(resolved)): continue
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

    def set_mode(self, mode: AgentMode) -> None:
        self.mode = mode

    def _approve(self, decision: PermissionDecision, prompt: ApprovalPrompt) -> bool:
        if decision.action is PermissionAction.ALLOW:
            return True
        if decision.action is PermissionAction.DENY:
            return False
        return self.approver(prompt)

    def _file_change_allowed(self, tool: str, path: str, diff: str) -> tuple[bool, PermissionDecision]:
        decision = self.permissions.decide_tool(self.mode, tool, {"path": path})
        prompt = ApprovalPrompt(
            kind="file_change",
            tool=tool,
            summary=f"{tool} proposes changes to {path}",
            reason=decision.reason,
            risk=decision.risk,
            rule_id=decision.rule_id,
            details={"path": path, "diff": diff},
        )
        return self._approve(decision, prompt), decision

    def write_file(self, call_id: str, path: str, content: str) -> ToolResult:
        target = self._path("write_file", call_id, path)
        if isinstance(target, ToolResult): return target
        denied = self._require_read("write_file", call_id, target)
        if denied: return denied
        old = target.read_text(encoding="utf-8") if target.exists() else ""
        if old == content: return self._failure("write_file", call_id, "NO_CHANGE", "new content is identical")
        diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}"))
        allowed, decision = self._file_change_allowed("write_file", path, diff)
        if not allowed:
            code = "PERMISSION_DENIED" if decision.action is PermissionAction.DENY else "CHANGE_REJECTED"
            return self._failure("write_file", call_id, code, decision.reason if decision.action is PermissionAction.DENY else "user rejected file change")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._changed(target)
        return ToolResult.success(
            tool="write_file", call_id=call_id, summary=f"wrote {path}",
            data={"path": path, "diff": diff, "permission": decision.rule_id},
        )

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
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}"))
        allowed, decision = self._file_change_allowed("edit_file", path, diff)
        if not allowed:
            code = "PERMISSION_DENIED" if decision.action is PermissionAction.DENY else "CHANGE_REJECTED"
            return self._failure("edit_file", call_id, code, decision.reason if decision.action is PermissionAction.DENY else "user rejected file change")
        target.write_text(new, encoding="utf-8")
        self._changed(target)
        return ToolResult.success(
            tool="edit_file", call_id=call_id, summary=f"edited {path}",
            data={"path": path, "diff": diff, "permission": decision.rule_id},
        )

    def run_command(self, call_id: str, commands: list[dict[str, Any]], *, stop_on_failure: bool = True) -> ToolResult:
        if not isinstance(commands, list) or not 1 <= len(commands) <= 8:
            return self._failure("run_command", call_id, "INVALID_ARGUMENT", "commands must contain between 1 and 8 entries")
        if not isinstance(stop_on_failure, bool):
            return self._failure("run_command", call_id, "INVALID_ARGUMENT", "stop_on_failure must be boolean")
        prepared: list[dict[str, Any]] = []
        for index, command in enumerate(commands):
            if not isinstance(command, dict):
                return self._failure("run_command", call_id, "INVALID_ARGUMENT", f"commands[{index}] must be an object")
            argv = command.get("argv")
            purpose = command.get("purpose")
            timeout_sec = command.get("timeout_sec", 30)
            if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
                return self._failure("run_command", call_id, "INVALID_ARGUMENT", f"commands[{index}].argv must be a non-empty string array")
            if purpose not in {"test", "build", "lint", "other"}:
                return self._failure("run_command", call_id, "INVALID_ARGUMENT", f"commands[{index}].purpose is invalid")
            if not isinstance(timeout_sec, int) or not 1 <= timeout_sec <= 120:
                return self._failure("run_command", call_id, "INVALID_ARGUMENT", f"commands[{index}].timeout_sec must be between 1 and 120")
            prepared.append({"argv": list(argv), "purpose": purpose, "timeout_sec": timeout_sec})
        child_env = os.environ.copy()
        for secret_name in ("MINICODEX_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            child_env.pop(secret_name, None)
        results: list[dict[str, Any]] = []
        failed_index: int | None = None
        failure_code: str | None = None
        failure_message: str | None = None
        for index, command in enumerate(prepared):
            if failed_index is not None and stop_on_failure:
                results.append({"index": index, "status": "skipped", "argv": command.get("argv", []), "purpose": command.get("purpose")})
                continue
            argv = command.get("argv")
            purpose = command.get("purpose")
            timeout_sec = command.get("timeout_sec", 30)
            decision = self.permissions.decide_command(self.mode, argv, purpose)
            prompt = ApprovalPrompt(
                kind="command", tool="run_command", summary=f"run command {index + 1} of {len(commands)}",
                reason=decision.reason, risk=decision.risk, rule_id=decision.rule_id,
                details={"index": index, "count": len(commands), "argv": argv, "purpose": purpose, "timeout_sec": timeout_sec},
            )
            if decision.action is PermissionAction.DENY:
                failed_index, failure_code, failure_message = index, "COMMAND_DENIED", decision.reason
                results.append({"index": index, "status": "denied", "argv": argv, "purpose": purpose, "reason": decision.reason})
                continue
            if not self._approve(decision, prompt):
                failed_index, failure_code, failure_message = index, "COMMAND_REJECTED", "user rejected command"
                results.append({"index": index, "status": "rejected", "argv": argv, "purpose": purpose})
                continue
            try:
                completed = subprocess.run(argv, cwd=self.guard.root, shell=False, capture_output=True, text=True, timeout=timeout_sec, env=child_env)
                step = {"index": index, "status": "completed", "argv": argv, "purpose": purpose, "timeout_sec": timeout_sec, "exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr, "permission": decision.rule_id}
            except subprocess.TimeoutExpired as exc:
                step = {"index": index, "status": "timeout", "argv": argv, "purpose": purpose, "timeout_sec": timeout_sec, "exit_code": None, "stdout": exc.stdout or "", "stderr": exc.stderr or "", "permission": decision.rule_id}
                failed_index, failure_code, failure_message = index, "COMMAND_TIMEOUT", f"command {index + 1} timed out after {timeout_sec}s"
            except OSError as exc:
                step = {"index": index, "status": "error", "argv": argv, "purpose": purpose, "timeout_sec": timeout_sec, "exit_code": None, "stdout": "", "stderr": str(exc), "permission": decision.rule_id}
                failed_index, failure_code, failure_message = index, "COMMAND_ERROR", str(exc)
            results.append(step)
            if step.get("exit_code") not in {0, None}:
                failed_index, failure_code, failure_message = index, "COMMAND_FAILED", f"command {index + 1} exited {step['exit_code']}"
            if purpose in {"test", "build", "lint"} and self.change_seq and step.get("exit_code") is not None:
                self.last_verification = {
                    **step,
                    "step_status": step["status"],
                    "status": "VERIFIED" if step["exit_code"] == 0 else "FAILED",
                    "change_seq": self.change_seq,
                }
        data = {"commands": results, "stop_on_failure": stop_on_failure, "failed_index": failed_index}
        if failure_code:
            return ToolResult.failure(tool="run_command", call_id=call_id, code=failure_code, message=failure_message or failure_code, data=data)
        return ToolResult.success(tool="run_command", call_id=call_id, summary=f"completed {len(results)} command(s)", data=data)

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
