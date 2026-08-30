from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from .permissions import ApprovalPrompt


class ReviewerModel(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]): ...


class ReviewDecision(str, Enum):
    ALLOW = "allow"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class ReviewOutcome:
    decision: ReviewDecision
    reason: str
    risk: str


REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_permission_review",
        "description": "Return a structured decision for one shell approval request.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["allow", "escalate"]},
                "reason": {"type": "string"},
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["decision", "reason", "risk"],
        },
    },
}


REVIEW_SYSTEM_PROMPT = """You are MiniCodex's independent permission reviewer.
You cannot execute commands, use tools other than submit_permission_review, or expand permissions.
Approve routine local development work when the stated command and context support it.
Escalate when intent is unclear, the action may affect external systems or credentials, or the evidence is insufficient.
Never assume that a purpose label makes a command safe. Return exactly one submit_permission_review tool call."""


class ModelPermissionReviewer:
    def __init__(self, model: ReviewerModel) -> None:
        self.model = model

    def review(self, prompt: ApprovalPrompt) -> ReviewOutcome:
        request = {
            "command": prompt.details.get("command"),
            "purpose": prompt.details.get("purpose"),
            "workspace": prompt.details.get("workspace"),
            "timeout_sec": prompt.details.get("timeout_sec"),
            "signals": prompt.details.get("signals", []),
            "policy_reason": prompt.reason,
            "analysis": prompt.details.get("analysis", {}),
        }
        try:
            reply = self.model.complete(
                [
                    {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
                ],
                [REVIEW_TOOL],
            )
            calls = [call for call in reply.tool_calls if call.name == "submit_permission_review"]
            if len(calls) != 1:
                return ReviewOutcome(ReviewDecision.ESCALATE, "reviewer did not return one structured decision", "medium")
            arguments = calls[0].arguments
            decision = ReviewDecision(arguments.get("decision"))
            reason = arguments.get("reason")
            risk = arguments.get("risk")
            if not isinstance(reason, str) or not reason.strip() or risk not in {"low", "medium", "high"}:
                raise ValueError("reviewer returned invalid fields")
            return ReviewOutcome(decision, reason.strip(), risk)
        except Exception as exc:
            return ReviewOutcome(ReviewDecision.ESCALATE, f"reviewer unavailable or invalid: {exc}", "medium")
