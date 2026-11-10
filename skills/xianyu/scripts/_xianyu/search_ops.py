"""Search and category discovery for Xianyu."""

from __future__ import annotations

from typing import Any

from . import auth


async def search_items(
    keyword: str,
    page: int = 1,
    min_price: float | None = None,
    max_price: float | None = None,
    sort_field: str | None = None,
) -> list[dict[str, Any]]:
    """Search Xianyu marketplace listings."""
    client = auth.get_client()
    data: dict[str, Any] = {
        "keyword": keyword,
        "pageNumber": page,
    }
    if min_price is not None:
        data["startPrice"] = str(min_price)
    if max_price is not None:
        data["endPrice"] = str(max_price)
    if sort_field:
        data["sortField"] = sort_field

    res = await client.call("search", data)
    items = res.get("items") or res.get("data", {}).get("items") or []

    cleaned: list[dict[str, Any]] = []
    for it in items:
        cleaned.append({
            "item_id": str(it.get("itemId") or it.get("id", "")),
            "title": it.get("title", ""),
            "price": it.get("price"),
            "location": it.get("area") or it.get("city", ""),
            "seller": it.get("userNick") or it.get("nick", ""),
            "wants": it.get("wantNum", 0),
        })
    return cleaned


async def suggest(keyword: str) -> list[str]:
    """Retrieve auto-complete search suggestions."""
    client = auth.get_client()
    res = await client.call("search_suggest", {"keyword": keyword})
    sug_list = res.get("suggestions") or res.get("result", [])
    return [str(s.get("word") if isinstance(s, dict) else s) for s in sug_list]


async def get_categories() -> list[dict[str, Any]]:
    """List marketplace categories."""
    client = auth.get_client()
    res = await client.call("category_list", {})
    cats = res.get("categories") or []
    return [
        {"id": c.get("id"), "name": c.get("name")}
        for c in cats if isinstance(c, dict)
    ]
