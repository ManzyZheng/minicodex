from __future__ import annotations

import threading

import pytest

from minicodex.agent import AgentSession
from minicodex.models import ModelReply
from minicodex.permissions import AgentMode, PlanState
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


def make_web_session(tmp_path, model, *, mode: AgentMode = AgentMode.ACT) -> WebSession:
    events = EventBus()
    approvals = ApprovalGate(events, wait_timeout=0.2)
    runtime = ToolRuntime(tmp_path, approver=approvals.request, mode=mode)
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


def test_close_reports_closing_until_worker_finishes(tmp_path) -> None:
    model = BlockingModel()
    web = make_web_session(tmp_path, model)
    web.submit_prompt("work")
    assert model.started.wait(0.5)

    web.close(wait_timeout=0)
    assert web.snapshot()["status"] == "CLOSING"

    model.release.set()
    assert web.wait_until_idle(timeout=1.0)
    assert web.snapshot()["status"] == "CLOSED"


def test_web_session_changes_mode_only_while_idle(tmp_path) -> None:
    model = BlockingModel()
    web = make_web_session(tmp_path, model)
    assert web.set_mode(AgentMode.PLAN) == AgentMode.PLAN
    assert web.snapshot()["mode"] == "plan"

    web.submit_prompt("plan it")
    assert model.started.wait(0.5)
    with pytest.raises(SessionBusyError):
        web.set_mode(AgentMode.ACT)
    model.release.set()
    assert web.wait_until_idle(1.0)


def test_approving_plan_switches_mode_and_continues_same_session(tmp_path) -> None:
    model = ReplyModel([ModelReply(content="## Plan\n\nChange app.py"), ModelReply(content="implemented")])
    web = make_web_session(tmp_path, model)
    web.set_mode(AgentMode.PLAN)
    web.submit_prompt("design the feature")
    assert web.wait_until_idle(1.0)

    web.approve_plan(AgentMode.AUTO_ACT)

    assert web.wait_until_idle(1.0)
    assert web.snapshot()["mode"] == "auto-act"
    assert web.agent.prompt_count == 2
    assert any(
        message.get("role") == "user" and "approved plan" in str(message.get("content", "")).lower()
        for message in web.agent.messages
    )


def test_execute_pending_plan_uses_existing_auto_act_mode(tmp_path) -> None:
    web = make_web_session(
        tmp_path,
        ReplyModel([ModelReply(content="implemented")]),
        mode=AgentMode.AUTO_ACT,
    )
    web.agent.enter_plan_mode("enter")
    plan = web.mark_plan_ready("先修改实现，再运行测试")

    web.resolve_plan(plan.id, "execute")

    assert web.wait_until_idle(1.0)
    assert web.agent.execution_mode is AgentMode.AUTO_ACT
    assert web.agent.plan_state is PlanState.INACTIVE
    assert web.snapshot()["pending_plan"] is None


def test_plan_feedback_stays_read_only_and_reenters_agent(tmp_path) -> None:
    web = make_web_session(
        tmp_path,
        ReplyModel([ModelReply(content="revised plan")]),
        mode=AgentMode.AUTO_ACT,
    )
    web.agent.enter_plan_mode("enter")
    plan = web.mark_plan_ready("原计划")

    web.resolve_plan(plan.id, "revise", "保持旧 API 兼容")

    assert web.wait_until_idle(1.0)
    assert web.agent.execution_mode is AgentMode.AUTO_ACT
    assert web.agent.plan_state is PlanState.PLANNING
    assert web.agent.tools.mode is AgentMode.PLAN
    assert any(message.get("content") == "保持旧 API 兼容" for message in web.agent.messages)


def test_cancel_plan_returns_idle_without_starting_implementation(tmp_path) -> None:
    web = make_web_session(tmp_path, ReplyModel([]), mode=AgentMode.ACT)
    web.agent.enter_plan_mode("enter")
    plan = web.mark_plan_ready("原计划")

    web.resolve_plan(plan.id, "cancel")

    assert web.snapshot()["status"] == "IDLE"
    assert web.agent.plan_state is PlanState.INACTIVE
    assert web.agent.prompt_count == 0


def test_session_snapshot_restores_cumulative_file_changes(tmp_path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    web = make_web_session(tmp_path, ReplyModel([]), mode=AgentMode.AUTO_ACT)
    web.agent.tools.begin_prompt(1)
    web.agent.tools.read_file("read", "app.py")
    web.agent.tools.edit_file("edit", "app.py", "1", "2")

    snapshot = web.snapshot()

    assert snapshot["file_changes"][0]["path"] == "app.py"
    assert snapshot["file_changes"][0]["prompt_index"] == 1
    assert "+value = 2" in snapshot["file_changes"][0]["diff"]
