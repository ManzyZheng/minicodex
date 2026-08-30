from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Literal

from .shell_analysis import ShellAnalysis, ShellCommandAnalyzer


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
    REVIEW = "review"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str
    risk: Literal["low", "medium", "high"]
    rule_id: str
    signals: tuple[str, ...] = ()


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
class PermissionPolicy:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.shell_analyzer = ShellCommandAnalyzer(self.workspace)

    @staticmethod
    def _decision(
        action: PermissionAction,
        reason: str,
        risk: Literal["low", "medium", "high"],
        rule_id: str,
        *signals: str,
    ) -> PermissionDecision:
        return PermissionDecision(action, reason, risk, rule_id, tuple(signals))

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
    def _matches(command: str, patterns: tuple[str, ...]) -> bool:
        return any(re.search(pattern, command, re.IGNORECASE) for pattern in patterns)

    def _references_outside_workspace(self, command: str) -> bool:
        candidates = re.findall(r"(?<![\w])([a-zA-Z]:[\\/][^\s'\"|;&]+)", command)
        candidates += re.findall(r"(?<![\w:/])(/(?!/)[^\s'\"|;&]+)", command)
        for match in candidates:
            if match.casefold() in {"/c", "/s", "/q", "/f"}:
                continue
            try:
                candidate = Path(match.rstrip(".,)")).resolve()
            except (OSError, ValueError):
                return True
            if not candidate.is_relative_to(self.workspace):
                return True
        return False

    @classmethod
    def _references_protected_credential(cls, command: str) -> bool:
        tokens = re.findall(r"'[^']*'|\"[^\"]*\"|\S+", command)
        return any(cls.is_protected_path(token.strip("'\",;()")) for token in tokens)

    def analyze_shell(self, command: str) -> ShellAnalysis:
        return self.shell_analyzer.analyze(command)

    def decide_shell(
        self,
        mode: AgentMode,
        command: str,
        purpose: str,
        *,
        analysis: ShellAnalysis | None = None,
    ) -> PermissionDecision:
        normalized = command.strip()
        analysis = analysis or self.analyze_shell(normalized)
        operations = set(analysis.operations)
        dangerous = (
            r"\bgit\s+reset\s+--hard\b",
            r"\bgit\s+clean\s+-[^\s]*f",
            r"\bgit\s+push\b[^\r\n]*(?:--force(?:-with-lease)?|\s-f(?:\s|$))",
            r"\b(?:format-volume|format\.com|shutdown(?:\.exe)?)\b",
            r"(?:^|\s)-(?:encodedcommand|enc)\b",
            r"\b(?:invoke-expression|iex)\b",
        )
        if self._matches(normalized, dangerous) or operations & {"git.reset_hard", "git.clean_force", "git.push_force"} or "opaque_execution" in analysis.signals:
            return self._decision(PermissionAction.DENY, "destructive command is blocked", "high", "global.dangerous_command")
        if mode is AgentMode.PLAN:
            return self._decision(PermissionAction.DENY, "commands are unavailable in Plan Mode", "medium", "plan.read_only")
        if mode is AgentMode.ACT:
            return self._decision(PermissionAction.ASK, "ACT requires command approval", "medium", "act.command")
        if self._references_protected_credential(normalized):
            return self._decision(PermissionAction.DENY, "command references protected credentials", "high", "global.protected_credential")
        delete_decision: PermissionDecision | None = None
        if "filesystem.delete" in analysis.operations:
            relations = {target.relation for target in analysis.targets}
            if not analysis.targets:
                delete_decision = self._decision(
                    PermissionAction.REVIEW,
                    "delete target is supplied indirectly or cannot be extracted",
                    "medium",
                    "auto_act.reviewer",
                    *analysis.signals,
                )
            if relations & {"workspace_root", "system_root", "protected", "dynamic", "unknown"}:
                return self._decision(
                    PermissionAction.DENY,
                    "delete target is broad, protected, dynamic, or cannot be resolved",
                    "high",
                    "global.dangerous_delete",
                    *analysis.signals,
                )
            if "outside_workspace" in relations:
                return self._decision(
                    PermissionAction.DENY,
                    "recursive or forced deletion outside the workspace is blocked",
                    "high",
                    "global.outside_delete",
                    *analysis.signals,
                )
            if analysis.targets and all(target.generated for target in analysis.targets):
                delete_decision = self._decision(
                    PermissionAction.ALLOW,
                    "cleanup targets are recognized generated artifacts inside the workspace",
                    "low",
                    "auto_act.generated_cleanup",
                    *analysis.signals,
                )
            elif delete_decision is None:
                delete_decision = self._decision(
                    PermissionAction.REVIEW,
                    "workspace deletion needs reviewer judgment",
                    "medium",
                    "auto_act.reviewer",
                    *analysis.signals,
                )
        if self._references_outside_workspace(normalized) or (
            "relative_parent_path" in analysis.signals and operations - {"filesystem.delete"}
        ):
            return self._decision(PermissionAction.ASK, "command references a path outside the workspace", "high", "auto_act.outside_workspace", "outside_workspace")
        if operations & {"git.push", "system.configure"}:
            return self._decision(PermissionAction.ASK, "command crosses a user or system boundary", "high", "auto_act.user_boundary", "user_boundary")
        if operations & {"package.install", "network.request", "git.rebase", "shell.nested"} or not analysis.fully_analyzed:
            return self._decision(PermissionAction.REVIEW, "command needs reviewer judgment", "medium", "auto_act.reviewer", *analysis.signals)
        if delete_decision is not None:
            return delete_decision
        return self._decision(PermissionAction.ALLOW, "ordinary local shell command", "low", "auto_act.local_shell")
