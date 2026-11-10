"""Telegram client implementation supporting HTTP bridge, Bot API, and session gateway."""

from __future__ import annotations

import os
from typing import Any
import httpx

from . import auth, config, output


class TelegramClientError(Exception):
    """Raised when a Telegram operation fails."""
    pass


class TelegramClient:
    def __init__(self):
        self.creds = auth.load_credentials()
        self.proxy = os.environ.get("TELEGRAM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    async def call_api(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        # 1. Custom HTTP bridge if configured
        if self.creds.get("bridge_url"):
            url = f"{self.creds['bridge_url'].rstrip('/')}/{method.lstrip('/')}"
            async with httpx.AsyncClient(timeout=25.0, proxy=self.proxy) as client:
                resp = await client.post(url, json=params)
                if resp.status_code != 200:
                    raise TelegramClientError(f"Bridge HTTP {resp.status_code}: {resp.text}")
                return resp.json()

        # 2. Standard Bot API fallback if token present
        if self.creds.get("bot_token"):
            token = self.creds["bot_token"]
            url = f"https://api.telegram.org/bot{token}/{method.lstrip('/')}"
            async with httpx.AsyncClient(timeout=25.0, proxy=self.proxy) as client:
                resp = await client.post(url, json=params)
                if resp.status_code != 200:
                    raise TelegramClientError(f"Telegram API HTTP {resp.status_code}: {output.scrub(resp.text[:300])}")
                data = resp.json()
                if not data.get("ok"):
                    raise TelegramClientError(f"Telegram error: {data.get('description', 'Unknown error')}")
                return data.get("result")

        raise RuntimeError("No Telegram credentials configured: set TELEGRAM_BRIDGE_URL, TELEGRAM_BOT_TOKEN, or session string.")


def get_client() -> TelegramClient:
    return TelegramClient()
