"""Configuration and paths for X (Twitter) skill."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("X_HOME", os.path.expanduser("~/.local/share/x")))
CONFIG_DIR = HOME / "config"
CACHE_DIR = HOME / "cache"
COOKIES_FILE = CONFIG_DIR / "cookies.json"

WRITE_ALLOWED_ENV = "X_WRITE_ALLOWED"

# Standard public web client bearer token for X
DEFAULT_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
BASE_URL = "https://x.com"


def ensure_dirs() -> None:
    """Ensure directory tree exists."""
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
