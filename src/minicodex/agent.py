from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from .context import compact_messages, serialize_tool_result
from .models import ModelReply, ToolCall, ToolResult
from .session import SessionTrace
from .tools import TOOL_SCHEMAS, ToolRuntime


SYSTEM_PROMPT = """You are MiniCodex, a coding agent working only inside the provided workspace.
Inspect before changing existing files. Use edit_file only with an exact unique old_text.
Use argv arrays for commands. Tool errors are recoverable: read the error and adjust.
After changing code, run a relevant test, build, or lint command when possible.
Be honest in the final answer about what was verified and what was not."""


class Model(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...


class StopReason(str, Enum):
    COMPLETED = "COMPLETED"
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
        max_turns_per_prompt: int = 20,
        trace: SessionTrace | None = None,
        on_tool_result: Callable[[ToolResult], None] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.max_turns_per_prompt = max_turns_per_prompt
        self.trace = trace
        self.on_tool_result = on_tool_result
        self.on_event = on_event
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.prompt_count = 0

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
        self.prompt_count += 1
        self.messages.append({"role": "user", "content": prompt})
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
                turns += 1
                self.messages = compact_messages(self.messages)
                reply = self.model.complete(self.messages, TOOL_SCHEMAS)
                self._trace("model_reply", {"turn": turns, "content": reply.content, "tool_calls": [c.__dict__ for c in reply.tool_calls]})
                if reply.content:
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
                    self._emit(
                        "tool_call",
                        {"call_id": call.id, "name": call.name, "arguments": call.arguments, "turn": turns},
                    )
                    fingerprint = json.dumps({"name": call.name, "arguments": call.arguments}, ensure_ascii=False, sort_keys=True)
                    repeated = repeated + 1 if fingerprint == previous_fingerprint else 1
                    previous_fingerprint = fingerprint
                    result = self.tools.execute(call.name, call.id, call.arguments)
                    if self.on_tool_result:
                        self.on_tool_result(result)
                    self._emit("tool_result", result.to_dict())
                    if result.ok and isinstance(result.data, dict) and result.data.get("diff"):
                        self._emit(
                            "diff",
                            {
                                "call_id": call.id,
                                "path": str(call.arguments.get("path", "")),
                                "diff": result.data["diff"],
                            },
                        )
                    if call.name == "run_command" and isinstance(result.data, dict):
                        self._emit(
                            "command_output",
                            {
                                "call_id": call.id,
                                "argv": result.data.get("argv", call.arguments.get("argv", [])),
                                "purpose": result.data.get("purpose", call.arguments.get("purpose")),
                                "exit_code": result.data.get("exit_code"),
                                "stdout": result.data.get("stdout", ""),
                                "stderr": result.data.get("stderr", ""),
                            },
                        )
                        self._emit(
                            "verification",
                            {"status": self._verification_status(), "evidence": self.tools.last_verification},
                        )
                    if result.ok:
                        repeated = 0
                        previous_fingerprint = None
                    content = serialize_tool_result(result)
                    self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                    self._trace("tool_result", result.to_dict())
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
        max_turns: int = 20,
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
