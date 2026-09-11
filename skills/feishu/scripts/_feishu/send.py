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


def fingerprint(
    chat_id: str,
    text: str,
    reply_id: str,
    at_user_ids: list[str],
    file_sha256: str = "",
) -> str:
    h = hashlib.sha256()
    h.update(chat_id.encode())
    h.update(b"|")
    h.update(reply_id.encode())
    h.update(b"|")
    h.update(text.encode())
    h.update(b"|")
    h.update(",".join(sorted(at_user_ids)).encode())
    if file_sha256:
        h.update(b"|")
        h.update(file_sha256.encode())
    return h.hexdigest()[:16]


def make_draft(
    chat_id: str,
    text: str,
    reply_id: str = "",
    at_user_ids: list[str] | None = None,
    file_path: str = "",
    file_name: str = "",
    file_size: int = 0,
    file_sha256: str = "",
) -> dict:
    at = at_user_ids or []
    draft = {
        "status": "draft_not_sent",
        "chat_id": chat_id,
        "reply_to": reply_id or None,
        "at_user_ids": at,
        "text": text,
        "fingerprint": fingerprint(chat_id, text, reply_id, at, file_sha256),
    }
    if file_path:
        draft["file_path"] = file_path
        draft["file_name"] = file_name
        draft["file_size"] = file_size
        draft["file_sha256"] = file_sha256
    return draft


async def send(
    chat_id: str,
    text: str = "",
    reply_id: str = "",
    at_user_ids: list[str] | None = None,
    file_path: str = "",
    confirm: str = "",
    direct: bool = False,
) -> dict:
    file_bytes = None
    file_sha256 = ""
    file_name = ""
    file_size = 0
    if file_path:
        from pathlib import Path
        from .errors import FeishuError

        p = Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise FeishuError(f"文件不存在: {file_path}")
        file_bytes = p.read_bytes()
        file_size = len(file_bytes)
        file_name = p.name
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()

    draft = make_draft(
        chat_id,
        text,
        reply_id,
        at_user_ids,
        file_path=str(p) if file_path else "",
        file_name=file_name,
        file_size=file_size,
        file_sha256=file_sha256,
    )
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

    final_text = text
    if file_bytes is not None:
        from lark_bridge import drive
        from .errors import FeishuError

        note(f"正在上传文件到飞书云盘: {file_name} ({file_size} 字节)...")
        async with transport.allow_write_operation(direct=direct):
            upload_res = await drive.upload_file(
                cookie=auth.cookie_header(data["cookies"]),
                cookies=data["cookies"],
                folder_token="",
                file_name=file_name,
                file_content=file_bytes,
                domain=data.get("domain") or "",
            )
        if not upload_res or not upload_res.get("url"):
            raise FeishuError(f"文件上传失败: {file_name}")
        file_url = upload_res["url"]
        file_desc = f"📎 【文件】{file_name}\n{file_url}"
        if final_text and final_text.strip():
            final_text = f"{final_text.strip()}\n\n{file_desc}"
        else:
            final_text = file_desc
        draft["file_url"] = file_url

    (transport.arm_direct_send_once() if direct else transport.arm_send_once())
    ok = await sender.send_message(
        auth.cookie_header(data["cookies"]),
        chat_id,
        final_text,
        reply_id=reply_id,
        at_user_ids=at_user_ids or [],
        domain=data.get("domain") or "",
    )
    if not ok:
        raise SendBlocked("网关拒绝了发送（或网络失败）——没有自动重试，请人工确认后再发")
    return {**draft, "status": "sent", "delivered_text": final_text}
