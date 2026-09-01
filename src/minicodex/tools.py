from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .context import truncate_text
from .models import ToolResult
from .permissions import AgentMode, ApprovalPrompt, PermissionAction, PermissionDecision, PermissionPolicy
from .reviewer import ReviewDecision, ReviewOutcome
from .workspace import WorkspaceError, WorkspaceGuard


Approver = Callable[[ApprovalPrompt], bool]
Reviewer = Callable[[ApprovalPrompt], ReviewOutcome]
InterruptChecker = Callable[[], bool]

IGNORED_DISCOVERY_DIRECTORIES = frozenset({
    ".git",
    ".minicodex",
    ".pytest_cache",
    ".pytest-tmp",
    ".tmp-pytest",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
})

WINDOWS_POWERSHELL_COMMANDS = frozenset({
    "copy-item",
    "get-childitem",
    "get-content",
    "move-item",
    "remove-item",
    "rename-item",
    "set-content",
    "test-path",
    "write-output",
})


@dataclass
class FileChange:
    path: str
    prompt_index: int
    before: str
    after: str
    additions: int
    deletions: int
    diff: str
    first_change_seq: int
    last_change_seq: int


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "list_files", "description": "List files inside the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}}},
    {"type": "function", "function": {"name": "search_text", "description": "Search UTF-8 text files.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string", "default": "."}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 file before editing it. For large files, request a numbered line range instead of using shell commands to slice the file; returned line-number prefixes are annotations, not file content.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a UTF-8 file. Existing files must be read first.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit a UTF-8 file after reading it. Use old_text/new_text for one unique literal replacement, or edits for up to 12 sequential unique replacements. Batch edits are atomic: any invalid match leaves the file unchanged.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                            },
                            "required": ["old_text", "new_text"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {"type": "function", "function": {"name": "run_shell", "description": "Run one or more commands sequentially. Each item must contain exactly one of argv or command. Prefer structured argv for real executables. PowerShell cmdlets such as Remove-Item and Get-Content, pipelines, redirection, and other shell syntax must use command. Each step is permission-checked independently.", "parameters": {"type": "object", "properties": {"commands": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object", "properties": {"command": {"type": "string", "minLength": 1}, "argv": {"type": "array", "minItems": 1, "maxItems": 64, "items": {"type": "string"}}, "purpose": {"type": "string", "enum": ["test", "build", "lint", "other"]}, "timeout_sec": {"type": "integer", "minimum": 1, "default": 30}, "expected_exit_codes": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 8, "default": [0]}}, "required": ["purpose"]}}, "stop_on_failure": {"type": "boolean", "default": True}}, "required": ["commands"]}}},
]


class ToolRuntime:
    def __init__(
        self,
        workspace: str | Path,
        *,
        approver: Approver,
        reviewer: Reviewer | None = None,
        mode: AgentMode = AgentMode.ACT,
    ) -> None:
        self.guard = WorkspaceGuard(workspace)
        self.approver = approver
        self.reviewer = reviewer
        self.mode = mode
        self.permissions = PermissionPolicy(self.guard.root)
        self.read_paths: set[Path] = set()
        self._recent_files: deque[Path] = deque()
        self.change_seq = 0
        self.last_verification: dict[str, Any] | None = None
        self.prompt_index = 0
        self._file_changes: dict[int, dict[Path, FileChange]] = {}
        self._interrupt_checker: InterruptChecker = lambda: False

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
            if any(part.casefold() in IGNORED_DISCOVERY_DIRECTORIES for part in candidate.parts): continue
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
            if any(part.casefold() in IGNORED_DISCOVERY_DIRECTORIES for part in file.parts): continue
            if self.permissions.is_protected_path(self.guard.relative(resolved)): continue
            try: lines = resolved.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError): continue
            for number, line in enumerate(lines, 1):
                if query in line: matches.append({"path": self.guard.relative(resolved), "line": number, "text": line})
        return ToolResult.success(tool="search_text", call_id=call_id, summary=f"found {len(matches)} matches", data={"matches": matches})

    def read_file(
        self,
        call_id: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        if start_line is not None and (not isinstance(start_line, int) or start_line < 1):
            return self._failure("read_file", call_id, "INVALID_ARGUMENT", "start_line must be a positive integer")
        if end_line is not None and (not isinstance(end_line, int) or end_line < 1):
            return self._failure("read_file", call_id, "INVALID_ARGUMENT", "end_line must be a positive integer")
        if start_line is not None and end_line is not None and end_line < start_line:
            return self._failure("read_file", call_id, "INVALID_ARGUMENT", "end_line must not be less than start_line")
        target = self._path("read_file", call_id, path)
        if isinstance(target, ToolResult): return target
        try: content = target.read_text(encoding="utf-8")
        except FileNotFoundError: return self._failure("read_file", call_id, "FILE_NOT_FOUND", f"file not found: {path}")
        except (OSError, UnicodeDecodeError) as exc: return self._failure("read_file", call_id, "READ_ERROR", str(exc))
        self.read_paths.add(target)
        self._touch_recent_file(target)
        if start_line is None and end_line is None:
            return ToolResult.success(tool="read_file", call_id=call_id, summary=f"read {len(content)} characters", data={"path": self.guard.relative(target), "content": content})
        lines = content.splitlines()
        first = start_line or 1
        last = end_line or first + 199
        if last - first + 1 > 1_000:
            return self._failure("read_file", call_id, "INVALID_ARGUMENT", "a line range may contain at most 1000 lines")
        actual_last = min(last, len(lines))
        numbered = "\n".join(f"{number}: {lines[number - 1]}" for number in range(first, actual_last + 1))
        data = {
            "path": self.guard.relative(target),
            "content": numbered,
            "start_line": first,
            "end_line": actual_last,
            "total_lines": len(lines),
            "truncated": first > 1 or actual_last < len(lines),
        }
        return ToolResult.success(
            tool="read_file",
            call_id=call_id,
            summary=f"read lines {first}-{actual_last} of {len(lines)}",
            data=data,
        )

    def _touch_recent_file(self, target: Path) -> None:
        try:
            self._recent_files.remove(target)
        except ValueError:
            pass
        self._recent_files.append(target)

    def context_checkpoint(
        self,
        *,
        max_files: int = 5,
        per_file_chars: int = 6_000,
        total_chars: int = 30_000,
    ) -> list[dict[str, str]]:
        """Return bounded current-disk snapshots for recent workspace files."""
        files: list[dict[str, str]] = []
        used = 0
        for target in reversed(self._recent_files):
            if len(files) >= max_files or used >= total_chars:
                break
            try:
                resolved = target.resolve()
                if not resolved.is_relative_to(self.guard.root) or not resolved.is_file():
                    continue
                relative = self.guard.relative(resolved)
                if self.permissions.is_protected_path(relative):
                    continue
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            limit = min(per_file_chars, total_chars - used)
            preview, _ = truncate_text(content, limit=limit)
            files.append({"path": relative, "content": preview})
            used += len(preview)
        return files

    def _require_read(self, tool: str, call_id: str, target: Path) -> ToolResult | None:
        if target.exists() and target not in self.read_paths:
            return self._failure(tool, call_id, "READ_REQUIRED", "existing file must be read before editing")
        return None

    @staticmethod
    def _diff_stats(diff: str) -> tuple[int, int]:
        lines = diff.splitlines()
        additions = sum(line.startswith("+") and not line.startswith("+++") for line in lines)
        deletions = sum(line.startswith("-") and not line.startswith("---") for line in lines)
        return additions, deletions

    def begin_prompt(self, prompt_index: int) -> None:
        self.prompt_index = prompt_index
        self._file_changes.setdefault(prompt_index, {})

    def _changed(self, target: Path, before: str, after: str) -> FileChange:
        self.read_paths.add(target)
        self._touch_recent_file(target)
        self.change_seq += 1
        self.last_verification = None
        changes = self._file_changes.setdefault(self.prompt_index, {})
        existing = changes.get(target)
        baseline = existing.before if existing else before
        path = self.guard.relative(target)
        diff = "".join(
            difflib.unified_diff(
                baseline.splitlines(True),
                after.splitlines(True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        additions, deletions = self._diff_stats(diff)
        change = FileChange(
            path=path,
            prompt_index=self.prompt_index,
            before=baseline,
            after=after,
            additions=additions,
            deletions=deletions,
            diff=diff,
            first_change_seq=existing.first_change_seq if existing else self.change_seq,
            last_change_seq=self.change_seq,
        )
        changes[target] = change
        return change

    def changes_snapshot(self, prompt_index: int | None = None) -> list[dict[str, Any]]:
        if prompt_index is not None:
            changes = self._file_changes.get(prompt_index, {})
            return [asdict(change) for change in sorted(changes.values(), key=lambda item: item.path)]
        return [
            asdict(change)
            for index in sorted(self._file_changes)
            for change in sorted(self._file_changes[index].values(), key=lambda item: item.path)
        ]

    def set_mode(self, mode: AgentMode) -> None:
        self.mode = mode

    def set_interrupt_checker(self, checker: InterruptChecker) -> None:
        self._interrupt_checker = checker

    def _interrupted(self) -> bool:
        return bool(self._interrupt_checker())

    def _approve(self, decision: PermissionDecision, prompt: ApprovalPrompt) -> bool:
        if self._interrupted():
            return False
        if decision.action is PermissionAction.ALLOW:
            allowed = True
        elif decision.action is PermissionAction.DENY:
            allowed = False
        else:
            allowed = self.approver(prompt)
        return allowed and not self._interrupted()

    def _approval_result(self, tool: str, call_id: str) -> ToolResult:
        return self._failure(tool, call_id, "INTERRUPTED", "tool execution was interrupted")

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
        existed = target.exists()
        old = target.read_text(encoding="utf-8") if existed else ""
        if existed and old == content: return self._failure("write_file", call_id, "NO_CHANGE", "new content is identical")
        diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}"))
        allowed, decision = self._file_change_allowed("write_file", path, diff)
        if not allowed:
            if self._interrupted():
                return self._approval_result("write_file", call_id)
            code = "PERMISSION_DENIED" if decision.action is PermissionAction.DENY else "CHANGE_REJECTED"
            return self._failure("write_file", call_id, code, decision.reason if decision.action is PermissionAction.DENY else "user rejected file change")
        if self._interrupted():
            return self._approval_result("write_file", call_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        change = self._changed(target, old, content)
        return ToolResult.success(
            tool="write_file", call_id=call_id, summary=f"wrote {path}",
            data={
                "path": change.path,
                "prompt_index": change.prompt_index,
                "additions": change.additions,
                "deletions": change.deletions,
                "first_change_seq": change.first_change_seq,
                "last_change_seq": change.last_change_seq,
                "permission": decision.rule_id,
            },
        )

    def edit_file(
        self,
        call_id: str,
        path: str,
        old_text: str | None = None,
        new_text: str | None = None,
        edits: list[dict[str, str]] | None = None,
    ) -> ToolResult:
        target = self._path("edit_file", call_id, path)
        if isinstance(target, ToolResult): return target
        denied = self._require_read("edit_file", call_id, target)
        if denied: return denied
        try: old = target.read_text(encoding="utf-8")
        except FileNotFoundError: return self._failure("edit_file", call_id, "FILE_NOT_FOUND", f"file not found: {path}")
        legacy_requested = old_text is not None or new_text is not None
        batch_requested = edits is not None
        if legacy_requested == batch_requested:
            return self._failure("edit_file", call_id, "INVALID_ARGUMENT", "provide exactly one of old_text/new_text or edits")
        if legacy_requested:
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                return self._failure("edit_file", call_id, "INVALID_ARGUMENT", "old_text and new_text must both be strings")
            edit_items = [{"old_text": old_text, "new_text": new_text}]
        else:
            if not isinstance(edits, list) or not 1 <= len(edits) <= 12:
                return self._failure("edit_file", call_id, "INVALID_ARGUMENT", "edits must contain between 1 and 12 entries")
            edit_items = edits

        new = old
        for index, item in enumerate(edit_items):
            if not isinstance(item, dict) or set(item) != {"old_text", "new_text"}:
                return self._failure("edit_file", call_id, "INVALID_ARGUMENT", f"edits[{index}] must contain only old_text and new_text")
            item_old = item.get("old_text")
            item_new = item.get("new_text")
            if not isinstance(item_old, str) or not isinstance(item_new, str):
                return self._failure("edit_file", call_id, "INVALID_ARGUMENT", f"edits[{index}] values must be strings")
            if not item_old:
                return self._failure("edit_file", call_id, "INVALID_ARGUMENT", f"edits[{index}].old_text must not be empty")
            if item_old == item_new:
                return self._failure("edit_file", call_id, "NO_CHANGE", f"edits[{index}] old_text and new_text are identical")
            count = new.count(item_old)
            if count == 0:
                return self._failure("edit_file", call_id, "OLD_TEXT_NOT_FOUND", f"edits[{index}] old_text was not found")
            if count > 1:
                return self._failure("edit_file", call_id, "AMBIGUOUS_MATCH", f"edits[{index}] old_text matched {count} times")
            new = new.replace(item_old, item_new, 1)
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}"))
        allowed, decision = self._file_change_allowed("edit_file", path, diff)
        if not allowed:
            if self._interrupted():
                return self._approval_result("edit_file", call_id)
            code = "PERMISSION_DENIED" if decision.action is PermissionAction.DENY else "CHANGE_REJECTED"
            return self._failure("edit_file", call_id, code, decision.reason if decision.action is PermissionAction.DENY else "user rejected file change")
        if self._interrupted():
            return self._approval_result("edit_file", call_id)
        target.write_text(new, encoding="utf-8")
        change = self._changed(target, old, new)
        return ToolResult.success(
            tool="edit_file", call_id=call_id, summary=f"edited {path}",
            data={
                "path": change.path,
                "prompt_index": change.prompt_index,
                "additions": change.additions,
                "deletions": change.deletions,
                "first_change_seq": change.first_change_seq,
                "last_change_seq": change.last_change_seq,
                "edit_count": len(edit_items),
                "permission": decision.rule_id,
            },
        )

    @staticmethod
    def _shell_argv(command: str) -> tuple[str, list[str]]:
        if os.name == "nt":
            executable = shutil.which("pwsh") or "powershell.exe"
            wrapped = (
                "$ErrorActionPreference = 'Stop'; "
                "try { "
                f"& {{ {command} }}; "
                "$__minicodex_exit = $LASTEXITCODE; "
                "if ($null -ne $__minicodex_exit -and $__minicodex_exit -ne 0) { exit $__minicodex_exit }; "
                "exit 0 "
                "} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
            )
            return "powershell", [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                wrapped,
            ]
        return "sh", ["/bin/sh", "-lc", command]

    def run_shell(self, call_id: str, commands: list[dict[str, Any]], *, stop_on_failure: bool = True) -> ToolResult:
        if not isinstance(commands, list) or not 1 <= len(commands) <= 8:
            return self._failure("run_shell", call_id, "INVALID_ARGUMENT", "commands must contain between 1 and 8 entries")
        if not isinstance(stop_on_failure, bool):
            return self._failure("run_shell", call_id, "INVALID_ARGUMENT", "stop_on_failure must be boolean")
        prepared: list[dict[str, Any]] = []
        for index, item in enumerate(commands):
            if not isinstance(item, dict):
                return self._failure("run_shell", call_id, "INVALID_ARGUMENT", f"commands[{index}] must be an object")
            command = item.get("command")
            argv = item.get("argv")
            purpose = item.get("purpose")
            requested_timeout_sec = item.get("timeout_sec", 30)
            expected_exit_codes = item.get("expected_exit_codes", [0])
            has_command = isinstance(command, str) and bool(command.strip())
            has_argv = isinstance(argv, list) and bool(argv) and all(isinstance(arg, str) and arg for arg in argv)
            if has_command == has_argv:
                return self._failure("run_shell", call_id, "INVALID_ARGUMENT", f"commands[{index}] must contain exactly one of command or argv")
            if has_argv and len(argv) > 64:
                return self._failure("run_shell", call_id, "INVALID_ARGUMENT", f"commands[{index}].argv may contain at most 64 entries")
            if has_argv and os.name == "nt" and argv[0].casefold() in WINDOWS_POWERSHELL_COMMANDS:
                return ToolResult.failure(
                    tool="run_shell",
                    call_id=call_id,
                    code="SHELL_REQUIRED",
                    message=(
                        f"{argv[0]} is a PowerShell command and cannot be launched through argv; "
                        "use the command field instead"
                    ),
                    retryable=True,
                    data={"command_name": argv[0], "recommended_field": "command", "command_index": index},
                )
            if purpose not in {"test", "build", "lint", "other"}:
                return self._failure("run_shell", call_id, "INVALID_ARGUMENT", f"commands[{index}].purpose is invalid")
            if not isinstance(requested_timeout_sec, int) or requested_timeout_sec < 1:
                return self._failure("run_shell", call_id, "INVALID_ARGUMENT", f"commands[{index}].timeout_sec must be a positive integer")
            if (
                not isinstance(expected_exit_codes, list)
                or not 1 <= len(expected_exit_codes) <= 8
                or any(not isinstance(code, int) for code in expected_exit_codes)
            ):
                return self._failure("run_shell", call_id, "INVALID_ARGUMENT", f"commands[{index}].expected_exit_codes must contain 1 to 8 integers")
            prepared.append({
                "command": command.strip() if has_command else subprocess.list2cmdline(argv),
                "argv": list(argv) if has_argv else None,
                "purpose": purpose,
                "requested_timeout_sec": requested_timeout_sec,
                "timeout_sec": min(requested_timeout_sec, 120),
                "expected_exit_codes": list(dict.fromkeys(expected_exit_codes)),
            })

        child_env = os.environ.copy()
        for secret_name in ("MINICODEX_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            child_env.pop(secret_name, None)
        local_tmp = self.guard.root / ".minicodex" / "tmp"
        for temp_name in ("TMP", "TEMP", "TMPDIR"):
            child_env[temp_name] = str(local_tmp)

        results: list[dict[str, Any]] = []
        failed_index: int | None = None
        failure_code: str | None = None
        failure_message: str | None = None
        for index, item in enumerate(prepared):
            command = item["command"]
            argv = item["argv"]
            purpose = item["purpose"]
            timeout_sec = item["timeout_sec"]
            requested_timeout_sec = item["requested_timeout_sec"]
            expected_exit_codes = item["expected_exit_codes"]
            if failed_index is not None and stop_on_failure:
                results.append({"index": index, "status": "skipped", "command": command, "purpose": purpose})
                continue

            analysis = self.permissions.analyze_shell(command)
            decision = self.permissions.decide_shell(self.mode, command, purpose, analysis=analysis)
            prompt = ApprovalPrompt(
                kind="command",
                tool="run_shell",
                summary=f"run shell command {index + 1} of {len(prepared)}",
                reason=decision.reason,
                risk=decision.risk,
                rule_id=decision.rule_id,
                details={
                    "index": index,
                    "count": len(prepared),
                    "command": command,
                    "purpose": purpose,
                    "timeout_sec": timeout_sec,
                    "requested_timeout_sec": requested_timeout_sec,
                    "expected_exit_codes": expected_exit_codes,
                    "workspace": str(self.guard.root),
                    "signals": list(decision.signals),
                    "analysis": analysis.to_dict(),
                },
            )
            review: ReviewOutcome | None = None
            if decision.action is PermissionAction.DENY:
                failed_index, failure_code, failure_message = index, "COMMAND_DENIED", decision.reason
                results.append({"index": index, "status": "denied", "command": command, "purpose": purpose, "reason": decision.reason})
                continue
            if decision.action is PermissionAction.REVIEW:
                review = self.reviewer(prompt) if self.reviewer else ReviewOutcome(
                    ReviewDecision.ESCALATE,
                    "automatic reviewer is not configured",
                    "medium",
                )
                prompt.details["review"] = {
                    "decision": review.decision.value,
                    "reason": review.reason,
                    "risk": review.risk,
                }
                allowed = review.decision is ReviewDecision.ALLOW
                if not allowed and not self._interrupted():
                    allowed = self.approver(prompt)
                allowed = allowed and not self._interrupted()
            else:
                allowed = self._approve(decision, prompt)
            if not allowed:
                if self._interrupted():
                    return self._approval_result("run_shell", call_id)
                failed_index, failure_code, failure_message = index, "COMMAND_REJECTED", "user rejected command"
                results.append({
                    "index": index,
                    "status": "rejected",
                    "command": command,
                    "purpose": purpose,
                    **({"review": prompt.details["review"]} if review else {}),
                })
                continue

            if self._interrupted():
                return self._approval_result("run_shell", call_id)
            shell_name, shell_argv = ("argv", argv) if argv is not None else self._shell_argv(command)
            common = {
                "index": index,
                "command": command,
                **({"argv": argv} if argv is not None else {}),
                "shell": shell_name,
                "purpose": purpose,
                "timeout_sec": timeout_sec,
                "requested_timeout_sec": requested_timeout_sec,
                "timeout_normalized": requested_timeout_sec != timeout_sec,
                "expected_exit_codes": expected_exit_codes,
                "permission": decision.rule_id,
                "analysis": analysis.to_dict(),
                **({"review": prompt.details["review"]} if review else {}),
            }
            try:
                local_tmp.mkdir(parents=True, exist_ok=True)
                completed = subprocess.run(
                    shell_argv,
                    cwd=self.guard.root,
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_sec,
                    env=child_env,
                )
                step = {
                    **common,
                    "status": "completed",
                    "exit_code": completed.returncode,
                    "exit_code_expected": completed.returncode in expected_exit_codes,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            except subprocess.TimeoutExpired as exc:
                step = {**common, "status": "timeout", "exit_code": None, "stdout": exc.stdout or "", "stderr": exc.stderr or ""}
                failed_index, failure_code, failure_message = index, "COMMAND_TIMEOUT", f"command {index + 1} timed out after {timeout_sec}s"
            except FileNotFoundError as exc:
                step = {
                    **common,
                    "status": "spawn_failed",
                    "exit_code": None,
                    "executable": str(shell_argv[0]),
                    "stdout": "",
                    "stderr": str(exc),
                }
                failed_index, failure_code, failure_message = index, "COMMAND_SPAWN_FAILED", str(exc)
            except OSError as exc:
                step = {**common, "status": "error", "exit_code": None, "stdout": "", "stderr": str(exc)}
                failed_index, failure_code, failure_message = index, "COMMAND_ERROR", str(exc)
            results.append(step)
            if step.get("exit_code") is not None and not step.get("exit_code_expected", False):
                failed_index, failure_code, failure_message = index, "COMMAND_FAILED", f"command {index + 1} exited {step['exit_code']}"
            is_verification = any(operation.startswith("verification.") for operation in analysis.operations)
            if purpose in {"test", "build", "lint"} and is_verification and self.change_seq and step.get("exit_code") is not None:
                self.last_verification = {
                    **step,
                    "step_status": step["status"],
                    "status": "VERIFIED" if step.get("exit_code_expected", False) else "FAILED",
                    "change_seq": self.change_seq,
                }

        data = {"commands": results, "stop_on_failure": stop_on_failure, "failed_index": failed_index}
        if failure_code:
            return ToolResult.failure(
                tool="run_shell",
                call_id=call_id,
                code=failure_code,
                message=failure_message or failure_code,
                retryable=failure_code == "COMMAND_SPAWN_FAILED",
                data=data,
            )
        return ToolResult.success(tool="run_shell", call_id=call_id, summary=f"completed {len(results)} shell command(s)", data=data)

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
