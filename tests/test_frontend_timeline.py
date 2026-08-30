from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
ROOT = Path(__file__).parents[1]
TEST_SCRIPT = ROOT / "tests" / "js" / "turn_timeline_test.cjs"
APP_SCRIPT = ROOT / "src" / "minicodex" / "web" / "static" / "codex-app.js"
MARKDOWN_SCRIPT = ROOT / "src" / "minicodex" / "web" / "static" / "markdown.js"


@pytest.mark.skipif(NODE is None, reason="Node.js is optional and unavailable")
def test_events_are_grouped_and_all_turns_stay_expanded() -> None:
    completed = subprocess.run(
        [NODE, str(TEST_SCRIPT), str(MARKDOWN_SCRIPT), str(APP_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=3,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_reference_ui_is_present_and_uses_metadata_only_api() -> None:
    html = (ROOT / "src" / "minicodex" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    script = APP_SCRIPT.read_text(encoding="utf-8")

    assert 'id="session-references"' in html
    assert 'id="reference-list"' in html
    assert "本会话参考" in html
    assert "移除只影响后续请求" in html
    assert "/api/references/" in script
    assert 'method: "DELETE"' in script
