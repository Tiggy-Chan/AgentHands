"""Send path: draft + fingerprint, double gate, no auto-retry.

Default invocation produces a draft and exits 3 (SendBlocked) — no network
request happens at all. An actual send requires ALL of:

1. --confirm-send <fingerprint> matching this exact draft
2. FEISHU_ALLOW_SEND=1 in the environment
3. one-shot arming via transport.arm_send_once() (disarmed after one use)

The vendor sender additionally calls _http.require_send_allowed(), so even
a future code path that forgets step 3 is still stopped.
"""

from __future__ import annotations

import hashlib
import json

from . import auth, transport
from .errors import SendBlocked
from .output import emit_json, note

from lark_bridge import sender


def fingerprint(chat_id: str, text: str, reply_id: str, at_user_ids: list[str]) -> str:
    h = hashlib.sha256()
    h.update(chat_id.encode())
    h.update(b"|")
    h.update(reply_id.encode())
    h.update(b"|")
    h.update(text.encode())
    h.update(b"|")
    h.update(",".join(sorted(at_user_ids)).encode())
    return h.hexdigest()[:16]


def make_draft(chat_id: str, text: str, reply_id: str = "", at_user_ids: list[str] | None = None) -> dict:
    at = at_user_ids or []
    return {
        "status": "draft_not_sent",
        "chat_id": chat_id,
        "reply_to": reply_id or None,
        "at_user_ids": at,
        "text": text,
        "fingerprint": fingerprint(chat_id, text, reply_id, at),
    }


async def send(
    chat_id: str,
    text: str,
    reply_id: str = "",
    at_user_ids: list[str] | None = None,
    confirm: str = "",
    direct: bool = False,
) -> dict:
    draft = make_draft(chat_id, text, reply_id, at_user_ids)
    if direct:
        note("直接发送模式：本次用户已明确授权；仅发送这一条，不自动重试。")
    elif not confirm:
        note("未发送。确认内容无误后，把指纹填进 --confirm-send 并设 FEISHU_ALLOW_SEND=1 再发。")
        raise SendBlocked("draft only", hint=json.dumps(draft, ensure_ascii=False))
    if not direct and confirm != draft["fingerprint"]:
        raise SendBlocked(
            "指纹不匹配，拒绝发送",
            hint=f"这条消息的指纹是 {draft['fingerprint']}；内容任何改动都会变",
        )
    import os

    if not direct and os.environ.get("FEISHU_ALLOW_SEND") != "1":
        raise SendBlocked(
            "指纹正确，但环境变量 FEISHU_ALLOW_SEND=1 未设置，拒绝发送",
            hint="双重闸门：FEISHU_ALLOW_SEND=1 + 本指纹，缺一不可",
        )
    # Verify the live session before a direct mutation.  This also refreshes
    # CSRF and binds the exact cookie set used by the request.
    await auth.prepare()
    data = auth.load_cookies()
    transport.bind_session(
        auth.cookie_header(data["cookies"]),
        data["cookies"].get("swp_csrf_token", ""),
        data.get("domain") or "",
    )
    (transport.arm_direct_send_once() if direct else transport.arm_send_once())
    ok = await sender.send_message(
        auth.cookie_header(data["cookies"]),
        chat_id,
        text,
        reply_id=reply_id,
        at_user_ids=at_user_ids or [],
        domain=data.get("domain") or "",
    )
    if not ok:
        raise SendBlocked("网关拒绝了发送（或网络失败）——没有自动重试，请人工确认后再发")
    return {**draft, "status": "sent"}
