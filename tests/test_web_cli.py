from minicodex.web_cli import build_parser, local_console_url, serve


def test_web_cli_exposes_only_local_port_configuration() -> None:
    args = build_parser().parse_args(["--workspace", "demo", "--port", "8123"])
    assert args.workspace == "demo"
    assert args.port == 8123
    assert not hasattr(args, "host")


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
