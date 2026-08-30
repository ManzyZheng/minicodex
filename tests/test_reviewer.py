from __future__ import annotations

from minicodex.models import ModelReply, ToolCall
from minicodex.permissions import ApprovalPrompt
from minicodex.reviewer import ModelPermissionReviewer, ReviewDecision


class ReplyModel:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.messages_seen: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.messages_seen.append(messages)
        self.tools_seen.append(tools)
        return self.replies.pop(0)


def prompt(command: str = "npm install") -> ApprovalPrompt:
    return ApprovalPrompt(
        kind="command",
        tool="run_shell",
        summary="review shell command",
        reason="command needs reviewer judgment",
        risk="medium",
        rule_id="auto_act.reviewer",
        details={
            "command": command,
            "purpose": "build",
            "timeout_sec": 30,
            "workspace": "D:/project",
            "signals": ["package_install"],
            "analysis": {
                "operations": ["package.install"],
                "fully_analyzed": True,
                "targets": [],
            },
        },
    )


def test_model_reviewer_returns_structured_allow_decision() -> None:
    model = ReplyModel([
        ModelReply(tool_calls=[ToolCall("review", "submit_permission_review", {
            "decision": "allow",
            "risk": "medium",
            "reason": "安装当前项目声明的依赖。",
        })])
    ])

    outcome = ModelPermissionReviewer(model).review(prompt())

    assert outcome.decision is ReviewDecision.ALLOW
    assert outcome.reason == "安装当前项目声明的依赖。"
    assert model.tools_seen[0][0]["function"]["name"] == "submit_permission_review"
    assert "You cannot execute commands" in model.messages_seen[0][0]["content"]
    assert '"operations": ["package.install"]' in model.messages_seen[0][1]["content"]


def test_model_reviewer_escalates_malformed_or_failed_reviews() -> None:
    malformed = ModelPermissionReviewer(ReplyModel([ModelReply(content="looks fine")])).review(prompt())

    class FailingModel:
        def complete(self, _messages, _tools):
            raise RuntimeError("review service unavailable")

    failed = ModelPermissionReviewer(FailingModel()).review(prompt())

    assert malformed.decision is ReviewDecision.ESCALATE
    assert failed.decision is ReviewDecision.ESCALATE
    assert "review service unavailable" in failed.reason
