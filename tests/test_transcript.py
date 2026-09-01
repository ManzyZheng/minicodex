from __future__ import annotations

import json
from pathlib import Path

from minicodex.persistence import ApplicationPaths
from minicodex.project_sessions import SessionRepository


def make_session(tmp_path: Path):
    paths = ApplicationPaths(tmp_path / "data")
    repository = SessionRepository(paths)
    project_id = "project-test"
    record = repository.create(project_id, title="Transcript test")
    return paths, repository, project_id, record.id


def test_transcript_is_append_only_sanitized_and_keeps_diff_in_artifact(tmp_path: Path) -> None:
    paths, repository, project_id, session_id = make_session(tmp_path)

    repository.append_transcript_event(
        project_id,
        session_id,
        "user_prompt",
        {"text": "修改 app.py", "prompt_index": 1, "secret": "must-not-be-stored"},
    )
    repository.append_transcript_event(
        project_id,
        session_id,
        "tool_summary",
        {
            "text": "已修改 app.py",
            "tool": "edit_file",
            "ok": True,
            "turn": 2,
            "detail": {"before": "old body", "after": "new body"},
        },
    )
    repository.append_transcript_event(
        project_id,
        session_id,
        "file_changed",
        {
            "path": "app.py",
            "prompt_index": 1,
            "additions": 1,
            "deletions": 1,
            "diff": "--- a/app.py\n+++ b/app.py\n-old\n+new\n",
            "before": "old body",
            "after": "new body",
        },
    )
    repository.append_transcript_event(
        project_id,
        session_id,
        "model_reasoning",
        {"content": "private chain of thought", "prompt_index": 1},
    )

    events = repository.load_transcript(project_id, session_id)
    raw = (paths.session_root(project_id, session_id) / "transcript.jsonl").read_text(encoding="utf-8")

    assert [event["type"] for event in events] == ["user_prompt", "tool_summary", "file_changed"]
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert "must-not-be-stored" not in raw
    assert "private chain of thought" not in raw
    assert "old body" not in raw
    assert "new body" not in raw
    assert events[1]["payload"] == {"text": "已修改 app.py", "tool": "edit_file", "ok": True, "turn": 2}
    diff_ref = events[2]["payload"]["diff_ref"]
    assert (paths.session_root(project_id, session_id) / diff_ref).read_text(encoding="utf-8").endswith("+new\n")


def test_transcript_paginates_and_projects_history_and_file_changes(tmp_path: Path) -> None:
    _paths, repository, project_id, session_id = make_session(tmp_path)
    repository.append_transcript_event(project_id, session_id, "user_prompt", {"text": "第一轮", "prompt_index": 1})
    repository.append_transcript_event(
        project_id,
        session_id,
        "final_answer",
        {"text": "第一轮完成", "prompt_index": 1, "turns": 3, "verification_status": "VERIFIED"},
    )
    repository.append_transcript_event(project_id, session_id, "user_prompt", {"text": "第二轮", "prompt_index": 2})
    repository.append_transcript_event(
        project_id,
        session_id,
        "file_changed",
        {"path": "app.py", "prompt_index": 2, "additions": 2, "deletions": 0, "diff": "+value = 2\n"},
    )

    latest = repository.load_transcript(project_id, session_id, limit=2)
    earlier = repository.load_transcript(project_id, session_id, before_seq=latest[0]["seq"], limit=10)

    assert [event["type"] for event in latest] == ["user_prompt", "file_changed"]
    assert [event["type"] for event in earlier] == ["user_prompt", "final_answer"]
    assert repository.history_snapshot(project_id, session_id) == [
        {"role": "user", "content": "第一轮", "prompt_index": 1},
        {
            "role": "assistant",
            "content": "第一轮完成",
            "prompt_index": 1,
            "turns": 3,
            "verification_status": "VERIFIED",
        },
        {"role": "user", "content": "第二轮", "prompt_index": 2},
    ]
    changes = repository.transcript_file_changes(project_id, session_id)
    assert changes[0]["path"] == "app.py"
    assert changes[0]["diff"] == "+value = 2\n"


def test_context_compaction_transcript_keeps_token_metrics_not_raw_character_counts(tmp_path: Path) -> None:
    _paths, repository, project_id, session_id = make_session(tmp_path)
    repository.append_transcript_event(
        project_id,
        session_id,
        "context_compacted",
        {
            "before_messages": 196,
            "after_messages": 120,
            "before_chars": 274_376,
            "after_chars": 120_000,
            "before_tokens": 76_911,
            "after_tokens": 33_700,
            "stages": ["stale_snip", "rolling_summary"],
            "compaction_count": 2,
            "turn": 13,
            "prompt_index": 4,
        },
    )

    payload = repository.load_transcript(project_id, session_id)[0]["payload"]

    assert payload == {
        "before_messages": 196,
        "after_messages": 120,
        "before_tokens": 76_911,
        "after_tokens": 33_700,
        "stages": ["stale_snip", "rolling_summary"],
        "compaction_count": 2,
        "turn": 13,
        "prompt_index": 4,
    }


def test_legacy_trace_migration_preserves_history_even_when_state_was_compacted(tmp_path: Path) -> None:
    paths, repository, project_id, session_id = make_session(tmp_path)
    root = paths.session_root(project_id, session_id)
    trace = root / "trace.jsonl"
    records = [
        {"timestamp": "2026-08-31T00:00:00+00:00", "event": "prompt_start", "payload": {"prompt": "原始问题", "prompt_index": 1}},
        {
            "timestamp": "2026-08-31T00:00:01+00:00",
            "event": "final",
            "payload": {"text": "原始回答", "turns": 4, "verification_status": "VERIFIED"},
        },
    ]
    trace.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
    repository.save_state(
        project_id,
        session_id,
        {"schema_version": 1, "messages": [{"role": "assistant", "content": "压缩摘要"}], "prompt_count": 1},
    )

    assert repository.ensure_transcript(project_id, session_id) is True
    assert repository.history_snapshot(project_id, session_id) == [
        {"role": "user", "content": "原始问题", "prompt_index": 1},
        {
            "role": "assistant",
            "content": "原始回答",
            "prompt_index": 1,
            "turns": 4,
            "verification_status": "VERIFIED",
        },
    ]
    assert repository.ensure_transcript(project_id, session_id) is False
