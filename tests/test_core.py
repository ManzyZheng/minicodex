from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicodex.config import Config, ConfigError
from minicodex.models import ToolResult
from minicodex.workspace import WorkspaceError, WorkspaceGuard
from minicodex.session import SessionTrace


def test_config_reads_key_from_environment_without_storing_it_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINICODEX_API_KEY", "top-secret")
    monkeypatch.setenv("MINICODEX_MODEL", "demo-model")
    config = Config.from_env()
    assert config.api_key == "top-secret"
    assert config.model == "demo-model"
    assert "top-secret" not in repr(config)


def test_config_requires_api_key_without_environment_or_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINICODEX_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("MINICODEX_MODEL", "demo-model")
    with pytest.raises(ConfigError, match="MINICODEX_API_KEY"):
        Config.from_env()


def test_config_loads_dashscope_qwen_settings_from_current_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINICODEX_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("MINICODEX_MODEL", raising=False)
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=test-key\n"
        "MINICODEX_MODEL=qwen3.8-flash\n"
        "MINICODEX_BASE_URL=https://example.test/compatible-mode/v1\n"
        "MINICODEX_ENABLE_THINKING=true\n",
        encoding="utf-8",
    )

    config = Config.from_env()

    assert config.api_key == "test-key"
    assert config.model == "qwen3.8-flash"
    assert config.base_url == "https://example.test/compatible-mode/v1"
    assert config.enable_thinking is True


def test_tool_result_has_stable_json_shape() -> None:
    result = ToolResult.failure(tool="read_file", call_id="call-1", code="FILE_NOT_FOUND", message="missing", retryable=False)
    assert json.loads(result.to_json()) == {
        "ok": False, "tool": "read_file", "call_id": "call-1", "summary": "missing", "data": None,
        "error": {"code": "FILE_NOT_FOUND", "message": "missing", "retryable": False},
        "meta": {"duration_ms": 0, "truncated": False, "artifact_path": None},
    }


def test_workspace_guard_accepts_inside_path_and_rejects_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    guard = WorkspaceGuard(workspace)
    assert guard.resolve("src/new.py") == workspace / "src" / "new.py"
    with pytest.raises(WorkspaceError, match="outside workspace"):
        guard.resolve("../secret.txt")


def test_workspace_guard_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace, outside = tmp_path / "project", tmp_path / "outside"
    workspace.mkdir(); outside.mkdir()
    link = workspace / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(WorkspaceError, match="outside workspace"):
        WorkspaceGuard(workspace).resolve("link/secret.txt")


def test_session_trace_rejects_path_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    with pytest.raises(WorkspaceError, match="outside workspace"):
        SessionTrace(tmp_path / "outside.jsonl", workspace=workspace)
