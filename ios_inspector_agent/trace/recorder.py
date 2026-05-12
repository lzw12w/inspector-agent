"""Append-only step recorder. One JSONL line per agent step."""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any


class Recorder:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.path = workdir / "trace.jsonl"
        self._fp = self.path.open("a", encoding="utf-8")

    def log(self, kind: str, payload: dict):
        record = {
            "ts": _dt.datetime.now().isoformat(timespec="milliseconds"),
            "kind": kind,
            **payload,
        }
        self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fp.flush()

    def close(self):
        try:
            self._fp.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
