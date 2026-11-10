"""Private messaging operations for Bilibili."""

from __future__ import annotations

from typing import Any

from . import auth, security


async def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    """List recent private message sessions."""
    from bilibili_api import session

    cred = auth.get_credential()
    res = await session.get_sessions(credential=cred)
    session_list = res.get("session_list") or []

    cleaned: list[dict[str, Any]] = []
    for s in session_list[:limit]:
        last_msg = s.get("last_msg") or {}
        cleaned.append({
            "talker_id": s.get("talker_id"),
            "session_type": s.get("session_type"),
            "unread_count": s.get("unread_count"),
            "last_msg_time": last_msg.get("msg_timestamp"),
            "last_msg_content": last_msg.get("content"),
        })
    return cleaned


async def get_history(receiver_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Retrieve message history for a specific conversation."""
    from bilibili_api import session

    cred = auth.get_credential()
    res = await session.get_history(talker_id=receiver_id, credential=cred)
    messages = res.get("messages") or []

    cleaned: list[dict[str, Any]] = []
    for m in messages[:limit]:
        cleaned.append({
            "msg_key": m.get("msg_key"),
            "sender_uid": m.get("sender_uid"),
            "receiver_id": m.get("receiver_id"),
            "msg_type": m.get("msg_type"),
            "content": m.get("content"),
            "timestamp": m.get("timestamp"),
        })
    return cleaned


async def send_message(
    receiver_id: int,
    text: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Send private text message with dual-gate write verification."""
    from bilibili_api import session

    payload = {"receiver_id": receiver_id, "text": text}
    security.require_write_allowed("send_message", payload, execute, direct, expected_fp)

    cred = auth.get_credential()
    res = await session.send_msg(
        receiver_id=receiver_id,
        content=text,
        credential=cred,
    )
    return {"status": "ok", "action": "send_message", "receiver_id": receiver_id, "result": res}
