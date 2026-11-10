"""Posting, commenting, and voting operations for Reddit."""

from __future__ import annotations

from typing import Any

from . import client, security


async def submit_post(
    subreddit: str,
    title: str,
    text: str = "",
    url: str = "",
    kind: str = "self",
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Submit a link or text post to a subreddit with dual-gate write verification."""
    payload = {
        "subreddit": subreddit,
        "title": title,
        "text": text,
        "url": url,
        "kind": kind,
    }
    security.require_write_allowed("submit_post", payload, execute, direct, expected_fp)

    c = client.get_client()
    sub_name = subreddit.replace("/r/", "").strip("/")
    post_data = {
        "api_type": "json",
        "sr": sub_name,
        "title": title,
        "kind": kind,
        "text": text,
        "url": url,
    }
    res = await c.post("api/submit", post_data)
    json_data = res.get("json", {})
    errors = json_data.get("errors", [])
    if errors:
        raise RuntimeError(f"Reddit submit error: {errors}")

    data = json_data.get("data", {})
    return {
        "status": "ok",
        "action": "submit_post",
        "subreddit": sub_name,
        "url": data.get("url"),
        "id": data.get("id"),
        "name": data.get("name"),
    }


async def submit_comment(
    parent_fullname: str,
    text: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Submit a comment on a submission or reply to a comment."""
    payload = {"parent": parent_fullname, "text": text}
    security.require_write_allowed("submit_comment", payload, execute, direct, expected_fp)

    c = client.get_client()
    data = {
        "api_type": "json",
        "thing_id": parent_fullname,
        "text": text,
    }
    res = await c.post("api/comment", data)
    json_data = res.get("json", {})
    errors = json_data.get("errors", [])
    if errors:
        raise RuntimeError(f"Reddit comment error: {errors}")

    return {
        "status": "ok",
        "action": "submit_comment",
        "parent": parent_fullname,
        "result": json_data.get("data"),
    }


async def vote(
    fullname: str,
    direction: int = 1,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Cast a vote (1 for upvote, -1 for downvote, 0 to clear)."""
    payload = {"id": fullname, "dir": direction}
    security.require_write_allowed("vote", payload, execute, direct, expected_fp)

    c = client.get_client()
    data = {
        "id": fullname,
        "dir": direction,
    }
    await c.post("api/vote", data)
    return {
        "status": "ok",
        "action": "vote",
        "id": fullname,
        "dir": direction,
    }
