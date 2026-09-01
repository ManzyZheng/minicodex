from __future__ import annotations

import json
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
        ModelReply(tool_calls=[ToolCall("4", "run_shell", {"commands": [{"command": "python -c \"print('ok')\"", "purpose": "test"}]})]),
        ModelReply(content="fixed and tested"),
    ])
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    model.replies[4] = ModelReply(tool_calls=[ToolCall("4", "run_shell", {"commands": [{"command": "python -m pytest -q", "purpose": "test"}]})])
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


def test_verified_task_receives_closeout_instruction_before_more_tool_work(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    class CloseoutAwareModel:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages: list[dict], _tools: list[dict]) -> ModelReply:
            self.calls += 1
            if self.calls == 1:
                return ModelReply(tool_calls=[ToolCall("w", "write_file", {"path": "feature.txt", "content": "done\n"})])
            if self.calls == 2:
                return ModelReply(tool_calls=[ToolCall("t", "run_shell", {"commands": [{"command": "python -m pytest -q -p no:cacheprovider", "purpose": "test"}]})])
            runtime = str(messages[2].get("content") or "")
            if "final answer now" in runtime:
                return ModelReply(content="已验证并完成。")
            return ModelReply(tool_calls=[ToolCall("extra", "run_shell", {"commands": [{"command": "Write-Output unnecessary", "purpose": "other"}]})])

    model = CloseoutAwareModel()
    outcome = Agent(
        model,
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
        max_turns=8,
    ).run("add a small feature and test it")

    assert outcome.stop_reason is StopReason.COMPLETED
    assert outcome.turns == 3
    assert outcome.final_text == "已验证并完成。"


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
    assert events[0].data["path"] == "new.txt"
    assert "diff" not in events[0].data


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


def test_context_compaction_does_not_copy_reference_bodies_into_summary() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": 'task\n\n<referenced_files trust="untrusted-data">\nSECRET_REFERENCE\n</referenced_files>'},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "three"},
        {"role": "user", "content": "four"},
        {"role": "assistant", "content": "five"},
        {"role": "user", "content": "six"},
    ]

    compacted = compact_messages(messages, max_chars=1)

    assert "task" in compacted[1]["content"]
    assert "SECRET_REFERENCE" not in compacted[1]["content"]


def test_repeated_context_compaction_replaces_the_previous_summary_instead_of_stacking_it() -> None:
    messages = [
        {"role": "system", "content": "static"},
        {"role": "system", "content": "session"},
        {"role": "system", "content": "runtime"},
        {"role": "system", "content": "Earlier context summary:\nuser: original task"},
        {"role": "user", "content": "older request"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "newer request"},
        {"role": "assistant", "content": "newer answer"},
        {"role": "user", "content": "current request"},
    ]

    for _ in range(4):
        messages = compact_messages(messages, max_chars=1)

    summaries = [
        message
        for message in messages
        if message.get("role") == "system"
        and str(message.get("content") or "").startswith("Earlier context summary:")
    ]
    assert len(summaries) == 1
    assert "original task" in summaries[0]["content"]


def test_context_compaction_event_belongs_to_the_new_prompt_and_model_turn(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    session = AgentSession(
        MockModel([ModelReply(content="done")]),
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
        on_event=lambda event_type, payload: events.append((event_type, payload)),
    )
    session.messages.extend(
        {"role": "user" if index % 2 == 0 else "assistant", "content": str(index) * 70_000}
        for index in range(6)
    )

    session.run_turn("next prompt")

    event_types = [event_type for event_type, _payload in events]
    compacted = next(payload for event_type, payload in events if event_type == "context_compacted")
    assert event_types.index("user_prompt") < event_types.index("context_compacted")
    assert compacted["turn"] == 1


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


def test_agent_injects_layered_prompt_and_session_reference_without_tracing_content(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    external = tmp_path / "api.md"
    external.write_text("PRIVATE_REFERENCE_BODY", encoding="utf-8")
    trace_path = workspace / "session.jsonl"
    events: list[tuple[str, dict]] = []
    model = MockModel([ModelReply(content="done")])
    session = AgentSession(
        model,
        ToolRuntime(workspace, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
        trace=SessionTrace(trace_path),
        on_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    outcome = session.run_turn(f"参考 @{{{external}}} 修改项目")

    assert outcome.stop_reason is StopReason.COMPLETED
    request = model.messages_seen[0]
    assert request[0]["role"] == "system" and "untrusted" in request[0]["content"]
    assert request[1]["role"] == "system" and str(workspace.resolve()) in request[1]["content"]
    assert request[2]["role"] == "system" and "effective_mode: auto-act" in request[2]["content"]
    assert "PRIVATE_REFERENCE_BODY" in request[-1]["content"]
    user_event = next(payload for event, payload in events if event == "user_prompt")
    assert user_event["text"] == f"参考 @{{{external}}} 修改项目"
    loaded_event = next(payload for event, payload in events if event == "context_loaded")
    assert "content" not in loaded_event
    assert "PRIVATE_REFERENCE_BODY" not in trace_path.read_text(encoding="utf-8")


def test_agent_keeps_active_reference_for_later_turns_and_can_remove_it(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    external = tmp_path / "api.md"
    external.write_text("SESSION_REFERENCE", encoding="utf-8")
    model = MockModel([ModelReply(content="first"), ModelReply(content="second"), ModelReply(content="third")])
    session = AgentSession(model, ToolRuntime(workspace, approver=lambda _request: True, mode=AgentMode.AUTO_ACT))

    session.run_turn(f"读取 @{{{external}}}")
    session.run_turn("继续修改")

    assert sum(
        "SESSION_REFERENCE" in str(message.get("content") or "") for message in model.messages_seen[1]
    ) == 1
    metadata = session.reference_metadata()
    assert len(metadata) == 1 and "content" not in metadata[0]
    assert session.remove_reference(metadata[0]["id"]) is True
    session.run_turn("再次继续")
    assert all("SESSION_REFERENCE" not in str(message.get("content") or "") for message in model.messages_seen[2])


def test_invalid_reference_stops_before_calling_model(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    model = MockModel([])
    events: list[tuple[str, dict]] = []
    session = AgentSession(
        model,
        ToolRuntime(workspace, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
        on_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    outcome = session.run_turn(f"参考 @{{{tmp_path / 'missing.md'}}}")

    assert outcome.stop_reason is StopReason.CONTEXT_ERROR
    assert model.messages_seen == []
    assert session.prompt_count == 1
    assert [event for event, _payload in events].index("user_prompt") < [event for event, _payload in events].index("context_error")
    error = next(payload for event, payload in events if event == "context_error")
    assert error["code"] == "REFERENCE_NOT_FOUND"


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


def test_agent_emits_cumulative_file_change_for_each_successful_edit(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("value = 1\n", encoding="utf-8")
    model = MockModel([
        ModelReply(tool_calls=[ToolCall("read", "read_file", {"path": "value.txt"})]),
        ModelReply(tool_calls=[ToolCall("one", "edit_file", {"path": "value.txt", "old_text": "1", "new_text": "2"})]),
        ModelReply(tool_calls=[ToolCall("two", "edit_file", {"path": "value.txt", "old_text": "2", "new_text": "3"})]),
        ModelReply(content="done"),
        ModelReply(content="not verified"),
    ])
    events: list[tuple[str, dict]] = []
    session = AgentSession(
        model,
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
        on_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    session.run_turn("修改 value")

    changes = [payload for event_type, payload in events if event_type == "file_changed"]
    assert len(changes) == 2
    assert changes[-1]["prompt_index"] == 1
    assert "-value = 1" in changes[-1]["diff"]
    assert "+value = 3" in changes[-1]["diff"]


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


def test_agent_traces_reasoning_but_does_not_replay_it_in_later_model_requests(tmp_path: Path) -> None:
    first = ModelReply(tool_calls=[ToolCall("list", "list_files", {})])
    first.reasoning_content = "先确认工作区内容"
    model = MockModel([first, ModelReply(content="done")])
    session = AgentSession(
        model,
        ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT),
    )

    session.run_turn("检查项目")

    assistant = next(
        message
        for message in model.messages_seen[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert "reasoning_content" not in assistant


def test_plan_mode_exposes_only_read_tools_and_injects_read_only_prompt(tmp_path: Path) -> None:
    model = MockModel([ModelReply(content="## Plan\n\nRead the code, then implement later.")])
    runtime = ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.PLAN)
    session = AgentSession(model, runtime)

    session.run_turn("design the change")

    names = {schema["function"]["name"] for schema in model.tools_seen[0]}
    assert names == {"list_files", "search_text", "read_file", "exit_plan_mode"}
    system_text = "\n".join(message["content"] for message in model.messages_seen[0] if message["role"] == "system")
    assert "PLAN MODE" in system_text
    assert "read-only" in system_text


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


def test_plan_final_text_without_exit_tool_waits_for_user_approval(tmp_path: Path) -> None:
    model = MockModel([
        ModelReply(tool_calls=[ToolCall("plan", "enter_plan_mode", {})]),
        ModelReply(content="我先只读检查相关代码。"),
    ])
    runtime = ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT)
    session = AgentSession(
        model,
        runtime,
    )

    outcome = session.run_turn("先分析这个改动")

    assert outcome.stop_reason is StopReason.COMPLETED
    assert outcome.final_text == "我先只读检查相关代码。"
    assert session.execution_mode is AgentMode.AUTO_ACT
    assert session.plan_state is PlanState.WAITING_APPROVAL
    assert session.pending_plan_text == "我先只读检查相关代码。"
    assert runtime.mode is AgentMode.PLAN
    tool_message = next(message for message in session.messages if message.get("tool_call_id") == "plan")
    assert "UNKNOWN_TOOL" not in tool_message["content"]


def test_agent_emits_each_batch_command_output_separately(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    model = MockModel([
        ModelReply(tool_calls=[ToolCall("batch", "run_shell", {"commands": [
            {"command": "Write-Output one", "purpose": "other"},
            {"command": "Write-Output two", "purpose": "other"},
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
