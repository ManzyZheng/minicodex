from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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


def create_app(web_session: WebSession, *, access_token: str) -> FastAPI:
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
        cursor = min(cursor, web_session.events.latest_id())

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
