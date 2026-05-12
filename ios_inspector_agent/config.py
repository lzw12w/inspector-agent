"""Configuration. Reads env first, then ~/.ios-inspector/config.toml if present."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # py3.11+
except ImportError:                   # pragma: no cover
    tomllib = None


CONFIG_PATH = Path.home() / ".ios-inspector" / "config.toml"


@dataclass
class Config:
    inspector_host: str = "localhost"
    inspector_port: int = 8765
    inspector_timeout: float = 10.0

    llm_provider: str = "anthropic"
    llm_model: str = "MiMo-V2.5-Pro"
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None

    workdir_root: Path = field(default_factory=lambda: Path.home() / ".ios-inspector" / "runs")
    confirm_for: set[str] = field(default_factory=lambda: {"open_url", "view_modify"})

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        # 1. file
        if CONFIG_PATH.exists() and tomllib is not None:
            try:
                data = tomllib.loads(CONFIG_PATH.read_text())
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:
                pass
        # 2. env overrides
        cfg.inspector_host = os.environ.get("INSPECTOR_HOST", cfg.inspector_host)
        cfg.inspector_port = int(os.environ.get("INSPECTOR_PORT", cfg.inspector_port))
        cfg.llm_model = os.environ.get("ANTHROPIC_MODEL", cfg.llm_model)
        cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", cfg.anthropic_api_key)
        cfg.anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL", cfg.anthropic_base_url)
        return cfg
