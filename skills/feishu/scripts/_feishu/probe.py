"""Safe browser-side protocol probe.

Only request metadata is retained.  Bodies, cookies, query strings, HAR files,
and rendered message/document content are deliberately never collected.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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
    if scope == "media-observe":
        if not msg_id:
            raise ValueError("media-observe probe 需要 --msg-id")
        return await run_worker("media-observe", {"msg_id": msg_id, "headed": headed})
    return await run_worker("probe", {"scope": scope, "keyword": keyword, "headed": headed})


async def _media_observe_impl(msg_id: str, headed: bool = False) -> dict:
    """Watch the real web client load a message's media resources.

    Opens the chat containing ``msg_id`` in the Messenger page and records
    only request metadata for URLs that carry one of the message's resource
    keys (or feishu-host image/audio/video responses).  The observed host +
    path template is what a later resource adapter may register — nothing
    else.  Bodies, cookies and query values are never collected; resource
    keys are redacted to ``<key:resource_id>`` in every recorded field.
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
            await browser_guard.install(page)

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

            page.on("request", on_request)
            page.on("response", on_response)

            gateway_commands: dict[str, int] = {}
            scroll_phase_requests: list[dict] = []
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

            page.on("request", on_any_request)
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
        "endpoint_templates": templates,
        "candidate_checks": candidate_checks,
        "diag": {**diag, "gateway_commands": dict(sorted(gateway_commands.items()))},
        "note": "只记录了携带资源 key 的请求元数据；路径中的 key 已脱敏。"
                "guard 拦截了页面可能发出的写类 command（如已读回执）。",
        "contains_bodies": False,
        "contains_credentials": False,
    }


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
