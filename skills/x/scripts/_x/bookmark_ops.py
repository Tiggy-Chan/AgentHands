"""Bookmark retrieval operations for X."""

from __future__ import annotations

from typing import Any

from . import client

BOOKMARKS_QID = "tMD698K218w6A7B_iIq3-g"


async def list_bookmarks(limit: int = 20) -> list[dict[str, Any]]:
    """Retrieve personal bookmarked tweets."""
    c = client.get_client()
    variables = {
        "count": limit,
        "includePromotedContent": False,
    }
    features = {
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "tweetypie_unmention_optimization_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
    }

    res = await c.graphql_get(BOOKMARKS_QID, "Bookmarks", variables, features)
    instructions = (
        res.get("bookmark_timeline_v2", {})
        .get("timeline", {})
        .get("instructions", [])
    )

    cleaned: list[dict[str, Any]] = []
    for inst in instructions:
        if inst.get("type") == "TimelineAddEntries":
            for entry in inst.get("entries", []):
                content = entry.get("content", {})
                item = content.get("itemContent", {})
                tweet_res = item.get("tweet_results", {}).get("result", {})
                legacy = tweet_res.get("legacy", {})
                if legacy:
                    cleaned.append({
                        "tweet_id": legacy.get("id_str"),
                        "text": legacy.get("full_text"),
                        "created_at": legacy.get("created_at"),
                        "favorite_count": legacy.get("favorite_count"),
                        "retweet_count": legacy.get("retweet_count"),
                    })

    return cleaned[:limit]
