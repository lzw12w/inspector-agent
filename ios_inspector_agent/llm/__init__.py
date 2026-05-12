from .base import AssistantTurn, LLMClient, StreamChunk, ToolCall, ToolResultMessage
from .prompts import SYSTEM_PROMPT
from .scripted import ScriptedLLM

__all__ = ["LLMClient", "AssistantTurn", "ToolCall", "ToolResultMessage",
           "StreamChunk", "ScriptedLLM", "SYSTEM_PROMPT"]


def make_llm(provider: str, **kwargs) -> LLMClient:
    if provider == "anthropic":
        from .anthropic_client import AnthropicLLM
        return AnthropicLLM(**kwargs)
    if provider == "scripted":
        return ScriptedLLM(kwargs.get("script", []))
    raise ValueError(f"unknown LLM provider: {provider}")
