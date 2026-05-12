"""Action framework. Each action declares JSON schema and executes against a session."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core import InspectorError
from ..session import InspectorSession


@dataclass
class ActionResult:
    ok: bool
    data: Any = None
    error: dict | None = None
    artifacts: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict:
        d = {
            "ok": self.ok,
            "duration_ms": round(self.duration_ms, 1),
        }
        if self.data is not None: d["data"] = self.data
        if self.error: d["error"] = self.error
        if self.artifacts: d["artifacts"] = self.artifacts
        if self.notes: d["notes"] = self.notes
        return d


class Action:
    name: str = "base"
    description: str = ""
    schema: dict = {"type": "object", "properties": {}}
    idempotent: bool = False

    def run(self, session: InspectorSession, **kwargs) -> ActionResult:
        start = time.time()
        try:
            data = self._execute(session, **kwargs)
        except InspectorError as e:
            return ActionResult(ok=False, error=e.to_dict(),
                                duration_ms=(time.time() - start) * 1000)
        except Exception as e:
            return ActionResult(ok=False,
                                error={"error": "E_UNEXPECTED", "message": str(e)},
                                duration_ms=(time.time() - start) * 1000)
        if isinstance(data, ActionResult):
            data.duration_ms = (time.time() - start) * 1000
            return data
        return ActionResult(ok=True, data=data,
                            duration_ms=(time.time() - start) * 1000)

    def _execute(self, session: InspectorSession, **kwargs) -> Any:
        raise NotImplementedError

    @classmethod
    def to_tool_schema(cls) -> dict:
        return {
            "name": cls.name,
            "description": cls.description.strip(),
            "input_schema": cls.schema,
        }


def _node_summary(node) -> dict:
    """Compact view-node dict for LLM consumption.

    Includes visibility hints (``on_screen``, ``alpha``) so the LLM can tell
    apart actually-visible content from off-screen reuse pool views.
    """
    out = {
        "address": node.address,
        "class": node.cls,
        "frame": [node.frame.x, node.frame.y, node.frame.width, node.frame.height],
        "text": (node.text or "")[:80] or None,
        "aid": node.accessibility_id,
        "hidden": node.hidden,
    }
    # Only attach visibility hints when meaningfully different from the default.
    if getattr(node, "on_screen", None) is False:
        out["on_screen"] = False
    if getattr(node, "alpha", 1.0) < 0.99:
        out["alpha"] = round(node.alpha, 3)
    if getattr(node, "offscreen_child_count", 0):
        out["offscreen_child_count"] = node.offscreen_child_count
    return out


def _vc_summary(vc) -> dict:
    out = {"class": vc.cls, "address": vc.address, "title": vc.title}
    if vc.children:
        out["children"] = [_vc_summary(c) for c in vc.children]
    if vc.presented:
        out["presented"] = _vc_summary(vc.presented)
    return out
