from __future__ import annotations

import threading

import pytest

from minicodex.agent import AgentSession
from minicodex.models import ModelReply
from minicodex.tools import ToolRuntime
from minicodex.web.approval import ApprovalGate
from minicodex.web.events import EventBus
from minicodex.web.session import SessionBusyError, WebSession


class ReplyModel:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies

    def complete(self, messages, tools) -> ModelReply:
        return self.replies.pop(0)


class BlockingModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, tools) -> ModelReply:
        self.started.set()
        assert self.release.wait(1.0)
        return ModelReply(content="done")


def make_web_session(tmp_path, model) -> WebSession:
    events = EventBus()
    approvals = ApprovalGate(events, wait_timeout=0.2)
    runtime = ToolRuntime(tmp_path, command_approver=approvals.request)
    agent = AgentSession(model, runtime, on_event=events.publish)
    return WebSession(
        agent,
        events,
        approvals,
        workspace=tmp_path,
        model_name="demo-model",
        max_turns_per_prompt=20,
    )


def test_web_session_runs_prompt_and_returns_to_idle(tmp_path) -> None:
    web = make_web_session(tmp_path, ReplyModel([ModelReply(content="done")]))
    web.submit_prompt("hello")

    assert web.wait_until_idle(timeout=1.0)
    assert web.snapshot()["status"] == "IDLE"
    assert any(event.type == "turn_completed" for event in web.events.after(0))


def test_web_session_rejects_second_prompt_while_running(tmp_path) -> None:
    model = BlockingModel()
    web = make_web_session(tmp_path, model)
    web.submit_prompt("first")
    assert model.started.wait(0.5)

    with pytest.raises(SessionBusyError):
        web.submit_prompt("second")

    model.release.set()
    assert web.wait_until_idle(timeout=1.0)
