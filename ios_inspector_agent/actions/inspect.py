"""Read-only inspection actions."""
from __future__ import annotations

import time
from typing import Any

from .base import Action, ActionResult, _node_summary, _vc_summary


class PingAction(Action):
    name = "ping"
    description = "Health check the SAInspector HTTP server. Use first when uncertain about connectivity."
    idempotent = True
    schema = {"type": "object", "properties": {}}

    def _execute(self, session, **kwargs):
        return session.client.ping()


class VCHierarchyAction(Action):
    name = "vc_hierarchy"
    description = "Get the current ViewController hierarchy. Use to know which page is shown."
    idempotent = True
    schema = {"type": "object", "properties": {
        "fresh": {"type": "boolean", "description": "Bypass cache.", "default": False},
    }}

    def _execute(self, session, *, fresh: bool = False):
        return _vc_summary(session.vc_hierarchy(fresh=fresh))


# Minimum number of nodes a "real" main-window subtree should contain.
# Anything below this is almost always a transient state (mid-transition,
# pre-viewDidAppear, app just launched, etc.).
_STABILITY_MIN_NODES = 4
# Number of stability check rounds.
_STABILITY_MAX_ROUNDS = 4
# Delay between rounds.
_STABILITY_RETRY_DELAY = 0.25


class ViewHierarchyAction(Action):
    name = "view_hierarchy"
    description = (
        "Get the current view tree as nested JSON. Each node has address, class, "
        "frame, text, accessibility id. Auto-retries briefly if the snapshot "
        "appears unstable (mid-animation / pre-viewDidAppear)."
    )
    idempotent = True
    schema = {"type": "object", "properties": {
        "depth": {"type": "integer", "minimum": 1, "maximum": 30, "default": 6},
        "fresh": {"type": "boolean", "default": False},
        "include_hidden": {"type": "boolean", "default": False},
        "on_screen_only": {
            "type": "boolean",
            "description": (
                "Prune views whose frame falls outside the visible viewport "
                "(or is clipped by an ancestor's clipsToBounds / scroll content area). "
                "Default true. Set false to inspect off-screen siblings."
            ),
            "default": True,
        },
        "address": {
            "type": "string",
            "description": (
                "Optional hex address (e.g. 0x10cf77800). When provided, "
                "the tree is rooted at that view rather than the key window. "
                "Useful for drilling into a specific cell or container without "
                "fetching the whole hierarchy. Stability gate is skipped."
            ),
        },
        "stability": {
            "type": "boolean",
            "description": "Wait for two consecutive snapshots to agree before returning.",
            "default": True,
        },
    }}

    def _execute(self, session, *, depth: int = 6, fresh: bool = False,
                 include_hidden: bool = False,
                 on_screen_only: bool = True,
                 address: str = None,
                 stability: bool = True) -> Any:
        if address:
            # Targeted subtree: skip stability gate (we already have a focused
            # subtree, instability is a non-issue) and skip on-screen pruning
            # by default so the agent can explore off-screen siblings.
            node = session.client.view_subtree(
                address=address,
                depth=depth,
                include_hidden=include_hidden,
                on_screen_only=on_screen_only,
            )
        else:
            node = self._snapshot_stable(
                session, depth=depth, fresh=fresh,
                include_hidden=include_hidden,
                on_screen_only=on_screen_only,
                stability=stability,
            )

        def to_dict(n, d):
            out = _node_summary(n)
            if d > 0 and n.children:
                out["children"] = [to_dict(c, d - 1) for c in n.children]
            return out

        result = to_dict(node, depth)
        # Surface diagnostics so the agent can decide whether to recover.
        try:
            total = node.total_node_count()
        except AttributeError:
            total = None
        if total is not None:
            meta = {
                "total_nodes": total,
                "is_key_window": getattr(node, "is_key_window", False),
                "stability_used": stability,
                "on_screen_only": on_screen_only,
                "contains_presented_sheet": getattr(
                    node, "contains_presented_sheet", False
                ),
            }
            window_class = getattr(node, "window_class", None)
            if window_class:
                meta["window_class"] = window_class
            window_level = getattr(node, "window_level", None)
            if window_level is not None:
                meta["window_level"] = window_level
            presented = getattr(node, "presented_views", ()) or ()
            if presented:
                meta["presented_view_count"] = len(presented)
                # Surface presented sheet/modal subtree as part of the result so
                # the LLM doesn't have to ask twice. Each presented view is a
                # full ViewNode subtree rooted at the presented VC's .view.
                def _summarize_presented(n, d):
                    s = _node_summary(n)
                    if d > 0 and n.children:
                        s["children"] = [_summarize_presented(c, d - 1) for c in n.children]
                    return s
                result["presented_views"] = [
                    _summarize_presented(p, depth) for p in presented
                ]
            offscreen = getattr(node, "offscreen_child_count", 0)
            if offscreen:
                meta["offscreen_child_count"] = offscreen
            # VC->view fallback markers (set by client.view_subtree when the
            # caller passed a UIViewController address by mistake).
            extra = getattr(node, "extra", {}) or {}
            if extra.get("resolved_from_view_controller"):
                meta["resolved_from_view_controller"] = True
                if extra.get("resolved_view_address"):
                    meta["resolved_view_address"] = extra["resolved_view_address"]
                if extra.get("view_controller_class"):
                    meta["view_controller_class"] = extra["view_controller_class"]
                if extra.get("resolve_hint"):
                    meta["hint"] = extra["resolve_hint"]
            result["_meta"] = meta
        return result

    @staticmethod
    def _snapshot_stable(session, *, depth, fresh, include_hidden,
                         on_screen_only, stability):
        """Return a hierarchy snapshot.

        If ``stability`` is True, take up to N snapshots and return the first
        one whose node-count matches the previous round AND meets a minimum
        size threshold. This avoids returning empty/transient trees mid-
        animation.
        """
        kwargs = {
            "depth": depth,
            "include_hidden": include_hidden,
            "on_screen_only": on_screen_only,
        }
        # Bypass cache for the first snapshot to guarantee freshness; then
        # always read fresh inside the stability loop too.
        last_count = -1
        last_node = None
        rounds = _STABILITY_MAX_ROUNDS if stability else 1
        for i in range(rounds):
            node = session.view_hierarchy(fresh=True if i > 0 else fresh, **kwargs)
            count = node.total_node_count() if hasattr(node, "total_node_count") else 1
            if not stability:
                return node
            # Decisive cases:
            #   - this is the first round => remember and try again only if
            #     it looks suspicious (too few nodes)
            #   - subsequent rounds => agree with previous AND above floor
            if i == 0:
                last_count, last_node = count, node
                if count >= _STABILITY_MIN_NODES:
                    # Looks reasonable already; one more confirmation round.
                    pass
            else:
                if count == last_count and count >= _STABILITY_MIN_NODES:
                    return node
                last_count, last_node = count, node
            time.sleep(_STABILITY_RETRY_DELAY)
        # Fall back to the last observed snapshot rather than failing — the
        # caller (agent) gets _meta.total_nodes and can decide what to do.
        return last_node if last_node is not None else node


class FindViewAction(Action):
    name = "find_view"
    description = (
        "Find views matching text / class / accessibility id. Returns a ranked list of candidates. "
        "Use this before tapping by text to inspect what would actually be hit."
    )
    idempotent = True
    schema = {"type": "object", "properties": {
        "text": {"type": "string"},
        "class": {"type": "string", "description": "Substring match on UIKit class name (e.g. UILabel, UIButton)."},
        "accessibility_id": {"type": "string"},
        "max_results": {"type": "integer", "default": 8, "maximum": 30},
        "visible_only": {
            "type": "boolean",
            "description": (
                "Filter out reuse-pool placeholders and views that aren't in "
                "the on-screen tree. Default true. Set false to also surface "
                "off-screen / queued cells (rarely needed)."
            ),
            "default": True,
        },
    }}

    def _execute(self, session, *, text=None, accessibility_id=None,
                 max_results: int = 8, visible_only: bool = True,
                 **kwargs) -> Any:
        cls = kwargs.get("class") or kwargs.get("cls")
        candidates = session.find(
            text=text, cls=cls, accessibility_id=accessibility_id,
            visible_only=visible_only,
        )
        # Rank: actually-on-screen first, then larger area, then top-left wins.
        def _rank(n):
            on_screen_score = 0 if getattr(n, "on_screen", None) is not False else 1
            in_window_score = 0 if (n.frame.x >= 0 and n.frame.y >= 0) else 1
            area = n.frame.width * n.frame.height
            return (on_screen_score, in_window_score, -area, n.frame.y, n.frame.x)
        candidates.sort(key=_rank)
        return {
            "count": len(candidates),
            "results": [_node_summary(n) for n in candidates[:max_results]],
            "filtered_visible_only": visible_only,
        }


class ViewInspectAction(Action):
    name = "view_inspect"
    description = "Get full property dump of a single view by address."
    idempotent = True
    schema = {"type": "object",
              "properties": {"address": {"type": "string"}},
              "required": ["address"]}

    def _execute(self, session, *, address):
        return session.client.view_inspect(address)


class ScreenshotAction(Action):
    name = "screenshot"
    description = "Capture the screen and save it under the run workdir. Returns the saved path."
    idempotent = True
    schema = {"type": "object", "properties": {
        "label": {"type": "string", "default": "snap"},
    }}

    def _execute(self, session, *, label: str = "snap"):
        path = session.screenshot(label=label)
        if not path:
            return ActionResult(ok=False, error={"error": "E_NO_IMAGE", "message": "screenshot returned no image"})
        return ActionResult(ok=True, data={"path": str(path)},
                            artifacts=[str(path)])


class AppStateAction(Action):
    name = "app_state"
    description = "Foreground/background, account, network snapshot."
    idempotent = True
    schema = {"type": "object", "properties": {}}

    def _execute(self, session, **kwargs):
        return session.client.app_state()


class NetworkLogAction(Action):
    name = "network_log"
    description = "Recent network requests captured by the inspector."
    idempotent = True
    schema = {"type": "object", "properties": {
        "limit": {"type": "integer", "default": 20, "maximum": 200},
    }}

    def _execute(self, session, *, limit: int = 20):
        return session.client.network_log(limit=limit)


class ConsoleLogAction(Action):
    name = "console_log"
    description = "Recent ALog/ContextLogger output. Not a raw NSLog pipe."
    idempotent = True
    schema = {"type": "object", "properties": {
        "limit": {"type": "integer", "default": 50, "maximum": 500},
    }}

    def _execute(self, session, *, limit: int = 50):
        return session.client.console_log(limit=limit)
