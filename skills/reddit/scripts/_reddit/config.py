"""Configuration and paths for Reddit skill."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("REDDIT_HOME", os.path.expanduser("~/.local/share/reddit")))
CONFIG_DIR = HOME / "config"
CACHE_DIR = HOME / "cache"
CONFIG_FILE = CONFIG_DIR / "credentials.json"

WRITE_ALLOWED_ENV = "REDDIT_WRITE_ALLOWED"

BASE_URL = "https://www.reddit.com"
OAUTH_URL = "https://oauth.reddit.com"
DEFAULT_USER_AGENT = "AgentHands:reddit-action:v0.2.0 (by /u/agenthands)"


def ensure_dirs() -> None:
    """Ensure directory tree exists."""
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
