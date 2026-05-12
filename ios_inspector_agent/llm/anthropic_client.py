"""Anthropic adapter using the Messages + tools API with streaming."""
from __future__ import annotations

import os
from typing import Any, Iterable

from .base import AssistantTurn, LLMClient, StreamChunk, ToolCall


class AnthropicLLM(LLMClient):
    def __init__(self, model: str = "claude-sonnet-4-5",
                 api_key: str | None = None,
                 base_url: str | None = None,
                 max_tokens: int = 4096):
        try:
            import anthropic  # noqa
        except ImportError as e:
            raise RuntimeError(
                "anthropic package not installed. `pip install anthropic`."
            ) from e
        from anthropic import Anthropic

        kwargs: dict[str, Any] = {
            "api_key": api_key or os.environ.get("ANTHROPIC_API_KEY"),
        }
        if base_url is not None:
            kwargs["base_url"] = base_url
        self._anthropic = Anthropic(**kwargs)
        self.model = model
        self.max_tokens = max_tokens

    def chat_stream(self, *, system: str, messages: list[dict],
                    tools: list[dict]) -> Iterable[StreamChunk]:
        with self._anthropic.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield StreamChunk(text_delta=text)

            message = stream.get_final_message()
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            for block in message.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append(ToolCall(
                        id=block.id, name=block.name,
                        arguments=block.input or {},
                    ))
            yield StreamChunk(
                turn_complete=AssistantTurn(
                    text="\n".join(text_parts).strip(),
                    tool_calls=tool_calls,
                    stop_reason=message.stop_reason or "end_turn",
                )
            )
