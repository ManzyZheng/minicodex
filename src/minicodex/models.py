from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolError:
    code: str
    message: str
    retryable: bool = False


@dataclass
class ToolMeta:
    duration_ms: int = 0
    truncated: bool = False
    artifact_path: str | None = None


@dataclass
class ToolResult:
    ok: bool
    tool: str
    call_id: str
    summary: str
    data: Any = None
    error: ToolError | None = None
    meta: ToolMeta = field(default_factory=ToolMeta)

    @classmethod
    def success(cls, *, tool: str, call_id: str, summary: str, data: Any = None) -> "ToolResult":
        return cls(True, tool, call_id, summary, data=data)

    @classmethod
    def failure(
        cls, *, tool: str, call_id: str, code: str, message: str, retryable: bool = False, data: Any = None
    ) -> "ToolResult":
        return cls(False, tool, call_id, message, data=data, error=ToolError(code, message, retryable))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelReply:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning_content: str | None = None
