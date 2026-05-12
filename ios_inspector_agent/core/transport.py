"""HTTP transport with retries, timeouts, idempotency awareness."""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import HTTPStatusError, InvalidResponse, Timeout, Unreachable


_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Transport:
    def __init__(self, host: str = "localhost", port: int = 8765, timeout: float = 10.0):
        if host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"Refusing non-local inspector host {host!r}; "
                f"use port forwarding (iproxy) for real devices."
            )
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def get(self, path: str, params: dict | None = None, *, retries: int = 3) -> Any:
        url = self._build_url(path, params)
        return self._do(urllib.request.Request(url, method="GET"), retries=retries)

    def post(self, path: str, body: dict | None = None, *,
             idempotent: bool = False, retries: int | None = None) -> Any:
        url = self._build_url(path, None)
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        # Non-idempotent POSTs (tap, swipe, etc.) MUST NOT auto-retry.
        if retries is None:
            retries = 2 if idempotent else 0
        return self._do(req, retries=retries)

    def _build_url(self, path: str, params: dict | None) -> str:
        url = self.base_url + path
        if params:
            normalized = {}
            for k, v in params.items():
                if v is None or v == "":
                    continue
                if isinstance(v, bool):
                    normalized[k] = "true" if v else "false"
                elif isinstance(v, (list, tuple)):
                    if v:
                        normalized[k] = ",".join(str(x) for x in v)
                else:
                    normalized[k] = str(v)
            if normalized:
                url += "?" + urllib.parse.urlencode(normalized)
        return url

    def _do(self, req: urllib.request.Request, *, retries: int) -> Any:
        last_exc: Exception | None = None
        backoff = 0.5
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read().decode("utf-8", errors="replace")
                    if not payload:
                        return {}
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError as e:
                        raise InvalidResponse(f"Non-JSON response: {payload[:200]}") from e
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                # 4xx is not retriable
                if 400 <= e.code < 500:
                    raise HTTPStatusError(e.code, body)
                last_exc = HTTPStatusError(e.code, body)
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", e)
                if isinstance(reason, socket.timeout):
                    last_exc = Timeout(f"Request to {req.full_url} timed out after {self.timeout}s")
                else:
                    last_exc = Unreachable(
                        f"Cannot reach inspector at {self.base_url}: {reason}",
                        detail={"hint": f"Run `iproxy {self.port} {self.port}` for a real device."},
                    )
            except socket.timeout:
                last_exc = Timeout(f"Socket timeout after {self.timeout}s")

            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2
        assert last_exc is not None
        raise last_exc
