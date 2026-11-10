"""Comment operations for YouTube."""

from __future__ import annotations

from typing import Any

from . import client, security


async def post_comment(
    video_id: str,
    text: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Post comment on a video with dual-gate write verification."""
    payload = {"video_id": video_id, "text": text}
    security.require_write_allowed("post_comment", payload, execute, direct, expected_fp)

    c = client.get_client()
    body = {
        "commentText": text,
        "videoId": video_id,
    }
    res = await c.call_innertube("comment/create_comment", body)
    return {
        "status": "ok",
        "action": "post_comment",
        "video_id": video_id,
        "result": res,
    }


async def reply_comment(
    comment_id: str,
    text: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Reply to an existing comment with dual-gate write verification."""
    payload = {"comment_id": comment_id, "text": text}
    security.require_write_allowed("reply_comment", payload, execute, direct, expected_fp)

    c = client.get_client()
    body = {
        "commentText": text,
        "parentCommentId": comment_id,
    }
    res = await c.call_innertube("comment/create_comment_reply", body)
    return {
        "status": "ok",
        "action": "reply_comment",
        "comment_id": comment_id,
        "result": res,
    }
