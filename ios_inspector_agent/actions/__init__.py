"""Action registry — maps tool name to Action class."""
from __future__ import annotations

from .base import Action, ActionResult
from .inspect import (
    AppStateAction, ConsoleLogAction, FindViewAction, NetworkLogAction,
    PingAction, ScreenshotAction, VCHierarchyAction, ViewHierarchyAction,
    ViewInspectAction,
)
from .interact import (
    BackAction, DismissAction, FindAndTapAction, InputTextAction, OpenURLAction,
    ScrollAction, SwipeAction, SwitchTabAction, TapAction, ViewModifyAction,
)


_ALL_ACTIONS: list[type[Action]] = [
    PingAction, VCHierarchyAction, ViewHierarchyAction, FindViewAction,
    ViewInspectAction, ScreenshotAction, AppStateAction, NetworkLogAction,
    ConsoleLogAction,
    TapAction, FindAndTapAction, ScrollAction, SwipeAction, InputTextAction,
    DismissAction, BackAction, SwitchTabAction, OpenURLAction, ViewModifyAction,
]

_REGISTRY: dict[str, type[Action]] = {a.name: a for a in _ALL_ACTIONS}


def get_action(name: str) -> Action:
    if name not in _REGISTRY:
        raise KeyError(f"unknown action: {name}")
    return _REGISTRY[name]()


def list_tool_schemas() -> list[dict]:
    return [a.to_tool_schema() for a in _ALL_ACTIONS]


def list_action_names() -> list[str]:
    return list(_REGISTRY.keys())


__all__ = ["Action", "ActionResult", "get_action",
           "list_tool_schemas", "list_action_names"]
