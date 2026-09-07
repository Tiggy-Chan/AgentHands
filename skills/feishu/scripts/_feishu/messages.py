"""History + name resolution + markdown rendering for messages."""

from __future__ import annotations

import asyncio
import json
import os

from . import config, gateway
from .message_model import public_message
from .output import fmt_ts, note

_CHAT_TYPE = {0: "unknown", 1: "p2p", 2: "group", 3: "topic"}


def _seconds(value) -> float:
    try:
        stamp = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return stamp / 1000.0 if stamp > 1e12 else stamp


def in_time_window(message: dict, start_time: int = 0, end_time: int = 0) -> bool:
    """Apply the requested window locally as a second line of defense."""
    stamp = _seconds(message.get("create_time"))
    if not stamp:
        return False if start_time or end_time else True
    if start_time and stamp < start_time:
        return False
    if end_time and stamp > end_time:
        return False
    return True


def _load_names() -> dict:
    try:
        return json.loads(config.NAMES_CACHE.read_text())
    except Exception:
        return {"users": {}, "chats": {}}


def _save_names(names: dict) -> None:
    config.ensure_home()
    config.NAMES_CACHE.write_text(json.dumps(names, ensure_ascii=False, indent=1))
    os.chmod(config.NAMES_CACHE, 0o600)


async def resolve_names(messages: list[dict]) -> None:
    """Fill from_name/chat_name in place, caching cmd=64/5023 lookups."""
    names = _load_names()
    dirty = False
    chat_ids = {m["chat_id"] for m in messages if m.get("chat_id")}
    user_ids = {m["from_id"] for m in messages if m.get("from_id")}
    semaphore = asyncio.Semaphore(max(1, config.NAME_LOOKUP_CONCURRENCY))

    async def lookup(func, identifier: str, *extra) -> tuple[str, str]:
        async with semaphore:
            try:
                value = await asyncio.wait_for(
                    func(identifier, *extra), timeout=config.NAME_LOOKUP_TIMEOUT_SECONDS
                )
                return identifier, value or ""
            except Exception:
                return identifier, ""

    missing_chats = sorted(cid for cid in chat_ids if cid not in names["chats"])
    chat_results = await asyncio.gather(
        *(lookup(gateway.get_group_name, cid) for cid in missing_chats)
    )
    for cid, value in chat_results:
        names["chats"][cid] = value
        dirty = True
    any_chat = sorted(chat_ids)[0] if chat_ids else "0"
    missing_users = sorted(uid for uid in user_ids if uid not in names["users"])
    user_results = await asyncio.gather(
        *(lookup(gateway.get_user_name, uid, any_chat) for uid in missing_users)
    )
    for uid, value in user_results:
        names["users"][uid] = value
        dirty = True
    if dirty:
        _save_names(names)
    for m in messages:
        m["from_name"] = names["users"].get(m.get("from_id", ""), "")
        m["chat_name"] = names["chats"].get(m.get("chat_id", ""), "")
        if isinstance(m.get("sender"), dict):
            m["sender"]["name"] = m["from_name"]


async def history(chat_id: str, start_time: int = 0, end_time: int = 0, limit: int = 30) -> dict:
    """→ {'messages': [...], 'has_more': bool}, chronological order."""
    if not 1 <= limit <= config.MAX_HISTORY_LIMIT:
        raise ValueError(f"history limit 必须在 1..{config.MAX_HISTORY_LIMIT} 之间")

    async def run() -> dict:
        res = await gateway.search_msg_ids(chat_id, start_time, end_time, limit=limit)
        msgs = await gateway.fetch_messages(res["msg_ids"])
        msgs = [m for m in msgs if in_time_window(m, start_time, end_time)]
        msgs.sort(key=lambda m: (m.get("create_time") or 0, m["msg_id"]))
        await resolve_names(msgs)
        return {"messages": msgs, "has_more": res["has_more"]}

    # This is an end-to-end deadline, not just a per-request timeout.  It
    # prevents a sequence of slow/repeated pages from keeping a CLI process
    # alive indefinitely.
    return await asyncio.wait_for(run(), timeout=config.HISTORY_TIMEOUT_SECONDS)


def render_markdown(msgs: list[dict], title: str = "") -> str:
    lines = [f"# {title}"] if title else []
    for m in msgs:
        if m.get("kind") == "mention_context":
            ctx = m.get("context") or {}
            lines.append(
                f"- `context:{m.get('trigger_msg_id', '?')}` 话题上下文 "
                f"（{len(ctx.get('messages', []))} 条，"
                f"{'完整' if ctx.get('complete') else '不完整'}）"
            )
            continue
        if m.get("kind") == "mention_context_error":
            lines.append(
                f"- `context-error:{m.get('trigger_msg_id', '?')}` "
                f"上下文抓取失败：{m.get('error', 'unknown')}"
            )
            continue
        who = m.get("from_name") or m.get("from_id", "?")
        ts = fmt_ts(m.get("create_time"))
        text = m.get("text") or f"[{m.get('type_name', '?')} 消息]"
        if m.get("is_removed"):
            text = "~~已撤回~~"
        body = "\n".join(f"  > {ln}" for ln in str(text).splitlines())
        suffix = []
        if m.get("resources"):
            suffix.append(f"资源 {len(m['resources'])} 个")
        if m.get("nested_messages"):
            suffix.append(f"合并消息 {len(m['nested_messages'])} 条")
        semantic = (m.get("decode") or {}).get("status")
        if semantic and semantic != "decoded":
            suffix.append(f"解码状态 {semantic}")
        tail = f"｜{'；'.join(suffix)}" if suffix else ""
        lines.append(f"- `{m['msg_id']}` **{who}** ({ts}){tail}\n{body}")
    return "\n".join(lines)


def render_json(msgs: list[dict]) -> list[dict]:
    rendered = []
    for message in msgs:
        item = public_message(message)
        item["create_time_iso"] = fmt_ts(message.get("create_time"))
        rendered.append(item)
    return rendered
