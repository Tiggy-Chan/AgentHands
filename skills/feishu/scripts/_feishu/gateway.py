"""IM gateway (protobuf) calls, all routed through transport's guard.

cmd 8     fetch messages by id
cmd 64    group name
cmd 5023  user name
cmd 11021 message search (time window / sender / mention filters)

cmd 5/13 live in send.py behind the send gate.
"""

from __future__ import annotations

import hashlib
import json
import time
import asyncio

from google.protobuf.internal.decoder import _DecodeVarint

from lark_bridge.proto import proto_pb2 as pb
from lark_bridge._proto_utils import proto_to_dict
from lark_bridge.search import _build_search_params, _ev

from . import config, transport
from .errors import FeishuError
from .message_decode import decode_message
from .message_model import build_envelope


def _rid(tag: str = "") -> str:
    return hashlib.md5(f"{time.time()}{tag}".encode()).hexdigest()[:10]


def _parse_reply_packet(data: bytes) -> dict:
    pkt = pb.Packet()
    pkt.ParseFromString(data)
    return proto_to_dict(pkt)


# --- cmd 8: fetch messages --------------------------------------------------

async def fetch_messages(msg_ids: list[str]) -> list[dict]:
    """→ normalized message dicts (with structured decode), input order kept."""
    if not msg_ids:
        return []
    inner = b""
    for mid in msg_ids:
        inner += b"\x0a" + _ev(len(mid)) + mid.encode()
    data = await transport.gateway_call(8, inner, _rid("fetch"))
    pl = _parse_reply_packet(data).get("payload", b"")
    if not pl:
        return []
    try:
        push = pb.PushMessagesRequest()
        push.ParseFromString(pl)
        push_dict = proto_to_dict(push)
    except Exception as e:
        raise FeishuError(f"cmd=8 响应解析失败：{type(e).__name__}")
    at_me_map = push_dict.get("messagesAtMe", {})
    out = []
    for mid, m in push_dict.get("messages", {}).items():
        out.append(normalize(mid, m, at_me_map.get(mid)))
    order = {mid: i for i, mid in enumerate(msg_ids)}
    out.sort(key=lambda x: order.get(x["msg_id"], len(order)))
    return out


def normalize(msg_id: str, m: dict, at_me_server=None) -> dict:
    """Raw proto dict → the message shape used everywhere in this skill."""
    dec = decode_message(m)
    return build_envelope(msg_id, m, dec, at_me_server=at_me_server)


# --- cmd 11021: message search ----------------------------------------------

async def search_msg_ids_page(
    chat_id: str,
    start_time: int = 0,
    end_time: int = 0,
    from_id: str = "",
    mention_user_id: str = "",
    page_token: str = "",
) -> dict:
    """One page → {msg_ids, has_more, page_token}.

    start_time/end_time units are UNVERIFIED (probably epoch seconds;
    confirm against a live session — see references/protocol.md)."""
    session_id = _rid("s")
    search_params = _build_search_params(chat_id, start_time, end_time, from_id, mention_user_id)

    search_req = b"\x0a" + _ev(len(session_id)) + session_id.encode()
    search_req += b"\x10\x01\x1a\x00"
    if page_token:
        pt = page_token.encode()
        search_req += b"\x22" + _ev(len(pt)) + pt
    search_req += b"\x2a" + _ev(len(search_params)) + search_params
    search_req += b"\x32\x05zh_CN"
    inner = b"\x0a" + _ev(len(search_req)) + search_req

    data = await transport.gateway_call(11021, inner, _rid("search"))
    pl = _parse_reply_packet(data).get("payload", b"")
    if not pl:
        return {"msg_ids": [], "has_more": False, "page_token": ""}

    msg_ids: list[str] = []
    json_str = ""
    pos = 0
    while pos < len(pl):
        try:
            tag, pos2 = _DecodeVarint(pl, pos)
            fn, wt = tag >> 3, tag & 7
            if wt == 2:
                ln, pos2 = _DecodeVarint(pl, pos2)
                val = pl[pos2 : pos2 + ln]
                if fn == 1:
                    t = val.decode("utf-8", errors="replace")
                    j = t.find("{")
                    if j >= 0:
                        json_str = t[j : t.rfind("}") + 1]
                elif fn == 2:
                    if len(val) > 21 and val[0:2] == b"\x0a\x13":
                        mid = val[2:21].decode("utf-8", errors="replace")
                        if mid.startswith("7") and mid.isdigit():
                            msg_ids.append(mid)
                pos = pos2 + ln
            elif wt == 0:
                _, pos = _DecodeVarint(pl, pos2)
            else:
                break
        except Exception:
            break

    has_more = False
    next_token = ""
    if json_str:
        try:
            j = json.loads(json_str)
            has_more = bool(j.get("HasMore"))
            if has_more:
                next_token = json.dumps(j, separators=(",", ":"))
        except Exception:
            pass
    return {"msg_ids": list(dict.fromkeys(msg_ids)), "has_more": has_more, "page_token": next_token}


async def search_msg_ids(
    chat_id: str,
    start_time: int = 0,
    end_time: int = 0,
    from_id: str = "",
    mention_user_id: str = "",
    limit: int = 30,
) -> dict:
    """Auto-paginating search → {msg_ids, has_more}."""
    if limit <= 0:
        return {"msg_ids": [], "has_more": False}
    if limit > 200:
        raise FeishuError("history limit 不能超过 200", hint="收窄时间窗或分段查询")

    all_ids: list[str] = []
    page_token = ""
    has_more = False
    seen_tokens: set[str] = set()
    for _page_no in range(config.SEARCH_MAX_PAGES):
        if len(all_ids) >= limit:
            break
        if page_token in seen_tokens:
            raise FeishuError("消息搜索返回了重复分页 token，已停止以避免无限请求")
        seen_tokens.add(page_token)
        page = await asyncio.wait_for(
            search_msg_ids_page(chat_id, start_time, end_time, from_id, mention_user_id, page_token),
            timeout=config.HISTORY_TIMEOUT_SECONDS,
        )
        if not page["msg_ids"]:
            break
        all_ids.extend(page["msg_ids"])
        has_more = page["has_more"]
        next_token = page.get("page_token", "")
        if not has_more:
            break
        if not next_token or next_token == page_token:
            raise FeishuError("消息搜索分页 token 无效，已停止以避免无限请求")
        page_token = next_token
    else:
        raise FeishuError(
            f"消息搜索超过 {config.SEARCH_MAX_PAGES} 页，已停止以保护主机",
            hint="收窄时间窗或降低 --limit",
        )
    all_ids = list(dict.fromkeys(all_ids))[:limit]
    return {"msg_ids": all_ids, "has_more": has_more or len(all_ids) >= limit}


# --- cmd 64 / 5023: name resolution -----------------------------------------
# lark-cli showed the request shapes; the response is a UserInfo proto whose
# exact field numbers we don't vendor, so names are extracted by a wire walk
# and picked heuristically. Best-effort: verify against real sessions.

def _walk_strings(
    data: bytes,
    depth: int = 0,
    out: list | None = None,
    *,
    max_bytes: int = 1 << 20,
    max_strings: int = 256,
) -> list[str]:
    """Collect plausible UTF-8 strings with a hard resource budget.

    cmd=64/5023 responses are not fully described by the vendored schema. A
    malformed or unexpectedly large length-delimited field must not create an
    unbounded recursive search or result list.
    """
    if out is None:
        out = []
    if depth > 4 or not data or len(out) >= max_strings:
        return out
    data = data[:max_bytes]
    pos = 0
    while pos < len(data):
        try:
            tag, pos2 = _DecodeVarint(data, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            try:
                ln, pos3 = _DecodeVarint(data, pos2)
            except Exception:
                break
            val = data[pos3 : pos3 + ln]
            if pos3 + ln > len(data):
                break
            try:
                s = val.decode("utf-8")
                if s.isprintable() and 1 <= len(s) <= 64 and not s.isdigit():
                    out.append(s)
                    pos = pos3 + ln
                    continue
            except (UnicodeDecodeError, ValueError):
                pass
            if len(val) <= max_bytes and len(out) < max_strings:
                _walk_strings(
                    val, depth + 1, out,
                    max_bytes=max_bytes, max_strings=max_strings,
                )
            pos = pos3 + ln
        elif wt == 0:
            try:
                _, pos = _DecodeVarint(data, pos2)
            except Exception:
                break
        elif wt == 5:
            pos = pos2 + 4
        elif wt == 1:
            pos = pos2 + 8
        else:
            break
    return out


def _pick_name(candidates: list[str]) -> str:
    """Heuristic: skip ids/urls/emails, prefer strings with CJK or spaces."""
    bad = ("http", "://", "@", ".com", ".cn")
    pool = [
        s for s in candidates
        if not any(b in s for b in bad) and not s.replace("_", "").replace("-", "").isalnum()
    ] or [s for s in candidates if not any(b in s for b in bad)]
    if not pool:
        return ""
    pool.sort(key=lambda s: (not any("一" <= c <= "鿿" for c in s), len(s)))
    return pool[0]


async def get_group_name(chat_id: str) -> str:
    """cmd=64 → group display name (best effort)."""
    req = b"\x0a" + _ev(len(chat_id)) + chat_id.encode()
    data = await transport.gateway_call(64, req, _rid("g"))
    pl = _parse_reply_packet(data).get("payload", b"")
    return _pick_name(_walk_strings(pl)) if pl else ""


async def get_user_name(user_id: str, chat_id: str = "0") -> str:
    """cmd=5023 → user display name (best effort)."""
    try:
        uid, cid = int(user_id), int(chat_id) if str(chat_id).isdigit() else 0
    except (TypeError, ValueError):
        return ""
    req = b"\x08" + _ev(uid) + b"\x10" + _ev(cid) + b"\x18\x01"
    data = await transport.gateway_call(5023, req, _rid("u"))
    pl = _parse_reply_packet(data).get("payload", b"")
    return _pick_name(_walk_strings(pl)) if pl else ""
