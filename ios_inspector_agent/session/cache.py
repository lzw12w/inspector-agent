"""Tiny TTL cache so repeated reads inside a step don't hammer the server."""
from __future__ import annotations

import time
from typing import Any


class TTLCache:
    def __init__(self, default_ttl: float = 2.0):
        self.default_ttl = default_ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        expires, value = item
        if time.time() > expires:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float | None = None):
        ttl = self.default_ttl if ttl is None else ttl
        self._store[key] = (time.time() + ttl, value)

    def invalidate(self, prefix: str | None = None):
        if prefix is None:
            self._store.clear()
            return
        for k in [k for k in self._store if k.startswith(prefix)]:
            self._store.pop(k, None)
