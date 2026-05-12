"""Per-run working directory: screens, traces, audit logs."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path


def make_run_workdir(root: Path | None = None) -> Path:
    root = root or (Path.home() / ".ios-inspector" / "runs")
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    workdir = root / f"run_{ts}"
    (workdir / "screens").mkdir(parents=True, exist_ok=True)
    return workdir
