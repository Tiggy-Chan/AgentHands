"""Channel profile and community post operations for YouTube."""

from __future__ import annotations

from typing import Any

from . import client, security


async def get_channel_info(channel_id: str = "FEmy_channel") -> dict[str, Any]:
    """Retrieve channel details and subscriber stats."""
    c = client.get_client()
    body = {"browseId": channel_id}
    res = await c.call_innertube("browse", body)

    header = res.get("header", {}).get("c4TabbedHeaderRenderer", {})
    metadata = res.get("metadata", {}).get("channelMetadataRenderer", {})

    return {
        "title": header.get("title") or metadata.get("title"),
        "channel_id": metadata.get("externalId") or channel_id,
        "description": metadata.get("description", "")[:300],
        "subscriber_count": header.get("subscriberCountText", {}).get("simpleText"),
        "avatar_url": metadata.get("avatar", {}).get("thumbnails", [{}])[0].get("url"),
    }


async def post_community_update(
    text: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Post an update to channel community tab with dual-gate write verification."""
    payload = {"text": text}
    security.require_write_allowed("post_community_update", payload, execute, direct, expected_fp)

    c = client.get_client()
    body = {
        "postText": text,
    }
    res = await c.call_innertube("backstage/create_post", body, is_studio=True)
    return {
        "status": "ok",
        "action": "post_community_update",
        "result": res,
    }
