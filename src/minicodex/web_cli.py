from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime
from pathlib import Path

import uvicorn

from .agent import AgentSession
from .cli import print_tool_result
from .config import Config, ConfigError
from .model_adapter import OpenAIChatModel
from .session import SessionTrace
from .tools import ToolRuntime
from .web.app import create_app
from .web.approval import ApprovalGate
from .web.events import EventBus
from .web.session import WebSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minicodex-web", description="Run the local MiniCodex web console.")
    parser.add_argument("--workspace", default=".", help="project directory (default: current directory)")
    parser.add_argument("--model", help="model name; otherwise MINICODEX_MODEL")
    parser.add_argument("--max-turns", type=int, default=20, help="maximum model turns per prompt (default: 20)")
    parser.add_argument("--port", type=int, default=8000, help="loopback port (default: 8000)")
    return parser


def serve(app, port: int) -> None:
    """Serve locally; the host is deliberately not configurable."""
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


def local_console_url(port: int, access_token: str) -> str:
    return f"http://127.0.0.1:{port}/?token={access_token}"


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
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        trace_path = workspace / ".minicodex" / "sessions" / f"{stamp}.jsonl"
        trace = SessionTrace(trace_path, workspace=workspace)
        events = EventBus()
        approvals = ApprovalGate(events)
        runtime = ToolRuntime(workspace, command_approver=approvals.request)
        model = OpenAIChatModel.from_config(config)
        agent = AgentSession(
            model,
            runtime,
            max_turns_per_prompt=config.max_turns,
            trace=trace,
            on_tool_result=print_tool_result,
            on_event=events.publish,
        )
        session = WebSession(
            agent,
            events,
            approvals,
            workspace=workspace,
            model_name=config.model,
            max_turns_per_prompt=config.max_turns,
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
