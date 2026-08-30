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
    allowed_models: tuple[str, ...] = ()
    base_url: str | None = None
    enable_thinking: bool = False
    reviewer_enabled: bool = True
    reviewer_model: str | None = None
    max_turns: int = 50

    @classmethod
    def from_env(cls, *, model: str | None = None, max_turns: int = 50) -> "Config":
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
        configured_models = os.getenv("MINICODEX_MODELS") or dotenv.get("MINICODEX_MODELS") or ""
        allowed_models = tuple(
            dict.fromkeys(
                [selected_model]
                + [item.strip() for item in configured_models.split(",") if item.strip() and item.strip() != selected_model]
            )
        )
        reviewer_enabled = (
            os.getenv("MINICODEX_REVIEWER_ENABLED")
            or dotenv.get("MINICODEX_REVIEWER_ENABLED")
            or "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        reviewer_model = (
            os.getenv("MINICODEX_REVIEWER_MODEL")
            or dotenv.get("MINICODEX_REVIEWER_MODEL")
            or selected_model
        )
        return cls(
            api_key=api_key,
            model=selected_model,
            allowed_models=allowed_models,
            base_url=os.getenv("MINICODEX_BASE_URL") or dotenv.get("MINICODEX_BASE_URL") or None,
            enable_thinking=(os.getenv("MINICODEX_ENABLE_THINKING") or dotenv.get("MINICODEX_ENABLE_THINKING") or "false").strip().lower()
            in {"1", "true", "yes", "on"},
            reviewer_enabled=reviewer_enabled,
            reviewer_model=str(reviewer_model),
            max_turns=max_turns,
        )
