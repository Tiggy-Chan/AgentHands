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
from urllib.parse import parse_qsl, urlsplit

from . import auth, browser_guard, config
from .errors import FeishuError, ResourceIsolationUnavailable
from .secure_store import atomic_write

_INTERESTING = (
    "/api/", "/gateway", "/im/", "/search", "/space/", "/passport/",
    "/mail", "/calendar", "/accounts/",
)


async def run(
    scope: str,
    keyword: str = "测试",
    headed: bool = False,
    msg_id: str = "",
) -> dict:
    from .browser_worker import run as run_worker

    if scope == "message-media":
        if not msg_id:
            raise ValueError("message-media probe 需要 --msg-id")
        return await run_worker("message-media-probe", {"msg_id": msg_id})
    return await run_worker("probe", {"scope": scope, "keyword": keyword, "headed": headed})


async def _run_impl(scope: str, keyword: str = "测试", headed: bool = False) -> dict:
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("网页 probe 只能由受限 Browser Worker 执行")
    if scope not in {"chats", "docs-search", "mail", "calendar", "all"}:
        raise ValueError("probe scope 必须是 chats、docs-search、mail、calendar 或 all")
    # Check that the skill has a login profile without exposing its contents.
    auth.load_cookies()
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright", hint="先运行 feishu.py login") from exc

    config.ensure_home()
    records: list[dict] = []
    pages: list[dict] = []
    blocked_requests: list[dict] = []
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=not headed,
            ignore_https_errors=False,
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=config.BROWSER_ARGS,
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            blocked_requests = await browser_guard.install(page)

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
                    "query_keys": tuple(sorted({key for key, _ in parse_qsl(parsed.query)})),
                    "status": None,
                    "response_content_length": None,
                }

            def on_request(request) -> None:
                if len(records) >= config.MAX_BROWSER_REQUESTS:
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
                        try:
                            item["response_content_length"] = int(
                                response.headers.get("content-length", "0") or 0
                            ) or None
                        except ValueError:
                            item["response_content_length"] = None
                        break

            page.on("request", on_request)
            page.on("response", on_response)
            domain = auth.load_cookies().get("domain") or config.DOMAIN

            if scope in {"chats", "all"}:
                requested = f"https://{domain}/next/messenger/"
                await page.goto(requested, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(8000)
                pages.append(await _page_meta("chats", requested, page))

            if scope in {"docs-search", "all"}:
                requested = f"https://{domain}/next/messenger/"
                await page.goto(requested, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2500)
                # Search only with a visible input; no page object/React tree
                # introspection and no message/document content collection.
                search = page.get_by_text("搜索", exact=True).first
                if await search.count():
                    await search.click()
                    await page.wait_for_timeout(1000)
                for selector in ('[contenteditable=true]', 'input[placeholder*="搜索"]', 'input[aria-label*="搜索"]', 'input'):
                    box = page.locator(selector).first
                    if await box.count():
                        try:
                            await box.fill(keyword[:512])
                            await box.press("Enter")
                            await page.wait_for_timeout(4000)
                            cloud = page.get_by_text("云文档", exact=True).last
                            if await cloud.count():
                                await cloud.click()
                                await page.wait_for_timeout(2500)
                            break
                        except Exception:
                            continue
                pages.append(await _page_meta("docs-search", requested, page))

            for resource, enabled in (("mail", scope in {"mail", "all"}), ("calendar", scope in {"calendar", "all"})):
                if not enabled:
                    continue
                url = (config.MAIL_URL if resource == "mail" else config.CALENDAR_URL)
                if not url:
                    url = f"https://{domain}/{resource}/"
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(8000)
                pages.append(await _page_meta(resource, url, page))

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
        "requests": records[:config.MAX_BROWSER_REQUESTS],
        "pages": pages[:10],
        "blocked_requests": blocked_requests[:100],
        "contains_bodies": False,
        "contains_credentials": False,
    }


def save(result: dict, path: str) -> None:
    target = Path(path).expanduser()
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode()
    atomic_write(target, payload)


async def _page_meta(scope: str, requested: str, page) -> dict:
    """Return navigation and DOM shape metadata without page content."""
    from urllib.parse import urlsplit

    def public_url(value: str) -> str:
        parsed = urlsplit(value)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    try:
        title = str(await page.title() or "")[:256]
    except Exception:
        title = ""
    try:
        shape = await page.evaluate(
            """() => {
              const els = Array.from(document.querySelectorAll('*')).slice(0, 1500);
              const count = {};
              const classes = new Set();
              for (const e of els) {
                const tag = e.tagName.toLowerCase();
                count[tag] = (count[tag] || 0) + 1;
                const role = e.getAttribute('role');
                if (role) count['role:' + role] = (count['role:' + role] || 0) + 1;
                for (const c of Array.from(e.classList).slice(0, 8)) {
                  if (/^[a-zA-Z][a-zA-Z0-9_-]{1,80}$/.test(c)) classes.add(c);
                }
              }
              return {
                sampled_elements: els.length,
                counts: count,
                classes: Array.from(classes).sort().slice(0, 300),
                selectors: {
                  inputs: document.querySelectorAll('input').length,
                  rows: document.querySelectorAll('[role="row"], tr').length,
                  links: document.querySelectorAll('a').length,
                  buttons: document.querySelectorAll('button').length,
                  contenteditables: document.querySelectorAll('[contenteditable="true"]').length,
                  data_attributes: Array.from(new Set(els.flatMap(e =>
                    Array.from(e.attributes).map(a => a.name).filter(n => n.startsWith('data-'))
                  ))).sort().slice(0, 200)
                }
              };
            }"""
        )
    except Exception:
        shape = {"error": "dom_shape_unavailable"}
    return {
        "scope": scope,
        "requested_url": public_url(requested),
        "final_url": public_url(page.url),
        "title": title,
        "dom_shape": shape,
    }
