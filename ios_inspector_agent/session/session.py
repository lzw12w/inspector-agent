"""InspectorSession — stateful wrapper around InspectorClient.

Responsibilities:
- cache short-lived hierarchies (TTL)
- archive screenshots into the run workdir
- track view_modify ops and rollback on exit
- offer a high-level `find` over the cached view tree
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core import InspectorClient, ViewNode, VCNode
from .cache import TTLCache


@dataclass
class Modification:
    address: str
    prop: str
    original: object


class InspectorSession:
    def __init__(self, client: InspectorClient, workdir: Path):
        self.client = client
        self.workdir = workdir
        self._cache = TTLCache(default_ttl=2.0)
        self._undo_stack: list[Modification] = []
        self._screen_counter = 0

    # ---- snapshots ------------------------------------------------------
    def view_hierarchy(self, depth: int = 8, *, fresh: bool = False,
                       include_hidden: bool = False,
                       on_screen_only: bool = True) -> ViewNode:
        key = f"view_hierarchy:{depth}:{int(include_hidden)}:{int(on_screen_only)}"
        if not fresh:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        node = self.client.view_hierarchy(
            depth=depth,
            include_hidden=include_hidden,
            on_screen_only=on_screen_only,
        )
        self._cache.set(key, node)
        return node

    def vc_hierarchy(self, *, fresh: bool = False) -> VCNode:
        if not fresh:
            cached = self._cache.get("vc_hierarchy")
            if cached is not None:
                return cached
        node = self.client.vc_hierarchy()
        self._cache.set("vc_hierarchy", node)
        return node

    def screenshot(self, label: str = "snap") -> Path:
        self._screen_counter += 1
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)[:40] or "snap"
        ts = _dt.datetime.now().strftime("%H%M%S")
        out = self.workdir / "screens" / f"{self._screen_counter:03d}_{ts}_{safe}.jpg"
        result = self.client.screenshot(output=out)
        if "saved_to" not in result:
            return Path()  # screenshot returned no image
        return out

    # ---- search ---------------------------------------------------------
    def find(self, *, text: Optional[str] = None, cls: Optional[str] = None,
             accessibility_id: Optional[str] = None,
             depth: int = 12,
             prefer_visible: bool = True,
             visible_only: bool = True) -> list[ViewNode]:
        """Search both via server endpoint and local tree (catches synonyms).

        ``visible_only`` (default True) drops reuse-pool zero-frame placeholders
        and entries that are neither in the on-screen tree nor have a
        meaningful frame. Set False to include the full search index.

        ``prefer_visible`` is the legacy soft filter (hidden / non-zero size).
        """
        results: list[ViewNode] = []
        try:
            results = self.client.view_search(
                text=text, cls=cls, accessibility_id=accessibility_id
            )
        except Exception:
            pass

        if not results:
            tree = self.view_hierarchy(depth=depth)
            for node in tree.walk():
                if cls and cls.lower() not in node.cls.lower():
                    continue
                if text and (not node.text or text.lower() not in node.text.lower()):
                    continue
                if accessibility_id and node.accessibility_id != accessibility_id:
                    continue
                results.append(node)

        if prefer_visible:
            results = [n for n in results
                       if not n.hidden and n.frame.width > 0 and n.frame.height > 0]

        if visible_only and results:
            # Cross-check against the on-screen tree: any address that does not
            # appear in the visible-window subtree is treated as a reuse-pool
            # match and dropped. This is the only way to filter out things like
            # 30 UILabels with frame=(0,0,428,848) returned by view_search.
            try:
                onscreen_tree = self.view_hierarchy(depth=depth, on_screen_only=True)
                onscreen_addrs = {n.address for n in onscreen_tree.walk() if n.address}
            except Exception:
                onscreen_addrs = set()
            if onscreen_addrs:
                filtered = [n for n in results if n.address in onscreen_addrs]
                # Don't reduce results to empty if cross-check is overly strict;
                # fall back to soft-visible heuristic instead.
                if filtered:
                    results = filtered
                else:
                    # Heuristic: drop frame-at-origin labels that look like reuse
                    # pool placeholders (zero x AND zero y AND non-trivial size
                    # is highly suspicious for non-window views).
                    results = [
                        n for n in results
                        if not (n.frame.x == 0 and n.frame.y == 0 and (n.text is None or n.text == ""))
                    ]
        return results

    # ---- modify with undo ----------------------------------------------
    def modify(self, address: str, prop: str, value):
        try:
            inspect = self.client.view_inspect(address)
            original = inspect.get(prop) if isinstance(inspect, dict) else None
        except Exception:
            original = None
        result = self.client.view_modify(address, prop, value)
        self._undo_stack.append(Modification(address, prop, original))
        self._cache.invalidate("view_hierarchy")
        return result

    def rollback_all(self) -> int:
        n = 0
        while self._undo_stack:
            mod = self._undo_stack.pop()
            try:
                self.client.view_modify(mod.address, mod.prop, mod.original)
                n += 1
            except Exception:
                pass
        return n

    # ---- lifecycle ------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.rollback_all()
        return False
