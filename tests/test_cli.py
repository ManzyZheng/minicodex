import minicodex.cli as cli
from minicodex.cli import build_parser, main
from minicodex.models import ModelReply


def test_cli_exposes_workspace_model_and_turn_limit_but_not_api_key() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "--workspace" in help_text
    assert "--model" in help_text
    assert "--max-turns" in help_text
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


def test_terminal_cli_wires_model_reasoning_to_output(tmp_path, monkeypatch, capsys) -> None:
    class ReasoningModel:
        def complete(self, _messages, _tools):
            reply = ModelReply(content="finished")
            reply.reasoning_content = "inspect before answering"
            return reply

    monkeypatch.setenv("MINICODEX_API_KEY", "secret")
    monkeypatch.setenv("MINICODEX_MODEL", "demo")
    monkeypatch.setattr(cli.OpenAIChatModel, "from_config", classmethod(lambda _cls, _config: ReasoningModel()))

    assert main(["explain", "--workspace", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "[thinking:turn 1]" in output
    assert "inspect before answering" in output
