"""High-level client. Methods return dataclasses, raise typed errors."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from .errors import InvalidArgument, InvalidResponse
from .models import TapResult, VCNode, ViewNode
from .transport import Transport


# ---------------------------------------------------------------------------
# Internal helpers — picking the "main" window from a multi-window response.
# ---------------------------------------------------------------------------

# Window classes we never want to mistake for the app's main window.
# These are well-known UIKit-internal windows that frequently float on top.
_OVERLAY_WINDOW_CLASSES = {
    "UITextEffectsWindow",
    "UIRemoteKeyboardWindow",
    "_UIAlertControllerShimPresenterWindow",
    "UITransitionView",
}


def _node_area(node: dict) -> float:
    if not isinstance(node, dict):
        return 0.0
    frame = node.get("frame") or {}
    try:
        return float(frame.get("width", 0) or 0) * float(frame.get("height", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _node_subview_count(node: dict) -> int:
    if not isinstance(node, dict):
        return 0
    kids = node.get("subviews") or node.get("children") or []
    return len(kids) if isinstance(kids, list) else 0


def _pick_main_window(windows: list[dict]) -> dict:
    """Pick the most relevant window from the server's window list.

    The server returns *all* connected UIWindows (app main window, keyboard
    window, system overlays, etc.). Picking the wrong one makes the agent
    think the screen is empty — or worse, miss a presented sheet that has
    actually replaced the visible UI.

    Selection priority:
      1. window whose subtree contains a presented sheet/modal — that's
         what the user is actually looking at
      2. server-provided ``isKeyWindow`` flag (newer protocol)
      3. exclude well-known overlay window classes
      4. window whose subtree has the largest visible footprint
      5. fall back to first window
    """
    if not windows:
        raise InvalidResponse("server returned empty windows list")

    # 1. window owning a presented sheet/modal trumps everything else
    for w in windows:
        if isinstance(w, dict) and w.get("containsPresentedSheet"):
            return w
    for w in windows:
        if isinstance(w, dict):
            presented = w.get("presentedViews")
            if isinstance(presented, list) and presented:
                return w

    # 2. explicit isKeyWindow hint from server
    for w in windows:
        if isinstance(w, dict) and w.get("isKeyWindow"):
            return w

    # 3. exclude obvious overlays unless they're all we have
    candidates = [
        w for w in windows
        if isinstance(w, dict)
        and str(w.get("class", "")) not in _OVERLAY_WINDOW_CLASSES
    ]
    if not candidates:
        candidates = [w for w in windows if isinstance(w, dict)]
    if not candidates:
        raise InvalidResponse("no valid window dicts in response")

    # 4. footprint + subview-count heuristic
    return max(candidates, key=lambda w: (_node_area(w), _node_subview_count(w)))


def _normalize_hierarchy_response(raw, kind: str) -> dict:
    """Coerce server response into a single root node dict.

    Supports three protocol shapes:
      - ``{"windows": [...]}``           — current SAInspector
      - ``{"root": {...}}``              — older / alt servers
      - bare node dict                   — defensive fallback
    """
    if not isinstance(raw, dict):
        raise InvalidResponse(f"{kind} returned non-object: {type(raw).__name__}")

    # New canonical: {"windows": [...]}
    if "windows" in raw:
        wins = raw.get("windows") or []
        if not isinstance(wins, list):
            raise InvalidResponse(f"{kind}.windows is not a list")
        if not wins:
            raise InvalidResponse(
                f"{kind}: server reported no visible windows "
                "(app may be backgrounded or still launching)"
            )
        return _pick_main_window(wins)

    # Legacy: {"root": {...}}
    if "root" in raw and isinstance(raw["root"], dict):
        return raw["root"]

    # Bare node — only treat as such if it has structural fields
    if "class" in raw or "address" in raw or "subviews" in raw or "children" in raw:
        return raw

    raise InvalidResponse(
        f"{kind}: response has no 'windows', 'root', or node fields. keys={list(raw.keys())[:8]}"
    )


class InspectorClient:
    def __init__(self, host: str = "localhost", port: int = 8765, timeout: float = 10.0):
        self._t = Transport(host=host, port=port, timeout=timeout)

    @property
    def base_url(self) -> str:
        return self._t.base_url

    # ---- health / state -------------------------------------------------
    def ping(self) -> dict:
        return self._t.get("/api/ping")

    def app_state(self) -> dict:
        return self._t.get("/api/app_state")

    def memory_usage(self) -> dict:
        return self._t.get("/api/memory_usage")

    # ---- structure ------------------------------------------------------
    def view_hierarchy(
        self,
        depth: int = 8,
        include_hidden: bool = False,
        on_screen_only: bool = True,
    ) -> ViewNode:
        raw = self._t.get(
            "/api/view_hierarchy",
            {
                "depth": depth,
                "include_hidden": include_hidden,
                "on_screen_only": on_screen_only,
            },
        )
        node = _normalize_hierarchy_response(raw, "view_hierarchy")
        return ViewNode.from_dict(node)

    def view_hierarchy_raw(
        self,
        depth: int = 8,
        include_hidden: bool = False,
        on_screen_only: bool = True,
    ) -> dict:
        """Raw response with all windows. Useful for diagnostics."""
        return self._t.get(
            "/api/view_hierarchy",
            {
                "depth": depth,
                "include_hidden": include_hidden,
                "on_screen_only": on_screen_only,
            },
        )

    def vc_hierarchy(self) -> VCNode:
        raw = self._t.get("/api/vc_hierarchy")
        # vc_hierarchy returns {"windows": [{"rootViewController": {...}, ...}]}
        if isinstance(raw, dict) and "windows" in raw:
            wins = raw.get("windows") or []
            if not wins:
                raise InvalidResponse(
                    "vc_hierarchy: no windows with rootViewController returned"
                )
            # pick first window that actually has a rootViewController
            chosen = None
            for w in wins:
                if isinstance(w, dict) and isinstance(w.get("rootViewController"), dict):
                    chosen = w["rootViewController"]
                    break
            if chosen is None:
                # fall back to first window dict so we at least surface something
                chosen = wins[0] if isinstance(wins[0], dict) else {}
            return VCNode.from_dict(chosen)
        if isinstance(raw, dict) and "root" in raw:
            return VCNode.from_dict(raw["root"])
        return VCNode.from_dict(raw if isinstance(raw, dict) else {})

    def view_inspect(self, address: str) -> dict:
        if not address:
            raise InvalidArgument("address is required")
        return self._t.get("/api/view_inspect", {"address": address})

    def view_subtree(
        self,
        address: str,
        depth: int = 8,
        include_hidden: bool = False,
        on_screen_only: bool = False,
    ) -> ViewNode:
        """Pull the view subtree rooted at ``address``.

        Use this when ``view_hierarchy`` is too coarse or hits its depth
        cap before reaching the area you care about.
        """
        if not address:
            raise InvalidArgument("address is required")
        raw = self._t.get(
            "/api/view_subtree",
            {
                "address": address,
                "depth": depth,
                "include_hidden": include_hidden,
                "on_screen_only": on_screen_only,
            },
        )
        if not isinstance(raw, dict):
            raise InvalidResponse(
                f"view_subtree returned non-object: {type(raw).__name__}"
            )
        node = raw.get("root")
        if not isinstance(node, dict):
            raise InvalidResponse(
                f"view_subtree response missing 'root'. keys={list(raw.keys())[:8]}"
            )
        # Server-side VC->view fallback: when the address belonged to a
        # UIViewController, the server transparently walked to vc.view and
        # marks the response. Stash the markers on the node so the action
        # layer can surface them in _meta and the agent can update its
        # mental model (and avoid passing VC addresses next time).
        if raw.get("resolvedFromViewController"):
            # ``ViewNode`` is a frozen dataclass — re-build with extra mutated.
            base = ViewNode.from_dict(node)
            extra = dict(base.extra)
            extra["resolved_from_view_controller"] = True
            if raw.get("resolvedViewAddress"):
                extra["resolved_view_address"] = raw["resolvedViewAddress"]
            if raw.get("viewControllerClass"):
                extra["view_controller_class"] = raw["viewControllerClass"]
            if raw.get("hint"):
                extra["resolve_hint"] = raw["hint"]
            from dataclasses import replace
            return replace(base, extra=extra)
        return ViewNode.from_dict(node)

    def view_search(self, *, cls: Optional[str] = None, text: Optional[str] = None,
                    accessibility_id: Optional[str] = None,
                    tag: Optional[int] = None) -> list[ViewNode]:
        params: dict = {}
        if cls: params["class"] = cls
        if text: params["text"] = text
        if accessibility_id: params["accessibility_id"] = accessibility_id
        if tag is not None: params["tag"] = tag
        raw = self._t.get("/api/view_search", params)
        items = raw.get("results") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise InvalidResponse("view_search did not return a list")
        return [ViewNode.from_dict(it) for it in items if isinstance(it, dict)]

    def screenshot(self, *, quality: float = 0.7, output: Optional[Path] = None) -> dict:
        raw = self._t.get("/api/screenshot", {"quality": quality})
        if output and isinstance(raw, dict) and "base64" in raw:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(base64.b64decode(raw["base64"]))
            return {"saved_to": str(output),
                    "width": raw.get("width"), "height": raw.get("height")}
        return raw

    # ---- interaction ----------------------------------------------------
    def tap(self, *, address: Optional[str] = None,
            x: Optional[float] = None, y: Optional[float] = None,
            full: bool = False) -> TapResult:
        body = self._point_body(address, x, y)
        body["compact"] = not full
        raw = self._t.post("/api/tap", body, idempotent=False)
        return TapResult.from_dict(raw)

    def long_press(self, *, address: Optional[str] = None,
                   x: Optional[float] = None, y: Optional[float] = None,
                   duration: float = 0.6) -> dict:
        body = self._point_body(address, x, y)
        body["duration"] = duration
        return self._t.post("/api/long_press", body, idempotent=False)

    def swipe(self, *, address: Optional[str] = None,
              start_x: Optional[float] = None, start_y: Optional[float] = None,
              end_x: Optional[float] = None, end_y: Optional[float] = None,
              dx: Optional[float] = None, dy: Optional[float] = None,
              duration: float = 0.25) -> dict:
        body: dict = {"duration": duration}
        if address: body["address"] = address
        for k, v in (("start_x", start_x), ("start_y", start_y),
                     ("end_x", end_x), ("end_y", end_y),
                     ("dx", dx), ("dy", dy)):
            if v is not None:
                body[k] = v
        return self._t.post("/api/swipe", body, idempotent=False)

    def scroll(self, *, dx: float = 0.0, dy: float = 400.0,
               address: Optional[str] = None, animated: bool = True) -> dict:
        body: dict = {"dx": dx, "dy": dy, "animated": animated}
        if address:
            body["address"] = address
        return self._t.post("/api/scroll", body, idempotent=False)

    def input_text(self, text: str, *, submit: bool = False, clear: bool = False) -> dict:
        return self._t.post(
            "/api/input_text",
            {"text": text, "submit": submit, "clear_existing": clear},
            idempotent=False,
        )

    def dismiss(self, animated: bool = True) -> dict:
        return self._t.post("/api/dismiss", {"animated": animated}, idempotent=False)

    def back(self, animated: bool = True) -> dict:
        return self._t.post("/api/back", {"animated": animated}, idempotent=False)

    def switch_tab(self, *, index: Optional[int] = None,
                   title: Optional[str] = None) -> dict:
        body: dict = {}
        if index is not None: body["index"] = index
        if title: body["title"] = title
        if not body:
            raise InvalidArgument("provide --index or --title")
        return self._t.post("/api/switch_tab", body, idempotent=False)

    def open_url(self, url: str, animated: bool = True) -> dict:
        if not url:
            raise InvalidArgument("url is required")
        return self._t.post("/api/open_url", {"url": url, "animated": animated})

    # ---- modify (with caller-managed undo) ------------------------------
    def view_modify(self, address: str, prop: str, value) -> dict:
        if not address or not prop:
            raise InvalidArgument("address and property are required")
        return self._t.post("/api/view_modify",
                            {"address": address, "property": prop, "value": value})

    # ---- read state -----------------------------------------------------
    def network_log(self, limit: int = 20) -> dict:
        return self._t.get("/api/network_log", {"limit": limit})

    def console_log(self, limit: int = 50) -> dict:
        return self._t.get("/api/console_log", {"limit": limit})

    def user_defaults(self, *, prefix: Optional[str] = None,
                      keys: Optional[list[str]] = None, limit: int = 50) -> dict:
        params: dict = {"limit": limit}
        if prefix: params["prefix"] = prefix
        if keys: params["keys"] = keys
        return self._t.get("/api/user_defaults", params)

    def ab_experiments(self, *, keys: Optional[list[str]] = None, limit: int = 30) -> dict:
        params: dict = {"limit": limit}
        if keys: params["keys"] = keys
        return self._t.get("/api/ab_experiments", params)

    def feature_flags(self, *, keys: Optional[list[str]] = None, limit: int = 30) -> dict:
        params: dict = {"limit": limit}
        if keys: params["keys"] = keys
        return self._t.get("/api/feature_flags", params)

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def _point_body(address, x, y) -> dict:
        if not address and (x is None or y is None):
            raise InvalidArgument("provide address or both x/y")
        body: dict = {}
        if address:
            body["address"] = address
        if x is not None and y is not None:
            body["x"] = x
            body["y"] = y
        return body
