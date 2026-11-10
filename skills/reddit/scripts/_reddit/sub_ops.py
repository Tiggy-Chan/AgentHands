"""Subreddit discovery and comment inspection for Reddit."""

from __future__ import annotations

from typing import Any

from . import client


async def list_posts(
    subreddit: str,
    sort: str = "hot",
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Retrieve posts from a subreddit."""
    c = client.get_client()
    sub_name = subreddit.replace("/r/", "").strip("/")
    path = f"r/{sub_name}/{sort}.json"
    data = await c.get(path, params={"limit": limit})

    children = data.get("data", {}).get("children", [])
    cleaned: list[dict[str, Any]] = []
    for item in children:
        d = item.get("data", {})
        cleaned.append({
            "id": d.get("id"),
            "fullname": d.get("name"),
            "title": d.get("title"),
            "author": d.get("author"),
            "score": d.get("score"),
            "num_comments": d.get("num_comments"),
            "url": d.get("url"),
            "selftext": d.get("selftext", "")[:300],
            "subreddit": d.get("subreddit"),
        })
    return cleaned


async def get_comments(post_id: str, limit: int = 25) -> list[dict[str, Any]]:
    """Retrieve comments on a Reddit post."""
    c = client.get_client()
    clean_id = post_id.replace("t3_", "")
    path = f"comments/{clean_id}.json"
    data = await c.get(path, params={"limit": limit})

    comments_data = data[1].get("data", {}).get("children", []) if len(data) > 1 else []
    cleaned: list[dict[str, Any]] = []
    for item in comments_data[:limit]:
        d = item.get("data", {})
        if d.get("body"):
            cleaned.append({
                "id": d.get("id"),
                "fullname": d.get("name"),
                "author": d.get("author"),
                "score": d.get("score"),
                "body": d.get("body"),
            })
    return cleaned
