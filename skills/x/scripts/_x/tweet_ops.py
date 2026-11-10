"""Tweet publishing, interaction, and deletion operations for X."""

from __future__ import annotations

from typing import Any

from . import client, security

# Standard Query IDs for X GraphQL
CREATE_TWEET_QID = "a1p9RWpkYKBkVmGwQRTSjA"
DELETE_TWEET_QID = "VaenaVgh5q0AcOCbvVagxQ"
FAVORITE_TWEET_QID = "lI07N6Otwv1PhnEgXILM7A"
RETWEET_QID = "ojPdsNQgahqBGT паспорта"


async def post_tweet(
    text: str,
    media_ids: list[str] | None = None,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Publish a new tweet with dual-gate write verification."""
    payload = {"text": text, "media_ids": media_ids or []}
    security.require_write_allowed("post_tweet", payload, execute, direct, expected_fp)

    c = client.get_client()
    variables: dict[str, Any] = {
        "tweet_text": text,
        "dark_request": False,
        "media": {"media_entities": [], "possibly_sensitive": False},
        "semantic_annotation_ids": [],
    }
    if media_ids:
        variables["media"]["media_entities"] = [{"media_id": mid} for mid in media_ids]

    res = await c.graphql_post(CREATE_TWEET_QID, "CreateTweet", variables)
    tweet_result = res.get("create_tweet", {}).get("tweet_results", {}).get("result", {})
    return {
        "status": "ok",
        "action": "post_tweet",
        "tweet_id": tweet_result.get("rest_id"),
        "text": text,
    }


async def reply_tweet(
    tweet_id: str,
    text: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Reply to an existing tweet with dual-gate write verification."""
    payload = {"reply_to": tweet_id, "text": text}
    security.require_write_allowed("reply_tweet", payload, execute, direct, expected_fp)

    c = client.get_client()
    variables = {
        "tweet_text": text,
        "reply": {"in_reply_to_tweet_id": str(tweet_id), "exclude_reply_user_ids": []},
        "dark_request": False,
        "media": {"media_entities": [], "possibly_sensitive": False},
        "semantic_annotation_ids": [],
    }
    res = await c.graphql_post(CREATE_TWEET_QID, "CreateTweet", variables)
    tweet_result = res.get("create_tweet", {}).get("tweet_results", {}).get("result", {})
    return {
        "status": "ok",
        "action": "reply_tweet",
        "reply_to": tweet_id,
        "tweet_id": tweet_result.get("rest_id"),
    }


async def delete_tweet(
    tweet_id: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Delete a tweet with dual-gate write verification."""
    payload = {"tweet_id": str(tweet_id)}
    security.require_write_allowed("delete_tweet", payload, execute, direct, expected_fp)

    c = client.get_client()
    variables = {"tweet_id": str(tweet_id), "dark_request": False}
    await c.graphql_post(DELETE_TWEET_QID, "DeleteTweet", variables)
    return {"status": "ok", "action": "delete_tweet", "tweet_id": tweet_id}


async def interact_tweet(
    tweet_id: str,
    action: str = "like",
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Like or retweet a tweet with dual-gate write verification."""
    payload = {"tweet_id": str(tweet_id), "action": action}
    security.require_write_allowed(f"interact_tweet_{action}", payload, execute, direct, expected_fp)

    c = client.get_client()
    variables = {"tweet_id": str(tweet_id)}
    if action == "like":
        await c.graphql_post(FAVORITE_TWEET_QID, "FavoriteTweet", variables)
    elif action == "retweet":
        await c.graphql_post(RETWEET_QID, "CreateRetweet", variables)
    else:
        raise ValueError(f"Unsupported interact action: {action}")

    return {"status": "ok", "action": action, "tweet_id": tweet_id}
