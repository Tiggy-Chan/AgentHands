"""Authentication and session loading for Xianyu."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config, mtop, output

_CLIENT_INSTANCE: mtop.MtopClient | None = None


def load_credentials() -> dict[str, str]:
    """Load Xianyu cookies and user ID with multi-source fallback."""
    creds = {
        "cookies": os.environ.get("XIANYU_COOKIES", ""),
        "user_id": os.environ.get("XIANYU_USER_ID", ""),
    }

    # 1. Local config file
    if not creds["cookies"] and config.COOKIES_FILE.exists():
        try:
            with open(config.COOKIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                creds["cookies"] = data.get("cookies", "")
                creds["user_id"] = data.get("user_id", "")
        except Exception:
            pass

    # 2. Hermes .env fallback
    hermes_env = Path.home() / ".hermes" / ".env"
    if not creds["cookies"] and hermes_env.exists():
        try:
            with open(hermes_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "XIANYU_COOKIES" and not creds["cookies"]:
                        creds["cookies"] = v
                    elif k == "XIANYU_USER_ID" and not creds["user_id"]:
                        creds["user_id"] = v
        except Exception:
            pass

    # Register secrets
    if creds["cookies"]:
        output.register_secret(creds["cookies"])
    if creds["user_id"]:
        output.register_secret(creds["user_id"])

    return creds


def save_credentials(cookies: str, user_id: str = "") -> None:
    """Save credentials to local storage with 0600 permissions."""
    config.ensure_dirs()
    payload = {"cookies": cookies, "user_id": user_id}

    tmp_file = config.CONFIG_DIR / ".cookies.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, config.COOKIES_FILE)

    output.register_secret(cookies)
    if user_id:
        output.register_secret(user_id)


def get_client() -> mtop.MtopClient:
    """Obtain or initialize MtopClient."""
    global _CLIENT_INSTANCE
    if _CLIENT_INSTANCE is not None:
        return _CLIENT_INSTANCE

    creds = load_credentials()
    if not creds["cookies"]:
        raise RuntimeError(
            "Xianyu session cookies not found. Please set XIANYU_COOKIES or run `xianyu.py login`."
        )

    _CLIENT_INSTANCE = mtop.MtopClient(
        cookies_str=creds["cookies"],
        user_id=creds["user_id"],
    )
    return _CLIENT_INSTANCE
