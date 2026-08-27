from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
ROOT = Path(__file__).parents[1]
TEST_SCRIPT = ROOT / "tests" / "js" / "turn_timeline_test.cjs"
APP_SCRIPT = ROOT / "src" / "minicodex" / "web" / "static" / "app.js"


@pytest.mark.skipif(NODE is None, reason="Node.js is optional and unavailable")
def test_events_are_grouped_and_only_the_latest_final_result_stays_expanded() -> None:
    completed = subprocess.run(
        [NODE, str(TEST_SCRIPT), str(APP_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=3,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
