"""Session management and persistent conversation memory for the Feishu Command Agent.

Maps Feishu chat threads / users to isolated, resumable sessions with independent
Claude session IDs, conversation history, and anti-abuse quota tracking.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

_MAX_HISTORY_TURNS = 20


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


def sanitize_key(raw: str) -> str:
    """Sanitize key for safe filesystem use."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", raw)[:128]


def get_session_key(chat_id: str, thread_id: str = "", from_id: str = "") -> str:
    """Derive a stable session key from chat_id, thread_id, or from_id."""
    anchor = thread_id or from_id or "direct"
    return sanitize_key(f"{chat_id}__{anchor}")


def _session_path(session_key: str) -> Path:
    config.ensure_home()
    return config.AGENT_SESSIONS_DIR / f"{session_key}.json"


def get_or_create_session(
    chat_id: str,
    from_id: str,
    from_name: str = "",
    thread_id: str = "",
) -> dict[str, Any]:
    """Retrieve an existing session or initialize a fresh one."""
    config.ensure_home()
    key = get_session_key(chat_id, thread_id=thread_id, from_id=from_id)
    path = _session_path(key)

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Ensure backwards-compatible keys
                data.setdefault("session_key", key)
                data.setdefault("claude_session_id", str(uuid.uuid4()))
                data.setdefault("history", [])
                data.setdefault("daily_counts", {})
                data.setdefault("last_request_time", 0.0)
                if from_name:
                    data["from_name"] = from_name
                return data
        except (OSError, ValueError):
            pass

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    session: dict[str, Any] = {
        "session_key": key,
        "session_id": str(uuid.uuid4()),
        "claude_session_id": str(uuid.uuid4()),
        "chat_id": chat_id,
        "from_id": from_id,
        "from_name": from_name,
        "thread_id": thread_id,
        "created_at": now_iso,
        "updated_at": now_iso,
        "history": [],
        "daily_counts": {},
        "last_request_time": 0.0,
    }
    save_session(session)
    return session


def save_session(session: dict[str, Any]) -> None:
    """Save session state atomically with 0600 permissions."""
    config.ensure_home()
    key = session.get("session_key") or "default"
    path = _session_path(key)
    session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def reset_session(chat_id: str, from_id: str = "", thread_id: str = "") -> dict[str, Any]:
    """Wipe session history and allocate a fresh Claude session ID."""
    config.ensure_home()
    key = get_session_key(chat_id, thread_id=thread_id, from_id=from_id)
    path = _session_path(key)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    session: dict[str, Any] = {
        "session_key": key,
        "session_id": str(uuid.uuid4()),
        "claude_session_id": str(uuid.uuid4()),
        "chat_id": chat_id,
        "from_id": from_id,
        "thread_id": thread_id,
        "created_at": now_iso,
        "updated_at": now_iso,
        "history": [],
        "daily_counts": {},
        "last_request_time": 0.0,
    }
    save_session(session)
    return session


def check_rate_limit(
    session: dict[str, Any],
    rules: dict[str, Any],
) -> tuple[bool, str]:
    """Check anti-abuse rate limits and daily quota.

    Returns:
        (allowed, reason_or_prompt)
    """
    rl = rules.get("rate_limit") or {}
    cooldown = float(rl.get("cooldown_seconds") or 30)
    max_daily = int(rl.get("max_daily_requests_per_user") or 6)

    now = time.time()
    last_time = float(session.get("last_request_time") or 0.0)

    # Cooldown check
    if last_time > 0 and (now - last_time) < cooldown:
        remaining = int(cooldown - (now - last_time))
        return False, f"⚠️ 指令交互过于频繁，请稍候 {remaining} 秒后再试。"

    # Daily quota check
    today = _today_str()
    counts = session.get("daily_counts") or {}
    today_count = int(counts.get(today, 0))

    if today_count >= max_daily:
        sponsor = rules.get("sponsor") or {}
        notice_tmpl = sponsor.get("notice") or (
            "💡 今日技术支援免费额度已用完（{used}/{max} 次）。\n"
            "若属业务极其紧急的阻塞，请扫码投喂 Token 咖啡提频，或联系负责人加急处理。"
        )
        msg = notice_tmpl.format(used=today_count, max=max_daily)
        qr_hint = sponsor.get("qr_code_hint") or ""
        if qr_hint:
            msg += f"\n👉 收款码：{qr_hint}"
        return False, msg

    return True, ""


def record_request(session: dict[str, Any]) -> None:
    """Record an executed request against quota and cooldown."""
    today = _today_str()
    counts = session.setdefault("daily_counts", {})
    counts[today] = int(counts.get(today, 0)) + 1
    session["last_request_time"] = time.time()
    save_session(session)


def append_turn(session: dict[str, Any], question: str, answer: str) -> None:
    """Append a Q&A turn to session history, pruning to MAX_HISTORY_TURNS."""
    history = session.setdefault("history", [])
    history.append({
        "role": "user",
        "content": question,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    history.append({
        "role": "assistant",
        "content": answer,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    if len(history) > _MAX_HISTORY_TURNS * 2:
        session["history"] = history[-_MAX_HISTORY_TURNS * 2:]
    save_session(session)


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    """List recently updated sessions."""
    config.ensure_home()
    if not config.AGENT_SESSIONS_DIR.is_dir():
        return []

    sessions = []
    for p in config.AGENT_SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                sessions.append(data)
        except (OSError, ValueError):
            continue

    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return sessions[:limit]
