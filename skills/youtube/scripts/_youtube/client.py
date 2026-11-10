"""InnerTube and Studio API client for YouTube."""

from __future__ import annotations

import os
from typing import Any
import httpx

from . import auth, config, output


class YouTubeClientError(Exception):
    """Raised when an operation against YouTube fails."""
    pass


class YouTubeClient:
    def __init__(self):
        self.creds = auth.load_credentials()
        self.proxy = os.environ.get("YOUTUBE_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.context = {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240101.00.00",
                "hl": "en",
                "gl": "US",
            }
        }

    def _get_headers(self, origin: str = "https://www.youtube.com") -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            ),
            "Origin": origin,
            "X-Origin": origin,
            "Referer": f"{origin}/",
        }
        if self.creds.get("cookies"):
            headers["Cookie"] = self.creds["cookies"]
        if self.creds.get("sapisid"):
            headers["Authorization"] = auth.generate_sapisid_hash(self.creds["sapisid"], origin)
        return headers

    async def call_innertube(self, endpoint: str, payload: dict[str, Any] | None = None, is_studio: bool = False) -> Any:
        base = config.STUDIO_BASE_URL if is_studio else config.INNERTUBE_BASE_URL
        origin = "https://studio.youtube.com" if is_studio else "https://www.youtube.com"
        url = f"{base}/{endpoint.lstrip('/')}"

        body = dict(payload or {})
        if "context" not in body:
            body["context"] = self.context

        headers = self._get_headers(origin)

        async with httpx.AsyncClient(timeout=25.0, proxy=self.proxy) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                raise YouTubeClientError(f"HTTP {resp.status_code}: {output.scrub(resp.text[:300])}")
            return resp.json()


def get_client() -> YouTubeClient:
    return YouTubeClient()
