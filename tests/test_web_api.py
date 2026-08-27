from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from minicodex.agent import AgentSession
from minicodex.models import ModelReply
from minicodex.tools import ToolRuntime
from minicodex.web.app import create_app, format_sse_event
from minicodex.web.approval import ApprovalGate
from minicodex.web.events import EventBus, WebEvent
from minicodex.web.session import WebSession


class BlockingModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, tools) -> ModelReply:
        self.started.set()
        assert self.release.wait(1.0)
        return ModelReply(content="done")


def make_client(tmp_path):
    events = EventBus()
    approvals = ApprovalGate(events, wait_timeout=0.2)
    model = BlockingModel()
    runtime = ToolRuntime(tmp_path, command_approver=approvals.request)
    agent = AgentSession(model, runtime, on_event=events.publish)
    session = WebSession(agent, events, approvals, workspace=tmp_path, model_name="demo", max_turns_per_prompt=20)
    return TestClient(create_app(session, access_token="test-token"), base_url="http://127.0.0.1"), session, model


def api(client: TestClient, path: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}token=test-token"


def test_session_and_prompt_endpoints_report_state_and_busy(tmp_path) -> None:
    client, session, model = make_client(tmp_path)
    snapshot = client.get(api(client, "/api/session"))
    assert snapshot.status_code == 200
    assert snapshot.json()["max_turns_per_prompt"] == 20

    accepted = client.post(api(client, "/api/prompts"), json={"text": "first"})
    assert accepted.status_code == 202
    assert model.started.wait(0.5)
    assert client.post(api(client, "/api/prompts"), json={"text": "second"}).status_code == 409

    model.release.set()
    assert session.wait_until_idle(1.0)


def test_prompt_endpoint_rejects_blank_text(tmp_path) -> None:
    client, _session, _model = make_client(tmp_path)
    response = client.post(api(client, "/api/prompts"), json={"text": "   "})
    assert response.status_code == 422


def test_api_requires_session_token_and_rejects_cross_site_origin(tmp_path) -> None:
    client, _session, _model = make_client(tmp_path)
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/session?token=wrong").status_code == 401
    assert client.get(api(client, "/api/session"), headers={"Origin": "https://evil.example"}).status_code == 403


def test_app_rejects_non_loopback_host(tmp_path) -> None:
    client, _session, _model = make_client(tmp_path)
    response = client.get(api(client, "/api/session"), headers={"Host": "attacker.example"})
    assert response.status_code == 400


def test_sse_formatter_uses_id_event_and_compact_json() -> None:
    event = WebEvent(7, "status", "2026-08-27T00:00:00Z", {"value": "IDLE"})
    assert format_sse_event(event) == 'id: 7\nevent: status\ndata: {"value":"IDLE"}\n\n'
