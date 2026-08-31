"""Remote and local document search providers.

The current tenant's search UI renders result cards through the web runtime
without exposing a stable HTTP endpoint.  Remote search therefore uses a
short-lived persistent browser context and reads bounded ordinary DOM fields;
it never inspects React internals or stores page bodies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import auth, config
from .errors import FeishuError
from .secure_store import decrypt_blob


async def search(keyword: str, local: bool = False, limit: int = 30) -> dict:
    if not keyword.strip():
        raise ValueError("文档搜索关键词不能为空")
    if not 1 <= limit <= 200:
        raise ValueError("文档搜索 limit 必须在 1..200 之间")
    if local:
        return _local_search(keyword, limit)
    return await _browser_search(keyword, limit)


async def _browser_search(keyword: str, limit: int) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright", hint="先运行 feishu.py login") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    cards: list[dict] = []
    complete_marker_found = False
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
            await page.wait_for_timeout(2500)
            await page.get_by_text("搜索", exact=True).first.click()
            await page.wait_for_timeout(1200)
            editor = page.locator("[contenteditable=true]").first
            if not await editor.count():
                # The search panel can render asynchronously after a cached
                # Messenger load. One bounded retry is enough; never inspect
                # the page's React object graph.
                await page.get_by_text("搜索", exact=True).first.click()
                await page.wait_for_timeout(1200)
                editor = page.locator("[contenteditable=true]").first
            if not await editor.count():
                raise FeishuError("搜索框未渲染", hint="运行 feishu.py probe --scope docs-search 检查网页变化")
            await editor.click()
            await page.keyboard.type(keyword)
            await page.wait_for_timeout(1200)
            cloud = page.get_by_text("云文档", exact=True).last
            if await cloud.count():
                await cloud.click()
            await page.wait_for_timeout(4500)

            result_cards = page.locator(".search-doc-card-box")
            raw_count = await result_cards.count()
            seen = set()
            for i in range(min(raw_count, limit * 3)):
                card = result_cards.nth(i)
                title = await card.locator(".doc-title-text").first.inner_text()
                summary = ""
                subtitle = card.locator(".doc-info-subtitle").first
                if await subtitle.count():
                    summary = (await subtitle.inner_text()).strip()
                tags = [
                    (await card.locator(".search-tag").nth(j).inner_text()).strip()
                    for j in range(min(await card.locator(".search-tag").count(), 4))
                ]
                meta = (await card.locator(".doc-extra-info").first.inner_text()).strip()
                key = (title.strip(), summary.strip(), meta.strip())
                if key in seen:
                    continue
                seen.add(key)
                if len(cards) >= limit:
                    break
                cards.append({
                    "name": title.strip(),
                    "type": tags[-1] if tags else "doc",
                    "summary": summary[:1000],
                    "meta": meta[:500],
                    "url": "",
                    "source": "browser_dom",
                })
            complete_marker_found = bool(await page.get_by_text("已经展示全部结果", exact=True).count())
        finally:
            await ctx.close()
    complete = complete_marker_found and len(cards) <= limit
    return {
        "keyword": keyword,
        "scope": "remote",
        "hits": cards,
        "total": len(cards),
        "complete": complete,
        "note": "飞书网页端云文档搜索结果（当前租户未暴露稳定 REST 搜索接口）",
    }


def _iter_cached_markdown():
    for root in (config.DOCS_CACHE_DIR, config.DOCS_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.suffix == ".md":
                    yield path, path.read_bytes()
                elif path.suffix == ".enc":
                    yield path, decrypt_blob(path.read_bytes(), aad=b"agenthands/feishu/docs/v2")
            except Exception:
                continue


def _local_search(keyword: str, limit: int) -> dict:
    hits = []
    kw = keyword.lower()
    if config.DOCS_INDEX.exists():
        for line in config.DOCS_INDEX.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                node = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kw in (node.get("name") or "").lower() or kw in (node.get("path") or "").lower():
                hits.append({**node, "match": "name"})
    for path, raw in _iter_cached_markdown() or ():
        text = raw.decode("utf-8", errors="replace")
        low = text.lower()
        if kw in low:
            i = low.find(kw)
            hits.append({
                "name": path.stem,
                "type": "cached_md",
                "path": str(path),
                "match": "content",
                "snippet": text[max(0, i - 60): i + 60].strip(),
            })
    return {
        "keyword": keyword,
        "scope": "local",
        "hits": hits[:limit],
        "total": len(hits),
        "complete": True,
        "note": "本地缓存搜索（doc --walk 的索引 + 已导出的文档），不是远程全量结果",
    }
