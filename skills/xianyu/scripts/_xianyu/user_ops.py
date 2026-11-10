"""User profile and shipping address management for Xianyu."""

from __future__ import annotations

from typing import Any

from . import auth, security


async def get_self_info() -> dict[str, Any]:
    """Retrieve logged-in account information."""
    client = auth.get_client()
    res = await client.call("keepalive")
    user_id = res.get("userId") or client.user_id
    return {
        "user_id": str(user_id),
        "status": "authenticated",
        "has_token": bool(client._get_token()),
    }


async def list_addresses() -> list[dict[str, Any]]:
    """List saved delivery addresses."""
    client = auth.get_client()
    res = await client.call("address_list")
    addr_list = res.get("addressList") or res.get("addresses") or []

    cleaned: list[dict[str, Any]] = []
    for a in addr_list:
        cleaned.append({
            "address_id": str(a.get("addressId") or a.get("id", "")),
            "recipient": a.get("name") or a.get("fullName", ""),
            "phone": a.get("mobile") or a.get("phone", ""),
            "province": a.get("province", ""),
            "city": a.get("city", ""),
            "area": a.get("area", ""),
            "detail": a.get("addressDetail", ""),
            "is_default": bool(a.get("isDefault")),
        })
    return cleaned


async def add_address(
    name: str,
    phone: str,
    province: str,
    city: str,
    area: str,
    detail: str,
    is_default: bool = False,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Add new delivery address with dual-gate write verification."""
    payload = {
        "recipient": name,
        "phone": phone,
        "province": province,
        "city": city,
        "area": area,
        "detail": detail,
        "is_default": is_default,
    }
    security.require_write_allowed("add_address", payload, execute, direct, expected_fp)

    client = auth.get_client()
    data = {
        "fullName": name,
        "mobile": phone,
        "prov": province,
        "city": city,
        "area": area,
        "addressDetail": detail,
        "isDefault": 1 if is_default else 0,
    }
    res = await client.call("address_add", data)
    return {"status": "ok", "action": "add_address", "result": res}


async def delete_address(
    address_id: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Remove delivery address with dual-gate write verification."""
    payload = {"address_id": str(address_id)}
    security.require_write_allowed("delete_address", payload, execute, direct, expected_fp)

    client = auth.get_client()
    res = await client.call("address_delete", {"addressId": str(address_id)})
    return {"status": "ok", "action": "delete_address", "address_id": address_id, "result": res}
