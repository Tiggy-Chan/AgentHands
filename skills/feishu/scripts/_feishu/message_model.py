"""Canonical, safe message envelopes used by every Feishu read path.

The gateway returns a fairly flat legacy message shape because existing
callers depend on it.  This module adds the schema-2 envelope without putting
raw protobuf bytes, session material, or private resource keys in normal
output or the event store.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .output import fmt_ts


SCHEMA = 2

# Keep this in sync with entities.Message.Type in the vendored proto.  The
# fallback for a future numeric type is deliberately ``type_<n>``.
TYPE_NAMES = {
    0: "unknown",
    2: "post",
    3: "file",
    4: "text",
    5: "image",
    6: "system",
    7: "audio",
    8: "email",
    9: "share_chat",
    10: "sticker",
    11: "merge_forward",
    12: "calendar",
    13: "cloud_file",
    14: "card",
    15: "media",
    16: "share_calendar_event",
    17: "hongbao",
    18: "general_calendar",
    19: "video_chat",
    20: "location",
    22: "commercialized_hongbao",
    23: "share_user",
    24: "todo",
    25: "folder",
}

RESOURCE_KINDS = frozenset(
    {"image", "file", "cloud_file", "audio", "media", "sticker", "document", "attachment"}
)

_SENSITIVE_KEYS = frozenset(
    {
        "cookie",
        "cookies",
        "authorization",
        "ticket",
        "access_key",
        "accessKey",
        "csrf",
        "csrf_token",
        "csrfToken",
        "cryptoToken",
    }
)
_SENSITIVE_QUERY = re.compile(
    r"(?i)(ticket|access[_-]?key|authorization|csrf(?:[_-]?token)?)=([^&\s]+)"
)


def type_name(code: int) -> str:
    return TYPE_NAMES.get(int(code or 0), f"type_{int(code or 0)}")


def resource_id(kind: str, key: str, ordinal: int = 0) -> str:
    """Create a stable public id without exposing Feishu's resource key."""
    # Rendered post image URLs may carry a short-lived signature/query string.
    # The query is retained in the private adapter key, but must not make the
    # public selector change between the metadata and worker fetches.
    parsed = urlsplit(str(key))
    if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.path:
        stable_key = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}"
    else:
        stable_key = str(key)
    material = f"{kind}\x00{stable_key}\x00{ordinal}".encode("utf-8", errors="replace")
    return f"{kind}:{hashlib.sha256(material).hexdigest()[:24]}"


def content_digest(content: bytes) -> dict[str, Any]:
    return {
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def public_resource(resource: dict[str, Any]) -> dict[str, Any]:
    """Remove private adapter keys while keeping stable resource metadata."""
    return {
        key: public_value(value)
        for key, value in resource.items()
        if not key.startswith("_") and key not in _SENSITIVE_KEYS
    }


def public_value(value: Any, depth: int = 0) -> Any:
    """Bound and sanitize values before they cross a user/storage boundary."""
    if depth > 6:
        return "[depth-limit]"
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _SENSITIVE_KEYS or key_text.lower() in {
                "cookie",
                "authorization",
                "ticket",
                "access_key",
                "csrftoken",
            }:
                continue
            if key_text.startswith("_"):
                continue
            out[key_text] = public_value(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [public_value(item, depth + 1) for item in list(value)[:200]]
    if isinstance(value, str):
        return _SENSITIVE_QUERY.sub(r"\1=***", value[:16 * 1024])
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:16 * 1024]


def public_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe message with all internal/raw fields removed."""
    return public_value(message)


def build_envelope(
    msg_id: str,
    raw: dict[str, Any],
    decoded: dict[str, Any],
    *,
    at_me_server: bool | None = None,
) -> dict[str, Any]:
    """Build a schema-2 message while retaining the old flat fields."""
    code = int(raw.get("type") or decoded.get("msg_type") or 0)
    raw_state = raw.get("status")
    if raw.get("isRemoved") or raw_state in (2, "DELETED", "STATUS_DELETED"):
        state = "removed"
    elif raw_state in (3, "MODIFIED", "STATUS_MODIFIED"):
        state = "modified"
    else:
        state = "normal"
    status = decoded.get("semantic_status") or decoded.get("decode_status", "opaque")
    complete = not bool(raw.get("isTruncated")) and not bool(decoded.get("truncated"))
    raw_resources = list(decoded.get("resources", []))
    resources = [public_resource(item) for item in raw_resources]
    resource_keys = {
        str(item.get("resource_id")): item.get("_key")
        for item in raw_resources
        if item.get("resource_id") and item.get("_key")
    }
    resource_data = {
        str(item.get("resource_id")): item.get("_data")
        for item in raw_resources
        if item.get("resource_id") and item.get("_data")
    }
    typed = {
        "code": code,
        "name": type_name(code),
    }
    text = str(decoded.get("text") or "")
    structured = public_value(decoded.get("structured") or {})
    mentions = sorted(set(str(x) for x in decoded.get("mention_user_ids", []) if x))
    decode_errors = [str(x)[:256] for x in decoded.get("errors", [])]

    envelope: dict[str, Any] = {
        "schema": SCHEMA,
        "id": msg_id,
        "msg_id": msg_id,
        "chat_id": raw.get("chatId", ""),
        "chat_type": raw.get("chatType", 0),
        "sender": {"id": raw.get("fromId", "")},
        "from_id": raw.get("fromId", ""),
        "created_at": fmt_ts(raw.get("createTime")),
        "create_time": raw.get("createTime", 0),
        "type": typed,
        "type_name": typed["name"],
        "content": {"text": text, "structured": structured},
        "text": text,
        "mentions": mentions,
        "mention_user_ids": mentions,
        "resources": resources,
        "nested_messages": public_value(decoded.get("nested_messages", [])),
        "state": state,
        "decode": {
            "status": status,
            "errors": decode_errors,
            "raw": public_value(decoded.get("raw_summary") or {}),
        },
        # Legacy callers expect values such as ``nontext``; the typed schema
        # above carries the more useful decoded/opaque/unavailable status.
        "decode_status": decoded.get("decode_status", status),
        "complete": complete,
        "parent_id": raw.get("parentId", ""),
        "root_id": raw.get("rootId", ""),
        "thread_id": raw.get("threadId", ""),
        "is_removed": bool(raw.get("isRemoved", False)),
        "is_truncated": bool(raw.get("isTruncated", False)),
        "at_out_chatter_ids": list(raw.get("atOutChatterIds", [])),
        "at_me_server": at_me_server,
    }
    # Private adapter data is available only in-process to media/resource
    # providers.  It is never serialized by public_message or event_store.
    for key in ("_raw_message", "_raw_content", "_resource_keys"):
        if key in decoded:
            envelope[key] = decoded[key]
    if resource_keys:
        envelope["_resource_keys"] = resource_keys
    if resource_data:
        envelope["_resource_data"] = resource_data
    return envelope


def event_message(message: dict[str, Any]) -> dict[str, Any]:
    """Strip all transport/private values before putting a message in storage."""
    return public_message(message)


def json_size(value: Any) -> int:
    return len(json.dumps(public_value(value), ensure_ascii=False, separators=(",", ":")).encode())
