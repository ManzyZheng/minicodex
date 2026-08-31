from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..permissions import AgentMode
from .events import WebEvent
from .session import PlanResolutionError, SessionBusyError, WebSession


class PromptRequest(BaseModel):
    text: str
    permission: Literal["act", "auto-act"] | None = None
    model: str | None = None


class ApprovalDecision(BaseModel):
    allow: bool


class ModeRequest(BaseModel):
    mode: AgentMode


class PlanResolutionRequest(BaseModel):
    action: Literal["execute", "revise", "cancel"]
    feedback: str | None = None


class ProjectRequest(BaseModel):
    workspace: str
    name: str | None = None


class SessionRequest(BaseModel):
    title: str = "新会话"


class MemoryRequest(BaseModel):
    scope: Literal["global", "project"]
    kind: Literal["preference", "decision", "reference"]
    title: str
    content: str
    project_id: str | None = None


def format_sse_event(event: WebEvent) -> str:
    payload = {**event.payload, "event_timestamp": event.timestamp}
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"


def create_app(web_session: WebSession, *, access_token: str) -> FastAPI:
    def require_active_session() -> None:
        if getattr(web_session, "has_active_session", True) is False:
            raise HTTPException(status_code=409, detail="select or add a project before running the Agent")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        web_session.close()

    app = FastAPI(title="MiniCodex Web", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def protect_local_api(request: Request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"detail": "invalid Host header"}, status_code=400)
        origin = request.headers.get("origin")
        if origin and not (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")):
            return JSONResponse({"detail": "cross-site requests are not allowed"}, status_code=403)
        if request.url.path.startswith("/api/"):
            supplied = request.query_params.get("token", "")
            if not hmac.compare_digest(supplied, access_token):
                return JSONResponse({"detail": "invalid session token"}, status_code=401)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=FileResponse)
    def index() -> Path:
        return static_dir / "index.html"

    @app.get("/api/session")
    def session_snapshot() -> dict:
        return web_session.snapshot()

    @app.get("/api/projects")
    def list_projects() -> dict:
        provider = getattr(web_session, "projects_snapshot", None)
        if not callable(provider):
            snapshot = web_session.snapshot()
            return {"projects": [], "active_project_id": None, "active_session_id": None, "legacy": snapshot}
        return provider()

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def register_project(body: ProjectRequest) -> dict:
        try:
            return asdict(web_session.register_project(body.workspace, name=body.name))
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/sessions", status_code=status.HTTP_201_CREATED)
    def create_session(project_id: str, body: SessionRequest) -> dict:
        try:
            return asdict(web_session.create_session(project_id, title=body.title))
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc

    @app.post("/api/projects/{project_id}/sessions/{session_id}/activate")
    def activate_session(project_id: str, session_id: str) -> dict:
        try:
            return asdict(web_session.switch_session(project_id, session_id))
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc

    @app.get("/api/memories")
    def list_memories(scope: Literal["global", "project"], project_id: str | None = None) -> list[dict]:
        provider = getattr(web_session, "list_memories", None)
        if not callable(provider):
            raise HTTPException(status_code=404, detail="memory is not available")
        return [asdict(item) for item in provider(scope=scope, project_id=project_id)]

    @app.post("/api/memories", status_code=status.HTTP_201_CREATED)
    def remember(body: MemoryRequest) -> dict:
        provider = getattr(web_session, "remember", None)
        if not callable(provider):
            raise HTTPException(status_code=404, detail="memory is not available")
        try:
            return asdict(provider(**body.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/memories/{memory_id}")
    def forget_memory(memory_id: str, project_id: str | None = None) -> dict[str, str]:
        provider = getattr(web_session, "forget_memory", None)
        if not callable(provider) or not provider(memory_id, project_id=project_id):
            raise HTTPException(status_code=404, detail="memory not found")
        return {"status": "forgotten"}

    @app.post("/api/prompts", status_code=status.HTTP_202_ACCEPTED)
    def submit_prompt(body: PromptRequest) -> dict[str, str]:
        require_active_session()
        if not body.text.strip():
            raise HTTPException(status_code=422, detail="prompt must not be empty")
        try:
            permission = AgentMode(body.permission) if body.permission is not None else None
            web_session.submit_prompt(body.text, permission=permission, model=body.model)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "accepted"}

    @app.post("/api/interrupt", status_code=status.HTTP_202_ACCEPTED)
    def interrupt_prompt() -> dict[str, str]:
        require_active_session()
        if not web_session.interrupt():
            raise HTTPException(status_code=409, detail="no Agent prompt is currently running")
        return {"status": "stopping"}

    @app.post("/api/mode")
    def change_mode(body: ModeRequest) -> dict[str, str]:
        require_active_session()
        try:
            mode = web_session.set_mode(body.mode)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"mode": mode.value}

    @app.post("/api/plans/approve", status_code=status.HTTP_202_ACCEPTED)
    def approve_plan(body: ModeRequest) -> dict[str, str]:
        require_active_session()
        try:
            web_session.approve_plan(body.mode)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "accepted", "mode": body.mode.value}

    @app.post("/api/plans/{plan_id}/resolve", status_code=status.HTTP_202_ACCEPTED)
    def resolve_plan(plan_id: str, body: PlanResolutionRequest) -> dict[str, str]:
        require_active_session()
        try:
            web_session.resolve_plan(plan_id, body.action, body.feedback)
        except (SessionBusyError, PlanResolutionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "accepted", "action": body.action}

    @app.post("/api/approvals/{request_id}")
    def resolve_approval(request_id: str, body: ApprovalDecision) -> dict[str, str]:
        require_active_session()
        if not web_session.resolve_approval(request_id, body.allow):
            raise HTTPException(status_code=409, detail="approval is missing, stale, or already resolved")
        return {"status": "resolved"}

    @app.delete("/api/references/{reference_id}")
    def remove_reference(reference_id: str) -> dict[str, str]:
        require_active_session()
        try:
            removed = web_session.remove_reference(reference_id)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="reference is missing or already removed")
        return {"status": "removed"}

    @app.get("/api/events")
    async def event_stream(
        request: Request,
        after: int = 0,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            cursor = max(0, int(last_event_id)) if last_event_id is not None else max(0, after)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
        cursor = min(cursor, web_session.events.latest_id())
        subscription = web_session.events.subscribe(cursor)

        async def generate():
            nonlocal cursor
            try:
                for event in subscription.replay:
                    cursor = event.id
                    yield format_sse_event(event)
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(subscription.queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield "event: heartbeat\ndata: {}\n\n"
                        continue
                    cursor = event.id
                    yield format_sse_event(event)
            finally:
                subscription.close()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
