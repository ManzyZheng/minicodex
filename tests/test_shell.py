from __future__ import annotations

import os
import sys
from pathlib import Path

from minicodex.permissions import AgentMode, ApprovalPrompt
from minicodex.reviewer import ReviewDecision, ReviewOutcome
from minicodex.tools import ToolRuntime


def python_command(code: str) -> str:
    if os.name == "nt":
        return f'python -c "{code}"'
    import shlex

    return shlex.join([sys.executable, "-c", code])


def test_run_shell_executes_compound_command_and_captures_output(tmp_path: Path) -> None:
    command = "Write-Output one && Write-Output two" if os.name == "nt" else "printf 'one\\n' && printf 'two\\n'"
    runtime = ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.ACT)

    result = runtime.run_shell("shell", [{"command": command, "purpose": "other"}])

    assert result.ok
    assert result.tool == "run_shell"
    assert result.data["commands"][0]["command"] == command
    assert result.data["commands"][0]["shell"] in {"powershell", "sh"}
    assert result.data["commands"][0]["analysis"]["operations"] == ["process.execute", "process.execute"]
    assert result.data["commands"][0]["stdout"].split() == ["one", "two"]


def test_auto_act_uses_reviewer_for_gray_command_without_human_prompt(tmp_path: Path) -> None:
    human_prompts: list[ApprovalPrompt] = []
    reviewer_prompts: list[ApprovalPrompt] = []

    def reviewer(request: ApprovalPrompt) -> ReviewOutcome:
        reviewer_prompts.append(request)
        return ReviewOutcome(ReviewDecision.ALLOW, "普通项目依赖操作", "medium")

    runtime = ToolRuntime(
        tmp_path,
        approver=lambda request: human_prompts.append(request) or False,
        reviewer=reviewer,
        mode=AgentMode.AUTO_ACT,
    )
    result = runtime.run_shell("shell", [{"command": "npm install --help", "purpose": "build"}])

    assert result.ok
    assert len(reviewer_prompts) == 1
    assert human_prompts == []
    assert reviewer_prompts[0].details["analysis"]["operations"] == ["package.install"]
    assert result.data["commands"][0]["review"]["decision"] == "allow"


def test_reviewer_escalation_falls_back_to_human_approval(tmp_path: Path) -> None:
    human_prompts: list[ApprovalPrompt] = []
    runtime = ToolRuntime(
        tmp_path,
        approver=lambda request: human_prompts.append(request) or False,
        reviewer=lambda _request: ReviewOutcome(ReviewDecision.ESCALATE, "需要用户确认", "medium"),
        mode=AgentMode.AUTO_ACT,
    )

    result = runtime.run_shell("shell", [{"command": "npm install", "purpose": "build"}])

    assert not result.ok and result.error and result.error.code == "COMMAND_REJECTED"
    assert len(human_prompts) == 1
    assert human_prompts[0].details["review"]["decision"] == "escalate"


def test_hard_denial_never_calls_reviewer_or_human(tmp_path: Path) -> None:
    calls: list[str] = []
    runtime = ToolRuntime(
        tmp_path,
        approver=lambda _request: calls.append("human") or True,
        reviewer=lambda _request: calls.append("reviewer") or ReviewOutcome(ReviewDecision.ALLOW, "allow", "low"),
        mode=AgentMode.AUTO_ACT,
    )

    result = runtime.run_shell("shell", [{"command": "git reset --hard", "purpose": "other"}])

    assert not result.ok and result.error and result.error.code == "COMMAND_DENIED"
    assert calls == []


def test_shell_process_does_not_inherit_api_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "must-not-leak")
    command = python_command("import os; print(os.getenv('DASHSCOPE_API_KEY', 'absent'))")
    runtime = ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.ACT)

    result = runtime.run_shell("shell", [{"command": command, "purpose": "other"}])

    assert result.ok
    assert result.data["commands"][0]["stdout"].strip() == "absent"


def test_powershell_cmdlet_error_returns_failed_tool_result(tmp_path: Path) -> None:
    if os.name != "nt":
        return
    runtime = ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.ACT)

    result = runtime.run_shell(
        "shell",
        [{"command": "Get-Content -LiteralPath '__minicodex_missing_file__'", "purpose": "other"}],
    )

    assert not result.ok
    assert result.error and result.error.code == "COMMAND_FAILED"
    assert result.data["commands"][0]["exit_code"] != 0
