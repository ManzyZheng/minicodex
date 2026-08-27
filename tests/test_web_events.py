from __future__ import annotations

import asyncio
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


def test_event_bus_bounds_history_and_entire_payload() -> None:
    bus = EventBus(max_events=2, max_event_chars=120)
    bus.publish("one", {"text": "a"})
    bus.publish("two", {"items": ["b" * 20 for _ in range(100)]})
    third = bus.publish("three", {"text": "c"})

    retained = bus.after(0)
    assert [event.type for event in retained] == ["two", "three"]
    assert third.id == 3
    assert retained[0].payload["_truncated"] is True
    assert len(retained[0].payload["preview"]) <= 120


def test_async_subscription_replays_and_unregisters() -> None:
    async def scenario() -> None:
        bus = EventBus()
        bus.publish("old", {"value": 1})
        subscription = bus.subscribe(0)
        assert [event.type for event in subscription.replay] == ["old"]
        bus.publish("new", {"value": 2})
        assert (await asyncio.wait_for(subscription.queue.get(), 0.2)).type == "new"
        subscription.close()
        assert bus.subscriber_count() == 0

    asyncio.run(scenario())
