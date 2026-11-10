"""Authentication and endpoint configuration for QQ OneBot client."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config, output


def load_credentials() -> dict[str, str]:
    """Load OneBot endpoint URL and access token with multi-source fallback."""
    creds = {
        "url": os.environ.get("QQ_ONEBOT_URL", ""),
        "token": os.environ.get("QQ_ACCESS_TOKEN", ""),
    }

    if not creds["url"] and config.CONFIG_FILE.exists():
        try:
            with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                creds["url"] = data.get("url", "")
                creds["token"] = data.get("token", "")
        except Exception:
            pass

    if not creds["url"]:
        creds["url"] = config.DEFAULT_ONEBOT_URL

    if creds["token"]:
        output.register_secret(creds["token"])

    return creds


def save_credentials(url: str, token: str = "") -> None:
    """Save OneBot endpoint and token to local configuration."""
    config.ensure_dirs()
    payload = {"url": url.rstrip("/"), "token": token}

    tmp_file = config.CONFIG_DIR / ".config.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, config.CONFIG_FILE)

    if token:
        output.register_secret(token)
