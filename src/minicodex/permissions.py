from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Literal


class AgentMode(str, Enum):
    PLAN = "plan"
    ACT = "act"
    AUTO_ACT = "auto-act"


class PlanState(str, Enum):
    INACTIVE = "inactive"
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"


class PermissionAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str
    risk: Literal["low", "medium", "high"]
    rule_id: str


@dataclass(frozen=True)
class ApprovalPrompt:
    kind: Literal["command", "file_change"]
    tool: str
    summary: str
    reason: str
    risk: Literal["low", "medium", "high"]
    rule_id: str
    details: dict[str, Any] = field(default_factory=dict)


READ_TOOLS = {"list_files", "search_text", "read_file"}
MUTATING_TOOLS = {"write_file", "edit_file"}
SENSITIVE_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
PROTECTED_PARTS = {".git", ".minicodex"}
SHELL_WRAPPERS = {"bash", "bash.exe", "sh", "sh.exe", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}


class PermissionPolicy:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    @staticmethod
    def _decision(
        action: PermissionAction,
        reason: str,
        risk: Literal["low", "medium", "high"],
        rule_id: str,
    ) -> PermissionDecision:
        return PermissionDecision(action, reason, risk, rule_id)

    @staticmethod
    def is_protected_path(path: str) -> bool:
        normalized = path.replace("\\", "/")
        candidate = PurePath(normalized)
        name = candidate.name.casefold()
        parts = {part.casefold() for part in candidate.parts}
        if parts & PROTECTED_PARTS:
            return True
        if name == ".env.example":
            return False
        return (
            name in SENSITIVE_NAMES
            or name.startswith(".env.")
            or any(name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)
        )

    def decide_tool(self, mode: AgentMode, tool_name: str, arguments: dict[str, Any]) -> PermissionDecision:
        path = arguments.get("path")
        if isinstance(path, str) and self.is_protected_path(path):
            return self._decision(PermissionAction.DENY, f"protected path is unavailable: {path}", "high", "global.protected_path")
        if tool_name in READ_TOOLS:
            return self._decision(PermissionAction.ALLOW, "read-only workspace tool", "low", "global.read_only")
        if tool_name in MUTATING_TOOLS:
            if mode is AgentMode.PLAN:
                return self._decision(PermissionAction.DENY, "Plan Mode is read-only", "medium", "plan.read_only")
            if mode is AgentMode.ACT:
                return self._decision(PermissionAction.ASK, "ACT requires review before file changes", "medium", "act.file_change")
            return self._decision(PermissionAction.ALLOW, "ordinary file change inside the workspace", "low", "auto_act.workspace_edit")
        return self._decision(PermissionAction.ALLOW, "tool is covered by the active mode", "low", "global.default")

    @staticmethod
    def _program(argv: list[str]) -> str:
        return Path(argv[0]).name.casefold() if argv else ""

    @classmethod
    def _is_dangerous_command(cls, argv: list[str]) -> bool:
        if not argv:
            return False
        program = cls._program(argv)
        args = [item.casefold() for item in argv[1:]]
        joined = " ".join(args)
        if program in {"git", "git.exe"}:
            return (
                args[:2] == ["reset", "--hard"]
                or (args and args[0] == "clean" and any("f" in arg.lstrip("-") for arg in args[1:] if arg.startswith("-")))
                or (args and args[0] == "push" and any(arg in {"--force", "-f", "--force-with-lease"} for arg in args[1:]))
            )
        if program in {"rm", "rm.exe"}:
            flags = "".join(arg.lstrip("-") for arg in args if arg.startswith("-"))
            return "r" in flags and "f" in flags
        if program in {"format", "format.com", "shutdown", "shutdown.exe"}:
            return True
        if program in {"cmd", "cmd.exe"}:
            return "rmdir" in joined and "/s" in joined
        if program in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            return "remove-item" in joined and "-recurse" in joined and "-force" in joined
        return False

    @classmethod
    def _is_verification(cls, argv: list[str], purpose: str) -> bool:
        if purpose not in {"test", "build", "lint"} or not argv:
            return False
        program = cls._program(argv)
        args = [item.casefold() for item in argv[1:]]
        if program in {"python", "python.exe", "python3", "python3.exe"}:
            return args[:2] == ["-m", "pytest"]
        if program in {"pytest", "pytest.exe"}:
            return True
        if program in {"npm", "npm.cmd", "npm.exe"}:
            return args[:1] == ["test"] or args[:2] in (["run", "test"], ["run", "lint"], ["run", "build"])
        if program in {"cargo", "cargo.exe"}:
            return args[:1] in (["test"], ["check"])
        if program in {"go", "go.exe"}:
            return args[:1] == ["test"]
        return False

    @classmethod
    def _is_read_only_git(cls, argv: list[str]) -> bool:
        return bool(argv) and cls._program(argv) in {"git", "git.exe"} and len(argv) > 1 and argv[1].casefold() in {
            "status", "diff", "log", "show"
        }

    def decide_command(self, mode: AgentMode, argv: list[str], purpose: str) -> PermissionDecision:
        if self._is_dangerous_command(argv):
            return self._decision(PermissionAction.DENY, "destructive command is blocked", "high", "global.dangerous_command")
        if mode is AgentMode.PLAN:
            return self._decision(PermissionAction.DENY, "commands are unavailable in Plan Mode", "medium", "plan.read_only")
        if mode is AgentMode.ACT:
            return self._decision(PermissionAction.ASK, "ACT requires command approval", "medium", "act.command")
        if self._is_verification(argv, purpose):
            return self._decision(PermissionAction.ALLOW, "recognized local verification command", "low", "auto_act.verification")
        if self._is_read_only_git(argv):
            return self._decision(PermissionAction.ALLOW, "recognized read-only Git command", "low", "auto_act.read_only_git")
        if self._program(argv) in SHELL_WRAPPERS:
            return self._decision(PermissionAction.ASK, "shell wrapper is outside automatic approval", "high", "auto_act.shell_wrapper")
        return self._decision(PermissionAction.ASK, "command is outside the AUTO-ACT allowlist", "medium", "auto_act.unknown_command")
