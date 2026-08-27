from __future__ import annotations

import threading

from minicodex.web.approval import ApprovalGate
from minicodex.web.events import EventBus


def test_approval_gate_blocks_until_matching_request_is_allowed() -> None:
    bus = EventBus()
    gate = ApprovalGate(bus, wait_timeout=0.5)
    decisions: list[bool] = []
    thread = threading.Thread(target=lambda: decisions.append(gate.request(["python", "-m", "pytest"], "test", 30)))
    thread.start()

    event = bus.wait_after(0, timeout=0.2)[0]
    assert event.type == "approval_required"
    assert event.payload["purpose"] == "test"
    assert gate.resolve(event.payload["request_id"], True)
    thread.join(0.5)

    assert decisions == [True]
    assert gate.pending() is None


def test_approval_gate_rejects_on_timeout_and_stale_id() -> None:
    bus = EventBus()
    gate = ApprovalGate(bus, wait_timeout=0.01)

    assert gate.resolve("not-pending", True) is False
    assert gate.request(["python", "-V"], "other", 30) is False
    resolved = [event for event in bus.after(0) if event.type == "approval_resolved"]
    assert resolved[-1].payload["reason"] == "timeout"


def test_approval_gate_reports_waiting_and_running_status() -> None:
    bus = EventBus()
    gate = ApprovalGate(bus, wait_timeout=0.5)
    thread = threading.Thread(target=lambda: gate.request(["pytest"], "test", 120))
    thread.start()
    event = bus.wait_after(0, timeout=0.2)[0]
    assert gate.resolve(event.payload["request_id"], True)
    thread.join(0.5)

    statuses = [item.payload["value"] for item in bus.after(0) if item.type == "status"]
    assert statuses == ["WAITING_APPROVAL", "RUNNING"]
