"""Authentication and SAPISIDHASH generator for YouTube."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from . import config, output


def generate_sapisid_hash(sapisid: str, origin: str = "https://www.youtube.com") -> str:
    """Generate Google SAPISIDHASH authentication header."""
    now = str(int(time.time()))
    payload = f"{now} {sapisid} {origin}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {now}_{digest}"


def load_credentials() -> dict[str, str]:
    """Load YouTube credentials with multi-source fallback."""
    creds = {
        "cookies": os.environ.get("YOUTUBE_COOKIES", ""),
        "sapisid": os.environ.get("YOUTUBE_SAPISID", ""),
    }

    if not creds["cookies"] and config.COOKIES_FILE.exists():
        try:
            with open(config.COOKIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                creds["cookies"] = data.get("cookies", "")
                creds["sapisid"] = data.get("sapisid", "")
        except Exception:
            pass

    # Extract SAPISID if inside cookie string
    if not creds["sapisid"] and creds["cookies"]:
        for part in creds["cookies"].split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k in ("SAPISID", "__Secure-3PAPISID"):
                    creds["sapisid"] = v.strip()
                    break

    for v in creds.values():
        if v:
            output.register_secret(v)

    return creds


def save_credentials(cookies: str) -> None:
    """Save raw cookies to local configuration with 0600 permissions."""
    config.ensure_dirs()
    sapisid = ""
    for part in cookies.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k in ("SAPISID", "__Secure-3PAPISID"):
                sapisid = v.strip()
                break

    payload = {"cookies": cookies, "sapisid": sapisid}
    tmp_file = config.CONFIG_DIR / ".cookies.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, config.COOKIES_FILE)

    output.register_secret(cookies)
    if sapisid:
        output.register_secret(sapisid)
