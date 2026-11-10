"""Product and listing management for Xianyu."""

from __future__ import annotations

from typing import Any

from . import auth, security


async def list_products(
    status: str = "ALL",
    page: int = 1,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """List seller products (support filtering by status)."""
    client = auth.get_client()
    data = {
        "status": status,
        "pageNumber": page,
        "pageSize": page_size,
    }
    res = await client.call("item_list", data)
    items = res.get("items") or res.get("itemList") or []

    cleaned: list[dict[str, Any]] = []
    for it in items:
        cleaned.append({
            "item_id": str(it.get("itemId") or it.get("id", "")),
            "title": it.get("title", ""),
            "price": it.get("price"),
            "status": it.get("status"),
            "views": it.get("views", 0),
            "wants": it.get("wants", 0),
        })
    return cleaned


async def get_product(item_id: str) -> dict[str, Any]:
    """Retrieve detailed item information."""
    client = auth.get_client()
    res = await client.call("item_detail", {"itemId": str(item_id)})
    item = res.get("item") or res
    return {
        "item_id": str(item_id),
        "title": item.get("title"),
        "desc": item.get("desc"),
        "price": item.get("price"),
        "original_price": item.get("originalPrice"),
        "category_id": item.get("categoryId"),
        "status": item.get("status"),
        "seller_id": item.get("sellerId"),
    }


async def publish_product(
    title: str,
    desc: str,
    price: float,
    original_price: float | None = None,
    images: list[str] | None = None,
    category_id: str = "",
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Publish a new product with dual-gate write verification."""
    payload = {
        "title": title,
        "desc": desc,
        "price": price,
        "original_price": original_price or price,
        "images": images or [],
        "category_id": category_id,
    }
    security.require_write_allowed("publish_product", payload, execute, direct, expected_fp)

    client = auth.get_client()
    mtop_data = {
        "title": title,
        "desc": desc,
        "price": str(price),
        "originalPrice": str(original_price or price),
        "categoryId": category_id,
        "imageUrls": images or [],
    }
    res = await client.call("item_publish", mtop_data)
    return {"status": "ok", "action": "publish_product", "result": res}


async def update_product(
    item_id: str,
    price: float | None = None,
    title: str | None = None,
    desc: str | None = None,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Update product price or details with dual-gate write verification."""
    payload = {
        "item_id": str(item_id),
        "price": price,
        "title": title,
        "desc": desc,
    }
    security.require_write_allowed("update_product", payload, execute, direct, expected_fp)

    client = auth.get_client()
    mtop_data: dict[str, Any] = {"itemId": str(item_id)}
    if price is not None:
        mtop_data["price"] = str(price)
    if title is not None:
        mtop_data["title"] = title
    if desc is not None:
        mtop_data["desc"] = desc

    res = await client.call("item_update", mtop_data)
    return {"status": "ok", "action": "update_product", "item_id": item_id, "result": res}


async def set_shelf(
    item_id: str,
    online: bool = True,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Put product on or off shelf with dual-gate write verification."""
    action = "item_online" if online else "item_offline"
    payload = {"item_id": str(item_id), "online": online}
    security.require_write_allowed("set_shelf", payload, execute, direct, expected_fp)

    client = auth.get_client()
    res = await client.call(action, {"itemId": str(item_id)})
    return {"status": "ok", "action": action, "item_id": item_id, "result": res}


async def delete_product(
    item_id: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Delete a product listing with dual-gate write verification."""
    payload = {"item_id": str(item_id), "action": "delete"}
    security.require_write_allowed("delete_product", payload, execute, direct, expected_fp)

    client = auth.get_client()
    res = await client.call("item_delete", {"itemId": str(item_id)})
    return {"status": "ok", "action": "delete_product", "item_id": item_id, "result": res}
