"""Event dispatcher and stage-based decision router for the Feishu Command Agent.

Routes incoming mention events:
1. Access control (allowed_chats, blocked_chats, allowed_users, blocked_users)
2. Boundary defense (blocked_topics -> boundary_message)
3. Stage 1: Zero-token menu prompt when mentioned without a command
4. Stage 2: Directive execution (/qa, /diag, /doc, /reset, /help, /sponsor)
5. Anti-abuse rate limiting & sponsorship prompt
"""

from __future__ import annotations

import os
from typing import Any

from . import agent_rules, agent_runner, agent_session, config
from .output import note


async def dispatch_event(
    event: dict[str, Any],
    send_reply: bool = False,
    override_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process a single event through the Command Agent rules and directives.

    Args:
        event: Message event dictionary.
        send_reply: If True, automatically send the generated reply via Feishu.
        override_rules: Optional rules dictionary (useful for unit testing).

    Returns:
        Structured result dict with status, action, reply text, and metadata.
    """
    # 1. Relevance check
    if event.get("kind") != "message":
        return {"status": "ignored_not_message", "handled": False}

    is_p2p = int(event.get("chat_type") or 0) == 1
    is_at_me = bool(event.get("at_me"))
    if not (is_p2p or is_at_me):
        return {"status": "ignored_not_for_me", "handled": False}

    # 2. Extract fields
    msg_id = str(event.get("msg_id") or "")
    chat_id = str(event.get("chat_id") or "")
    chat_name = str(event.get("chat_name") or "")
    from_id = str(
        (event.get("sender") or {}).get("id")
        or event.get("from_id")
        or ""
    )
    from_name = str(
        (event.get("sender") or {}).get("name")
        or event.get("from_name")
        or ""
    )
    if not from_name and from_id:
        try:
            from . import messages
            names = messages._load_names()
            from_name = names.get("users", {}).get(from_id, "")
        except Exception:
            pass

    raw_text = str(event.get("text") or "")

    # Feishu sets thread_id == msg_id for regular chat messages without a topic thread.
    # Group sessions must anchor on (chat_id, from_id) so the same user keeps their multi-turn context.
    # If there is an explicit reply root_id / thread_id that differs from msg_id, group by that thread.
    root_id = str(event.get("root_id") or "")
    raw_thread_id = str(event.get("thread_id") or "")
    if root_id and root_id != msg_id:
        thread_id = f"thread_{root_id}"
    elif raw_thread_id and raw_thread_id != msg_id:
        thread_id = f"thread_{raw_thread_id}"
    else:
        thread_id = ""

    # 3. Rules & Boundaries
    rules = override_rules or agent_rules.load_rules()
    if not rules.get("enabled", True):
        return {"status": "disabled", "handled": False}

    if not agent_rules.is_chat_allowed(chat_id, chat_name=chat_name, rules=rules):
        return {"status": "blocked_chat", "handled": False}

    if not agent_rules.is_user_allowed(from_id, user_name=from_name, rules=rules):
        return {"status": "blocked_user", "handled": False}

    topic_ok, boundary_msg = agent_rules.check_topic_boundary(raw_text, rules=rules)
    if not topic_ok:
        res = {
            "status": "blocked_topic",
            "action": "topic_boundary",
            "handled": True,
            "reply": boundary_msg,
            "msg_id": msg_id,
            "chat_id": chat_id,
        }
        if send_reply:
            await _try_send(chat_id, msg_id, from_id, boundary_msg)
        return res

    # 4. Parse Command
    cmd, arg = agent_runner.parse_command(raw_text)
    session = agent_session.get_or_create_session(chat_id, from_id=from_id, from_name=from_name, thread_id=thread_id)

    reply_text = ""
    action = ""

    # Stage 1: Bare mention without command
    if cmd is None:
        assistant_name = rules.get("assistant_name") or "技术助手"
        reply_text = agent_runner.get_stage1_menu(user_name=from_name, assistant_name=assistant_name)
        action = "stage1_menu"

    # Command: /help
    elif cmd in ("help", "h"):
        reply_text = agent_runner.handle_help(session, rules)
        action = "help"

    # Command: /sponsor
    elif cmd in ("sponsor", "token", "pay", "coffee"):
        reply_text = agent_runner.handle_sponsor(session, rules)
        action = "sponsor"

    # Command: /reset
    elif cmd in ("reset", "new", "clear"):
        reply_text = agent_runner.handle_reset(session)
        action = "reset"

    # Command: /doc
    elif cmd in ("doc", "docs", "search"):
        quota_ok, quota_msg = agent_session.check_rate_limit(session, rules)
        if not quota_ok:
            reply_text = quota_msg
            action = "rate_limited"
        else:
            agent_session.record_request(session)
            reply_text = await agent_runner.handle_doc(arg, rules=rules)
            action = "doc"

    # Command: /diag
    elif cmd in ("diag", "check", "inspect"):
        quota_ok, quota_msg = agent_session.check_rate_limit(session, rules)
        if not quota_ok:
            reply_text = quota_msg
            action = "rate_limited"
        else:
            agent_session.record_request(session)
            reply_text = await agent_runner.handle_diag(arg)
            action = "diag"

    # Command: /qa
    elif cmd in ("qa", "ask", "q"):
        quota_ok, quota_msg = agent_session.check_rate_limit(session, rules)
        if not quota_ok:
            reply_text = quota_msg
            action = "rate_limited"
        else:
            agent_session.record_request(session)
            reply_text = await agent_runner.handle_qa(arg, session)
            agent_session.append_turn(session, arg, reply_text)
            action = "qa"

    # Unknown Command
    else:
        reply_text = f"❓ 未识别的指令: /{cmd}。请在回复中输入 /help 查看可用指令列表。"
        action = "unknown_command"

    result = {
        "status": "success",
        "action": action,
        "handled": True,
        "reply": reply_text,
        "msg_id": msg_id,
        "chat_id": chat_id,
        "from_id": from_id,
        "session_key": session.get("session_key", ""),
        "claude_session_id": session.get("claude_session_id", ""),
    }

    if send_reply and reply_text:
        send_result = await _try_send(chat_id, msg_id, from_id, reply_text)
        result["send_result"] = send_result

    return result


async def _try_send(chat_id: str, reply_id: str, at_user_id: str, text: str) -> dict[str, Any]:
    """Helper to send reply under direct authorization."""
    from . import send, transport

    try:
        transport.configure()
        return await send.send(
            chat_id=chat_id,
            text=text,
            reply_id=reply_id,
            at_user_ids=[at_user_id] if at_user_id else [],
            direct=True,
        )
    except Exception as exc:
        note(f"Agent reply delivery failed: {exc}")
        return {"status": "send_failed", "error": str(exc)}
