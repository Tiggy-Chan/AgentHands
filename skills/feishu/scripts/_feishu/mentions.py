"""Historical @me lookup: server-side mention filter × local decode evidence."""

from __future__ import annotations

from . import gateway, messages
from .output import note


def classify_hit(msg: dict, my_id: str) -> dict:
    """Combine server hit + local text evidence into a confidence label."""
    server_hit = True  # caller only feeds us cmd=11021 mention hits
    text_hit = my_id in msg.get("mention_user_ids", [])
    if server_hit and text_hit:
        conf, sources = "confirmed", ["search_mention", "rich_text"]
    elif server_hit:
        conf, sources = "server_only", ["search_mention"]
    else:
        conf, sources = "text_only", ["rich_text"]
    return {**msg, "at_me": True, "at_me_confidence": conf, "at_me_sources": sources}


async def find_mentions(
    my_id: str,
    chat_ids: list[str],
    start_time: int = 0,
    end_time: int = 0,
    limit_per_chat: int = 15,
) -> dict:
    """Search @me in each chat → {'mentions': [...], 'chats_searched': N,
    'chats_failed': [...]}. A chat failing never silently drops the rest."""
    out: list[dict] = []
    failed: list[str] = []
    for cid in chat_ids:
        try:
            res = await gateway.search_msg_ids(
                cid, start_time, end_time, mention_user_id=my_id, limit=limit_per_chat
            )
            msgs = await gateway.fetch_messages(res["msg_ids"])
            out.extend(classify_hit(m, my_id) for m in msgs)
        except Exception as e:
            failed.append(cid)
            note(f"  ! 群 {cid} 的 @我 检索失败：{type(e).__name__}")
    out.sort(key=lambda m: (m.get("create_time") or 0, m["msg_id"]))
    await messages.resolve_names(out)
    return {
        "mentions": out,
        "chats_searched": len(chat_ids) - len(failed),
        "chats_failed": failed,
    }
