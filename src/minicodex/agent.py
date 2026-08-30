from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from .context import ContextManager, serialize_tool_result
from .models import ModelReply, ToolCall, ToolResult
from .permissions import AgentMode, PlanState
from .prompting import (
    SessionEnvironment,
    build_runtime_prompt,
    build_session_prompt,
    build_static_prompt,
    build_user_context,
)
from .references import ExternalReferenceError, ExternalReferenceRegistry
from .session import SessionTrace
from .tools import TOOL_SCHEMAS, ToolRuntime

ENTER_PLAN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "enter_plan_mode",
        "description": "Enter a temporary read-only planning phase before proposing implementation.",
        "parameters": {"type": "object", "properties": {}},
    },
}

EXIT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "exit_plan_mode",
        "description": "Submit a completed Markdown plan for user approval without gaining write permission.",
        "parameters": {
            "type": "object",
            "properties": {"plan": {"type": "string", "description": "The complete Markdown plan."}},
            "required": ["plan"],
        },
    },
}


class Model(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...


class StopReason(str, Enum):
    COMPLETED = "COMPLETED"
    CONTEXT_ERROR = "CONTEXT_ERROR"
    MAX_TURNS = "MAX_TURNS"
    REPEATED_CALL = "REPEATED_CALL"
    INTERRUPTED = "INTERRUPTED"
    MODEL_ERROR = "MODEL_ERROR"


@dataclass
class AgentOutcome:
    stop_reason: StopReason
    final_text: str
    turns: int
    verification_status: str
    verification: dict[str, Any] | None = None


class AgentSession:
    def __init__(
        self,
        model: Model,
        tools: ToolRuntime,
        *,
        max_turns_per_prompt: int = 50,
        trace: SessionTrace | None = None,
        on_tool_result: Callable[[ToolResult], None] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        memory_prompt_provider: Callable[[], str] | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.max_turns_per_prompt = max_turns_per_prompt
        self.trace = trace
        self.on_tool_result = on_tool_result
        self.on_event = on_event
        self.memory_prompt_provider = memory_prompt_provider or (lambda: "")
        initial_mode = self.tools.mode
        self.execution_mode = AgentMode.ACT if initial_mode is AgentMode.PLAN else initial_mode
        self.plan_state = PlanState.PLANNING if initial_mode is AgentMode.PLAN else PlanState.INACTIVE
        self.pending_plan_text: str | None = None
        self.references = ExternalReferenceRegistry(self.tools.guard.root)
        environment = SessionEnvironment.capture(self.tools.guard.root, max_turns=self.max_turns_per_prompt)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_static_prompt()},
            {"role": "system", "content": build_session_prompt(environment)},
            {"role": "system", "content": ""},
            {"role": "system", "content": ""},
        ]
        self._reference_messages: list[dict[str, Any]] = []
        self.context = ContextManager()
        self._active_turn = 0
        self._interrupt_event = threading.Event()
        self.tools.set_interrupt_checker(self._interrupt_event.is_set)
        self._apply_effective_mode(emit=False)
        self.prompt_count = 0
        self.last_memory_extracted_prompt_index = 0
        self._refresh_memory_prompt()

    @property
    def _persistent_message_offset(self) -> int:
        return 4

    def _refresh_memory_prompt(self) -> None:
        try:
            content = self.memory_prompt_provider() or ""
        except Exception as exc:
            content = f"Memory context is temporarily unavailable: {type(exc).__name__}."
        self.messages[3]["content"] = content

    def export_state(self) -> dict[str, Any]:
        return {
            "messages": [dict(message) for message in self.messages[self._persistent_message_offset :]],
            "prompt_count": self.prompt_count,
            "execution_mode": self.execution_mode.value,
            "last_memory_extracted_prompt_index": self.last_memory_extracted_prompt_index,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        raw_messages = state.get("messages", [])
        restored = [dict(message) for message in raw_messages if isinstance(message, dict) and message.get("role")]
        self.messages = self.messages[: self._persistent_message_offset] + restored
        self.prompt_count = max(0, int(state.get("prompt_count", 0)))
        self.last_memory_extracted_prompt_index = max(0, int(state.get("last_memory_extracted_prompt_index", 0)))
        try:
            mode = AgentMode(str(state.get("execution_mode", self.execution_mode.value)))
        except ValueError:
            mode = self.execution_mode
        if mode is AgentMode.PLAN:
            mode = AgentMode.ACT
        self.execution_mode = mode
        self.plan_state = PlanState.INACTIVE
        self.pending_plan_text = None
        self._reference_messages = []
        self._refresh_memory_prompt()
        self._apply_effective_mode(emit=False)

    def history_snapshot(self) -> list[dict[str, Any]]:
        return [
            {"role": message.get("role"), "content": message.get("content", "")}
            for message in self.messages[self._persistent_message_offset :]
            if message.get("role") in {"user", "assistant"} and not message.get("tool_calls")
        ]

    def set_mode(self, mode: AgentMode, *, emit: bool = True) -> None:
        if mode is AgentMode.PLAN:
            self.plan_state = PlanState.PLANNING
            self.pending_plan_text = None
        else:
            self.execution_mode = mode
            self.plan_state = PlanState.INACTIVE
            self.pending_plan_text = None
        self._apply_effective_mode(emit=emit)

    def set_execution_mode(self, mode: AgentMode, *, emit: bool = True) -> None:
        if mode is AgentMode.PLAN:
            raise ValueError("PLAN is a temporary Agent state, not an execution permission")
        self.execution_mode = mode
        self._apply_effective_mode(emit=emit)

    def _apply_effective_mode(self, *, emit: bool = True) -> None:
        previous = self.tools.mode
        effective = AgentMode.PLAN if self.plan_state is not PlanState.INACTIVE else self.execution_mode
        self.tools.set_mode(effective)
        self._refresh_runtime_prompt()
        if emit and previous is not effective:
            payload = {"from": previous.value, "to": effective.value}
            self._trace("mode_changed", payload)
            self._emit("mode_changed", payload)

    def _refresh_runtime_prompt(self) -> None:
        effective = AgentMode.PLAN if self.plan_state is not PlanState.INACTIVE else self.execution_mode
        self.messages[2]["content"] = build_runtime_prompt(
            effective_mode=effective,
            execution_mode=self.execution_mode,
            plan_state=self.plan_state,
            verification_status=self._verification_status(),
            turn_in_prompt=self._active_turn,
        )
        self._refresh_memory_prompt()

    def reference_metadata(self) -> list[dict[str, Any]]:
        return self.references.metadata()

    def _compact_history(self, *, turn: int | None = None) -> None:
        result = self.context.prepare(
            self.messages,
            checkpoint_factory=self.tools.context_checkpoint,
        )
        if result.changed:
            self.messages = result.messages
            payload = {
                "before_messages": result.before_messages,
                "after_messages": result.after_messages,
                "before_chars": result.before_chars,
                "after_chars": result.after_chars,
                "before_tokens": result.before_tokens,
                "after_tokens": result.after_tokens,
                "stages": list(result.stages),
                "reductions": result.reductions,
                "compaction_count": self.context.compaction_count,
            }
            if turn is not None:
                payload["turn"] = turn
            self._trace("context_compacted", payload)
            self._emit("context_compacted", payload)
        active_message_ids = {id(message) for message in self.messages}
        self._reference_messages = [
            entry for entry in self._reference_messages if id(entry["message"]) in active_message_ids
        ]

    def reset_interrupt(self) -> None:
        self._interrupt_event.clear()

    def request_interrupt(self) -> None:
        self._interrupt_event.set()
        self._trace("interrupt_requested", {"prompt_index": self.prompt_count})
        self._emit("interrupt_requested", {"prompt_index": self.prompt_count})

    def _interrupted(self, turns: int) -> AgentOutcome:
        self._cancel_unanswered_tool_calls("Skipped because the prompt was interrupted by the user.")
        return self._outcome(StopReason.INTERRUPTED, "已停止当前任务。", turns)

    def context_snapshot(self) -> dict[str, Any]:
        policy = self.context.policy
        return {
            "current_chars": sum(len(json.dumps(message, ensure_ascii=False, separators=(",", ":"))) for message in self.messages),
            "budget_tokens": policy.budget_tokens,
            "stale_tokens": policy.stale_tokens,
            "auto_compact_tokens": policy.auto_compact_tokens,
            "target_tokens": policy.target_tokens,
            "recent_tool_turns": policy.recent_tool_turns,
            "recent_tool_chars": policy.recent_tool_chars,
            "compaction_count": self.context.compaction_count,
        }

    def _scrub_reference_messages(self, reference_id: str) -> None:
        active_message_ids = {id(message) for message in self.messages}
        retained_entries: list[dict[str, Any]] = []
        for entry in self._reference_messages:
            message = entry["message"]
            if id(message) not in active_message_ids:
                continue
            entry["references"] = [
                reference for reference in entry["references"] if reference.id != reference_id
            ]
            message["content"] = build_user_context(entry["prompt"], entry["references"])
            retained_entries.append(entry)
        self._reference_messages = retained_entries

    def _attach_missing_references(self, entry: dict[str, Any]) -> None:
        represented = {
            reference.id
            for reference_entry in self._reference_messages
            for reference in reference_entry["references"]
        }
        missing = [reference for reference in self.references.active() if reference.id not in represented]
        if not missing:
            return
        entry["references"].extend(missing)
        entry["message"]["content"] = build_user_context(entry["prompt"], entry["references"])

    def remove_reference(self, reference_id: str) -> bool:
        metadata = next((item for item in self.references.metadata() if item["id"] == reference_id), None)
        if metadata is None or not self.references.remove(reference_id):
            return False
        self._scrub_reference_messages(reference_id)
        self._trace("context_removed", metadata)
        self._emit("context_removed", metadata)
        return True

    def enter_plan_mode(self, call_id: str) -> ToolResult:
        if self.plan_state is PlanState.INACTIVE:
            self.plan_state = PlanState.PLANNING
            self.pending_plan_text = None
            self._apply_effective_mode()
            self._emit("plan_started", {"execution_mode": self.execution_mode.value})
        return ToolResult.success(
            tool="enter_plan_mode",
            call_id=call_id,
            summary="entered temporary read-only Plan Mode",
            data={"plan_state": self.plan_state.value, "execution_mode": self.execution_mode.value},
        )

    def request_plan_approval(self, call_id: str, plan_text: str) -> ToolResult:
        if self.plan_state is not PlanState.PLANNING:
            return ToolResult.failure(
                tool="exit_plan_mode",
                call_id=call_id,
                code="INVALID_STATE",
                message="exit_plan_mode is only available while planning",
            )
        if not isinstance(plan_text, str) or not plan_text.strip():
            return ToolResult.failure(
                tool="exit_plan_mode",
                call_id=call_id,
                code="INVALID_ARGUMENT",
                message="plan must be a non-empty string",
            )
        self.plan_state = PlanState.WAITING_APPROVAL
        self.pending_plan_text = plan_text.strip()
        self._apply_effective_mode()
        payload = {"plan": self.pending_plan_text, "execution_mode": self.execution_mode.value}
        self._trace("plan_ready", payload)
        return ToolResult.success(
            tool="exit_plan_mode",
            call_id=call_id,
            summary="plan is waiting for user approval",
            data={"plan_state": self.plan_state.value, **payload},
        )

    def resume_plan(self, *, execute: bool, feedback: str | None = None) -> None:
        if self.plan_state is not PlanState.WAITING_APPROVAL:
            raise ValueError("the session is not waiting for plan approval")
        self.pending_plan_text = None
        self.plan_state = PlanState.INACTIVE if execute else PlanState.PLANNING
        self._apply_effective_mode()
        self._trace(
            "plan_resolved",
            {"action": "execute" if execute else "revise", "feedback": feedback},
        )

    def _tool_schemas(self) -> list[dict[str, Any]]:
        if self.plan_state is not PlanState.INACTIVE:
            read_tools = [schema for schema in TOOL_SCHEMAS if schema["function"]["name"] in {"list_files", "search_text", "read_file"}]
            return read_tools + ([EXIT_PLAN_SCHEMA] if self.plan_state is PlanState.PLANNING else [])
        return TOOL_SCHEMAS + [ENTER_PLAN_SCHEMA]

    def _trace(self, event: str, payload: dict[str, Any]) -> None:
        if self.trace:
            self.trace.write(event, payload)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.on_event:
            self.on_event(event_type, payload)

    @staticmethod
    def _assistant_message(reply: ModelReply) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                for c in reply.tool_calls
            ]
        return message

    def _verification_status(self) -> str:
        if not self.tools.change_seq:
            return "NOT_RUN"
        verification = self.tools.last_verification
        if verification and verification.get("change_seq") == self.tools.change_seq:
            return str(verification["status"])
        return "NOT_RUN"

    def _cancel_unanswered_tool_calls(self, reason: str) -> None:
        assistant_index = next(
            (index for index in range(len(self.messages) - 1, -1, -1) if self.messages[index].get("tool_calls")),
            None,
        )
        if assistant_index is None:
            return
        calls = self.messages[assistant_index].get("tool_calls") or []
        answered = {
            message.get("tool_call_id")
            for message in self.messages[assistant_index + 1 :]
            if message.get("role") == "tool"
        }
        for call in calls:
            if call["id"] in answered:
                continue
            result = ToolResult.failure(
                tool=call["function"]["name"],
                call_id=call["id"],
                code="TOOL_CALL_CANCELLED",
                message=reason,
            )
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": serialize_tool_result(result)})

    def _outcome(self, reason: StopReason, text: str, turns: int) -> AgentOutcome:
        outcome = AgentOutcome(reason, text, turns, self._verification_status(), self.tools.last_verification)
        self._trace("final", {"stop_reason": reason.value, "text": text, "turns": turns, "verification_status": outcome.verification_status})
        self._emit(
            "turn_completed",
            {
                "stop_reason": reason.value,
                "text": text,
                "turns": turns,
                "verification_status": outcome.verification_status,
                "verification": outcome.verification,
            },
        )
        return outcome

    def run_turn(self, prompt: str) -> AgentOutcome:
        try:
            loaded_references = self.references.load_from_prompt(prompt)
        except ExternalReferenceError as exc:
            self.prompt_count += 1
            self._emit("user_prompt", {"text": prompt, "prompt_index": self.prompt_count})
            self._trace("prompt_start", {"prompt": prompt, "prompt_index": self.prompt_count})
            payload = {"code": exc.code, "message": str(exc), "path": exc.path}
            self._trace("context_error", payload)
            self._emit("context_error", payload)
            return self._outcome(StopReason.CONTEXT_ERROR, f"引用文件失败：{exc}", 0)
        for reference in loaded_references:
            self._scrub_reference_messages(reference.id)
            metadata = reference.metadata()
            self._trace("context_loaded", metadata)
            self._emit("context_loaded", metadata)
        self.prompt_count += 1
        self._active_turn = 0
        self.tools.begin_prompt(self.prompt_count)
        user_message = {"role": "user", "content": prompt}
        self.messages.append(user_message)
        reference_entry = {"message": user_message, "prompt": prompt, "references": []}
        self._reference_messages.append(reference_entry)
        self._attach_missing_references(reference_entry)
        self._emit("user_prompt", {"text": prompt, "prompt_index": self.prompt_count})
        turns = 0
        previous_fingerprint: str | None = None
        repeated = 0
        verification_nudged = False
        if self.prompt_count == 1:
            self._trace(
                "session_start",
                {
                    "task": prompt,
                    "workspace": str(self.tools.guard.root),
                    "max_turns": self.max_turns_per_prompt,
                },
            )
        self._trace("prompt_start", {"prompt": prompt, "prompt_index": self.prompt_count})
        try:
            while turns < self.max_turns_per_prompt:
                if self._interrupt_event.is_set():
                    return self._interrupted(turns)
                turns += 1
                self._active_turn = turns
                self._refresh_runtime_prompt()
                self._compact_history(turn=turns)
                self._attach_missing_references(reference_entry)
                reply = self.model.complete(self.messages, self._tool_schemas())
                self._trace(
                    "model_reply",
                    {
                        "turn": turns,
                        "reasoning_content": reply.reasoning_content,
                        "content": reply.content,
                        "tool_calls": [c.__dict__ for c in reply.tool_calls],
                    },
                )
                if self._interrupt_event.is_set():
                    self.messages.append(self._assistant_message(reply))
                    return self._interrupted(turns)
                if reply.reasoning_content:
                    self._emit("model_reasoning", {"content": reply.reasoning_content, "turn": turns})
                if reply.content and reply.tool_calls:
                    self._emit("model_message", {"content": reply.content, "turn": turns})
                self.messages.append(self._assistant_message(reply))
                if not reply.tool_calls:
                    if (
                        self.tools.change_seq
                        and self._verification_status() == "NOT_RUN"
                        and not verification_nudged
                        and turns < self.max_turns_per_prompt
                    ):
                        verification_nudged = True
                        self.messages.append({"role": "user", "content": "You changed files but have not verified them. If possible, run a test, build, or lint command now; otherwise explain why verification cannot be run."})
                        continue
                    return self._outcome(StopReason.COMPLETED, reply.content or "", turns)

                for call in reply.tool_calls:
                    if self._interrupt_event.is_set():
                        return self._interrupted(turns)
                    self._emit(
                        "tool_call",
                        {"call_id": call.id, "name": call.name, "arguments": call.arguments, "turn": turns},
                    )
                    fingerprint = json.dumps({"name": call.name, "arguments": call.arguments}, ensure_ascii=False, sort_keys=True)
                    repeated = repeated + 1 if fingerprint == previous_fingerprint else 1
                    previous_fingerprint = fingerprint
                    if call.name == "enter_plan_mode":
                        result = self.enter_plan_mode(call.id)
                    elif call.name == "exit_plan_mode":
                        result = self.request_plan_approval(call.id, call.arguments.get("plan", ""))
                    else:
                        result = self.tools.execute(call.name, call.id, call.arguments)
                    if self.on_tool_result:
                        self.on_tool_result(result)
                    self._emit("tool_result", {**result.to_dict(), "turn": turns})
                    if result.ok and call.name in {"write_file", "edit_file"} and isinstance(result.data, dict):
                        changed_path = str(result.data.get("path", ""))
                        change = next(
                            (
                                item
                                for item in self.tools.changes_snapshot(self.prompt_count)
                                if item.get("path") == changed_path
                            ),
                            None,
                        )
                    else:
                        change = None
                    if change:
                        self._emit("file_changed", dict(change))
                        self._emit(
                            "diff",
                            {
                                "call_id": call.id,
                                "path": changed_path,
                                "diff": change["diff"],
                            },
                        )
                    if call.name == "run_shell" and isinstance(result.data, dict):
                        for step in result.data.get("commands", []):
                            self._emit(
                                "command_output",
                                {
                                    "call_id": call.id,
                                    "index": step.get("index"),
                                    "status": step.get("status"),
                                    "command": step.get("command", ""),
                                    "purpose": step.get("purpose"),
                                    "exit_code": step.get("exit_code"),
                                    "stdout": step.get("stdout", ""),
                                    "stderr": step.get("stderr", ""),
                                    "permission": step.get("permission"),
                                    "review": step.get("review"),
                                    "turn": turns,
                                },
                            )
                        self._emit(
                            "verification",
                            {"status": self._verification_status(), "evidence": self.tools.last_verification},
                        )
                    if result.ok:
                        repeated = 0
                        previous_fingerprint = None
                    content = serialize_tool_result(result, limit=self.context.policy.tool_result_limit)
                    self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                    self._trace("tool_result", result.to_dict())
                    if self._interrupt_event.is_set():
                        return self._interrupted(turns)
                    if call.name == "exit_plan_mode" and result.ok:
                        self._cancel_unanswered_tool_calls("Skipped because the completed plan is waiting for user approval.")
                        return self._outcome(StopReason.COMPLETED, self.pending_plan_text or "", turns)
                    if repeated >= 3:
                        self._cancel_unanswered_tool_calls("Skipped because the current prompt stopped after repeated failed calls.")
                        return self._outcome(StopReason.REPEATED_CALL, "Stopped after three identical failed tool calls without progress.", turns)
            return self._outcome(
                StopReason.MAX_TURNS,
                f"Stopped after reaching the maximum of {self.max_turns_per_prompt} model turns.",
                turns,
            )
        except KeyboardInterrupt:
            self._cancel_unanswered_tool_calls("Skipped because the prompt was interrupted.")
            return self._outcome(StopReason.INTERRUPTED, "Interrupted by user.", turns)
        except Exception as exc:
            self._cancel_unanswered_tool_calls("Skipped because prompt processing stopped after an error.")
            self._trace("model_error", {"type": type(exc).__name__, "message": str(exc)})
            self._emit("error", {"code": type(exc).__name__, "message": str(exc)})
            return self._outcome(StopReason.MODEL_ERROR, f"Model error: {exc}", turns)


class Agent(AgentSession):
    """Backward-compatible one-shot Agent used by the terminal CLI."""

    def __init__(
        self,
        model: Model,
        tools: ToolRuntime,
        *,
        max_turns: int = 50,
        trace: SessionTrace | None = None,
        on_tool_result: Callable[[ToolResult], None] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(
            model,
            tools,
            max_turns_per_prompt=max_turns,
            trace=trace,
            on_tool_result=on_tool_result,
            on_event=on_event,
        )
        self.max_turns = max_turns

    def run(self, task: str) -> AgentOutcome:
        return self.run_turn(task)
