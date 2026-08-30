from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

import uvicorn

from .agent import AgentSession
from .cli import print_agent_event, print_tool_result
from .config import Config, ConfigError
from .model_adapter import OpenAIChatModel
from .memory import MemoryExtractor, MemoryService, MemoryStore
from .permissions import AgentMode
from .persistence import ApplicationPaths
from .project_sessions import SessionRecord, SessionRepository
from .projects import ProjectRecord, ProjectRegistry
from .reviewer import ModelPermissionReviewer
from .session import SessionTrace
from .tools import ToolRuntime
from .web.app import create_app
from .web.approval import ApprovalGate
from .web.events import EventBus
from .web.manager import WebWorkspaceManager
from .web.session import WebSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minicodex-web", description="Run the local MiniCodex web console.")
    parser.add_argument("--workspace", default=".", help="project directory (default: current directory)")
    parser.add_argument("--model", help="model name; otherwise MINICODEX_MODEL")
    parser.add_argument("--max-turns", type=int, default=50, help="maximum model turns per prompt (default: 50)")
    parser.add_argument("--port", type=int, default=8000, help="loopback port (default: 8000)")
    parser.add_argument("--mode", choices=[mode.value for mode in AgentMode], default=AgentMode.ACT.value, help="initial permission mode")
    return parser


def serve(app, port: int) -> None:
    """Serve locally; the host is deliberately not configurable."""
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


def local_console_url(port: int, access_token: str) -> str:
    return f"http://127.0.0.1:{port}/?token={access_token}"


def summarize_tool_result(payload: dict) -> dict:
    tool = str(payload.get("tool") or "tool")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if not payload.get("ok"):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        text = f"{tool} 失败 · {error.get('code') or 'UNKNOWN_ERROR'}"
    elif tool == "read_file":
        text = f"已读取 {data.get('path') or '文件'}"
    elif tool == "list_files":
        text = f"已列出 {len(data.get('files', []))} 个文件"
    elif tool == "search_text":
        text = f"已搜索文本，找到 {len(data.get('matches', []))} 处"
    elif tool in {"write_file", "edit_file"}:
        text = f"已修改 {data.get('path') or '文件'}"
    elif tool == "run_shell":
        text = f"已运行 {len(data.get('commands', []))} 个命令"
    else:
        text = str(payload.get("summary") or tool)
    return {
        "text": text,
        "tool": tool,
        "ok": bool(payload.get("ok")),
        "detail": payload,
        "turn": payload.get("turn"),
    }


def summarize_command(payload: dict) -> dict:
    exit_code = payload.get("exit_code")
    purpose = payload.get("purpose")
    if exit_code == 0 and purpose in {"test", "build", "lint"}:
        text = "验证通过"
    elif exit_code == 0:
        text = "命令完成"
    elif exit_code is None:
        text = f"命令{payload.get('status') or '未执行'}"
    else:
        text = f"命令失败 · exit code {exit_code}"
    return {"text": text, "detail": payload, "turn": payload.get("turn")}


def publish_agent_event(events: EventBus, event_type: str, payload: dict) -> None:
    print_agent_event(event_type, payload)
    if event_type == "model_reasoning":
        return
    if event_type == "model_message":
        content = str(payload.get("content") or "").strip()
        if content:
            events.publish("progress", {"text": content, "turn": payload.get("turn")})
        return
    if event_type == "tool_result":
        events.publish("tool_summary", summarize_tool_result(payload))
        return
    if event_type == "command_output":
        events.publish("command_summary", summarize_command(payload))
        return
    if event_type == "turn_completed":
        events.publish("final_answer", payload)
        events.publish("turn_completed", payload)
        return
    if event_type in {"tool_call", "diff"}:
        return
    events.publish(event_type, payload)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_turns < 1:
        print("error: --max-turns must be at least 1", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("error: --port must be between 1 and 65535", file=sys.stderr)
        return 2
    try:
        workspace = Path(args.workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace is not a directory")
        config = Config.from_env(model=args.model, max_turns=args.max_turns)
        paths = ApplicationPaths()
        registry = ProjectRegistry(paths)
        session_repository = SessionRepository(paths)
        memories = MemoryStore(paths)
        events = EventBus()
        model = OpenAIChatModel.from_config(config)
        memory_model = OpenAIChatModel.from_config(config, enable_thinking=False)
        reviewer = None
        if config.reviewer_enabled:
            review_model = OpenAIChatModel.from_config(
                config,
                model=config.reviewer_model,
                enable_thinking=False,
            )
            reviewer = ModelPermissionReviewer(review_model).review
        def session_factory(
            project: ProjectRecord,
            record: SessionRecord,
            state: dict,
            on_complete,
        ) -> WebSession:
            approvals = ApprovalGate(events)
            selected_model = record.model if record.model in config.allowed_models else config.model
            model.set_model(selected_model)
            runtime = ToolRuntime(
                project.workspace,
                approver=approvals.request,
                reviewer=reviewer,
                mode=AgentMode(record.mode) if record.mode in {"act", "auto-act"} else AgentMode(args.mode),
            )
            trace_path = paths.session_root(project.id, record.id) / "trace.jsonl"
            agent = AgentSession(
                model,
                runtime,
                max_turns_per_prompt=config.max_turns,
                trace=SessionTrace(trace_path),
                on_tool_result=print_tool_result,
                on_event=lambda event_type, payload: publish_agent_event(events, event_type, payload),
                memory_prompt_provider=lambda: memories.prompt_context(project.id),
            )
            if state:
                agent.restore_state(state)
            return WebSession(
                agent,
                events,
                approvals,
                workspace=project.workspace,
                model_name=selected_model,
                max_turns_per_prompt=config.max_turns,
                allowed_models=config.allowed_models,
                on_prompt_complete=on_complete,
            )

        session = WebWorkspaceManager(
            paths=paths,
            registry=registry,
            sessions=session_repository,
            memories=memories,
            memory_service=MemoryService(memories, MemoryExtractor(memory_model)),
            session_factory=session_factory,
            events=events,
            initial_workspace=workspace,
        )
    except (OSError, ValueError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: unable to initialize MiniCodex Web: {exc}", file=sys.stderr)
        return 2

    access_token = secrets.token_urlsafe(32)
    console_url = local_console_url(args.port, access_token)
    print(f"MiniCodex workspace: {workspace}", flush=True)
    trace_path = paths.session_root(session.active_project_id, session.active_session_id) / "trace.jsonl"
    print(f"Session trace: {trace_path}", flush=True)
    print(f"Web console: {console_url}", flush=True)
    try:
        serve(create_app(session, access_token=access_token), args.port)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
