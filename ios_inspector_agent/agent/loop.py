"""Agent loop: think → act → observe.

The conversation is multi-turn:
- The user can send a new message at any time (CLI driven).
- For each user message, the agent runs an inner loop:
    LLM plan → if tool_use, execute → feed result back → repeat
    until LLM returns end_turn (a text reply).
- Conversation history is preserved across user turns within one session.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from rich.console import Console

from ..actions import get_action, list_tool_schemas
from ..llm import LLMClient, SYSTEM_PROMPT, ToolCall
from ..session import InspectorSession
from ..trace import Recorder


# Compact JSON to save tokens
def _compact(payload) -> str:
    s = json.dumps(payload, ensure_ascii=False, default=str)
    if len(s) > 8000:
        return s[:8000] + f"…[truncated, total {len(s)} chars]"
    return s


@dataclass
class AgentConfig:
    max_inner_steps: int = 12
    max_taps: int = 30
    max_modifications: int = 8
    confirm_for: set[str] = field(default_factory=lambda: {"open_url", "view_modify"})


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        session: InspectorSession,
        recorder: Recorder,
        *,
        config: AgentConfig | None = None,
        confirm_fn: Optional[Callable[[ToolCall], bool]] = None,
        console: Console | None = None,
    ):
        self.llm = llm
        self.session = session
        self.recorder = recorder
        self.config = config or AgentConfig()
        self.confirm_fn = confirm_fn
        self.console = console or Console()

        self._messages: list[dict] = []
        self._tool_schemas = list_tool_schemas()
        self._counters = {"tap": 0, "view_modify": 0}

    # ---- public API ----------------------------------------------------
    def chat(self, user_text: str) -> str:
        """Process a single user turn; return the assistant's final text reply."""
        self._messages.append({"role": "user", "content": user_text})
        self.recorder.log("user", {"text": user_text})

        final_text = ""
        for step in range(self.config.max_inner_steps):
            turn = self._stream_turn()
            self.recorder.log("assistant", {
                "text": turn.text,
                "tool_calls": [{"name": t.name, "args": t.arguments} for t in turn.tool_calls],
                "stop_reason": turn.stop_reason,
            })

            assistant_blocks: list[dict] = []
            if turn.text:
                assistant_blocks.append({"type": "text", "text": turn.text})
            for tc in turn.tool_calls:
                assistant_blocks.append({
                    "type": "tool_use",
                    "id": tc.id, "name": tc.name, "input": tc.arguments,
                })
            self._messages.append({"role": "assistant", "content": assistant_blocks})

            if not turn.tool_calls:
                final_text = turn.text or "(no reply)"
                break

            # Execute tool calls and append results
            tool_results: list[dict] = []
            for tc in turn.tool_calls:
                result_payload, is_error = self._execute_tool(tc)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": _compact(result_payload),
                    "is_error": is_error,
                })
            self._messages.append({"role": "user", "content": tool_results})
        else:
            final_text = "(reached max inner steps; stopping. ask me to continue if needed.)"

        return final_text

    def _stream_turn(self):
        """Stream one LLM turn to console, return the completed AssistantTurn."""
        for chunk in self.llm.chat_stream(
            system=SYSTEM_PROMPT,
            messages=self._messages,
            tools=self._tool_schemas,
        ):
            if chunk.text_delta:
                self.console.print(chunk.text_delta, end="")
            if chunk.turn_complete is not None:
                # Ensure newline after streamed text
                self.console.print()
                return chunk.turn_complete
        # Fallback — should never reach here if protocol is followed
        from ..llm import AssistantTurn
        return AssistantTurn(text="", tool_calls=[], stop_reason="end_turn")

    # ---- tool dispatch -------------------------------------------------
    def _execute_tool(self, tc: ToolCall) -> tuple[dict, bool]:
        # Budget check
        if tc.name == "tap" or tc.name == "find_and_tap":
            self._counters["tap"] += 1
            if self._counters["tap"] > self.config.max_taps:
                return ({"error": "E_BUDGET", "message": "tap budget exhausted"}, True)
        if tc.name == "view_modify":
            self._counters["view_modify"] += 1
            if self._counters["view_modify"] > self.config.max_modifications:
                return ({"error": "E_BUDGET", "message": "modify budget exhausted"}, True)

        # Confirmation gate
        if tc.name in self.config.confirm_for and self.confirm_fn:
            if not self.confirm_fn(tc):
                return ({"error": "E_DECLINED",
                         "message": f"user declined to allow {tc.name}"}, True)

        # Pretty-print the action
        self.console.print(f"[cyan]→ {tc.name}[/cyan] [dim]{json.dumps(tc.arguments, ensure_ascii=False)}[/dim]")

        try:
            action = get_action(tc.name)
        except KeyError:
            return ({"error": "E_UNKNOWN_TOOL", "message": f"no such tool: {tc.name}"}, True)

        result = action.run(self.session, **(tc.arguments or {}))
        payload = result.to_dict()
        self.recorder.log("tool_result", {"name": tc.name, "result": payload})

        # Compact preview to console
        preview = "ok" if result.ok else f"err: {result.error}"
        self.console.print(f"[green]✓[/green] {tc.name} → {preview}")
        return payload, (not result.ok)

    # ---- conversation reset --------------------------------------------
    def reset(self):
        self._messages.clear()
        self._counters = {"tap": 0, "view_modify": 0}
