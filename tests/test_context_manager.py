from __future__ import annotations

import json

from minicodex.context import ContextManager, ContextPolicy, estimate_tokens, message_chars


def _tool_pair(call_id: str, name: str, arguments: dict, content: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(
                {
                    "ok": True,
                    "tool": name,
                    "call_id": call_id,
                    "summary": f"completed {name}",
                    "data": {"path": arguments.get("path"), "content": content},
                    "error": None,
                    "meta": {"truncated": False},
                }
            ),
        },
    ]


def test_auto_checkpoint_uses_token_budget_and_restores_current_files() -> None:
    manager = ContextManager(ContextPolicy(
        budget_tokens=2_000,
        stale_tokens=2_500,
        auto_compact_tokens=4_000,
        target_tokens=3_000,
    ))
    messages = [{"role": "system", "content": "system"}]
    for index in range(10):
        messages.extend(
            [
                {"role": "user", "content": f"request-{index}:" + "U" * 7_000},
                {"role": "assistant", "content": f"answer-{index}:" + "A" * 7_000},
            ]
        )

    result = manager.prepare(
        messages,
        checkpoint_factory=lambda: [{"path": "src/app.py", "content": "CURRENT_DISK_CONTENT"}],
    )

    assert result.before_tokens > 4_000
    assert result.after_chars < result.before_chars
    assert result.after_tokens <= 3_000
    assert "auto_checkpoint" in result.stages
    assert "CURRENT_DISK_CONTENT" in json.dumps(result.messages, ensure_ascii=False)
    summaries = [
        message
        for message in result.messages
        if message.get("role") == "system"
        and str(message.get("content") or "").startswith("Earlier context summary:")
    ]
    assert len(summaries) == 1


def test_stale_snip_keeps_latest_duplicate_read_and_the_tool_call_skeleton() -> None:
    policy = ContextPolicy(
        budget_tokens=60,
        stale_tokens=85,
        auto_compact_tokens=5_000,
        target_tokens=50,
        recent_tool_turns=1,
        recent_tool_chars=10_000,
        min_stage_savings=1,
    )
    manager = ContextManager(policy)
    messages = [{"role": "system", "content": "system"}]
    messages += _tool_pair("old", "read_file", {"path": "app.py"}, "OLD_BODY" * 30)
    messages += _tool_pair("new", "read_file", {"path": "app.py"}, "LATEST_BODY" * 30)

    result = manager.prepare(messages)
    encoded = json.dumps(result.messages, ensure_ascii=False)

    assert "stale_snip" in result.stages
    assert "OLD_BODY" not in encoded
    assert "LATEST_BODY" in encoded
    assert '"id": "old"' in encoded
    assert "旧工具结果已裁剪" in encoded


def test_budget_never_rewrites_historical_write_arguments() -> None:
    policy = ContextPolicy(
        budget_tokens=120,
        stale_tokens=5_000,
        auto_compact_tokens=10_000,
        target_tokens=100,
        recent_tool_turns=2,
        recent_tool_chars=10_000,
        min_stage_savings=1,
    )
    manager = ContextManager(policy)
    messages = [{"role": "system", "content": "system"}]
    for index in range(5):
        messages += _tool_pair(
            str(index),
            "write_file",
            {"path": f"file-{index}.py", "content": f"SOURCE_{index}_" * 80},
            f"RESULT_{index}_" * 5,
        )

    result = manager.prepare(messages)
    calls = {
        call["id"]: json.loads(call["function"]["arguments"])
        for message in result.messages
        for call in message.get("tool_calls") or []
    }

    assert "tool_args" not in result.stages
    for index in range(5):
        assert calls[str(index)]["path"] == f"file-{index}.py"
        assert f"SOURCE_{index}_" in calls[str(index)]["content"]
    for call_id in calls:
        assert any(message.get("tool_call_id") == call_id for message in result.messages)


def test_auto_checkpoint_caps_recent_tool_groups_by_turns_and_characters() -> None:
    policy = ContextPolicy(
        budget_tokens=25,
        stale_tokens=50,
        auto_compact_tokens=125,
        target_tokens=55,
        recent_tool_turns=2,
        recent_tool_chars=1_200,
        summary_limit=80,
        min_stage_savings=1,
    )
    manager = ContextManager(policy)
    messages = [{"role": "system", "content": "system"}]
    messages.extend(
        [
            {"role": "user", "content": "old request " + "x" * 500},
            {"role": "assistant", "content": "old answer " + "y" * 500},
        ]
    )
    for index in range(4):
        repeats = 10 if index == 3 else 300
        messages += _tool_pair(
            str(index),
            "write_file",
            {"path": f"file-{index}.py", "content": f"SOURCE_{index}_" * repeats},
            "write complete",
        )
    messages.append({"role": "user", "content": "latest request"})

    result = manager.prepare(messages)
    encoded = json.dumps(result.messages, ensure_ascii=False)

    assert "auto_checkpoint" in result.stages
    assert '"id": "3"' in encoded
    assert '"tool_call_id": "3"' in encoded
    assert '"id": "2"' not in encoded
    assert "SOURCE_3_" in encoded


def test_repeated_prepare_never_stacks_summaries_or_grows_context() -> None:
    policy = ContextPolicy(
        budget_tokens=50,
        stale_tokens=75,
        auto_compact_tokens=125,
        target_tokens=70,
        summary_limit=120,
        min_stage_savings=1,
    )
    manager = ContextManager(policy)
    messages = [{"role": "system", "content": "system"}]
    for index in range(8):
        messages.extend(
            [
                {"role": "user", "content": f"request-{index}:" + "x" * 100},
                {"role": "assistant", "content": f"answer-{index}:" + "y" * 100},
            ]
        )

    first = manager.prepare(messages)
    second = manager.prepare(first.messages)

    assert first.after_chars < first.before_chars
    assert second.after_chars <= second.before_chars
    assert sum(
        str(message.get("content") or "").startswith("Earlier context summary:")
        for message in second.messages
    ) == 1
    assert second.after_chars == message_chars(second.messages)
    assert second.after_tokens == estimate_tokens(second.messages)
