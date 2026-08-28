from __future__ import annotations

import json
import sys
from pathlib import Path

from minicodex.agent import Agent, AgentSession, StopReason
from minicodex.context import compact_messages, serialize_tool_result, truncate_text
from minicodex.models import ModelReply, ToolCall
from minicodex.permissions import AgentMode, PlanState
from minicodex.session import SessionTrace
from minicodex.tools import ToolRuntime


class MockModel:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.messages_seen: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.messages_seen.append(list(messages))
        self.tools_seen.append(list(tools))
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
        ModelReply(tool_calls=[ToolCall("4", "run_command", {"commands": [{"argv": ["python", "-c", "print('ok')"], "purpose": "test"}]})]),
        ModelReply(content="fixed and tested"),
    ])
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    model.replies[4] = ModelReply(tool_calls=[ToolCall("4", "run_command", {"commands": [{"argv": [sys.executable, "-m", "pytest", "-q"], "purpose": "test"}]})])
    agent = Agent(model, ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT), max_turns=10)
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
    outcome = Agent(model, ToolRuntime(tmp_path, approver=lambda _request: True), max_turns=10).run("loop")
    assert outcome.stop_reason is StopReason.REPEATED_CALL


def test_agent_stops_at_max_turns(tmp_path: Path) -> None:
    replies = [ModelReply(tool_calls=[ToolCall(str(i), "list_files", {})]) for i in range(4)]
    outcome = Agent(MockModel(replies), ToolRuntime(tmp_path, approver=lambda _request: True), max_turns=2).run("work")
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
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
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
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
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
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
        max_turns_per_prompt=1,
    )

    assert session.run_turn("first").stop_reason is StopReason.COMPLETED
    assert session.run_turn("second").stop_reason is StopReason.COMPLETED


def test_agent_session_emits_tool_diff_and_completion_events(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("old\n", encoding="utf-8")
    model = MockModel([
        ModelReply(tool_calls=[ToolCall("read", "read_file", {"path": "value.txt"})]),
        ModelReply(tool_calls=[ToolCall("edit", "edit_file", {"path": "value.txt", "old_text": "old", "new_text": "new"})]),
        ModelReply(content="done"),
        ModelReply(content="changed but not verified"),
    ])
    events: list[tuple[str, dict]] = []
    session = AgentSession(
        model,
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
        on_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    session.run_turn("change value")

    event_types = [event_type for event_type, _payload in events]
    assert event_types[0] == "user_prompt"
    assert event_types.index("tool_call") < event_types.index("tool_result")
    assert "diff" in event_types
    assert events[-1][0] == "turn_completed"
    diff_payload = next(payload for event_type, payload in events if event_type == "diff")
    assert diff_payload["path"] == "value.txt"
    assert "+new" in diff_payload["diff"]


def test_agent_session_repairs_unanswered_tool_calls_before_next_prompt(tmp_path: Path) -> None:
    repeated_calls = [ToolCall(str(index), "read_file", {"path": "missing.txt"}) for index in range(4)]
    model = MockModel([ModelReply(tool_calls=repeated_calls), ModelReply(content="continued safely")])
    session = AgentSession(
        model,
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
    )

    assert session.run_turn("trigger repeated failures").stop_reason is StopReason.REPEATED_CALL
    assert session.run_turn("continue").stop_reason is StopReason.COMPLETED

    second_request = model.messages_seen[-1]
    answered = {message["tool_call_id"] for message in second_request if message.get("role") == "tool"}
    assert answered == {"0", "1", "2", "3"}
    cancelled = next(message for message in second_request if message.get("tool_call_id") == "3")
    assert "TOOL_CALL_CANCELLED" in cancelled["content"]


def test_final_reply_emits_once_and_keeps_final_turn_number(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    session = AgentSession(
        MockModel([ModelReply(content="## Finished\n\nAll tests passed.")]),
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
        on_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    session.run_turn("finish")

    assert [event_type for event_type, _payload in events].count("model_message") == 0
    completed = next(payload for event_type, payload in events if event_type == "turn_completed")
    assert completed["text"] == "## Finished\n\nAll tests passed."
    assert completed["turns"] == 1


def test_agent_emits_and_traces_reasoning_without_mixing_it_into_final_text(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    trace_path = tmp_path / "reasoning.jsonl"
    reply = ModelReply(content="final answer")
    reply.reasoning_content = "inspect the implementation first"
    session = AgentSession(
        MockModel([reply]),
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
        trace=SessionTrace(trace_path),
        on_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    outcome = session.run_turn("explain the code")

    reasoning_index = next(index for index, event in enumerate(events) if event[0] == "model_reasoning")
    completed_index = next(index for index, event in enumerate(events) if event[0] == "turn_completed")
    assert reasoning_index < completed_index
    assert events[reasoning_index][1] == {"content": "inspect the implementation first", "turn": 1}
    assert outcome.final_text == "final answer"
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    model_record = next(record for record in records if record["event"] == "model_reply")
    assert model_record["payload"]["reasoning_content"] == "inspect the implementation first"


def test_plan_mode_exposes_only_read_tools_and_injects_read_only_prompt(tmp_path: Path) -> None:
    model = MockModel([ModelReply(content="## Plan\n\nRead the code, then implement later.")])
    runtime = ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.PLAN)
    session = AgentSession(model, runtime)

    session.run_turn("design the change")

    names = {schema["function"]["name"] for schema in model.tools_seen[0]}
    assert names == {"list_files", "search_text", "read_file", "exit_plan_mode"}
    assert "PLAN MODE" in model.messages_seen[0][0]["content"]
    assert "read-only" in model.messages_seen[0][0]["content"]


def test_plan_overlay_keeps_selected_auto_act_mode(tmp_path: Path) -> None:
    runtime = ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT)
    session = AgentSession(MockModel([]), runtime)

    result = session.enter_plan_mode("enter")

    assert result.ok
    assert session.execution_mode is AgentMode.AUTO_ACT
    assert session.plan_state is PlanState.PLANNING
    assert runtime.mode is AgentMode.PLAN


def test_plan_tools_allow_read_and_exit_control_only(tmp_path: Path) -> None:
    session = AgentSession(
        MockModel([]),
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.ACT),
    )
    session.enter_plan_mode("enter")

    schemas = {schema["function"]["name"]: schema for schema in session._tool_schemas()}

    assert set(schemas) == {"list_files", "search_text", "read_file", "exit_plan_mode"}
    exit_parameters = schemas["exit_plan_mode"]["function"]["parameters"]
    assert exit_parameters["required"] == ["plan"]


def test_model_can_enter_plan_without_calling_workspace_runtime(tmp_path: Path) -> None:
    model = MockModel([
        ModelReply(tool_calls=[ToolCall("plan", "enter_plan_mode", {})]),
        ModelReply(content="我先只读检查相关代码。"),
    ])
    session = AgentSession(
        model,
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
    )

    outcome = session.run_turn("先分析这个改动")

    assert outcome.stop_reason is StopReason.COMPLETED
    assert session.execution_mode is AgentMode.AUTO_ACT
    assert session.plan_state is PlanState.PLANNING
    tool_message = next(message for message in session.messages if message.get("tool_call_id") == "plan")
    assert "UNKNOWN_TOOL" not in tool_message["content"]


def test_agent_emits_each_batch_command_output_separately(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    model = MockModel([
        ModelReply(tool_calls=[ToolCall("batch", "run_command", {"commands": [
            {"argv": [sys.executable, "-c", "print('one')"], "purpose": "other"},
            {"argv": [sys.executable, "-c", "print('two')"], "purpose": "other"},
        ]})]),
        ModelReply(content="done"),
    ])
    session = AgentSession(
        model,
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.ACT),
        on_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    session.run_turn("run both")

    outputs = [payload for event_type, payload in events if event_type == "command_output"]
    assert [item["index"] for item in outputs] == [0, 1]
    assert outputs[0]["stdout"].strip() == "one"
    assert outputs[1]["stdout"].strip() == "two"
