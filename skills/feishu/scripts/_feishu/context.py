"""Thread/context reconstruction around one message.

Pulls the target, walks the parent chain to the root, then searches the
chat in a time window around the target and keeps the root's replies plus
surrounding chatter. Always reports completeness — a partial thread must
never look like a full one.
"""

from __future__ import annotations

import json

from . import config
from . import gateway, messages
from .message_model import public_message
from .output import fmt_ts

_MAX_PARENT_CHAIN = 20


def _encoded_size(message: dict) -> int:
    try:
        return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode())
    except (TypeError, ValueError):
        return 0


def _clean_message(message: dict) -> dict:
    """Keep the schema-2 envelope while stripping private transport fields."""
    safe = public_message(message)
    fields = (
        "schema", "id", "msg_id", "chat_id", "chat_type", "sender", "from_id",
        "created_at", "create_time", "type", "type_name", "content", "text",
        "mentions", "mention_user_ids", "resources", "nested_messages", "state",
        "decode", "decode_status", "complete", "parent_id", "root_id", "thread_id",
        "is_removed", "is_truncated", "at_out_chatter_ids", "at_me_server", "from_name",
        "chat_name",
    )
    return {key: safe[key] for key in fields if key in safe}


async def thread_context(msg_id: str, window_min: int = 15, limit: int = 60) -> dict:
    if not 0 <= window_min <= config.MAX_THREAD_WINDOW_MINUTES:
        raise ValueError(f"thread window 必须在 0..{config.MAX_THREAD_WINDOW_MINUTES} 分钟之间")
    if not 1 <= limit <= config.CONTEXT_MAX_MESSAGES:
        raise ValueError(f"thread limit 必须在 1..{config.CONTEXT_MAX_MESSAGES} 之间")

    return await _thread_context(msg_id, window_min, limit)


async def _thread_context(msg_id: str, window_min: int, limit: int) -> dict:
    targets = await gateway.fetch_messages([msg_id])
    if not targets:
        from .errors import FeishuError

        raise FeishuError(f"取不到消息 {msg_id}（id 不对，或无权限，或已删除）")
    target = targets[0]
    chat_id = target["chat_id"]
    create_s = int(target.get("create_time") or 0)
    if create_s > 1e12:  # tolerate ms
        create_s //= 1000

    # parent chain up to the root
    chain: list[dict] = []
    missing: list[str] = []
    seen = {msg_id}
    pid = target.get("parent_id") or ""
    while pid and pid not in seen and len(chain) < _MAX_PARENT_CHAIN:
        seen.add(pid)
        got = await gateway.fetch_messages([pid])
        if not got:
            missing.append(pid)
            break
        chain.append(got[0])
        pid = got[0].get("parent_id") or ""
    chain.reverse()  # root first
    root = chain[0] if chain else target
    root_id = target.get("root_id") or root["msg_id"]

    # time window around the target inside the same chat
    half = window_min * 60
    res = await gateway.search_msg_ids(
        chat_id, max(0, create_s - half), create_s + half, limit=limit
    )
    window_msgs = await gateway.fetch_messages(res["msg_ids"])
    window_msgs = [
        m for m in window_msgs
        if messages.in_time_window(m, max(0, create_s - half), create_s + half)
    ]
    window_msgs.sort(key=lambda m: (m.get("create_time") or 0, m["msg_id"]))

    chain_ids = {m["msg_id"] for m in chain} | {msg_id}
    replies = [
        m for m in window_msgs
        if (m.get("root_id") and m["root_id"] == root_id and m["msg_id"] not in chain_ids)
    ]
    context = [m for m in window_msgs if m["msg_id"] not in chain_ids and m not in replies]

    # A reply can be posted long after the target.  The normal time window is
    # intentionally narrow for surrounding chatter, so do one bounded root
    # scan when it found no replies.  The gateway has no root-id search filter;
    # 200 ids is the hard ceiling and an incomplete scan stays visible.
    reply_scan_has_more = False
    if root_id and not replies:
        broad = await gateway.search_msg_ids(
            chat_id, 0, 0, limit=config.MAX_HISTORY_LIMIT
        )
        reply_scan_has_more = bool(broad.get("has_more"))
        broad_msgs = await gateway.fetch_messages(broad["msg_ids"])
        known_ids = chain_ids | {m["msg_id"] for m in context}
        replies.extend(
            m for m in broad_msgs
            if m.get("root_id") == root_id and m["msg_id"] not in known_ids
        )

    all_msgs = chain + [target] + replies + context
    await messages.resolve_names(all_msgs)

    result = {
        "target": target,
        "root": root,
        "chain": chain,
        "replies": replies,
        "window": context,
        "complete": not missing and not res["has_more"] and not reply_scan_has_more,
        "missing_parent_ids": missing,
        "truncated": bool(res["has_more"]) or reply_scan_has_more,
        "reply_scan_has_more": reply_scan_has_more,
        "search_window": {
            "start": fmt_ts(create_s - half),
            "end": fmt_ts(create_s + half),
        },
    }
    return bound_context(result)


def bound_context(ctx: dict, *, max_messages: int | None = None, max_bytes: int | None = None) -> dict:
    """Bound a live context before it reaches stdout or a durable event.

    Keep the grouping shape used by the Markdown renderer, but strip transport
    fields and clip text so a single unusually large message cannot turn a
    bounded search into an unbounded output.
    """
    max_messages = max_messages or config.CONTEXT_MAX_MESSAGES
    max_bytes = max_bytes or config.CONTEXT_MAX_BYTES
    used = 0
    count = 0
    clipped = False

    def clean_group(group: list[dict]) -> list[dict]:
        nonlocal used, count, clipped
        out: list[dict] = []
        for message in group:
            if count >= max_messages:
                clipped = True
                break
            clean = _clean_message(message)
            encoded = _encoded_size(clean)
            if encoded > max_bytes - used:
                # Preserve the message identity and metadata even when its
                # body is too large.  The body itself is explicitly marked.
                clean["text"] = "[正文超出上下文大小限制，已截断]"
                encoded = _encoded_size(clean)
                if encoded > max_bytes - used:
                    clipped = True
                    break
                clipped = True
            out.append(clean)
            used += encoded
            count += 1
        return out

    chain = clean_group(ctx.get("chain", []))
    target_group = clean_group([ctx.get("target", {})])
    replies = clean_group(ctx.get("replies", []))
    window = clean_group(ctx.get("window", []))
    return {
        **ctx,
        "target": target_group[0] if target_group else ctx.get("target", {}),
        "root": chain[0] if chain else ctx.get("root", {}),
        "chain": chain,
        "replies": replies,
        "window": window,
        "truncated": bool(ctx.get("truncated")) or clipped,
        "complete": bool(ctx.get("complete")) and not clipped,
    }


def compact_context(ctx: dict, *, max_messages: int | None = None, max_bytes: int | None = None) -> dict:
    """Remove raw transport data and cap a context bundle for event storage."""
    max_messages = max_messages or config.CONTEXT_MAX_MESSAGES
    max_bytes = max_bytes or config.CONTEXT_MAX_BYTES
    candidates: list[dict] = []
    seen: set[str] = set()
    for group in (ctx.get("chain", []), [ctx.get("target", {})], ctx.get("replies", []), ctx.get("window", [])):
        for message in group:
            mid = message.get("msg_id", "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            clean = _clean_message(message)
            candidates.append(clean)

    ordered: list[dict] = []
    used_bytes = 0
    for clean in candidates:
        if len(ordered) >= max_messages:
            break
        encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > max_bytes or (ordered and used_bytes + len(encoded) > max_bytes):
            break
        ordered.append(clean)
        used_bytes += len(encoded)
    truncated = bool(ctx.get("truncated")) or len(ordered) < len(candidates)
    complete = bool(ctx.get("complete")) and not truncated and not ctx.get("missing_parent_ids")
    return {
        "target_msg_id": ctx.get("target", {}).get("msg_id"),
        "root_msg_id": ctx.get("root", {}).get("msg_id"),
        "messages": ordered,
        "complete": complete,
        "truncated": truncated,
        "missing_parent_ids": ctx.get("missing_parent_ids", []),
        "search_window": ctx.get("search_window", {}),
    }


def render_markdown(ctx: dict) -> str:
    def block(title: str, msgs: list[dict]) -> list[str]:
        if not msgs:
            return []
        return [f"\n## {title}（{len(msgs)}）\n", messages.render_markdown(msgs)]

    t = ctx["target"]
    parts = [
        f"# 话题上下文：{t.get('chat_name') or t['chat_id']}",
        f"窗口：{ctx['search_window']['start']} ~ {ctx['search_window']['end']}"
        f"｜完整性：{'完整' if ctx['complete'] else '不完整'}",
    ]
    parts += block("话题主线（根 → 目标）", ctx["chain"] + [t])
    parts += block("话题内其他回复", ctx["replies"])
    parts += block("同群前后消息", ctx["window"])
    if ctx["missing_parent_ids"]:
        parts.append(f"\n> ⚠️ 缺父消息：{', '.join(ctx['missing_parent_ids'])}")
    if ctx["truncated"]:
        parts.append("\n> ⚠️ 消息搜索达到有界上限（has_more），上下文可能不全")
    return "\n".join(parts)
