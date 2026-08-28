from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime
from pathlib import Path

import uvicorn

from .agent import AgentSession
from .cli import print_agent_event, print_tool_result
from .config import Config, ConfigError
from .model_adapter import OpenAIChatModel
from .permissions import AgentMode
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
    elif tool == "run_command":
        text = f"已运行 {len(data.get('commands', []))} 个命令"
    else:
        text = str(payload.get("summary") or tool)
    return {
        "text": text,
        "tool": tool,
        "ok": bool(payload.get("ok")),
        "detail": payload,
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
    return {"text": text, "detail": payload}


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
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        trace_path = workspace / ".minicodex" / "sessions" / f"{stamp}.jsonl"
        trace = SessionTrace(trace_path, workspace=workspace)
        events = EventBus()
        approvals = ApprovalGate(events)
        runtime = ToolRuntime(workspace, approver=approvals.request, mode=AgentMode(args.mode))
        model = OpenAIChatModel.from_config(config)
        agent = AgentSession(
            model,
            runtime,
            max_turns_per_prompt=config.max_turns,
            trace=trace,
            on_tool_result=print_tool_result,
            on_event=lambda event_type, payload: publish_agent_event(events, event_type, payload),
        )
        session = WebSession(
            agent,
            events,
            approvals,
            workspace=workspace,
            model_name=config.model,
            max_turns_per_prompt=config.max_turns,
            allowed_models=config.allowed_models,
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
