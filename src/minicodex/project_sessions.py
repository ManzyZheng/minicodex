from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .persistence import ApplicationPaths, atomic_write_json, read_json
from .transcript import SessionTranscript


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SessionRecord:
    id: str
    project_id: str
    title: str
    status: str
    verification: str
    model: str | None
    mode: str
    prompt_count: int
    created_at: str
    updated_at: str
    schema_version: int = 2


class SessionRepository:
    def __init__(self, paths: ApplicationPaths) -> None:
        self.paths = paths

    def _sessions_dir(self, project_id: str) -> Path:
        return self.paths.project_root(project_id) / "sessions"

    def _metadata_path(self, project_id: str, session_id: str) -> Path:
        return self.paths.session_root(project_id, session_id) / "metadata.json"

    def create(
        self,
        project_id: str,
        *,
        title: str = "新会话",
        model: str | None = None,
        mode: str = "act",
    ) -> SessionRecord:
        timestamp = _now()
        record = SessionRecord(
            id=f"sess_{uuid.uuid4().hex}",
            project_id=project_id,
            title=title.strip() or "新会话",
            status="idle",
            verification="NOT_RUN",
            model=model,
            mode=mode,
            prompt_count=0,
            created_at=timestamp,
            updated_at=timestamp,
        )
        atomic_write_json(self._metadata_path(project_id, record.id), asdict(record))
        atomic_write_json(
            self.paths.session_root(project_id, record.id) / "state.json",
            {"schema_version": 2, "messages": [], "prompt_count": 0},
        )
        self._transcript(project_id, record.id).ensure_file()
        return record

    def _transcript(self, project_id: str, session_id: str) -> SessionTranscript:
        return SessionTranscript(self.paths.session_root(project_id, session_id))

    def list(self, project_id: str) -> list[SessionRecord]:
        directory = self._sessions_dir(project_id)
        if not directory.exists():
            return []
        records: list[SessionRecord] = []
        for path in directory.glob("*/metadata.json"):
            payload = read_json(path)
            if isinstance(payload, dict) and payload.get("project_id") == project_id:
                records.append(SessionRecord(**payload))
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    def get(self, project_id: str, session_id: str) -> SessionRecord | None:
        payload = read_json(self._metadata_path(project_id, session_id))
        if not isinstance(payload, dict) or payload.get("project_id") != project_id:
            return None
        return SessionRecord(**payload)

    def update(self, session_id: str, project_id: str, **changes: Any) -> SessionRecord:
        current = self.get(project_id, session_id)
        if current is None:
            raise KeyError(session_id)
        allowed = {"title", "status", "verification", "model", "mode", "prompt_count"}
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"unsupported session fields: {sorted(invalid)}")
        updated = replace(current, updated_at=_now(), **changes)
        atomic_write_json(self._metadata_path(project_id, session_id), asdict(updated))
        return updated

    def rename(self, project_id: str, session_id: str, title: str) -> SessionRecord:
        display_title = title.strip()
        if not display_title:
            raise ValueError("session title must not be blank")
        return self.update(session_id, project_id, title=display_title)

    def delete(self, project_id: str, session_id: str) -> bool:
        current = self.get(project_id, session_id)
        if current is None:
            return False
        owned_root = self.paths.session_root(project_id, current.id)
        sessions_root = (self.paths.project_root(project_id) / "sessions").resolve()
        if owned_root.resolve().parent != sessions_root:
            raise RuntimeError("refusing to remove data outside the MiniCodex sessions directory")
        if owned_root.exists():
            shutil.rmtree(owned_root)
        return True

    def save_state(self, project_id: str, session_id: str, state: dict[str, Any]) -> None:
        if self.get(project_id, session_id) is None:
            raise KeyError(session_id)
        atomic_write_json(self.paths.session_root(project_id, session_id) / "state.json", state)

    def load_state(self, project_id: str, session_id: str) -> dict[str, Any]:
        if self.get(project_id, session_id) is None:
            raise KeyError(session_id)
        payload = read_json(self.paths.session_root(project_id, session_id) / "state.json", {})
        return payload if isinstance(payload, dict) else {}

    def append_transcript_event(
        self,
        project_id: str,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        timestamp: str | None = None,
    ) -> dict[str, Any] | None:
        if self.get(project_id, session_id) is None:
            raise KeyError(session_id)
        return self._transcript(project_id, session_id).append(event_type, payload, timestamp=timestamp)

    def load_transcript(
        self,
        project_id: str,
        session_id: str,
        *,
        before_seq: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if self.get(project_id, session_id) is None:
            raise KeyError(session_id)
        return self._transcript(project_id, session_id).load(before_seq=before_seq, limit=limit)

    def history_snapshot(self, project_id: str, session_id: str) -> list[dict[str, Any]]:
        if self.get(project_id, session_id) is None:
            raise KeyError(session_id)
        return self._transcript(project_id, session_id).history_snapshot()

    def transcript_file_changes(self, project_id: str, session_id: str) -> list[dict[str, Any]]:
        if self.get(project_id, session_id) is None:
            raise KeyError(session_id)
        return self._transcript(project_id, session_id).file_changes()

    def ensure_transcript(self, project_id: str, session_id: str) -> bool:
        if self.get(project_id, session_id) is None:
            raise KeyError(session_id)
        transcript = self._transcript(project_id, session_id)
        transcript.ensure_file()
        if transcript.path.stat().st_size:
            return False
        root = self.paths.session_root(project_id, session_id)
        migrated = False
        current_prompt = 0
        trace_path = root / "trace.jsonl"
        if trace_path.is_file():
            with trace_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    event = record.get("event")
                    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
                    timestamp = record.get("timestamp")
                    if event == "prompt_start" and payload.get("prompt"):
                        current_prompt = max(current_prompt + 1, int(payload.get("prompt_index") or 0))
                        transcript.append(
                            "user_prompt",
                            {"text": payload["prompt"], "prompt_index": current_prompt},
                            timestamp=timestamp,
                        )
                        migrated = True
                    elif event == "final" and payload.get("text"):
                        final = {
                            "text": payload["text"],
                            "turns": payload.get("turns", "—"),
                            "verification_status": payload.get("verification_status", "NOT_RUN"),
                            "prompt_index": current_prompt,
                        }
                        transcript.append("final_answer", final, timestamp=timestamp)
                        transcript.append(
                            "turn_completed",
                            {**final, "stop_reason": payload.get("stop_reason", "COMPLETED")},
                            timestamp=timestamp,
                        )
                        migrated = True
        state = self.load_state(project_id, session_id)
        for change in state.get("file_changes", []) if isinstance(state.get("file_changes"), list) else []:
            if isinstance(change, dict):
                transcript.append("file_changed", change)
                migrated = True
        if not migrated:
            prompt_index = 0
            for message in state.get("messages", []) if isinstance(state.get("messages"), list) else []:
                if not isinstance(message, dict) or message.get("tool_calls"):
                    continue
                if message.get("role") == "user":
                    prompt_index += 1
                    transcript.append(
                        "user_prompt",
                        {"text": str(message.get("content") or ""), "prompt_index": prompt_index},
                    )
                    migrated = True
                elif message.get("role") == "assistant" and prompt_index:
                    transcript.append(
                        "final_answer",
                        {
                            "text": str(message.get("content") or ""),
                            "prompt_index": prompt_index,
                            "turns": "—",
                            "verification_status": "NOT_RUN",
                        },
                    )
                    migrated = True
        return migrated
