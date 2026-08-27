from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import ToolResult


def truncate_text(text: str, *, limit: int = 16_000) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n...[truncated]...\n"
    if limit <= len(marker):
        return text[:limit], True
    available = limit - len(marker)
    head = int(available * 0.7)
    tail = available - head
    return text[:head] + marker + text[-tail:], True


def serialize_tool_result(result: "ToolResult", *, limit: int = 16_000) -> str:
    payload = result.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) <= limit:
        return encoded
    result.meta.truncated = True
    payload = result.to_dict()
    original_data = json.dumps(payload.get("data"), ensure_ascii=False)
    payload["summary"], _ = truncate_text(str(payload.get("summary") or ""), limit=256)
    if payload.get("error"):
        payload["error"]["message"], _ = truncate_text(payload["error"]["message"], limit=256)
    payload["data"] = {"preview": ""}
    low, high = 0, len(original_data)
    best = json.dumps(payload, ensure_ascii=False)
    while low <= high:
        candidate_limit = (low + high) // 2
        preview, _ = truncate_text(original_data, limit=candidate_limit)
        payload["data"]["preview"] = preview
        candidate = json.dumps(payload, ensure_ascii=False)
        if len(candidate) <= limit:
            best = candidate
            low = candidate_limit + 1
        else:
            high = candidate_limit - 1
    return best


def _summarize_removed(messages: list[dict[str, Any]]) -> str:
    facts: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == "user" and message.get("content"):
            facts.append(f"user: {message['content']}")
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            facts.append(f"tool_call {function.get('name')}: {function.get('arguments')}")
        if role == "tool":
            facts.append(f"tool_result: {message.get('content')}")
        elif role == "assistant" and message.get("content"):
            facts.append(f"assistant: {message['content']}")
    summary, _ = truncate_text("Earlier context summary:\n" + "\n".join(facts), limit=4_000)
    return summary


def compact_messages(messages: list[dict[str, Any]], *, max_chars: int = 80_000) -> list[dict[str, Any]]:
    """Compact old complete conversation groups without splitting tool calls/results."""
    size = sum(len(str(message)) for message in messages)
    if size <= max_chars or len(messages) < 8:
        return messages
    keep_from = max(1, len(messages) // 2)
    while keep_from < len(messages) and messages[keep_from].get("role") == "tool":
        keep_from += 1
    removed = messages[1:keep_from]
    summary = {"role": "system", "content": _summarize_removed(removed)}
    return [messages[0], summary, *messages[keep_from:]]
