"""Messaging operations for QQ via OneBot."""

from __future__ import annotations

from typing import Any

from . import client, security


async def send_private_message(
    user_id: int,
    message: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Send a private message to a QQ friend with dual-gate write verification."""
    payload = {"user_id": user_id, "message": message}
    security.require_write_allowed("send_private_message", payload, execute, direct, expected_fp)

    res = await client.call_action(
        "send_private_msg",
        {"user_id": user_id, "message": message},
    )
    return {
        "status": "ok",
        "action": "send_private_message",
        "user_id": user_id,
        "message_id": res.get("message_id") if isinstance(res, dict) else res,
    }


async def send_group_message(
    group_id: int,
    message: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Send a group message to a QQ group with dual-gate write verification."""
    payload = {"group_id": group_id, "message": message}
    security.require_write_allowed("send_group_message", payload, execute, direct, expected_fp)

    res = await client.call_action(
        "send_group_msg",
        {"group_id": group_id, "message": message},
    )
    return {
        "status": "ok",
        "action": "send_group_message",
        "group_id": group_id,
        "message_id": res.get("message_id") if isinstance(res, dict) else res,
    }


async def delete_message(
    message_id: int,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Recall a sent message with dual-gate write verification."""
    payload = {"message_id": message_id}
    security.require_write_allowed("delete_message", payload, execute, direct, expected_fp)

    await client.call_action("delete_msg", {"message_id": message_id})
    return {"status": "ok", "action": "delete_message", "message_id": message_id}


async def get_history(group_id: int, count: int = 20) -> list[dict[str, Any]]:
    """Retrieve chat history messages for a group."""
    res = await client.call_action("get_group_msg_history", {"group_id": group_id, "count": count})
    messages = res.get("messages") or [] if isinstance(res, dict) else []
    cleaned: list[dict[str, Any]] = []
    for m in messages:
        sender = m.get("sender", {})
        cleaned.append({
            "message_id": m.get("message_id"),
            "sender_id": sender.get("user_id"),
            "sender_name": sender.get("nickname") or sender.get("card", ""),
            "time": m.get("time"),
            "raw_message": m.get("raw_message") or m.get("message"),
        })
    return cleaned
