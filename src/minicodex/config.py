from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    model: str
    base_url: str | None = None
    enable_thinking: bool = False
    max_turns: int = 20

    @classmethod
    def from_env(cls, *, model: str | None = None, max_turns: int = 20) -> "Config":
        dotenv = dotenv_values(Path.cwd() / ".env")
        api_key = (
            os.getenv("MINICODEX_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or dotenv.get("MINICODEX_API_KEY")
            or dotenv.get("DASHSCOPE_API_KEY")
        )
        selected_model = model or os.getenv("MINICODEX_MODEL") or dotenv.get("MINICODEX_MODEL")
        if not api_key:
            raise ConfigError("MINICODEX_API_KEY or DASHSCOPE_API_KEY is required")
        if not selected_model:
            raise ConfigError("MINICODEX_MODEL is required (or pass --model)")
        return cls(
            api_key=api_key,
            model=selected_model,
            base_url=os.getenv("MINICODEX_BASE_URL") or dotenv.get("MINICODEX_BASE_URL") or None,
            enable_thinking=(os.getenv("MINICODEX_ENABLE_THINKING") or dotenv.get("MINICODEX_ENABLE_THINKING") or "false").strip().lower()
            in {"1", "true", "yes", "on"},
            max_turns=max_turns,
        )
