"""User inspection and social operations for X."""

from __future__ import annotations

from typing import Any

from . import client, security

USER_BY_SCREEN_NAME_QID = "G3KGOASz96M-Qu0nwmGXNg"


async def get_user_info(screen_name: str) -> dict[str, Any]:
    """Retrieve public user profile details."""
    c = client.get_client()
    variables = {
        "screen_name": screen_name.lstrip("@"),
        "withSafetyModeUserFields": True,
    }
    features = {
        "hidden_profile_likes_enabled": True,
        "hidden_profile_subscriptions_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "subscriptions_verification_info_is_identity_verified_enabled": True,
        "subscriptions_verification_info_verified_since_enabled": True,
        "highlights_tweets_tab_ui_enabled": True,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "responsive_web_graphql_timeline_navigation_enabled": True,
    }

    res = await c.graphql_get(USER_BY_SCREEN_NAME_QID, "UserByScreenName", variables, features)
    user_res = res.get("user", {}).get("result", {})
    legacy = user_res.get("legacy", {})

    return {
        "rest_id": user_res.get("rest_id"),
        "name": legacy.get("name"),
        "screen_name": legacy.get("screen_name"),
        "description": legacy.get("description"),
        "followers_count": legacy.get("followers_count"),
        "friends_count": legacy.get("friends_count"),
        "favourites_count": legacy.get("favourites_count"),
        "statuses_count": legacy.get("statuses_count"),
        "created_at": legacy.get("created_at"),
    }
