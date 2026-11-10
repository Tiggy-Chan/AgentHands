"""Search and comment retrieval for Xiaohongshu."""

from __future__ import annotations

from typing import Any

from . import auth, config


async def search_notes(
    keyword: str,
    sort: str = "general",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search Xiaohongshu notes."""
    from playwright.async_api import async_playwright

    cookies = auth.parse_cookie_list()
    config.ensure_dirs()

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()
        url = f"https://www.xiaohongshu.com/search_result?keyword={keyword}&source=web_search_result_notes"
        await page.goto(url, wait_until="networkidle")

        cards = page.locator('.note-item')
        count = await cards.count()

        results: list[dict[str, Any]] = []
        for i in range(min(count, limit)):
            card = cards.nth(i)
            title = await card.locator('.title').inner_text() if await card.locator('.title').count() else ""
            author = await card.locator('.author').inner_text() if await card.locator('.author').count() else ""
            likes = await card.locator('.count').inner_text() if await card.locator('.count').count() else "0"
            href = await card.locator('a').first.get_attribute('href') if await card.locator('a').count() else ""

            note_id = href.split('/')[-1] if href else ""
            results.append({
                "note_id": note_id,
                "title": title.strip(),
                "author": author.strip(),
                "likes": likes.strip(),
                "url": f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else "",
            })

        await context.close()
    return results


async def get_comments(
    note_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Fetch comments on a specific Xiaohongshu note."""
    from playwright.async_api import async_playwright

    cookies = auth.parse_cookie_list()
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=True,
        )
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()
        await page.goto(f"https://www.xiaohongshu.com/explore/{note_id}", wait_until="networkidle")

        items = page.locator('.comment-item')
        count = await items.count()

        comments: list[dict[str, Any]] = []
        for i in range(min(count, limit)):
            item = items.nth(i)
            author = await item.locator('.author-name').inner_text() if await item.locator('.author-name').count() else ""
            content = await item.locator('.content').inner_text() if await item.locator('.content').count() else ""
            likes = await item.locator('.like .count').inner_text() if await item.locator('.like .count').count() else "0"
            comments.append({
                "author": author.strip(),
                "message": content.strip(),
                "likes": likes.strip(),
            })

        await context.close()
    return comments
