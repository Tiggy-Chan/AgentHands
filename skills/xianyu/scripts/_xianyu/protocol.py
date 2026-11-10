"""Protocol constants, signing, and cookie utilities for Xianyu MTOP."""

from __future__ import annotations

import hashlib
from typing import Union

APP_KEY: str = "34839810"
H5API_BASE: str = "https://h5api.m.goofish.com/h5"


def generate_sign(t: Union[str, int], token: str, data: str) -> str:
    """Generate MD5 signature for Alibaba MTOP gateway."""
    raw = f"{token}&{t}&{APP_KEY}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def trans_cookies(cookies_str: str) -> dict[str, str]:
    """Parse cookie header string into dictionary."""
    cookies: dict[str, str] = {}
    if not cookies_str:
        return cookies
    for item in cookies_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, val = item.split("=", 1)
        cookies[key.strip()] = val.strip()
    return cookies


def format_cookies(cookies_dict: dict[str, str]) -> str:
    """Format cookie dictionary into header string."""
    return "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
