"""Authentication and credentials resolution for Reddit."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config, output


def load_credentials() -> dict[str, str]:
    """Load Reddit authentication configuration with multi-source fallback."""
    creds = {
        "session": os.environ.get("REDDIT_SESSION", ""),
        "token": os.environ.get("REDDIT_TOKEN", ""),
        "client_id": os.environ.get("REDDIT_CLIENT_ID", ""),
        "client_secret": os.environ.get("REDDIT_CLIENT_SECRET", ""),
        "username": os.environ.get("REDDIT_USERNAME", ""),
        "password": os.environ.get("REDDIT_PASSWORD", ""),
    }

    if not (creds["session"] or creds["token"] or (creds["client_id"] and creds["client_secret"])) and config.CONFIG_FILE.exists():
        try:
            with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k in creds:
                    creds[k] = creds[k] or data.get(k, "")
        except Exception:
            pass

    for k in ("session", "token", "client_secret", "password"):
        if creds.get(k):
            output.register_secret(creds[k])

    return creds


def save_credentials(data: dict[str, str]) -> None:
    """Save credentials to local configuration with 0600 permissions."""
    config.ensure_dirs()
    tmp_file = config.CONFIG_DIR / ".credentials.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, config.CONFIG_FILE)

    for k in ("session", "token", "client_secret", "password"):
        if data.get(k):
            output.register_secret(data[k])
