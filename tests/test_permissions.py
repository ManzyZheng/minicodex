from pathlib import Path

from minicodex.permissions import AgentMode, PermissionAction, PermissionPolicy


def test_plan_denies_mutations_while_act_asks_and_auto_act_allows(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    assert policy.decide_tool(AgentMode.PLAN, "edit_file", {"path": "app.py"}).action is PermissionAction.DENY
    assert policy.decide_tool(AgentMode.ACT, "edit_file", {"path": "app.py"}).action is PermissionAction.ASK
    decision = policy.decide_tool(AgentMode.AUTO_ACT, "edit_file", {"path": "app.py"})
    assert decision.action is PermissionAction.ALLOW
    assert decision.rule_id == "auto_act.workspace_edit"


def test_sensitive_paths_are_denied_in_every_mode_but_env_example_is_readable(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    for mode in AgentMode:
        assert policy.decide_tool(mode, "read_file", {"path": ".env"}).action is PermissionAction.DENY
        assert policy.decide_tool(mode, "read_file", {"path": "config/.env.development"}).action is PermissionAction.DENY
        assert policy.decide_tool(mode, "write_file", {"path": ".git/config"}).action is PermissionAction.DENY
    assert policy.decide_tool(AgentMode.AUTO_ACT, "read_file", {"path": ".env.example"}).action is PermissionAction.ALLOW


def test_auto_act_allows_verification_and_read_only_git_but_asks_for_unknown_commands(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    verified = policy.decide_command(AgentMode.AUTO_ACT, ["python", "-m", "pytest", "-q"], "test")
    readonly = policy.decide_command(AgentMode.AUTO_ACT, ["git", "diff", "--stat"], "other")
    unknown = policy.decide_command(AgentMode.AUTO_ACT, ["python", "expense_tracker.py"], "other")

    assert verified.action is PermissionAction.ALLOW
    assert verified.rule_id == "auto_act.verification"
    assert readonly.action is PermissionAction.ALLOW
    assert unknown.action is PermissionAction.ASK


def test_dangerous_commands_are_denied_before_mode_rules(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    for mode in AgentMode:
        decision = policy.decide_command(mode, ["git", "reset", "--hard"], "other")
        assert decision.action is PermissionAction.DENY
        assert decision.risk == "high"

    powershell = policy.decide_command(
        AgentMode.AUTO_ACT,
        ["powershell", "-Command", "Remove-Item -Recurse -Force C:\\important"],
        "other",
    )
    assert powershell.action is PermissionAction.DENY
    cmd = policy.decide_command(
        AgentMode.AUTO_ACT,
        ["cmd", "/c", "rmdir /s /q C:\\important"],
        "other",
    )
    assert cmd.action is PermissionAction.DENY
