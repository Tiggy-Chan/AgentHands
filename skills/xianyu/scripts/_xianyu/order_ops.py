"""Order fulfillment and transaction operations for Xianyu."""

from __future__ import annotations

from typing import Any

from . import auth, security


async def list_orders(
    role: str = "seller",
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """List orders as seller or buyer."""
    client = auth.get_client()
    data: dict[str, Any] = {
        "role": role,
        "pageNumber": page,
        "pageSize": page_size,
    }
    if status:
        data["status"] = status

    res = await client.call("order_list", data)
    orders = res.get("orderList") or res.get("orders") or []

    cleaned: list[dict[str, Any]] = []
    for o in orders:
        cleaned.append({
            "order_id": str(o.get("orderId") or o.get("id", "")),
            "item_id": str(o.get("itemId", "")),
            "item_title": o.get("itemTitle") or o.get("title", ""),
            "price": o.get("price") or o.get("actualPay", 0),
            "status": o.get("statusDesc") or o.get("status", ""),
            "buyer_id": o.get("buyerId"),
            "seller_id": o.get("sellerId"),
            "create_time": o.get("createTime"),
        })
    return cleaned


async def get_order(order_id: str) -> dict[str, Any]:
    """Retrieve detailed order information."""
    client = auth.get_client()
    res = await client.call("order_detail", {"orderId": str(order_id)})
    order = res.get("order") or res
    return {
        "order_id": str(order_id),
        "item_id": order.get("itemId"),
        "title": order.get("title"),
        "price": order.get("price"),
        "status": order.get("status"),
        "buyer_id": order.get("buyerId"),
        "seller_id": order.get("sellerId"),
        "address": order.get("address"),
        "logistics": order.get("logistics"),
    }


async def ship_order(
    order_id: str,
    waybill_no: str,
    express_company: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Fulfill order by shipping goods with waybill number."""
    payload = {
        "order_id": str(order_id),
        "waybill_no": waybill_no,
        "express_company": express_company,
    }
    security.require_write_allowed("ship_order", payload, execute, direct, expected_fp)

    client = auth.get_client()
    data = {
        "orderId": str(order_id),
        "mailNo": waybill_no,
        "cpCode": express_company,
    }
    res = await client.call("order_ship", data)
    return {"status": "ok", "action": "ship_order", "order_id": order_id, "result": res}


async def cancel_order(
    order_id: str,
    reason: str = "协商退款",
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Cancel order with dual-gate write verification."""
    payload = {"order_id": str(order_id), "reason": reason}
    security.require_write_allowed("cancel_order", payload, execute, direct, expected_fp)

    client = auth.get_client()
    data = {
        "orderId": str(order_id),
        "reason": reason,
    }
    res = await client.call("order_cancel", data)
    return {"status": "ok", "action": "cancel_order", "order_id": order_id, "result": res}


async def evaluate_order(
    order_id: str,
    content: str = "交易顺利，非常满意！",
    score: int = 5,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Rate transaction with dual-gate write verification."""
    payload = {"order_id": str(order_id), "content": content, "score": score}
    security.require_write_allowed("evaluate_order", payload, execute, direct, expected_fp)

    client = auth.get_client()
    data = {
        "orderId": str(order_id),
        "rateContent": content,
        "rateScore": score,
    }
    res = await client.call("order_rate", data)
    return {"status": "ok", "action": "evaluate_order", "order_id": order_id, "result": res}
