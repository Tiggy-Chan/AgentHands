"""Send typed messages through Feishu's internal message gateway.

The public Feishu skill keeps confirmation and one-shot arming in
``_feishu.send``/``_feishu.transport``.  This module owns the vendor wire
builders and the single non-retrying HTTP send path.  It deliberately returns
a structured result so an HTTP 200 cannot be mistaken for a gateway ack.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

from lark_bridge import _http
from lark_bridge.proto import proto_pb2 as pb
from lark_bridge._urls import INTERNAL_API, USER_AGENT

logger = logging.getLogger(__name__)


def _new_cid(prefix: str = "message") -> str:
    return hashlib.md5(f"{prefix}-{time.time_ns()}-{id(object())}".encode()).hexdigest()[:10]


def _new_request(
    msg_type: int,
    chat_id: str,
    reply_id: str = "",
    *,
    cid: str = "",
) -> tuple[pb.PutMessageRequest, str]:
    msg = pb.PutMessageRequest()
    msg.type = int(msg_type)
    msg.chatId = chat_id
    message_cid = cid or _new_cid("message")
    msg.cid = message_cid
    msg.isNotified = True
    msg.version = 1
    if reply_id:
        msg.parentId = reply_id
        msg.rootId = reply_id
    return msg, message_cid


def _build_text_request(
    text: str,
    chat_id: str,
    reply_id: str = "",
    at_user_ids: list[str] | None = None,
) -> tuple[bytes, str]:
    """Build the inner ``PutMessageRequest`` for one text message."""
    msg, message_cid = _new_request(4, chat_id, reply_id)
    full_text = ""
    element_ids: list[str] = []

    for uid in at_user_ids or []:
        at_eid = hashlib.md5(f"a{uid}{time.time_ns()}".encode()).hexdigest()[:10]
        at_tp = pb.TextProperty()
        at_tp.content = uid
        at_tp.i18nKey = f"@{uid}"
        msg.content.richText.elements.dictionary[at_eid].tag = 5
        msg.content.richText.elements.dictionary[at_eid].property = at_tp.SerializeToString()
        element_ids.append(at_eid)
        full_text += f"@{uid} "

    text_eid = hashlib.md5(f"t{time.time_ns()}".encode()).hexdigest()[:10]
    tp = pb.TextProperty()
    tp.content = text
    msg.content.richText.elements.dictionary[text_eid].tag = 1
    msg.content.richText.elements.dictionary[text_eid].property = tp.SerializeToString()
    element_ids.append(text_eid)
    full_text += text

    msg.content.richText.innerText = full_text
    msg.content.richText.elementIds.extend(element_ids)
    if at_user_ids:
        for eid in element_ids[:-1]:
            msg.content.richText.atIds.append(eid)
    return msg.SerializeToString(), message_cid


def _build_file_request(
    file_key: str,
    file_name: str,
    file_mime: str,
    file_size: int,
    chat_id: str,
    reply_id: str = "",
) -> tuple[bytes, str]:
    """Build the inner ``PutMessageRequest`` for a native type-3 file."""
    if not file_key or not file_name:
        raise ValueError("原生文件消息需要 file_key 和 file_name")
    msg, message_cid = _new_request(3, chat_id, reply_id)
    msg.content.fileKey = file_key
    msg.content.fileName = file_name
    msg.content.fileMime = file_mime or "application/octet-stream"
    msg.content.fileSize = max(0, int(file_size))
    return msg.SerializeToString(), message_cid


def _build_sticker_request(
    *,
    file_key: str = "",
    sticker_set_id: str = "",
    sticker_id: str = "",
    chat_id: str,
    reply_id: str = "",
) -> tuple[bytes, str]:
    """Build the inner ``PutMessageRequest`` for a type-10 sticker.

    The current tenant exposes either a sticker file key, a stable
    set/id pair, or both.  The raw incoming content is never copied into the
    outbound request; callers must first normalize it into these fields.
    """
    if not file_key and not (sticker_set_id and sticker_id):
        raise ValueError("sticker 需要 file_key 或 sticker_set_id + sticker_id")
    msg, message_cid = _new_request(10, chat_id, reply_id)
    if file_key:
        msg.content.fileKey = file_key
    if sticker_set_id:
        msg.content.stickerSetId = sticker_set_id
    if sticker_id:
        msg.content.stickerId = sticker_id
    return msg.SerializeToString(), message_cid


def _packet(request_payload: bytes, request_id: str) -> bytes:
    packet = pb.Packet()
    packet.payloadType = 1
    packet.cmd = 5
    packet.cid = request_id
    packet.payload = request_payload
    return packet.SerializeToString()


def _build_send_packet(
    text: str,
    chat_id: str,
    reply_id: str = "",
    at_user_ids: list[str] | None = None,
) -> tuple[bytes, str]:
    """Backward-compatible outer packet builder for text callers."""
    payload, _message_cid = _build_text_request(text, chat_id, reply_id, at_user_ids)
    request_id = _new_cid("request")
    return _packet(payload, request_id), request_id


def _extract_server_message_id(payload: bytes) -> str:
    """Best-effort extraction of a 19-digit message id without exposing payload."""
    matches = re.findall(rb"(?<!\d)7\d{18}(?!\d)", bytes(payload or b""))
    return matches[0].decode("ascii") if matches else ""


def _parse_send_reply(data: bytes) -> dict[str, Any]:
    """Classify a gateway response without returning raw response bytes."""
    response = bytes(data or b"")
    result: dict[str, Any] = {
        "response_bytes": len(response),
        "response_sha256": hashlib.sha256(response).hexdigest(),
        "server_msg_id": "",
    }
    try:
        packet = pb.Packet()
        packet.ParseFromString(response)
    except Exception:
        result.update({"status": "unverified", "detail": "gateway_response_unparsed"})
        return result

    payload_type = int(packet.payloadType)
    packet_status = int(packet.status)
    result["response_payload_type"] = payload_type
    result["response_cmd"] = int(packet.cmd)
    result["response_status"] = packet_status
    result["server_msg_id"] = _extract_server_message_id(packet.payload)

    if packet_status:
        result.update({"status": "rejected", "detail": f"gateway_status_{packet_status}"})
    elif payload_type == 1:
        result.update({"status": "accepted", "detail": "gateway_ack"})
    elif payload_type == 5:
        result.update({"status": "rejected", "detail": "gateway_rejected"})
    else:
        # A type-0/unknown response may mean a server-side error or an
        # accepted request whose acknowledgement is not understood.  Do not
        # claim delivery; callers can surface the unverified state and avoid
        # a duplicate retry.
        lowered = packet.payload[:512].lower()
        if any(token in lowered for token in (b"error", b"failed", b"reject", b"invalid")):
            result.update({"status": "rejected", "detail": "gateway_error_payload"})
        else:
            result.update({"status": "unverified", "detail": f"gateway_payload_type_{payload_type}"})
    return result


async def send_payload_result(cookie: str, request_payload: bytes, domain: str = "") -> dict[str, Any]:
    """Send one typed message, exactly once, and classify its response."""
    # The caller arms the one-shot gate immediately before this function.
    # Keep this call outside the network try block so a gate failure propagates.
    _http.require_send_allowed()
    request_id = _new_cid("request")
    data = _packet(request_payload, request_id)
    gateway_url = f"{INTERNAL_API}/im/gateway/"
    headers = {
        "Cookie": cookie,
        "content-type": "application/x-protobuf",
        "user-agent": USER_AGENT,
        "x-command": "5",
        "x-command-version": "5.7.0",
        "x-request-id": request_id,
        "x-appid": "161471",
        "x-source": "web",
        "x-web-version": "3.9.32",
        "x-lgw-os-type": "1",
        "x-lgw-terminal-type": "2",
        "origin": f"https://{domain}",
        "referer": f"https://{domain}/",
    }
    try:
        # There is deliberately no retry around cmd=5: a lost response can
        # follow a successful server-side write.
        async with _http.make_client(timeout=10) as client:
            response = await client.post(gateway_url, headers=headers, content=data)
        if response.status_code != 200:
            return {
                "status": "rejected",
                "detail": f"http_status_{response.status_code}",
                "http_status": response.status_code,
                "request_id": request_id,
                "sent": False,
            }
        result = _parse_send_reply(response.content)
        result.update({
            "http_status": response.status_code,
            "request_id": request_id,
            "sent": result.get("status") == "accepted",
        })
        if result.get("status") != "accepted":
            logger.warning("Message gateway response was not an acknowledgement: %s", result.get("detail"))
        return result
    except Exception as exc:
        # The request may have reached the server before the network failed;
        # classify as unverified and never automatically resend it.
        logger.error("Send error: %s", exc)
        return {
            "status": "unverified",
            "detail": f"network_error_{type(exc).__name__}",
            "request_id": request_id,
            "sent": False,
        }


async def send_message_result(
    cookie: str,
    chat_id: str,
    text: str,
    reply_id: str = "",
    at_user_ids: list[str] | None = None,
    domain: str = "",
) -> dict[str, Any]:
    payload, _message_cid = _build_text_request(text, chat_id, reply_id, at_user_ids)
    return await send_payload_result(cookie, payload, domain)


async def send_file_message_result(
    cookie: str,
    chat_id: str,
    file_key: str,
    file_name: str,
    file_mime: str,
    file_size: int,
    reply_id: str = "",
    domain: str = "",
) -> dict[str, Any]:
    payload, _message_cid = _build_file_request(
        file_key, file_name, file_mime, file_size, chat_id, reply_id
    )
    return await send_payload_result(cookie, payload, domain)


async def send_sticker_message_result(
    cookie: str,
    chat_id: str,
    *,
    file_key: str = "",
    sticker_set_id: str = "",
    sticker_id: str = "",
    reply_id: str = "",
    domain: str = "",
) -> dict[str, Any]:
    payload, _message_cid = _build_sticker_request(
        file_key=file_key,
        sticker_set_id=sticker_set_id,
        sticker_id=sticker_id,
        chat_id=chat_id,
        reply_id=reply_id,
    )
    return await send_payload_result(cookie, payload, domain)


async def send_message(
    cookie: str,
    chat_id: str,
    text: str,
    reply_id: str = "",
    at_user_ids: list[str] | None = None,
    domain: str = "",
) -> bool:
    """Backward-compatible text send API used by the upstream client."""
    result = await send_message_result(cookie, chat_id, text, reply_id, at_user_ids, domain)
    return bool(result.get("status") == "accepted")
