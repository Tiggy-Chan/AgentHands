"""Configuration and paths for QQ skill."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("QQ_HOME", os.path.expanduser("~/.local/share/qq")))
CONFIG_DIR = HOME / "config"
CACHE_DIR = HOME / "cache"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Write gate environment variable
WRITE_ALLOWED_ENV = "QQ_WRITE_ALLOWED"

# Default OneBot 11 HTTP API endpoint
DEFAULT_ONEBOT_URL = "http://127.0.0.1:3000"


def ensure_dirs() -> None:
    """Ensure directory tree exists."""
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
