"""Chat and channel operations for Telegram."""

from __future__ import annotations

from typing import Any

from . import client


async def get_chat(chat_id: str | int) -> dict[str, Any]:
    """Inspect chat or channel details."""
    c = client.get_client()
    res = await c.call_api("getChat", {"chat_id": str(chat_id)})
    return {
        "id": res.get("id"),
        "title": res.get("title") or res.get("first_name"),
        "username": res.get("username"),
        "type": res.get("type"),
        "description": res.get("description"),
    }
