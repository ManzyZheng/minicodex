from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..agent import AgentSession
from ..permissions import AgentMode
from .approval import ApprovalGate
from .events import EventBus


class SessionBusyError(RuntimeError):
    pass


class WebSession:
    def __init__(
        self,
        agent: AgentSession,
        events: EventBus,
        approvals: ApprovalGate,
        *,
        workspace: str | Path,
        model_name: str,
        max_turns_per_prompt: int,
    ) -> None:
        self.agent = agent
        self.events = events
        self.approvals = approvals
        self.workspace = Path(workspace).resolve()
        self.model_name = model_name
        self.max_turns_per_prompt = max_turns_per_prompt
        self._condition = threading.Condition()
        self._status = "IDLE"
        self._closed = False
        self._worker: threading.Thread | None = None
        self.events.publish(
            "session_started",
            {
                "workspace": str(self.workspace),
                "model": self.model_name,
                "max_turns_per_prompt": self.max_turns_per_prompt,
                "mode": self.agent.tools.mode.value,
            },
        )

    def _verification_status(self) -> str:
        runtime = self.agent.tools
        if not runtime.change_seq:
            return "NOT_RUN"
        evidence = runtime.last_verification
        if evidence and evidence.get("change_seq") == runtime.change_seq:
            return str(evidence["status"])
        return "NOT_RUN"

    def snapshot(self) -> dict[str, Any]:
        for _attempt in range(3):
            before = self.events.latest_id()
            pending = self.approvals.pending()
            with self._condition:
                status = self._status
            event_id = self.events.latest_id()
            if before == event_id:
                break
        if pending is not None:
            status = "WAITING_APPROVAL"
        return {
            "workspace": str(self.workspace),
            "model": self.model_name,
            "status": status,
            "verification_status": self._verification_status(),
            "mode": self.agent.tools.mode.value,
            "max_turns_per_prompt": self.max_turns_per_prompt,
            "prompt_count": self.agent.prompt_count,
            "event_id": event_id,
            "pending_approval": pending.to_payload(wait_timeout=self.approvals.wait_timeout) if pending else None,
        }

    def set_mode(self, mode: AgentMode) -> AgentMode:
        with self._condition:
            if self._status != "IDLE":
                raise SessionBusyError("mode can only change while the Agent is idle")
            self.agent.set_mode(mode)
        return mode

    def approve_plan(self, mode: AgentMode) -> None:
        if mode is AgentMode.PLAN:
            raise ValueError("an approved plan must continue in act or auto-act mode")
        prompt = "Implement the approved plan above. Preserve its constraints and verify the completed changes."
        with self._condition:
            if self._status != "IDLE":
                raise SessionBusyError("the Agent must be idle before approving a plan")
            if self.agent.tools.mode is not AgentMode.PLAN:
                raise ValueError("the session is not in Plan Mode")
            self.agent.set_mode(mode)
            self._start_prompt_locked(prompt)

    def submit_prompt(self, text: str) -> None:
        prompt = text.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        if len(prompt) > 20_000:
            raise ValueError("prompt must not exceed 20000 characters")
        with self._condition:
            if self._closed:
                raise RuntimeError("web session is closed")
            if self._status != "IDLE":
                raise SessionBusyError("an Agent prompt is already running")
            self._start_prompt_locked(prompt)

    def _start_prompt_locked(self, prompt: str) -> None:
        self._status = "RUNNING"
        self.events.publish("status", {"value": "RUNNING"})
        self._worker = threading.Thread(target=self._run_prompt, args=(prompt,), daemon=True)
        self._worker.start()

    def _run_prompt(self, prompt: str) -> None:
        try:
            self.agent.run_turn(prompt)
        except Exception as exc:
            self.events.publish("error", {"code": type(exc).__name__, "message": str(exc)})
        finally:
            with self._condition:
                self._status = "CLOSED" if self._closed else "IDLE"
                self._worker = None
                self.events.publish("status", {"value": self._status})
                self._condition.notify_all()

    def wait_until_idle(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._status in {"IDLE", "CLOSED"}, timeout=timeout)

    def resolve_approval(self, request_id: str, allow: bool) -> bool:
        return self.approvals.resolve(request_id, allow)

    def close(self, *, wait_timeout: float = 2.0) -> None:
        with self._condition:
            self._closed = True
            worker = self._worker
        self.approvals.close()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, wait_timeout))
        with self._condition:
            self._status = "CLOSING" if worker is not None and worker.is_alive() else "CLOSED"
            self.events.publish("status", {"value": self._status})
            self._condition.notify_all()
