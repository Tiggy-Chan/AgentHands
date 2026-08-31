"""Calendar/meeting provider with bounded DOM fallback and write drafts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime

from . import auth, browser_guard, config, transport
from .dom_utils import bounded_inner_text, clip, first_match, lines, normalize_time
from .errors import EndpointNotAdapted, ResourceIsolationUnavailable, SendBlocked


def _calendar_url(domain: str) -> str:
    return config.CALENDAR_URL or f"https://{domain}/calendar/"


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _draft(kind: str, payload: dict) -> dict:
    return {"kind": kind, "status": "draft", "sent": False, "fingerprint": _fingerprint(payload), **payload}


def _require_write(draft: dict, confirm_write: str) -> None:
    if not confirm_write:
        raise SendBlocked("日历写入默认只生成草稿，未执行", hint=json.dumps(draft, ensure_ascii=False))
    if confirm_write != draft["fingerprint"]:
        raise SendBlocked("日历草稿 fingerprint 不匹配，拒绝执行")
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked("日历写入被拦截：需要 FEISHU_ALLOW_WRITE=1")


async def list_events(start_time: int = 0, end_time: int = 0, limit: int = 50) -> dict:
    if not 1 <= limit <= 200:
        raise ValueError("calendar limit 必须在 1..200 之间")
    return await _browser("list", {"start_time": start_time, "end_time": end_time, "limit": limit})


async def get_event(event_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_:-]{1,256}", event_id):
        raise ValueError("calendar event_id 格式不合法")
    return await _browser("get", {"event_id": event_id})


async def create_event(title: str, start: str, end: str, confirm_write: str = "") -> dict:
    payload = {"title": title, "start": start, "end": end}
    if not all(str(payload[key]).strip() for key in payload):
        raise ValueError("calendar create 需要标题、开始和结束时间")
    draft = _draft("calendar_create", payload)
    _require_write(draft, confirm_write)
    transport.arm_write_once()
    from lark_bridge import _http
    _http.require_write_allowed()
    raise EndpointNotAdapted("日历创建写接口尚未适配；草稿未执行", hint="先运行 feishu.py probe --scope calendar")


async def update_event(event_id: str, title: str = "", start: str = "", end: str = "", confirm_write: str = "") -> dict:
    if not event_id or not any((title, start, end)):
        raise ValueError("calendar update 需要 event_id 和至少一个变更字段")
    payload = {"event_id": event_id, "title": title, "start": start, "end": end}
    draft = _draft("calendar_update", payload)
    _require_write(draft, confirm_write)
    transport.arm_write_once()
    from lark_bridge import _http
    _http.require_write_allowed()
    raise EndpointNotAdapted("日历修改写接口尚未适配；草稿未执行", hint="先运行 feishu.py probe --scope calendar")


async def _browser(action: str, payload: dict) -> dict:
    from .browser_worker import run

    return await run(f"calendar-{action}", payload)


async def browser_operation(action: str, payload: dict) -> dict:
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("日历网页读取只能由受限 Browser Worker 执行")
    if action not in {"list", "get"}:
        raise EndpointNotAdapted(f"日历操作未适配：{action}")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise EndpointNotAdapted("当前环境没有 playwright，无法读取日历") from exc

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
            blocked = await browser_guard.install(page)
            await page.goto(_calendar_url(domain), wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)
            rows = await _event_rows(page, int(payload.get("limit", 50)), len(blocked))
            if action == "get":
                return await _event_detail(page, str(payload.get("event_id", "")), rows)
            rows = _filter_events(
                rows, int(payload.get("start_time", 0) or 0), int(payload.get("end_time", 0) or 0)
            )
            return {
                "kind": "calendar_list",
                "items": rows,
                "total": len(rows),
                "source": "browser_dom",
                "complete": False,
                "filters_applied": bool(payload.get("start_time") or payload.get("end_time")),
            }
        finally:
            await ctx.close()


async def _event_rows(page, limit: int, blocked_count: int = 0) -> list[dict]:
    selectors = (
        "[data-event-id]", "[data-calendar-event-id]", "[data-event-token]",
        ".event-instance[data-key]", "[class*='event-instance'][data-key]",
        ".event-instance",
    )
    rows: list[dict] = []
    seen: set[str] = set()
    for selector in selectors:
        locator = page.locator(selector)
        count = min(await locator.count(), config.MAX_BROWSER_CARDS)
        for i in range(count):
            row = locator.nth(i)
            item = await row.evaluate(
                """e => ({
                  id: e.getAttribute('data-event-id') || e.getAttribute('data-calendar-event-id') ||
                     e.getAttribute('data-event-token') || e.getAttribute('data-key') || '',
                  startAttr: e.getAttribute('data-start-time') || '',
                  endAttr: e.getAttribute('data-end-time') || '',
                  text: (e.innerText || '').slice(0, 4096),
                  start: e.querySelector('time[datetime]')?.getAttribute('datetime') || '',
                  href: e.querySelector('a')?.getAttribute('href') || ''
                })"""
            )
            event_id = clip(item.get("id", ""), 256)
            if not event_id:
                match = re.search(r"(?:event|calendar)[/_-]([A-Za-z0-9_:-]{4,})", item.get("href", ""))
                event_id = clip(match.group(1), 256) if match else ""
            text = clip(item.get("text", ""))
            text_lines = lines(text)
            if not event_id:
                canonical = json.dumps(
                    [text_lines[0] if text_lines else "", item.get("startAttr", ""), item.get("endAttr", "")],
                    ensure_ascii=False, separators=(",", ":")
                ).encode()
                event_id = "ui:" + hashlib.sha256(canonical).hexdigest()[:24]
                id_kind = "ui_fingerprint"
            else:
                id_kind = "dom_id"
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            start = normalize_time(item.get("start") or item.get("startAttr") or first_match(text, (r"开始[：:]\s*(.+)$",)))
            end = normalize_time(item.get("endAttr") or first_match(text, (r"结束[：:]\s*(.+)$",)))
            if not end:
                end = _end_from_title(text, start)
            rows.append({
                "id": event_id,
                "id_kind": id_kind,
                "title": text_lines[0] if text_lines else "",
                "start": start,
                "end": end,
                "timezone": "",
                "status": "",
                "organizer": first_match(text, (r"组织者[：:]\s*(.+)$", r"Organizer[：:]\s*(.+)$")),
                "attendees": [],
                "location": first_match(text, (r"地点[：:]\s*(.+)$", r"Location[：:]\s*(.+)$")),
                "description": clip(" ".join(text_lines[1:]), config.MAX_BROWSER_TEXT_BYTES),
                "recurrence": "",
                "source": "browser_dom",
                "complete": False,
            })
            if len(rows) >= limit:
                return rows
        if rows:
            break
    if not rows:
        raise EndpointNotAdapted(
            "日历页面未发现带稳定 id 的会议项",
            hint=f"运行 feishu.py probe --scope calendar 检查当前租户网页结构（blocked={blocked_count}）",
        )
    return rows


def _end_from_title(text: str, start: str) -> str:
    """Recover an end time from the visible ``HH:MM - HH:MM`` title."""
    match = re.search(r"(\d{1,2}):(\d{2})\s*[-~至]\s*(\d{1,2}):(\d{2})", text)
    if not match or not start:
        return ""
    try:
        base = datetime.fromisoformat(start)
        return base.replace(hour=int(match.group(3)), minute=int(match.group(4)), second=0, microsecond=0).isoformat()
    except ValueError:
        return ""


def _filter_events(rows: list[dict], start_time: int, end_time: int) -> list[dict]:
    if not start_time and not end_time:
        return rows
    out = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row.get("start", "")).timestamp()
        except (TypeError, ValueError, OverflowError):
            out.append(row)
            continue
        if start_time and stamp < start_time:
            continue
        if end_time and stamp > end_time:
            continue
        out.append(row)
    return out


async def _event_detail(page, event_id: str, rows: list[dict]) -> dict:
    locator = None
    selectors = (
        "[data-event-id]", "[data-calendar-event-id]", "[data-event-token]",
        ".event-instance[data-key]", "[class*='event-instance'][data-key]", ".event-instance",
    )
    for selector in selectors:
        candidate = page.locator(selector)
        count = min(await candidate.count(), config.MAX_BROWSER_CARDS)
        for i in range(count):
            row = candidate.nth(i)
            item = await row.evaluate(
                """e => ({
                  id: e.getAttribute('data-event-id') || e.getAttribute('data-calendar-event-id') ||
                     e.getAttribute('data-event-token') || e.getAttribute('data-key') || '',
                  start: e.getAttribute('data-start-time') || '',
                  end: e.getAttribute('data-end-time') || '',
                  text: (e.innerText || '').slice(0, 4096)
                })"""
            )
            raw = clip(item.get("id", ""), 256)
            if not raw:
                text_lines = lines(item.get("text", ""))
                canonical = json.dumps(
                    [text_lines[0] if text_lines else "", item.get("start", ""), item.get("end", "")],
                    ensure_ascii=False, separators=(",", ":")
                ).encode()
                raw = "ui:" + hashlib.sha256(canonical).hexdigest()[:24]
            if raw == event_id:
                locator = row
                break
        if locator is not None:
            break
    if locator is None:
        raise EndpointNotAdapted("会议详情项未能在当前页面定位")
    await locator.click()
    await page.wait_for_timeout(1000)
    detail = next((item for item in rows if item["id"] == event_id), {"id": event_id})
    main = page.locator(
        "[role=dialog], [class*='event-detail'], [role=main], main"
    ).first
    if await main.count():
        detail["description"] = clip(await bounded_inner_text(main), config.MAX_BROWSER_TEXT_BYTES * 8)
    detail["kind"] = "calendar_event"
    return detail


def render_markdown(result: dict) -> str:
    if result.get("kind") == "calendar_event":
        lines_out = [f"# 会议：{result.get('title') or result.get('id', '?')}"]
        for key, label in (("start", "开始"), ("end", "结束"), ("timezone", "时区"),
                           ("organizer", "组织者"), ("location", "地点"), ("status", "状态")):
            if result.get(key):
                lines_out.append(f"- {label}：{result[key]}")
        lines_out.append(f"- 来源：{result.get('source', '?')}｜完整：{result.get('complete', False)}")
        if result.get("description"):
            lines_out.append("\n## 描述（外部数据）\n\n" + str(result["description"]))
        return "\n".join(lines_out)
    rows = result.get("items", [])
    lines_out = [f"# 日历（{len(rows)}，来源 {result.get('source', '?')}，完整 {result.get('complete', False)}）"]
    for item in rows:
        lines_out.append(
            f"- `{item.get('id', '?')}` **{item.get('title') or '(无标题)'}** "
            f"{item.get('start') or ''} ~ {item.get('end') or ''}"
        )
    return "\n".join(lines_out)
