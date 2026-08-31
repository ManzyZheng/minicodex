from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minicodex.agent import AgentSession
from minicodex.memory import MemoryExtractor, MemoryService, MemoryStore
from minicodex.models import ModelReply
from minicodex.persistence import ApplicationPaths
from minicodex.project_sessions import SessionRecord, SessionRepository
from minicodex.projects import ProjectRecord, ProjectRegistry
from minicodex.tools import ToolRuntime
from minicodex.web.app import create_app
from minicodex.web.approval import ApprovalGate
from minicodex.web.events import EventBus
from minicodex.web.manager import WebWorkspaceManager
from minicodex.web.session import SessionBusyError, WebSession


class MemoryModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools) -> ModelReply:
        self.calls += 1
        return ModelReply(content='{"candidates": []}')


class ReplyModel:
    def complete(self, messages, tools) -> ModelReply:
        return ModelReply(content="完成")


class BlockingModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, tools) -> ModelReply:
        self.started.set()
        assert self.release.wait(1.0)
        return ModelReply(content="完成")


def make_manager(tmp_path: Path, *, task_model=None, with_project: bool = True):
    paths = ApplicationPaths(tmp_path / "data")
    registry = ProjectRegistry(paths)
    repository = SessionRepository(paths)
    store = MemoryStore(paths)
    memory_model = MemoryModel()
    events = EventBus()
    model = task_model or ReplyModel()

    def factory(
        project: ProjectRecord,
        record: SessionRecord,
        state: dict,
        on_complete,
    ) -> WebSession:
        approvals = ApprovalGate(events, wait_timeout=0.2)
        runtime = ToolRuntime(project.workspace, approver=approvals.request)
        agent = AgentSession(
            model,
            runtime,
            on_event=events.publish,
            memory_prompt_provider=lambda: store.prompt_context(project.id),
        )
        if state:
            agent.restore_state(state)
        return WebSession(
            agent,
            events,
            approvals,
            workspace=project.workspace,
            model_name="demo",
            max_turns_per_prompt=20,
            on_prompt_complete=on_complete,
        )

    manager = WebWorkspaceManager(
        paths=paths,
        registry=registry,
        sessions=repository,
        memories=store,
        memory_service=MemoryService(store, MemoryExtractor(memory_model)),
        session_factory=factory,
        events=events,
        initial_workspace=(tmp_path / "workspace") if with_project else None,
    )
    return manager, memory_model


def test_manager_starts_on_project_home_and_activates_the_first_added_project(tmp_path: Path) -> None:
    manager, _memory_model = make_manager(tmp_path, with_project=False)

    initial = manager.snapshot()
    assert initial["status"] == "NO_PROJECT"
    assert initial["active_project_id"] is None
    assert initial["active_session_id"] is None
    assert initial["projects"] == []

    workspace = tmp_path / "booknest"
    workspace.mkdir()
    project = manager.register_project(str(workspace), name="BookNest")

    assert manager.active_project_id == project.id
    assert manager.active_session_id is not None
    assert manager.snapshot()["workspace"] == str(workspace.resolve())


def test_project_home_api_rejects_agent_actions_until_a_project_is_selected(tmp_path: Path) -> None:
    manager, _memory_model = make_manager(tmp_path, with_project=False)
    client = TestClient(create_app(manager, access_token="token"), base_url="http://127.0.0.1")
    api = lambda path: f"{path}{'&' if '?' in path else '?'}token=token"

    snapshot = client.get(api("/api/session"))
    prompt = client.post(api("/api/prompts"), json={"text": "创建项目"})

    assert snapshot.status_code == 200
    assert snapshot.json()["status"] == "NO_PROJECT"
    assert prompt.status_code == 409
    assert "project" in prompt.json()["detail"].lower()


def test_project_home_can_manage_global_memory_without_an_active_session(tmp_path: Path) -> None:
    manager, _memory_model = make_manager(tmp_path, with_project=False)

    item = manager.remember(scope="global", kind="preference", title="语言", content="默认中文")

    assert manager.list_memories(scope="global")[0].id == item.id
    assert item.source_session_id is None
    assert item.source_prompt_index == 0


def test_manager_creates_switches_and_restores_sessions(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir()
    manager, memory_model = make_manager(tmp_path)
    first = manager.active_session_id
    manager.submit_prompt("第一条消息")
    assert manager.wait_until_idle(1.0)
    second = manager.create_session(manager.active_project_id, title="第二个会话")

    assert second.id != first
    assert manager.active_session_id == second.id
    manager.switch_session(manager.active_project_id, first)

    snapshot = manager.snapshot()
    assert snapshot["active_session_id"] == first
    assert snapshot["history"][-2:] == [
        {"role": "user", "content": "第一条消息"},
        {"role": "assistant", "content": "完成"},
    ]
    assert memory_model.calls == 1


def test_manager_rejects_session_switch_while_prompt_runs(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir()
    model = BlockingModel()
    manager, _memory_model = make_manager(tmp_path, task_model=model)
    other = manager.create_session(manager.active_project_id, title="other")
    first = manager.create_session(manager.active_project_id, title="running")
    assert manager.active_session_id == first.id
    manager.submit_prompt("long task")
    assert model.started.wait(0.5)

    with pytest.raises(SessionBusyError):
        manager.switch_session(manager.active_project_id, other.id)

    model.release.set()
    assert manager.wait_until_idle(1.0)


def test_manager_memory_crud_and_prompt_extraction_index_are_persisted(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir()
    manager, memory_model = make_manager(tmp_path)
    item = manager.remember(scope="global", kind="preference", title="语言", content="默认中文")
    manager.submit_prompt("普通任务")
    assert manager.wait_until_idle(1.0)
    manager._persist_completed_prompt("普通任务", None)

    assert memory_model.calls == 1
    assert manager.list_memories(scope="global")[0].id == item.id
    assert manager.forget_memory(item.id) is True
    assert manager.list_memories(scope="global") == []


def test_project_session_and_memory_api_routes(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir()
    manager, _memory_model = make_manager(tmp_path)
    client = TestClient(create_app(manager, access_token="token"), base_url="http://127.0.0.1")
    api = lambda path: f"{path}{'&' if '?' in path else '?'}token=token"

    projects = client.get(api("/api/projects"))
    assert projects.status_code == 200
    project_id = projects.json()["active_project_id"]
    created = client.post(api(f"/api/projects/{project_id}/sessions"), json={"title": "新任务"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert client.post(api(f"/api/projects/{project_id}/sessions/{session_id}/activate"), json={}).status_code == 200

    memory = client.post(
        api("/api/memories"),
        json={"scope": "global", "kind": "preference", "title": "语言", "content": "默认中文"},
    )
    assert memory.status_code == 201
    memory_id = memory.json()["id"]
    assert client.get(api("/api/memories?scope=global")).json()[0]["id"] == memory_id
    assert client.delete(api(f"/api/memories/{memory_id}")).status_code == 200
