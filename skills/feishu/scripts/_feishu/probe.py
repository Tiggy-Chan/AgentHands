"""Safe browser-side protocol probe.

Only request metadata is retained.  Bodies, cookies, query strings, HAR files,
and rendered message/document content are deliberately never collected.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import auth, config
from .errors import FeishuError
from .secure_store import atomic_write

_INTERESTING = ("/api/", "/gateway", "/im/", "/search", "/space/", "/passport/")


async def run(scope: str, keyword: str = "测试", headed: bool = False) -> dict:
    if scope not in {"chats", "docs-search", "all"}:
        raise ValueError("probe scope 必须是 chats、docs-search 或 all")
    # Check that the skill has a login profile without exposing its contents.
    auth.load_cookies()
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright", hint="先运行 feishu.py login") from exc

    config.ensure_home()
    records: list[dict] = []
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=not headed,
            ignore_https_errors=False,
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=["--no-proxy-server"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            def request_meta(request) -> dict | None:
                parsed = urlsplit(request.url)
                if "feishu" not in parsed.netloc or not any(
                    part in parsed.path.lower() for part in _INTERESTING
                ):
                    return None
                return {
                    "method": request.method,
                    "host": parsed.netloc,
                    "path": parsed.path,
                    "x_command": request.headers.get("x-command", ""),
                    "content_type": request.headers.get("content-type", ""),
                    "status": None,
                }

            def on_request(request) -> None:
                if len(records) >= 1000:
                    return
                item = request_meta(request)
                if item:
                    records.append(item)

            def on_response(response) -> None:
                parsed = urlsplit(response.url)
                key = (parsed.netloc, parsed.path, response.request.method,
                       response.request.headers.get("x-command", ""))
                for item in reversed(records):
                    if (item["host"], item["path"], item["method"], item["x_command"]) == key:
                        item["status"] = response.status
                        break

            page.on("request", on_request)
            page.on("response", on_response)
            domain = auth.load_cookies().get("domain") or config.DOMAIN

            if scope in {"chats", "all"}:
                await page.goto(f"https://{domain}/next/messenger/", wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(8000)

            if scope in {"docs-search", "all"}:
                await page.goto(f"https://{domain}/search/", wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2500)
                # Search only with a visible input; no page object/React tree
                # introspection and no message/document content collection.
                for selector in ('input[placeholder*="搜索"]', 'input[aria-label*="搜索"]', 'input'):
                    box = page.locator(selector).first
                    if await box.count():
                        try:
                            await box.fill(keyword)
                            await box.press("Enter")
                            await page.wait_for_timeout(4000)
                            break
                        except Exception:
                            continue

            # Deduplicate metadata while preserving observed order.
            unique = []
            seen = set()
            for item in records:
                key = tuple(item.items())
                if key not in seen:
                    seen.add(key)
                    unique.append(item)
            records = unique
        finally:
            await ctx.close()

    return {
        "schema": 1,
        "scope": scope,
        "keyword": keyword if scope in {"docs-search", "all"} else None,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "requests": records,
        "contains_bodies": False,
        "contains_credentials": False,
    }


def save(result: dict, path: str) -> None:
    target = Path(path).expanduser()
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode()
    atomic_write(target, payload)
