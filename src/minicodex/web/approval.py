from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import uuid4

from .events import EventBus


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    argv: list[str]
    purpose: str
    timeout_sec: int


class ApprovalGate:
    def __init__(self, events: EventBus, *, wait_timeout: float = 300.0) -> None:
        self.events = events
        self.wait_timeout = wait_timeout
        self._condition = threading.Condition()
        self._pending: ApprovalRequest | None = None
        self._resolved = False
        self._decision = False
        self._closed = False

    def pending(self) -> ApprovalRequest | None:
        with self._condition:
            return self._pending

    def request(self, argv: list[str], purpose: str, timeout_sec: int) -> bool:
        with self._condition:
            if self._closed:
                return False
            if self._pending is not None:
                raise RuntimeError("another command approval is already pending")
            request = ApprovalRequest(uuid4().hex, list(argv), purpose, timeout_sec)
            self._pending = request
            self._resolved = False
            self._decision = False

        self.events.publish(
            "approval_required",
            {
                "request_id": request.id,
                "argv": request.argv,
                "purpose": request.purpose,
                "timeout_sec": request.timeout_sec,
                "approval_timeout_sec": self.wait_timeout,
            },
        )
        self.events.publish("status", {"value": "WAITING_APPROVAL"})

        with self._condition:
            resolved = self._condition.wait_for(lambda: self._resolved or self._closed, timeout=self.wait_timeout)
            decision = self._decision if resolved and not self._closed else False
            reason = "closed" if self._closed else ("allowed" if decision else ("rejected" if resolved else "timeout"))
            self._pending = None
            self._resolved = False
            self._decision = False

        self.events.publish(
            "approval_resolved",
            {"request_id": request.id, "allow": decision, "reason": reason},
        )
        if reason != "closed":
            self.events.publish("status", {"value": "RUNNING"})
        return decision

    def resolve(self, request_id: str, allow: bool) -> bool:
        with self._condition:
            if self._pending is None or self._pending.id != request_id or self._resolved:
                return False
            self._decision = bool(allow)
            self._resolved = True
            self._condition.notify_all()
            return True

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
