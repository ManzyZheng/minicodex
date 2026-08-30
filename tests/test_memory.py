from __future__ import annotations

import json
from pathlib import Path

from minicodex.memory import MemoryExtractor, MemoryService, MemoryStore
from minicodex.models import ModelReply
from minicodex.persistence import ApplicationPaths


class ReplyModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0
        self.received_tools = None

    def complete(self, messages, tools) -> ModelReply:
        self.calls += 1
        self.received_tools = tools
        return ModelReply(content=self.content)


def candidate(**changes) -> dict:
    payload = {
        "scope": "project",
        "kind": "preference",
        "title": "版本号限制",
        "content": "除非明确要求，否则不要修改版本号。",
        "evidence": "这个项目以后不要自动修改版本号",
        "scope_evidence": "这个项目以后",
        "durability": "explicit",
        "confidence": 0.98,
    }
    payload.update(changes)
    return payload


def test_memory_store_separates_scopes_and_forgets_logically(tmp_path: Path) -> None:
    store = MemoryStore(ApplicationPaths(tmp_path / "data"))
    global_item = store.remember(
        scope="global", kind="preference", title="语言", content="默认使用中文回答。", source="manual"
    )
    project_item = store.remember(
        scope="project",
        project_id="proj_demo",
        kind="decision",
        title="测试",
        content="使用 pytest。",
        source="manual",
    )

    assert [item.id for item in store.list(scope="global")] == [global_item.id]
    assert [item.id for item in store.list(scope="project", project_id="proj_demo")] == [project_item.id]
    assert store.forget(project_item.id, project_id="proj_demo") is True
    assert store.list(scope="project", project_id="proj_demo") == []
    assert store.get(project_item.id, project_id="proj_demo", include_deleted=True).status == "deleted"


def test_extractor_accepts_empty_candidates_and_uses_no_tools(tmp_path: Path) -> None:
    model = ReplyModel('{"candidates": []}')
    extractor = MemoryExtractor(model)

    assert extractor.extract(project_name="Demo", recent_user_messages=["修复 Bug"], existing_index=[]) == []
    assert model.calls == 1
    assert model.received_tools == []


def test_service_auto_saves_valid_project_and_global_candidates(tmp_path: Path) -> None:
    payload = {
        "candidates": [
            candidate(),
            candidate(
                scope="global",
                title="回答语言",
                content="所有项目默认使用中文回答。",
                evidence="以后所有项目都使用中文回答",
                scope_evidence="以后所有项目",
            ),
        ]
    }
    model = ReplyModel(json.dumps(payload, ensure_ascii=False))
    store = MemoryStore(ApplicationPaths(tmp_path / "data"))
    service = MemoryService(store, MemoryExtractor(model))

    result = service.process_completed_prompt(
        project_id="proj_demo",
        project_name="Demo",
        session_id="sess_demo",
        prompt_index=3,
        recent_user_messages=["这个项目以后不要自动修改版本号；以后所有项目都使用中文回答。"],
    )

    assert len(result.created) == 2
    assert len(store.list(scope="project", project_id="proj_demo")) == 1
    assert len(store.list(scope="global")) == 1


def test_service_rejects_missing_evidence_secret_and_weak_global_scope(tmp_path: Path) -> None:
    payload = {
        "candidates": [
            candidate(evidence="用户没有说过这句话"),
            candidate(content="API Key 是 sk-super-secret", evidence="这个项目以后不要自动修改版本号"),
        ]
    }
    store = MemoryStore(ApplicationPaths(tmp_path / "data"))
    service = MemoryService(store, MemoryExtractor(ReplyModel(json.dumps(payload, ensure_ascii=False))))

    result = service.process_completed_prompt(
        project_id="proj_demo",
        project_name="Demo",
        session_id="sess_demo",
        prompt_index=1,
        recent_user_messages=["这个项目以后不要自动修改版本号"],
    )

    assert result.created == []
    assert len(result.rejected) == 2
    assert store.list(scope="global") == []
    assert store.list(scope="project", project_id="proj_demo") == []

    weak_global = MemoryService(
        store,
        MemoryExtractor(
            ReplyModel(
                json.dumps(
                    {
                        "candidates": [
                            candidate(
                                scope="global",
                                title="测试工具",
                                content="所有项目使用 pytest。",
                                evidence="这个项目以后不要自动修改版本号",
                                scope_evidence="这个项目以后",
                            )
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        ),
    ).process_completed_prompt(
        project_id="proj_demo",
        project_name="Demo",
        session_id="sess_demo",
        prompt_index=2,
        recent_user_messages=["这个项目以后不要自动修改版本号"],
    )
    assert len(weak_global.rejected) == 1
    assert store.list(scope="global") == []


def test_duplicate_candidate_does_not_accumulate(tmp_path: Path) -> None:
    payload = json.dumps({"candidates": [candidate()]}, ensure_ascii=False)
    store = MemoryStore(ApplicationPaths(tmp_path / "data"))
    service = MemoryService(store, MemoryExtractor(ReplyModel(payload)))
    kwargs = {
        "project_id": "proj_demo",
        "project_name": "Demo",
        "session_id": "sess_demo",
        "recent_user_messages": ["这个项目以后不要自动修改版本号"],
    }

    first = service.process_completed_prompt(prompt_index=1, **kwargs)
    second = service.process_completed_prompt(prompt_index=2, **kwargs)

    assert len(first.created) == 1
    assert second.created == []
    assert len(second.duplicates) == 1
    assert len(store.list(scope="project", project_id="proj_demo")) == 1


def test_malformed_extractor_output_is_non_fatal(tmp_path: Path) -> None:
    store = MemoryStore(ApplicationPaths(tmp_path / "data"))
    service = MemoryService(store, MemoryExtractor(ReplyModel("not-json")))

    result = service.process_completed_prompt(
        project_id="proj_demo",
        project_name="Demo",
        session_id="sess_demo",
        prompt_index=1,
        recent_user_messages=["以后都这样"],
    )

    assert result.created == []
    assert result.error is not None
