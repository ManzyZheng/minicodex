from __future__ import annotations

import sys
from pathlib import Path

from minicodex.tools import ToolRuntime


def runtime(tmp_path: Path, *, approve=lambda _argv, _purpose, _timeout: True) -> ToolRuntime:
    return ToolRuntime(tmp_path, command_approver=approve)


def test_read_before_edit_and_unique_diff(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    tools = runtime(tmp_path)

    denied = tools.edit_file("c1", "app.py", "value = 1", "value = 2")
    assert denied.error and denied.error.code == "READ_REQUIRED"

    assert tools.read_file("c2", "app.py").data["content"] == "value = 1\n"
    changed = tools.edit_file("c3", "app.py", "value = 1", "value = 2")
    assert changed.ok
    assert "-value = 1" in changed.data["diff"]
    assert "+value = 2" in changed.data["diff"]
    assert source.read_text(encoding="utf-8") == "value = 2\n"


def test_edit_rejects_zero_or_multiple_matches(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("x = 1\nx = 1\n", encoding="utf-8")
    tools = runtime(tmp_path)
    tools.read_file("r", "app.py")
    ambiguous = tools.edit_file("e1", "app.py", "x = 1", "x = 2")
    missing = tools.edit_file("e2", "app.py", "y = 1", "y = 2")
    assert ambiguous.error and ambiguous.error.code == "AMBIGUOUS_MATCH"
    assert missing.error and missing.error.code == "OLD_TEXT_NOT_FOUND"


def test_write_existing_requires_read_but_new_file_does_not(tmp_path: Path) -> None:
    existing = tmp_path / "old.txt"
    existing.write_text("old", encoding="utf-8")
    tools = runtime(tmp_path)
    assert tools.write_file("w1", "old.txt", "new").error.code == "READ_REQUIRED"
    assert tools.write_file("w2", "new.txt", "hello").ok
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello"


def test_file_listing_search_and_workspace_errors_are_results(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle here\n", encoding="utf-8")
    tools = runtime(tmp_path)
    assert tools.list_files("l", "src").data["files"] == ["src/a.py"]
    assert tools.search_text("s", "needle", "src").data["matches"][0]["line"] == 1
    escaped = tools.read_file("r", "../secret")
    assert not escaped.ok and escaped.error.code == "WORKSPACE_VIOLATION"


def test_command_requires_approval_and_uses_argv(tmp_path: Path) -> None:
    denied = runtime(tmp_path, approve=lambda _argv, _purpose, _timeout: False).run_command(
        "c1", [sys.executable, "-c", "print('should-not-run')"], purpose="test"
    )
    assert denied.error and denied.error.code == "COMMAND_REJECTED"

    accepted = runtime(tmp_path).run_command(
        "c2", [sys.executable, "-c", "print('hello world')"], purpose="test"
    )
    assert accepted.ok
    assert accepted.data["exit_code"] == 0
    assert accepted.data["stdout"].strip() == "hello world"


def test_tool_failure_never_raises_to_caller(tmp_path: Path) -> None:
    result = runtime(tmp_path).execute("unknown", "c", {})
    assert not result.ok
    assert result.error and result.error.code == "UNKNOWN_TOOL"


def test_failed_verification_command_returns_diagnostics(tmp_path: Path) -> None:
    tools = runtime(tmp_path)
    assert tools.write_file("w", "changed.txt", "changed").ok
    result = tools.run_command(
        "c", [sys.executable, "-c", "import sys; print('failure detail', file=sys.stderr); raise SystemExit(3)"], purpose="test"
    )
    assert not result.ok
    assert result.error and result.error.code == "COMMAND_FAILED"
    assert result.data["exit_code"] == 3
    assert "failure detail" in result.data["stderr"]
    assert tools.last_verification["status"] == "FAILED"


def test_search_skips_candidates_that_resolve_outside_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    tools = runtime(workspace)
    monkeypatch.setattr(Path, "rglob", lambda _self, _pattern: iter([outside]))
    result = tools.search_text("s", "SECRET")
    assert result.ok
    assert result.data["matches"] == []


def test_command_does_not_inherit_minicodex_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINICODEX_API_KEY", "must-not-leak")
    result = runtime(tmp_path).run_command(
        "c",
        [sys.executable, "-c", "import os; print(os.getenv('MINICODEX_API_KEY', 'absent'))"],
        purpose="other",
    )
    assert result.ok
    assert result.data["stdout"].strip() == "absent"


def test_command_approval_receives_purpose_and_timeout(tmp_path: Path) -> None:
    approvals = []
    tools = runtime(tmp_path, approve=lambda argv, purpose, timeout: approvals.append((argv, purpose, timeout)) or False)
    tools.run_command("c", [sys.executable, "-V"], purpose="lint", timeout_sec=17)
    assert approvals[0][1] == "lint"
    assert approvals[0][2] == 17
