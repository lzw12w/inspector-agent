"""Provider-agnostic LLM client interface.

Decisions are tool calls or final messages. The agent loop is provider-agnostic;
each provider implementation translates between this interface and its native
tool-use API.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class AssistantTurn:
    """One LLM response. May contain text + zero or more tool calls."""
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # "end_turn", "tool_use", "max_tokens", ...


@dataclass
class ToolResultMessage:
    """Sent back to the LLM after executing a tool call."""
    tool_call_id: str
    content: Any  # JSON-serializable
    is_error: bool = False


@dataclass
class StreamChunk:
    """A single chunk emitted during streaming.

    - text_delta: incremental text to display live
    - turn_complete: the fully assembled turn (emitted once at the end)
    """
    text_delta: str = ""
    turn_complete: AssistantTurn | None = None


class LLMClient(ABC):
    """Stateless: caller passes the full message history each turn.

    The chat_stream method yields StreamChunk objects:
      - text_delta chunks for live display
      - a final chunk with turn_complete containing the full AssistantTurn
    """

    @abstractmethod
    def chat_stream(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> Iterable[StreamChunk]:
        ...
