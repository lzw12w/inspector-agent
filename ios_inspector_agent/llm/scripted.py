"""Scripted LLM — drives a deterministic tool-call sequence for tests / dry runs."""
from __future__ import annotations

from typing import Iterable

from .base import AssistantTurn, LLMClient, StreamChunk, ToolCall


class ScriptedLLM(LLMClient):
    def __init__(self, script: Iterable):
        self._steps = list(script)
        self._idx = 0

    def chat_stream(self, *, system, messages, tools) -> Iterable[StreamChunk]:
        if self._idx >= len(self._steps):
            turn = AssistantTurn(text="(scripted: end)", tool_calls=[], stop_reason="end_turn")
            self._idx += 1
            yield StreamChunk(turn_complete=turn)
            return
        step = self._steps[self._idx]
        self._idx += 1
        if isinstance(step, str):
            turn = AssistantTurn(text=step, tool_calls=[], stop_reason="end_turn")
            yield StreamChunk(text_delta=step, turn_complete=turn)
            return
        name, args = step
        turn = AssistantTurn(
            text="",
            tool_calls=[ToolCall(id=f"scripted-{self._idx}", name=name, arguments=args)],
            stop_reason="tool_use",
        )
        yield StreamChunk(turn_complete=turn)
