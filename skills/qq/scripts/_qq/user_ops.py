"""User and profile operations for QQ via OneBot."""

from __future__ import annotations

from typing import Any

from . import client


async def get_self_info() -> dict[str, Any]:
    """Retrieve logged in account information."""
    res = await client.call_action("get_login_info")
    return {
        "user_id": res.get("user_id"),
        "nickname": res.get("nickname"),
    }


async def list_friends() -> list[dict[str, Any]]:
    """Retrieve friends list."""
    res = await client.call_action("get_friend_list")
    friends = res if isinstance(res, list) else []
    return [
        {
            "user_id": f.get("user_id"),
            "nickname": f.get("nickname"),
            "remark": f.get("remark", ""),
        }
        for f in friends if isinstance(f, dict)
    ]
