from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


Relation = Literal[
    "inside_workspace", "workspace_root", "outside_workspace", "protected",
    "dynamic", "system_root", "unknown",
]
_GENERATED_NAMES = {".pytest_cache", ".tmp-pytest", "__pycache__", "smoke.json", ".coverage"}
_PROTECTED_PARTS = {".git", ".minicodex"}


@dataclass(frozen=True)
class ResourceTarget:
    raw: str
    resolved: str | None
    relation: Relation
    generated: bool = False


@dataclass(frozen=True)
class ShellAnalysis:
    command: str
    segments: tuple[str, ...]
    operations: tuple[str, ...]
    targets: tuple[ResourceTarget, ...]
    signals: tuple[str, ...]
    fully_analyzed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "segments": list(self.segments),
            "operations": list(self.operations),
            "targets": [asdict(target) for target in self.targets],
            "signals": list(self.signals),
            "fully_analyzed": self.fully_analyzed,
        }


class ShellCommandAnalyzer:
    """Conservative command-family analysis, not a complete Shell parser."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    @staticmethod
    def _split(command: str) -> tuple[str, ...]:
        parts: list[str] = []
        start = 0
        quote: str | None = None
        index = 0
        while index < len(command):
            char = command[index]
            if quote:
                if char == quote and (index == 0 or command[index - 1] != "\\"):
                    quote = None
                index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                index += 1
                continue
            length = 2 if command[index:index + 2] in {"&&", "||"} else 1 if char in {";", "|", "&", "\r", "\n"} else 0
            if length:
                part = command[start:index].strip()
                if part:
                    parts.append(part)
                index += length
                start = index
                continue
            index += 1
        tail = command[start:].strip()
        if tail:
            parts.append(tail)
        return tuple(parts)

    @staticmethod
    def _tokens(segment: str) -> list[str]:
        try:
            return [token.strip("'\"") for token in shlex.split(segment, posix=False)]
        except ValueError:
            return segment.split()

    @staticmethod
    def _operation(tokens: list[str]) -> str:
        if not tokens:
            return "process.execute"
        program = Path(tokens[0]).name.casefold()
        args = [item.casefold() for item in tokens[1:]]
        if program in {"remove-item", "rm", "del", "erase", "rmdir", "rd", "ri"}:
            return "filesystem.delete"
        if program == "git":
            index = 0
            while index < len(args) and args[index].startswith("-"):
                index += 2 if args[index] in {"-c", "-c", "--git-dir", "--work-tree"} else 1
            subcommand = args[index] if index < len(args) else ""
            rest = args[index + 1:]
            if subcommand == "reset" and "--hard" in rest: return "git.reset_hard"
            if subcommand == "clean" and any(item == "--force" or (item.startswith("-") and "f" in item[1:]) for item in rest):
                return "git.clean_force"
            if subcommand == "push" and any(item in {"--force", "--force-with-lease", "-f"} for item in rest):
                return "git.push_force"
            if subcommand == "push": return "git.push"
            if subcommand == "rebase": return "git.rebase"
            if subcommand in {"status", "diff", "log", "show", "branch", "rev-parse"}: return "git.read"
            return "git.write"
        if program == "find" and "-delete" in args:
            return "filesystem.delete"
        if program in {"foreach-object", "%", "xargs"} and any(
            Path(item.strip("{}()")).name.casefold() in {"remove-item", "rm", "del", "erase", "rmdir", "rd", "ri"}
            for item in tokens[1:]
        ):
            return "filesystem.delete"
        if (
            program in {"pytest", "ruff", "mypy"}
            or (program in {"python", "python.exe", "py"} and args[:2] == ["-m", "pytest"])
            or (program in {"npm", "pnpm", "yarn"} and args[:1] in (["test"], ["lint"]))
        ):
            return "verification.test"
        if program in {"npm", "pnpm", "yarn", "pip", "pip3"} and any(item in {"install", "add"} for item in args[:2]):
            return "package.install"
        if program in {"curl", "wget", "invoke-webrequest", "iwr"}:
            return "network.request"
        if program in {"setx", "reg", "reg.exe", "sc", "sc.exe", "netsh", "new-service", "set-service"}:
            return "system.configure"
        shells = {"powershell", "powershell.exe", "pwsh", "cmd", "cmd.exe", "bash", "sh", "dash", "zsh", "ksh"}
        if program in shells and any(
            item in {"-command", "-c", "-lc", "/c", "-file"} or item.startswith(("-command:", "/c\"")) for item in args
        ):
            return "shell.nested"
        if program == "env":
            wrapped = next((item for item in args if "=" not in item), "")
            if Path(wrapped).name.casefold() in shells:
                return "shell.nested"
        return "process.execute"

    @staticmethod
    def _delete_arguments(tokens: list[str]) -> list[str]:
        if not tokens:
            return []
        program = Path(tokens[0]).name.casefold()
        if program == "remove-item":
            lowered = [token.casefold() for token in tokens]
            for option in ("-literalpath", "-path"):
                if option in lowered:
                    index = lowered.index(option)
                    return tokens[index + 1:index + 2]
            values: list[str] = []
            skip_value = False
            value_options = {"-filter", "-include", "-exclude", "-erroraction"}
            for token in tokens[1:]:
                if skip_value:
                    skip_value = False
                    continue
                if token.casefold() in value_options:
                    skip_value = True
                elif not token.startswith("-"):
                    values.append(token)
            return values
        if program == "rm":
            return [token for token in tokens[1:] if not token.startswith("-")]
        return [token for token in tokens[1:] if not token.startswith("-") and token.casefold() not in {"/s", "/q", "/f"}]

    @staticmethod
    def _is_dynamic(raw: str) -> bool:
        return bool(re.search(r"\$|%[^%]+%|[`*?]|\$\(|^~(?:[\\/]|$)", raw))

    def _target(self, raw: str) -> ResourceTarget:
        if self._is_dynamic(raw):
            return ResourceTarget(raw, None, "dynamic")
        try:
            candidate = Path(raw)
            resolved = (candidate if candidate.is_absolute() else self.workspace / candidate).resolve()
        except (OSError, ValueError):
            return ResourceTarget(raw, None, "unknown")
        parts = {part.casefold() for part in resolved.parts}
        name = resolved.name.casefold()
        generated = name in _GENERATED_NAMES or name.endswith(".pyc")
        if parts & _PROTECTED_PARTS or name == ".env" or name.startswith(".env."):
            relation: Relation = "protected"
        elif resolved == self.workspace:
            relation = "workspace_root"
        elif resolved == Path(resolved.anchor):
            relation = "system_root"
        elif resolved.is_relative_to(self.workspace):
            relation = "inside_workspace"
        else:
            relation = "outside_workspace"
        return ResourceTarget(raw, str(resolved), relation, generated)

    def analyze(self, command: str) -> ShellAnalysis:
        segments = self._split(command)
        operations: list[str] = []
        targets: list[ResourceTarget] = []
        signals: set[str] = {"compound_command"} if len(segments) > 1 else set()
        fully_analyzed = True
        if re.search(r"(?:^|[\s'\"])\.\.[\\/]", command):
            signals.add("relative_parent_path")
        if re.search(r"(?:^|\s)-(?:encodedcommand|enc|e)\b|\b(?:invoke-expression|iex)\b", command, re.I):
            signals.add("opaque_execution")
            fully_analyzed = False
        if re.search(r"\$\(|`", command):
            signals.add("dynamic_shell")
            fully_analyzed = False
        for segment in segments:
            tokens = self._tokens(segment)
            operation = self._operation(tokens)
            operations.append(operation)
            if operation != "filesystem.delete":
                encoded_flag = "-encodedcommand"
                if tokens and Path(tokens[0]).name.casefold() in {"powershell", "powershell.exe", "pwsh"} and any(
                    token.casefold() == "-ec"
                    or (
                        token.casefold().startswith("-e")
                        and encoded_flag.startswith(token.casefold())
                    )
                    for token in tokens[1:]
                ):
                    signals.add("opaque_execution")
                    fully_analyzed = False
                continue
            if tokens and Path(tokens[0]).name.casefold() not in {"remove-item", "rm", "del", "erase", "rmdir", "rd", "ri"}:
                signals.add("indirect_delete")
                fully_analyzed = False
                continue
            lowered = {token.casefold() for token in tokens}
            if "-recurse" in lowered or lowered & {"-r", "-rf", "-fr", "/s"}:
                signals.add("recursive_delete")
            if "-force" in lowered or lowered & {"-f", "-rf", "-fr"}:
                signals.add("force_delete")
            arguments = self._delete_arguments(tokens)
            if not arguments:
                fully_analyzed = False
                signals.add("missing_delete_target")
            for raw in arguments:
                target = self._target(raw)
                targets.append(target)
                if target.relation in {"dynamic", "unknown"}:
                    fully_analyzed = False
        return ShellAnalysis(command, segments, tuple(operations), tuple(targets), tuple(sorted(signals)), fully_analyzed)
