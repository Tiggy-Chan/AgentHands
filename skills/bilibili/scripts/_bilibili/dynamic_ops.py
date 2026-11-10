"""Dynamic feeds and posting operations for Bilibili."""

from __future__ import annotations

from typing import Any

from . import auth, security


async def list_dynamics(
    uid: int | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Retrieve user dynamic posts."""
    from bilibili_api import dynamic, user

    cred = auth.get_credential()
    if uid is None:
        self_info = await user.get_self_info(cred)
        uid = int(self_info["mid"])

    u = user.User(uid, cred)
    res = await u.get_dynamics(offset=0)
    items = res.get("cards") or res.get("items") or []

    cleaned: list[dict[str, Any]] = []
    for item in items[:limit]:
        desc = item.get("desc", {})
        cleaned.append({
            "dynamic_id": desc.get("dynamic_id_str") or str(item.get("id_str", "")),
            "type": desc.get("type") or item.get("type"),
            "timestamp": desc.get("timestamp") or item.get("timestamp"),
            "author": desc.get("user_profile", {}).get("info", {}).get("uname", ""),
        })
    return cleaned


async def post_dynamic(
    text: str,
    images: list[str] | None = None,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Publish a dynamic update with text or pictures."""
    from bilibili_api import dynamic

    images = images or []
    payload = {"text": text, "images": images}
    security.require_write_allowed("post_dynamic", payload, execute, direct, expected_fp)

    cred = auth.get_credential()
    if images:
        from bilibili_api import Picture
        pics = [Picture(p, cred) for p in images]
        res = await dynamic.send_dynamic_with_pictures(text, pics, credential=cred)
    else:
        res = await dynamic.send_dynamic(text, credential=cred)

    return {"status": "ok", "action": "post_dynamic", "result": res}


async def delete_dynamic(
    dynamic_id: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Remove a dynamic post."""
    from bilibili_api import dynamic

    payload = {"dynamic_id": str(dynamic_id)}
    security.require_write_allowed("delete_dynamic", payload, execute, direct, expected_fp)

    cred = auth.get_credential()
    res = await dynamic.delete_dynamic(dynamic_id, credential=cred)
    return {"status": "ok", "action": "delete_dynamic", "dynamic_id": dynamic_id, "result": res}
