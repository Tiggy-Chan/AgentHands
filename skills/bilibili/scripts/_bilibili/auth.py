"""Authentication and session management for Bilibili."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from . import config, output

_CACHED_CREDENTIAL = None


def load_credentials() -> dict[str, str]:
    """Load Bilibili credentials with multi-source fallback."""
    creds: dict[str, str] = {
        "sessdata": "",
        "bili_jct": "",
        "buvid3": "",
        "dedeuserid": "",
    }

    # 1. Environment variables
    for key in ("sessdata", "bili_jct", "buvid3", "dedeuserid"):
        env_val = os.environ.get(f"BILIBILI_{key.upper()}", "")
        if env_val:
            creds[key] = env_val

    # 2. Local config JSON
    if not (creds["sessdata"] and creds["bili_jct"]) and config.COOKIES_FILE.exists():
        try:
            with open(config.COOKIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for key in creds:
                    if not creds[key] and data.get(key):
                        creds[key] = str(data[key])
        except Exception:
            pass

    # 3. Hermes .env fallback
    hermes_env = Path.home() / ".hermes" / ".env"
    if not (creds["sessdata"] and creds["bili_jct"]) and hermes_env.exists():
        try:
            with open(hermes_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "BILIBILI_SESSDATA" and not creds["sessdata"]:
                        creds["sessdata"] = v
                    elif k == "BILIBILI_BILI_JCT" and not creds["bili_jct"]:
                        creds["bili_jct"] = v
                    elif k == "BILIBILI_BUVID3" and not creds["buvid3"]:
                        creds["buvid3"] = v
                    elif k == "BILIBILI_DEDEUSERID" and not creds["dedeuserid"]:
                        creds["dedeuserid"] = v
        except Exception:
            pass

    # Register secrets to prevent leakage in traces
    for v in creds.values():
        if v:
            output.register_secret(v)

    return creds


def save_credentials(creds: dict[str, str]) -> None:
    """Save credentials with 0600 permissions."""
    config.ensure_dirs()
    payload = {
        "sessdata": creds.get("sessdata", ""),
        "bili_jct": creds.get("bili_jct", ""),
        "buvid3": creds.get("buvid3", ""),
        "dedeuserid": creds.get("dedeuserid", ""),
    }

    # Save to local JSON
    tmp_file = config.CONFIG_DIR / ".cookies.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, config.COOKIES_FILE)

    for v in payload.values():
        if v:
            output.register_secret(v)


def get_credential():
    """Obtain bilibili_api Credential instance."""
    global _CACHED_CREDENTIAL
    if _CACHED_CREDENTIAL is not None:
        return _CACHED_CREDENTIAL

    try:
        from bilibili_api import Credential
    except ImportError as exc:
        raise RuntimeError("bilibili-api-python package is not installed.") from exc

    creds = load_credentials()
    _CACHED_CREDENTIAL = Credential(
        sessdata=creds.get("sessdata") or None,
        bili_jct=creds.get("bili_jct") or None,
        buvid3=creds.get("buvid3") or None,
        dedeuserid=creds.get("dedeuserid") or None,
    )
    return _CACHED_CREDENTIAL
