from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .agent import Agent, StopReason
from .config import Config, ConfigError
from .context import truncate_text
from .models import ToolResult
from .model_adapter import OpenAIChatModel
from .session import SessionTrace
from .tools import ToolRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minicodex", description="A small, safe coding agent.")
    parser.add_argument("task", nargs="?", help="coding task; if omitted, MiniCodex prompts for it")
    parser.add_argument("--workspace", default=".", help="project directory (default: current directory)")
    parser.add_argument("--model", help="model name; otherwise MINICODEX_MODEL")
    parser.add_argument("--max-turns", type=int, default=20, help="maximum model turns (default: 20)")
    return parser


def confirm_command(argv: list[str], purpose: str, timeout_sec: int) -> bool:
    print("\n[permission] The agent wants to run this argv command:")
    print(f"  purpose: {purpose}" + (" (success will verify current changes)" if purpose in {"test", "build", "lint"} else ""))
    print(f"  timeout: {timeout_sec}s")
    print("  " + repr(argv))
    try:
        answer = input("Allow? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def print_tool_result(result: ToolResult) -> None:
    mark = "ok" if result.ok else "error"
    print(f"\n[tool:{mark}] {result.tool}: {result.summary}")
    if result.error:
        print(f"  {result.error.code}: {result.error.message}")
    if isinstance(result.data, dict):
        detail = result.data.get("diff")
        if not detail and result.tool == "run_command":
            detail = (result.data.get("stdout") or "") + (result.data.get("stderr") or "")
        if detail:
            visible, _ = truncate_text(str(detail), limit=8_000)
            print(visible.rstrip())


def print_agent_event(event_type: str, payload: dict) -> None:
    if event_type != "model_reasoning":
        return
    content = str(payload.get("content") or "").strip()
    if not content:
        return
    visible, _ = truncate_text(content, limit=8_000)
    print(f"\n[thinking:turn {payload.get('turn', '—')}]\n{visible.rstrip()}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_turns < 1:
        print("error: --max-turns must be at least 1", file=sys.stderr)
        return 2
    try:
        workspace = Path(args.workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace is not a directory")
        config = Config.from_env(model=args.model, max_turns=args.max_turns)
    except (OSError, ValueError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        task = args.task or input("What should MiniCodex do? ").strip()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except EOFError:
        print("error: no task was provided", file=sys.stderr)
        return 2
    if not task:
        print("error: task cannot be empty", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    trace_path = workspace / ".minicodex" / "sessions" / f"{stamp}.jsonl"
    try:
        trace = SessionTrace(trace_path, workspace=workspace)
        runtime = ToolRuntime(workspace, command_approver=confirm_command)
        model = OpenAIChatModel.from_config(config)
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: unable to initialize MiniCodex: {exc}", file=sys.stderr)
        return 2
    print(f"MiniCodex workspace: {workspace}")
    print(f"Session trace: {trace_path}")
    outcome = Agent(
        model,
        runtime,
        max_turns=config.max_turns,
        trace=trace,
        on_tool_result=print_tool_result,
        on_event=print_agent_event,
    ).run(task)

    print("\n--- MiniCodex result ---")
    print(outcome.final_text)
    print(f"stop_reason: {outcome.stop_reason.value}")
    print(f"verification: {outcome.verification_status}")
    if outcome.verification:
        print(f"verification command: {outcome.verification.get('argv')}")
        print(f"verification exit code: {outcome.verification.get('exit_code')}")
    return 0 if outcome.stop_reason is StopReason.COMPLETED else (130 if outcome.stop_reason is StopReason.INTERRUPTED else 1)


if __name__ == "__main__":
    raise SystemExit(main())
