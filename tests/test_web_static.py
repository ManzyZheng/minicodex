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
    client = TestClient(create_app(StubSession(), access_token="test-token"), base_url="http://127.0.0.1")

    page = client.get("/")
    stylesheet = client.get("/static/codex-app.css")
    markdown = client.get("/static/markdown.js")
    script = client.get("/static/codex-app.js")

    assert page.status_code == 200
    assert 'id="conversation"' in page.text
    assert 'id="review-panel"' in page.text
    assert 'id="review-diff"' in page.text
    assert 'id="prompt-input"' in page.text
    assert 'id="approval-dialog"' in page.text
    assert 'id="permission-select"' in page.text
    assert 'id="model-select"' in page.text
    assert 'id="approval-title"' in page.text
    assert stylesheet.status_code == 200
    assert "--ink" in stylesheet.text
    assert markdown.status_code == 200
    assert "renderMarkdown" in markdown.text
    assert script.status_code == 200
    assert "EventSource" in script.text


def test_frontend_does_not_insert_untrusted_event_html() -> None:
    static_dir = Path(__file__).parents[1] / "src" / "minicodex" / "web" / "static"
    for script_name in ("codex-app.js", "markdown.js"):
        source = (static_dir / script_name).read_text(encoding="utf-8")
        assert ".innerHTML" not in source


def test_project_session_and_memory_controls_are_present() -> None:
    static_dir = Path(__file__).parents[1] / "src" / "minicodex" / "web" / "static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    script = (static_dir / "codex-app.js").read_text(encoding="utf-8")

    assert 'id="project-sidebar"' in html
    assert 'id="project-list"' in html
    assert 'id="new-project"' in html
    assert 'id="global-memory"' in html
    assert 'id="memory-view"' in html
    assert 'id="memory-form"' in html
    assert "/api/projects" in script
    assert "/sessions/${encodeURIComponent(sessionId)}/activate" in script
    assert "/api/memories" in script
    assert "session_reset" in script
    assert "memory_created" in script
    assert '"project-session-add"' in script
    assert '"project-workspace"' in script
    assert '"project-section-label"' in script
    assert '"＋ 新建 Session"' in script
    assert script.index('"project-session-add"') < script.index('"project-workspace"')
