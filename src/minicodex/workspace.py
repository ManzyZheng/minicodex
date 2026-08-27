from __future__ import annotations

from pathlib import Path


class WorkspaceError(ValueError):
    pass


class WorkspaceGuard:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"workspace does not exist: {self.root}")

    def resolve(self, user_path: str | Path) -> Path:
        raw = Path(user_path)
        candidate = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError(f"path is outside workspace: {user_path}")
        return candidate

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()
