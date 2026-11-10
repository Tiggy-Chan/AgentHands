"""Video details, metadata updates, and interactions for YouTube."""

from __future__ import annotations

from typing import Any

from . import client, security


async def get_video_details(video_id: str) -> dict[str, Any]:
    """Retrieve video metadata via InnerTube player API."""
    c = client.get_client()
    payload = {"videoId": video_id}
    res = await c.call_innertube("player", payload)

    details = res.get("videoDetails", {})
    return {
        "video_id": details.get("videoId"),
        "title": details.get("title"),
        "length_seconds": details.get("lengthSeconds"),
        "channel_id": details.get("channelId"),
        "author": details.get("author"),
        "view_count": details.get("viewCount"),
        "short_description": details.get("shortDescription", "")[:300],
    }


async def update_video_metadata(
    video_id: str,
    title: str | None = None,
    description: str | None = None,
    privacy: str | None = None,
    tags: list[str] | None = None,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Update video title, description, privacy, or tags with dual-gate write verification."""
    payload = {
        "video_id": video_id,
        "title": title,
        "description": description,
        "privacy": privacy,
        "tags": tags or [],
    }
    security.require_write_allowed("update_video_metadata", payload, execute, direct, expected_fp)

    c = client.get_client()
    body: dict[str, Any] = {
        "encryptedVideoId": video_id,
    }
    if title is not None:
        body["title"] = {"newTitle": title}
    if description is not None:
        body["description"] = {"newDescription": description}
    if privacy is not None:
        body["privacy"] = {"newPrivacy": privacy.upper()}

    res = await c.call_innertube("video_manager/metadata_update", body, is_studio=True)
    return {
        "status": "ok",
        "action": "update_video_metadata",
        "video_id": video_id,
        "result": res,
    }


async def interact_video(
    video_id: str,
    action: str = "like",
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Like or remove like from a video with dual-gate write verification."""
    payload = {"video_id": video_id, "action": action}
    security.require_write_allowed(f"interact_{action}", payload, execute, direct, expected_fp)

    c = client.get_client()
    endpoint = "like/like" if action == "like" else "like/removelike"
    body = {
        "target": {"videoId": video_id}
    }
    res = await c.call_innertube(endpoint, body)
    return {
        "status": "ok",
        "action": action,
        "video_id": video_id,
        "result": res,
    }
