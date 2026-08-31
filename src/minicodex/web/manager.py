from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from ..agent import AgentOutcome
from ..memory import MemoryItem, MemoryService, MemoryStore
from ..persistence import ApplicationPaths
from ..project_sessions import SessionRecord, SessionRepository
from ..projects import ProjectRecord, ProjectRegistry
from .events import EventBus
from .session import SessionBusyError, WebSession


SessionFactory = Callable[
    [ProjectRecord, SessionRecord, dict[str, Any], Callable[[WebSession, str, AgentOutcome], None]],
    WebSession,
]


class WebWorkspaceManager:
    def __init__(
        self,
        *,
        paths: ApplicationPaths,
        registry: ProjectRegistry,
        sessions: SessionRepository,
        memories: MemoryStore,
        memory_service: MemoryService,
        session_factory: SessionFactory,
        events: EventBus,
        initial_workspace: str | Path | None = None,
    ) -> None:
        self.paths = paths
        self.registry = registry
        self.sessions = sessions
        self.memories = memories
        self.memory_service = memory_service
        self.session_factory = session_factory
        self.events = events
        self._lock = threading.RLock()
        self._active: WebSession | None = None
        self.active_project_id: str | None = None
        self.active_session_id: str | None = None
        if initial_workspace is not None:
            project = self.registry.register(initial_workspace)
            records = self.sessions.list(project.id)
            record = next((item for item in records if item.id == project.last_session_id), None)
            if record is None:
                record = records[0] if records else self.sessions.create(project.id)
            self._activate(project, record, initial=True)

    @property
    def has_active_session(self) -> bool:
        return self._active is not None

    @property
    def active(self) -> WebSession:
        if self._active is None:
            raise SessionBusyError("select or add a project before running the Agent")
        return self._active

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.active, name)

    def _ensure_idle(self) -> None:
        if self._active is None:
            return
        if self.active.snapshot()["status"] != "IDLE":
            raise SessionBusyError("stop the running Agent before changing project or session")

    def _activate(self, project: ProjectRecord, record: SessionRecord, *, initial: bool = False) -> None:
        state = self.sessions.load_state(project.id, record.id)
        if self._active is not None:
            self._active.close()
        self.active_project_id = project.id
        self.active_session_id = record.id
        if not initial:
            self.events.publish("session_reset", {"project_id": project.id, "session_id": record.id})
        self._active = self.session_factory(project, record, state, self._on_prompt_complete)
        self._active.restore_presentation_state(
            file_changes=state.get("file_changes", []) if isinstance(state, dict) else [],
            verification_status=record.verification,
        )
        self.registry.touch(project.id, last_session_id=record.id)

    def projects_snapshot(self) -> dict[str, Any]:
        projects = []
        for project in self.registry.list():
            payload = asdict(project)
            payload["sessions"] = [asdict(item) for item in self.sessions.list(project.id)]
            projects.append(payload)
        return {
            "projects": projects,
            "active_project_id": self.active_project_id,
            "active_session_id": self.active_session_id,
        }

    def snapshot(self) -> dict[str, Any]:
        if self._active is None:
            return {
                **self.projects_snapshot(),
                "workspace": None,
                "model": None,
                "allowed_models": [],
                "status": "NO_PROJECT",
                "verification_status": "NOT_RUN",
                "execution_mode": "act",
                "plan_state": "inactive",
                "pending_plan": None,
                "pending_approval": None,
                "file_changes": [],
                "references": [],
                "history": [],
                "event_id": self.events.latest_id(),
            }
        snapshot = self.active.snapshot()
        snapshot.update(self.projects_snapshot())
        snapshot["history"] = self.active.agent.history_snapshot()
        return snapshot

    def register_project(self, workspace: str, *, name: str | None = None) -> ProjectRecord:
        with self._lock:
            self._ensure_idle()
            project = self.registry.register(workspace, name=name)
            records = self.sessions.list(project.id)
            record = records[0] if records else self.sessions.create(project.id)
            self._activate(project, record)
            return project

    def create_session(self, project_id: str, *, title: str = "新会话") -> SessionRecord:
        with self._lock:
            self._ensure_idle()
            project = self.registry.get(project_id)
            if project is None:
                raise KeyError(project_id)
            record = self.sessions.create(
                project.id,
                title=title,
                model=self.active.model_name if self._active is not None else None,
                mode=self.active.agent.execution_mode.value if self._active is not None else "act",
            )
            self._activate(project, record)
            return record

    def switch_session(self, project_id: str, session_id: str) -> SessionRecord:
        with self._lock:
            self._ensure_idle()
            project = self.registry.get(project_id)
            record = self.sessions.get(project_id, session_id)
            if project is None or record is None:
                raise KeyError(session_id)
            if project_id == self.active_project_id and session_id == self.active_session_id:
                return record
            self._activate(project, record)
            return record

    def _on_prompt_complete(self, web: WebSession, prompt: str, outcome: AgentOutcome) -> None:
        self._persist_completed_prompt(prompt, outcome)

    def _persist_completed_prompt(self, prompt: str, outcome: AgentOutcome | None) -> None:
        with self._lock:
            project = self.registry.get(self.active_project_id)
            record = self.sessions.get(self.active_project_id, self.active_session_id)
            if project is None or record is None:
                return
            state = self.active.agent.export_state()
            state["file_changes"] = self.active.snapshot()["file_changes"]
            extracted_index = self.active.agent.last_memory_extracted_prompt_index
            prompt_index = self.active.agent.prompt_count
            if prompt_index > extracted_index:
                memory_result = self.memory_service.process_completed_prompt(
                    project_id=project.id,
                    project_name=project.name,
                    session_id=record.id,
                    prompt_index=prompt_index,
                    recent_user_messages=[prompt],
                )
                state["last_memory_extracted_prompt_index"] = prompt_index
                self.active.agent.last_memory_extracted_prompt_index = prompt_index
                for item in memory_result.created:
                    self.events.publish("memory_created", asdict(item))
                if memory_result.error:
                    self.events.publish("memory_extraction_error", {"message": memory_result.error})
            title = record.title
            if record.prompt_count == 0 and prompt.strip():
                compact = " ".join(prompt.split())
                title = compact[:48] + ("…" if len(compact) > 48 else "")
            verification = outcome.verification_status if outcome else self.active.snapshot()["verification_status"]
            status = outcome.stop_reason.value.lower() if outcome else "completed"
            self.sessions.save_state(project.id, record.id, state)
            self.sessions.update(
                record.id,
                project.id,
                title=title,
                status=status,
                verification=verification,
                model=self.active.model_name,
                mode=self.active.agent.execution_mode.value,
                prompt_count=prompt_index,
            )
            self.registry.touch(project.id, last_session_id=record.id)

    def list_memories(self, *, scope: str, project_id: str | None = None) -> list[MemoryItem]:
        owner = project_id or self.active_project_id if scope == "project" else None
        return self.memories.list(scope=scope, project_id=owner)

    def remember(self, *, scope: str, kind: str, title: str, content: str, project_id: str | None = None) -> MemoryItem:
        owner = (project_id or self.active_project_id) if scope == "project" else None
        item = self.memories.remember(
            scope=scope,
            project_id=owner,
            kind=kind,
            title=title,
            content=content,
            source="manual",
            source_session_id=self.active_session_id,
            source_prompt_index=self.active.agent.prompt_count if self._active is not None else 0,
        )
        self.events.publish("memory_created", asdict(item))
        return item

    def forget_memory(self, memory_id: str, *, project_id: str | None = None) -> bool:
        removed = self.memories.forget(memory_id, project_id=project_id or self.active_project_id)
        if removed:
            self.events.publish("memory_forgotten", {"id": memory_id})
        return removed

    def close(self, *, wait_timeout: float = 2.0) -> None:
        if self._active is not None:
            self._active.close(wait_timeout=wait_timeout)
