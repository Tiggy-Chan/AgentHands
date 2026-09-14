"""Structured message decode for every known Feishu message type.

Wraps the vendor text decoder and adds mention extraction that the vendor
flattens away. Mention evidence hierarchy (best first):

1. Message.atOutChatterIds / PushMessagesRequest.messagesAtMe — server side
   (handled by the caller, not here)
2. type-2 post HTML — <at user_id=...> entries
3. RichText atIds + AT elements — content bytes parsed as TextContent
4. type-14 JSON bodies — <at userID=...> entries
"""

from __future__ import annotations

import json
import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

from google.protobuf.internal.decoder import _DecodeVarint

from .message_model import TYPE_NAMES, content_digest, resource_id, type_name as model_type_name
from .stickers import parse_sticker_content, public_sticker_details
from lark_bridge.decoder import decode_text, decode_type2_content
from lark_bridge.proto import proto_pb2 as pb
from lark_bridge._proto_utils import proto_to_dict

# entities.Message.Type — the ones with a known text representation.
_TEXT_TYPES = {2, 4, 14}
_MAX_CONTENT_BYTES = 8 * 1024 * 1024
_MAX_RESOURCES = 64
_MAX_NESTED_MESSAGES = 200
_MAX_WIRE_FIELDS = 128
_FILE_TOKEN_RE = re.compile(r"^(?:file|file_v3)_[A-Za-z0-9][A-Za-z0-9._:-]{7,4095}$")

# Tenant-verified wire layout (2026-09-07, media-observe probe + raw content
# dumps on real messages): media keys on the wire look like
#   img_v3_0215a_<uuid>g      (IMAGE, inside nested field 2)
#   file_v3_00157_<uuid>g     (FILE, field 1; name/mime/size in fields 2/4/3)
# and the web client fetches them from /static-resource/v1/<key minus the
# kind prefix>~ on internal-api-lark-file.feishu.cn / s1-imfile.feishucdn.com.
_MEDIA_KEY_RE = re.compile(
    r"\b(?P<prefix>img|image|file|audio|media|video)_v\d+_[A-Za-z0-9]{2,16}_[0-9a-fA-F-]{8,}g~?\b"
)
_MEDIA_KIND = {
    "img": "image", "image": "image", "file": "file",
    "audio": "audio", "media": "media", "video": "media",
}
# The vendored Content proto is misaligned for FILE/IMAGE (a FILE message's
# field 2 is the file *name*, which the vendored schema reads as imageKey).
# Proto-derived media keys must therefore pass a plausibility check; anything
# else (filenames with dots, mime strings, short region tags) is dropped.
_PROTO_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,512}$")

# Kept as a compatibility alias for callers that imported the old constant.
_TYPE_NAMES = TYPE_NAMES


def type_name(t: int) -> str:
    return model_type_name(t)


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        # proto_to_dict restores bytes, but test fixtures and a few callers
        # pass base64-like strings.  Do not treat arbitrary text as binary
        # unless it is already a string payload in a known text decoder.
        return value.encode("utf-8", errors="replace")
    return b""


def _clip(value: Any, limit: int = 4096) -> str:
    return str(value or "")[:limit]


def _wire_fields(data: bytes):
    """Yield bounded protobuf field tuples without recursively expanding data."""
    if len(data) > _MAX_CONTENT_BYTES:
        data = data[:_MAX_CONTENT_BYTES]
    pos = 0
    count = 0
    while pos < len(data) and count < _MAX_WIRE_FIELDS:
        count += 1
        try:
            tag, next_pos = _DecodeVarint(data, pos)
            field_number, wire_type = tag >> 3, tag & 0x7
            if not field_number:
                return
            if wire_type == 0:
                value, pos = _DecodeVarint(data, next_pos)
                yield field_number, wire_type, value
            elif wire_type == 1:
                end = next_pos + 8
                if end > len(data):
                    return
                yield field_number, wire_type, data[next_pos:end]
                pos = end
            elif wire_type == 2:
                length, value_pos = _DecodeVarint(data, next_pos)
                end = value_pos + length
                if end > len(data):
                    return
                yield field_number, wire_type, data[value_pos:end]
                pos = end
            elif wire_type == 5:
                end = next_pos + 4
                if end > len(data):
                    return
                yield field_number, wire_type, data[next_pos:end]
                pos = end
            else:
                return
        except Exception:
            return


def _wire_summary(content: bytes) -> dict[str, Any]:
    fields = []
    for number, wire_type, value in _wire_fields(content):
        item: dict[str, Any] = {"field": number, "wire_type": wire_type}
        if wire_type == 2:
            raw = bytes(value)
            item["bytes"] = len(raw)
            # Do not expose printable wire fields here: resource keys,
            # signed URLs, and future opaque tokens can look like harmless
            # text.  Type-specific decoders expose only intentional fields.
        elif wire_type == 0:
            item["value"] = int(value)
        else:
            item["bytes"] = len(value)
        fields.append(item)
    return {**content_digest(content), "fields": fields}


def _try_str(value: Any) -> str:
    if not isinstance(value, (bytes, bytearray)):
        return ""
    try:
        text = bytes(value).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return ""
    return text if text.isprintable() and len(text) <= 2048 else ""


def _wire_strings(data: bytes, depth: int = 0, out: list[str] | None = None) -> list[str]:
    """Collect bounded printable strings, recursing into nested fields."""
    if out is None:
        out = []
    if depth > 2 or len(out) >= 64:
        return out
    for _fn, wire_type, value in _wire_fields(data):
        if wire_type != 2 or len(out) >= 64:
            continue
        raw = bytes(value)
        text = _try_str(raw)
        if text:
            out.append(text)
        elif 2 < len(raw) <= 4096:
            _wire_strings(raw, depth + 1, out)
    return out


def _wire_media_resources(content: bytes) -> list[dict[str, Any]]:
    """Extract img/file/audio/media keys straight off the wire.

    Type-specific layouts below cover the common cases; this scan is the
    safety net for kinds whose layout we have not mapped (audio, video,
    future variants), keyed on the stable ``<kind>_v<N>_<shard>_<uuid>g``
    shape instead of field numbers.
    """
    out: list[dict[str, Any]] = []
    for text in _wire_strings(content):
        for match in _MEDIA_KEY_RE.finditer(text):
            key = match.group(0).rstrip("~")
            kind = _MEDIA_KIND[match.group("prefix")]
            _append_resource(out, _resource(kind, key, source=f"content.wire_scan.{kind}"))
    return out


def _plausible_media_key(key: str) -> bool:
    if not key:
        return False
    if _MEDIA_KEY_RE.search(key):
        return True
    if urlsplit(key).scheme == "https":
        return True
    return bool(_PROTO_KEY_RE.fullmatch(key))


def _file_details(content: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """FILE (type 3) layout, tenant-verified 2026-09-07:
    f1 str = key (file_v3_…), f2 str = name, f3 varint = size, f4 str = mime.
    2026-09-10: f7 str = region tag (e.g. eu_nc) now captured; f5/f6 (if
    present) recorded as presence+length only — if file payloads turn out to
    be AES-256-GCM like images, the crypto material lives in one of those.
    """
    key = name = mime = ""
    size = 0
    region = ""
    unknown: dict[str, int] = {}
    for fn, wire_type, value in _wire_fields(content):
        if fn == 1 and wire_type == 2:
            key = _try_str(value)
        elif fn == 2 and wire_type == 2:
            name = _try_str(value)
        elif fn == 3 and wire_type == 0:
            size = int(value)
        elif fn == 4 and wire_type == 2:
            mime = _try_str(value)
        elif fn == 7 and wire_type == 2:
            region = _try_str(value)
        elif fn in (5, 6) and wire_type == 2:
            unknown[f"f{fn}"] = len(value)  # presence+length only, never content
    structured: dict[str, Any] = {}
    if name:
        structured["fileName"] = _clip(name, 512)
    if mime:
        structured["fileMime"] = _clip(mime, 256)
    if size:
        structured["fileSize"] = size
    if region:
        structured["fileRegion"] = _clip(region, 64)
    if unknown:
        structured["fileUnknownFields"] = unknown
    structured["field_mapping"] = "tenant-verified-2026-09-07"
    resources: list[dict[str, Any]] = []
    if key:
        _append_resource(resources, _resource(
            "file", key, name=name, mime=mime, size=size, source="content.file_key"))
    return structured, resources


def _image_details(content: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """IMAGE (type 5) layout, tenant-verified 2026-09-07:
    f2 nested {f1 str key (img_v3_…), f2 str region, f3 nested crypto
    {f2 nested {f1 32B AES-256 key, f2 12B GCM nonce}}, f4 repeated size
    variant {f1 varint slot, f2 nested {f1 width, f2 height}}};
    root f4 varint = bytes.  Media bytes are AES-256-GCM encrypted; the tag
    is the last 16 bytes of the downloaded blob (verified by decrypting a
    live download to a valid JPEG matching the declared dimensions).
    """
    key = region = ""
    crypto: dict[str, str] = {}
    width = height = size = 0
    for fn, wire_type, value in _wire_fields(content):
        if fn == 2 and wire_type == 2:
            for sfn, swt, sval in _wire_fields(bytes(value)):
                if sfn == 1 and swt == 2:
                    key = _try_str(sval)
                elif sfn == 2 and swt == 2:
                    region = _try_str(sval)
                elif sfn == 3 and swt == 2:
                    for cfn, cwt, cval in _wire_fields(bytes(sval)):
                        if cfn == 2 and cwt == 2:
                            raw_key = raw_nonce = b""
                            for kfn, kwt, kval in _wire_fields(bytes(cval)):
                                if kfn == 1 and kwt == 2 and len(bytes(kval)) == 32:
                                    raw_key = bytes(kval)
                                elif kfn == 2 and kwt == 2 and len(bytes(kval)) == 12:
                                    raw_nonce = bytes(kval)
                            if raw_key and raw_nonce:
                                crypto = {
                                    "algo": "aes-256-gcm",
                                    "key": raw_key.hex(),
                                    "nonce": raw_nonce.hex(),
                                }
                elif sfn == 4 and swt == 2:
                    vslot = 0
                    wh = (0, 0)
                    for vfn, vwt, vval in _wire_fields(bytes(sval)):
                        if vfn == 1 and vwt == 0:
                            vslot = int(vval)
                        elif vfn == 2 and vwt == 2:
                            w = h = 0
                            for dfn, dwt, dval in _wire_fields(bytes(vval)):
                                if dfn == 1 and dwt == 0:
                                    w = int(dval)
                                elif dfn == 2 and dwt == 0:
                                    h = int(dval)
                            wh = (w, h)
                    if vslot == 1 and wh != (0, 0):
                        width, height = wh
        elif fn == 4 and wire_type == 0:
            size = int(value)
    structured: dict[str, Any] = {}
    if width and height:
        structured["image"] = {"width": width, "height": height}
    if size:
        structured["imageBytes"] = size
    if crypto:
        structured["encryption"] = crypto["algo"]
    structured["field_mapping"] = "tenant-verified-2026-09-07"
    resources: list[dict[str, Any]] = []
    if key:
        item = _resource("image", key, size=size, source="content.image_key")
        if item and crypto:
            item["_crypto"] = crypto
        _append_resource(resources, item)
    return structured, resources


def _find_crypto(data: bytes, depth: int = 0) -> dict[str, str]:
    """Recursively discover AES-256-GCM material (32B key + 12B nonce) in wire fields."""
    if depth > 4 or not data:
        return {}
    raw_key = raw_nonce = b""
    nested: list[bytes] = []
    for fn, wt, val in _wire_fields(data):
        if wt == 2:
            bval = bytes(val)
            if fn == 1 and len(bval) == 32:
                raw_key = bval
            elif fn == 2 and len(bval) == 12:
                raw_nonce = bval
            elif 10 < len(bval) <= 4096:
                nested.append(bval)
    if raw_key and raw_nonce:
        return {"algo": "aes-256-gcm", "key": raw_key.hex(), "nonce": raw_nonce.hex()}
    for child in nested:
        found = _find_crypto(child, depth + 1)
        if found:
            return found
    return {}


def _media_details(content: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """MEDIA / Video (type 15) layout, tenant-verified 2026-09-07:
    f1 str = key (file_v3_…), f2 str = name, f3 varint = size, f4 str = mime,
    f6 nested = video cover (img_v3_… + AES-256-GCM crypto), f7 varint = duration_ms.
    """
    key = name = mime = ""
    size = duration = 0
    cover_image_key = ""
    cover_crypto: dict[str, str] = {}

    for fn, wire_type, value in _wire_fields(content):
        if fn == 1 and wire_type == 2:
            key = _try_str(value)
        elif fn == 2 and wire_type == 2:
            name = _try_str(value)
        elif fn == 3 and wire_type == 0:
            size = int(value)
        elif fn == 4 and wire_type == 2:
            mime = _try_str(value)
        elif fn == 6 and wire_type == 2:
            raw_f6 = bytes(value)
            for text in _wire_strings(raw_f6):
                match = _MEDIA_KEY_RE.search(text)
                if match and not cover_image_key:
                    cover_image_key = match.group(0).rstrip("~")
            cover_crypto = _find_crypto(raw_f6)
        elif fn == 7 and wire_type == 0:
            duration = int(value)

    structured: dict[str, Any] = {}
    if name:
        structured["fileName"] = _clip(name, 512)
    if mime:
        structured["fileMime"] = _clip(mime, 256)
    if size:
        structured["fileSize"] = size
    if duration:
        structured["duration"] = duration
    if cover_crypto:
        structured["coverEncryption"] = cover_crypto["algo"]
    structured["field_mapping"] = "tenant-verified-2026-09-07"

    resources: list[dict[str, Any]] = []
    if key:
        _append_resource(
            resources,
            _resource("media", key, name=name, mime=mime, size=size, duration=duration, source="content.media_key"),
        )
    if cover_image_key:
        cover_res = _resource(
            "image",
            cover_image_key,
            name=f"{name}.cover.webp" if name else "cover.webp",
            source="content.media_cover",
        )
        if cover_res and cover_crypto:
            cover_res["_crypto"] = cover_crypto
        _append_resource(resources, cover_res)

    return structured, resources


def _resource(
    kind: str,
    key: str,
    *,
    ordinal: int = 0,
    name: str = "",
    mime: str = "",
    size: int = 0,
    duration: int = 0,
    source: str = "message_content",
) -> dict[str, Any] | None:
    key = _clip(key, 2048).strip()
    if not key or kind not in {"image", "file", "cloud_file", "audio", "media", "sticker", "document", "attachment"}:
        return None
    # The rendered image URL in a post can change on every gateway fetch
    # (short-lived path and signature). Its position in this message is the
    # only stable selector; the full URL remains private as the adapter key.
    id_key = key
    parsed = urlsplit(key)
    if source == "post.img" and parsed.scheme in {"http", "https"}:
        id_key = f"{source}:{ordinal}"
    item: dict[str, Any] = {
        "resource_id": resource_id(kind, id_key, ordinal),
        "kind": kind,
        "status": "metadata",
        "source": source,
        "name": _clip(name, 512),
        "mime": _clip(mime, 256),
        "size": max(0, int(size or 0)),
        "duration": max(0, int(duration or 0)),
        "_key": key,
    }
    return item


def _append_resource(resources: list[dict[str, Any]], item: dict[str, Any] | None) -> None:
    if not item or len(resources) >= _MAX_RESOURCES:
        return
    rid = item.get("resource_id")
    if rid and any(existing.get("resource_id") == rid for existing in resources):
        return
    resources.append(item)


def _safe_content_fields(content_obj: pb.Content) -> dict[str, Any]:
    """Expose descriptive Content fields while excluding keys and secrets."""
    fields: dict[str, Any] = {}
    for name in (
        "text", "title", "fileName", "fileMime", "fileSize", "audioDuration",
        "duration", "attendeesCount", "isOriginSource", "isGroupAnnouncement",
        "fileTransMode",
    ):
        value = getattr(content_obj, name, None)
        if value not in (None, "", 0, False):
            fields[name] = value.name if hasattr(value, "name") else value
    return fields


def _parse_content_proto(content: bytes) -> tuple[dict[str, Any], list[dict[str, Any]], bool, list[str]]:
    """Parse the shared non-text Content envelope and extract resource refs."""
    if not content or len(content) > _MAX_CONTENT_BYTES:
        return {}, [], False, ["content_size_limit"] if content else []
    obj = pb.Content()
    try:
        obj.ParseFromString(content)
    except Exception as exc:
        return {}, [], False, [type(exc).__name__]
    fields = obj.ListFields()
    if not fields:
        return {}, [], False, []
    structured = _safe_content_fields(obj)
    resources: list[dict[str, Any]] = []

    def add_proto_resource(kind: str, key: str, **kwargs) -> None:
        # The vendored Content field numbers drift per tenant/message type;
        # a proto-derived media key that cannot be a real key (file name,
        # mime string, region tag) is dropped instead of becoming a phantom.
        if not _plausible_media_key(key):
            return
        _append_resource(resources, _resource(kind, key, **kwargs))

    add_proto_resource("image", obj.imageKey, name="", source="content.imageKey")
    add_proto_resource(
        "file",
        obj.fileKey,
        name=obj.fileName,
        mime=obj.fileMime,
        size=obj.fileSize,
        source="content.fileKey",
    )
    add_proto_resource(
        "audio",
        obj.audioKey,
        duration=obj.audioDuration or obj.duration,
        source="content.audioKey",
    )
    for index, key in enumerate(obj.attachments):
        add_proto_resource("attachment", key, ordinal=index, source="content.attachments")
    # Wire scan is the source of truth for tenant media keys; union it in for
    # kinds the proto mapping missed (and it is what catches future drift).
    for resource in _wire_media_resources(content):
        _append_resource(resources, resource)
    if obj.stickerSetId or obj.stickerId:
        _append_resource(
            resources,
            _resource(
                "sticker",
                f"{obj.stickerSetId}:{obj.stickerId}",
                name=obj.stickerId,
                source="content.sticker",
            ),
        )
        structured["stickerSetId"] = _clip(obj.stickerSetId, 256)
        structured["stickerId"] = _clip(obj.stickerId, 256)
    if obj.shareUserId:
        structured["shareUserId"] = _clip(obj.shareUserId, 256)
    return structured, resources, True, []


def _richtext_details(content: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    details: dict[str, Any] = {}
    resources: list[dict[str, Any]] = []
    try:
        tc = pb.TextContent()
        tc.ParseFromString(content)
        rt = tc.richText
        if rt.innerText:
            details["innerText"] = _clip(rt.innerText)
        for name, values in (
            ("imageIds", rt.imageIds),
            ("mediaIds", rt.mediaIds),
            ("docsIds", rt.docsIds),
            ("interactiveIds", rt.interactiveIds),
            ("anchorIds", rt.anchorIds),
        ):
            if values:
                details[name] = [_clip(v, 512) for v in list(values)[:_MAX_RESOURCES]]
        for index, key in enumerate(rt.imageIds):
            _append_resource(resources, _resource("image", key, ordinal=index, source="richText.imageIds"))
        for index, key in enumerate(rt.mediaIds):
            _append_resource(resources, _resource("media", key, ordinal=index, source="richText.mediaIds"))
        for index, key in enumerate(rt.docsIds):
            _append_resource(resources, _resource("document", key, ordinal=index, source="richText.docsIds"))
    except Exception:
        pass
    return details, resources


def _card_json(content: bytes) -> dict[str, Any] | None:
    for field_number, wire_type, value in _wire_fields(content):
        if field_number != 20 or wire_type != 2:
            continue
        try:
            parsed = json.loads(bytes(value).decode("utf-8", errors="replace"))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _card_details(body: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    details: dict[str, Any] = {}
    resources: list[dict[str, Any]] = []
    mentions: list[str] = []
    text_parts: list[str] = []
    nodes = 0

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal nodes
        if depth > 8 or nodes >= 200:
            return
        nodes += 1
        if isinstance(node, dict):
            tag = str(node.get("tag") or "")
            prop = node.get("property") if isinstance(node.get("property"), dict) else {}
            if tag in {"plain_text", "text", "markdown", "code", "code_block"}:
                value = prop.get("content") or node.get("content")
                if value:
                    text_parts.append(_clip(value, 4096))
            elif tag == "a":
                href = prop.get("href") or prop.get("url") or prop.get("content")
                if href:
                    text_parts.append(_clip(href, 2048))
                    details.setdefault("links", []).append(_clip(href, 2048))
            elif tag == "at":
                uid = prop.get("userID") or prop.get("user_id") or node.get("userID")
                if uid:
                    mentions.append(_clip(uid, 256))
            key_fields = (
                ("image", "imageKey"), ("media", "mediaKey"), ("file", "fileKey"),
                ("audio", "audioKey"), ("document", "docKey"),
            )
            for kind, field in key_fields:
                key = prop.get(field) or node.get(field)
                if key:
                    _append_resource(resources, _resource(kind, str(key), source=f"card.{field}"))
            for key in ("imageId", "image_id", "mediaId", "media_id", "fileToken", "file_token", "docToken", "doc_token"):
                value = prop.get(key) or node.get(key)
                if value:
                    kind = "image" if "image" in key else "media" if "media" in key else "file" if "file" in key else "document"
                    _append_resource(resources, _resource(kind, str(value), source=f"card.{key}"))
            for child in node.values():
                walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node[:200]:
                walk(child, depth + 1)

    walk(body)
    if text_parts:
        details["text_preview"] = "".join(text_parts)[:16 * 1024]
    if mentions:
        details["mention_count"] = len(set(mentions))
    if "links" in details:
        details["links"] = details["links"][:64]
    return details, resources, sorted(set(mentions))


def _post_resources(content: bytes) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    html = ""
    for field_number, wire_type, value in _wire_fields(content):
        if field_number in (1, 2, 20) and wire_type == 2:
            try:
                candidate = bytes(value).decode("utf-8", errors="replace")
            except Exception:
                continue
            if "<" in candidate or "http" in candidate:
                html = candidate
                break
    for index, match in enumerate(re.finditer(r"<img\b[^>]*\bsrc=['\"]([^'\"]+)", html, re.I)):
        _append_resource(resources, _resource("image", match.group(1), ordinal=index, source="post.img"))
    return resources


def _inline_resources(content: bytes) -> list[dict[str, Any]]:
    """Recover image-like blobs embedded in a post payload when present."""
    resources: list[dict[str, Any]] = []
    for index, (field_number, wire_type, value) in enumerate(_wire_fields(content)):
        if wire_type != 2:
            continue
        raw = bytes(value)
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            kind = "image"
            mime = "image/png"
        elif raw.startswith(b"\xff\xd8\xff"):
            kind = "image"
            mime = "image/jpeg"
        elif raw.startswith((b"GIF87a", b"GIF89a")):
            kind = "image"
            mime = "image/gif"
        elif raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
            kind = "image"
            mime = "image/webp"
        else:
            continue
        key = "inline:" + hashlib.sha256(raw).hexdigest()
        item = _resource(kind, key, ordinal=index, mime=mime, size=len(raw), source="post.inline")
        if item:
            item["_data"] = raw
            item["status"] = "inline"
        _append_resource(resources, item)
    return resources


def _json_nested_payloads(content: bytes) -> list[dict[str, Any]]:
    """Find bounded JSON message lists used by some merge-forward variants."""
    candidates: list[bytes] = []
    raw = content[:_MAX_CONTENT_BYTES]
    candidates.append(raw)
    candidates.extend(bytes(value) for _, wt, value in _wire_fields(raw) if wt == 2 and len(value) <= _MAX_CONTENT_BYTES)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate.decode("utf-8", errors="replace"))
        except Exception:
            continue
        values = parsed.get("messages") if isinstance(parsed, dict) else parsed
        if not isinstance(values, list):
            continue
        result = []
        for item in values[:_MAX_NESTED_MESSAGES]:
            if not isinstance(item, dict):
                continue
            raw_type_value = item.get("type") or item.get("type_code") or 0
            try:
                raw_type = int(raw_type_value)
            except (TypeError, ValueError):
                raw_type = next(
                    (code for code, name in TYPE_NAMES.items() if name == str(raw_type_value).lower()),
                    0,
                )
            text = _clip(item.get("text") or item.get("content") or "", 16 * 1024)
            result.append({
                "id": _clip(item.get("id") or item.get("msg_id") or "", 256),
                "type": {"code": raw_type, "name": type_name(raw_type)},
                "text": text,
                "decode": {"status": "decoded" if text else "opaque", "errors": []},
            })
        if result:
            return result
    return []


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
    """Decode one protobuf message without throwing away unknown content.

    The legacy ``decode_status`` values are retained for existing callers;
    ``semantic_status`` is the schema-2 value exposed by ``MessageEnvelope``.
    Raw content is never returned here.  Resource keys are private in-process
    values consumed by the message-resource provider and removed at output.
    """
    msg_type = int(m.get("type", 0) or 0)
    content = _as_bytes(m.get("content", b""))
    text = ""
    mentions: list[str] = []
    structured: dict[str, Any] = {}
    resources: list[dict[str, Any]] = []
    nested_messages: list[dict[str, Any]] = []
    errors: list[str] = []
    parsed = False

    if len(content) > _MAX_CONTENT_BYTES:
        errors.append("content_size_limit")
        content = content[:_MAX_CONTENT_BYTES]

    try:
        text = decode_text({**m, "content": content}) or ""
    except Exception as exc:
        errors.append(type(exc).__name__)

    if msg_type == 2 and content:
        try:
            _, mentions = decode_type2_content(content)
            for resource in _post_resources(content) + _inline_resources(content):
                _append_resource(resources, resource)
            parsed = bool(text or resources)
        except Exception as exc:
            errors.append(type(exc).__name__)
    elif msg_type == 4 and content:
        details, rich_resources = _richtext_details(content)
        structured.update(details)
        for resource in rich_resources:
            _append_resource(resources, resource)
        try:
            tc = pb.TextContent()
            tc.ParseFromString(content)
            mentions = _mentions_from_richtext(proto_to_dict(tc).get("richText", {}))
            parsed = bool(text or structured or resources)
        except Exception as exc:
            errors.append(type(exc).__name__)
    elif msg_type == 14 and content:
        mentions = _mentions_from_card_json(content)
        body = _card_json(content)
        if body:
            details, card_resources, card_mentions = _card_details(body)
            structured.update(details)
            mentions = sorted(set(mentions + card_mentions))
            for resource in card_resources:
                _append_resource(resources, resource)
        parsed = bool(text or structured or resources)
    elif msg_type == 3 and content:
        # FILE: the vendored Content schema is field-misaligned for this type
        # (it reads the file name as imageKey and the mime as attachments).
        # Use the tenant-verified wire layout instead.
        details, file_resources = _file_details(content)
        structured.update(details)
        for resource in file_resources + _wire_media_resources(content):
            _append_resource(resources, resource)
        parsed = bool(text or structured or resources)
        if structured.get("fileName"):
            text = _clip(structured["fileName"], 512)
    elif msg_type == 5 and content:
        # IMAGE: Content.f2 is a nested descriptor the vendored schema reads
        # as a flat string (decode fails on bad UTF-8).  Wire layout instead.
        details, image_resources = _image_details(content)
        structured.update(details)
        for resource in image_resources + _wire_media_resources(content):
            _append_resource(resources, resource)
        parsed = bool(text or structured or resources)
    elif msg_type == 15 and content:
        # MEDIA (video): wire layout mapped 2026-09-07.
        # f1: file_v3_ key, f2: fileName, f3: fileSize, f4: fileMime,
        # f6: cover image & crypto, f7: duration (ms).
        details, media_resources = _media_details(content)
        structured.update(details)
        for resource in media_resources + _wire_media_resources(content):
            _append_resource(resources, resource)
        parsed = bool(text or structured or resources)
        if structured.get("fileName"):
            text = f"[视频: {structured['fileName']}]"
        elif not text:
            text = "[视频]"
    elif msg_type == 10 and content:
        # STICKER: tenants use both generic Content fields and an image-like
        # type-10 payload.  Keep the key private while exposing enough
        # metadata to distinguish a real sticker from an opaque attachment.
        sticker = parse_sticker_content(content)
        structured.update(public_sticker_details(sticker))
        sticker_key = str(sticker.get("file_key") or "")
        if sticker_key:
            _append_resource(
                resources,
                _resource(
                    "sticker",
                    sticker_key,
                    name=str(sticker.get("sticker_id") or ""),
                    source="content.sticker",
                ),
            )
        elif sticker.get("sticker_set_id") and sticker.get("sticker_id"):
            _append_resource(
                resources,
                _resource(
                    "sticker",
                    f"{sticker['sticker_set_id']}:{sticker['sticker_id']}",
                    name=str(sticker.get("sticker_id") or ""),
                    source="content.sticker",
                ),
            )
        parsed = bool(structured or resources)
    else:
        structured, content_resources, content_parsed, content_errors = _parse_content_proto(content)
        resources.extend(content_resources)
        errors.extend(content_errors)
        parsed = content_parsed
        if msg_type == 11 and content:
            nested_messages = _json_nested_payloads(content)
            parsed = parsed or bool(nested_messages)

    # Some live file messages use a compact file token in Content.text and do
    # not populate Content.fileKey. Promote that token to the verified file
    # resource provider instead of misclassifying it as ordinary text.
    if msg_type in {3, 13}:
        candidate = str(text or structured.get("text") or "").strip()
        if _FILE_TOKEN_RE.fullmatch(candidate):
            kind = "cloud_file" if msg_type == 13 else "file"
            _append_resource(
                resources,
                _resource(
                    kind,
                    candidate,
                    name=str(structured.get("fileName") or ""),
                    mime=str(structured.get("fileMime") or ""),
                    size=int(structured.get("fileSize") or 0),
                    source="content.fileToken",
                ),
            )
            parsed = True

    # Some text-like Content envelopes carry their visible text in Content,
    # not TextContent.  Use it as a fallback for system/card variants only.
    if not text and structured.get("text"):
        text = _clip(structured["text"], 16 * 1024)

    known = msg_type in TYPE_NAMES and msg_type != 0
    if known and (text or parsed):
        semantic_status = "decoded"
    elif known and not content:
        semantic_status = "unavailable"
    else:
        semantic_status = "opaque"

    if msg_type not in _TEXT_TYPES:
        legacy_status = "nontext"
    elif text:
        legacy_status = "ok"
    elif not content:
        legacy_status = "empty"
    else:
        legacy_status = "undecoded"

    result = {
        "text": text,
        "mention_user_ids": sorted(set(mentions)),
        "decode_status": legacy_status,
        "semantic_status": semantic_status,
        "msg_type": msg_type,
        "msg_type_name": type_name(msg_type),
        "structured": structured,
        "resources": resources[:_MAX_RESOURCES],
        "nested_messages": nested_messages[:_MAX_NESTED_MESSAGES],
        "errors": errors[:16],
        "raw_summary": _wire_summary(content),
    }
    # Keep raw content only for type-10 source-copy support.  Retaining every
    # message's full payload would multiply memory use for large histories.
    if msg_type == 10 and content:
        result["_raw_content"] = content
    return result
