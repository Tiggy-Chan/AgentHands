"""Configuration and paths for Xiaohongshu skill."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("XIAOHONGSHU_HOME", os.path.expanduser("~/.local/share/xiaohongshu")))
CONFIG_DIR = HOME / "config"
CACHE_DIR = HOME / "cache"
COOKIES_FILE = CONFIG_DIR / "cookies.json"
QR_CODE_FILE = CACHE_DIR / "xiaohongshu_qr.png"
PROFILE_DIR = HOME / "browser_profile"

WRITE_ALLOWED_ENV = "XIAOHONGSHU_WRITE_ALLOWED"


def ensure_dirs() -> None:
    """Ensure directory tree exists."""
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
