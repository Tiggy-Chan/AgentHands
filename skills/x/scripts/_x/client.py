"""GraphQL client for X internal web endpoints."""

from __future__ import annotations

import os
from typing import Any
import httpx

from . import auth, config, output


class XClientError(Exception):
    """Raised when an X API request fails."""
    pass


class XClient:
    def __init__(self, auth_token: str, ct0: str):
        self.auth_token = auth_token
        self.ct0 = ct0
        self.proxy = os.environ.get("X_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self._headers = {
            "authorization": f"Bearer {config.DEFAULT_BEARER_TOKEN}",
            "x-csrf-token": ct0,
            "cookie": f"auth_token={auth_token}; ct0={ct0}",
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            ),
            "content-type": "application/json",
            "referer": "https://x.com/",
        }

    async def graphql_get(self, query_id: str, endpoint: str, variables: dict[str, Any], features: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send GET request to X GraphQL endpoint."""
        import json
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
        }
        if features:
            params["features"] = json.dumps(features, separators=(",", ":"))

        url = f"{config.BASE_URL}/i/api/graphql/{query_id}/{endpoint}"
        async with httpx.AsyncClient(timeout=25.0, proxy=self.proxy) as client:
            resp = await client.get(url, params=params, headers=self._headers)
            if resp.status_code != 200:
                raise XClientError(f"HTTP {resp.status_code}: {output.scrub(resp.text[:300])}")
            data = resp.json()
            if "errors" in data:
                err_msg = "; ".join(e.get("message", "Error") for e in data["errors"])
                raise XClientError(f"GraphQL Error: {err_msg}")
            return data.get("data") or {}

    async def graphql_post(self, query_id: str, endpoint: str, variables: dict[str, Any], features: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send POST request to X GraphQL mutation endpoint."""
        body: dict[str, Any] = {
            "variables": variables,
            "queryId": query_id,
        }
        if features:
            body["features"] = features

        url = f"{config.BASE_URL}/i/api/graphql/{query_id}/{endpoint}"
        async with httpx.AsyncClient(timeout=25.0, proxy=self.proxy) as client:
            resp = await client.post(url, json=body, headers=self._headers)
            if resp.status_code != 200:
                raise XClientError(f"HTTP {resp.status_code}: {output.scrub(resp.text[:300])}")
            data = resp.json()
            if "errors" in data:
                err_msg = "; ".join(e.get("message", "Error") for e in data["errors"])
                raise XClientError(f"GraphQL Error: {err_msg}")
            return data.get("data") or {}


def get_client() -> XClient:
    """Build XClient from loaded credentials."""
    creds = auth.load_credentials()
    if not (creds["auth_token"] and creds["ct0"]):
        raise RuntimeError("Missing X credentials: set X_AUTH_TOKEN and X_CT0 or run `x.py login`.")
    return XClient(creds["auth_token"], creds["ct0"])
