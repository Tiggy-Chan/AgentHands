"""Small bounded helpers shared by Browser Worker resource providers."""

from __future__ import annotations

import re
from datetime import datetime

from . import config


def clip(value: object, limit: int | None = None) -> str:
    """Clip text by UTF-8 bytes so a page cannot inflate result memory."""
    text = str(value or "")
    limit = limit or config.MAX_BROWSER_TEXT_BYTES
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + "…"


def lines(value: object) -> list[str]:
    return [clip(line.strip()) for line in str(value or "").splitlines() if line.strip()]


def first_match(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.M)
        if match:
            return clip(match.group(1))
    return ""


def normalize_time(value: object) -> str:
    """Return an ISO-ish display value without guessing absent timestamps."""
    text = clip(value)
    if not text:
        return ""
    try:
        number = float(text)
        if number > 1e12:
            number /= 1000.0
        if number > 0:
            return datetime.fromtimestamp(number).astimezone().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return text


async def attr(locator, name: str) -> str:
    try:
        return clip(await locator.get_attribute(name))
    except Exception:
        return ""


async def bounded_inner_text(locator) -> str:
    try:
        return clip(await locator.inner_text())
    except Exception:
        return ""


async def text_from_first(page, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count():
                value = await bounded_inner_text(locator)
                if value:
                    return value
        except Exception:
            continue
    return ""
