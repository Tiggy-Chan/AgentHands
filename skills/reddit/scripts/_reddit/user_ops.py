"""User profile and account operations for Reddit."""

from __future__ import annotations

from typing import Any

from . import client


async def get_me() -> dict[str, Any]:
    """Retrieve logged-in user profile."""
    c = client.get_client()
    data = await c.get("api/v1/me")
    return {
        "name": data.get("name"),
        "id": data.get("id"),
        "total_karma": data.get("total_karma"),
        "link_karma": data.get("link_karma"),
        "comment_karma": data.get("comment_karma"),
        "inbox_count": data.get("inbox_count"),
        "created_utc": data.get("created_utc"),
    }


async def get_user_profile(username: str) -> dict[str, Any]:
    """Retrieve public profile for a user."""
    c = client.get_client()
    clean_user = username.replace("/u/", "").strip("/")
    data = await c.get(f"user/{clean_user}/about.json")
    user_data = data.get("data", {})
    return {
        "name": user_data.get("name"),
        "id": user_data.get("id"),
        "total_karma": user_data.get("total_karma"),
        "link_karma": user_data.get("link_karma"),
        "comment_karma": user_data.get("comment_karma"),
        "created_utc": user_data.get("created_utc"),
    }
