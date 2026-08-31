"""Cookie lifecycle: Playwright QR login, manual paste fallback, probe.

Never prints, logs, or returns raw cookie values; the cookie header is
registered with output.register_secret the moment it exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from . import config, transport
from .errors import AuthRequired, FeishuError
from .output import note, register_secret, scrubbed_exc

from lark_bridge._urls import INTERNAL_API
from lark_bridge.client import LarkBridge

_REQUIRED = ("session", "passport_web_did")


def cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def load_cookies() -> dict:
    """→ {'cookies': {...}, 'domain': ...} or raises AuthRequired."""
    try:
        data = json.loads(config.COOKIES_FILE.read_text())
    except FileNotFoundError:
        raise AuthRequired("还没有登录态", hint="先跑 feishu.py login（扫码）或 login --manual")
    except Exception as e:
        raise AuthRequired(f"cookie 文件损坏：{scrubbed_exc(e)}", hint="重新 login")
    cookies = data.get("cookies", {})
    missing = [key for key in _REQUIRED if key not in cookies]
    if missing:
        raise AuthRequired(
            f"cookie 文件缺少关键字段：{', '.join(missing)}", hint="重新 login"
        )
    register_secret(cookie_header(cookies))
    for v in cookies.values():
        register_secret(v)
    return data


def save_cookies(cookies: dict, domain: str) -> None:
    config.ensure_home()
    tmp = config.COOKIES_FILE.with_suffix(".tmp")
    payload = {
        "version": 1,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "domain": domain,
        "cookies": cookies,
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, config.COOKIES_FILE)
    register_secret(cookie_header(cookies))
    for v in cookies.values():
        register_secret(v)


async def fetch_csrf(cookies: dict) -> dict:
    """POST /accounts/csrf → merge swp_csrf_token (and friends) into cookies."""
    resp = await transport.request(
        "POST",
        f"{INTERNAL_API}/accounts/csrf",
        headers={
            "Cookie": cookie_header(cookies),
            "x-api-version": "1.0.8",
            "x-app-id": "12",
            "x-device-info": "platform=websdk",
            "x-lgw-os-type": "1",
            "x-lgw-terminal-type": "2",
            "x-terminal-type": "2",
        },
        params={"_t": str(int(time.time() * 1000))},
        retries=1,
        timeout=10,
    )
    token = resp.cookies.get("swp_csrf_token")
    if not token:
        raise AuthRequired("未能从 /accounts/csrf 拿到 swp_csrf_token（会话可能已失效）")
    merged = dict(cookies)
    merged["swp_csrf_token"] = token
    register_secret(token)
    return merged


async def whoami(cookies: dict) -> dict:
    """GET /accounts/web/user → {'user_id','name',...}. AuthRequired if dead."""
    resp = await transport.request(
        "GET",
        f"{INTERNAL_API}/accounts/web/user",
        headers={
            "Cookie": cookie_header(cookies),
            "accept": "application/json, text/plain, */*",
            "x-api-version": "1.0.8",
            "x-app-id": "12",
            "x-csrf-token": cookies.get("swp_csrf_token", ""),
            "x-device-info": "platform=websdk",
            "x-lgw-os-type": "1",
            "x-lgw-terminal-type": "2",
            "x-locale": "zh-CN",
            "x-terminal-type": "2",
        },
        params={"app_id": "12", "_t": str(int(time.time() * 1000))},
        retries=1,
        timeout=10,
    )
    try:
        data = resp.json()
    except Exception:
        raise AuthRequired("探活接口没回 JSON（会话可能已失效）")
    user = (data.get("data") or {}).get("user") or {}
    uid = user.get("id")
    if not uid:
        raise AuthRequired("会话已失效（accounts/web/user 无用户数据）", hint="重新 login")
    return {
        "user_id": str(uid),
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "tenant": (user.get("tenant") or {}).get("name", "")
        if isinstance(user.get("tenant"), dict)
        else "",
    }


async def prepare() -> dict:
    """Load → refresh CSRF → verify → bind transport. Returns whoami dict."""
    async def run() -> dict:
        data = load_cookies()
        cookies = await fetch_csrf(data["cookies"])
        me = await whoami(cookies)
        save_cookies(cookies, data.get("domain") or config.DOMAIN)  # persist csrf merge
        transport.bind_session(
            cookie_header(cookies), cookies.get("swp_csrf_token", ""), data.get("domain") or config.DOMAIN
        )
        return me

    return await asyncio.wait_for(run(), timeout=config.AUTH_TIMEOUT_SECONDS)


async def touch(cookies: dict) -> dict:
    """Keep-alive: hit the account endpoint, merge refreshed cookies, persist."""
    me = await whoami(cookies)
    return me


# --- login ------------------------------------------------------------------

async def login_playwright() -> dict:
    """Headed browser, user scans QR themselves. Never types credentials."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise FeishuError(
            "venv 里还没有 playwright",
            hint="跑任意命令会自动装；或手动：~/.local/share/feishu/venv/bin/pip install playwright",
        )
    config.ensure_home()
    note(f"打开浏览器登录 {config.DOMAIN} —— 请自己扫码，脚本不接触账号密码")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=False,
            ignore_https_errors=False,
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=["--no-proxy-server"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(f"https://{config.DOMAIN}/messenger/")
            deadline = time.monotonic() + config.LOGIN_WAIT_SECONDS
            while time.monotonic() < deadline:
                cookies = {
                    c["name"]: c["value"]
                    for c in await ctx.cookies()
                    if "feishu.cn" in (c.get("domain") or "")
                }
                if all(k in cookies for k in _REQUIRED):
                    try:
                        cookies = await fetch_csrf(cookies)
                        me = await whoami(cookies)
                        save_cookies(cookies, config.DOMAIN)
                        note(f"✓ 已登录：{me['name']}（{me['user_id']}）")
                        return me
                    except FeishuError:
                        pass  # page may still be mid-login; keep polling
                await asyncio.sleep(2)
            raise AuthRequired("等待登录超时（5 分钟）", hint="重新跑 feishu.py login")
        finally:
            await ctx.close()


async def login_manual() -> dict:
    """Paste a cookie copied from browser DevTools. Hidden echo."""
    import getpass

    note("从浏览器 DevTools → Application → Cookies 复制 feishu.cn 的完整 Cookie 头")
    raw = getpass.getpass("Cookie（输入不显示，回车结束）: ").strip()
    if not raw:
        raise AuthRequired("没有输入 cookie")
    cookies = LarkBridge._parse_cookies(raw)
    missing = [k for k in _REQUIRED if k not in cookies]
    if missing:
        raise AuthRequired(
            f"cookie 缺少关键字段：{', '.join(missing)}",
            hint="确认复制的是已登录 feishu.cn 会话的完整 Cookie",
        )
    cookies = await fetch_csrf(cookies)
    me = await whoami(cookies)
    save_cookies(cookies, config.DOMAIN)
    note(f"✓ 已登录：{me['name']}（{me['user_id']}）")
    return me
