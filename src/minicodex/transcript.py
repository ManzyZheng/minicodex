from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VISIBLE_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "user_prompt": ("text", "prompt_index", "references"),
    "progress": ("text", "turn", "prompt_index"),
    "tool_summary": ("text", "tool", "ok", "turn", "prompt_index"),
    "command_summary": ("text", "turn", "prompt_index", "exit_code", "purpose"),
    "file_changed": (
        "path",
        "prompt_index",
        "additions",
        "deletions",
        "first_change_seq",
        "last_change_seq",
        "permission",
    ),
    "final_answer": ("text", "turns", "verification_status", "prompt_index"),
    "turn_completed": ("text", "turns", "verification_status", "stop_reason", "prompt_index"),
    "plan_started": ("execution_mode", "prompt_index"),
    "plan_ready": ("id", "text", "execution_mode", "prompt_index"),
    "plan_resolved": ("id", "action", "prompt_index"),
    "approval_required": (
        "id",
        "kind",
        "tool",
        "summary",
        "reason",
        "risk",
        "rule_id",
        "prompt_index",
    ),
    "approval_resolved": ("id", "allow", "reason", "prompt_index"),
    "context_loaded": ("id", "name", "path", "scope", "prompt_index"),
    "context_removed": ("id", "name", "path", "scope", "prompt_index"),
    "context_error": ("code", "message", "path", "prompt_index"),
    "context_compacted": (
        "before_messages",
        "after_messages",
        "before_tokens",
        "after_tokens",
        "stages",
        "compaction_count",
        "turn",
        "prompt_index",
    ),
    "memory_created": (
        "id",
        "scope",
        "kind",
        "title",
        "content",
        "source",
        "source_session_id",
        "source_prompt_index",
    ),
    "memory_forgotten": ("id", "prompt_index"),
    "interrupt_requested": ("prompt_index",),
    "error": ("code", "message", "prompt_index"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SessionTranscript:
    """Append-only, sanitized presentation history for one Agent Session."""

    def __init__(self, session_root: str | Path) -> None:
        self.root = Path(session_root).resolve()
        self.path = self.root / "transcript.jsonl"
        self._lock = threading.RLock()
        self._last_seq: int | None = None

    def ensure_file(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("seq"), int):
                    records.append(record)
        return records

    def _next_seq(self) -> int:
        if self._last_seq is None:
            records = self._read_all()
            self._last_seq = records[-1]["seq"] if records else 0
        self._last_seq += 1
        return self._last_seq

    def _store_diff(self, payload: dict[str, Any]) -> str | None:
        diff = payload.get("diff")
        if not isinstance(diff, str) or not diff:
            return None
        prompt_index = max(0, int(payload.get("prompt_index", 0)))
        path = str(payload.get("path") or "change")
        digest = hashlib.sha256(f"{prompt_index}\0{path}".encode("utf-8")).hexdigest()[:16]
        relative = Path("artifacts") / "diffs" / f"prompt-{prompt_index}-{digest}.patch"
        _atomic_write_text(self.root / relative, diff)
        return relative.as_posix()

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        timestamp: str | None = None,
    ) -> dict[str, Any] | None:
        allowed = VISIBLE_EVENT_FIELDS.get(event_type)
        if allowed is None:
            return None
        sanitized = {key: payload[key] for key in allowed if key in payload}
        if event_type == "file_changed":
            diff_ref = self._store_diff(payload)
            if diff_ref:
                sanitized["diff_ref"] = diff_ref
        with self._lock:
            self.ensure_file()
            record = {
                "seq": self._next_seq(),
                "timestamp": timestamp or _now(),
                "type": event_type,
                "payload": sanitized,
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
        return record

    def load(self, *, before_seq: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        records = self._read_all()
        if before_seq is not None:
            records = [record for record in records if record["seq"] < before_seq]
        if limit is not None:
            records = [] if limit <= 0 else records[-limit:]
        return records

    def history_snapshot(self) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for event in self._read_all():
            payload = event.get("payload", {})
            prompt_index = int(payload.get("prompt_index") or 0)
            if event.get("type") == "user_prompt":
                history.append({"role": "user", "content": str(payload.get("text") or ""), "prompt_index": prompt_index})
            elif event.get("type") == "final_answer":
                history.append(
                    {
                        "role": "assistant",
                        "content": str(payload.get("text") or ""),
                        "prompt_index": prompt_index,
                        "turns": payload.get("turns", "—"),
                        "verification_status": str(payload.get("verification_status") or "NOT_RUN"),
                    }
                )
        return history

    def file_changes(self) -> list[dict[str, Any]]:
        changes: dict[tuple[int, str], dict[str, Any]] = {}
        for event in self._read_all():
            if event.get("type") != "file_changed":
                continue
            payload = dict(event.get("payload", {}))
            diff_ref = payload.get("diff_ref")
            if isinstance(diff_ref, str):
                candidate = (self.root / diff_ref).resolve()
                if candidate.is_relative_to(self.root) and candidate.is_file():
                    payload["diff"] = candidate.read_text(encoding="utf-8")
            key = (int(payload.get("prompt_index") or 0), str(payload.get("path") or ""))
            changes[key] = payload
        return list(changes.values())
