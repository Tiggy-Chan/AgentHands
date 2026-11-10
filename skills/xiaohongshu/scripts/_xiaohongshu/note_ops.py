"""Note publishing and media operations for Xiaohongshu."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import auth, config, security


async def publish_image_note(
    title: str,
    content: str,
    images: list[str],
    topics: list[str] | None = None,
    card_style: str = "基础",
    draft: bool = False,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Publish image-text note to Xiaohongshu Creator Center."""
    if len(title) > 20:
        raise ValueError(f"Title exceeds 20 characters: {len(title)}")
    if not images:
        raise ValueError("At least one image is required")
    for img in images:
        if not os.path.exists(img):
            raise FileNotFoundError(f"Image not found: {img}")

    payload = {
        "title": title,
        "content": content,
        "images": images,
        "topics": topics or [],
        "card_style": card_style,
        "draft": draft,
    }
    security.require_write_allowed("publish_image_note", payload, execute, direct, expected_fp)

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
        await page.goto("https://creator.xiaohongshu.com/publish/publish?target=image", wait_until="networkidle")

        # Set file input
        file_input = page.locator('input[type="file"]')
        if await file_input.count() > 0:
            await file_input.set_input_files(images)
            await page.wait_for_timeout(2000)

        # Title
        title_input = page.locator('input[placeholder*="填写标题"], input[class*="title"]').first
        if await title_input.count() > 0:
            await title_input.fill(title)

        # Content
        content_input = page.locator('textarea[placeholder*="填写更全面的信息"], div[contenteditable="true"]').first
        if await content_input.count() > 0:
            await content_input.fill(content)

        # Topics
        if topics:
            for t in topics:
                await content_input.type(f" #{t} ")

        # Submit or Draft
        if draft:
            draft_btn = page.locator('button:has-text("存草稿"), button:has-text("保存草稿")').first
            if await draft_btn.count() > 0:
                await draft_btn.click()
                await page.wait_for_timeout(2000)
                await context.close()
                return {"status": "ok", "action": "draft_saved", "title": title}

        publish_btn = page.locator('button:has-text("发布"), .publishBtn').first
        if await publish_btn.count() > 0:
            await publish_btn.click()
            await page.wait_for_timeout(3000)

        await context.close()
        return {"status": "ok", "action": "published", "title": title}


async def publish_video_note(
    title: str,
    content: str,
    video_path: str,
    cover_path: str | None = None,
    topics: list[str] | None = None,
    draft: bool = False,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Publish video note to Xiaohongshu Creator Center with Shadow DOM support."""
    if len(title) > 20:
        raise ValueError(f"Title exceeds 20 characters: {len(title)}")
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    payload = {
        "title": title,
        "content": content,
        "video": video_path,
        "cover": cover_path or "",
        "topics": topics or [],
        "draft": draft,
    }
    security.require_write_allowed("publish_video_note", payload, execute, direct, expected_fp)

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
        await page.goto("https://creator.xiaohongshu.com/publish/publish?target=video", wait_until="networkidle")

        file_input = page.locator('input[type="file"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(video_path)
            await page.wait_for_timeout(3000)

        title_input = page.locator('input[placeholder*="填写标题"]').first
        if await title_input.count() > 0:
            await title_input.fill(title)

        content_input = page.locator('textarea[placeholder*="填写更全面的信息"], div[contenteditable="true"]').first
        if await content_input.count() > 0:
            await content_input.fill(content)

        # Trigger internal publish handler if shadow root is present
        await page.evaluate("""() => {
            const btn = document.querySelector('xhs-publish-btn');
            if (btn && typeof btn._onPublish === 'function') {
                btn._onPublish();
            }
        }""")
        await page.wait_for_timeout(3000)
        await context.close()

    return {"status": "ok", "action": "video_published", "title": title}


async def delete_note(
    note_id: str,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Delete a published note."""
    payload = {"note_id": str(note_id), "action": "delete"}
    security.require_write_allowed("delete_note", payload, execute, direct, expected_fp)

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
        await page.goto(f"https://creator.xiaohongshu.com/creator/notes?noteId={note_id}", wait_until="networkidle")
        del_btn = page.locator('button:has-text("删除")').first
        if await del_btn.count() > 0:
            await del_btn.click()
            confirm = page.locator('.modal-confirm, button:has-text("确定")').first
            if await confirm.count() > 0:
                await confirm.click()

        await context.close()
    return {"status": "ok", "action": "delete_note", "note_id": note_id}


async def get_note_detail(note_id: str) -> dict[str, Any]:
    """Retrieve public note details and stats."""
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

        title = await page.locator('#detail-title').inner_text() if await page.locator('#detail-title').count() else ""
        desc = await page.locator('#detail-desc').inner_text() if await page.locator('#detail-desc').count() else ""
        likes = await page.locator('.like-wrapper .count').inner_text() if await page.locator('.like-wrapper .count').count() else "0"
        collects = await page.locator('.collect-wrapper .count').inner_text() if await page.locator('.collect-wrapper .count').count() else "0"

        await context.close()

    return {
        "note_id": str(note_id),
        "title": title.strip(),
        "content": desc.strip(),
        "stats": {
            "likes": likes.strip(),
            "collects": collects.strip(),
        },
    }
