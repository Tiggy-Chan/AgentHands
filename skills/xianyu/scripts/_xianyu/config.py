"""Configuration and paths for Xianyu skill."""

from __future__ import annotations

import os
from pathlib import Path

# Storage directories
HOME = Path(os.environ.get("XIANYU_HOME", os.path.expanduser("~/.local/share/xianyu")))
CONFIG_DIR = HOME / "config"
CACHE_DIR = HOME / "cache"
COOKIES_FILE = CONFIG_DIR / "cookies.json"
QR_CODE_FILE = CACHE_DIR / "xianyu_qr.png"

# Safety write gate environment variable
WRITE_ALLOWED_ENV = "XIANYU_WRITE_ALLOWED"


def ensure_dirs() -> None:
    """Ensure directory tree exists."""
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
