"""Authentication and credentials loader for X."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config, output


def load_credentials() -> dict[str, str]:
    """Load X session credentials with fallback resolution."""
    creds = {
        "auth_token": os.environ.get("X_AUTH_TOKEN", ""),
        "ct0": os.environ.get("X_CT0", ""),
    }

    # 1. Local skill config
    if not (creds["auth_token"] and creds["ct0"]) and config.COOKIES_FILE.exists():
        try:
            with open(config.COOKIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                creds["auth_token"] = creds["auth_token"] or data.get("auth_token", "")
                creds["ct0"] = creds["ct0"] or data.get("ct0", "")
        except Exception:
            pass

    # 2. Twikit config fallback
    twikit_file = Path.home() / ".config" / "x-twikit" / "cookies.json"
    if not (creds["auth_token"] and creds["ct0"]) and twikit_file.exists():
        try:
            with open(twikit_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                creds["auth_token"] = creds["auth_token"] or data.get("auth_token", "")
                creds["ct0"] = creds["ct0"] or data.get("ct0", "")
        except Exception:
            pass

    # 3. Hermes .env fallback
    hermes_env = Path.home() / ".hermes" / ".env"
    if not (creds["auth_token"] and creds["ct0"]) and hermes_env.exists():
        try:
            with open(hermes_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "X_AUTH_TOKEN" and not creds["auth_token"]:
                        creds["auth_token"] = v
                    elif k == "X_CT0" and not creds["ct0"]:
                        creds["ct0"] = v
        except Exception:
            pass

    for v in creds.values():
        if v:
            output.register_secret(v)

    return creds


def save_credentials(auth_token: str, ct0: str) -> None:
    """Save auth_token and ct0 locally with 0600 permissions."""
    config.ensure_dirs()
    payload = {"auth_token": auth_token, "ct0": ct0}

    tmp_file = config.CONFIG_DIR / ".cookies.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, config.COOKIES_FILE)

    output.register_secret(auth_token)
    output.register_secret(ct0)
