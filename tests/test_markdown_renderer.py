from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "tests" / "js" / "markdown_renderer_test.cjs"
RENDERER = ROOT / "src" / "minicodex" / "web" / "static" / "markdown.js"


@pytest.mark.skipif(NODE is None, reason="Node.js is optional and unavailable")
@pytest.mark.parametrize("mode", ["normal", "empty-markers", "invalid-entities", "diff-lines"])
def test_markdown_renderer_is_safe_and_terminates(mode: str) -> None:
    completed = subprocess.run(
        [NODE, str(SCRIPT), str(RENDERER), mode],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
