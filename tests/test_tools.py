from __future__ import annotations

import os
import sys
from pathlib import Path

from minicodex.permissions import AgentMode, ApprovalPrompt
from minicodex.tools import ToolRuntime


def runtime(tmp_path: Path, *, approve=lambda _request: True, reviewer=None, mode: AgentMode = AgentMode.AUTO_ACT) -> ToolRuntime:
    return ToolRuntime(tmp_path, approver=approve, reviewer=reviewer, mode=mode)


def python_command(code: str) -> str:
    if os.name == "nt":
        return f'python -c "{code}"'
    import shlex

    return shlex.join([sys.executable, "-c", code])


def python_script(path: str) -> str:
    if os.name == "nt":
        return f"& '{sys.executable}' '{path}'"
    import shlex

    return shlex.join([sys.executable, path])


def test_read_before_edit_and_unique_diff(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    tools = runtime(tmp_path)

    denied = tools.edit_file("c1", "app.py", "value = 1", "value = 2")
    assert denied.error and denied.error.code == "READ_REQUIRED"

    assert tools.read_file("c2", "app.py").data["content"] == "value = 1\n"
    changed = tools.edit_file("c3", "app.py", "value = 1", "value = 2")
    assert changed.ok
    assert "before" not in changed.data
    assert "after" not in changed.data
    assert "diff" not in changed.data
    snapshot = tools.changes_snapshot()[0]
    assert "-value = 1" in snapshot["diff"]
    assert "+value = 2" in snapshot["diff"]
    assert source.read_text(encoding="utf-8") == "value = 2\n"
    assert tools.context_checkpoint() == [{"path": "app.py", "content": "value = 2\n"}]


def test_read_file_returns_a_numbered_line_range_and_still_authorizes_edit(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    tools = runtime(tmp_path)

    result = tools.read_file("r", "app.py", start_line=2, end_line=3)

    assert result.ok
    assert result.data == {
        "path": "app.py",
        "content": "2: two\n3: three",
        "start_line": 2,
        "end_line": 3,
        "total_lines": 4,
        "truncated": True,
    }
    assert tools.edit_file("e", "app.py", "two", "TWO").ok


def test_multiple_edits_report_one_diff_from_prompt_start_to_latest_content(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")
    tools = runtime(tmp_path)
    tools.begin_prompt(1)
    tools.read_file("r", "app.py")
    tools.edit_file("e1", "app.py", "1", "2")
    tools.edit_file("e2", "app.py", "2", "3")

    change = tools.changes_snapshot(1)[0]

    assert "-value = 1" in change["diff"]
    assert "+value = 3" in change["diff"]
    assert "+value = 2" not in change["diff"]
    assert (change["additions"], change["deletions"]) == (1, 1)


def test_each_prompt_uses_content_at_prompt_start_as_diff_baseline(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")
    tools = runtime(tmp_path)
    tools.begin_prompt(1)
    tools.read_file("r1", "app.py")
    tools.edit_file("e1", "app.py", "1", "2")
    tools.begin_prompt(2)
    tools.edit_file("e2", "app.py", "2", "3")

    first = tools.changes_snapshot(1)[0]
    second = tools.changes_snapshot(2)[0]

    assert "-value = 1" in first["diff"] and "+value = 2" in first["diff"]
    assert "-value = 2" in second["diff"] and "+value = 3" in second["diff"]


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


def test_interrupt_after_approval_prevents_file_side_effect(tmp_path: Path) -> None:
    interrupted = False

    def approve_then_interrupt(_request: ApprovalPrompt) -> bool:
        nonlocal interrupted
        interrupted = True
        return True

    tools = runtime(tmp_path, approve=approve_then_interrupt, mode=AgentMode.ACT)
    tools.set_interrupt_checker(lambda: interrupted)

    result = tools.write_file("w", "cancelled.txt", "must not be written")

    assert not result.ok
    assert result.error and result.error.code == "INTERRUPTED"
    assert not (tmp_path / "cancelled.txt").exists()


def test_file_listing_search_and_workspace_errors_are_results(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle here\n", encoding="utf-8")
    tools = runtime(tmp_path)
    assert tools.list_files("l", "src").data["files"] == ["src/a.py"]
    assert tools.search_text("s", "needle", "src").data["matches"][0]["line"] == 1
    escaped = tools.read_file("r", "../secret")
    assert not escaped.ok and escaped.error.code == "WORKSPACE_VIOLATION"


def test_file_discovery_ignores_generated_and_dependency_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")
    for directory in (".pytest-tmp", ".tmp-pytest", "__pycache__", ".venv", "venv", "node_modules"):
        generated = tmp_path / directory
        generated.mkdir()
        (generated / "noise.py").write_text("needle\n", encoding="utf-8")
    tools = runtime(tmp_path)

    listed = tools.list_files("l").data["files"]
    matches = tools.search_text("s", "needle").data["matches"]

    assert listed == ["src/app.py"]
    assert [match["path"] for match in matches] == ["src/app.py"]


def test_atomic_batch_edit_writes_once_and_rolls_back_on_any_invalid_match(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    tools = runtime(tmp_path)
    tools.read_file("r", "app.py")

    changed = tools.edit_file(
        "e1",
        "app.py",
        edits=[
            {"old_text": "alpha = 1", "new_text": "alpha = 10"},
            {"old_text": "beta = 2", "new_text": "beta = 20"},
        ],
    )

    assert changed.ok
    assert changed.data["edit_count"] == 2
    assert source.read_text(encoding="utf-8") == "alpha = 10\nbeta = 20\n"
    change_seq = tools.change_seq

    failed = tools.edit_file(
        "e2",
        "app.py",
        edits=[
            {"old_text": "alpha = 10", "new_text": "alpha = 100"},
            {"old_text": "missing", "new_text": "never-written"},
        ],
    )

    assert failed.error and failed.error.code == "OLD_TEXT_NOT_FOUND"
    assert "edits[1]" in failed.error.message
    assert source.read_text(encoding="utf-8") == "alpha = 10\nbeta = 20\n"
    assert tools.change_seq == change_seq


def test_shell_requires_approval_and_executes_command_string(tmp_path: Path) -> None:
    denied = runtime(tmp_path, approve=lambda _request: False, mode=AgentMode.ACT).run_shell(
        "c1", [{"command": python_command("print('should-not-run')"), "purpose": "other"}]
    )
    assert denied.error and denied.error.code == "COMMAND_REJECTED"

    accepted = runtime(tmp_path, mode=AgentMode.ACT).run_shell(
        "c2", [{"command": python_command("print('hello world')"), "purpose": "other"}]
    )
    assert accepted.ok
    assert accepted.data["commands"][0]["exit_code"] == 0
    assert accepted.data["commands"][0]["stdout"].strip() == "hello world"


def test_batch_shell_stops_after_failure_and_preserves_each_step_output(tmp_path: Path) -> None:
    (tmp_path / "first.py").write_text("print('first')\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("import sys\nprint('bad')\nsys.exit(4)\n", encoding="utf-8")
    (tmp_path / "last.py").write_text("print('must-not-run')\n", encoding="utf-8")
    result = runtime(tmp_path, mode=AgentMode.ACT).run_shell(
        "batch",
        [
            {"command": python_script("first.py"), "purpose": "other"},
            {"command": python_script("bad.py"), "purpose": "other"},
            {"command": python_script("last.py"), "purpose": "other"},
        ],
        stop_on_failure=True,
    )

    assert not result.ok
    assert result.error and result.error.code == "COMMAND_FAILED"
    assert [step["status"] for step in result.data["commands"]] == ["completed", "completed", "skipped"]
    assert result.data["commands"][1]["exit_code"] == 4
    assert result.data["failed_index"] == 1


def test_auto_act_allows_ordinary_batch_steps_without_human_approval(tmp_path: Path) -> None:
    approvals: list[ApprovalPrompt] = []
    tools = runtime(tmp_path, approve=lambda request: approvals.append(request) or True)
    assert tools.write_file("w", "changed.txt", "changed").ok
    result = tools.run_shell(
        "batch",
        [
            {"command": python_command("print('verified')"), "purpose": "test"},
            {"command": python_command("print('ordinary')"), "purpose": "other"},
        ],
    )

    assert result.ok
    assert approvals == []
    assert tools.last_verification is None


def test_tool_failure_never_raises_to_caller(tmp_path: Path) -> None:
    result = runtime(tmp_path).execute("unknown", "c", {})
    assert not result.ok
    assert result.error and result.error.code == "UNKNOWN_TOOL"


def test_failed_verification_command_returns_diagnostics(tmp_path: Path) -> None:
    tools = runtime(tmp_path)
    assert tools.write_file("w", "changed.txt", "changed").ok
    (tmp_path / "test_failure.py").write_text(
        "def test_failure():\n    assert False, 'failure detail'\n",
        encoding="utf-8",
    )
    result = tools.run_shell(
        "c", [{"command": "python -m pytest test_failure.py -q -p no:cacheprovider", "purpose": "test"}]
    )
    assert not result.ok
    assert result.error and result.error.code == "COMMAND_FAILED"
    assert result.data["commands"][0]["exit_code"] == 1
    assert "failure detail" in result.data["commands"][0]["stdout"]
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


def test_shell_does_not_inherit_minicodex_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINICODEX_API_KEY", "must-not-leak")
    result = runtime(tmp_path).run_shell(
        "c",
        [{"command": python_command("import os; print(os.getenv('MINICODEX_API_KEY', 'absent'))"), "purpose": "other"}],
    )
    assert result.ok
    assert result.data["commands"][0]["stdout"].strip() == "absent"


def test_shell_does_not_inherit_dashscope_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "must-not-leak")
    result = runtime(tmp_path, mode=AgentMode.ACT).run_shell(
        "c",
        [{"command": python_command("import os; print(os.getenv('DASHSCOPE_API_KEY', 'absent'))"), "purpose": "other"}],
    )
    assert result.ok
    assert result.data["commands"][0]["stdout"].strip() == "absent"


def test_argv_command_runs_without_shell_and_uses_workspace_temp(tmp_path: Path) -> None:
    result = runtime(tmp_path, mode=AgentMode.ACT).run_shell(
        "c",
        [{
            "argv": [
                sys.executable,
                "-c",
                "import os; from pathlib import Path; print(Path(os.environ['TMP']).resolve())",
            ],
            "purpose": "other",
        }],
    )

    assert result.ok
    step = result.data["commands"][0]
    assert step["shell"] == "argv"
    assert step["argv"][0] == sys.executable
    assert Path(step["stdout"].strip()) == (tmp_path / ".minicodex" / "tmp").resolve()


def test_shell_approval_receives_command_purpose_and_timeout(tmp_path: Path) -> None:
    approvals = []
    tools = runtime(tmp_path, approve=lambda request: approvals.append(request) or False, mode=AgentMode.ACT)
    tools.run_shell("c", [{"command": "python -V", "purpose": "lint", "timeout_sec": 17}])
    assert approvals[0].details["purpose"] == "lint"
    assert approvals[0].details["timeout_sec"] == 17
    assert approvals[0].details["command"] == "python -V"


def test_shell_normalizes_oversized_timeout_instead_of_rejecting_the_tool_call(tmp_path: Path) -> None:
    result = runtime(tmp_path, mode=AgentMode.ACT).run_shell(
        "c", [{"command": python_command("print('ok')"), "purpose": "other", "timeout_sec": 180}]
    )

    assert result.ok
    step = result.data["commands"][0]
    assert step["requested_timeout_sec"] == 180
    assert step["timeout_sec"] == 120
    assert step["timeout_normalized"] is True


def test_shell_accepts_an_explicitly_expected_nonzero_exit_code(tmp_path: Path) -> None:
    result = runtime(tmp_path, mode=AgentMode.ACT).run_shell(
        "c",
        [{
            "command": python_command("import sys; print('expected failure'); sys.exit(2)"),
            "purpose": "other",
            "expected_exit_codes": [2],
        }],
    )

    assert result.ok
    step = result.data["commands"][0]
    assert step["exit_code"] == 2
    assert step["expected_exit_codes"] == [2]
    assert step["exit_code_expected"] is True


def test_act_previews_file_diff_before_writing_and_rejection_preserves_file(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    approvals: list[ApprovalPrompt] = []
    tools = runtime(tmp_path, approve=lambda request: approvals.append(request) or False, mode=AgentMode.ACT)
    tools.read_file("r", "app.py")

    result = tools.edit_file("e", "app.py", "value = 1", "value = 2")

    assert not result.ok and result.error and result.error.code == "CHANGE_REJECTED"
    assert source.read_text(encoding="utf-8") == "value = 1\n"
    assert approvals[0].kind == "file_change"
    assert "+value = 2" in approvals[0].details["diff"]


def test_sensitive_file_is_blocked_before_reading(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DASHSCOPE_API_KEY=secret", encoding="utf-8")
    result = runtime(tmp_path).read_file("r", ".env")
    assert not result.ok and result.error and result.error.code == "PROTECTED_PATH"


def test_listing_and_search_never_expose_sensitive_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DASHSCOPE_API_KEY=needle-secret", encoding="utf-8")
    (tmp_path / ".env.example").write_text("DASHSCOPE_API_KEY=example", encoding="utf-8")
    tools = runtime(tmp_path)

    listed = tools.list_files("l")
    searched = tools.search_text("s", "needle-secret")

    assert ".env" not in listed.data["files"]
    assert ".env.example" in listed.data["files"]
    assert searched.data["matches"] == []


def test_invalid_later_batch_entry_prevents_all_commands_from_running(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    tools = runtime(tmp_path, mode=AgentMode.ACT)

    result = tools.run_shell(
        "batch",
        [
            {"command": python_command("from pathlib import Path; Path('ran.txt').write_text('ran')"), "purpose": "other"},
            {"command": "", "purpose": "other"},
        ],
    )

    assert not result.ok and result.error and result.error.code == "INVALID_ARGUMENT"
    assert not marker.exists()
