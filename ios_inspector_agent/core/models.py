"""Domain models. Frozen dataclasses; never mutate in place."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


@dataclass(frozen=True)
class Frame:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_any(cls, value: Any) -> "Frame":
        if value is None:
            return cls(0, 0, 0, 0)
        if isinstance(value, dict):
            return cls(
                float(value.get("x", 0) or 0),
                float(value.get("y", 0) or 0),
                float(value.get("width", value.get("w", 0)) or 0),
                float(value.get("height", value.get("h", 0)) or 0),
            )
        if isinstance(value, (list, tuple)) and len(value) >= 4:
            return cls(float(value[0]), float(value[1]), float(value[2]), float(value[3]))
        return cls(0, 0, 0, 0)

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)


@dataclass(frozen=True)
class ViewNode:
    address: str
    cls: str
    frame: Frame
    text: Optional[str] = None
    accessibility_id: Optional[str] = None
    hidden: bool = False
    alpha: float = 1.0
    # True only when the server explicitly tells us this is the keyWindow.
    is_key_window: bool = False
    # Window-level metadata (only meaningful on the root UIWindow node)
    window_level: Optional[float] = None
    window_class: Optional[str] = None
    contains_presented_sheet: bool = False
    presented_views: tuple["ViewNode", ...] = ()
    # Per-node visibility metadata (server-side viewport pruning)
    on_screen: Optional[bool] = None
    offscreen_child_count: int = 0
    extra: dict = field(default_factory=dict)
    children: tuple["ViewNode", ...] = ()

    @classmethod
    def from_dict(cls, raw: dict) -> "ViewNode":
        if not isinstance(raw, dict):
            return cls(address="", cls="Empty", frame=Frame(0, 0, 0, 0))

        children_raw = raw.get("children") or raw.get("subviews") or []
        children = tuple(
            cls.from_dict(c) for c in children_raw if isinstance(c, dict)
        )

        presented_raw = raw.get("presentedViews") or []
        if isinstance(presented_raw, list):
            presented_views = tuple(
                cls.from_dict(p) for p in presented_raw if isinstance(p, dict)
            )
        else:
            presented_views = ()

        # Defensive: many fields use camelCase from Swift JSONSerialization.
        cls_name = raw.get("class") or raw.get("cls")
        address = raw.get("address")
        if not cls_name and not address and not children and not presented_views:
            # Almost certainly a wrapper dict (e.g. {"windows": [...]}) leaked in.
            return cls(address="", cls="Empty", frame=Frame(0, 0, 0, 0))

        # Keys we map onto first-class fields — everything else flows into ``extra``.
        known = {
            "address", "class", "cls", "frame", "text",
            "accessibility_id", "accessibilityIdentifier",
            "hidden", "alpha",
            "isKeyWindow", "is_key_window",
            "windowLevel", "window_level",
            "windowClass", "window_class",
            "containsPresentedSheet", "contains_presented_sheet",
            "presentedViews", "presented_views",
            "onScreen", "on_screen",
            "offscreenChildCount", "offscreen_child_count",
            "children", "subviews",
        }
        extra = {k: v for k, v in raw.items() if k not in known}

        try:
            alpha = float(raw.get("alpha", 1.0) or 1.0)
        except (TypeError, ValueError):
            alpha = 1.0

        try:
            window_level_raw = raw.get("windowLevel", raw.get("window_level"))
            window_level = float(window_level_raw) if window_level_raw is not None else None
        except (TypeError, ValueError):
            window_level = None

        on_screen_raw = raw.get("onScreen", raw.get("on_screen"))
        on_screen = bool(on_screen_raw) if on_screen_raw is not None else None

        try:
            offscreen_child_count = int(
                raw.get("offscreenChildCount", raw.get("offscreen_child_count", 0)) or 0
            )
        except (TypeError, ValueError):
            offscreen_child_count = 0

        return cls(
            address=str(address or ""),
            cls=str(cls_name or "Unknown"),
            frame=Frame.from_any(raw.get("frame")),
            text=raw.get("text"),
            accessibility_id=(
                raw.get("accessibility_id") or raw.get("accessibilityIdentifier")
            ),
            hidden=bool(raw.get("hidden", False)),
            alpha=alpha,
            is_key_window=bool(raw.get("isKeyWindow") or raw.get("is_key_window") or False),
            window_level=window_level,
            window_class=raw.get("windowClass") or raw.get("window_class"),
            contains_presented_sheet=bool(
                raw.get("containsPresentedSheet")
                or raw.get("contains_presented_sheet")
                or False
            ),
            presented_views=presented_views,
            on_screen=on_screen,
            offscreen_child_count=offscreen_child_count,
            extra=extra,
            children=children,
        )

    def walk(self):
        """Depth-first traversal that ALSO yields presented sheet/modal subtrees.

        Without yielding ``presented_views`` here, any caller building an
        on-screen address whitelist (e.g. ``session.find`` cross-check) would
        miss sheet content entirely and drop legitimate matches there.
        """
        yield self
        for c in self.children:
            yield from c.walk()
        for p in self.presented_views:
            yield from p.walk()

    def is_visible(self) -> bool:
        """Heuristic visibility check. Useful for filtering search results."""
        if self.hidden:
            return False
        if self.alpha <= 0.01:
            return False
        if self.frame.width <= 0 or self.frame.height <= 0:
            return False
        return True

    def total_node_count(self) -> int:
        n = 1
        for c in self.children:
            n += c.total_node_count()
        return n


@dataclass(frozen=True)
class VCNode:
    address: str
    cls: str
    title: Optional[str] = None
    presented: Optional["VCNode"] = None
    children: tuple["VCNode", ...] = ()

    @classmethod
    def from_dict(cls, raw: dict) -> "VCNode":
        if not isinstance(raw, dict):
            return cls(address="", cls="Empty")
        children_raw = (
            raw.get("children")
            or raw.get("childViewControllers")
            or raw.get("viewControllers")
            or []
        )
        children = tuple(
            cls.from_dict(c) for c in children_raw if isinstance(c, dict)
        )
        presented_raw = raw.get("presented") or raw.get("presentedViewController")
        presented = (
            cls.from_dict(presented_raw) if isinstance(presented_raw, dict) else None
        )
        cls_name = raw.get("class") or raw.get("cls")
        address = raw.get("address")
        if not cls_name and not address and not children and not presented:
            return cls(address="", cls="Empty")
        return cls(
            address=str(address or ""),
            cls=str(cls_name or "Unknown"),
            title=raw.get("title"),
            presented=presented,
            children=children,
        )

    def walk(self):
        yield self
        if self.presented:
            yield from self.presented.walk()
        for c in self.children:
            yield from c.walk()


@dataclass(frozen=True)
class TapResult:
    target_address: Optional[str]
    method: Literal["public_api", "gesture_reflection", "coordinate", "unknown"]
    handled_by: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "TapResult":
        if not isinstance(raw, dict):
            return cls(target_address=None, method="unknown")
        method = raw.get("method") or raw.get("via") or "unknown"
        if method not in ("public_api", "gesture_reflection", "coordinate"):
            method = "unknown"
        return cls(
            target_address=raw.get("address") or raw.get("target"),
            method=method,
            handled_by=raw.get("handled_by") or raw.get("handledBy"),
            raw=raw,
        )
