from __future__ import annotations

import json
from typing import Any, Protocol

from ..models import ModelReply
from .models import MEMORY_KINDS, MEMORY_SCOPES, MemoryCandidate


class ExtractionModel(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...


class MemoryExtractionError(ValueError):
    pass


EXTRACTION_PROMPT = """You are MiniCodex's conservative long-term memory extractor.
Inspect only recent user messages. Default to an empty candidates array and extract at most two items.
Keep only explicit durable preferences, reusable corrections, project rules, decisions, or long-lived references.
Never keep one-off instructions, current implementation details, workspace-derivable facts, test results, code, logs, secrets, or assistant inferences.
Global requires explicit cross-project language or a stable personal preference independent of this project.
Every evidence and scope_evidence must be a continuous exact substring of recent user text.
Return JSON only using this exact schema:
{"candidates":[{"scope":"global|project","kind":"preference|decision|reference","title":"short title","content":"durable rule","evidence":"exact user substring","scope_evidence":"exact substring proving scope","durability":"explicit","confidence":0.0}]}
Use scope "global" only when scope_evidence explicitly applies across projects or states a stable personal preference.
Use scope "project" for an explicit durable rule tied to the named project.
Usually return {"candidates":[]}.
"""


class MemoryExtractor:
    def __init__(self, model: ExtractionModel) -> None:
        self.model = model

    def extract(self, *, project_name: str, recent_user_messages: list[str], existing_index: list[dict[str, str]]) -> list[MemoryCandidate]:
        payload = {"project_name": project_name, "recent_user_messages": recent_user_messages[-2:], "existing_memory_index": existing_index}
        reply = self.model.complete(
            [{"role": "system", "content": EXTRACTION_PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            [],
        )
        try:
            raw_candidates = json.loads(reply.content or "")["candidates"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise MemoryExtractionError("memory extractor returned invalid JSON") from exc
        if not isinstance(raw_candidates, list):
            raise MemoryExtractionError("memory candidates must be a list")
        candidates: list[MemoryCandidate] = []
        malformed = False
        for raw in raw_candidates[:2]:
            if not isinstance(raw, dict):
                malformed = True
                continue
            try:
                item = MemoryCandidate(
                    str(raw["scope"]), str(raw["kind"]), str(raw["title"]), str(raw["content"]),
                    str(raw["evidence"]), str(raw["scope_evidence"]), str(raw["durability"]),
                    float(raw.get("confidence", 0.0)),
                )
            except (KeyError, TypeError, ValueError):
                malformed = True
                continue
            if item.scope not in MEMORY_SCOPES or item.kind not in MEMORY_KINDS:
                malformed = True
                continue
            candidates.append(item)
        if raw_candidates and malformed and not candidates:
            raise MemoryExtractionError("memory extractor returned no valid candidates")
        return candidates
