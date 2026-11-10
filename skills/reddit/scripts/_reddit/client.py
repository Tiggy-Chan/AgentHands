"""HTTP and API client for Reddit."""

from __future__ import annotations

import os
from typing import Any
import httpx

from . import auth, config, output


class RedditClientError(Exception):
    """Raised when an operation against Reddit API fails."""
    pass


class RedditClient:
    def __init__(self):
        self.creds = auth.load_credentials()
        self.proxy = os.environ.get("REDDIT_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.user_agent = os.environ.get("REDDIT_USER_AGENT") or config.DEFAULT_USER_AGENT

    def _get_base_and_headers(self) -> tuple[str, dict[str, str]]:
        headers = {
            "User-Agent": self.user_agent,
        }
        if self.creds.get("token"):
            headers["Authorization"] = f"bearer {self.creds['token']}"
            return config.OAUTH_URL, headers
        elif self.creds.get("session"):
            headers["Cookie"] = f"reddit_session={self.creds['session']}"
            return config.BASE_URL, headers
        else:
            return config.BASE_URL, headers

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        base, headers = self._get_base_and_headers()
        url = f"{base}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=25.0, proxy=self.proxy) as client:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                raise RedditClientError(f"HTTP {resp.status_code}: {output.scrub(resp.text[:300])}")
            return resp.json()

    async def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        base, headers = self._get_base_and_headers()
        url = f"{base}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=25.0, proxy=self.proxy) as client:
            resp = await client.post(url, data=data or {}, headers=headers)
            if resp.status_code not in (200, 201):
                raise RedditClientError(f"HTTP {resp.status_code}: {output.scrub(resp.text[:300])}")
            return resp.json()


def get_client() -> RedditClient:
    return RedditClient()
