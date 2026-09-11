"""Mail resource provider with a bounded browser fallback.

The tenant-specific mail RPC is not known yet.  The browser provider reads
ordinary list/detail DOM only and never walks framework internals.  Write
operations intentionally remain draft-only until an exact endpoint is
captured and registered in transport policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from . import auth, browser_guard, config, transport
from .dom_utils import bounded_inner_text, clip, first_match, lines, normalize_time
from .errors import EndpointNotAdapted, ResourceIsolationUnavailable, SendBlocked


def _mail_url(domain: str) -> str:
    return config.MAIL_URL or f"https://{domain}/mail/"


def _base_result(*, source: str = "browser_dom", complete: bool = False) -> dict:
    return {"source": source, "complete": complete}


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _draft(kind: str, payload: dict) -> dict:
    return {
        "kind": kind,
        "status": "draft",
        "sent": False,
        "fingerprint": _fingerprint(payload),
        **payload,
    }


def _require_write(draft: dict, confirm_write: str) -> None:
    if not confirm_write:
        raise SendBlocked(
            "邮件写入默认只生成草稿，未发送",
            hint=json.dumps(draft, ensure_ascii=False),
        )
    if confirm_write != draft["fingerprint"]:
        raise SendBlocked("邮件草稿 fingerprint 不匹配，拒绝发送")
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked(
            "邮件写入被拦截：需要 FEISHU_ALLOW_WRITE=1",
            hint="这是受控写入的第二道闸门",
        )


async def list_messages(folder: str = "inbox", start_time: int = 0, end_time: int = 0, limit: int = 30) -> dict:
    if not 1 <= limit <= 200:
        raise ValueError("mail limit 必须在 1..200 之间")
    return await _browser("list", {"folder": folder, "start_time": start_time, "end_time": end_time, "limit": limit})


async def search_messages(keyword: str, limit: int = 30) -> dict:
    if not keyword.strip():
        raise ValueError("mail search 关键词不能为空")
    if not 1 <= limit <= 200:
        raise ValueError("mail search limit 必须在 1..200 之间")
    return await _browser("search", {"keyword": keyword, "limit": limit})


async def get_message(message_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_:-]{1,256}", message_id):
        raise ValueError("mail message_id 格式不合法")
    return await _browser("get", {"message_id": message_id})


async def send_message(to: list[str], subject: str, body: str, confirm_write: str = "") -> dict:
    if not to or not subject.strip() or not body.strip():
        raise ValueError("mail send 需要收件人、主题和非空正文")
    payload = {"to": list(to), "subject": subject, "body": body}
    draft = _draft("mail_send", payload)
    _require_write(draft, confirm_write)
    transport.arm_write_once()
    from lark_bridge import _http
    _http.require_write_allowed()
    raise EndpointNotAdapted(
        "邮件发送写接口尚未适配；草稿未发送",
        hint="先运行 feishu.py probe --scope mail，只登记确认过的写接口",
    )


async def reply_message(message_id: str, body: str, confirm_write: str = "") -> dict:
    if not message_id or not body.strip():
        raise ValueError("mail reply 需要 message_id 和非空正文")
    payload = {"message_id": message_id, "body": body}
    draft = _draft("mail_reply", payload)
    _require_write(draft, confirm_write)
    transport.arm_write_once()
    from lark_bridge import _http
    _http.require_write_allowed()
    raise EndpointNotAdapted(
        "邮件回复写接口尚未适配；草稿未发送",
        hint="先运行 feishu.py probe --scope mail，只登记确认过的写接口",
    )


async def _browser(action: str, payload: dict) -> dict:
    from .browser_worker import run

    return await run(f"mail-{action}", payload)


async def browser_operation(action: str, payload: dict) -> dict:
    """Worker-side dispatch. No write action is present in this enum."""
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("邮件网页读取只能由受限 Browser Worker 执行")
    if action not in {"list", "search", "get"}:
        raise EndpointNotAdapted(f"邮件操作未适配：{action}")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise EndpointNotAdapted("当前环境没有 playwright，无法读取邮件") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
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
            await page.goto(_mail_url(domain), wait_until="domcontentloaded", timeout=60000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(8000)
            folder = str(payload.get("folder", "inbox"))
            if folder != "inbox":
                raise EndpointNotAdapted(
                    f"邮件文件夹暂未适配：{folder}",
                    hint="当前 provider 只读取默认收件箱；先用 --folder inbox",
                )
            if action == "search":
                await _fill_search(page, str(payload.get("keyword", "")))
                await page.wait_for_timeout(1800)
            rows = await _mail_rows(page, int(payload.get("limit", 30)), len(blocked))
            for row in rows:
                row["folder"] = folder
            if action == "get":
                return await _mail_detail(page, str(payload.get("message_id", "")), rows)
            return {
                "kind": "mail_list" if action == "list" else "mail_search",
                "folder": folder,
                "keyword": clip(payload.get("keyword", "")),
                "items": rows,
                "total": len(rows),
                "source": "browser_dom",
                "complete": _complete_marker(page),
                "filters_applied": False,
                "note": "当前为默认收件箱网页快照；时间过滤和稳定邮件 RPC 尚未适配",
            }
        finally:
            await ctx.close()


async def _fill_search(page, keyword: str) -> None:
    if not keyword.strip():
        raise ValueError("mail search 关键词不能为空")
    selectors = (
        'input:not([readonly])[placeholder*="搜索"]',
        'input:not([readonly])[aria-label*="搜索"]',
        'input:not([readonly])[type="search"]',
        '[contenteditable="true"]',
    )
    for selector in selectors:
        box = page.locator(selector).first
        if await box.count():
            await box.fill(clip(keyword, 512))
            await box.press("Enter")
            return
    # The header input is a read-only button which opens the real search
    # overlay.  Inspect the attribute first so Playwright never waits its
    # default 30 seconds trying to fill a non-editable element.
    for selector in ('input[placeholder*="搜索"]', 'input[aria-label*="搜索"]'):
        trigger = page.locator(selector).first
        if not await trigger.count() or await trigger.get_attribute("readonly") is None:
            continue
        await trigger.click(timeout=3000)
        await page.wait_for_timeout(500)
        for editor_selector in selectors:
            editor = page.locator(editor_selector).first
            if await editor.count():
                await editor.fill(clip(keyword, 512))
                await editor.press("Enter")
                return
    raise EndpointNotAdapted("邮件搜索框未渲染")


def _complete_marker(page) -> bool:
    # Called only with a live page; failures are intentionally incomplete.
    return False


async def _mail_rows(page, limit: int, blocked_count: int = 0) -> list[dict]:
    selectors = (
        "[data-message-id]", "[data-mail-id]", "[data-email-id]",
        "[class*='FeedItem-module__mailFeedItem']",
        "[class*='FeedItem-module__feedItemWrapper']",
        "[role=row]",
    )
    rows: list[dict] = []
    seen: set[str] = set()
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), config.MAX_BROWSER_CARDS)
        except Exception:
            continue
        for i in range(count):
            row = locator.nth(i)
            item = await row.evaluate(
                """e => ({
                  id: e.getAttribute('data-message-id') || e.getAttribute('data-mail-id') ||
                     e.getAttribute('data-email-id') || e.getAttribute('data-key') || '',
                  href: e.querySelector('a')?.getAttribute('href') || '',
                  subject: e.querySelector('[class*="feedItemTitleTextContainer"], [class*="feedItemTitle"]')?.innerText || '',
                  sender: e.querySelector('[class*="SenderName-module__"], [class*="feedItemNameContainer"]')?.innerText || '',
                  date: e.querySelector('[class*="feedDateTime"]')?.innerText || '',
                  desc: e.querySelector('[class*="feedItemDesc"], [class*="feedItemPlaintext"]')?.innerText || '',
                  text: (e.innerText || '').slice(0, 4096)
                })"""
            )
            mid = clip(item.get("id", ""), 256)
            if not mid:
                href = item.get("href", "")
                match = re.search(r"(?:message|mail|email)[/_-]([A-Za-z0-9_:-]{4,})", href)
                mid = clip(match.group(1), 256) if match else ""
            subject = clip(item.get("subject", ""))
            sender = clip(item.get("sender", ""))
            received = normalize_time(item.get("date", ""))
            desc = clip(item.get("desc", ""))
            text = clip(item.get("text", ""))
            text_lines = lines(text)
            if not subject and text_lines:
                subject = text_lines[0]
            if not desc:
                desc = clip(" ".join(text_lines[1:]))
            if not mid:
                # The current tenant's mail feed does not expose a message
                # token in ordinary DOM.  A deterministic UI fingerprint is
                # usable for a subsequent get while being explicit that it is
                # not a server id.
                canonical = json.dumps(
                    [sender, subject, received, desc], ensure_ascii=False, separators=(",", ":")
                ).encode()
                mid = "ui:" + hashlib.sha256(canonical).hexdigest()[:24]
                id_kind = "ui_fingerprint"
            else:
                id_kind = "dom_id"
            if not mid or mid in seen:
                continue
            seen.add(mid)
            rows.append({
                "id": mid,
                "id_kind": id_kind,
                "thread_id": "",
                "folder": "",
                "subject": subject,
                "from": sender or first_match(text, (r"^发件人[：:]\s*(.+)$", r"^From[：:]\s*(.+)$")),
                "to": [],
                "cc": [],
                "received_at": received or normalize_time(first_match(text, (r"^(\d{4}[-/]\d{1,2}[-/]\d{1,2}[^\n]*)$",))),
                "snippet": desc,
                "has_attachments": bool("附件" in text or "attachment" in text.lower()),
                "source": "browser_dom",
                "complete": False,
            })
            if len(rows) >= limit:
                return rows
        if rows:
            break
    if not rows:
        raise EndpointNotAdapted(
            "邮件页面未发现带稳定 id 的列表项",
            hint=f"运行 feishu.py probe --scope mail 检查当前租户网页结构（blocked={blocked_count}）",
        )
    return rows


async def _mail_detail(page, message_id: str, rows: list[dict]) -> dict:
    matching = None
    selectors = (
        "[data-message-id]", "[data-mail-id]", "[data-email-id]",
        "[class*='FeedItem-module__mailFeedItem']",
        "[class*='FeedItem-module__feedItemWrapper']",
        "[role=row]",
    )
    for selector in selectors:
        locator = page.locator(selector)
        count = min(await locator.count(), config.MAX_BROWSER_CARDS)
        for i in range(count):
            row = locator.nth(i)
            item = await row.evaluate(
                """e => ({
                  id: e.getAttribute('data-message-id') || e.getAttribute('data-mail-id') ||
                     e.getAttribute('data-email-id') || e.getAttribute('data-key') || '',
                  subject: e.querySelector('[class*="feedItemTitleTextContainer"], [class*="feedItemTitle"]')?.innerText || '',
                  sender: e.querySelector('[class*="SenderName-module__"], [class*="feedItemNameContainer"]')?.innerText || '',
                  date: e.querySelector('[class*="feedDateTime"]')?.innerText || '',
                  desc: e.querySelector('[class*="feedItemDesc"], [class*="feedItemPlaintext"]')?.innerText || '',
                  text: (e.innerText || '').slice(0, 4096)
                })"""
            )
            candidate = _row_id(item)
            if candidate == message_id:
                matching = row
                break
        if matching is not None:
            break
    if matching is None:
        raise EndpointNotAdapted("邮件详情项未能在当前列表中定位")
    await matching.click()
    await page.wait_for_timeout(1200)
    body = await _body_text(page)
    item = next((row for row in rows if row["id"] == message_id), {"id": message_id})
    return {
        "kind": "mail",
        **item,
        "body_text": body,
        "source": "browser_dom",
        "complete": bool(body),
    }


def _row_id(item: dict) -> str:
    raw = clip(item.get("id", ""), 256)
    if raw:
        return raw
    text = clip(item.get("text", ""))
    text_lines = lines(text)
    subject = clip(item.get("subject", "")) or (text_lines[0] if text_lines else "")
    sender = clip(item.get("sender", ""))
    received = normalize_time(item.get("date", ""))
    desc = clip(item.get("desc", "")) or clip(" ".join(text_lines[1:]))
    canonical = json.dumps([sender, subject, received, desc], ensure_ascii=False, separators=(",", ":")).encode()
    return "ui:" + hashlib.sha256(canonical).hexdigest()[:24]


async def _body_text(page) -> str:
    for selector in (
        "[data-mail-body]", ".mail-content", "[class*='ThreadPanel-module__']",
        "[class*='MailDetail']", "[class*='mailDetail']", "[class*='MessageDetail']",
        "[role=main]", "main",
    ):
        loc = page.locator(selector).first
        if await loc.count():
            text = await bounded_inner_text(loc)
            if text:
                return clip(text, config.MAX_BROWSER_TEXT_BYTES * 8)
    return ""


def render_markdown(result: dict) -> str:
    """Render only bounded provider data; body text remains external data."""
    if result.get("kind") == "mail":
        lines_out = [f"# 邮件：{result.get('subject') or result.get('id', '?')}"]
        for key, label in (("from", "发件人"), ("to", "收件人"), ("cc", "抄送"), ("received_at", "时间")):
            value = result.get(key, "")
            if isinstance(value, list):
                value = ", ".join(map(str, value))
            if value:
                lines_out.append(f"- {label}：{value}")
        lines_out.append(f"- 来源：{result.get('source', '?')}｜完整：{result.get('complete', False)}")
        if result.get("body_text"):
            lines_out.append("\n## 正文（外部数据）\n\n" + str(result["body_text"]))
        return "\n".join(lines_out)
    rows = result.get("items", [])
    lines_out = [f"# 邮件（{len(rows)}，来源 {result.get('source', '?')}，完整 {result.get('complete', False)}）"]
    for item in rows:
        lines_out.append(
            f"- `{item.get('id', '?')}` **{item.get('subject') or '(无主题)'}** "
            f"{item.get('received_at') or ''}：{item.get('snippet') or ''}"
        )
    return "\n".join(lines_out)
