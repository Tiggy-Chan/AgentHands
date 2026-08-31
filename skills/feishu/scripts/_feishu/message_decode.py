"""Structured message decode: text + mention user ids + status.

Wraps the vendor text decoder and adds mention extraction that the vendor
flattens away. Mention evidence hierarchy (best first):

1. Message.atOutChatterIds / PushMessagesRequest.messagesAtMe — server side
   (handled by the caller, not here)
2. RichText atIds + AT elements — content bytes parsed as TextContent
3. type-14 JSON bodies — <at userID=...> entries
"""

from __future__ import annotations

import json

from google.protobuf.internal.decoder import _DecodeVarint

from lark_bridge.decoder import decode_text
from lark_bridge.proto import proto_pb2 as pb
from lark_bridge._proto_utils import proto_to_dict

# entities.Message.Type — the ones we try to decode text from
_TEXT_TYPES = {4, 14}

_TYPE_NAMES = {
    2: "post", 3: "file", 4: "text", 5: "image", 6: "system", 7: "audio",
    9: "share_chat", 10: "sticker", 11: "merge_forward", 13: "cloud_file",
    14: "card", 15: "media", 23: "share_user", 24: "todo",
}


def type_name(t: int) -> str:
    return _TYPE_NAMES.get(t, f"type_{t}")


def _mentions_from_richtext(rt_dict: dict) -> list[str]:
    out: list[str] = []
    elements = rt_dict.get("elements", {}).get("dictionary", {})
    for eid in rt_dict.get("atIds", []):
        el = elements.get(eid, {})
        prop = el.get("property", b"")
        if not prop:
            continue
        try:
            tp = pb.TextProperty()
            tp.ParseFromString(prop)
            if tp.content:
                out.append(tp.content)
        except Exception:
            continue
    return out


def _mentions_from_card_json(content: bytes) -> list[str]:
    """type=14 card/post: find the JSON body (field 20) and walk at tags."""
    out: list[str] = []
    try:
        pos = 0
        json_body = ""
        while pos < len(content):
            tag, pos2 = _DecodeVarint(content, pos)
            fn, wt = tag >> 3, tag & 7
            if wt == 0:
                _, pos = _DecodeVarint(content, pos2)
            elif wt == 2:
                ln, pos2 = _DecodeVarint(content, pos2)
                val = content[pos2 : pos2 + ln]
                if fn == 20:
                    json_body = val.decode("utf-8", errors="replace")
                pos = pos2 + ln
            elif wt == 5:
                pos = pos2 + 4
            elif wt == 1:
                pos = pos2 + 8
            else:
                break
        if not json_body:
            return out
        body = json.loads(json_body)

        def walk(node):
            if isinstance(node, dict):
                if node.get("tag") == "at":
                    uid = (node.get("property") or {}).get("userID") or node.get("userID")
                    if uid:
                        out.append(str(uid))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(body)
    except Exception:
        pass
    return out


def decode_message(m: dict) -> dict:
    """m: proto_to_dict(entities.Message) with content restored to bytes.

    → {text, mention_user_ids, decode_status, msg_type, msg_type_name}
    decode_status: ok | empty | undecoded | nontext
    """
    msg_type = m.get("type", 0)
    content = m.get("content", b"") or b""
    text = decode_text(m) or ""
    mentions: list[str] = []

    if msg_type == 4 and content:
        # Try the full TextContent proto (richText carries atIds); the
        # vendor's fast path only reads wire field 1 and loses them.
        try:
            tc = pb.TextContent()
            tc.ParseFromString(content)
            mentions = _mentions_from_richtext(proto_to_dict(tc).get("richText", {}))
        except Exception:
            mentions = []
    elif msg_type == 14 and content:
        mentions = _mentions_from_card_json(content)

    if text:
        status = "ok"
    elif msg_type not in _TEXT_TYPES:
        status = "nontext"
    elif not content:
        status = "empty"
    else:
        status = "undecoded"

    return {
        "text": text,
        "mention_user_ids": sorted(set(mentions)),
        "decode_status": status,
        "msg_type": msg_type,
        "msg_type_name": type_name(msg_type),
    }
