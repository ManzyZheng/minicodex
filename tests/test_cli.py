from minicodex.cli import build_parser, main


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
