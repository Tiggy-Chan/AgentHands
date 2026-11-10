"""User profile, relations, and account operations for Bilibili."""

from __future__ import annotations

from typing import Any

from . import auth, security


async def get_self_info() -> dict[str, Any]:
    """Retrieve logged-in account profile and metadata."""
    from bilibili_api import user as bilibili_user

    cred = auth.get_credential()
    info = await bilibili_user.get_self_info(cred)
    return {
        "mid": info.get("mid"),
        "name": info.get("name"),
        "sign": info.get("sign"),
        "level": info.get("level"),
        "vip": bool(info.get("vip", {}).get("status")),
        "coins": info.get("money"),
        "face": info.get("face"),
    }


async def get_user_info(uid: int) -> dict[str, Any]:
    """Retrieve public profile for a specific Bilibili user."""
    from bilibili_api.user import User

    cred = auth.get_credential()
    u = User(uid, cred)
    info = await u.get_user_info()
    return {
        "mid": info.get("mid"),
        "name": info.get("name"),
        "sign": info.get("sign"),
        "level": info.get("level"),
        "official": info.get("official", {}).get("title", ""),
        "face": info.get("face"),
    }


async def follow_user(
    uid: int,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Follow a user with dual-gate write verification."""
    from bilibili_api.user import RelationType, User

    payload = {"uid": uid, "action": "follow"}
    security.require_write_allowed("follow_user", payload, execute, direct, expected_fp)

    cred = auth.get_credential()
    u = User(uid, cred)
    await u.modify_relation(RelationType.SUBSCRIBE)
    return {"status": "ok", "action": "follow", "uid": uid}


async def unfollow_user(
    uid: int,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Unfollow a user with dual-gate write verification."""
    from bilibili_api.user import RelationType, User

    payload = {"uid": uid, "action": "unfollow"}
    security.require_write_allowed("unfollow_user", payload, execute, direct, expected_fp)

    cred = auth.get_credential()
    u = User(uid, cred)
    await u.modify_relation(RelationType.UNSUBSCRIBE)
    return {"status": "ok", "action": "unfollow", "uid": uid}
