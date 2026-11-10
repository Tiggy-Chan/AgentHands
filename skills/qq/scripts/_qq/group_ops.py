"""Group management operations for QQ via OneBot."""

from __future__ import annotations

from typing import Any

from . import client, security


async def list_groups() -> list[dict[str, Any]]:
    """Retrieve joined QQ groups list."""
    res = await client.call_action("get_group_list")
    groups = res if isinstance(res, list) else []
    return [
        {
            "group_id": g.get("group_id"),
            "group_name": g.get("group_name"),
            "member_count": g.get("member_count"),
            "max_member_count": g.get("max_member_count"),
        }
        for g in groups if isinstance(g, dict)
    ]


async def get_group_members(group_id: int) -> list[dict[str, Any]]:
    """Retrieve group member list."""
    res = await client.call_action("get_group_member_list", {"group_id": group_id})
    members = res if isinstance(res, list) else []
    return [
        {
            "user_id": m.get("user_id"),
            "nickname": m.get("nickname"),
            "card": m.get("card", ""),
            "role": m.get("role"),
            "join_time": m.get("join_time"),
        }
        for m in members if isinstance(m, dict)
    ]


async def set_group_ban(
    group_id: int,
    user_id: int,
    duration: int = 1800,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Mute a group member with dual-gate write verification."""
    payload = {"group_id": group_id, "user_id": user_id, "duration": duration}
    security.require_write_allowed("set_group_ban", payload, execute, direct, expected_fp)

    await client.call_action(
        "set_group_ban",
        {"group_id": group_id, "user_id": user_id, "duration": duration},
    )
    return {
        "status": "ok",
        "action": "set_group_ban",
        "group_id": group_id,
        "user_id": user_id,
        "duration": duration,
    }


async def set_group_kick(
    group_id: int,
    user_id: int,
    reject_add_request: bool = False,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Kick a group member with dual-gate write verification."""
    payload = {
        "group_id": group_id,
        "user_id": user_id,
        "reject_add_request": reject_add_request,
    }
    security.require_write_allowed("set_group_kick", payload, execute, direct, expected_fp)

    await client.call_action(
        "set_group_kick",
        {
            "group_id": group_id,
            "user_id": user_id,
            "reject_add_request": reject_add_request,
        },
    )
    return {
        "status": "ok",
        "action": "set_group_kick",
        "group_id": group_id,
        "user_id": user_id,
    }
