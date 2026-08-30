import minicodex.web_cli as web_cli
from minicodex.web.events import EventBus
from minicodex.web_cli import build_parser, local_console_url, serve


def test_web_cli_exposes_only_local_port_configuration() -> None:
    args = build_parser().parse_args(["--workspace", "demo", "--port", "8123"])
    assert args.workspace == "demo"
    assert args.port == 8123
    assert not hasattr(args, "host")
    assert args.mode == "act"
    assert args.max_turns == 50


def test_serve_always_binds_to_loopback(monkeypatch) -> None:
    captured = {}

    def fake_run(app, **kwargs):
        captured.update({"app": app, **kwargs})

    monkeypatch.setattr("minicodex.web_cli.uvicorn.run", fake_run)
    app = object()
    serve(app, 8123)

    assert captured == {"app": app, "host": "127.0.0.1", "port": 8123, "log_level": "info"}


def test_local_console_url_contains_the_session_token() -> None:
    assert local_console_url(8123, "secret-token") == "http://127.0.0.1:8123/?token=secret-token"


def test_web_cli_keeps_reasoning_in_terminal_but_hides_it_from_event_bus(capsys) -> None:
    events = EventBus()
    web_cli.publish_agent_event(events, "model_reasoning", {"turn": 3, "content": "check the diff"})
    assert events.after(0) == []
    assert "[thinking:turn 3]" in capsys.readouterr().out


def test_web_cli_projects_short_progress_and_deterministic_tool_summary() -> None:
    events = EventBus()

    web_cli.publish_agent_event(events, "model_message", {"turn": 2, "content": "正在检查失败测试。"})
    web_cli.publish_agent_event(events, "tool_result", {
        "turn": 2,
        "ok": True,
        "tool": "read_file",
        "call_id": "read",
        "summary": "read 173 characters",
        "data": {"path": "app.py", "content": "..."},
        "error": None,
        "meta": {"duration_ms": 0, "truncated": False, "artifact_path": None},
    })

    retained = [(event.type, event.payload) for event in events.after(0)]
    assert retained[0] == ("progress", {"text": "正在检查失败测试。", "turn": 2})
    assert retained[1][0] == "tool_summary"
    assert retained[1][1]["text"] == "已读取 app.py"
    assert retained[1][1]["turn"] == 2
    assert retained[1][1]["detail"]["call_id"] == "read"


def test_web_cli_projects_completion_as_final_answer_and_turn_boundary() -> None:
    events = EventBus()
    payload = {"text": "已修复。", "turns": 4, "verification_status": "VERIFIED"}

    web_cli.publish_agent_event(events, "turn_completed", payload)

    retained = [(event.type, event.payload) for event in events.after(0)]
    assert retained == [("final_answer", payload), ("turn_completed", payload)]
