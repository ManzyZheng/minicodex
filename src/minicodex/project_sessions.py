from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .persistence import ApplicationPaths, atomic_write_json, read_json


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
        atomic_write_json(self.paths.session_root(project_id, record.id) / "state.json", {"messages": [], "prompt_count": 0})
        return record

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

    def save_state(self, project_id: str, session_id: str, state: dict[str, Any]) -> None:
        if self.get(project_id, session_id) is None:
            raise KeyError(session_id)
        atomic_write_json(self.paths.session_root(project_id, session_id) / "state.json", state)

    def load_state(self, project_id: str, session_id: str) -> dict[str, Any]:
        if self.get(project_id, session_id) is None:
            raise KeyError(session_id)
        payload = read_json(self.paths.session_root(project_id, session_id) / "state.json", {})
        return payload if isinstance(payload, dict) else {}
