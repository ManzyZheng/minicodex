from __future__ import annotations

from pathlib import Path

import pytest

from minicodex.persistence import ApplicationPaths
from minicodex.project_sessions import SessionRepository
from minicodex.projects import ProjectRegistry


def test_project_registry_deduplicates_resolved_workspace_and_never_deletes_it(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(ApplicationPaths(data_root))

    first = registry.register(workspace, name="Demo")
    second = registry.register(workspace / ".", name="Renamed")

    assert second.id == first.id
    assert second.name == "Renamed"
    assert len(registry.list()) == 1
    assert registry.remove(first.id) is True
    assert workspace.is_dir()
    assert registry.list() == []


def test_project_registry_rejects_missing_workspace(tmp_path: Path) -> None:
    registry = ProjectRegistry(ApplicationPaths(tmp_path / "data"))

    with pytest.raises(ValueError, match="workspace"):
        registry.register(tmp_path / "missing")


def test_session_repository_creates_lists_and_round_trips_state(tmp_path: Path) -> None:
    paths = ApplicationPaths(tmp_path / "data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = ProjectRegistry(paths).register(workspace, name="BookNest")
    sessions = SessionRepository(paths)

    first = sessions.create(project.id, title="新会话")
    second = sessions.create(project.id, title="评分功能")
    sessions.save_state(
        project.id,
        second.id,
        {"messages": [{"role": "user", "content": "添加评分"}], "prompt_count": 1},
    )
    sessions.update(second.id, project.id, title="添加评分", status="completed", verification="VERIFIED")

    listed = sessions.list(project.id)
    assert [item.id for item in listed] == [second.id, first.id]
    assert listed[0].title == "添加评分"
    assert listed[0].verification == "VERIFIED"
    assert sessions.load_state(project.id, second.id)["prompt_count"] == 1


def test_session_repository_rejects_cross_project_session_lookup(tmp_path: Path) -> None:
    paths = ApplicationPaths(tmp_path / "data")
    work_a = tmp_path / "a"
    work_b = tmp_path / "b"
    work_a.mkdir()
    work_b.mkdir()
    projects = ProjectRegistry(paths)
    project_a = projects.register(work_a)
    project_b = projects.register(work_b)
    sessions = SessionRepository(paths)
    session = sessions.create(project_a.id)

    assert sessions.get(project_b.id, session.id) is None
    with pytest.raises(KeyError):
        sessions.save_state(project_b.id, session.id, {"messages": []})
