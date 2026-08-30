from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .models import ToolResult


_REFERENCE_BLOCK = re.compile(
    r"\n\n<referenced_files\b[^>]*>\n.*?\n</referenced_files>",
    re.DOTALL,
)
_SUMMARY_PREFIX = "Earlier context summary:\n"
_COMPACTABLE_TOOLS = {"read_file", "search_text", "list_files", "run_shell"}
_STALE_TOOLS = {"read_file", "search_text", "list_files"}
_CHECKPOINT_PREFIX = '<workspace_checkpoint trust="untrusted-data" source="current-disk">\n'


@dataclass(frozen=True)
class ContextPolicy:
    """Session-scoped estimated-token budgets for the three compaction tiers."""

    tool_result_limit: int = 16_000
    budget_tokens: int = 60_000
    stale_tokens: int = 76_000
    auto_compact_tokens: int = 96_000
    target_tokens: int = 64_000
    recent_tool_turns: int = 2
    recent_tool_chars: int = 30_000
    summary_limit: int = 8_000
    min_stage_savings: int = 1_000

    def __post_init__(self) -> None:
        if not (0 < self.budget_tokens <= self.stale_tokens <= self.auto_compact_tokens):
            raise ValueError("context thresholds must be positive and ordered")
        if not 0 < self.target_tokens < self.auto_compact_tokens:
            raise ValueError("context target must be below the auto-compact threshold")
        if self.recent_tool_turns < 1:
            raise ValueError("recent_tool_turns must be positive")
        if self.recent_tool_chars < 1:
            raise ValueError("recent_tool_chars must be positive")
        if self.min_stage_savings < 0:
            raise ValueError("min_stage_savings must not be negative")


@dataclass(frozen=True)
class ContextCompactionResult:
    messages: list[dict[str, Any]]
    before_chars: int
    after_chars: int
    before_messages: int
    after_messages: int
    before_tokens: int
    after_tokens: int
    stages: tuple[str, ...] = ()
    reductions: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.after_chars < self.before_chars


def message_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(message, ensure_ascii=False, separators=(",", ":"))) for message in messages)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate mixed code/Chinese prompt tokens without adding a tokenizer dependency."""
    encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    ascii_chars = sum(ord(character) < 128 for character in encoded)
    non_ascii_chars = len(encoded) - ascii_chars
    return max(1, math.ceil(ascii_chars / 4 + non_ascii_chars))


def _tool_metadata(messages: list[dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
    metadata: dict[str, tuple[str, dict[str, Any]]] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            metadata[str(call.get("id") or "")] = (str(function.get("name") or ""), arguments)
    return metadata


def _replace_tool_body(content: Any, placeholder: str, *, limit: int | None = None) -> Any:
    try:
        payload = json.loads(str(content))
    except (TypeError, json.JSONDecodeError):
        if limit is not None:
            return truncate_text(str(content), limit=limit)[0]
        return placeholder
    data = payload.get("data")
    if isinstance(data, dict):
        for key, value in list(data.items()):
            if isinstance(value, str) and key in {"content", "stdout", "stderr", "preview", "diff", "files", "matches"}:
                data[key] = truncate_text(value, limit=limit)[0] if limit is not None else placeholder
            elif isinstance(value, list) and key == "commands":
                compacted_commands = []
                for command in value:
                    if not isinstance(command, dict):
                        continue
                    compacted = dict(command)
                    for stream in ("stdout", "stderr"):
                        if isinstance(compacted.get(stream), str):
                            compacted[stream] = (
                                truncate_text(compacted[stream], limit=limit)[0]
                                if limit is not None
                                else placeholder
                            )
                    compacted_commands.append(compacted)
                data[key] = compacted_commands
            elif isinstance(value, list) and key in {"files", "matches"}:
                data[key] = placeholder
        if not data:
            payload["data"] = {"content": placeholder}
    elif data is not None:
        payload["data"] = {"content": placeholder}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _protected_tool_indices(messages: list[dict[str, Any]], *, turns: int, chars: int) -> set[int]:
    """Protect the newest complete tool groups within a bounded character budget."""
    result_indices = {
        str(message.get("tool_call_id") or ""): index
        for index, message in enumerate(messages)
        if message.get("role") == "tool"
    }
    groups: list[tuple[int, list[int]]] = []
    for index, message in enumerate(messages):
        calls = message.get("tool_calls") or []
        tool_indices = [
            result_indices[str(call.get("id") or "")]
            for call in calls
            if str(call.get("id") or "") in result_indices
        ]
        if tool_indices:
            groups.append((index, tool_indices))

    protected: set[int] = set()
    used = 0
    selected = 0
    for assistant_index, tool_indices in reversed(groups):
        if selected >= turns:
            break
        group_chars = message_chars([messages[assistant_index], *[messages[index] for index in tool_indices]])
        if selected and used + group_chars > chars:
            break
        protected.update(tool_indices)
        used += group_chars
        selected += 1
    return protected


class ContextManager:
    """Apply light-to-heavy context compaction without breaking tool-call structure."""

    def __init__(self, policy: ContextPolicy | None = None) -> None:
        self.policy = policy or ContextPolicy()
        self.compaction_count = 0

    def _record_stage(self, stage, messages, operation, stages, reductions) -> None:
        before = message_chars(messages)
        snapshot = list(messages)
        operation()
        after = message_chars(messages)
        if before - after >= self.policy.min_stage_savings:
            stages.append(stage)
            reductions[stage] = before - after
        else:
            messages[:] = snapshot

    def _budget(self, messages: list[dict[str, Any]]) -> None:
        metadata = _tool_metadata(messages)
        tool_indices = [index for index, message in enumerate(messages) if message.get("role") == "tool"]
        protected = _protected_tool_indices(
            messages,
            turns=self.policy.recent_tool_turns,
            chars=self.policy.recent_tool_chars,
        )
        limit = max(256, min(8_000, self.policy.target_tokens // 4))
        for index in tool_indices:
            if index in protected:
                continue
            call_id = str(messages[index].get("tool_call_id") or "")
            name = metadata.get(call_id, ("", {}))[0]
            if name in _COMPACTABLE_TOOLS and len(str(messages[index].get("content") or "")) > limit:
                messages[index] = {**messages[index], "content": _replace_tool_body(messages[index].get("content"), "[旧工具结果已压缩]", limit=limit)}

    def _stale_snip(self, messages: list[dict[str, Any]]) -> None:
        metadata = _tool_metadata(messages)
        tool_indices = [index for index, message in enumerate(messages) if message.get("role") == "tool"]
        protected = _protected_tool_indices(
            messages,
            turns=self.policy.recent_tool_turns,
            chars=self.policy.recent_tool_chars,
        )
        seen: set[tuple[str, str]] = set()
        for index in reversed(tool_indices):
            call_id = str(messages[index].get("tool_call_id") or "")
            name, arguments = metadata.get(call_id, ("", {}))
            if name not in _STALE_TOOLS:
                continue
            identity = str(arguments.get("path") or arguments.get("query") or arguments.get("pattern") or "")
            key = (name, identity)
            if key in seen and index not in protected:
                messages[index] = {**messages[index], "content": _replace_tool_body(messages[index].get("content"), "[旧工具结果已裁剪；如有需要请重新读取]")}
            seen.add(key)

    @staticmethod
    def _checkpoint_message(files: list[dict[str, str]]) -> dict[str, Any] | None:
        if not files:
            return None
        content = _CHECKPOINT_PREFIX + json.dumps(files, ensure_ascii=False, separators=(",", ":")) + "\n</workspace_checkpoint>"
        return {"role": "user", "content": content}

    def _auto_compact(
        self,
        messages: list[dict[str, Any]],
        checkpoint_files: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        prefix_end = 0
        while (
            prefix_end < len(messages)
            and messages[prefix_end].get("role") == "system"
            and not str(messages[prefix_end].get("content") or "").startswith(_SUMMARY_PREFIX)
        ):
            prefix_end += 1
        protected_tool_indices = _protected_tool_indices(
            messages,
            turns=self.policy.recent_tool_turns,
            chars=self.policy.recent_tool_chars,
        )
        protected_call_ids = {
            str(messages[index].get("tool_call_id") or "") for index in protected_tool_indices
        }
        protected_group_starts = [
            index
            for index, message in enumerate(messages)
            if any(str(call.get("id") or "") in protected_call_ids for call in message.get("tool_calls") or [])
        ]
        latest_safe_boundary = min(protected_group_starts) if protected_group_starts else len(messages) - 1
        candidates = [
            index
            for index in range(prefix_end + 1, len(messages))
            if index <= latest_safe_boundary and messages[index].get("role") != "tool"
        ]
        best: list[dict[str, Any]] | None = None
        for keep_from in candidates:
            removed = messages[prefix_end:keep_from]
            if not removed:
                continue
            summary = {"role": "system", "content": _summarize_removed(removed, limit=self.policy.summary_limit)}
            checkpoint = self._checkpoint_message(checkpoint_files)
            candidate = [*messages[:prefix_end], summary]
            if checkpoint:
                candidate.append(checkpoint)
            candidate.extend(messages[keep_from:])
            best = candidate
            if estimate_tokens(candidate) <= self.policy.target_tokens:
                break
        return best or messages

    def prepare(
        self,
        source: list[dict[str, Any]],
        *,
        checkpoint_factory: Callable[[], list[dict[str, str]]] | None = None,
    ) -> ContextCompactionResult:
        before_chars = message_chars(source)
        before_tokens = estimate_tokens(source)
        messages = list(source)
        stages: list[str] = []
        reductions: dict[str, int] = {}
        if before_tokens >= self.policy.budget_tokens:
            self._record_stage("budget", messages, lambda: self._budget(messages), stages, reductions)
        if estimate_tokens(messages) >= self.policy.stale_tokens:
            self._record_stage("stale_snip", messages, lambda: self._stale_snip(messages), stages, reductions)
        if estimate_tokens(messages) >= self.policy.auto_compact_tokens:
            before = message_chars(messages)
            checkpoint_files = checkpoint_factory() if checkpoint_factory else []
            candidate = self._auto_compact(messages, checkpoint_files)
            after = message_chars(candidate)
            if before - after >= self.policy.min_stage_savings:
                messages = candidate
                stages.append("auto_checkpoint")
                reductions["auto_checkpoint"] = before - after
        after_chars = message_chars(messages)
        after_tokens = estimate_tokens(messages)
        if after_chars > before_chars:
            messages = source
            stages = []
            reductions = {}
            after_chars = before_chars
            after_tokens = before_tokens
        if stages:
            self.compaction_count += 1
        return ContextCompactionResult(
            messages=messages,
            before_chars=before_chars,
            after_chars=after_chars,
            before_messages=len(source),
            after_messages=len(messages),
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            stages=tuple(stages),
            reductions=reductions,
        )


def _without_reference_context(content: str) -> str:
    """Keep the user's request in summaries without retaining file snapshots."""
    return _REFERENCE_BLOCK.sub("", content)


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


def _summarize_removed(messages: list[dict[str, Any]], *, limit: int = 4_000) -> str:
    goals: list[str] = []
    conclusions: list[str] = []
    changed_paths: set[str] = set()
    actions: list[str] = []
    commands: list[str] = []
    failures: list[str] = []
    prior_summary = ""
    for message in messages:
        role = message.get("role")
        if role == "system" and str(message.get("content") or "").startswith(_SUMMARY_PREFIX):
            prior_summary, _ = truncate_text(
                str(message["content"])[len(_SUMMARY_PREFIX) :].strip(),
                limit=max(300, limit // 4),
            )
            continue
        if role == "user" and message.get("content"):
            goal, _ = truncate_text(_without_reference_context(str(message["content"])), limit=800)
            goals.append(goal)
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            name = str(function.get("name") or "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            path = arguments.get("path")
            if isinstance(path, str):
                actions.append(f"{name}: {path}")
            elif name:
                actions.append(name)
            if name in {"write_file", "edit_file"} and isinstance(path, str):
                changed_paths.add(path)
        if role == "tool":
            try:
                payload = json.loads(str(message.get("content") or "{}"))
            except (TypeError, json.JSONDecodeError):
                payload = {}
            summary = str(payload.get("summary") or "")
            error = payload.get("error")
            if error:
                failures.append(str(error.get("message") or summary or error))
            data = payload.get("data") or {}
            if isinstance(data, dict):
                for step in data.get("commands") or []:
                    if not isinstance(step, dict) or step.get("exit_code") is None:
                        continue
                    command, _ = truncate_text(str(step.get("command") or ""), limit=300)
                    commands.append(f"exit {step['exit_code']}: {command}")
        elif role == "assistant" and message.get("content"):
            conclusion, _ = truncate_text(str(message["content"]), limit=600)
            conclusions.append(conclusion)
    sections = ["Current goals and constraints:", *[f"- {item}" for item in goals[-3:]]]
    if changed_paths:
        sections.extend(["Changed files:", *[f"- {path}" for path in sorted(changed_paths)]])
    if actions:
        sections.extend(["Recent tool activity:", *[f"- {item}" for item in actions[-6:]]])
    if commands:
        sections.extend(["Recent verification and commands:", *[f"- {item}" for item in commands[-4:]]])
    if failures:
        sections.extend(["Recent failures:", *[f"- {item}" for item in failures[-3:]]])
    if conclusions:
        sections.extend(["Recent progress:", *[f"- {item}" for item in conclusions[-2:]]])
    if prior_summary:
        sections.extend(["Earlier checkpoint:", prior_summary])
    summary, _ = truncate_text(_SUMMARY_PREFIX + "\n".join(sections), limit=limit)
    return summary


def compact_messages(messages: list[dict[str, Any]], *, max_chars: int = 80_000) -> list[dict[str, Any]]:
    """Compact old complete conversation groups without splitting tool calls/results."""
    size = sum(len(str(message)) for message in messages)
    if size <= max_chars or len(messages) < 8:
        return messages
    prefix_end = 0
    while (
        prefix_end < len(messages)
        and messages[prefix_end].get("role") == "system"
        and not str(messages[prefix_end].get("content") or "").startswith(_SUMMARY_PREFIX)
    ):
        prefix_end += 1
    keep_from = max(prefix_end, len(messages) // 2)
    if (
        prefix_end < len(messages)
        and str(messages[prefix_end].get("content") or "").startswith(_SUMMARY_PREFIX)
    ):
        keep_from = max(keep_from, prefix_end + 2)
    while keep_from < len(messages) and messages[keep_from].get("role") == "tool":
        keep_from += 1
    removed = messages[prefix_end:keep_from]
    if not removed:
        return messages
    summary = {"role": "system", "content": _summarize_removed(removed)}
    return [*messages[:prefix_end], summary, *messages[keep_from:]]
