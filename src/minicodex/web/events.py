from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class WebEvent:
    id: int
    type: str
    timestamp: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    def __init__(self, *, max_events: int = 2_000, max_string_chars: int = 16_000) -> None:
        self._events: deque[WebEvent] = deque(maxlen=max_events)
        self._next_id = 1
        self._max_string_chars = max_string_chars
        self._condition = threading.Condition()

    def _bounded(self, value: Any) -> Any:
        if isinstance(value, str) and len(value) > self._max_string_chars:
            marker = "\n...[web event truncated]...\n"
            available = self._max_string_chars - len(marker)
            head = max(0, int(available * 0.7))
            tail = max(0, available - head)
            return value[:head] + marker + (value[-tail:] if tail else "")
        if isinstance(value, dict):
            return {key: self._bounded(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._bounded(item) for item in value]
        return value

    def publish(self, event_type: str, payload: dict[str, Any]) -> WebEvent:
        with self._condition:
            event = WebEvent(
                id=self._next_id,
                type=event_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=self._bounded(dict(payload)),
            )
            self._next_id += 1
            self._events.append(event)
            self._condition.notify_all()
            return event

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
