"""Authentication configuration and session loader for Telegram."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config, output


def load_credentials() -> dict[str, str]:
    """Load Telegram credentials with multi-source fallback."""
    creds = {
        "api_id": os.environ.get("TELEGRAM_API_ID", ""),
        "api_hash": os.environ.get("TELEGRAM_API_HASH", ""),
        "session_string": os.environ.get("TELEGRAM_SESSION_STRING", ""),
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "bridge_url": os.environ.get("TELEGRAM_BRIDGE_URL", ""),
    }

    if not (creds["session_string"] or creds["bot_token"] or creds["bridge_url"]) and config.CONFIG_FILE.exists():
        try:
            with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k in creds:
                    creds[k] = creds[k] or data.get(k, "")
        except Exception:
            pass

    for k in ("api_hash", "session_string", "bot_token"):
        if creds.get(k):
            output.register_secret(creds[k])

    return creds


def save_credentials(data: dict[str, str]) -> None:
    """Save credentials to local configuration with 0600 permissions."""
    config.ensure_dirs()
    tmp_file = config.CONFIG_DIR / ".session.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, config.CONFIG_FILE)

    for k in ("api_hash", "session_string", "bot_token"):
        if data.get(k):
            output.register_secret(data[k])
