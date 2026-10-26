"""Read-only message resource resolution and bounded streaming downloads."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

from lark_bridge._urls import DRIVE_STREAM

from . import auth, config, gateway, media_store, transport
from .errors import EndpointNotAdapted, FeishuError, ResourceError, ResourceIsolationUnavailable
from .message_model import public_message, public_resource


_TOKEN_RE = re.compile(r"^[^\s\x00]{1,4096}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
_MAX_OUT_NAME = 255
_KNOWN_DOWNLOAD_KINDS = frozenset({"file", "cloud_file", "document"})
# Kinds served by the tenant's static-resource route (observed from the real
# web client; verified 200 + image/jpeg with a live img_v3 key on 2026-09-07).
_STATIC_RESOURCE_KINDS = frozenset({"image", "audio", "media", "attachment"})
_STATIC_RESOURCE_HOST = "https://internal-api-lark-file.feishu.cn"
_DIRECT_HOSTS = frozenset({urlsplit(DRIVE_STREAM).netloc, config.DOMAIN, "www.feishu.cn"})


def _resource_key(message: dict, resource: dict) -> str:
    rid = str(resource.get("resource_id") or "")
    private = message.get("_resource_keys") or {}
    return str(private.get(rid) or resource.get("_key") or "")


def _resource_data(message: dict, resource: dict) -> bytes:
    rid = str(resource.get("resource_id") or "")
    private = message.get("_resource_data") or {}
    value = private.get(rid) or resource.get("_data") or b""
    return value if isinstance(value, bytes) else b""


def _resource_crypto(message: dict, resource: dict) -> dict:
    """In-process AES-256-GCM material for tenant-encrypted media blobs."""
    rid = str(resource.get("resource_id") or "")
    private = message.get("_resource_crypto") or {}
    value = private.get(rid) or resource.get("_crypto") or {}
    if not isinstance(value, dict):
        return {}
    if value.get("algo") != "aes-256-gcm":
        return {}
    key_hex, nonce_hex = str(value.get("key") or ""), str(value.get("nonce") or "")
    if len(key_hex) != 64 or len(nonce_hex) != 24:
        return {}
    return {"key": key_hex, "nonce": nonce_hex}


def _decrypt_in_place(path: Path, crypto: dict) -> None:
    """AES-256-GCM decrypt a downloaded media blob (tag = last 16 bytes)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from .output import register_secret

    register_secret(crypto["key"])
    register_secret(crypto["nonce"])
    blob = path.read_bytes()
    if len(blob) <= 16:
        raise ResourceError("加密媒体数据过短，无法校验完整性")
    plain = AESGCM(bytes.fromhex(crypto["key"])).decrypt(
        bytes.fromhex(crypto["nonce"]), blob, None
    )
    path.write_bytes(plain)
    os.chmod(path, 0o600)


async def fetch_message(msg_id: str) -> dict:
    if not _MESSAGE_ID_RE.fullmatch(str(msg_id or "")):
        raise ResourceError("消息 ID 格式不合法")
    messages = await gateway.fetch_messages([msg_id])
    if not messages:
        raise ResourceError(f"取不到消息 {msg_id}（id 不对，或无权限，或已删除）")
    return messages[0]


async def list_resources(msg_id: str) -> dict:
    message = await fetch_message(msg_id)
    return {
        "message": public_message(message),
        "resources": [public_resource(item) for item in message.get("resources", [])],
    }


async def probe_metadata(msg_id: str) -> dict:
    """Return resource/adapter metadata without downloading bodies or media."""
    message = await fetch_message(msg_id)
    adapters = []
    for resource in message.get("resources", []):
        item = public_resource(resource)
        try:
            if _resource_data(message, resource):
                adapter = "inline"
            else:
                keyed = {**resource, "_key": _resource_key(message, resource)}
                if _is_browser_only_file(keyed):
                    item["adapter"] = "browser_download"
                    item["endpoint_status"] = "browser_only"
                    adapters.append(item)
                    continue
                _url_for(keyed)
                adapter = "drive_stream" if resource.get("kind") in _KNOWN_DOWNLOAD_KINDS else "direct_url"
            item["adapter"] = adapter
            item["endpoint_status"] = "registered"
        except EndpointNotAdapted:
            item["adapter"] = None
            item["endpoint_status"] = "not_adapted"
        except ResourceError:
            item["adapter"] = None
            item["endpoint_status"] = "invalid_reference"
        adapters.append(item)
    return {
        "schema": 1,
        "scope": "message-media",
        "message_id": msg_id,
        "message_type": message.get("type"),
        "resources": adapters,
        "contains_bodies": False,
        "contains_credentials": False,
    }


def select_resource(message: dict, resource_id_value: str = "") -> dict:
    resources = list(message.get("resources", []))
    if not resources:
        raise ResourceError("这条消息没有可处理的资源")
    if resource_id_value:
        for resource in resources:
            if resource.get("resource_id") == resource_id_value:
                selected = dict(resource)
                selected["_key"] = _resource_key(message, resource)
                selected["_data"] = _resource_data(message, resource)
                selected["_crypto"] = _resource_crypto(message, resource)
                if not selected["_key"] and not selected["_data"]:
                    raise EndpointNotAdapted("消息资源只有元数据，尚未取得可下载的资源 key")
                return selected
        raise ResourceError("resource_id 不属于这条消息")
    if len(resources) != 1:
        raise ResourceError("这条消息包含多个资源，请指定 --resource-id")
    selected = dict(resources[0])
    selected["_key"] = _resource_key(message, resources[0])
    selected["_data"] = _resource_data(message, resources[0])
    selected["_crypto"] = _resource_crypto(message, resources[0])
    if not selected["_key"] and not selected["_data"]:
        raise EndpointNotAdapted("消息资源只有元数据，尚未取得可下载的资源 key")
    return selected


def _is_browser_only_file(resource: dict) -> bool:
    """Chat file messages (file_v3_ keys): no registered HTTP route works."""
    key = str(resource.get("_key") or "")
    return resource.get("kind") == "file" and key.startswith("file_v3_")


def _iter_file_chunks(path: Path, size: int = 1024 * 1024):
    with path.open("rb") as f:
        while True:
            chunk = f.read(size)
            if not chunk:
                return
            yield chunk


async def _download_file_via_browser(message: dict, resource: dict) -> dict:
    from .browser_worker import run

    return await run(
        "media-file-download",
        {
            "msg_id": str(message.get("msg_id") or ""),
            "chat_id": str(message.get("chat_id") or ""),
            "resource_id": str(resource.get("resource_id") or ""),
            "file_name": str(resource.get("name") or ""),
        },
        timeout=max(config.BROWSER_TIMEOUT_SECONDS, 180),
    )


def _url_for(resource: dict) -> str:
    kind = str(resource.get("kind") or "")
    key = str(resource.get("_key") or "")
    if not _TOKEN_RE.fullmatch(key):
        raise ResourceError("消息资源 key 格式不合法")

    # Legacy type=2 rich-text posts expose a rendered public CDN URL instead
    # of an img_v3_ key. Validate the exact host/path before the generic image
    # key route so the URL is not embedded into /static-resource/v1/.
    if resource.get("source") == "post.img":
        raw_url = key
        # 即使消息源 HTML 给出明文 http，也强制升级为 https，绝不走明文链路下载
        if raw_url.startswith("http://"):
            raw_url = "https://" + raw_url[len("http://"):]
        if kind == "image" and transport.is_legacy_post_image_url(raw_url):
            return raw_url
        raise EndpointNotAdapted(
            "富文本图片地址不是受支持的 legacy post.img CDN 路由",
            hint="只允许 p数字.pstatp.com/origin/<token> 且不带 query 的地址（自动升级至 https）",
        )

    # This is the already-verified Drive stream route used for file/cloud-file
    # tokens.  Image/audio/media routes are intentionally not guessed: once a
    # tenant-specific route is observed by probe it must be added explicitly.
    if kind in _KNOWN_DOWNLOAD_KINDS:
        if _is_browser_only_file(resource):
            raise EndpointNotAdapted(
                "聊天文件（file_v3_）没有可用的静态 HTTP 路由，走浏览器下载",
                hint="media --action download 会自动用受限浏览器点击真实下载按钮",
            )
        return f"{DRIVE_STREAM}/space/api/box/stream/download/all/{quote(key, safe='')}/"

    # Message media keys (img_v3_…/audio_v3_…/media_v3_…).  The trailing "~"
    # is the exact observed form; query params are only used for stickers.
    if kind in _STATIC_RESOURCE_KINDS:
        return f"{_STATIC_RESOURCE_HOST}/static-resource/v1/{quote(key, safe='')}~"

    # A post may contain an absolute, already-rendered image URL.  Permit it
    # only for the small host set already covered by transport policy; signed
    # or third-party URLs remain unavailable rather than becoming an SSRF
    # primitive.
    parsed = urlsplit(key)
    if parsed.scheme == "https" and parsed.netloc in _DIRECT_HOSTS and not parsed.query:
        return key

    raise EndpointNotAdapted(
        f"{kind or 'unknown'} 消息资源下载接口尚未登记",
        hint="先通过受控 probe 观察真实 host/path，再加入资源 provider 白名单",
    )


def _safe_output(path_text: str) -> Path:
    target = Path(path_text).expanduser()
    if not target.name or len(target.name) > _MAX_OUT_NAME:
        raise ResourceError("输出文件名不合法")
    if target.exists():
        raise ResourceError(f"输出文件已存在，拒绝覆盖：{target}")
    if not target.parent.exists() or not target.parent.is_dir():
        raise ResourceError("输出文件的父目录不存在")
    return target


def _install_output(source: Path, target: Path) -> None:
    """Install a user-requested output atomically without overwriting it."""
    if target.exists():
        raise ResourceError(f"输出文件已存在，拒绝覆盖：{target}")
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, target)
        os.chmod(target, 0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _sniff_mime(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return ""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and b"WEBP" in head[:16]:
        return "image/webp"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if head.startswith(b"ID3") or head.startswith(b"\xff\xfb"):
        return "audio/mpeg"
    return ""


async def download_resource(
    message: dict,
    resource: dict,
    *,
    keep_media: bool = False,
    out: str = "",
) -> dict:
    """Stream one resource to a private temp file, cache, or explicit output."""
    target = _safe_output(out) if out else None
    fd, temp_name = tempfile.mkstemp(prefix="feishu-resource-", dir=str(config.HOME))
    os.close(fd)
    temp = Path(temp_name)
    os.chmod(temp, 0o600)
    total = 0
    digest = hashlib.sha256()
    content_type = ""
    download_url = ""
    try:
        inline = resource.get("_data") or b""
        if inline:
            if len(inline) > config.MESSAGE_RESOURCE_MAX_BYTES:
                raise ResourceError("消息资源超过单文件大小上限")
            temp.write_bytes(inline)
            total = len(inline)
            digest.update(inline)
            content_type = str(resource.get("mime") or "")
        elif _is_browser_only_file(resource):
            # Chat file_v3_ keys have no static HTTP route (2026-09-10:
            # static-resource 400/404, drive-stream 404).  The web client
            # mints a one-time URL through a channel invisible to request
            # hooks (service worker), so the bounded browser clicks the real
            # download button and harvests the download event instead.
            browser = await _download_file_via_browser(message, resource)
            saved = Path(browser["saved_path"])
            try:
                size = saved.stat().st_size
                if size > config.MESSAGE_RESOURCE_MAX_BYTES:
                    raise ResourceError("消息资源超过单文件大小上限")
                with saved.open("rb") as src, temp.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                total = size
                for chunk in _iter_file_chunks(temp):
                    digest.update(chunk)
                content_type = ""
            finally:
                try:
                    saved.unlink()
                except FileNotFoundError:
                    pass
        else:
            url = _url_for(resource)
            download_url = url
            public_media = transport.is_legacy_post_image_url(url)
            headers = (
                transport.public_media_headers()
                if public_media
                else transport.session_headers({"accept-encoding": "identity"})
            )
            async with transport.stream_request(
                "GET",
                url,
                headers=headers,
                timeout=config.MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
                max_response_bytes=config.MESSAGE_RESOURCE_MAX_BYTES,
                public=public_media,
            ) as response:
                if response.status_code >= 400:
                    raise ResourceError(f"消息资源下载失败（HTTP {response.status_code}）")
                if response.headers.get("content-encoding", "").lower() not in {"", "identity"}:
                    raise ResourceError("消息资源返回了不支持的压缩编码，已拒绝写入")
                content_type = str(response.headers.get("content-type", "")).split(";", 1)[0].strip()
                try:
                    declared = int(response.headers.get("content-length", "0"))
                except ValueError:
                    declared = 0
                if declared > config.MESSAGE_RESOURCE_MAX_BYTES:
                    raise ResourceError("消息资源超过单文件大小上限")
                with temp.open("wb") as f:
                    async for chunk in response.aiter_raw():
                        total += len(chunk)
                        if total > config.MESSAGE_RESOURCE_MAX_BYTES:
                            raise ResourceError("消息资源超过单文件大小上限")
                        f.write(chunk)
                        digest.update(chunk)
                    f.flush()
                    os.fsync(f.fileno())
        crypto = resource.get("_crypto") or {}
        if crypto and total:
            _decrypt_in_place(temp, crypto)
            total = temp.stat().st_size
            digest = hashlib.sha256(temp.read_bytes())
            content_type = ""  # re-sniff below; the ciphertext claimed a mime
        if total > config.MEDIA_JOB_MAX_BYTES:
            raise ResourceError("媒体任务超过总输入大小上限")
        sniffed = _sniff_mime(temp)
        if transport.is_legacy_post_image_url(download_url) and sniffed not in {
            "image/jpeg", "image/png", "image/gif", "image/webp",
        }:
            raise ResourceError("legacy post.img CDN 返回的内容不是可识别图片")
        result = {
            "resource_id": resource.get("resource_id"),
            "kind": resource.get("kind"),
            "status": "downloaded",
            "bytes": total,
            "sha256": digest.hexdigest(),
            "mime": content_type or resource.get("mime") or sniffed or mimetypes.guess_type(resource.get("name", ""))[0] or "application/octet-stream",
            "sniffed_mime": sniffed,
            "name": resource.get("name", ""),
        }
        if keep_media:
            cache = media_store.store(temp, str(resource.get("resource_id")))
            result["cache_path"] = str(cache)
        if target:
            _install_output(temp, target)
            result["path"] = str(target)
        return result
    except FeishuError:
        raise
    except Exception as exc:
        raise ResourceError(f"消息资源下载请求失败（{type(exc).__name__}）") from exc
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def resource_from_message(message: dict, resource_id_value: str = "") -> dict:
    return select_resource(message, resource_id_value)


# --- browser-based chat file download (worker only) -------------------------

async def _download_file_browser_impl(msg_id: str, chat_id: str, file_name: str, headed: bool = False) -> dict:
    """Download a chat file by clicking its real download button.

    The web client mints a one-time download URL through a service-worker
    channel invisible to request hooks (probe-observed 2026-09-10), so the
    only honest route is driving the UI and harvesting the download event.
    Saves under FEISHU_HOME; the parent process validates and installs it.
    """
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("浏览器文件下载只能由受限 Browser Worker 执行")
    if not chat_id or not file_name:
        raise ResourceError("浏览器文件下载需要 chat_id 和文件名")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright", hint="先运行 feishu.py login") from exc

    from . import browser_guard

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    fd, temp_name = tempfile.mkstemp(prefix="feishu-file-dl-", dir=str(config.HOME))
    os.close(fd)
    os.chmod(temp_name, 0o600)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=not headed,
            ignore_https_errors=False,
            accept_downloads=True,
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=config.BROWSER_ARGS,
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await ctx.add_cookies(auth.playwright_cookie_jar())
            await browser_guard.install(page)
            # Mirror the probe's proven open sequence: plain messenger first,
            # THEN the chatId deep link (the param only applies post-init).
            await page.goto(f"https://{domain}/next/messenger/",
                            wait_until="domcontentloaded", timeout=60000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(2500)
            await page.goto(f"https://{domain}/next/messenger/?chatId={chat_id}",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(6000)
            # The chatId deep link is unreliable on a warm SPA; clicking the
            # sidebar feed card (keyed by chat_id) definitely opens the chat.
            feed = page.locator(f'[data-feed-id="{chat_id}"]').first
            if await feed.count():
                try:
                    await feed.click(timeout=3000)
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass

            async def find_file_card():
                # File messages render as div.messageFile containers; text
                # matches alone also hit sidebar previews and earlier link
                # messages (a name substring like test.txt ⊂ feishu_probe_test.txt).
                files = page.locator("[class*=messageFile_filename]")
                for i in range(min(await files.count(), 10)):
                    cand = files.nth(i)
                    try:
                        if file_name[:30] not in (await cand.inner_text()):
                            continue
                        box = await cand.bounding_box()
                    except Exception:
                        continue
                    if box and box['x'] > 350:
                        row = cand.locator("xpath=ancestor::*[contains(@class,'message-content')][1]")
                        if await row.count():
                            return row.first
                        wrapper = cand.locator("xpath=ancestor::*[contains(@class,'messageFile_wrapper')][1]")
                        if await wrapper.count():
                            return wrapper.first
                        return cand
                return page.locator('__never__')

            card = await find_file_card()
            # Position the mouse over the message list (rightmost tall
            # scrollable) before wheeling — bare wheel events go nowhere.
            region = await page.evaluate(
                """() => {
                  const els = Array.from(document.querySelectorAll('div'))
                    .filter(e => e.scrollHeight > e.clientHeight + 100 && e.clientHeight > 300)
                    .map(e => { const r = e.getBoundingClientRect();
                        return {x: r.x, y: r.y, w: r.width, h: r.height}; })
                    .filter(b => b.w > 200).sort((a, b) => b.x - a.x);
                  return els[0] || null;
                }"""
            )
            if region:
                await page.mouse.move(region['x'] + region['w'] / 2,
                                      region['y'] + region['h'] / 2)
            for _ in range(30):  # the list may need a scroll to reveal the file
                if await card.count():
                    break
                await page.mouse.wheel(0, -1600)
                await page.wait_for_timeout(700)
            if not await card.count():
                # Search jump: messenger search for the file name, click the
                # 消息 result — lands directly on the file message.
                search_btn = page.get_by_text("搜索", exact=True).first
                if await search_btn.count():
                    await search_btn.click()
                    await page.wait_for_timeout(1000)
                for selector in ("input[placeholder*=搜索]", "input[type=search]", "input"):
                    box = page.locator(selector).first
                    if not await box.count():
                        continue
                    try:
                        await box.fill(file_name[:40])
                        await box.press("Enter")
                        break
                    except Exception:
                        continue
                await page.wait_for_timeout(4000)
                for label in ("文件", "消息", "Files", "Messages"):
                    tab = page.get_by_text(label, exact=True).last
                    if await tab.count():
                        try:
                            await tab.click()
                            await page.wait_for_timeout(2000)
                            break
                        except Exception:
                            pass
                hit = page.get_by_text(file_name[:30], exact=False).first
                if await hit.count():
                    try:
                        await hit.click()
                        await page.wait_for_timeout(3000)
                    except Exception:
                        pass
                card = await find_file_card()
            if not await card.count():
                diag_url = page.url
                diag_feed = await page.locator("[data-feed-id]").count()
                diag_title = await page.title()
                raise ResourceError(
                    f"浏览器里没找到文件消息：{file_name[:40]!r}"
                    f"（url={diag_url[-80:]} title={diag_title[:40]!r} feed_cards={diag_feed}）",
                )

            async def dump_menu_items(active_page) -> list:
                # Dropdowns render in portal layers — collect every visible
                # menu-ish item text so the diag shows what actually opened.
                try:
                    return await active_page.evaluate(
                        """() => {
                          const seen = new Set();
                          const out = [];
                          const sels = '[role=menuitem], [role=option], [class*=dropdown] li, [class*=menu] li, [class*=menuItem], [class*=menu-item]';
                          document.querySelectorAll(sels).forEach(e => {
                            const r = e.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                              const t = (e.textContent || '').trim().slice(0, 30);
                              if (t && !seen.has(t) && out.length < 15) { seen.add(t); out.push(t); }
                            }
                          });
                          return out;
                        }"""
                    )
                except Exception:
                    return []

            async def click_download_targets(active_page) -> str:
                # File cards carry ud__button--icon actions (options/download)
                # at their right edge; the global navItem-download is the
                # download-the-app button — never click that.
                n_icon = await card.locator(".ud__button--icon").count()
                clicked_log.append(f'icon_btns_in_card={n_icon}')
                for bi in range(min(n_icon, 3)):
                    scoped = card.locator(".ud__button--icon").nth(bi)
                    try:
                        await scoped.click(timeout=2500)
                        clicked_log.append(f'card-icon-btn#{bi}')
                        await page.wait_for_timeout(800)
                        items = await dump_menu_items(active_page)
                        clicked_log.append(f'menu#{bi}={items}')
                        hit = None
                        for sel in ('[role=menuitem]', '[class*=menuItem]', '[class*=menu-item]', '[class*=dropdown] li', '[class*=menu] li'):
                            loc = active_page.locator(sel).filter(has_text="下载")
                            if await loc.count():
                                hit = loc.last
                                break
                        if hit is not None:
                            await hit.click(timeout=2500)
                            clicked_log.append(f'icon-menu#bi:下载')
                            return f'card-icon-btn#{bi}:menu'
                        # No download entry — dismiss and try the next button.
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(300)
                    except Exception as e:
                        clicked_log.append(f'icon#{bi}-fail:{type(e).__name__}')
                for sel in ('[class*=download]', '[aria-label*="下载"]', '[data-tip*="下载"]'):
                    for i in range(min(await active_page.locator(sel).count(), 3)):
                        btn = active_page.locator(sel).nth(i)
                        try:
                            cls = (await btn.get_attribute('class')) or ''
                            if 'navItem' in cls:
                                continue  # download-the-app navbar button
                            if await btn.is_visible():
                                await btn.click(timeout=2000)
                                return f'{sel}#{i}:visible'
                        except Exception:
                            continue
                # Toolbar buttons can be display:none until the row hover
                # lands; force-click ignores actionability but still fires.
                for sel in ('[class*=download]', '[aria-label*="下载"]', '[data-tip*="下载"]'):
                    btn = active_page.locator(sel).first
                    try:
                        cls = (await btn.get_attribute('class')) or ''
                        if 'navItem' in cls:
                            continue
                        if await btn.count():
                            await btn.click(force=True, timeout=2000)
                            return f'{sel}:force'
                    except Exception:
                        continue
                return

            download = None
            clicked_log = []
            await card.hover(timeout=3000)
            await page.wait_for_timeout(1200)
            toolbar_diag = await page.evaluate(
                """() => {
                  const file = Array.from(document.querySelectorAll('[class*=messageFile_filename]'))
                    .find(e => e.getBoundingClientRect().x > 350);
                  if (!file) return null;
                  let row = file;
                  for (let d = 0; d < 8 && row; d++, row = row.parentElement) {
                    if ((row.className || '').toString().includes('MessageActionPermissionWrapper')) break;
                  }
                  if (!row) return null;
                  const out = [];
                  row.querySelectorAll('[role=button], button, [class*=icon], [data-tip], [aria-label]').forEach(b => {
                    const r = b.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && out.length < 25) {
                      out.push({cls: (b.className.toString().match(/[a-zA-Z][a-zA-Z0-9_-]{2,50}/g) || []).slice(0,3).join('.'),
                                tip: b.getAttribute('data-tip') || '', aria: b.getAttribute('aria-label') || '',
                                x: r.x|0, y: r.y|0});
                    }
                  });
                  return out;
                }"""
            )
            pass  # toolbar_diag reported at the end
            try:
                async with page.expect_download(timeout=8000) as dl_info:
                    r = await click_download_targets(page)
                    if r:
                        clicked_log.append('hover-icon:' + r)
                download = await dl_info.value
            except Exception:
                pass
            if download is None:
                # Hover toolbar may hide 下载 under the ⋯ overflow menu.
                try:
                    more = page.locator("[class*=more], [aria-label*=更多], [data-tip*=更多]").last
                    if await more.count() and await more.is_visible():
                        async with page.expect_download(timeout=8000) as dl_info:
                            await more.click(timeout=2500)
                            await page.wait_for_timeout(600)
                            item = page.get_by_text("下载", exact=True).last
                            if await item.count() and await item.is_visible():
                                await item.click(timeout=2500)
                                clicked_log.append("menu:下载")
                        download = await dl_info.value
                except Exception:
                    pass
            if download is None:
                # Right-click context menu → 下载.
                try:
                    async with page.expect_download(timeout=8000) as dl_info:
                        await card.click(button="right", timeout=2500)
                        await page.wait_for_timeout(600)
                        item = page.get_by_text("下载", exact=True).last
                        if await item.count() and await item.is_visible():
                            await item.click(timeout=2500)
                            clicked_log.append("ctx:下载")
                    download = await dl_info.value
                except Exception:
                    pass
            if download is None:
                # Card body opens a preview popup; its page has the button.
                before = set(ctx.pages)
                try:
                    await card.click(timeout=2500)
                except Exception:
                    pass
                await page.wait_for_timeout(2000)
                if len(ctx.pages) == len(before):
                    try:
                        await card.dblclick(timeout=2500)
                        clicked_log.append('dblclick')
                    except Exception:
                        pass
                await page.wait_for_timeout(2500)
                n_popups = len(ctx.pages) - len(before)
                clicked_log.append(f'popups={n_popups}')
                if n_popups == 0:
                    # Same-page preview overlay/dialog: its download button is
                    # on the main page now.
                    try:
                        async with page.expect_download(timeout=10000) as dl_info:
                            r = await click_download_targets(page)
                            if r:
                                clicked_log.append('dialog-icon:' + r)
                        download = await dl_info.value
                    except Exception:
                        pass
                for popup in ctx.pages:
                    if popup in before:
                        continue
                    try:
                        async with popup.expect_download(timeout=15000) as dl_info:
                            r = await click_download_targets(popup)
                            if r:
                                clicked_log.append('popup-icon:' + r)
                        download = await dl_info.value
                    except Exception:
                        continue
                    break
            if download is None:
                try:
                    (config.HOME / '.last-file-download-diag.json').write_text(json.dumps({
                        'clicked': clicked_log, 'toolbar': toolbar_diag,
                    }, ensure_ascii=False, indent=1))
                except Exception:
                    pass
                try:
                    btn_info = await page.evaluate(
                        """() => Array.from(document.querySelectorAll('[class*=download], [aria-label*=下载], [data-tip*=下载]')).slice(0,6).map(b => {
                          const r = b.getBoundingClientRect();
                          const chain = [];
                          let el = b;
                          for (let d = 0; d < 5 && el; d++, el = el.parentElement) {
                            chain.push(el.tagName.toLowerCase() + '.' + Array.from(el.classList).filter(c => /^[a-zA-Z][a-zA-Z0-9_-]{1,40}$/.test(c)).slice(0,2).join('.'));
                          }
                          return {cls: b.className.toString().slice(0,80), vis: r.width > 0 && r.height > 0, x: r.x|0, y: r.y|0, chain};
                        })"""
                    )
                    clicked_log.append(json.dumps(btn_info, ensure_ascii=False)[:600])
                except Exception as e:
                    clicked_log.append('btninfo-fail:' + type(e).__name__)
                try:
                    btn_count = 0
                    for sel in ('[class*=download]', '[aria-label*=下载]', '[data-tip*=下载]'):
                        btn_count += await page.locator(sel).count()
                    clicked_log.append(f'dl_btns_on_page={btn_count}')
                    for pg in ctx.pages:
                        clicked_log.append(f'page:{pg.url[-60:]}')
                except Exception:
                    pass
                dom_diag = await page.evaluate(
                    """(fname) => {
                      const matches = Array.from(document.querySelectorAll('*'))
                        .filter(e => e.children.length === 0 && (e.textContent || '').includes(fname));
                      return matches.slice(0, 4).map(m => {
                        const chain = [];
                        let el = m;
                        for (let d = 0; d < 6 && el; d++, el = el.parentElement) {
                          chain.push(el.tagName.toLowerCase() + '.' +
                            Array.from(el.classList).filter(c => /^[a-zA-Z][a-zA-Z0-9_-]{1,40}$/.test(c)).slice(0,3).join('.'));
                        }
                        const r = m.getBoundingClientRect();
                        return {chain, x: r.x|0, y: r.y|0};
                      });
                    }""", file_name[:40])
                raise ResourceError("未触发文件下载。"
                                    f" clicked={clicked_log} toolbar={json.dumps(toolbar_diag, ensure_ascii=False)[:700]}")
            await download.save_as(temp_name)
            suggested = download.suggested_filename or ""
        finally:
            await ctx.close()

    size = os.path.getsize(temp_name)
    if size == 0:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise ResourceError("浏览器下载得到空文件")
    if size > config.MESSAGE_RESOURCE_MAX_BYTES:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise ResourceError("消息资源超过单文件大小上限")
    return {
        "saved_path": temp_name,
        "size": size,
        "suggested_name": suggested,
    }
