"""Message sending, editing, and history operations for Telegram."""

from __future__ import annotations

from typing import Any

from . import client, security


async def send_message(
    chat_id: str | int,
    text: str,
    reply_to: int | None = None,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Send message to a chat or channel with dual-gate write verification."""
    payload = {"chat_id": str(chat_id), "text": text, "reply_to": reply_to}
    security.require_write_allowed("send_message", payload, execute, direct, expected_fp)

    c = client.get_client()
    params: dict[str, Any] = {
        "chat_id": str(chat_id),
        "text": text,
    }
    if reply_to is not None:
        params["reply_to_message_id"] = reply_to

    res = await c.call_api("sendMessage", params)
    return {
        "status": "ok",
        "action": "send_message",
        "chat_id": str(chat_id),
        "message_id": res.get("message_id") if isinstance(res, dict) else res,
    }


async def delete_message(
    chat_id: str | int,
    message_id: int,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Delete message from a chat with dual-gate write verification."""
    payload = {"chat_id": str(chat_id), "message_id": message_id}
    security.require_write_allowed("delete_message", payload, execute, direct, expected_fp)

    c = client.get_client()
    params = {
        "chat_id": str(chat_id),
        "message_id": message_id,
    }
    await c.call_api("deleteMessage", params)
    return {
        "status": "ok",
        "action": "delete_message",
        "chat_id": str(chat_id),
        "message_id": message_id,
    }


async def get_history(chat_id: str | int, limit: int = 20) -> list[dict[str, Any]]:
    """Retrieve recent messages via bridge or updates."""
    c = client.get_client()
    res = await c.call_api("getHistory", {"chat_id": str(chat_id), "limit": limit})
    messages = res if isinstance(res, list) else []
    cleaned: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, dict):
            cleaned.append({
                "message_id": m.get("message_id"),
                "from_user": m.get("from", {}).get("username") or m.get("from", {}).get("first_name"),
                "date": m.get("date"),
                "text": m.get("text", ""),
            })
    return cleaned
