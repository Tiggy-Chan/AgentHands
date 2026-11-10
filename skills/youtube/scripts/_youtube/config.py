"""Configuration and paths for YouTube skill."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("YOUTUBE_HOME", os.path.expanduser("~/.local/share/youtube")))
CONFIG_DIR = HOME / "config"
CACHE_DIR = HOME / "cache"
COOKIES_FILE = CONFIG_DIR / "cookies.json"

WRITE_ALLOWED_ENV = "YOUTUBE_WRITE_ALLOWED"

INNERTUBE_BASE_URL = "https://www.youtube.com/youtubei/v1"
STUDIO_BASE_URL = "https://studio.youtube.com/youtubei/v1"


def ensure_dirs() -> None:
    """Ensure directory tree exists."""
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
