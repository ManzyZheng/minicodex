from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_REFERENCE_COUNT = 8
MAX_REFERENCE_BYTES = 64 * 1024
MAX_TOTAL_REFERENCE_BYTES = 128 * 1024

ALLOWED_REFERENCE_SUFFIXES = {
    ".cfg",
    ".csv",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
SENSITIVE_REFERENCE_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json"}
SENSITIVE_REFERENCE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

_BRACED_REFERENCE = re.compile(r"(?<![\w@])@\{([^{}\r\n]+)\}")
_PLAIN_REFERENCE = re.compile(r"(?<![\w@])@([^\s@{}]+)")
_TRAILING_PUNCTUATION = ",.;!?，。；！？"


class ExternalReferenceError(ValueError):
    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class ExternalReference:
    id: str
    path: Path
    name: str
    content: str
    size: int
    modified_at: int
    scope: str

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "name": self.name,
            "size": self.size,
            "modified_at": self.modified_at,
            "scope": self.scope,
            "access": "read-only-session-snapshot",
        }


class ExternalReferenceRegistry:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ExternalReferenceError(
                "WORKSPACE_NOT_FOUND",
                f"workspace does not exist: {self.workspace}",
                path=str(self.workspace),
            )
        self._references: dict[Path, ExternalReference] = {}

    @staticmethod
    def parse(text: str) -> list[str]:
        braced_spans: list[tuple[int, int]] = []
        matches: list[tuple[int, str]] = []
        for match in _BRACED_REFERENCE.finditer(text):
            braced_spans.append(match.span())
            matches.append((match.start(), match.group(1).strip()))
        for match in _PLAIN_REFERENCE.finditer(text):
            if any(start <= match.start() < end for start, end in braced_spans):
                continue
            value = match.group(1).rstrip(_TRAILING_PUNCTUATION)
            candidate = Path(value)
            name = candidate.name.casefold()
            if value and (
                candidate.suffix.casefold() in ALLOWED_REFERENCE_SUFFIXES
                or name in SENSITIVE_REFERENCE_NAMES
                or name.startswith(".env.")
                or candidate.suffix.casefold() in SENSITIVE_REFERENCE_SUFFIXES
            ):
                matches.append((match.start(), value))
        return [value for _position, value in sorted(matches, key=lambda item: item[0])]

    def _resolve(self, value: str) -> Path:
        raw = Path(value).expanduser()
        return raw.resolve() if raw.is_absolute() else (self.workspace / raw).resolve()

    @staticmethod
    def _is_sensitive(path: Path) -> bool:
        name = path.name.casefold()
        if name == ".env" or name.startswith(".env."):
            return True
        if name in SENSITIVE_REFERENCE_NAMES:
            return True
        return path.suffix.casefold() in SENSITIVE_REFERENCE_SUFFIXES

    def _read(self, path: Path, *, existing_id: str | None = None) -> ExternalReference:
        display = str(path)
        if not path.exists():
            raise ExternalReferenceError("REFERENCE_NOT_FOUND", f"reference file not found: {display}", path=display)
        if not path.is_file():
            raise ExternalReferenceError("REFERENCE_NOT_FILE", f"reference path is not a file: {display}", path=display)
        if self._is_sensitive(path):
            raise ExternalReferenceError("SENSITIVE_REFERENCE", f"sensitive reference is not allowed: {path.name}", path=display)
        if path.suffix.casefold() not in ALLOWED_REFERENCE_SUFFIXES:
            raise ExternalReferenceError(
                "UNSUPPORTED_REFERENCE_TYPE",
                f"unsupported reference file type: {path.suffix or '(none)'}",
                path=display,
            )
        try:
            stat = path.stat()
        except OSError as exc:
            raise ExternalReferenceError("REFERENCE_READ_ERROR", str(exc), path=display) from exc
        if stat.st_size > MAX_REFERENCE_BYTES:
            raise ExternalReferenceError(
                "REFERENCE_TOO_LARGE",
                f"reference exceeds {MAX_REFERENCE_BYTES} bytes: {display}",
                path=display,
            )
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ExternalReferenceError("REFERENCE_ENCODING", f"reference must be UTF-8 text: {display}", path=display) from exc
        except OSError as exc:
            raise ExternalReferenceError("REFERENCE_READ_ERROR", str(exc), path=display) from exc
        if "\x00" in content:
            raise ExternalReferenceError("REFERENCE_ENCODING", f"reference appears to be binary: {display}", path=display)
        try:
            scope = "workspace" if path.is_relative_to(self.workspace) else "external"
        except ValueError:
            scope = "external"
        return ExternalReference(
            id=existing_id or uuid.uuid4().hex,
            path=path,
            name=path.name,
            content=content,
            size=stat.st_size,
            modified_at=stat.st_mtime_ns,
            scope=scope,
        )

    def load_from_prompt(self, text: str) -> list[ExternalReference]:
        paths = [self._resolve(value) for value in self.parse(text)]
        if not paths:
            return []
        prepared: list[ExternalReference] = []
        simulated = dict(self._references)
        for path in paths:
            current = simulated.get(path)
            reference = self._read(path, existing_id=current.id if current else None)
            if current is None and len(simulated) >= MAX_REFERENCE_COUNT:
                raise ExternalReferenceError(
                    "REFERENCE_COUNT_LIMIT",
                    f"at most {MAX_REFERENCE_COUNT} references may be active",
                    path=str(path),
                )
            simulated[path] = reference
            total = sum(item.size for item in simulated.values())
            if total > MAX_TOTAL_REFERENCE_BYTES:
                raise ExternalReferenceError(
                    "REFERENCE_TOTAL_LIMIT",
                    f"active references exceed {MAX_TOTAL_REFERENCE_BYTES} bytes",
                    path=str(path),
                )
            prepared.append(reference)
        self._references = simulated
        return prepared

    def active(self) -> list[ExternalReference]:
        return list(self._references.values())

    def metadata(self) -> list[dict[str, Any]]:
        return [reference.metadata() for reference in self.active()]

    def remove(self, reference_id: str) -> bool:
        for path, reference in list(self._references.items()):
            if reference.id == reference_id:
                del self._references[path]
                return True
        return False
