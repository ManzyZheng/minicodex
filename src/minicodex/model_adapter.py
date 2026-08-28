from __future__ import annotations

import json
import time
from typing import Any, Callable

from .config import Config
from .models import ModelReply, ToolCall


class OpenAIChatModel:
    """Small adapter for OpenAI-compatible Chat Completions tool calling."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        enable_thinking: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.model = model
        self.enable_thinking = enable_thinking
        self.sleep = sleep

    @classmethod
    def from_config(cls, config: Config) -> "OpenAIChatModel":
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("The 'openai' package is required. Install with: pip install -e .") from exc
        kwargs: dict[str, Any] = {"api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return cls(OpenAI(**kwargs), model=config.model, enable_thinking=config.enable_thinking)

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        return status == 429 or isinstance(status, int) and status >= 500 or type(exc).__name__ in {
            "APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"
        }

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        for attempt in range(3):
            try:
                request: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                }
                if self.enable_thinking:
                    request["extra_body"] = {"enable_thinking": True, "preserve_thinking": False}
                response = self.client.chat.completions.create(**request)
                message = response.choices[0].message
                calls = []
                for call in message.tool_calls or []:
                    arguments = json.loads(call.function.arguments or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError(f"tool arguments for {call.function.name} must be a JSON object")
                    calls.append(ToolCall(call.id, call.function.name, arguments))
                return ModelReply(
                    content=message.content,
                    tool_calls=calls,
                    reasoning_content=getattr(message, "reasoning_content", None) or None,
                )
            except Exception as exc:
                if attempt == 2 or not self._is_transient(exc):
                    raise
                self.sleep(0.5 * (2**attempt))
        raise AssertionError("unreachable")
