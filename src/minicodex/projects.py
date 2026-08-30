from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .persistence import ApplicationPaths, atomic_write_json, read_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    workspace: str
    created_at: str
    updated_at: str
    last_session_id: str | None = None


class ProjectRegistry:
    def __init__(self, paths: ApplicationPaths) -> None:
        self.paths = paths

    def _load(self) -> list[ProjectRecord]:
        payload = read_json(self.paths.registry, {"projects": []}) or {"projects": []}
        return [ProjectRecord(**item) for item in payload.get("projects", [])]

    def _save(self, projects: list[ProjectRecord]) -> None:
        atomic_write_json(self.paths.registry, {"projects": [asdict(item) for item in projects]})

    def list(self) -> list[ProjectRecord]:
        return sorted(self._load(), key=lambda item: item.updated_at, reverse=True)

    def get(self, project_id: str) -> ProjectRecord | None:
        return next((item for item in self._load() if item.id == project_id), None)

    def register(self, workspace: str | Path, *, name: str | None = None) -> ProjectRecord:
        try:
            resolved = Path(workspace).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("workspace must be an existing directory") from exc
        if not resolved.is_dir():
            raise ValueError("workspace must be an existing directory")
        projects = self._load()
        existing = next((item for item in projects if Path(item.workspace) == resolved), None)
        timestamp = _now()
        display_name = (name or resolved.name).strip() or resolved.name
        if existing:
            updated = ProjectRecord(
                existing.id,
                display_name,
                str(resolved),
                existing.created_at,
                timestamp,
                existing.last_session_id,
            )
            projects[projects.index(existing)] = updated
            self._save(projects)
            return updated
        record = ProjectRecord(f"proj_{uuid.uuid4().hex}", display_name, str(resolved), timestamp, timestamp)
        projects.append(record)
        self._save(projects)
        atomic_write_json(self.paths.project_root(record.id) / "project.json", asdict(record))
        return record

    def touch(self, project_id: str, *, last_session_id: str | None = None) -> ProjectRecord:
        projects = self._load()
        current = next((item for item in projects if item.id == project_id), None)
        if current is None:
            raise KeyError(project_id)
        updated = ProjectRecord(
            current.id,
            current.name,
            current.workspace,
            current.created_at,
            _now(),
            last_session_id if last_session_id is not None else current.last_session_id,
        )
        projects[projects.index(current)] = updated
        self._save(projects)
        atomic_write_json(self.paths.project_root(updated.id) / "project.json", asdict(updated))
        return updated

    def remove(self, project_id: str) -> bool:
        projects = self._load()
        remaining = [item for item in projects if item.id != project_id]
        if len(remaining) == len(projects):
            return False
        self._save(remaining)
        return True
