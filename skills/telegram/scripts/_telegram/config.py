"""Configuration and paths for Telegram skill."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("TELEGRAM_HOME", os.path.expanduser("~/.local/share/telegram")))
CONFIG_DIR = HOME / "config"
CACHE_DIR = HOME / "cache"
CONFIG_FILE = CONFIG_DIR / "session.json"

WRITE_ALLOWED_ENV = "TELEGRAM_WRITE_ALLOWED"


def ensure_dirs() -> None:
    """Ensure directory tree exists."""
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
