"""User profile and identity operations for Telegram."""

from __future__ import annotations

from typing import Any

from . import client


async def get_me() -> dict[str, Any]:
    """Retrieve authenticated user/bot profile."""
    c = client.get_client()
    res = await c.call_api("getMe")
    return {
        "id": res.get("id"),
        "username": res.get("username"),
        "first_name": res.get("first_name"),
        "is_bot": bool(res.get("is_bot")),
    }
