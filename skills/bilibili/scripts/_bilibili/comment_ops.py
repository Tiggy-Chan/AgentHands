"""Comment retrieval and posting operations for Bilibili."""

from __future__ import annotations

from typing import Any

from . import auth, security


async def list_comments(
    bvid: str,
    page: int = 1,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """Retrieve video comments with structured author and content fields."""
    from bilibili_api import comment, video

    cred = auth.get_credential()
    v = video.Video(bvid=bvid, credential=cred)
    aid = await v.get_aid()

    res = await comment.get_comments(
        oid=aid,
        type_=comment.ResourceType.VIDEO,
        page_index=page,
        credential=cred,
    )

    replies = res.get("replies") or []
    cleaned: list[dict[str, Any]] = []
    for r in replies:
        member = r.get("member", {})
        content = r.get("content", {})
        cleaned.append({
            "rpid": r.get("rpid"),
            "mid": member.get("mid"),
            "uname": member.get("uname"),
            "message": content.get("message"),
            "ctime": r.get("ctime"),
            "like": r.get("like"),
            "rcount": r.get("rcount"),
        })
    return cleaned


async def send_comment(
    bvid: str,
    text: str,
    root_rpid: int | None = None,
    parent_rpid: int | None = None,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Publish a video comment or floor reply with dual-gate write verification."""
    from bilibili_api import comment, video

    payload = {
        "bvid": bvid,
        "text": text,
        "root_rpid": root_rpid,
        "parent_rpid": parent_rpid,
    }
    security.require_write_allowed("send_comment", payload, execute, direct, expected_fp)

    cred = auth.get_credential()
    v = video.Video(bvid=bvid, credential=cred)
    aid = await v.get_aid()

    kwargs: dict[str, Any] = {
        "text": text,
        "oid": aid,
        "type_": comment.ResourceType.VIDEO,
        "credential": cred,
    }
    if root_rpid is not None:
        kwargs["root"] = root_rpid
    if parent_rpid is not None:
        kwargs["parent"] = parent_rpid

    res = await comment.send_comment(**kwargs)
    return {"status": "ok", "action": "send_comment", "result": res}
