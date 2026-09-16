"""Typed message sending with draft fingerprints and one-shot gates."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

from . import auth, message_resources, stickers, transport
from .errors import EndpointNotAdapted, FeishuError, SendBlocked
from .output import note

from lark_bridge import sender


def fingerprint(
    chat_id: str,
    text: str,
    reply_id: str,
    at_user_ids: list[str],
    file_sha256: str = "",
    *,
    file_name: str = "",
    file_size: int = 0,
    file_mime: str = "",
    message_kind: str = "text",
    sticker_source: str = "",
    sticker_set_id: str = "",
    sticker_id: str = "",
    sticker_key_sha256: str = "",
) -> str:
    """Hash the complete outbound intent, not only visible text."""
    material = {
        "chat_id": chat_id,
        "reply_id": reply_id,
        "text": text,
        "at_user_ids": sorted(str(uid) for uid in at_user_ids),
        "file_sha256": file_sha256,
        "file_name": file_name,
        "file_size": int(file_size or 0),
        "file_mime": file_mime,
        "message_kind": message_kind,
        "sticker_source": sticker_source,
        "sticker_set_id": sticker_set_id,
        "sticker_id": sticker_id,
        "sticker_key_sha256": sticker_key_sha256,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def make_draft(
    chat_id: str,
    text: str,
    reply_id: str = "",
    at_user_ids: list[str] | None = None,
    file_path: str = "",
    file_name: str = "",
    file_size: int = 0,
    file_sha256: str = "",
    *,
    file_mime: str = "",
    message_kind: str = "text",
    sticker_source: str = "",
    sticker_set_id: str = "",
    sticker_id: str = "",
    sticker_key_sha256: str = "",
) -> dict:
    at = list(at_user_ids or [])
    draft = {
        "status": "draft_not_sent",
        "sent": False,
        "chat_id": chat_id,
        "reply_to": reply_id or None,
        "at_user_ids": at,
        "text": text,
        "message_kind": message_kind,
        "fingerprint": fingerprint(
            chat_id,
            text,
            reply_id,
            at,
            file_sha256,
            file_name=file_name,
            file_size=file_size,
            file_mime=file_mime,
            message_kind=message_kind,
            sticker_source=sticker_source,
            sticker_set_id=sticker_set_id,
            sticker_id=sticker_id,
            sticker_key_sha256=sticker_key_sha256,
        ),
    }
    if file_path:
        draft["file_path"] = file_path
        draft["file_name"] = file_name
        draft["file_size"] = file_size
        draft["file_sha256"] = file_sha256
        if file_mime:
            draft["file_mime"] = file_mime
    if sticker_source:
        draft["sticker_source"] = sticker_source
    if sticker_set_id:
        draft["sticker_set_id"] = sticker_set_id
    if sticker_id:
        draft["sticker_id"] = sticker_id
    if sticker_key_sha256:
        draft["sticker_key_sha256"] = sticker_key_sha256
    return draft


def _message_type_code(message: dict[str, Any]) -> int:
    value = message.get("type", 0)
    if isinstance(value, dict):
        value = value.get("code", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def _sticker_from_message(msg_id: str) -> tuple[dict, dict[str, Any]]:
    message = await message_resources.fetch_message(msg_id)
    if _message_type_code(message) != 10:
        raise EndpointNotAdapted("源消息不是 sticker（type=10），未发送")
    raw_content = message.get("_raw_content")
    if not isinstance(raw_content, (bytes, bytearray)) or not raw_content:
        raise EndpointNotAdapted("源 sticker 没有可用的私有内容，未发送")
    spec = stickers.parse_sticker_content(bytes(raw_content))
    if not stickers.has_sendable_identity(spec):
        raise EndpointNotAdapted(
            "源 sticker 的 set/id 或 file key 尚未适配，未发送",
            hint="先用受限 probe 捕获当前租户的 sticker 发送 payload",
        )
    return message, spec


def _sticker_from_ids(sticker_set_id: str, sticker_id: str) -> dict[str, Any]:
    if not sticker_set_id or not sticker_id:
        raise ValueError("sticker 需要同时提供 --sticker-set-id 和 --sticker-id")
    sticker_set_id = stickers.validate_sticker_id(sticker_set_id, "sticker set id")
    sticker_id = stickers.validate_sticker_id(sticker_id, "sticker id")
    return {
        "file_key": "",
        "sticker_set_id": sticker_set_id,
        "sticker_id": sticker_id,
        "width": 0,
        "height": 0,
        "region": "",
        "raw_sha256": "",
        "raw_bytes": 0,
    }


async def send(
    chat_id: str,
    text: str = "",
    reply_id: str = "",
    at_user_ids: list[str] | None = None,
    file_path: str = "",
    confirm: str = "",
    direct: bool = False,
    *,
    sticker_from_message: str = "",
    sticker_set_id: str = "",
    sticker_id: str = "",
) -> dict:
    """Create a draft or send one typed message exactly once."""
    if sticker_from_message and (sticker_set_id or sticker_id):
        raise ValueError("--sticker-from-message 与 --sticker-set-id/--sticker-id 不能同时使用")
    if sticker_set_id and not sticker_id or sticker_id and not sticker_set_id:
        raise ValueError("--sticker-set-id 与 --sticker-id 必须成对使用")
    if file_path and (sticker_from_message or sticker_set_id or sticker_id):
        raise ValueError("文件消息不能同时携带 sticker")
    if file_path and text.strip():
        raise ValueError("原生文件消息暂不支持正文说明；请单独发送文本")

    file_bytes: bytes | None = None
    file_sha256 = ""
    file_name = ""
    file_size = 0
    file_mime = ""
    resolved_file_path = ""
    if file_path:
        p = Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise FeishuError(f"文件不存在: {file_path}")
        file_bytes = p.read_bytes()
        file_size = len(file_bytes)
        file_name = p.name
        file_mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        resolved_file_path = str(p)
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()

    sticker_spec: dict[str, Any] = {}
    prepared_me: dict[str, Any] | None = None
    if sticker_from_message:
        # Source-message mode needs a read session even when the final
        # invocation is draft-only; otherwise cmd=8 has no bound cookies.
        prepared_me = await auth.prepare()
        _source_message, sticker_spec = await _sticker_from_message(sticker_from_message)
    elif sticker_set_id or sticker_id:
        sticker_spec = _sticker_from_ids(sticker_set_id, sticker_id)

    has_sticker = bool(sticker_from_message or sticker_set_id or sticker_id)
    if not text.strip() and file_bytes is None and not has_sticker:
        raise ValueError("正文、文件和 sticker 均为空，拒绝")

    message_kind = "sticker" if has_sticker else ("file" if file_bytes is not None else "text")
    sticker_material = stickers.sticker_fingerprint_material(sticker_spec) if has_sticker else {}
    draft = make_draft(
        chat_id,
        text,
        reply_id,
        at_user_ids,
        file_path=resolved_file_path,
        file_name=file_name,
        file_size=file_size,
        file_sha256=file_sha256,
        file_mime=file_mime,
        message_kind=message_kind,
        sticker_source=sticker_from_message,
        sticker_set_id=str(sticker_material.get("sticker_set_id") or ""),
        sticker_id=str(sticker_material.get("sticker_id") or ""),
        sticker_key_sha256=str(sticker_material.get("file_key_sha256") or ""),
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

    # The env var has to be on the very command that writes: whoever approves that command
    # sees it, and it does not outlive the command. --direct skips only the fingerprint check.
    if os.environ.get("FEISHU_ALLOW_SEND") != "1":
        raise SendBlocked(
            "环境变量 FEISHU_ALLOW_SEND=1 未设置，拒绝发送",
            hint="--direct 只免指纹核对；这道闸门没有任何命令行旗标能绕过",
        )
    if file_bytes is not None and os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked(
            "原生文件发送需要同时允许云盘上传：FEISHU_ALLOW_WRITE=1",
            hint="文件上传和消息发送属于同一次受控操作，缺少 FEISHU_ALLOW_WRITE=1 时不上传",
        )

    me = prepared_me or await auth.prepare()
    data = auth.load_cookies()
    transport.bind_session(
        auth.cookie_header(data["cookies"]),
        data["cookies"].get("swp_csrf_token", ""),
        data.get("domain") or "",
    )

    upload_result: dict[str, Any] = {}
    if file_bytes is not None:
        from lark_bridge import drive

        note(f"正在上传文件到飞书云盘以构造原生文件消息: {file_name} ({file_size} 字节)...")
        async with transport.allow_write_operation(direct=direct):
            upload_result = await drive.upload_file(
                cookie=auth.cookie_header(data["cookies"]),
                cookies=data["cookies"],
                folder_token="",
                file_name=file_name,
                file_content=file_bytes,
                domain=data.get("domain") or "",
            ) or {}
        file_key = str(upload_result.get("file_key") or upload_result.get("file_token") or "")
        if not file_key:
            raise FeishuError(f"文件上传失败: {file_name}")

    transport.arm_direct_send_once() if direct else transport.arm_send_once()
    if message_kind == "text":
        send_result = await sender.send_message_result(
            auth.cookie_header(data["cookies"]),
            chat_id,
            text,
            reply_id=reply_id,
            at_user_ids=at_user_ids or [],
            domain=data.get("domain") or "",
        )
    elif message_kind == "file":
        send_result = await sender.send_file_message_result(
            auth.cookie_header(data["cookies"]),
            chat_id,
            file_key,
            file_name,
            file_mime,
            file_size,
            reply_id=reply_id,
            domain=data.get("domain") or "",
        )
    else:
        send_result = await sender.send_sticker_message_result(
            auth.cookie_header(data["cookies"]),
            chat_id,
            file_key=str(sticker_spec.get("file_key") or ""),
            sticker_set_id=str(sticker_spec.get("sticker_set_id") or ""),
            sticker_id=str(sticker_spec.get("sticker_id") or ""),
            reply_id=reply_id,
            domain=data.get("domain") or "",
        )

    result = {**draft, **send_result}
    result["sent"] = result.get("status") == "accepted"
    if result.get("status") == "accepted":
        result["verification"] = "gateway_ack"
        result["verified"] = False
        result["verification_source"] = "gateway_ack"
        result["verification_detail"] = "server_message_id_missing"
        if result.get("server_msg_id"):
            try:
                observed = await message_resources.fetch_message(str(result["server_msg_id"]))
                observed_type = _message_type_code(observed)
                expected_type = {"text": 4, "file": 3, "sticker": 10}[message_kind]
                same_chat = str(observed.get("chat_id") or "") == str(chat_id)
                same_sender = str(observed.get("from_id") or "") == str(me.get("user_id") or "")
                if observed_type == expected_type and same_chat and same_sender:
                    result["verified"] = True
                    result["verification"] = "message_fetch"
                    result["verification_source"] = "gateway_cmd8"
                    result["verification_detail"] = "message_type_chat_sender_match"
                else:
                    result["verification_source"] = "gateway_cmd8"
                    result["verification_detail"] = "message_type_chat_or_sender_mismatch"
            except Exception as exc:
                result["verification_source"] = "gateway_cmd8"
                result["verification_detail"] = f"message_fetch_failed_{type(exc).__name__}"
    elif result.get("status") == "unverified":
        result["verification"] = "not_confirmed_no_retry"
        result["verified"] = False
        result["verification_source"] = "none"
        result["verification_detail"] = "no_gateway_ack"
    else:
        result["verification"] = "gateway_rejected"
        result["verified"] = False
        result["verification_source"] = "gateway"
        result["verification_detail"] = str(result.get("detail") or "rejected")[:256]
    if file_bytes is not None:
        result["upload_completed"] = True
        result["uploaded_file_name"] = file_name
    return result
