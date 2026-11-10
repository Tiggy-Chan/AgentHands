"""Search operations for Bilibili."""

from __future__ import annotations

from typing import Any

from . import auth


async def search_content(
    keyword: str,
    search_type: str = "video",
    page: int = 1,
) -> list[dict[str, Any]]:
    """Search Bilibili for videos, users, or articles."""
    from bilibili_api import search

    cred = auth.get_credential()
    st = search.SearchObjectType.VIDEO
    if search_type == "user":
        st = search.SearchObjectType.USER

    res = await search.search_by_type(
        keyword=keyword,
        search_type=st,
        page=page,
        credential=cred,
    )

    items = res.get("result") or []
    cleaned: list[dict[str, Any]] = []
    for it in items:
        if search_type == "video":
            cleaned.append({
                "bvid": it.get("bvid"),
                "title": it.get("title", "").replace('<em class="keyword">', "").replace("</em>", ""),
                "author": it.get("author"),
                "play": it.get("play"),
                "pubdate": it.get("pubdate"),
            })
        elif search_type == "user":
            cleaned.append({
                "mid": it.get("mid"),
                "uname": it.get("uname"),
                "sign": it.get("usign"),
                "fans": it.get("fans"),
                "videos": it.get("videos"),
            })
    return cleaned
