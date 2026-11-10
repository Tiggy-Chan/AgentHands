"""MTOP HTTP client with automated signature calculation and cookie lifecycle."""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx

from . import output, protocol

API_REGISTRY: dict[str, tuple[str, str]] = {
    # Auth & IM
    "keepalive": ("mtop.taobao.idlemessage.pc.loginuser.get", "1.0"),
    "im_login_token": ("mtop.taobao.idlemessage.pc.login.token", "1.0"),
    "im_session_sync": ("mtop.taobao.idlemessage.pc.session.sync", "3.0"),
    "im_message_sync": ("mtop.taobao.idlemessage.pc.message.sync", "1.0"),
    "im_message_send": ("mtop.taobao.idlemessage.message.card.send", "1.0"),

    # Product
    "item_publish": ("mtop.taobao.idlefish.item.publish", "1.0"),
    "item_update": ("mtop.taobao.idlefish.item.update", "1.0"),
    "item_delete": ("mtop.taobao.idlefish.item.delete", "1.0"),
    "item_offline": ("mtop.taobao.idlefish.item.offline", "1.0"),
    "item_online": ("mtop.taobao.idlefish.item.online", "1.0"),
    "item_list": ("mtop.taobao.idlefish.item.list", "1.0"),
    "item_detail": ("mtop.taobao.idle.pc.detail", "1.0"),

    # Order
    "order_list": ("mtop.taobao.idlefish.order.list", "1.0"),
    "order_detail": ("mtop.taobao.idlefish.order.detail", "1.0"),
    "order_confirm": ("mtop.taobao.idlefish.order.confirm", "1.0"),
    "order_cancel": ("mtop.taobao.idlefish.order.cancel", "1.0"),
    "order_ship": ("mtop.taobao.idlefish.shipment.ship", "1.0"),
    "order_rate": ("mtop.taobao.idlefish.order.rate", "1.0"),

    # Search & Categories
    "search": ("mtop.taobao.idlemtopsearch.pc.search", "1.0"),
    "search_suggest": ("mtop.taobao.idlefish.search.suggest", "1.0"),
    "home_feed": ("mtop.taobao.idlehome.home.webpc.feed", "1.0"),
    "category_list": ("mtop.taobao.idlefish.category.list", "1.0"),

    # User & Address
    "user_info": ("mtop.idle.web.user.page.head", "1.0"),
    "address_list": ("mtop.taobao.idlefish.address.list", "1.0"),
    "address_add": ("mtop.taobao.idlefish.address.add", "1.0"),
    "address_update": ("mtop.taobao.idlefish.address.update", "1.0"),
    "address_delete": ("mtop.taobao.idlefish.address.delete", "1.0"),

    # Favorite
    "fav_item_add": ("mtop.taobao.idlefish.favorite.item.add", "1.0"),
    "fav_item_remove": ("mtop.taobao.idlefish.favorite.item.remove", "1.0"),
    "fav_item_list": ("mtop.taobao.idlefish.favorite.item.list", "1.0"),
    "fav_user_add": ("mtop.taobao.idlefish.favorite.user.add", "1.0"),
    "fav_user_remove": ("mtop.taobao.idlefish.favorite.user.remove", "1.0"),
}


class MtopError(Exception):
    """Raised when an MTOP API request fails."""
    def __init__(self, ret: list[str], data: dict[str, Any] | None = None):
        self.ret = ret
        self.data = data or {}
        super().__init__(f"MTOP error: {', '.join(ret)}")


class MtopClient:
    def __init__(self, cookies_str: str, user_id: str = ""):
        self.cookies_dict = protocol.trans_cookies(cookies_str)
        self.user_id = user_id or self.cookies_dict.get("unb", "")
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.goofish.com/",
            "Origin": "https://www.goofish.com",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    @property
    def cookies_str(self) -> str:
        return protocol.format_cookies(self.cookies_dict)

    def _get_token(self) -> str:
        tk = self.cookies_dict.get("_m_h5_tk", "")
        return tk.split("_")[0] if "_" in tk else tk

    def _build_params(self, api_name: str, version: str, data: dict[str, Any]) -> dict[str, str]:
        token = self._get_token()
        t = str(int(time.time() * 1000))
        data_str = json.dumps(data, ensure_ascii=False)
        sign = protocol.generate_sign(t, token, data_str)

        return {
            "jsv": "2.7.2",
            "appKey": protocol.APP_KEY,
            "t": t,
            "sign": sign,
            "v": version,
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "api": api_name,
            "data": data_str,
        }

    async def call(self, api: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        data = data or {}
        if api in API_REGISTRY:
            api_name, version = API_REGISTRY[api]
        else:
            api_name, version = api, "1.0"

        params = self._build_params(api_name, version, data)
        url = f"{protocol.H5API_BASE}/{api_name}/{version}/"

        headers = dict(self._headers)
        headers["Cookie"] = self.cookies_str

        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(url, params=params, headers=headers)

            # Update cookies from Set-Cookie headers
            for header_val in resp.headers.get_list("set-cookie"):
                parts = header_val.split(";")[0].split("=", 1)
                if len(parts) == 2:
                    k, v = parts[0].strip(), parts[1].strip()
                    self.cookies_dict[k] = v
                    output.register_secret(v)

            body = resp.json()
            ret = body.get("ret", [])
            ret_str = ret[0] if ret else ""
            if not ret_str.startswith("SUCCESS"):
                raise MtopError(ret, body.get("data"))

            return body.get("data") or {}
