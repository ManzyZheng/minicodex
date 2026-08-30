from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_data_root() -> Path:
    configured = os.getenv("MINICODEX_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    local = os.getenv("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return (base / "MiniCodex").resolve()


@dataclass(frozen=True)
class ApplicationPaths:
    root: Path

    def __init__(self, root: str | Path | None = None) -> None:
        object.__setattr__(self, "root", Path(root).expanduser().resolve() if root else default_data_root())
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def registry(self) -> Path:
        return self.root / "registry.json"

    @property
    def global_memory(self) -> Path:
        return self.root / "memory" / "items"

    def project_root(self, project_id: str) -> Path:
        return self.root / "projects" / project_id

    def project_memory(self, project_id: str) -> Path:
        return self.project_root(project_id) / "memory" / "items"

    def session_root(self, project_id: str, session_id: str) -> Path:
        return self.project_root(project_id) / "sessions" / session_id


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
