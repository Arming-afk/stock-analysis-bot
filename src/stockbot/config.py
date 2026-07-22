"""Config loading. All thresholds come from config.yaml; all secrets from .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional at runtime
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class ConfigError(RuntimeError):
    pass


@dataclass
class Secrets:
    fireworks_api_key: str | None = None
    webull_app_key: str | None = None
    webull_app_secret: str | None = None
    webull_account_id: str | None = None
    webull_region: str = "us"
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_contact: str = "mailto:admin@example.com"
    newsapi_key: str | None = None

    @property
    def has_fireworks(self) -> bool:
        return bool(self.fireworks_api_key)

    @property
    def has_webull(self) -> bool:
        return bool(self.webull_app_key and self.webull_app_secret)

    @property
    def has_vapid(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)


class Config:
    """Thin typed wrapper over the YAML tree with dotted lookup."""

    def __init__(self, data: dict[str, Any], secrets: Secrets, path: Path | None = None):
        self._data = data
        self.secrets = secrets
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, _MISSING)
        if value is _MISSING:
            raise ConfigError(f"missing required config key: {dotted}")
        return value

    @property
    def phase(self) -> int:
        return int(self.get("phase", 0))

    @property
    def confidence_enabled(self) -> bool:
        """Phase 0 ships without the confidence score, by design."""
        return self.phase >= 1

    @property
    def watchlist(self) -> list[str]:
        return [t.strip().upper() for t in self.get("watchlist", []) if t and t.strip()]

    def as_dict(self) -> dict[str, Any]:
        return self._data


_MISSING = object()


def load_secrets(env_path: Path | None = None) -> Secrets:
    load_dotenv(env_path or (PROJECT_ROOT / ".env"))
    return Secrets(
        fireworks_api_key=os.getenv("FIREWORKS_API_KEY") or None,
        webull_app_key=os.getenv("WEBULL_APP_KEY") or None,
        webull_app_secret=os.getenv("WEBULL_APP_SECRET") or None,
        webull_account_id=os.getenv("WEBULL_ACCOUNT_ID") or None,
        webull_region=(os.getenv("WEBULL_REGION") or "us").lower(),
        vapid_public_key=os.getenv("VAPID_PUBLIC_KEY") or None,
        vapid_private_key=os.getenv("VAPID_PRIVATE_KEY") or None,
        vapid_contact=os.getenv("VAPID_CONTACT") or "mailto:admin@example.com",
        newsapi_key=os.getenv("NEWSAPI_KEY") or None,
    )


def load_config(path: Path | str | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"config file not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping: {cfg_path}")
    return Config(data, load_secrets(), cfg_path)


def resolve_path(relative: str | Path) -> Path:
    """Resolve a config-relative path against the project root."""
    p = Path(relative)
    return p if p.is_absolute() else PROJECT_ROOT / p
