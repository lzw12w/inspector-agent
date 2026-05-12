"""Typed error taxonomy for inspector operations.

Agent / actions catch these to make decisions (retry, fallback, abort).
"""
from __future__ import annotations


class InspectorError(Exception):
    code: str = "E_UNKNOWN"
    retriable: bool = False

    def __init__(self, message: str = "", *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "error": self.code,
            "message": str(self),
            "retriable": self.retriable,
            "detail": self.detail,
        }


class Unreachable(InspectorError):
    code = "E_UNREACHABLE"
    retriable = True


class Timeout(InspectorError):
    code = "E_TIMEOUT"
    retriable = True


class TargetNotFound(InspectorError):
    code = "E_TARGET_NOT_FOUND"


class TargetAmbiguous(InspectorError):
    code = "E_AMBIGUOUS"


class InvalidArgument(InspectorError):
    code = "E_INVALID_ARG"


class BuildFailed(InspectorError):
    code = "E_BUILD_FAILED"


class AppNotRunning(InspectorError):
    code = "E_APP_NOT_RUNNING"
    retriable = True


class HTTPStatusError(InspectorError):
    code = "E_HTTP_STATUS"

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}", detail={"status": status, "body": body})
        self.status = status


class InvalidResponse(InspectorError):
    code = "E_INVALID_RESPONSE"
