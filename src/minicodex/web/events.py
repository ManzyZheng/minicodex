from __future__ import annotations

import threading
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
    def __init__(self) -> None:
        self._events: list[WebEvent] = []
        self._condition = threading.Condition()

    def publish(self, event_type: str, payload: dict[str, Any]) -> WebEvent:
        with self._condition:
            event = WebEvent(
                id=len(self._events) + 1,
                type=event_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=dict(payload),
            )
            self._events.append(event)
            self._condition.notify_all()
            return event

    def after(self, last_id: int) -> list[WebEvent]:
        with self._condition:
            return list(self._events[max(0, last_id) :])

    def wait_after(self, last_id: int, timeout: float) -> list[WebEvent]:
        with self._condition:
            self._condition.wait_for(lambda: len(self._events) > last_id, timeout=timeout)
            return list(self._events[max(0, last_id) :])
