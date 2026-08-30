import minicodex.cli as cli
from minicodex.cli import build_parser, main
from minicodex.models import ModelReply
from minicodex.permissions import AgentMode, ApprovalPrompt


def test_cli_exposes_workspace_model_and_turn_limit_but_not_api_key() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "--workspace" in help_text
    assert "--model" in help_text
    assert "--max-turns" in help_text
    assert "--mode" in help_text
    assert "--api-key" not in help_text


def test_cli_ctrl_c_during_task_prompt_exits_130(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MINICODEX_API_KEY", "secret")
    monkeypatch.setenv("MINICODEX_MODEL", "demo")
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert main(["--workspace", str(tmp_path)]) == 130


def test_cli_prints_reasoning_as_a_bounded_separate_block(capsys) -> None:
    cli.print_agent_event("model_reasoning", {"turn": 2, "content": "R" * 9_000})
    output = capsys.readouterr().out
    assert "[thinking:turn 2]" in output
    assert "truncated" in output
    assert len(output) < 8_200


def test_cli_prints_compact_context_events_without_reference_content(capsys) -> None:
    cli.print_agent_event(
        "context_loaded",
        {"name": "api.md", "scope": "external", "size": 2048, "path": "D:/docs/api.md"},
    )
    cli.print_agent_event(
        "context_error",
        {"code": "REFERENCE_NOT_FOUND", "message": "reference file not found", "path": "D:/missing.md"},
    )
    cli.print_agent_event(
        "context_compacted",
        {"before_messages": 28, "after_messages": 15, "before_chars": 91_000, "after_chars": 58_000},
    )

    output = capsys.readouterr().out
    assert "[context:ok] api.md · external read-only · 2.0 KiB" in output
    assert "[context:error] REFERENCE_NOT_FOUND · reference file not found" in output
    assert "[context:compact] 28 → 15 messages" in output
    assert "content" not in output


def test_terminal_cli_wires_model_reasoning_to_output(tmp_path, monkeypatch, capsys) -> None:
    class ReasoningModel:
        def complete(self, _messages, _tools):
            reply = ModelReply(content="finished")
            reply.reasoning_content = "inspect before answering"
            return reply

    monkeypatch.setenv("MINICODEX_API_KEY", "secret")
    monkeypatch.setenv("MINICODEX_MODEL", "demo")
    monkeypatch.setattr(cli.OpenAIChatModel, "from_config", classmethod(lambda _cls, _config, **_kwargs: ReasoningModel()))

    assert main(["explain", "--workspace", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "[thinking:turn 1]" in output
    assert "inspect before answering" in output


def test_cli_mode_defaults_to_act_and_accepts_auto_act() -> None:
    assert build_parser().parse_args([]).mode == AgentMode.ACT.value
    assert build_parser().parse_args([]).max_turns == 50
    assert build_parser().parse_args(["--mode", "auto-act"]).mode == AgentMode.AUTO_ACT.value


def test_cli_file_approval_prints_diff_before_decision(monkeypatch, capsys) -> None:
    prompt = ApprovalPrompt(
        kind="file_change", tool="edit_file", summary="edit app.py", reason="ACT requires review",
        risk="medium", rule_id="act.file_change", details={"path": "app.py", "diff": "-old\n+new\n"},
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert cli.confirm_action(prompt) is False
    output = capsys.readouterr().out
    assert "app.py" in output
    assert "-old" in output and "+new" in output
