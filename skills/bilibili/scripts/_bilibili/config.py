"""Configuration and path definitions for Bilibili skill."""

from __future__ import annotations

import os
from pathlib import Path

# Base storage directory
HOME = Path(os.environ.get("BILIBILI_HOME", os.path.expanduser("~/.local/share/bilibili")))
CONFIG_DIR = HOME / "config"
CACHE_DIR = HOME / "cache"
COOKIES_FILE = CONFIG_DIR / "cookies.json"
QR_CODE_FILE = CACHE_DIR / "bilibili_qr.png"

# Safety write gate environment variable
WRITE_ALLOWED_ENV = "BILIBILI_WRITE_ALLOWED"


def ensure_dirs() -> None:
    """Ensure base directories exist with appropriate permissions."""
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
