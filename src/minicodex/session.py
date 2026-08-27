from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .workspace import WorkspaceGuard


class SessionTrace:
    def __init__(self, path: str | Path, *, workspace: str | Path | None = None) -> None:
        self.path = WorkspaceGuard(workspace).resolve(path) if workspace is not None else Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, payload: dict[str, Any]) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, "payload": payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
