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

from . import event_store, gateway
from . import config
from .errors import EndpointNotAdapted
from .messages import _CHAT_TYPE


async def list_chats(from_events: bool = False) -> list[dict]:
    if not from_events:
        return await _list_from_browser()
    seen: dict[str, dict] = {}
    for ev in event_store.read_events(limit=100000):
        cid = ev.get("chat_id")
        if not cid:
            continue
        cur = seen.setdefault(
            cid,
            {
                "chat_id": cid,
                "chat_type": _CHAT_TYPE.get(ev.get("chat_type", 0), "?"),
                "last_text": "",
                "last_time": 0,
                "source": "events",
                "member_count": None,
                "can_query": True,
                "status": "seen",
            },
        )
        event_time = event_store.event_time_ms(ev)
        if event_time >= cur["last_time"]:
            cur["last_time"] = event_time
            cur["last_text"] = (ev.get("text") or "")[:60]
    chats = sorted(seen.values(), key=lambda c: -c["last_time"])
    for c in chats:
        try:
            c["name"] = await gateway.get_group_name(c["chat_id"])
        except Exception:
            c["name"] = ""
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
    """Read the currently visible Messenger feed cards through a short-lived
    persistent browser context.

    The web client currently renders feed entities in its runtime state rather
    than exposing a stable HTTP list endpoint.  This provider deliberately
    reads ordinary DOM attributes only; it never walks React internals or
    captures page bodies.  ``source`` and ``complete`` make the limitation
    visible to callers.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise EndpointNotAdapted(
            "当前环境没有 playwright，无法读取 Messenger 会话列表",
            hint="先运行 feishu.py login 完成依赖安装",
        ) from exc

    from . import auth

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
            args=["--no-proxy-server"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(f"https://{domain}/next/messenger/", wait_until="domcontentloaded", timeout=60000)
            for _ in range(8):
                await page.wait_for_timeout(700)
                cards = page.locator("[data-feed-id]")
                count = min(await cards.count(), 500)
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
                    lines = [line.strip() for line in item.get("text", "").splitlines() if line.strip()]
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
                    hint="运行 feishu.py probe --scope chats 检查网页协议是否变化",
                )
        finally:
            await ctx.close()
    complete = stable_rounds >= 2 and not has_more_marker
    for row in rows.values():
        row["complete"] = complete
        row["completeness"] = "ui_snapshot"
    return sorted(rows.values(), key=lambda row: (-row.get("last_time", 0), row.get("name", "")))


async def known_chat_ids() -> list[str]:
    """chat_ids we can already query: daemon events (reliable superset over time)."""
    ids = []
    seen = set()
    for ev in event_store.read_events(limit=100000):
        cid = ev.get("chat_id")
        if cid and cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids
