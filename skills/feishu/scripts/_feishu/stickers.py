"""Bounded sticker payload parsing and outbound normalization.

Sticker messages are a tenant-specific type-10 protobuf payload.  The
Messenger UI currently emits an image-like payload (resource key, dimensions,
region and an optional numeric sticker id), while some tenants expose the
generic ``stickerSetId``/``stickerId`` fields instead.  This module keeps both
forms behind one small, private representation and never returns resource
keys from public output.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from google.protobuf.internal.decoder import _DecodeVarint


_MAX_BYTES = 64 * 1024
_MAX_FIELDS = 64
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_RESOURCE_KEY_RE = re.compile(
    r"^(?:v3_|(?:file|img|image|audio|media|video)_v)[A-Za-z0-9_.:-]{8,512}$"
)


def _wire_fields(data: bytes):
    """Yield a bounded protobuf wire walk without recursively expanding data."""
    data = bytes(data[:_MAX_BYTES])
    pos = 0
    count = 0
    while pos < len(data) and count < _MAX_FIELDS:
        count += 1
        try:
            tag, next_pos = _DecodeVarint(data, pos)
            field, wire = tag >> 3, tag & 0x7
            if not field:
                return
            if wire == 0:
                value, pos = _DecodeVarint(data, next_pos)
                yield field, wire, value
            elif wire == 2:
                length, value_pos = _DecodeVarint(data, next_pos)
                end = value_pos + length
                if end > len(data):
                    return
                yield field, wire, data[value_pos:end]
                pos = end
            elif wire == 1:
                end = next_pos + 8
                if end > len(data):
                    return
                yield field, wire, data[next_pos:end]
                pos = end
            elif wire == 5:
                end = next_pos + 4
                if end > len(data):
                    return
                yield field, wire, data[next_pos:end]
                pos = end
            else:
                return
        except Exception:
            return


def _text(value: Any, limit: int = 512) -> str:
    if not isinstance(value, (bytes, bytearray)):
        return ""
    try:
        text = bytes(value).decode("utf-8")
    except UnicodeDecodeError:
        return ""
    if not text or len(text) > limit or not text.isprintable():
        return ""
    return text


def _valid_id(value: str) -> str:
    return value if _ID_RE.fullmatch(value or "") else ""


def validate_sticker_id(value: str, field: str = "sticker id") -> str:
    """Validate a caller-provided stable sticker identifier."""
    normalized = str(value or "").strip()
    if not _ID_RE.fullmatch(normalized):
        raise ValueError(f"{field} 格式不合法")
    return normalized


def _valid_resource_key(value: str) -> str:
    return value if _RESOURCE_KEY_RE.fullmatch(value or "") else ""


def parse_sticker_content(content: bytes) -> dict[str, Any]:
    """Parse known type-10 forms into a private sticker specification.

    The returned ``file_key`` is transport-private.  Callers may use it to
    build a request, but it must not cross the public message/output boundary.
    """
    raw = bytes(content or b"")[:_MAX_BYTES]
    fields = list(_wire_fields(raw))
    file_key = ""
    sticker_set_id = ""
    sticker_id = ""
    width = 0
    height = 0
    region = ""

    # Generic Content fields are useful when the tenant includes them.
    try:
        from lark_bridge.proto import proto_pb2 as pb

        generic = pb.Content()
        generic.ParseFromString(raw)
        file_key = _valid_resource_key(str(generic.fileKey or ""))
        if not file_key:
            for value in generic.attachments:
                file_key = _valid_resource_key(str(value or ""))
                if file_key:
                    break
        sticker_set_id = _valid_id(str(generic.stickerSetId or ""))
        sticker_id = _valid_id(str(generic.stickerId or ""))
    except Exception:
        # The type-10 image-like layout below is intentionally independent of
        # the generic Content descriptor, which is not aligned for all types.
        pass

    # Tenant-observed type-10 layout:
    # f4 resource key, f5 width, f6 height, f8 region, f10 sticker id.
    for field, wire, value in fields:
        if field == 4 and wire == 2 and not file_key:
            file_key = _valid_resource_key(_text(value))
        elif field == 5 and wire == 0 and 0 < int(value) <= 100_000:
            width = int(value)
        elif field == 6 and wire == 0 and 0 < int(value) <= 100_000:
            height = int(value)
        elif field == 8 and wire == 2 and not region:
            region = _text(value, 128)
        elif field == 10 and wire == 2 and not sticker_id:
            sticker_id = _valid_id(_text(value))

    return {
        "file_key": file_key,
        "sticker_set_id": sticker_set_id,
        "sticker_id": sticker_id,
        "width": width,
        "height": height,
        "region": region,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_bytes": len(raw),
    }


def public_sticker_details(spec: dict[str, Any]) -> dict[str, Any]:
    """Return safe sticker metadata for the schema-2 public envelope."""
    result: dict[str, Any] = {}
    for key in ("sticker_set_id", "sticker_id", "region"):
        value = str(spec.get(key) or "")
        if value:
            result[{"sticker_set_id": "stickerSetId", "sticker_id": "stickerId", "region": "region"}[key]] = value
    for key in ("width", "height"):
        value = int(spec.get(key) or 0)
        if value:
            result[key] = value
    if spec.get("file_key"):
        result["has_file_key"] = True
    if spec.get("raw_sha256"):
        result["content_sha256"] = str(spec["raw_sha256"])
    if spec.get("raw_bytes"):
        result["content_bytes"] = int(spec["raw_bytes"])
    return result


def sticker_fingerprint_material(spec: dict[str, Any]) -> dict[str, Any]:
    """Return stable, non-secret fields used by the outbound draft hash."""
    return {
        "sticker_set_id": str(spec.get("sticker_set_id") or ""),
        "sticker_id": str(spec.get("sticker_id") or ""),
        "file_key_sha256": hashlib.sha256(str(spec.get("file_key") or "").encode()).hexdigest()
        if spec.get("file_key") else "",
        "raw_sha256": str(spec.get("raw_sha256") or ""),
    }


def has_sendable_identity(spec: dict[str, Any]) -> bool:
    """Whether a sticker has enough stable identity to attempt sending."""
    return bool(spec.get("file_key") or (spec.get("sticker_set_id") and spec.get("sticker_id")))
