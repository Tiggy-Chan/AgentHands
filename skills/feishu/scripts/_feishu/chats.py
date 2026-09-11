"""Chat list providers.

The tenant's stable RPC schema is not yet mapped.  The primary provider reads
bounded Messenger feed-card DOM data from a short-lived persistent browser;
``--from-events`` remains the historical event-pool fallback.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime

from . import event_store, gateway, messages
from . import config
from .errors import EndpointNotAdapted, ResourceIsolationUnavailable
from .messages import _CHAT_TYPE


def _daemon_my_id() -> str:
    """my_user_id recorded by the running daemon; '' when never seen."""
    return _daemon_identity()[0]


def _daemon_identity() -> tuple[str, str]:
    """(my_user_id, my_name) recorded by the daemon; ('', '') when unseen."""
    try:
        import json
        state = json.loads(config.DAEMON_STATE.read_text())
        return str(state.get("my_user_id") or ""), str(state.get("my_name") or "")
    except Exception:
        return "", ""


async def list_chats(from_events: bool = False) -> list[dict]:
    if not from_events:
        return await _list_from_browser()
    seen: dict[str, dict] = {}
    for cid, summary in event_store.chat_summaries().items():
        cur = seen.setdefault(
            cid,
            {
                "chat_id": cid,
                "chat_type": _CHAT_TYPE.get(summary.get("chat_type", 0), "?"),
                "last_text": "",
                "last_time": 0,
                "source": "events",
                "member_count": None,
                "can_query": True,
                "status": "seen",
                "permission_error": None,
                "complete": False,
                "completeness": "event_snapshot",
            },
        )
        cur["last_time"] = summary.get("last_time", 0)
        cur["last_text"] = summary.get("last_text", "")[:60]
    chats = sorted(seen.values(), key=lambda c: -c["last_time"])
    cached_names = messages._load_names()["chats"]
    for c in chats:
        # cmd=64 is dead tenant-side; names come from the cache (seeded by
        # the browser provider harvest) instead of a live lookup.
        c["name"] = cached_names.get(c["chat_id"], "")
    return chats


def _sidebar_time(lines: list[str]) -> int:
    """Best-effort local timestamp for the time label rendered in a feed card."""
    now = datetime.now().astimezone()
    for line in lines:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", line)
        if match:
            return int(now.replace(
                hour=int(match.group(1)), minute=int(match.group(2)),
                second=0, microsecond=0,
            ).timestamp() * 1000)
        match = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", line)
        if match:
            try:
                return int(now.replace(
                    month=int(match.group(1)), day=int(match.group(2)),
                    hour=0, minute=0, second=0, microsecond=0,
                ).timestamp() * 1000)
            except ValueError:
                continue
    return 0


async def _list_from_browser() -> list[dict]:
    from .browser_worker import run

    result = await run("chats", {})
    return result.get("items", [])


async def _list_from_browser_impl() -> list[dict]:
    """Read the currently visible Messenger feed cards through a short-lived
    persistent browser context.

    The web client currently renders feed entities in its runtime state rather
    than exposing a stable HTTP list endpoint.  This provider deliberately
    reads ordinary DOM attributes only; it never walks React internals or
    captures page bodies.  ``source`` and ``complete`` make the limitation
    visible to callers.
    """
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("Messenger 浏览器读取只能由受限 Browser Worker 执行")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise EndpointNotAdapted(
            "当前环境没有 playwright，无法读取 Messenger 会话列表",
            hint="先运行 feishu.py login 完成依赖安装",
        ) from exc

    from . import auth
    from . import browser_guard

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    rows: dict[str, dict] = {}
    cached = event_store.chat_summaries()
    previous_ids: set[str] = set()
    stable_rounds = 0
    has_more_marker = False
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=True,
            ignore_https_errors=False,
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=config.BROWSER_ARGS,
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await ctx.add_cookies(auth.playwright_cookie_jar())
            blocked = await browser_guard.install(page)
            await page.goto(f"https://{domain}/next/messenger/", wait_until="domcontentloaded", timeout=60000)
            # The SPA authenticates and renders feed cards asynchronously;
            # a fixed short sleep raced the render and produced a bogus
            # "协议变了" EndpointNotAdapted (observed 2026-09-10).
            try:
                await page.wait_for_selector("[data-feed-id]", timeout=25000)
            except Exception:
                await auth.raise_if_login_redirect(page)
            for _ in range(8):
                await page.wait_for_timeout(700)
                cards = page.locator("[data-feed-id]")
                count = min(await cards.count(), config.MAX_BROWSER_CARDS)
                for i in range(count):
                    card = cards.nth(i)
                    item = await card.evaluate(
                        """e => ({
                          id: e.getAttribute('data-feed-id') || '',
                          name: (e.querySelector('[style*="min-width"]')?.textContent || '').trim(),
                          text: (e.innerText || '').trim()
                        })"""
                    )
                    cid = item.get("id", "")
                    if not cid:
                        continue
                    lines = [line.strip() for line in item.get("text", "")[:config.MAX_BROWSER_TEXT_BYTES].splitlines() if line.strip()]
                    name = item.get("name") or (lines[0] if lines else "")
                    rest = [line for line in lines if line != name and line not in {"1", "2", "3"}]
                    preview = rest[1] if len(rest) > 1 else (rest[0] if rest else "")
                    rows[cid] = {
                        "chat_id": cid,
                        "name": name,
                        "chat_type": "unknown",
                        "last_text": preview[:200],
                        "last_time": _sidebar_time(lines),
                        "source": "browser_dom",
                        "complete": False,
                        "member_count": None,
                        "can_query": True,
                        "status": "visible",
                        "permission_error": None,
                    }
                    if cid in cached:
                        local = cached[cid]
                        rows[cid]["chat_type"] = _CHAT_TYPE.get(local.get("chat_type", 0), "unknown")
                        rows[cid]["last_time"] = max(rows[cid]["last_time"], local.get("last_time", 0))
                        if local.get("last_text"):
                            rows[cid]["last_text"] = local["last_text"]
                        rows[cid]["source"] = "browser_dom+events"
                await page.mouse.wheel(0, 1200)
                current_ids = set(rows)
                if current_ids == previous_ids:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                previous_ids = current_ids
            has_more_marker = bool(await page.get_by_text("加载更多", exact=True).count())
            if not rows:
                raise EndpointNotAdapted(
                    "Messenger 未渲染出会话卡片",
                    hint=f"运行 feishu.py probe --scope chats 检查网页协议是否变化（blocked={len(blocked)}）",
                )
        finally:
            await ctx.close()
    # A DOM snapshot is never evidence of the account's complete conversation
    # set: Messenger can virtualize or lazily load cards without a marker.
    complete = False
    for row in rows.values():
        row["complete"] = complete
        row["completeness"] = "ui_snapshot"
        row["pagination_observed"] = not has_more_marker
        row["last_message_time"] = row.get("last_time", 0)
    # Feed the display names back into the name cache; for known p2p chats the
    # conversation title is the peer's name (cmd=64/5023 are dead tenant-side).
    messages.harvest_chat_names(list(rows.values()), summaries=cached, my_id=_daemon_my_id())
    return sorted(rows.values(), key=lambda row: (-row.get("last_time", 0), row.get("name", "")))


async def known_chat_ids() -> list[str]:
    """chat_ids we can already query: daemon events (reliable superset over time)."""
    return list(event_store.chat_summaries().keys())
