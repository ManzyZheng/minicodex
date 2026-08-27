from minicodex.web_cli import build_parser, serve


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
