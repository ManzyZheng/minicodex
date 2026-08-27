from pathlib import Path

from fastapi.testclient import TestClient

from minicodex.web.app import create_app


class StubSession:
    def close(self) -> None:
        pass

    def snapshot(self) -> dict:
        return {
            "workspace": "C:/demo",
            "model": "demo",
            "status": "IDLE",
            "verification_status": "NOT_RUN",
            "max_turns_per_prompt": 20,
            "prompt_count": 0,
        }


def test_root_serves_the_web_console_and_assets() -> None:
    client = TestClient(create_app(StubSession()))

    page = client.get("/")
    stylesheet = client.get("/static/app.css")
    script = client.get("/static/app.js")

    assert page.status_code == 200
    assert 'id="timeline"' in page.text
    assert 'id="prompt-input"' in page.text
    assert 'id="approval-dialog"' in page.text
    assert stylesheet.status_code == 200
    assert "--color-ink" in stylesheet.text
    assert script.status_code == 200
    assert "EventSource" in script.text


def test_frontend_does_not_insert_untrusted_event_html() -> None:
    script_path = Path(__file__).parents[1] / "src" / "minicodex" / "web" / "static" / "app.js"
    source = script_path.read_text(encoding="utf-8")
    assert ".innerHTML" not in source
