from __future__ import annotations

import threading

from minicodex.web.events import EventBus


def test_event_bus_assigns_ids_and_replays_after_cursor() -> None:
    bus = EventBus()
    first = bus.publish("status", {"value": "RUNNING"})
    second = bus.publish("diff", {"path": "a.py"})

    assert first.id == 1
    assert second.id == 2
    assert [event.type for event in bus.after(1)] == ["diff"]


def test_event_bus_wait_wakes_when_event_is_published() -> None:
    bus = EventBus()
    ready = threading.Event()
    received = []

    def wait_for_event() -> None:
        ready.set()
        received.extend(bus.wait_after(0, timeout=0.5))

    thread = threading.Thread(target=wait_for_event)
    thread.start()
    assert ready.wait(0.2)
    bus.publish("status", {"value": "IDLE"})
    thread.join(0.5)

    assert not thread.is_alive()
    assert received[0].payload == {"value": "IDLE"}

