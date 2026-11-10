"""Authentication and session management for Xiaohongshu."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config, output


def load_credentials() -> dict[str, str]:
    """Load Xiaohongshu cookies with multi-source fallback."""
    creds = {
        "cookies": os.environ.get("XIAOHONGSHU_COOKIES", ""),
    }

    if not creds["cookies"] and config.COOKIES_FILE.exists():
        try:
            with open(config.COOKIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                creds["cookies"] = data.get("cookies", "")
        except Exception:
            pass

    hermes_env = Path.home() / ".hermes" / ".env"
    if not creds["cookies"] and hermes_env.exists():
        try:
            with open(hermes_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == "XIAOHONGSHU_COOKIES":
                        creds["cookies"] = v.strip().strip("'\"")
                        break
        except Exception:
            pass

    if creds["cookies"]:
        output.register_secret(creds["cookies"])

    return creds


def save_credentials(cookies: str | list[dict[str, Any]]) -> None:
    """Save credentials to local storage with 0600 permissions."""
    config.ensure_dirs()
    if isinstance(cookies, list):
        cookies_str = "; ".join(f"{c.get('name')}={c.get('value')}" for c in cookies)
    else:
        cookies_str = str(cookies)

    payload = {"cookies": cookies_str}
    tmp_file = config.CONFIG_DIR / ".cookies.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, config.COOKIES_FILE)

    output.register_secret(cookies_str)


def parse_cookie_list() -> list[dict[str, Any]]:
    """Convert cookie string to Playwright format cookie dict list."""
    creds = load_credentials()
    raw = creds["cookies"]
    if not raw:
        return []
    result = []
    for item in raw.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        result.append({
            "name": k.strip(),
            "value": v.strip(),
            "domain": ".xiaohongshu.com",
            "path": "/",
        })
    return result
