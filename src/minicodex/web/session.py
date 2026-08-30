from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from ..agent import AgentOutcome, AgentSession
from ..permissions import AgentMode, PlanState
from .approval import ApprovalGate
from .events import EventBus


class SessionBusyError(RuntimeError):
    pass


class PlanResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingPlan:
    id: str
    text: str
    execution_mode: str


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
        allowed_models: tuple[str, ...] | None = None,
        on_prompt_complete: Callable[["WebSession", str, AgentOutcome], None] | None = None,
    ) -> None:
        self.agent = agent
        self.events = events
        self.approvals = approvals
        self.workspace = Path(workspace).resolve()
        self.model_name = model_name
        self.allowed_models = allowed_models or (model_name,)
        self.max_turns_per_prompt = max_turns_per_prompt
        self.on_prompt_complete = on_prompt_complete
        self._condition = threading.Condition()
        self._status = "IDLE"
        self._closed = False
        self._worker: threading.Thread | None = None
        self._pending_plan: PendingPlan | None = None
        self._restored_file_changes: list[dict[str, Any]] = []
        self._restored_verification_status = "NOT_RUN"
        self.events.publish(
            "session_started",
            {
                "workspace": str(self.workspace),
                "model": self.model_name,
                "max_turns_per_prompt": self.max_turns_per_prompt,
                "mode": self.agent.tools.mode.value,
                "execution_mode": self.agent.execution_mode.value,
                "plan_state": self.agent.plan_state.value,
            },
        )

    def _verification_status(self) -> str:
        runtime = self.agent.tools
        if not runtime.change_seq:
            return self._restored_verification_status
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
        if status == "STOPPING":
            pass
        elif pending is not None:
            status = "WAITING_APPROVAL"
        elif self._pending_plan is not None:
            status = "WAITING_PLAN_APPROVAL"
        return {
            "workspace": str(self.workspace),
            "model": self.model_name,
            "allowed_models": list(self.allowed_models),
            "status": status,
            "verification_status": self._verification_status(),
            "mode": self.agent.tools.mode.value,
            "execution_mode": self.agent.execution_mode.value,
            "plan_state": self.agent.plan_state.value,
            "pending_plan": asdict(self._pending_plan) if self._pending_plan else None,
            "file_changes": self._file_changes_snapshot(),
            "max_turns_per_prompt": self.max_turns_per_prompt,
            "prompt_count": self.agent.prompt_count,
            "event_id": event_id,
            "pending_approval": pending.to_payload(wait_timeout=self.approvals.wait_timeout) if pending else None,
            "references": self.agent.reference_metadata(),
            "context": self.agent.context_snapshot(),
        }

    def restore_presentation_state(self, *, file_changes: list[dict[str, Any]], verification_status: str) -> None:
        self._restored_file_changes = [dict(item) for item in file_changes if isinstance(item, dict)]
        self._restored_verification_status = verification_status or "NOT_RUN"

    def _file_changes_snapshot(self) -> list[dict[str, Any]]:
        current = self.agent.tools.changes_snapshot()
        merged = {(item.get("prompt_index"), item.get("path")): dict(item) for item in self._restored_file_changes}
        merged.update({(item.get("prompt_index"), item.get("path")): dict(item) for item in current})
        return list(merged.values())

    def interrupt(self) -> bool:
        with self._condition:
            if self._status not in {"RUNNING", "WAITING_APPROVAL"}:
                return False
            self._status = "STOPPING"
            pending = self.approvals.pending()
            self.agent.request_interrupt()
            self.events.publish("status", {"value": "STOPPING"})
        if pending is not None:
            self.approvals.cancel_pending(pending.id)
        return True

    def remove_reference(self, reference_id: str) -> bool:
        with self._condition:
            if self._status != "IDLE":
                raise SessionBusyError("references can only be removed while the Agent is idle")
            return self.agent.remove_reference(reference_id)

    def set_mode(self, mode: AgentMode) -> AgentMode:
        with self._condition:
            if self._status != "IDLE":
                raise SessionBusyError("mode can only change while the Agent is idle")
            if mode is AgentMode.PLAN:
                self.agent.set_mode(mode)
            else:
                self.agent.set_execution_mode(mode)
                if self._pending_plan is not None:
                    self._pending_plan = PendingPlan(
                        self._pending_plan.id,
                        self._pending_plan.text,
                        mode.value,
                    )
                    self.events.publish("plan_ready", asdict(self._pending_plan))
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
            self.agent.execution_mode = mode
            if self._pending_plan is not None:
                plan = self._pending_plan
                self._pending_plan = None
                self.agent.resume_plan(execute=True)
                prompt = f"{prompt}\n\nApproved plan:\n{plan.text}"
            else:
                self.agent.set_mode(mode)
            self._start_prompt_locked(prompt)

    def mark_plan_ready(self, text: str) -> PendingPlan:
        if self.agent.plan_state is PlanState.PLANNING:
            result = self.agent.request_plan_approval("web-plan", text)
            if not result.ok:
                raise ValueError(result.summary)
        elif self.agent.plan_state is not PlanState.WAITING_APPROVAL:
            raise ValueError("the session is not in Plan Mode")
        plan_text = (self.agent.pending_plan_text or text).strip()
        plan = PendingPlan(uuid.uuid4().hex, plan_text, self.agent.execution_mode.value)
        with self._condition:
            self._pending_plan = plan
        self.events.publish("plan_ready", asdict(plan))
        return plan

    def resolve_plan(
        self,
        plan_id: str,
        action: Literal["execute", "revise", "cancel"],
        feedback: str | None = None,
    ) -> None:
        with self._condition:
            if self._status != "IDLE":
                raise SessionBusyError("the Agent must be idle before resolving a plan")
            plan = self._pending_plan
            if plan is None or plan.id != plan_id:
                raise PlanResolutionError("plan is missing, stale, or already resolved")
            self._pending_plan = None
            if action == "cancel":
                self.agent.resume_plan(execute=True)
                self.events.publish("plan_resolved", {"id": plan.id, "action": action})
                return
            if action == "revise":
                revision = (feedback or "").strip()
                if not revision:
                    self._pending_plan = plan
                    raise ValueError("feedback is required when revising a plan")
                self.agent.resume_plan(execute=False, feedback=revision)
                self.events.publish("plan_resolved", {"id": plan.id, "action": action})
                self._start_prompt_locked(revision)
                return
            self.agent.resume_plan(execute=True)
            self.events.publish("plan_resolved", {"id": plan.id, "action": action})
            self._start_prompt_locked(
                "Implement the approved plan below. Preserve its constraints and verify the completed changes.\n\n"
                f"{plan.text}"
            )

    def submit_prompt(
        self,
        text: str,
        *,
        permission: AgentMode | None = None,
        model: str | None = None,
    ) -> None:
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
            if permission is not None:
                self.agent.set_execution_mode(permission)
            if model is not None:
                if model not in self.allowed_models:
                    raise ValueError(f"model is not allowed: {model}")
                setter = getattr(self.agent.model, "set_model", None)
                if callable(setter):
                    setter(model)
                self.model_name = model
            if self._pending_plan is not None:
                plan = self._pending_plan
                self._pending_plan = None
                if prompt.casefold() in {"执行", "执行方案", "开始执行", "execute"}:
                    self.agent.resume_plan(execute=True)
                    self.events.publish("plan_resolved", {"id": plan.id, "action": "execute"})
                    prompt = (
                        "Implement the approved plan below. Preserve its constraints and verify the completed changes.\n\n"
                        f"{plan.text}"
                    )
                else:
                    self.agent.resume_plan(execute=False, feedback=prompt)
                    self.events.publish("plan_resolved", {"id": plan.id, "action": "revise"})
            self._start_prompt_locked(prompt)

    def _start_prompt_locked(self, prompt: str) -> None:
        self.agent.reset_interrupt()
        self._status = "RUNNING"
        self.events.publish("status", {"value": "RUNNING"})
        self._worker = threading.Thread(target=self._run_prompt, args=(prompt,), daemon=True)
        self._worker.start()

    def _run_prompt(self, prompt: str) -> None:
        try:
            outcome = self.agent.run_turn(prompt)
            if self.agent.plan_state is PlanState.WAITING_APPROVAL and self._pending_plan is None:
                self.mark_plan_ready(self.agent.pending_plan_text or "")
            if self.on_prompt_complete is not None:
                try:
                    self.on_prompt_complete(self, prompt, outcome)
                except Exception as exc:
                    self.events.publish(
                        "background_error",
                        {"code": type(exc).__name__, "message": str(exc), "operation": "persist_and_extract_memory"},
                    )
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
        if worker is not None:
            self.agent.request_interrupt()
        self.approvals.close()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, wait_timeout))
        with self._condition:
            self._status = "CLOSING" if worker is not None and worker.is_alive() else "CLOSED"
            self.events.publish("status", {"value": self._status})
            self._condition.notify_all()
