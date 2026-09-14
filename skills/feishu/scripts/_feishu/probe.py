"""Safe browser-side protocol probe.

Read probes retain only request metadata.  Explicit headed capture may retain
bounded bodies for calendar/mail protocol registration; message-send capture
retains only protobuf field shapes, lengths and digests.  Cookies, query
strings, HAR files, and rendered message/document content are never collected.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
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


def _wire_shape(data: bytes, depth: int = 0) -> list[dict]:
    """Return field numbers/wire widths without exposing protobuf values."""
    if depth > 2:
        return []
    raw = bytes(data or b"")[:4096]
    out: list[dict] = []
    pos = 0
    while pos < len(raw) and len(out) < 64:
        try:
            tag = 0
            shift = 0
            while True:
                if pos >= len(raw):
                    return out
                byte = raw[pos]
                pos += 1
                tag |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
                if shift > 63:
                    return out
            field, wire = tag >> 3, tag & 7
            if not field:
                return out
            item = {"field": field, "wire_type": wire}
            if wire == 0:
                while True:
                    if pos >= len(raw):
                        return out
                    byte = raw[pos]
                    pos += 1
                    if not byte & 0x80:
                        break
            elif wire == 2:
                length = 0
                shift = 0
                while True:
                    if pos >= len(raw):
                        return out
                    byte = raw[pos]
                    pos += 1
                    length |= (byte & 0x7F) << shift
                    if not byte & 0x80:
                        break
                    shift += 7
                    if shift > 63:
                        return out
                end = pos + length
                if end > len(raw):
                    return out
                item["bytes"] = length
                if length > 8 and depth < 2:
                    item["children"] = _wire_shape(raw[pos:end], depth + 1)
                pos = end
            elif wire == 1:
                if pos + 8 > len(raw):
                    return out
                item["bytes"] = 8
                pos += 8
            elif wire == 5:
                if pos + 4 > len(raw):
                    return out
                item["bytes"] = 4
                pos += 4
            else:
                return out
            out.append(item)
        except Exception:
            return out
    return out


def _capture_body(body: bytes, *, redact: bool) -> dict:
    raw = bytes(body or b"")
    if redact:
        return {
            "body_encoding": "protobuf_or_binary",
            "body_bytes": len(raw),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
            "body_truncated": len(raw) > 4096,
            "field_summary": _wire_shape(raw),
        }
    import base64

    sample = raw[:4096]
    try:
        encoded, encoding = sample.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        encoded, encoding = base64.b64encode(sample).decode(), "base64"
    return {
        "body_encoding": encoding,
        "body_truncated": len(raw) > 4096,
        "body": encoded,
    }


async def run(
    scope: str,
    keyword: str = "测试",
    headed: bool = False,
    msg_id: str = "",
    interact: bool = False,
    capture: bool = False,
) -> dict:
    from .browser_worker import run as run_worker

    if capture:
        if scope not in {"calendar", "mail", "message-send"}:
            raise ValueError("--capture 只支持 --scope calendar|mail|message-send")
        if not headed:
            raise ValueError("--capture 必须加 --headed（需要你在浏览器里手动操作）")
        return await run_worker(
            "probe", {"scope": f"{scope}-capture", "keyword": keyword, "headed": True},
            timeout=300,
        )
    if scope == "message-media":
        if not msg_id:
            raise ValueError("message-media probe 需要 --msg-id")
        return await run_worker("message-media-probe", {"msg_id": msg_id})
    if scope == "media-observe":
        if not msg_id:
            raise ValueError("media-observe probe 需要 --msg-id")
        return await run_worker(
            "media-observe",
            {"msg_id": msg_id, "headed": headed, "interact": interact},
            timeout=max(config.BROWSER_TIMEOUT_SECONDS, 240 if interact else 120),
        )
    return await run_worker("probe", {"scope": scope, "keyword": keyword, "headed": headed})


async def _media_observe_impl(msg_id: str, headed: bool = False, interact: bool = False) -> dict:
    """Watch the real web client load a message's media resources.

    Opens the chat containing ``msg_id`` in the Messenger page and records
    only request metadata for URLs that carry one of the message's resource
    keys (or feishu-host image/audio/video responses).  The observed host +
    path template is what a later resource adapter may register — nothing
    else.  Bodies, cookies and query values are never collected; resource
    keys are redacted to ``<key:resource_id>`` in every recorded field.

    With ``interact=True`` the probe additionally CLICKS the file card (files
    never auto-fetch, which is why the file_v3_ route was never observed) and
    captures download events / attachment responses.  With ``headed`` the user
    is asked to click the download button by hand as a last resort.
    """
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("media-observe 只能由受限 Browser Worker 执行")
    from . import message_resources

    await auth.prepare()  # binds cookies to transport before any gateway call
    message = await message_resources.fetch_message(msg_id)
    chat_id = str(message.get("chat_id") or "")
    private = message.get("_resource_keys") or {}
    keys: dict[str, str] = {}
    for item in message.get("resources", []):
        rid = str(item.get("resource_id") or "")
        key = str(private.get(rid) or item.get("_key") or "")
        if rid and key:
            keys[rid] = key
    if not chat_id or not keys:
        return {
            "schema": 1,
            "scope": "media-observe",
            "msg_id": msg_id,
            "observed": [],
            "note": "消息没有可观察的资源 key（可能已删除或无权限）",
            "contains_bodies": False,
            "contains_credentials": False,
        }

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright", hint="先运行 feishu.py login") from exc

    def redact(text: str) -> str:
        for rid, key in keys.items():
            text = text.replace(key, f"<key:{rid}>")
        return text

    def key_fragments() -> list[str]:
        """Matchable URL fragments per key: the web client may strip the
        kind prefix (``img_``/``file_``) or use only the UUID segment."""
        frags: set[str] = set()
        for key in keys.values():
            frags.add(key)
            parts = [p for p in re.split(r"[_/]", str(key)) if len(p) >= 8]
            frags.update(parts)
        return sorted(frags)

    fragments = key_fragments()

    _STATIC_RESOURCE_BASE = "https://internal-api-lark-file.feishu.cn/static-resource/v1/"

    def static_resource_candidates(key: str) -> list[str]:
        """URL candidates from the observed /static-resource/v1/<key>~ route.

        The client was observed fetching sticker/avatar keys there with the
        kind prefix already absent from the path, so try both shapes.  These
        are GET-only probes against a host+route the real client already used.
        """
        variants = [key]
        stripped = re.sub(r"^(?:img|image|file|audio|media|video)_", "", key)
        if stripped != key:
            variants.append(stripped)
        return [f"{_STATIC_RESOURCE_BASE}{variant}~" for variant in variants]

    matched: list[dict] = []
    media_responses: list[dict] = []
    attachment_responses: list[dict] = []
    downloads: list[dict] = []
    ws_frames: list[dict] = []
    diag: dict[str, object] = {}
    domain = auth.load_cookies().get("domain") or config.DOMAIN
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
            await ctx.add_cookies(auth.playwright_cookie_jar())
            blocked_requests = await browser_guard.install(page)

            def on_request(request) -> None:
                if len(matched) >= 100:
                    return
                hit = any(frag in request.url for frag in fragments)
                static = "/static-resource/" in urlsplit(request.url).path
                if hit or static:
                    parsed = urlsplit(request.url)
                    matched.append({
                        "method": request.method,
                        "host": parsed.netloc,
                        "path": redact(parsed.path),
                        "query_keys": tuple(sorted({k for k, _ in parse_qsl(parsed.query)})),
                        "key_match": bool(hit),
                        "status": None,
                        "content_type": "",
                        "response_content_length": None,
                    })

            def on_response(response) -> None:
                parsed = urlsplit(response.url)
                if any(frag in response.url for frag in fragments) or "/static-resource/" in parsed.path:
                    for item in reversed(matched):
                        if item["host"] == parsed.netloc and item["status"] is None:
                            item["status"] = response.status
                            item["content_type"] = response.headers.get("content-type", "")
                            try:
                                item["response_content_length"] = int(
                                    response.headers.get("content-length", "0") or 0) or None
                            except ValueError:
                                item["response_content_length"] = None
                            break
                    return
                ctype = response.headers.get("content-type", "")
                if "feishu" in parsed.netloc and ctype.split("/")[0] in {"image", "audio", "video"}:
                    if len(media_responses) < 100:
                        media_responses.append({
                            "host": parsed.netloc,
                            "path": redact(parsed.path),
                            "status": response.status,
                            "content_type": ctype,
                        })
                    return
                # A real file download surfaces as octet-stream and/or an
                # attachment disposition — record those on ANY host (the CDN
                # may be a non-feishu object store), plus redirects out of
                # feishu hosts (location captured host+path only).
                host = parsed.netloc
                disposition = response.headers.get("content-disposition", "")
                is_attachment = (ctype.startswith("application/octet-stream")
                                 or "attachment" in disposition)
                is_redirect = response.status in (301, 302, 303, 307, 308) and (
                    "feishu" in host or "feishucdn" in host or "lark" in host)
                if is_attachment or is_redirect:
                    if len(attachment_responses) < 100:
                        entry = {
                            "host": host,
                            "path": redact(parsed.path)[:160],
                            "status": response.status,
                            "content_type": ctype,
                            "content_disposition": "attachment" if "attachment" in disposition else "",
                            "query_keys": tuple(sorted({k for k, _ in parse_qsl(parsed.query)})),
                        }
                        if is_redirect:
                            loc = response.headers.get("location", "")
                            if loc:
                                lp = urlsplit(loc)
                                entry["redirect_to"] = f"{lp.netloc}{redact(lp.path)[:120]}"
                        attachment_responses.append(entry)

            def on_download(download) -> None:
                # Metadata only: the download is cancelled before any byte
                # lands on disk.  Filename is message content → extension only.
                if len(downloads) < 20:
                    downloads.append({
                        "url": redact(urlsplit(download.url).netloc + urlsplit(download.url).path)[:200],
                        "suggested_ext": os.path.splitext(download.suggested_filename or "")[1],
                    })
                asyncio.ensure_future(download.cancel())

            page.on("download", on_download)

            import base64 as _b64

            def _on_ws(ws):
                def _sent(payload):
                    if len(ws_frames) >= 40:
                        return
                    raw = payload if isinstance(payload, bytes) else str(payload).encode()
                    # Frames referencing the msg/file by ANY handle: resource
                    # key, msg id, file name, or a download marker.  The mint
                    # call for the one-time download URL turned out not to
                    # carry the raw key (2026-09-10).
                    needles = [f.encode() for f in fragments]
                    needles.append(msg_id.encode())
                    needles += [str(i.get('name') or '').encode() for i in message.get('resources', []) if i.get('name')]
                    needles.append(b'download')
                    if not any(n and n in raw for n in needles):
                        return
                    ws_frames.append({
                        "host": urlsplit(ws.url).netloc,
                        "path": urlsplit(ws.url).path,
                        "body_b64": _b64.b64encode(raw[:2048]).decode(),
                    })
                ws.on("framesent", _sent)

            page.on("websocket", _on_ws)

            page.on("request", on_request)
            page.on("response", on_response)

            gateway_commands: dict[str, int] = {}
            scroll_phase_requests: list[dict] = []
            download_route_requests: list[dict] = []
            in_scroll_phase = False

            def on_any_request(request) -> None:
                parsed = urlsplit(request.url)
                if parsed.path == "/im/gateway/":
                    cmd = request.headers.get("x-command", "?")
                    gateway_commands[cmd] = gateway_commands.get(cmd, 0) + 1
                if in_scroll_phase and "feishu" in parsed.netloc and len(scroll_phase_requests) < 200:
                    scroll_phase_requests.append({
                        "method": request.method,
                        "host": parsed.netloc,
                        "path": redact(parsed.path)[:160],
                        "x_command": request.headers.get("x-command", ""),
                    })
                # Download-ish routes never carry the resource key in the URL
                # (authcode-minted), so watch the route shape directly.
                if ("download" in parsed.path or "drive-stream" in parsed.netloc
                        or "lark-file" in parsed.netloc or "imfile" in parsed.netloc):
                    if len(download_route_requests) < 100:
                        download_route_requests.append({
                            "method": request.method,
                            "host": parsed.netloc,
                            "path": redact(parsed.path)[:160],
                            "query_keys": tuple(sorted({k for k, _ in parse_qsl(parsed.query)})),
                        })

            page.on("request", on_any_request)

            popup_requests: list[dict] = []

            def on_popup_request(request) -> None:
                """Preview popups mint the one-time download URL via their own
                API calls; record all feishu-host popup traffic (bounded)."""
                parsed = urlsplit(request.url)
                if "feishu" not in parsed.netloc and "feishucdn" not in parsed.netloc:
                    return
                if len(popup_requests) >= 150:
                    return
                popup_requests.append({
                    "method": request.method,
                    "host": parsed.netloc,
                    "path": redact(parsed.path)[:160],
                    "query_keys": tuple(sorted({k for k, _ in parse_qsl(parsed.query)})),
                    "x_command": request.headers.get("x-command", ""),
                })

            def attach_popup_watchers(popup) -> None:
                popup.on("request", on_request)
                popup.on("response", on_response)
                popup.on("request", on_any_request)
                popup.on("request", on_popup_request)
                popup.on("download", on_download)
                popup.on("websocket", _on_ws)

            ctx.on("page", attach_popup_watchers)
            await page.goto(f"https://{domain}/next/messenger/",
                            wait_until="domcontentloaded", timeout=60000)

            async def wait_keys(ms: int) -> bool:
                # Poll instead of a fixed sleep so we stop as soon as the
                # client has fetched a keyed resource.
                for _ in range(max(1, ms // 500)):
                    if matched:
                        return True
                    await page.wait_for_timeout(500)
                return bool(matched)

            # Path 1: deep link.  The web client honors ?chatId= on load.
            found_chat = False
            await page.goto(f"https://{domain}/next/messenger/?chatId={chat_id}",
                            wait_until="domcontentloaded", timeout=60000)
            if await wait_keys(6000):
                found_chat = True
            else:
                # Heuristic: the conversation header rendered for our chat.
                found_chat = await page.locator(f'[data-feed-id="{chat_id}"]').count() > 0 \
                    and chat_id in page.url
            diag["deep_link_url"] = urlsplit(page.url).path + (
                "?" + urlsplit(page.url).query[:60] if urlsplit(page.url).query else "")
            diag["deep_link_opened"] = found_chat
            # Path 2: click the feed card.  The sidebar is a virtualized list:
            # hover the actual card element's center (not a guessed
            # coordinate) and wheel until the card count stops growing.
            if not found_chat:
                first = page.locator("[data-feed-id]").first
                if await first.count():
                    box = await first.bounding_box()
                    if box:
                        await page.mouse.move(box["x"] + box["width"] / 2,
                                              box["y"] + box["height"] / 2)
                stable = 0
                last_count = 0
                for _ in range(30):
                    card = page.locator(f'[data-feed-id="{chat_id}"]')
                    if await card.count():
                        await card.first.click()
                        found_chat = True
                        break
                    await page.mouse.wheel(0, 1400)
                    await page.wait_for_timeout(900)
                    count = await page.locator("[data-feed-id]").count()
                    stable = stable + 1 if count == last_count else 0
                    last_count = count
                    if stable >= 3:
                        break  # list exhausted without our chat
                diag["feed_cards_seen"] = last_count
                diag["feed_click_opened"] = found_chat
                if not found_chat:
                    diag["feed_ids_sample"] = [
                        await page.locator("[data-feed-id]").nth(i).get_attribute("data-feed-id") or ""
                        for i in range(min(5, last_count))
                    ]
            # Path 3 (fallback when the chat is open but its old media never
            # entered the viewport): messenger search for the message's own
            # (unique) text, then click the message result — jumps straight
            # to the message and makes the client fetch its media.
            if not matched:
                snippet = str(message.get("text") or "").strip()[:80]
                diag["search_snippet_len"] = len(snippet)
                if len(snippet) >= 8:
                    search = page.get_by_text("搜索", exact=True).first
                    diag["search_button_found"] = bool(await search.count())
                    if await search.count():
                        await search.click()
                        await page.wait_for_timeout(1000)
                    box_used = ""
                    for selector in ('[contenteditable=true]', 'input[placeholder*="搜索"]',
                                     'input[aria-label*="搜索"]', 'input'):
                        box = page.locator(selector).first
                        if not await box.count():
                            continue
                        try:
                            await box.fill(snippet)
                            await box.press("Enter")
                            box_used = selector
                            break
                        except Exception:
                            continue
                    diag["search_box_used"] = box_used
                    await page.wait_for_timeout(4000)
                    diag["search_result_url"] = urlsplit(page.url).path
                    # Structural snapshot of the search panel: class names and
                    # counts only, never result text.
                    diag["search_panel_shape"] = await page.evaluate(
                        """() => {
                          const els = Array.from(document.querySelectorAll('[class*="search"]')).slice(0, 60);
                          return els.map(e => ({
                            tag: e.tagName.toLowerCase(),
                            cls: Array.from(e.classList).filter(c => /^[a-zA-Z][a-zA-Z0-9_-]{1,60}$/.test(c)).slice(0, 4),
                            children: e.children.length,
                          }));
                        }"""
                    )
                    # Message results tab/section, then the first result card.
                    for label in ("消息", "Messages"):
                        tab = page.get_by_text(label, exact=True).last
                        if await tab.count():
                            try:
                                await tab.click()
                                await page.wait_for_timeout(2000)
                                break
                            except Exception:
                                pass
                    hit = page.get_by_text(snippet[:20], exact=False).first
                    diag["search_hit_found"] = bool(await hit.count())
                    if await hit.count():
                        try:
                            await hit.click()
                            found_chat = True
                        except Exception:
                            pass
                    await wait_keys(6000)
                diag["search_opened"] = found_chat
            if found_chat:
                # Let the conversation render, then scroll the message list
                # upwards so the target message (and its media) enters the
                # viewport and the client fetches it.
                await page.wait_for_timeout(3000)
                scrollables = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('div'))
                        .filter(e => e.scrollHeight > e.clientHeight + 100 && e.clientHeight > 300)
                        .map(e => { const r = e.getBoundingClientRect();
                            return {x: r.x|0, y: r.y|0, w: r.width|0, h: r.height|0,
                                    scrollHeight: e.scrollHeight, clientHeight: e.clientHeight}; })
                        .filter(b => b.w > 200)
                        .sort((a, b) => b.x - a.x)
                        .slice(0, 5)"""
                )
                diag["scrollable_regions"] = scrollables
                target_region = scrollables[0] if scrollables else None
                if target_region:
                    await page.mouse.move(
                        target_region["x"] + target_region["w"] / 2,
                        target_region["y"] + target_region["h"] / 2)
                in_scroll_phase = True
                scroll_metrics = []
                for round_no in range(15):
                    if await wait_keys(1500):
                        break
                    await page.mouse.wheel(0, -1600)
                    # mouse.wheel can miss virtualized containers; drive the
                    # largest scrollable directly as a fallback and record
                    # whether it actually moved (metrics only, no content).
                    moved = await page.evaluate(
                        """() => {
                          const els = Array.from(document.querySelectorAll('div'))
                            .filter(e => e.scrollHeight > e.clientHeight + 100 && e.clientHeight > 300)
                            .map(e => { const r = e.getBoundingClientRect(); return {e, x: r.x, w: r.width}; })
                            .filter(b => b.w > 200).sort((a, b) => b.x - a.x);
                          if (!els.length) return null;
                          const el = els[0].e;
                          const before = el.scrollTop;
                          el.scrollTop = Math.max(0, el.scrollTop - 1500);
                          el.dispatchEvent(new Event('scroll', {bubbles: false}));
                          return {before, after: el.scrollTop, scrollHeight: el.scrollHeight};
                        }"""
                    )
                    if moved:
                        scroll_metrics.append(moved)
                in_scroll_phase = False
                diag["scroll_rounds"] = round_no + 1
                diag["scroll_metrics"] = scroll_metrics[-4:]
                diag["post_click_url"] = urlsplit(page.url).path
                diag["scroll_phase_requests"] = scroll_phase_requests[:80]
                await wait_keys(3000)

            # Interact phase: files never auto-fetch, so click the file card
            # (and its download affordance) to make the client issue the real
            # download request.  Headed mode offers a manual window instead.
            if interact and found_chat:
                in_scroll_phase = True  # record interact-phase traffic too
                file_names = [
                    str(item.get("name") or "")
                    for item in message.get("resources", [])
                    if item.get("kind") in ("file", "cloud_file") and item.get("name")
                ]
                diag["interact_file_names"] = len(file_names)
                for name in file_names[:2]:
                    card = page.get_by_text(name[:40], exact=False).first
                    if not await card.count():
                        continue
                    try:
                        await card.hover(timeout=3000)
                        await page.wait_for_timeout(800)
                        # Prefer an explicit download button/icon revealed by
                        # the hover; otherwise click the card body (preview).
                        clicked = ""
                        for sel in ('[class*=download]', '[aria-label*="下载"]',
                                    '[data-tip*="下载"]'):
                            btn = page.locator(sel).first
                            if await btn.count() and await btn.is_visible():
                                await btn.click(timeout=2500)
                                clicked = sel
                                break
                        if not clicked:
                            await card.click(timeout=2500)
                            clicked = "card"
                        diag.setdefault("interact_clicks", []).append(clicked)
                        await page.wait_for_timeout(1500)
                        # The click may open a hover menu or a preview tab —
                        # both carry a further 下载 action.
                        menu = page.get_by_text("下载", exact=True).last
                        if await menu.count() and await menu.is_visible():
                            await menu.click(timeout=2500)
                            diag.setdefault("interact_clicks", []).append("menu:下载")
                            await page.wait_for_timeout(1500)
                        # A preview popup page has its own download button.
                        for popup in ctx.pages:
                            if popup is page:
                                continue
                            for sel in ('[class*=download]', '[aria-label*="下载"]',
                                        '[data-tip*="下载"]'):
                                btn = popup.locator(sel).first
                                try:
                                    if await btn.count() and await btn.is_visible():
                                        await btn.click(timeout=2500)
                                        diag.setdefault("interact_clicks", []).append("popup" + sel)
                                        break
                                except Exception:
                                    continue
                        await page.wait_for_timeout(4000)
                    except Exception as exc:
                        diag.setdefault("interact_clicks", []).append(f"fail:{type(exc).__name__}")
                    if downloads or attachment_responses:
                        break
                if headed and not downloads and not attachment_responses:
                    print("请在打开的浏览器里手动点击该文件消息的下载按钮（120 秒窗口）",
                          file=sys.stderr)
                    for _ in range(120):
                        if downloads or attachment_responses:
                            break
                        await page.wait_for_timeout(1000)
                diag["interact_downloads"] = len(downloads)
                diag["interact_attachments"] = len(attachment_responses)
                in_scroll_phase = False
                diag["interact_phase_requests"] = scroll_phase_requests[-80:]

            # Candidate verification: when the client never fetched the key
            # (old message in a virtualized list), verify the observed
            # /static-resource/v1/<key>~ route directly with the message's
            # own keys.  GET-only, headers-only (no body is read), at most
            # two candidate shapes per resource.
            candidate_checks: list[dict] = []
            if not any(item.get("key_match") for item in matched):
                for rid, key in list(keys.items())[:4]:
                    for candidate in static_resource_candidates(key):
                        entry: dict = {"resource_id": rid, "url_template": redact(candidate)}
                        try:
                            resp = await ctx.request.get(candidate, timeout=15000)
                            entry["status"] = resp.status
                            entry["content_type"] = resp.headers.get("content-type", "")
                            entry["content_length"] = resp.headers.get("content-length", "")
                        except Exception as exc:
                            entry["error"] = type(exc).__name__
                        candidate_checks.append(entry)
                        if entry.get("status") == 200:
                            break  # route shape verified for this resource
        finally:
            await ctx.close()

    templates = sorted({
        f"{item['method']} {item['host']}{item['path']}" for item in matched
    })
    return {
        "schema": 1,
        "scope": "media-observe",
        "msg_id": msg_id,
        "chat_id": chat_id,
        "chat_opened": found_chat,
        "resource_ids": sorted(keys),
        "observed": matched,
        "media_responses": media_responses[:50],
        "attachment_responses": attachment_responses[:50],
        "downloads": downloads[:20],
        "ws_frames_sent": ws_frames[:40],
        "endpoint_templates": templates,
        "candidate_checks": candidate_checks,
        "diag": {**diag, "gateway_commands": dict(sorted(gateway_commands.items())),
                 "blocked_requests": blocked_requests[:50],
                 "download_route_requests": download_route_requests[:50],
                 "popup_requests": popup_requests[:80]},
        "note": "只记录了携带资源 key 的请求元数据；路径中的 key 已脱敏。"
                "guard 拦截了页面可能发出的写类 command（如已读回执）。",
        "contains_bodies": bool(ws_frames),
        "contains_credentials": False,
    }


async def _cdp_capture(profile: Path, records: dict, stop: asyncio.Event,
                       *, redact_body: bool = False) -> None:
    """Browser-level CDP network sniffer across ALL targets (page + workers).

    Page-level Playwright hooks miss worker-internal connections — 2026-09-11:
    the calendar SPA listed and created events with ZERO page-visible traffic
    (no POSTs, no meaningful page WS frames, no service workers).  With CDP
    flatten auto-attach every target (page/iframe/dedicated+shared worker)
    gets Network.enable, so worker-held fetches and WebSockets surface here.
    Bounded like the rest of capture mode: metadata + ≤4 KiB bodies only.
    """
    import websockets  # already a venv dep (WS daemon uses it)

    port_file = profile / "DevToolsActivePort"
    for _ in range(150):
        if stop.is_set() or port_file.exists():
            break
        await asyncio.sleep(0.2)
    if not port_file.exists():
        records["cdp_error"] = "DevToolsActivePort 未出现"
        return
    lines = port_file.read_text().splitlines()
    ws_url = f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
    next_id = 0
    sessions: set[str] = set()
    targets: dict[str, str] = {}  # sessionId -> target type

    async def send(ws, method: str, params: dict | None = None, session_id: str = "") -> None:
        nonlocal next_id
        next_id += 1
        msg: dict = {"id": next_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await ws.send(json.dumps(msg))

    try:
        async with websockets.connect(ws_url, max_size=2**22) as ws:
            await send(ws, "Target.setDiscoverTargets", {"discover": True})
            await send(ws, "Target.setAutoAttach",
                       {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                method = m.get("method", "")
                params = m.get("params") or {}
                if method == "Target.attachedToTarget":
                    t = params.get("targetInfo") or {}
                    # This Chromium delivers sessionId inside params even in
                    # flatten mode (verified 2026-09-11); commands to the
                    # child must still carry it top-level.
                    sid = str(params.get("sessionId") or m.get("sessionId") or "")
                    ttype = str(t.get("type") or "")
                    if len(records["targets"]) < 50:
                        records["targets"].append(
                            {"type": ttype, "url": str(t.get("url") or "")[:120]})
                    if sid and sid not in sessions:
                        sessions.add(sid)
                        targets[sid] = ttype
                        await send(ws, "Network.enable", session_id=sid)
                    continue
                sid = str(m.get("sessionId") or params.get("sessionId") or "")
                if method == "Network.requestWillBeSent":
                    req = params.get("request") or {}
                    url = str(req.get("url") or "")
                    parsed = urlsplit(url)
                    if "feishu" not in parsed.netloc:
                        continue
                    rmethod = str(req.get("method") or "")
                    if len(records["requests"]) < 300:
                        records["requests"].append({
                            "method": rmethod, "host": parsed.netloc,
                            "path": parsed.path, "target": targets.get(sid, "?"),
                        })
                    if rmethod.upper() in {"GET", "HEAD", "OPTIONS"}:
                        continue
                    post_data = req.get("postData") or ""
                    raw_body = post_data.encode("utf-8", errors="replace")
                    if len(records["writes"]) < 60:
                        item = {
                            "method": rmethod, "host": parsed.netloc,
                            "path": parsed.path, "target": targets.get(sid, "?"),
                        }
                        item.update(_capture_body(raw_body, redact=redact_body))
                        records["writes"].append(item)
                elif method == "Network.webSocketCreated":
                    parsed = urlsplit(str(params.get("url") or ""))
                    if len(records["ws_sockets"]) < 30:
                        records["ws_sockets"].append({
                            "host": parsed.netloc, "path": parsed.path,
                            "target": targets.get(sid, "?"),
                        })
                elif method in ("Network.webSocketFrameSent",):
                    resp = params.get("response") or {}
                    payload = str(resp.get("payloadData") or "")
                    raw_b = payload.encode("utf-8", errors="replace")
                    if len(records["ws_frames"]) < 200:
                        item = {
                            "target": targets.get(sid, "?"),
                            "opcode": resp.get("opcode"),
                        }
                        if redact_body:
                            item.update({
                                "encoding": "protobuf_or_binary",
                                "bytes": len(raw_b),
                                "sha256": hashlib.sha256(raw_b).hexdigest(),
                                "truncated": len(raw_b) > 4096,
                                "field_summary": _wire_shape(raw_b),
                            })
                        else:
                            item.update({
                                "encoding": "utf-8",
                                "truncated": len(raw_b) > 4096,
                                "body": payload[:4096],
                            })
                        records["ws_frames"].append(item)
    except Exception as exc:
        records["cdp_error"] = f"{type(exc).__name__}: {exc}"


async def _capture_impl(app: str) -> dict:
    """Headed write-endpoint capture: watch while the USER performs one write.

    Policy relaxation, deliberate and bounded: calendar/mail capture records
    non-GET request bodies (≤4 KiB each, base64 when binary) because their
    payload shape is exactly what we need to register.  message-send capture
    records only field shapes, lengths and SHA-256 digests.  Capture files must
    be written with --out and deleted once the endpoint is registered.
    """
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("capture 只能由受限 Browser Worker 执行")
    auth.load_cookies()
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright", hint="先运行 feishu.py login") from exc

    if app == "calendar":
        from .calendar_ops import _calendar_url as app_url
    elif app == "mail":
        from .mail import _mail_url as app_url
    else:
        app_url = lambda tenant: f"https://{tenant}/next/messenger/"
    domain = auth.load_cookies().get("domain") or config.DOMAIN

    requests_meta: list[dict] = []
    captured_writes: list[dict] = []
    ws_frames: list[dict] = []
    # Page-load noise that must never count as "the user did the write".
    _NOISE_HOSTS = ("mcs-bd.feishu.cn", "slardar-bd.feishu.cn",
                    "internal-api-security.feishu.cn", "accounts.feishu.cn")
    # Header allowlist for write capture (credentials NEVER recorded) —
    # 2026-09-11: replaying the exact captured cmd-1002003 body got a
    # payloadType-5 rejection, so the differentiator must be in these headers.
    _HEADER_ALLOWLIST = ("x-command", "x-command-version", "x-web-version",
                         "x-appid", "x-source", "x-lgw-os-type",
                         "x-lgw-req-sdk-type", "x-lgw-terminal-type",
                         "x-lsc-bizid", "x-lsc-version", "x-lsc-terminal",
                         "content-type", "locale", "x-tt-env",
                         "x-request-id", "x-b3-traceid")
    first_interesting_at = 0.0

    def is_noise(method: str, host: str, path: str, x_command: str) -> bool:
        if host in _NOISE_HOSTS:
            return True
        if path == "/im/gateway/":
            try:
                return int(x_command or "0") in browser_guard.READ_GATEWAY_COMMANDS
            except ValueError:
                return False
        return False

    async with async_playwright() as pw:
        # The debug port is loopback-only and lives only for this headed
        # capture window; it exists so the CDP sniffer can see worker-held
        # connections that page hooks structurally miss.
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=False,
            ignore_https_errors=False,
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=list(config.BROWSER_ARGS) + ["--remote-debugging-port=0"],
        )
        cdp_records: dict = {"targets": [], "requests": [], "writes": [],
                             "ws_sockets": [], "ws_frames": []}
        cdp_stop = asyncio.Event()
        cdp_task = asyncio.create_task(
            _cdp_capture(
                config.PROFILE,
                cdp_records,
                cdp_stop,
                redact_body=app == "message-send",
            )
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await ctx.add_cookies(auth.playwright_cookie_jar())
            would_block = await browser_guard.install(page, observe=True)

            def on_request(request) -> None:
                nonlocal first_interesting_at
                parsed = urlsplit(request.url)
                if "feishu" not in parsed.netloc:
                    return
                entry = {
                    "method": request.method,
                    "host": parsed.netloc,
                    "path": parsed.path,
                    "x_command": request.headers.get("x-command", ""),
                    "query_keys": tuple(sorted({k for k, _ in parse_qsl(parsed.query)})),
                }
                if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                    entry["headers"] = {
                        k: v for k, v in request.headers.items()
                        if k.lower() in _HEADER_ALLOWLIST
                    }
                    # 名字全量（值按白名单）——2026-09-11：日历头指纹修正后仍被
                    # 拒，需要确认有没有白名单外的签名类头（x-gorgon 等）。
                    entry["header_names"] = sorted(k.lower() for k in request.headers)
                if len(requests_meta) < 300:
                    requests_meta.append(entry)
                if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
                    return
                body = request.post_data_buffer or b""
                if not body:
                    return
                noise = is_noise(request.method, parsed.netloc, parsed.path,
                                 request.headers.get("x-command", ""))
                # Page-load telemetry must not crowd out the one write we are
                # hunting (2026-09-11: a 40-entry cap filled with boot noise
                # before the user ever clicked 保存).
                if noise or len(captured_writes) >= 200:
                    return
                captured_writes.append({
                    **entry,
                    "noise": False,
                    **_capture_body(bytes(body), redact=app == "message-send"),
                })
                if not noise and not first_interesting_at:
                    first_interesting_at = time.monotonic()  # diagnostic only

            page.on("request", on_request)

            # 2026-09-11: 重放抓到的创建包被网关回 payloadType=5 空响应，
            # 需要对照真实客户端成功时的响应形状。只记非 GET 网关调用的
            # 状态码 + ≤4KiB 截断响应体，与请求体同策略（--out、用后删除）。
            captured_responses: list[dict] = []

            def on_response(response) -> None:
                try:
                    req = response.request
                    parsed = urlsplit(req.url)
                    if "feishu" not in parsed.netloc:
                        return
                    if req.method.upper() in {"GET", "HEAD", "OPTIONS"}:
                        return
                    if parsed.path != "/im/gateway/":
                        return
                    xc = req.headers.get("x-command", "")
                    if is_noise(req.method, parsed.netloc, parsed.path, xc):
                        return
                    if len(captured_responses) >= 60:
                        return

                    async def read_body(resp=response, xcmd=xc):
                        try:
                            body = await resp.body()
                        except Exception:
                            body = b""
                        item = {
                            "x_command": xcmd,
                            "status": resp.status,
                        }
                        item.update(_capture_body(body, redact=app == "message-send"))
                        captured_responses.append(item)

                    asyncio.get_event_loop().create_task(read_body())
                except Exception:
                    pass

            page.on("response", on_response)

            def on_websocket(ws) -> None:
                # Calendar/docs sync mutations over WS frames — HTTP request
                # hooks never see them (learned the hard way 2026-09-10:
                # a manually created event produced zero captured POSTs).
                def on_sent(payload) -> None:
                    if len(ws_frames) >= 40:
                        return
                    raw = payload if isinstance(payload, bytes) else str(payload).encode()
                    item = {
                        "url_host": urlsplit(ws.url).netloc,
                        "url_path": urlsplit(ws.url).path,
                        "direction": "sent",
                    }
                    if app == "message-send":
                        item.update({
                            "encoding": "protobuf_or_binary",
                            "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "truncated": len(raw) > 4096,
                            "field_summary": _wire_shape(raw),
                        })
                    else:
                        captured = _capture_body(raw, redact=False)
                        item.update({
                            "encoding": captured.get("body_encoding", "utf-8"),
                            "truncated": captured.get("body_truncated", False),
                            "body": captured.get("body", ""),
                        })
                    ws_frames.append(item)

                ws.on("framesent", on_sent)

            page.on("websocket", on_websocket)
            await page.goto(app_url(domain), wait_until="domcontentloaded", timeout=60000)
            await auth.raise_if_login_redirect(page)
            # Fixed 240s window — no early-exit heuristics (page-load traffic
            # made every "first write" check exit before the user ever acted).
            # The user may also just close the window to finish early.
            closed_early = False
            window_s = float(os.environ.get("FEISHU_CAPTURE_WINDOW", 240))
            deadline = time.monotonic() + window_s
            while time.monotonic() < deadline:
                try:
                    await page.wait_for_timeout(1000)
                except Exception:
                    closed_early = True
                    break
            try:
                await asyncio.wait_for(asyncio.sleep(2), timeout=3)  # 收尾 response 任务
            except Exception:
                pass
        finally:
            cdp_stop.set()
            try:
                await asyncio.wait_for(cdp_task, timeout=5)
            except Exception:
                cdp_task.cancel()
            try:
                # The user closing the whole browser counts as a valid early
                # finish — never let it destroy the captured data.
                await ctx.close()
            except Exception:
                pass
    return {
        "schema": 2,
        "scope": f"{app}-capture",
        "requests": requests_meta,
        "captured_writes": captured_writes,
        "captured_responses": captured_responses,
        "ws_frames_sent": ws_frames,
        "cdp_targets": cdp_records["targets"],
        "cdp_requests": cdp_records["requests"],
        "cdp_writes": cdp_records["writes"],
        "cdp_ws_sockets": cdp_records["ws_sockets"],
        "cdp_ws_frames": cdp_records["ws_frames"],
        "cdp_error": cdp_records.get("cdp_error", ""),
        "would_block": would_block,
        "pages": [],
        "closed_early": closed_early,
        "contains_bodies": bool(app != "message-send" and (
            captured_writes or ws_frames or cdp_records["writes"]
            or cdp_records["ws_frames"]
        )),
        "contains_credentials": False,
        "body_redacted": app == "message-send",
        "note": "写接口抓包结果。登记进 transport 白名单后请删除此文件。",
    }


async def _run_impl(scope: str, keyword: str = "测试", headed: bool = False) -> dict:
    if scope.endswith("-capture"):
        return await _capture_impl(scope[: -len("-capture")])
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("网页 probe 只能由受限 Browser Worker 执行")
    if scope not in {"chats", "docs-search", "mail", "calendar", "message-send", "all"}:
        raise ValueError("probe scope 必须是 chats、docs-search、mail、calendar、message-send 或 all")
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
            await ctx.add_cookies(auth.playwright_cookie_jar())
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

            if scope in {"chats", "message-send", "all"}:
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
