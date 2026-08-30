from pathlib import Path

from minicodex.permissions import AgentMode, PermissionAction, PermissionPolicy, PlanState


def test_plan_state_is_separate_from_persistent_execution_mode() -> None:
    assert {state.value for state in PlanState} == {
        "inactive",
        "planning",
        "waiting_approval",
    }


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


def test_auto_act_allows_ordinary_and_compound_shell_commands(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    verified = policy.decide_shell(AgentMode.AUTO_ACT, "python -m pytest -q", "test")
    compound = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "python -m pytest -q && python expense_tracker.py",
        "test",
    )
    unknown = policy.decide_shell(AgentMode.AUTO_ACT, "python scripts/local_task.py", "other")

    assert verified.action is PermissionAction.ALLOW
    assert compound.action is PermissionAction.ALLOW
    assert unknown.action is PermissionAction.ALLOW
    assert unknown.rule_id == "auto_act.local_shell"


def test_auto_act_routes_ambiguous_shell_to_reviewer_and_external_boundary_to_user(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    install = policy.decide_shell(AgentMode.AUTO_ACT, "npm install", "build")
    push = policy.decide_shell(AgentMode.AUTO_ACT, "git push origin main", "other")
    outside = policy.decide_shell(AgentMode.AUTO_ACT, "Get-Content C:\\Users\\Public\\note.txt", "other")
    url = policy.decide_shell(AgentMode.AUTO_ACT, "curl https://example.com", "other")

    assert install.action is PermissionAction.REVIEW
    assert install.rule_id == "auto_act.reviewer"
    assert push.action is PermissionAction.ASK
    assert outside.action is PermissionAction.ASK
    assert url.action is PermissionAction.REVIEW


def test_auto_act_distinguishes_safe_uncertain_and_dangerous_deletes(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    safe = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "Remove-Item -LiteralPath '.tmp-pytest' -Recurse -Force",
        "other",
    )
    uncertain = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "Remove-Item -LiteralPath 'old_module' -Recurse -Force",
        "other",
    )
    root = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "Remove-Item -LiteralPath '.' -Recurse -Force",
        "other",
    )

    assert safe.action is PermissionAction.ALLOW
    assert safe.rule_id == "auto_act.generated_cleanup"
    assert uncertain.action is PermissionAction.REVIEW
    assert root.action is PermissionAction.DENY


def test_auto_act_reviews_piped_cleanup_and_asks_for_relative_workspace_escape(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    piped_cleanup = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force",
        "other",
    )
    traversal = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "Set-Content ..\\outside.txt value",
        "other",
    )

    assert piped_cleanup.action is PermissionAction.REVIEW
    assert traversal.action is PermissionAction.ASK
    assert traversal.rule_id == "auto_act.outside_workspace"

    indirect = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "find old -type f | xargs rm -f",
        "other",
    )
    assert indirect.action is PermissionAction.REVIEW


def test_compound_shell_uses_the_highest_risk_operation(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    push_after_cleanup = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "Remove-Item -LiteralPath '.tmp-pytest' -Recurse -Force; git push origin main",
        "other",
    )
    install_after_cleanup = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "Remove-Item -LiteralPath '.tmp-pytest' -Recurse -Force; npm install",
        "build",
    )

    assert push_after_cleanup.action is PermissionAction.ASK
    assert install_after_cleanup.action is PermissionAction.REVIEW


def test_dangerous_commands_are_denied_before_mode_rules(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)

    for mode in AgentMode:
        decision = policy.decide_shell(mode, "git reset --hard", "other")
        assert decision.action is PermissionAction.DENY
        assert decision.risk == "high"

    outside = policy.decide_shell(
        AgentMode.AUTO_ACT,
        "Remove-Item -LiteralPath 'C:\\important' -Recurse -Force",
        "other",
    )
    assert outside.action is PermissionAction.DENY
    encoded = policy.decide_shell(AgentMode.AUTO_ACT, "powershell -EncodedCommand ZQBjAGgAbwA=", "other")
    assert encoded.action is PermissionAction.DENY
    encoded_short = policy.decide_shell(AgentMode.AUTO_ACT, "pwsh -ec ZQBjAGgAbwA=", "other")
    assert encoded_short.action is PermissionAction.DENY

    assert policy.decide_shell(AgentMode.AUTO_ACT, "git -C . reset --hard", "other").action is PermissionAction.DENY
    assert policy.decide_shell(AgentMode.AUTO_ACT, "git clean -d -f", "other").action is PermissionAction.DENY


def test_shell_protects_relative_credentials_and_workspace_prefix_siblings(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path)
    sibling = tmp_path.with_name(tmp_path.name + "-other") / "note.txt"

    assert policy.decide_shell(AgentMode.AUTO_ACT, "Get-Content '.env'", "other").action is PermissionAction.DENY
    assert policy.decide_shell(AgentMode.AUTO_ACT, "cat private.key", "other").action is PermissionAction.DENY
    assert policy.decide_shell(AgentMode.AUTO_ACT, f"Get-Content {sibling}", "other").action is PermissionAction.ASK
    assert policy.decide_shell(AgentMode.AUTO_ACT, "cat /etc/passwd", "other").action is PermissionAction.ASK
