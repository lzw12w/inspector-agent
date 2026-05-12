"""State-changing interaction actions."""
from __future__ import annotations

from .base import Action, ActionResult, _node_summary, _vc_summary


class TapAction(Action):
    name = "tap"
    description = (
        "Tap a view. Provide either `address` or both `x`/`y`. "
        "If you only know the visible text, prefer `find_and_tap` instead."
    )
    idempotent = False
    schema = {"type": "object", "properties": {
        "address": {"type": "string"},
        "x": {"type": "number"},
        "y": {"type": "number"},
    }}

    def _execute(self, session, *, address=None, x=None, y=None):
        result = session.client.tap(address=address, x=x, y=y)
        session._cache.invalidate()  # state probably changed
        return {
            "target_address": result.target_address,
            "method": result.method,
            "handled_by": result.handled_by,
        }


class FindAndTapAction(Action):
    name = "find_and_tap"
    description = (
        "Find a view by text/aid/class then tap the best candidate. "
        "Fails if zero or multiple ambiguous candidates remain."
    )
    idempotent = False
    schema = {"type": "object", "properties": {
        "text": {"type": "string"},
        "accessibility_id": {"type": "string"},
        "class": {"type": "string"},
    }}

    def _execute(self, session, *, text=None, accessibility_id=None, **kwargs):
        cls = kwargs.get("class") or kwargs.get("cls")
        candidates = session.find(text=text, cls=cls,
                                  accessibility_id=accessibility_id)
        if not candidates:
            return ActionResult(ok=False, error={
                "error": "E_TARGET_NOT_FOUND",
                "message": f"no view matched text={text!r} aid={accessibility_id!r} class={cls!r}",
            })
        if len(candidates) > 1:
            sizes = [c.frame.width * c.frame.height for c in candidates]
            top = max(sizes); ratio = (sorted(sizes)[-2] / top) if top else 1
            if ratio > 0.7:
                return ActionResult(ok=False, error={
                    "error": "E_AMBIGUOUS",
                    "message": f"{len(candidates)} candidates of similar size",
                    "candidates": [_node_summary(c) for c in candidates[:5]],
                })

        target = candidates[0]
        before_vc = _vc_summary(session.vc_hierarchy())
        result = session.client.tap(address=target.address)
        session._cache.invalidate()
        after_vc = _vc_summary(session.vc_hierarchy(fresh=True))
        return {
            "tapped": _node_summary(target),
            "method": result.method,
            "handled_by": result.handled_by,
            "vc_changed": before_vc != after_vc,
        }


class ScrollAction(Action):
    name = "scroll"
    description = "Scroll the nearest UIScrollView by content offset delta."
    idempotent = False
    schema = {"type": "object", "properties": {
        "dx": {"type": "number", "default": 0},
        "dy": {"type": "number", "default": 400, "description": "Positive = scroll down."},
        "address": {"type": "string", "description": "Optional scroll-view address."},
    }}

    def _execute(self, session, *, dx=0.0, dy=400.0, address=None):
        result = session.client.scroll(dx=dx, dy=dy, address=address)
        session._cache.invalidate("view_hierarchy")
        return result


class SwipeAction(Action):
    name = "swipe"
    description = "Best-effort swipe / pan. Falls back to scroll-view backed motion."
    idempotent = False
    schema = {"type": "object", "properties": {
        "address": {"type": "string"},
        "start_x": {"type": "number"}, "start_y": {"type": "number"},
        "end_x": {"type": "number"}, "end_y": {"type": "number"},
        "dx": {"type": "number"}, "dy": {"type": "number"},
        "duration": {"type": "number", "default": 0.25},
    }}

    def _execute(self, session, **kwargs):
        result = session.client.swipe(**kwargs)
        session._cache.invalidate()
        return result


class InputTextAction(Action):
    name = "input_text"
    description = "Type text into the current first responder."
    idempotent = False
    schema = {"type": "object", "properties": {
        "text": {"type": "string"},
        "submit": {"type": "boolean", "default": False},
        "clear": {"type": "boolean", "default": False},
    }, "required": ["text"]}

    def _execute(self, session, *, text, submit=False, clear=False):
        return session.client.input_text(text, submit=submit, clear=clear)


class DismissAction(Action):
    name = "dismiss"
    description = "Dismiss keyboard or topmost modal."
    idempotent = False
    schema = {"type": "object", "properties": {}}

    def _execute(self, session, **kwargs):
        result = session.client.dismiss()
        session._cache.invalidate()
        return result


class BackAction(Action):
    name = "back"
    description = "Pop navigation stack or dismiss top VC."
    idempotent = False
    schema = {"type": "object", "properties": {}}

    def _execute(self, session, **kwargs):
        result = session.client.back()
        session._cache.invalidate()
        return result


class SwitchTabAction(Action):
    name = "switch_tab"
    description = "Switch the active UITabBarController tab by index or title."
    idempotent = False
    schema = {"type": "object", "properties": {
        "index": {"type": "integer"},
        "title": {"type": "string"},
    }}

    def _execute(self, session, *, index=None, title=None):
        result = session.client.switch_tab(index=index, title=title)
        session._cache.invalidate()
        return result


class OpenURLAction(Action):
    name = "open_url"
    description = (
        "Open a route URL inside the app, e.g. //commerce/member_page. "
        "Avoid routes containing wipe/clear/logout/delete unless explicitly required."
    )
    idempotent = False
    schema = {"type": "object", "properties": {
        "url": {"type": "string"},
        "animated": {"type": "boolean", "default": True},
    }, "required": ["url"]}

    def _execute(self, session, *, url, animated=True):
        # Soft guardrail: warn (not block) on dangerous keywords.
        lower = url.lower()
        warning = None
        for bad in ("wipe", "clear_cache", "logout", "delete_account",
                    "internal_debug", "hard_reset"):
            if bad in lower:
                warning = f"route contains sensitive keyword '{bad}'"
                break
        result = session.client.open_url(url, animated=animated)
        session._cache.invalidate()
        out = ActionResult(ok=True, data={"opened": url, "result": result})
        if warning:
            out.notes = warning
        return out


class ViewModifyAction(Action):
    name = "view_modify"
    description = (
        "Temporarily modify a view property (e.g. hidden=true) at runtime. "
        "Modifications are tracked and rolled back at session end."
    )
    idempotent = False
    schema = {"type": "object", "properties": {
        "address": {"type": "string"},
        "property": {"type": "string", "description": "e.g. hidden, alpha, backgroundColor"},
        "value": {"description": "New value (bool/number/string)."},
    }, "required": ["address", "property", "value"]}

    def _execute(self, session, *, address, value, **kwargs):
        prop = kwargs.get("property")
        return session.modify(address, prop, value)
