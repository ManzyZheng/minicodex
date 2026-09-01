from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass(frozen=True)
class WebEvent:
    id: int
    type: str
    timestamp: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventSubscription:
    bus: "EventBus"
    subscriber_id: int
    replay: list[WebEvent]
    queue: asyncio.Queue[WebEvent]

    def close(self) -> None:
        self.bus.unsubscribe(self.subscriber_id)


class EventBus:
    def __init__(self, *, max_events: int = 2_000, max_event_chars: int = 32_000) -> None:
        self._events: deque[WebEvent] = deque(maxlen=max_events)
        self._next_id = 1
        self._max_event_chars = max_event_chars
        self._next_subscriber_id = 1
        self._subscribers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue[WebEvent]]] = {}
        self._next_listener_id = 1
        self._listeners: dict[int, Callable[[WebEvent], None]] = {}
        self._condition = threading.Condition()

    def _bounded_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) <= self._max_event_chars:
            return dict(payload)
        low, high = 0, len(encoded)
        best: dict[str, Any] = {"_truncated": True, "preview": ""}
        while low <= high:
            midpoint = (low + high) // 2
            candidate = {"_truncated": True, "preview": encoded[:midpoint]}
            size = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
            if size <= self._max_event_chars:
                best = candidate
                low = midpoint + 1
            else:
                high = midpoint - 1
        return best

    @staticmethod
    def _enqueue(queue: asyncio.Queue[WebEvent], event: WebEvent) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(event)

    def publish(self, event_type: str, payload: dict[str, Any]) -> WebEvent:
        with self._condition:
            event = WebEvent(
                id=self._next_id,
                type=event_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=self._bounded_payload(payload),
            )
            self._next_id += 1
            self._events.append(event)
            self._condition.notify_all()
            subscribers = list(self._subscribers.values())
            listeners = list(self._listeners.values())
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                pass
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._enqueue, queue, event)
            except RuntimeError:
                pass
        return event

    def add_listener(self, listener: Callable[[WebEvent], None]) -> int:
        with self._condition:
            listener_id = self._next_listener_id
            self._next_listener_id += 1
            self._listeners[listener_id] = listener
            return listener_id

    def remove_listener(self, listener_id: int) -> bool:
        with self._condition:
            return self._listeners.pop(listener_id, None) is not None

    def after(self, last_id: int) -> list[WebEvent]:
        with self._condition:
            return [event for event in self._events if event.id > last_id]

    def wait_after(self, last_id: int, timeout: float) -> list[WebEvent]:
        with self._condition:
            self._condition.wait_for(lambda: self._next_id - 1 > last_id, timeout=timeout)
            return [event for event in self._events if event.id > last_id]

    def latest_id(self) -> int:
        with self._condition:
            return self._next_id - 1

    def subscribe(self, last_id: int, *, queue_size: int = 256) -> EventSubscription:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[WebEvent] = asyncio.Queue(maxsize=queue_size)
        with self._condition:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            replay = [event for event in self._events if event.id > last_id]
            self._subscribers[subscriber_id] = (loop, queue)
        return EventSubscription(self, subscriber_id, replay, queue)

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._condition:
            self._subscribers.pop(subscriber_id, None)

    def subscriber_count(self) -> int:
        with self._condition:
            return len(self._subscribers)
