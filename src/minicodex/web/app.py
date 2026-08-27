from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .events import WebEvent
from .session import SessionBusyError, WebSession


class PromptRequest(BaseModel):
    text: str


class ApprovalDecision(BaseModel):
    allow: bool


def format_sse_event(event: WebEvent) -> str:
    data = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"


def create_app(web_session: WebSession) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        web_session.close()

    app = FastAPI(title="MiniCodex Web", docs_url=None, redoc_url=None, lifespan=lifespan)
    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=FileResponse)
    def index() -> Path:
        return static_dir / "index.html"

    @app.get("/api/session")
    def session_snapshot() -> dict:
        return web_session.snapshot()

    @app.post("/api/prompts", status_code=status.HTTP_202_ACCEPTED)
    def submit_prompt(body: PromptRequest) -> dict[str, str]:
        if not body.text.strip():
            raise HTTPException(status_code=422, detail="prompt must not be empty")
        try:
            web_session.submit_prompt(body.text)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "accepted"}

    @app.post("/api/approvals/{request_id}")
    def resolve_approval(request_id: str, body: ApprovalDecision) -> dict[str, str]:
        if not web_session.resolve_approval(request_id, body.allow):
            raise HTTPException(status_code=409, detail="approval is missing, stale, or already resolved")
        return {"status": "resolved"}

    @app.get("/api/events")
    async def event_stream(
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            cursor = max(0, int(last_event_id or "0"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                events = await asyncio.to_thread(web_session.events.wait_after, cursor, 15.0)
                if not events:
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue
                for event in events:
                    cursor = event.id
                    yield format_sse_event(event)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
