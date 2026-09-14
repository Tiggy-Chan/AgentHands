"""Calendar/meeting provider with bounded DOM fallback and write drafts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta

from . import auth, browser_guard, config, transport
from .dom_utils import bounded_inner_text, clip, first_match, lines, normalize_time
from .errors import EndpointNotAdapted, ResourceIsolationUnavailable, SendBlocked


def _calendar_url(domain: str) -> str:
    return config.CALENDAR_URL or f"https://{domain}/calendar/"


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _draft(kind: str, payload: dict) -> dict:
    return {"kind": kind, "status": "draft", "sent": False, "fingerprint": _fingerprint(payload), **payload}


def _require_write(draft: dict, confirm_write: str) -> None:
    if not confirm_write:
        raise SendBlocked("日历写入默认只生成草稿，未执行", hint=json.dumps(draft, ensure_ascii=False))
    if confirm_write != draft["fingerprint"]:
        raise SendBlocked("日历草稿 fingerprint 不匹配，拒绝执行")
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked("日历写入被拦截：需要 FEISHU_ALLOW_WRITE=1")


async def list_events(start_time: int = 0, end_time: int = 0, limit: int = 50) -> dict:
    if not 1 <= limit <= 200:
        raise ValueError("calendar limit 必须在 1..200 之间")
    return await _browser("list", {"start_time": start_time, "end_time": end_time, "limit": limit})


async def list_calendars(my_id: str = "", my_name: str = "") -> dict:
    """List calendar IDs visible in the authenticated calendar page."""
    if not my_id or not my_name:
        me = await auth.prepare()
        my_id = my_id or me.get("user_id", "")
        my_name = my_name or me.get("name", "")
    try:
        return await _browser("calendars", {"my_id": my_id, "my_name": my_name})
    except ResourceIsolationUnavailable:
        # Calendar page loading also emits a read-only 1001001 response.  Use
        # it when the host has no systemd user session for Browser Worker;
        # this keeps automatic calendar selection inside the skill.
        return await _gateway_list_calendars(my_id=my_id, my_name=my_name)


async def resolve_calendar_id(calendar_id: str = "", my_id: str = "", my_name: str = "") -> tuple[str, str]:
    """Resolve explicit ID > environment > the user's managed calendar."""
    explicit = str(calendar_id or "").strip()
    configured = os.environ.get("FEISHU_CALENDAR_ID", "").strip()
    if explicit and explicit.lower() != "auto":
        return explicit, "explicit"
    if configured and configured.lower() != "auto":
        return configured, "environment"

    result = await list_calendars(my_id=my_id, my_name=my_name)
    items = list(result.get("items") or [])
    source = str(result.get("source") or "auto")
    managed = [item for item in items if item.get("managed")]
    if len(managed) == 1:
        return str(managed[0]["calendar_id"]), f"{source}_managed"
    if len(items) == 1:
        return str(items[0]["calendar_id"]), f"{source}_single"
    if managed:
        return str(managed[0]["calendar_id"]), f"{source}_first_managed"
    raise EndpointNotAdapted(
        "未能自动确定本人管理的主日历 ID",
        hint="先运行 feishu.py calendar calendars，或显式提供 --calendar-id",
    )


def _wire_fields(data: bytes):
    """Bounded top-level protobuf walk used for calendar bootstrap replies."""
    i = 0
    count = 0
    while i < len(data) and count < 256:
        count += 1
        try:
            key = 0
            shift = 0
            while True:
                b = data[i]
                i += 1
                key |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            field, wire = key >> 3, key & 7
            if wire == 0:
                value = 0
                shift = 0
                while True:
                    b = data[i]
                    i += 1
                    value |= (b & 0x7F) << shift
                    if not b & 0x80:
                        break
                    shift += 7
                yield field, wire, value
            elif wire == 2:
                length = 0
                shift = 0
                while True:
                    b = data[i]
                    i += 1
                    length |= (b & 0x7F) << shift
                    if not b & 0x80:
                        break
                    shift += 7
                end = i + length
                if end > len(data):
                    return
                yield field, wire, data[i:end]
                i = end
            elif wire == 1:
                if i + 8 > len(data):
                    return
                yield field, wire, data[i:i + 8]
                i += 8
            elif wire == 5:
                if i + 4 > len(data):
                    return
                yield field, wire, data[i:i + 4]
                i += 4
            else:
                return
        except (IndexError, ValueError):
            return


def _ascii_field(fields: list[tuple[int, int, object]], number: int) -> str:
    for field, wire, value in fields:
        if field != number or wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        try:
            text = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.isprintable() and text:
            return text[:256]
    return ""


async def _gateway_list_calendars(my_id: str = "", my_name: str = "") -> dict:
    """Parse the observed read-only calendar bootstrap command 1001001."""
    if not my_id or not my_name:
        me = await auth.prepare()
        my_id = my_id or me.get("user_id", "")
        my_name = my_name or me.get("name", "")
    response = await transport.gateway_call(
        1001001, b"", _new_rid(), header_overrides=_CALENDAR_HEADERS
    )
    return _parse_calendar_bootstrap(response, my_id=my_id, my_name=my_name)


def _parse_calendar_bootstrap(response: bytes, my_id: str = "", my_name: str = "") -> dict:
    """Parse a 1001001 response without returning raw protobuf fields."""
    from lark_bridge.proto import proto_pb2 as pb

    packet = pb.Packet()
    packet.ParseFromString(response)
    if packet.status or packet.payloadType == 5 or not packet.payload:
        raise EndpointNotAdapted("日历 bootstrap 读取被网关拒绝")
    items: list[dict] = []
    seen: set[str] = set()
    for field, wire, value in _wire_fields(packet.payload):
        if field != 1 or wire != 2:
            continue
        nested = list(_wire_fields(bytes(value)))
        calendar_id = _ascii_field(nested, 1)
        owner_id = _ascii_field(nested, 2)
        if not calendar_id or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", calendar_id):
            continue
        if calendar_id in seen:
            continue
        seen.add(calendar_id)
        name = _ascii_field(nested, 15)
        items.append({
            "calendar_id": calendar_id,
            "name": name,
            "managed": bool((my_name and name == my_name) or (my_id and owner_id == my_id)),
            "source": "gateway_1001001",
            "complete": True,
        })
    if not items:
        raise EndpointNotAdapted(
            "日历 bootstrap 响应中未发现可用 calendar ID",
            hint="运行 feishu.py probe --scope calendar 检查当前租户协议",
        )
    return {
        "kind": "calendar_calendars",
        "items": items,
        "total": len(items),
        "source": "gateway_1001001",
        "complete": True,
    }


async def get_event(event_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_:-]{1,256}", event_id):
        raise ValueError("calendar event_id 格式不合法")
    return await _browser("get", {"event_id": event_id})


# --- 日历写接口 wire 编码（2026-09-11 headed 抓包实测登记） -----------------
#
# 创建日程 = gateway cmd 1002003，body 即 Packet{cmd, payload}；payload 内层
# 结构（抓包样本逐字段核对）：
#   f1 "0"  f2 客户端生成的 event UUID  f3 0
#   f4/f5/f6 calendar_id（字符串 ×3）  f7/f8 空  f9..f12 0
#   f15 1  f16 -1(int64)  f17 1  f18 0  f20 0  f21 1  f22 1  f23 0  f24 2
#   f25 start_epoch  f26 时区  f27 end_epoch  f28 时区
#   f29 标题  f30 标题  f31 描述富文本 JSON  f32 {f4: f32 360.0, f5: f32 360.0}
#   f34 {f2 5, f3 3}  f35 0  f36 0
# 创建后客户端紧跟 cmd 1010023 {f1 uuid, f2 calendar_id}（疑似订阅/关联）。

def _ev(v: int) -> bytes:
    """protobuf varint（支持负 int64 的十字节形式）。"""
    if v < 0:
        v += 1 << 64
    out = bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _ev((field << 3) | wire)


def _fv(field: int, v: int) -> bytes:
    return _key(field, 0) + _ev(v)


def _fb(field: int, data: bytes) -> bytes:
    return _key(field, 2) + _ev(len(data)) + data


def _fs(field: int, s: str) -> bytes:
    return _fb(field, s.encode("utf-8"))


def _description_json(text: str) -> str:
    """描述富文本 JSON，形状逐字节对齐 2026-09-11 抓包样本（单 plain 行）。

    apool 必须带 1 个 author + 7 个随机 lineguid（nextNum=8）：抓包样本里
    单行描述也是这个形状，attribs 的 *1 引用要能在 numToAttrib 里解析，
    最小化 apool 会被网关以 payloadType=5 拒绝（实测）。
    """
    import random
    import string

    line = " ".join(text.split())  # 只支持单行；多行压平
    body = (line + "\n") if line else ""
    attrib = f"*1+{len(line)}|1+{len(body) - len(line)}" if line else "*0|1+0"
    guids = ["".join(random.choices(string.ascii_letters + string.digits, k=6))
             for _ in range(7)]
    num_to_attrib = {"0": ["author", "0"]}
    num_to_attrib.update({str(i + 1): ["lineguid", g] for i, g in enumerate(guids)})
    return json.dumps({
        "initialAttributedText": {"text": body, "attribs": attrib},
        "initialAttributedTexts": {
            "rows": {}, "cols": {},
            "text": {"0": body}, "attribs": {"0": attrib},
        },
        "apool": {"numToAttrib": num_to_attrib, "nextNum": 8},
        "style": {},
    }, ensure_ascii=False, separators=(",", ":"))


def _timezone_for_input(value: str) -> str:
    """Use an explicit input timezone when available, else tenant default."""
    configured = os.environ.get("FEISHU_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return configured
    if parsed.tzinfo is None:
        return configured
    key = str(getattr(parsed.tzinfo, "key", "") or "")
    if key:
        return key
    offset = parsed.utcoffset()
    if offset is None:
        return configured
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    if sign == "+" and hours == 8 and minutes == 0:
        return "Asia/Shanghai"
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _build_create_payload(calendar_id: str, title: str, start_epoch: int,
                          end_epoch: int, tz: str = "Asia/Shanghai",
                          description: str = "") -> tuple[bytes, str]:
    """构造 cmd 1002003 payload；返回 (payload, client_event_uuid)。"""
    import struct
    import uuid as _uuid

    event_uuid = str(_uuid.uuid4())
    inner = b""
    inner += _fs(1, "0")
    inner += _fs(2, event_uuid)
    inner += _fv(3, 0)
    for f in (4, 5, 6):
        inner += _fs(f, calendar_id)
    inner += _fb(7, b"") + _fb(8, b"")
    for f in (9, 10, 11, 12):
        inner += _fv(f, 0)
    inner += _fv(15, 1)
    inner += _fv(16, -1)
    inner += _fv(17, 1) + _fv(18, 0) + _fb(20, b"")
    inner += _fv(21, 1) + _fv(22, 1) + _fv(23, 0) + _fv(24, 2)
    inner += _fv(25, start_epoch)
    inner += _fs(26, tz)
    inner += _fv(27, end_epoch)
    inner += _fs(28, tz)
    inner += _fs(29, title)
    inner += _fs(30, title)
    inner += _fs(31, _description_json(description))
    f32 = _fb(1, b"") + _fb(2, b"")
    f32 += _key(4, 5) + struct.pack("<f", 360.0)
    f32 += _key(5, 5) + struct.pack("<f", 360.0)
    inner += _fb(32, f32)
    inner += _fb(34, _fb(1, b"") + _fv(2, 5) + _fv(3, 3))
    inner += _fv(35, 0) + _fv(36, 0)
    return _fb(1, inner), event_uuid


def _build_attach_payload(event_uuid: str, calendar_id: str) -> bytes:
    """cmd 1010023：创建后紧跟的关联包（抓包样本 f1 uuid / f2 calendar_id / f3 0）。"""
    return _fb(1, _fs(1, event_uuid) + _fs(2, calendar_id) + _fv(3, 0))


def _parse_create_reply(resp: bytes, event_uuid: str) -> tuple[str, str]:
    """解析 1002003 应答 → (status, detail)。

    2026-09-11 实测应答形态：
    - 成功：payloadType=1 的短 ack（回显 cid，不回显 event uuid）
    - 幂等去重：payloadType=0 + f1 错误串 "event already exist"（视为成功）
    - 拒绝：payloadType=5 + 空 payload（边缘 401，多为会话未绑定/过期）
    """
    try:
        from lark_bridge.proto import proto_pb2 as pb

        pkt = pb.Packet()
        pkt.ParseFromString(resp)
        err = ""
        try:
            err = pkt.payload.decode("utf-8", "replace") if pkt.payload else ""
        except Exception:
            err = ""
        # f1 错误串在有些应答里直接落在 payload 字段上
        if "already exist" in err:
            return "created", "dedup: event already exist"
        if pkt.payloadType == 1:
            return "created", "ack"
        if pkt.payloadType == 5:
            return "rejected", "payloadType=5（边缘拒绝；多半是请求未带登录态）"
        # 兜底：原始字节里找错误串
        raw = resp.decode("utf-8", "replace")
        if "already exist" in raw:
            return "created", "dedup: event already exist"
        if "failed" in raw or "err" in raw[:120].lower():
            return "rejected", raw[:200]
        return "sent_unverified", f"payloadType={pkt.payloadType}"
    except Exception:
        raw = resp.decode("utf-8", "replace")
        if "already exist" in raw:
            return "created", "dedup: event already exist"
        return "sent_unverified", f"unparsed {len(resp)}B"


async def _verify_created_event(
    calendar_id: str,
    event_uuid: str,
    title: str,
    start_epoch: int,
    end_epoch: int,
) -> tuple[bool, str, str, str]:
    """Use the bounded gateway sync to verify a successful create ack."""
    try:
        synced = await sync_events(calendar_id)
    except Exception as exc:
        return False, "gateway_1002002", f"sync_failed_{type(exc).__name__}", ""
    for item in synced.get("items", []):
        if item.get("uuid") == event_uuid:
            return True, "gateway_1002002", "matched_uuid", str(item.get("server_id") or "")
    # Some tenant responses omit the client UUID.  Match all immutable fields
    # before accepting a server record as the newly created event.
    candidates = [
        item for item in synced.get("items", [])
        if item.get("title") == title
        and int(item.get("start") or 0) == int(start_epoch)
        and int(item.get("end") or 0) == int(end_epoch)
    ]
    if len(candidates) == 1:
        return True, "gateway_1002002", "matched_title_time", str(candidates[0].get("server_id") or "")
    return False, "gateway_1002002", "created_event_not_found", ""


async def create_event(title: str, start: str, end: str, confirm_write: str = "",
                       calendar_id: str = "", description: str = "",
                       my_id: str = "", my_name: str = "") -> dict:
    from .output import parse_when

    if not all(str(value).strip() for value in (title, start, end)):
        raise ValueError("calendar create 需要标题、开始和结束时间")
    start_ms, end_ms = parse_when(start), parse_when(end)
    if not start_ms or not end_ms or end_ms <= start_ms:
        raise ValueError("calendar create 的结束时间必须晚于开始时间")
    calendar_id, calendar_source = await resolve_calendar_id(
        calendar_id, my_id=my_id, my_name=my_name
    )
    timezone_name = _timezone_for_input(start)
    payload = {"title": title, "start": start, "end": end,
               "calendar_id": calendar_id, "description": description,
               "timezone": timezone_name}
    draft = _draft("calendar_create", payload)
    _require_write(draft, confirm_write)

    wire, event_uuid = _build_create_payload(
        calendar_id, title, start_ms // 1000, end_ms // 1000,
        tz=timezone_name,
        description=description)
    # 一次创建 = 两个 gateway 请求（1002003 + 1010023），而传输层每次写检查
    # 都会走闸门——用 operation 作用域包住整个创建动作（fingerprint+env 已验）。
    async with transport.allow_write_operation():
        resp = await transport.gateway_call(1002003, wire, _new_rid(), header_overrides=_CALENDAR_HEADERS)
        if os.environ.get("FEISHU_DEBUG_CALENDAR") == "1":
            try:
                (config.HOME / ".last-calendar-create-resp.bin").write_bytes(resp[:8192])
            except Exception:
                pass
        status, detail = _parse_create_reply(resp, event_uuid)
        # 抓包中客户端无论结果都紧跟 1010023 关联包，照做。
        try:
            await transport.gateway_call(1010023, _build_attach_payload(event_uuid, calendar_id), _new_rid(), header_overrides=_CALENDAR_HEADERS)
            attached = True
        except Exception:
            attached = False
    verified = False
    verification_source = ""
    verification_detail = "not_attempted"
    server_id = ""
    if status == "created":
        verified, verification_source, verification_detail, server_id = await _verify_created_event(
            calendar_id, event_uuid, title, start_ms // 1000, end_ms // 1000
        )
    return {
        "kind": "calendar_create", "status": status,
        "sent": status == "created", "request_sent": True,
        "fingerprint": draft["fingerprint"],
        "event_uuid": event_uuid, "calendar_id": calendar_id,
        "calendar_source": calendar_source,
        "attach_ack": attached if status == "created" else None,
        "detail": detail,
        "verified": verified,
        "verification_source": verification_source,
        "verification_detail": verification_detail,
        "server_id": server_id,
        "timezone": timezone_name,
        "title": title, "start_epoch": start_ms // 1000, "end_epoch": end_ms // 1000,
    }


def _new_rid() -> str:
    return hashlib.md5(f"calendar-create-{os.getpid()}-{id(object())}".encode()).hexdigest()[:10]


# 日历 SPA 的 gateway 头指纹（2026-09-11 capture7 实测）。空字符串 = 真实客户端
# 没有该头，直接移除。注：Messenger 默认头实测也能过边缘校验（此前认为的
# 「错版头被 payloadType=5 拒」实为未绑会话的 401），此处仍保持真实指纹。
_CALENDAR_HEADERS = {
    "locale": "zh_CN",
    "x-command-version": "2.7.0",
    "x-lgw-os-type": "2",
    "x-lgw-req-sdk-type": "",
    "x-lsc-bizid": "3",
    "x-lsc-terminal": "web",
    "x-web-version": "3.40.0+1a01f3756ef",
}


# --- 删除 / 增量同步（2026-09-11 同批抓包登记） ------------------------------
#
# 删除日程 = cmd 1002005，payload = f1 calendar_id, f2 客户端 event uuid,
# f3 0, f4 0, f5 start_epoch（f5 用于定位重复日程的具体实例）。
# 成功应答 = payloadType=1 短 ack（同创建）。
#
# 增量同步 = cmd 1002002（读），payload = f1{ f1 calendar_id, f2 微秒时间戳
# 字符串, f3 上限(100) }；响应 f5.f1.f2 里重复 f1 事件记录：f1 服务端 id、
# f2 客户端 uuid、f25 start、f27 end、f29 标题。


def _build_delete_payload(calendar_id: str, event_uuid: str, start_epoch: int) -> bytes:
    return (_fs(1, calendar_id) + _fs(2, event_uuid)
            + _fv(3, 0) + _fv(4, 0) + _fv(5, start_epoch))


def _build_sync_payload(calendar_id: str, since_micros: int = 0, limit: int = 100) -> bytes:
    return _fb(1, _fs(1, calendar_id) + _fs(2, str(since_micros)) + _fv(3, limit))


def _parse_sync_events(resp: bytes) -> list[dict]:
    """从 1002002 响应提取事件记录（schema-less 字段遍历，只取已确认字段）。"""
    from lark_bridge.proto import proto_pb2 as pb

    def walk(buf: bytes, depth: int = 0):
        i = 0
        while i < len(buf):
            key = 0
            shift = 0
            while True:
                b = buf[i]
                i += 1
                key |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            field, wire = key >> 3, key & 7
            if wire == 0:
                v = 0
                shift = 0
                while True:
                    b = buf[i]
                    i += 1
                    v |= (b & 0x7F) << shift
                    if not b & 0x80:
                        break
                    shift += 7
                yield field, wire, v
            elif wire == 2:
                ln = 0
                shift = 0
                while True:
                    b = buf[i]
                    i += 1
                    ln |= (b & 0x7F) << shift
                    if not b & 0x80:
                        break
                    shift += 7
                yield field, wire, buf[i:i + ln]
                i += ln
            elif wire == 5:
                yield field, wire, buf[i:i + 4]
                i += 4
            else:
                return

    pkt = pb.Packet()
    pkt.ParseFromString(resp)
    events: list[dict] = []
    # 事件记录特征：同时含 19 位服务端 id (f1) 和 36 字符 uuid (f2) 和
    # 标题 (f29) 的子消息。直接在 payload 里找所有这样的记录块。
    def scan(buf: bytes) -> None:
        fields = {}
        try:
            for f, w, v in walk(buf):
                fields.setdefault(f, []).append((w, v))
        except Exception:
            return
        f1s = fields.get(1, [])
        f2s = fields.get(2, [])
        if f29 := fields.get(29):
            rec = {"server_id": "", "uuid": "", "title": "", "start": 0, "end": 0}
            for w, v in f1s:
                if w == 2 and v.isdigit() and len(v) == 19:
                    rec["server_id"] = v.decode()
            for w, v in f2s:
                if w == 2 and len(v) == 36 and v.count(b"-") == 4:
                    rec["uuid"] = v.decode()
            try:
                rec["title"] = f29[0][1].decode()
            except Exception:
                pass
            for fname, key in ((25, "start"), (27, "end")):
                for w, v in fields.get(fname, []):
                    if w == 0:
                        rec[key] = v
            if rec["uuid"] or rec["server_id"]:
                events.append(rec)
        # 递归扫描子消息
        for f, vals in fields.items():
            for w, v in vals:
                if w == 2 and len(v) > 8 and not (
                        v[:1].isalpha() or all(32 <= c < 127 for c in v[:16])):
                    try:
                        scan(v)
                    except Exception:
                        pass

    scan(pkt.payload)
    # 去重（f37/f38 里的 uuid 不含标题，不会误收）
    seen, out = set(), []
    for e in events:
        k = (e["server_id"], e["uuid"])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


async def sync_events(calendar_id: str = "", since_micros: int = 0,
                      my_id: str = "", my_name: str = "") -> dict:
    """cmd 1002002 增量同步（读）。since_micros=0 拉全量可见事件。"""
    calendar_id, calendar_source = await resolve_calendar_id(
        calendar_id, my_id=my_id, my_name=my_name
    )
    resp = await transport.gateway_call(
        1002002, _build_sync_payload(calendar_id, since_micros),
        _new_rid(), header_overrides=_CALENDAR_HEADERS)
    items = _parse_sync_events(resp)
    return {
        "kind": "calendar_sync", "items": items, "total": len(items),
        "calendar_id": calendar_id, "calendar_source": calendar_source,
        "source": "gateway_1002002", "complete": True,
    }


async def delete_event(event_uuid: str, start_epoch: int, confirm_write: str = "",
                       calendar_id: str = "") -> dict:
    """cmd 1002005 删除日程（双闸门）。start_epoch 必须与创建时一致。"""
    if not re.fullmatch(r"[0-9a-f-]{36}", event_uuid or ""):
        raise ValueError("calendar delete 需要创建时返回的 event_uuid")
    if not start_epoch:
        raise ValueError("calendar delete 需要事件的 start_epoch")
    calendar_id, calendar_source = await resolve_calendar_id(calendar_id)
    payload = {"event_uuid": event_uuid, "start_epoch": start_epoch,
               "calendar_id": calendar_id}
    draft = _draft("calendar_delete", payload)
    _require_write(draft, confirm_write)
    if not calendar_id:
        raise ValueError("缺少 calendar_id（--calendar-id 或 FEISHU_CALENDAR_ID）")
    async with transport.allow_write_operation():
        resp = await transport.gateway_call(
            1002005, _build_delete_payload(calendar_id, event_uuid, start_epoch),
            _new_rid(), header_overrides=_CALENDAR_HEADERS)
    status, detail = _parse_create_reply(resp, event_uuid)
    return {
        "kind": "calendar_delete",
        "status": "deleted" if status == "created" else status,
        "sent": status == "created", "request_sent": True,
        "fingerprint": draft["fingerprint"],
        "event_uuid": event_uuid, "calendar_id": calendar_id,
        "calendar_source": calendar_source,
        "start_epoch": start_epoch, "detail": detail,
    }


async def update_event(event_id: str, title: str = "", start: str = "", end: str = "", confirm_write: str = "") -> dict:
    if not event_id or not any((title, start, end)):
        raise ValueError("calendar update 需要 event_id 和至少一个变更字段")
    payload = {"event_id": event_id, "title": title, "start": start, "end": end}
    draft = _draft("calendar_update", payload)
    _require_write(draft, confirm_write)
    transport.arm_write_once()
    from lark_bridge import _http
    _http.require_write_allowed()
    raise EndpointNotAdapted("日历修改写接口尚未适配；草稿未执行", hint="先运行 feishu.py probe --scope calendar")


async def _browser(action: str, payload: dict) -> dict:
    from .browser_worker import run

    return await run(f"calendar-{action}", payload)


async def browser_operation(action: str, payload: dict) -> dict:
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("日历网页读取只能由受限 Browser Worker 执行")
    if action not in {"list", "get", "calendars"}:
        raise EndpointNotAdapted(f"日历操作未适配：{action}")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise EndpointNotAdapted("当前环境没有 playwright，无法读取日历") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=True,
            ignore_https_errors=False,
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=config.BROWSER_ARGS,
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await ctx.add_cookies(auth.playwright_cookie_jar())
            blocked = await browser_guard.install(page)
            await page.goto(_calendar_url(domain), wait_until="domcontentloaded", timeout=60000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(8000)
            if action == "calendars":
                return await _calendar_rows(
                    page,
                    my_id=str(payload.get("my_id") or ""),
                    my_name=str(payload.get("my_name") or ""),
                    blocked_count=len(blocked),
                )
            rows = await _event_rows(page, int(payload.get("limit", 50)), len(blocked))
            if action == "get":
                return await _event_detail(page, str(payload.get("event_id", "")), rows)
            rows = _filter_events(
                rows, int(payload.get("start_time", 0) or 0), int(payload.get("end_time", 0) or 0)
            )
            return {
                "kind": "calendar_list",
                "items": rows,
                "total": len(rows),
                "source": "browser_dom",
                "complete": False,
                "filters_applied": bool(payload.get("start_time") or payload.get("end_time")),
            }
        finally:
            await ctx.close()


async def _calendar_rows(page, my_id: str = "", my_name: str = "", blocked_count: int = 0) -> dict:
    """Extract visible calendar IDs from calendar controls, not event cards."""
    locator = page.locator("[data-calendar-id]")
    count = min(await locator.count(), config.MAX_BROWSER_CARDS)
    items: list[dict] = []
    event_fallbacks: dict[str, dict] = {}
    seen: set[str] = set()
    for index in range(count):
        row = locator.nth(index)
        item = await row.evaluate(
            """e => {
              const r = e.getBoundingClientRect();
              return {
                id: e.getAttribute('data-calendar-id') || '',
                text: (e.innerText || '').slice(0, 512),
                aria: e.getAttribute('aria-label') || '',
                title: e.getAttribute('title') || '',
                cls: String(e.className || ''),
                visible: r.width > 0 && r.height > 0
              };
            }"""
        )
        calendar_id = str(item.get("id") or "").strip()
        if not calendar_id or calendar_id in seen or not item.get("visible"):
            continue
        seen.add(calendar_id)
        cls = str(item.get("cls") or "")
        text = str(item.get("text") or "").strip()
        # Event instances carry the same calendar ID, but their class/text
        # describes an event rather than a calendar control.  Prefer sidebar
        # controls; retain an event-only ID as a last-resort unnamed calendar.
        is_event = "event-instance" in cls or "event-instance" in text.lower()
        if is_event:
            event_fallbacks.setdefault(calendar_id, {
                "calendar_id": calendar_id,
                "name": "",
                "managed": False,
                "source": "browser_dom_event_fallback",
                "complete": False,
            })
            continue
        name = str(item.get("aria") or item.get("title") or text).splitlines()[0].strip()
        managed = bool(
            (my_name and name == my_name)
            or "calendar-item" in cls
            or "calendars-group" in cls and "subscribed" not in cls.lower()
        )
        items.append({
            "calendar_id": calendar_id,
            "name": name,
            "managed": managed,
            "source": "browser_dom",
            "complete": False,
        })
    if not items:
        items = list(event_fallbacks.values())
    if not items:
        raise EndpointNotAdapted(
            "日历页未发现可用的 calendar ID",
            hint=f"请运行 feishu.py probe --scope calendar 检查当前租户网页结构（blocked={blocked_count}）",
        )
    # If the page exposes the same ID on an event and a sidebar control, the
    # first control metadata wins; otherwise a single event-only ID is still
    # safe to use because no alternate calendar was visible.
    return {
        "kind": "calendar_calendars",
        "items": items,
        "total": len(items),
        "source": "browser_dom",
        "complete": False,
    }


async def _event_rows(page, limit: int, blocked_count: int = 0) -> list[dict]:
    selectors = (
        "[data-event-id]", "[data-calendar-event-id]", "[data-event-token]",
        ".event-instance[data-key]", "[class*='event-instance'][data-key]",
        ".event-instance",
    )
    rows: list[dict] = []
    seen: set[str] = set()
    for selector in selectors:
        locator = page.locator(selector)
        count = min(await locator.count(), config.MAX_BROWSER_CARDS)
        for i in range(count):
            row = locator.nth(i)
            item = await row.evaluate(
                """e => ({
                  id: e.getAttribute('data-event-id') || e.getAttribute('data-calendar-event-id') ||
                     e.getAttribute('data-event-token') || e.getAttribute('data-key') || '',
                  startAttr: e.getAttribute('data-start-time') || '',
                  endAttr: e.getAttribute('data-end-time') || '',
                  text: (e.innerText || '').slice(0, 4096),
                  start: e.querySelector('time[datetime]')?.getAttribute('datetime') || '',
                  href: e.querySelector('a')?.getAttribute('href') || ''
                })"""
            )
            event_id = clip(item.get("id", ""), 256)
            if not event_id:
                match = re.search(r"(?:event|calendar)[/_-]([A-Za-z0-9_:-]{4,})", item.get("href", ""))
                event_id = clip(match.group(1), 256) if match else ""
            text = clip(item.get("text", ""))
            text_lines = lines(text)
            if not event_id:
                canonical = json.dumps(
                    [text_lines[0] if text_lines else "", item.get("startAttr", ""), item.get("endAttr", "")],
                    ensure_ascii=False, separators=(",", ":")
                ).encode()
                event_id = "ui:" + hashlib.sha256(canonical).hexdigest()[:24]
                id_kind = "ui_fingerprint"
            else:
                id_kind = "dom_id"
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            start = normalize_time(item.get("start") or item.get("startAttr") or first_match(text, (r"开始[：:]\s*(.+)$",)))
            end = normalize_time(item.get("endAttr") or first_match(text, (r"结束[：:]\s*(.+)$",)))
            if not end:
                end = _end_from_title(text, start)
            rows.append({
                "id": event_id,
                "id_kind": id_kind,
                "title": text_lines[0] if text_lines else "",
                "start": start,
                "end": end,
                "timezone": "",
                "status": "",
                "organizer": first_match(text, (r"组织者[：:]\s*(.+)$", r"Organizer[：:]\s*(.+)$")),
                "attendees": [],
                "location": first_match(text, (r"地点[：:]\s*(.+)$", r"Location[：:]\s*(.+)$")),
                "description": clip(" ".join(text_lines[1:]), config.MAX_BROWSER_TEXT_BYTES),
                "recurrence": "",
                "source": "browser_dom",
                "complete": False,
            })
            if len(rows) >= limit:
                return rows
        if rows:
            break
    if not rows:
        # An otherwise-rendered calendar with no event cards is a valid empty
        # result.  Keep endpoint failures distinct from an empty date view.
        shell = await page.locator(".calendars, .calendar-view, [data-calendar-id]").count()
        if shell:
            return []
        raise EndpointNotAdapted(
            "日历页面未发现带稳定 id 的会议项",
            hint=f"运行 feishu.py probe --scope calendar 检查当前租户网页结构（blocked={blocked_count}）",
        )
    return rows


def _end_from_title(text: str, start: str) -> str:
    """Recover an end time from the visible ``HH:MM - HH:MM`` title."""
    match = re.search(r"(\d{1,2}):(\d{2})\s*[-~至]\s*(\d{1,2}):(\d{2})", text)
    if not match or not start:
        return ""
    try:
        base = datetime.fromisoformat(start)
        return base.replace(hour=int(match.group(3)), minute=int(match.group(4)), second=0, microsecond=0).isoformat()
    except ValueError:
        return ""


def _filter_events(rows: list[dict], start_time: int, end_time: int) -> list[dict]:
    if not start_time and not end_time:
        return rows
    out = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row.get("start", "")).timestamp()
        except (TypeError, ValueError, OverflowError):
            out.append(row)
            continue
        if start_time and stamp < start_time:
            continue
        if end_time and stamp > end_time:
            continue
        out.append(row)
    return out


async def _event_detail(page, event_id: str, rows: list[dict]) -> dict:
    locator = None
    selectors = (
        "[data-event-id]", "[data-calendar-event-id]", "[data-event-token]",
        ".event-instance[data-key]", "[class*='event-instance'][data-key]", ".event-instance",
    )
    for selector in selectors:
        candidate = page.locator(selector)
        count = min(await candidate.count(), config.MAX_BROWSER_CARDS)
        for i in range(count):
            row = candidate.nth(i)
            item = await row.evaluate(
                """e => ({
                  id: e.getAttribute('data-event-id') || e.getAttribute('data-calendar-event-id') ||
                     e.getAttribute('data-event-token') || e.getAttribute('data-key') || '',
                  start: e.getAttribute('data-start-time') || '',
                  end: e.getAttribute('data-end-time') || '',
                  text: (e.innerText || '').slice(0, 4096)
                })"""
            )
            raw = clip(item.get("id", ""), 256)
            if not raw:
                text_lines = lines(item.get("text", ""))
                canonical = json.dumps(
                    [text_lines[0] if text_lines else "", item.get("start", ""), item.get("end", "")],
                    ensure_ascii=False, separators=(",", ":")
                ).encode()
                raw = "ui:" + hashlib.sha256(canonical).hexdigest()[:24]
            if raw == event_id:
                locator = row
                break
        if locator is not None:
            break
    if locator is None:
        raise EndpointNotAdapted("会议详情项未能在当前页面定位")
    await locator.click()
    await page.wait_for_timeout(1000)
    detail = next((item for item in rows if item["id"] == event_id), {"id": event_id})
    main = page.locator(
        "[role=dialog], [class*='event-detail'], [role=main], main"
    ).first
    if await main.count():
        detail["description"] = clip(await bounded_inner_text(main), config.MAX_BROWSER_TEXT_BYTES * 8)
    detail["kind"] = "calendar_event"
    return detail


def render_markdown(result: dict) -> str:
    if result.get("kind") == "calendar_calendars":
        lines_out = [
            f"# 可用日历（{result.get('total', 0)}，来源 {result.get('source', '?')}，完整 {result.get('complete', False)}）"
        ]
        for item in result.get("items", []):
            managed = "本人管理" if item.get("managed") else "订阅/共享"
            lines_out.append(
                f"- `{item.get('calendar_id', '?')}` **{item.get('name') or '(未命名)'}**（{managed}）"
            )
        return "\n".join(lines_out)
    if result.get("kind") == "calendar_event":
        lines_out = [f"# 会议：{result.get('title') or result.get('id', '?')}"]
        for key, label in (("start", "开始"), ("end", "结束"), ("timezone", "时区"),
                           ("organizer", "组织者"), ("location", "地点"), ("status", "状态")):
            if result.get(key):
                lines_out.append(f"- {label}：{result[key]}")
        lines_out.append(f"- 来源：{result.get('source', '?')}｜完整：{result.get('complete', False)}")
        if result.get("description"):
            lines_out.append("\n## 描述（外部数据）\n\n" + str(result["description"]))
        return "\n".join(lines_out)
    rows = result.get("items", [])
    lines_out = [f"# 日历（{len(rows)}，来源 {result.get('source', '?')}，完整 {result.get('complete', False)}）"]
    for item in rows:
        lines_out.append(
            f"- `{item.get('id', '?')}` **{item.get('title') or '(无标题)'}** "
            f"{item.get('start') or ''} ~ {item.get('end') or ''}"
        )
    return "\n".join(lines_out)
