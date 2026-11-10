"""Creator analytics and profile metrics for Xiaohongshu."""

from __future__ import annotations

from typing import Any

from . import auth, config


async def get_creator_profile() -> dict[str, Any]:
    """Retrieve creator profile information and follower stats."""
    from playwright.async_api import async_playwright

    cookies = auth.parse_cookie_list()
    config.ensure_dirs()

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=True,
        )
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()
        await page.goto("https://creator.xiaohongshu.com/creator/home", wait_until="networkidle")

        name = await page.locator('.creator-name, .user-name').first.inner_text() if await page.locator('.creator-name, .user-name').count() else "Unknown"
        fans = await page.locator('.fans-count, .data-count').first.inner_text() if await page.locator('.fans-count, .data-count').count() else "0"

        await context.close()

    return {
        "creator_name": name.strip(),
        "followers": fans.strip(),
    }
