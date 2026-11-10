"""OneBot 11 HTTP client implementation for QQ."""

from __future__ import annotations

from typing import Any
import httpx

from . import auth, output


class OneBotError(Exception):
    """Raised when an action fails on the OneBot server."""
    def __init__(self, retcode: int, msg: str, data: Any = None):
        self.retcode = retcode
        self.msg = msg
        self.data = data
        super().__init__(f"OneBot error ({retcode}): {msg}")


async def call_action(action: str, params: dict[str, Any] | None = None) -> Any:
    """Call an action on the OneBot HTTP server."""
    creds = auth.load_credentials()
    base_url = creds["url"].rstrip("/")
    token = creds.get("token")

    url = f"{base_url}/{action.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(url, json=params or {}, headers=headers)
        except Exception as exc:
            raise ConnectionError(f"Failed to connect to OneBot server at {base_url}: {output.scrubbed_exc(exc)}")

        if resp.status_code != 200:
            raise OneBotError(resp.status_code, f"HTTP {resp.status_code}: {resp.text}")

        res = resp.json()
        status = res.get("status")
        retcode = res.get("retcode", -1)

        if status == "failed" or retcode != 0:
            msg = res.get("msg") or res.get("wording") or "Action failed"
            raise OneBotError(retcode, msg, res.get("data"))

        return res.get("data")
