"""History + name resolution + markdown rendering for messages."""

from __future__ import annotations

import asyncio
import json
import os
import re

from . import config, gateway
from .message_model import public_message
from .output import fmt_ts, note

_CHAT_TYPE = {0: "unknown", 1: "p2p", 2: "group", 3: "topic"}

# Values the wire-walk heuristic has been observed to return for *every* user
# when cmd=64/5023 answers with an error payload instead of a UserInfo proto
# (2026-09-02 实踩：所有发送者显示为 "xi tong xiao xi").  Never cache or
# display them; treat as lookup failure.
_NAME_DENYLIST = frozenset({
    "xi tong xiao xi", "系统消息", "system message", "system", "unknown",
})

# A display name shared by this many distinct user ids in the cache is a
# poison signature (real people can share a name; one string covering a whole
# tenant cannot).
_NAME_UNIFORMITY_LIMIT = 3


def _name_ok(value) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.lower() not in _NAME_DENYLIST


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
        names = json.loads(config.NAMES_CACHE.read_text())
    except Exception:
        return {"users": {}, "chats": {}}
    names.setdefault("users", {})
    names.setdefault("chats", {})
    # Purge poisoned entries left by earlier heuristic failures so a bad
    # lookup cannot survive forever in the cache.
    for section in ("users", "chats"):
        names[section] = {
            key: value for key, value in names[section].items() if _name_ok(value)
        }
    return names


def _drop_uniform_names(names: dict) -> None:
    """Drop user names shared by >= _NAME_UNIFORMITY_LIMIT distinct ids."""
    histogram: dict[str, list[str]] = {}
    for uid, value in names["users"].items():
        histogram.setdefault(value, []).append(uid)
    for value, uids in histogram.items():
        if len(uids) >= _NAME_UNIFORMITY_LIMIT:
            for uid in uids:
                del names["users"][uid]


_MENTION_RE = re.compile(
    r'<at\b[^>]*\b(?:user_id|userid|id)=["\']([A-Za-z0-9_-]+)["\'][^>]*>@?([^<]+)</at>',
    re.IGNORECASE,
)


def harvest_mentions(messages: list[dict], names: dict | None = None) -> bool:
    """Harvest user_id -> display_name mappings from mentions in messages.

    In group chats, cmd=64/5023 may be dead on current web tenants, but mentions
    preserve the ground-truth user_id -> display name mapping.
    """
    if names is None:
        names = _load_names()
    dirty = False
    sources = names.setdefault("sources", {})
    users = names.setdefault("users", {})
    for m in messages:
        text_candidates = [
            str(m.get("text") or ""),
            str((m.get("content") or {}).get("text") or "") if isinstance(m.get("content"), dict) else "",
        ]
        for text in text_candidates:
            if "<at" not in text:
                continue
            for uid, raw_name in _MENTION_RE.findall(text):
                clean = raw_name.strip().lstrip("@").strip()
                if not _name_ok(clean) or clean.lower() in ("所有人", "all", "everyone"):
                    continue
                if users.get(uid) != clean:
                    users[uid] = clean
                    sources[f"user:{uid}"] = "message_mention"
                    dirty = True
    if dirty:
        _drop_uniform_names(names)
    return dirty


def _save_names(names: dict) -> None:
    config.ensure_home()
    config.NAMES_CACHE.write_text(json.dumps(names, ensure_ascii=False, indent=1))
    os.chmod(config.NAMES_CACHE, 0o600)


def apply_direction(messages: list[dict], my_id: str = "") -> None:
    """Label each message with is_me/direction from a whoami user id.

    This is the deterministic attribution anchor: even when display-name
    resolution fails, the p2p roles (我 / 对方) can never be swapped.
    """
    my_id = str(my_id or "")
    for m in messages:
        from_id = str(m.get("from_id") or "")
        is_me = bool(my_id) and bool(from_id) and from_id == my_id
        m["is_me"] = is_me
        m["direction"] = "outgoing" if is_me else ("incoming" if from_id else "")
        if isinstance(m.get("sender"), dict):
            m["sender"]["is_me"] = is_me


async def resolve_names(messages: list[dict], my_id: str = "", my_name: str = "") -> None:
    """Fill from_name/chat_name in place, caching cmd=64/5023 lookups.

    ``my_id``/``my_name`` come from whoami (reliable); the self name always
    wins over any cached or heuristic value.
    """
    names = _load_names()
    dirty = False
    if my_id and _name_ok(my_name) and names["users"].get(my_id) != my_name.strip():
        names["users"][my_id] = my_name.strip()
        dirty = True

    # Ground-truth name resolution from mentions inside the messages themselves
    if harvest_mentions(messages, names):
        dirty = True
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
        if _name_ok(value):
            names["chats"][cid] = value.strip()
            dirty = True
    any_chat = sorted(chat_ids)[0] if chat_ids else "0"
    missing_users = sorted(
        uid for uid in user_ids if uid not in names["users"] and uid != my_id
    )
    user_results = await asyncio.gather(
        *(lookup(gateway.get_user_name, uid, any_chat) for uid in missing_users)
    )
    for uid, value in user_results:
        # Only cache plausible names: a failed/garbage lookup must not be
        # persisted, otherwise one bad response poisons every later render.
        if _name_ok(value):
            names["users"][uid] = value.strip()
            dirty = True
    before = len(names["users"])
    _drop_uniform_names(names)
    if len(names["users"]) != before:
        dirty = True
    if dirty:
        _save_names(names)
    for m in messages:
        m["from_name"] = names["users"].get(m.get("from_id", ""), "")
        m["chat_name"] = names["chats"].get(m.get("chat_id", ""), "")
        m["name_status"] = "resolved" if m["from_name"] else "unresolved"
        if isinstance(m.get("sender"), dict):
            m["sender"]["name"] = m["from_name"]


async def history(chat_id: str, start_time: int = 0, end_time: int = 0, limit: int = 30,
                  my_id: str = "", my_name: str = "") -> dict:
    """→ {'messages': [...], 'has_more': bool}, chronological order."""
    if not 1 <= limit <= config.MAX_HISTORY_LIMIT:
        raise ValueError(f"history limit 必须在 1..{config.MAX_HISTORY_LIMIT} 之间")

    async def run() -> dict:
        res = await gateway.search_msg_ids(chat_id, start_time, end_time, limit=limit)
        msgs = await gateway.fetch_messages(res["msg_ids"])
        msgs = [m for m in msgs if in_time_window(m, start_time, end_time)]
        msgs.sort(key=lambda m: (m.get("create_time") or 0, m["msg_id"]))
        await resolve_names(msgs, my_id=my_id, my_name=my_name)
        apply_direction(msgs, my_id)
        return {"messages": msgs, "has_more": res["has_more"]}

    # This is an end-to-end deadline, not just a per-request timeout.  It
    # prevents a sequence of slow/repeated pages from keeping a CLI process
    # alive indefinitely.
    return await asyncio.wait_for(run(), timeout=config.HISTORY_TIMEOUT_SECONDS)


def harvest_chat_names(rows: list[dict], summaries: dict | None = None,
                       my_id: str = "") -> dict:
    """Feed chat display names from the Messenger DOM provider into the cache.

    For chats known (from the event pool) to be p2p, the conversation name is
    the peer's display name; when the peer's user id is unambiguous (exactly
    one non-me sender seen in that chat), infer the user-name mapping too.
    Returns a small report; provenance is kept in names['sources'].
    """
    names = _load_names()
    sources = names.setdefault("sources", {})
    harvested_chats = 0
    harvested_users = 0
    summaries = summaries or {}
    for row in rows:
        cid = str(row.get("chat_id") or "")
        name = str(row.get("name") or "").strip()
        if not cid or not _name_ok(name):
            continue
        if names["chats"].get(cid) != name:
            names["chats"][cid] = name
            sources[f"chat:{cid}"] = "browser_dom"
            harvested_chats += 1
        summary = summaries.get(cid) or {}
        if summary.get("chat_type") != 1:
            continue
        senders = [str(s) for s in (summary.get("senders") or [])]
        peers = [s for s in set(senders) if s and s != str(my_id or "")]
        if len(peers) != 1:
            continue
        peer = peers[0]
        # A p2p conversation title is the peer's name; only fill gaps, never
        # override a whoami-seeded or previously resolved name.
        if peer not in names["users"]:
            names["users"][peer] = name
            sources[f"user:{peer}"] = "p2p_chat_name"
            harvested_users += 1
    _drop_uniform_names(names)
    if harvested_chats or harvested_users:
        _save_names(names)
    return {"chat_names": harvested_chats, "peer_names": harvested_users}


def _sender_label(m: dict) -> str:
    """Display label for a sender; never invents a name.

    p2p chats get deterministic 我/对方 roles from ``is_me`` (whoami-anchored)
    so attribution cannot be swapped even when name resolution fails.
    """
    name = m.get("from_name") or ""
    if not _name_ok(name):
        name = ""
    from_id = str(m.get("from_id") or "")
    is_p2p = m.get("chat_type") in (1, "1", "p2p")
    # daemon events record the same fact as ``is_from_me`` rather than is_me.
    if m.get("is_me") or m.get("is_from_me"):
        return f"我（{name}）" if name else "我"
    if is_p2p:
        return f"对方（{name}）" if name else f"对方（id:{from_id}）" if from_id else "对方"
    return name or from_id or "?"


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
        who = _sender_label(m)
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
