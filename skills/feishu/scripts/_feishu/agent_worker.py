"""Background agent service and watcher for the Feishu Command Agent Gateway.

Monitors incoming @me and P2P events from the event store and automatically
routes them to the stage-based dispatcher.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from . import agent_dispatcher, agent_rules, agent_session, config, event_store
from .output import note


def get_agent_status() -> dict[str, Any]:
    """Inspect current agent gateway configuration, active sessions, and status."""
    rules = agent_rules.load_rules()
    sessions = agent_session.list_sessions(limit=10)

    return {
        "enabled": rules.get("enabled", True),
        "rules_file": str(config.AGENT_RULES_FILE),
        "sessions_dir": str(config.AGENT_SESSIONS_DIR),
        "total_active_sessions": len(sessions),
        "recent_sessions": [
            {
                "session_key": s.get("session_key"),
                "from_name": s.get("from_name"),
                "updated_at": s.get("updated_at"),
                "today_requests": (s.get("daily_counts") or {}).get(time.strftime("%Y-%m-%d"), 0),
            }
            for s in sessions
        ],
        "allowed_chats": rules.get("allowed_chats", []),
        "blocked_chats": rules.get("blocked_chats", []),
        "blocked_topics_count": len(rules.get("blocked_topics", [])),
        "cooldown_seconds": (rules.get("rate_limit") or {}).get("cooldown_seconds", 30),
        "max_daily_per_user": (rules.get("rate_limit") or {}).get("max_daily_requests_per_user", 6),
    }


async def process_msg_id(msg_id: str, send_reply: bool = False) -> dict[str, Any]:
    """Process a specific historical or newly arrived message ID."""
    from . import message_resources, messages

    # Check local event pool first
    for ev in event_store.read_events(limit=200):
        if ev.get("msg_id") == msg_id:
            return await agent_dispatcher.dispatch_event(ev, send_reply=send_reply)

    msg_env = await message_resources.fetch_message(msg_id)
    event = {
        "kind": "message",
        "msg_id": msg_env.get("msg_id") or msg_id,
        "chat_id": msg_env.get("chat_id") or "",
        "chat_name": msg_env.get("chat_name") or "",
        "from_id": (msg_env.get("sender") or {}).get("id") or msg_env.get("from_id") or "",
        "from_name": (msg_env.get("sender") or {}).get("name") or "",
        "text": (msg_env.get("content") or {}).get("text") or msg_env.get("text") or "",
        "chat_type": msg_env.get("chat_type", 2),
        "at_me": True,  # If explicitly processing, assume targeted
        "thread_id": msg_env.get("thread_id") or msg_env.get("root_id") or msg_id,
    }
    return await agent_dispatcher.dispatch_event(event, send_reply=send_reply)


async def watch_events_loop(
    poll_interval: float = 2.0,
    once: bool = False,
    send_reply: bool = False,
) -> None:
    """Continuously poll the event pool for new @me and P2P message events."""
    config.ensure_home()
    note("启动飞书 Command Agent 监听循环...")
    processed_ids: set[str] = set()

    # Pre-populate already seen IDs so we don't spam historical mentions
    recent = event_store.read_events(mentions_only=True, limit=50)
    for ev in recent:
        mid = ev.get("msg_id")
        if mid:
            processed_ids.add(mid)

    while True:
        try:
            # Poll events from the last 5 minutes
            five_min_ago = int((time.time() - 300) * 1000)
            events = event_store.read_events(since_ms=five_min_ago, mentions_only=False, limit=50)

            for ev in events:
                mid = ev.get("msg_id")
                if not mid or mid in processed_ids:
                    continue
                processed_ids.add(mid)

                # Process if relevant (at_me or p2p chat_type==1)
                is_p2p = int(ev.get("chat_type") or 0) == 1
                is_at_me = bool(ev.get("at_me"))
                if is_p2p or is_at_me:
                    note(f"Agent 接收到目标事件: msg_id={mid} from={ev.get('from_name') or ev.get('from_id')}")
                    try:
                        res = await agent_dispatcher.dispatch_event(ev, send_reply=send_reply)
                        note(f"Agent 处理完成: status={res.get('status')} action={res.get('action')}")
                    except Exception as e:
                        note(f"Agent 处理异常: {e}")

            if once:
                break
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            note("Agent 监听循环终止。")
            break
        except Exception as e:
            note(f"Agent 监听循环异常: {e}")
            if once:
                break
            await asyncio.sleep(poll_interval)
