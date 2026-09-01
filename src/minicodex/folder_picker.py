from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path


_PICK_FOLDER_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$shell = New-Object -ComObject Shell.Application
$folder = $shell.BrowseForFolder(0, '选择 MiniCodex 项目文件夹', 0, 0)
if ($null -ne $folder) { [Console]::Write($folder.Self.Path) }
""".strip()


class WindowsFolderPicker:
    """Open one serialized, host-native folder dialog without model involvement."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self) -> str | None:
        if os.name != "nt":
            raise RuntimeError("the native folder picker is only available on Windows")
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if executable is None:
            raise RuntimeError("Windows PowerShell is required for the native folder picker")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with self._lock:
            completed = subprocess.run(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-STA",
                    "-Command",
                    _PICK_FOLDER_SCRIPT,
                ],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=creation_flags,
            )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "folder picker failed"
            raise RuntimeError(detail)
        selected = completed.stdout.strip()
        if not selected:
            return None
        path = Path(selected).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise RuntimeError("selected path is not a directory")
        return str(path)
