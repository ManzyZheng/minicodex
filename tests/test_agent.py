from __future__ import annotations

import json
import sys
from pathlib import Path

from minicodex.agent import Agent, AgentSession, StopReason
from minicodex.context import compact_messages, serialize_tool_result, truncate_text
from minicodex.models import ModelReply, ToolCall
from minicodex.session import SessionTrace
from minicodex.tools import ToolRuntime


class MockModel:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.messages_seen: list[list[dict]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.messages_seen.append(list(messages))
        return self.replies.pop(0)


def test_truncate_text_keeps_head_and_tail() -> None:
    text = "A" * 70 + "B" * 30
    value, truncated = truncate_text(text, limit=40)
    assert truncated
    assert value.startswith("A" * 14)
    assert value.endswith("B" * 7)
    assert "truncated" in value
    assert len(value) == 40


def test_jsonl_trace_is_replayable(tmp_path: Path) -> None:
    trace = SessionTrace(tmp_path / "session.jsonl")
    trace.write("tool_result", {"call_id": "c1", "ok": False})
    trace.write("final", {"status": "FAILED"})
    events = [json.loads(line) for line in (tmp_path / "session.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == ["tool_result", "final"]
    assert events[0]["payload"]["call_id"] == "c1"
    assert all("timestamp" in e for e in events)


def test_agent_executes_tools_feeds_errors_back_and_finishes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    model = MockModel([
        ModelReply(tool_calls=[ToolCall("1", "edit_file", {"path": "a.txt", "old_text": "old", "new_text": "new"})]),
        ModelReply(tool_calls=[ToolCall("2", "read_file", {"path": "a.txt"})]),
        ModelReply(tool_calls=[ToolCall("3", "edit_file", {"path": "a.txt", "old_text": "old", "new_text": "new"})]),
        ModelReply(content="done"),
        ModelReply(tool_calls=[ToolCall("4", "run_command", {"argv": ["python", "-c", "print('ok')"], "purpose": "test"})]),
        ModelReply(content="fixed and tested"),
    ])
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    model.replies[4] = ModelReply(tool_calls=[ToolCall("4", "run_command", {"argv": [sys.executable, "-m", "pytest", "-q"], "purpose": "test"})])
    agent = Agent(model, ToolRuntime(tmp_path, command_approver=lambda _argv, _purpose: True), max_turns=10)
    outcome = agent.run("fix a.txt")
    assert outcome.stop_reason is StopReason.COMPLETED
    assert outcome.verification_status == "VERIFIED"
    assert outcome.final_text == "fixed and tested"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new\n"
    tool_messages = [m for batch in model.messages_seen for m in batch if m.get("role") == "tool"]
    assert any("READ_REQUIRED" in m["content"] for m in tool_messages)
    assert all(set(m) <= {"role", "tool_call_id", "content"} for m in tool_messages)
    assert any("run a test, build, or lint command" in (m.get("content") or "") for m in model.messages_seen[4])


def test_agent_stops_three_identical_tool_calls_without_progress(tmp_path: Path) -> None:
    call = ToolCall("same", "read_file", {"path": "missing.txt"})
    model = MockModel([ModelReply(tool_calls=[call]), ModelReply(tool_calls=[call]), ModelReply(tool_calls=[call])])
    outcome = Agent(model, ToolRuntime(tmp_path, command_approver=lambda _argv, _purpose: True), max_turns=10).run("loop")
    assert outcome.stop_reason is StopReason.REPEATED_CALL


def test_agent_stops_at_max_turns(tmp_path: Path) -> None:
    replies = [ModelReply(tool_calls=[ToolCall(str(i), "list_files", {})]) for i in range(4)]
    outcome = Agent(MockModel(replies), ToolRuntime(tmp_path, command_approver=lambda _argv, _purpose: True), max_turns=2).run("work")
    assert outcome.stop_reason is StopReason.MAX_TURNS
    assert outcome.turns == 2


def test_agent_reports_tool_results_to_ui_callback(tmp_path: Path) -> None:
    events = []
    model = MockModel([
        ModelReply(tool_calls=[ToolCall("1", "write_file", {"path": "new.txt", "content": "hello\n"})]),
        ModelReply(content="done"),
        ModelReply(content="unverified but explained"),
    ])
    agent = Agent(
        model,
        ToolRuntime(tmp_path, command_approver=lambda _argv, _purpose: True),
        max_turns=3,
        on_tool_result=events.append,
    )
    agent.run("write a file")
    assert events[0].tool == "write_file"
    assert "+hello" in events[0].data["diff"]


def test_large_tool_result_stays_valid_json_and_reports_truncation() -> None:
    from minicodex.models import ToolResult

    result = ToolResult.success(tool="read_file", call_id="large", summary="large file", data={"path": "a.txt", "content": "A" * 3000 + "TAIL"})
    content = serialize_tool_result(result, limit=800)
    payload = json.loads(content)
    assert len(content) <= 800
    assert payload["call_id"] == "large"
    assert payload["meta"]["truncated"] is True
    assert "TAIL" in json.dumps(payload["data"])


def test_context_compaction_preserves_specific_prior_state() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "fix billing.py"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"billing.py"}'}}]},
        {"role": "tool", "tool_call_id": "1", "content": '{"ok":false,"error":{"code":"READ_ERROR"}}'},
        {"role": "assistant", "content": "retrying"},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": "continue"},
    ]
    compacted = compact_messages(messages, max_chars=1)
    summary = compacted[1]["content"]
    assert "billing.py" in summary
    assert "read_file" in summary
    assert "READ_ERROR" in summary
    assert compacted[2].get("role") != "tool"


def test_agent_session_keeps_messages_between_prompts(tmp_path: Path) -> None:
    model = MockModel([ModelReply(content="first done"), ModelReply(content="second done")])
    session = AgentSession(
        model,
        ToolRuntime(tmp_path, command_approver=lambda _argv, _purpose: True),
        max_turns_per_prompt=20,
    )

    first = session.run_turn("first task")
    second = session.run_turn("second task")

    assert first.final_text == "first done"
    assert second.final_text == "second done"
    second_request = model.messages_seen[1]
    assert any(message.get("content") == "first task" for message in second_request)
    assert any(message.get("content") == "first done" for message in second_request)
    assert second_request[-1] == {"role": "user", "content": "second task"}


def test_agent_session_resets_model_turn_limit_for_each_prompt(tmp_path: Path) -> None:
    model = MockModel([ModelReply(content="one"), ModelReply(content="two")])
    session = AgentSession(
        model,
        ToolRuntime(tmp_path, command_approver=lambda _argv, _purpose: True),
        max_turns_per_prompt=1,
    )

    assert session.run_turn("first").stop_reason is StopReason.COMPLETED
    assert session.run_turn("second").stop_reason is StopReason.COMPLETED
