"""The single choke point for every HTTP request the feishu skill makes.

Policy (enforced here, not by convention):

- READ gateway cmds (8 fetch / 64 group name / 5023 user name / 11021 search)
  are allowed.
- cmd=5 (send message) is gated: FEISHU_ALLOW_SEND=1 must be set in the
  environment AND the send module arms the gate for exactly one request.
- cmd=13 (create chat) and every unregistered cmd are denied.
- REST: only the documented read paths plus the read-semantic export-create
  POST are allowed; everything else is denied.
- TLS verification is always on; FEISHU_CA_BUNDLE selects a corporate CA.
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from lark_bridge import _http
from lark_bridge._urls import INTERNAL_API, DRIVE_STREAM, LOGIN_HOST, USER_AGENT

from . import config
from .errors import PolicyViolation, SendBlocked
from .output import note, scrub, scrubbed_exc

# 1001001 is emitted by the calendar SPA while loading the calendar list; it
# is read-only and is used as a bounded fallback when a Browser Worker is not
# available for calendar ID discovery.
READ_GATEWAY_CMDS = frozenset({8, 64, 5023, 11021, 1001001, 1002002, 1002006})
SEND_CMD = 5
# Calendar write commands captured live 2026-09-11 (headed --capture, user
# created test events by hand): 1002003 = create event, 1010023 = follow-up
# attach/subscribe referencing the same client event UUID.
# 1002005 = delete event (same capture batch, user deleted test events):
# payload f1=calendar_id f2=client event uuid f3=0 f4=0 f5=start_epoch.
WRITE_GATEWAY_CMDS = frozenset({1002003, 1002005, 1010023})
DENIED_GATEWAY_CMDS = frozenset({13})  # create chat — never automated

_INTERNAL_HOST = urlsplit(INTERNAL_API).netloc
_DRIVE_STREAM_HOST = urlsplit(DRIVE_STREAM).netloc
_LOGIN_HOST = urlsplit(LOGIN_HOST).netloc

_ALLOW_SEND_ENV = "FEISHU_ALLOW_SEND"
_LEGACY_POST_IMAGE_HOST_RE = re.compile(r"^p\d+\.pstatp\.com$", re.IGNORECASE)
_LEGACY_POST_IMAGE_PATH_RE = re.compile(r"^/origin/[A-Za-z0-9._-]{8,512}$")

# One-shot arming: send.py arms, transport disarms after the first cmd=5.
_send_armed = False
_direct_send_armed = False
_write_armed = False
_direct_write_armed = False
_write_depth = 0

# Bound session for the current process (set by the CLI after auth load).
_cookie_header = ""
_csrf_token = ""
_domain = config.DOMAIN


def configure() -> None:
    """Install TLS policy and the send gate into the vendor seam."""
    _http.configure(verify=config.CA_BUNDLE or True)
    _http.install_send_gate(_send_gate_check)
    _http.install_write_gate(_write_gate_check)
    _http.install_request_policy(check_allowed)


def bind_session(cookie_header: str, csrf_token: str = "", domain: str = "") -> None:
    global _cookie_header, _csrf_token, _domain
    _cookie_header = cookie_header
    _csrf_token = csrf_token
    if domain:
        _domain = domain


def is_legacy_post_image_url(url: str) -> bool:
    """Recognize the tenant's legacy rich-text post image URL shape.

    These URLs come from a message's ``post.img`` HTML, not from an
    ``img_v3_`` key. Enforce HTTPS strictly so images downloaded for OCR or
    agent processing cannot be tampered with on a cleartext connection.
    """
    parsed = urlsplit(str(url or ""))
    return bool(
        parsed.scheme == "https"
        and _LEGACY_POST_IMAGE_HOST_RE.fullmatch(parsed.netloc)
        and _LEGACY_POST_IMAGE_PATH_RE.fullmatch(parsed.path)
        and not parsed.query
        and not parsed.fragment
    )


def arm_send_once() -> None:
    global _send_armed
    _send_armed = True


def arm_direct_send_once() -> None:
    """Arm one explicitly requested direct send without the draft flow.

    The CLI exposes this only behind ``send --direct``.  It remains one-shot
    and is deliberately separate from the normal confirmation gate so a
    direct test cannot silently weaken ordinary sends.
    """
    global _send_armed, _direct_send_armed
    _send_armed = True
    _direct_send_armed = True


def arm_write_once() -> None:
    """Arm exactly one explicitly confirmed mail/calendar/doc write."""
    global _write_armed
    _write_armed = True


def arm_direct_write_once() -> None:
    """Arm one explicitly requested direct write without the draft flow."""
    global _write_armed, _direct_write_armed
    _write_armed = True
    _direct_write_armed = True


@asynccontextmanager
async def allow_write_operation(direct: bool = False):
    """Context manager scoping an authorized multi-request write operation."""
    global _write_depth, _direct_write_armed
    _write_depth += 1
    old_direct = _direct_write_armed
    if direct:
        _direct_write_armed = True
    try:
        yield
    finally:
        _write_depth -= 1
        _direct_write_armed = old_direct


def _write_gate_check() -> None:
    global _write_armed, _direct_write_armed, _write_depth
    # The env var has to be on the very command that writes: whoever approves that command
    # sees it, and it does not outlive the command. --direct skips only the fingerprint check.
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked(
            "写入被拦截：需要环境变量 FEISHU_ALLOW_WRITE=1",
            hint="--direct 只免指纹核对；这道闸门没有任何命令行旗标能绕过",
        )
    if _write_depth > 0:
        return
    if not _write_armed and not _direct_write_armed:
        raise SendBlocked("写入被拦截：当前进程没有为本次请求武装一次性闸门")
    if not _direct_write_armed:
        _write_armed = False


def _send_gate_check() -> None:
    global _send_armed, _direct_send_armed
    if os.environ.get(_ALLOW_SEND_ENV) != "1":
        raise SendBlocked(
            "发送被拦截：需要环境变量 FEISHU_ALLOW_SEND=1",
            hint="--direct 只免指纹核对；这道闸门没有任何命令行旗标能绕过",
        )
    if not _send_armed:
        raise SendBlocked("发送被拦截：send 模块未为本次请求武装闸门")
    _send_armed = False  # exactly one send per arming
    _direct_send_armed = False


def classify(method: str, url: str, gateway_cmd: int = 0) -> str:
    """→ 'allow' | 'send' | 'deny'. Pure function, unit-tested."""
    method = method.upper()
    p = urlsplit(url)
    host, path = p.netloc, p.path

    if host == _INTERNAL_HOST:
        if path == "/accounts/csrf" and method == "POST":
            return "allow"
        if path == "/accounts/web/user" and method == "GET":
            return "allow"
        if path == "/im/gateway/" and method == "POST":
            if gateway_cmd in READ_GATEWAY_CMDS:
                return "allow"
            if gateway_cmd == SEND_CMD:
                return "send"
            if gateway_cmd in WRITE_GATEWAY_CMDS:
                return "write"
            return "deny"
        return "deny"

    if host == _LOGIN_HOST:
        if path.startswith("/suite/passport/frontier_ticket") and method == "GET":
            return "allow"
        return "deny"

    if host == _DRIVE_STREAM_HOST:
        if path.startswith("/space/api/box/stream/download/") and method == "GET":
            return "allow"
        if path.startswith("/space/api/box/stream/upload/") and method == "POST":
            return "write"
        return "deny"

    # Message media (image/audio/video keys) — route shape observed from the
    # real web client and verified with a live key on 2026-09-07
    # (media-observe probe: 200 image/jpeg for an own-message img_v3 key).
    if host == "internal-api-lark-file.feishu.cn":
        if path.startswith("/static-resource/v1/") and method == "GET":
            return "allow"
        return "deny"

    # Rich-text post images in older messages carry a rendered public CDN URL
    # instead of an img_v3_ key. Only this exact HTTPS GET shape is allowed;
    # callers use a no-cookie header set for it.
    if method == "GET" and is_legacy_post_image_url(url):
        return "allow"

    if host == _domain or host == config.DOMAIN:
        if method == "GET" and (
            path.startswith("/space/api/explorer/")
            or path.startswith("/space/api/wiki/")
            or path.startswith("/space/api/export/result/")
            or path.startswith("/space/api/import/result/")
        ):
            return "allow"
        # Export creation is a read in spirit: it renders a document the
        # account can already see into a downloadable file.
        if method == "POST" and path == "/space/api/export/create/":
            return "allow"
        if method == "POST" and (
            path.startswith("/space/api/box/upload/")
            or path == "/space/api/import/create/"
            or path.startswith("/space/api/explorer/v2/create/")
            or path == "/space/api/explorer/v3/remove/"
        ):
            return "write"
        return "deny"

    return "deny"


def check_allowed(method: str, url: str, gateway_cmd: int = 0) -> None:
    verdict = classify(method, url, gateway_cmd)
    if verdict == "allow":
        return
    if verdict == "send":
        # The vendor-level gate (installed via _http) does the env/armed
        # check; reaching it means the caller went through sender.py.
        _http.require_send_allowed()
        return
    if verdict == "write":
        _http.require_write_allowed()
        return
    raise PolicyViolation(
        f"传输层拦截 {method} {urlsplit(url).netloc}{urlsplit(url).path}"
        + (f" (cmd={gateway_cmd})" if gateway_cmd else ""),
        hint="该请求不在允许白名单内；如确属新接口，先抓包验证再登记到 transport.classify",
    )


def session_headers(extra: dict | None = None, *, csrf: bool = False) -> dict:
    h = {
        "Cookie": _cookie_header,
        "user-agent": USER_AGENT,
        "origin": f"https://{_domain}",
        "referer": f"https://{_domain}/",
    }
    if csrf and _csrf_token:
        h["x-csrftoken"] = _csrf_token
    if extra:
        h.update(extra)
    return h


def public_media_headers(extra: dict | None = None) -> dict:
    """Headers for public media URLs; deliberately never includes Cookie."""
    h = {
        "user-agent": USER_AGENT,
        "accept": "image/*",
        "accept-encoding": "identity",
    }
    if extra:
        h.update({key: value for key, value in extra.items() if key.lower() != "cookie"})
        h.pop("Cookie", None)
        h.pop("cookie", None)
    return h


async def request(
    method: str,
    url: str,
    *,
    gateway_cmd: int = 0,
    headers: dict | None = None,
    retries: int = 2,
    timeout: float = 15,
    **kw,
):
    """Guarded HTTP request with small retry budget for network errors only.

    Retries never apply to sends or writes (gateway cmd=5 / write cmds, and
    REST writes like upload/import/create) — a lost response must not
    duplicate the operation.
    """
    if os.environ.get("FEISHU_NO_NETWORK") == "1":
        from .errors import FeishuError

        raise FeishuError("网络请求已被 FEISHU_NO_NETWORK=1 禁用")
    check_allowed(method, url, gateway_cmd)
    # Sending/creating must not duplicate on a lost response — no retries.
    # REST 写（云盘上传、import/create）不带 gateway cmd，按 classify 判定。
    if gateway_cmd == SEND_CMD or gateway_cmd in WRITE_GATEWAY_CMDS \
            or classify(method, url, gateway_cmd) in ("send", "write"):
        retries = 0
    last_exc = None
    for attempt in range(retries + 1):
        try:
            async with _http.make_client(timeout=timeout) as client:
                return await client.request(method, url, headers=headers, **kw)
        except (PolicyViolation, SendBlocked, _http.ResponseTooLarge):
            raise
        except Exception as e:  # network/TLS/timeout — safe to retry reads
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(0.5 * (2 ** attempt))
            else:
                from .errors import FeishuError

                raise FeishuError(f"请求失败：{urlsplit(url).netloc}{urlsplit(url).path}：{scrubbed_exc(e)}")
    raise last_exc  # unreachable, keeps type checkers happy


@asynccontextmanager
async def stream_request(
    method: str,
    url: str,
    *,
    gateway_cmd: int = 0,
    headers: dict | None = None,
    timeout: float = 15,
    max_response_bytes: int = _http.DEFAULT_MAX_RESPONSE_BYTES,
    public: bool = False,
    **kw,
):
    """Yield a live bounded response for streaming resource downloads.

    ``request`` intentionally returns eagerly materialized responses for the
    existing gateway callers.  Media must stream into a capped file instead,
    so this context manager owns both the client and response lifecycle.
    The caller is responsible for enforcing the byte cap while iterating
    ``response.aiter_raw()``.

    Public media requests (e.g. legacy CDN images) deliberately carry no
    session cookies and set trust_env=True to inherit the caller's standard
    environment proxy (HTTP(S)_PROXY), while authenticated API requests
    remain strictly trust_env=False to prevent credential leaks.
    """
    # 离线开关必须管住这条路：媒体下载走的正是 stream。
    if os.environ.get("FEISHU_NO_NETWORK") == "1":
        from .errors import FeishuError

        raise FeishuError("网络请求已被 FEISHU_NO_NETWORK=1 禁用")
    check_allowed(method, url, gateway_cmd)
    async with _http.make_client(
        timeout=timeout,
        headers=headers,
        max_response_bytes=max_response_bytes,
        trust_env=public,
    ) as client:
        request_obj = client.build_request(method, url, headers=headers, **kw)
        response = await client.send(request_obj, stream=True)
        try:
            yield response
        finally:
            await response.aclose()


async def gateway_call(cmd: int, payload: bytes, rid: str, timeout: float = 15,
                       header_overrides: dict | None = None):
    """POST a protobuf Packet to the IM gateway. Returns raw response bytes."""
    import hashlib
    import time

    from lark_bridge.proto import proto_pb2 as pb

    pkt = pb.Packet()
    pkt.payloadType = 1
    pkt.cmd = cmd
    pkt.payload = payload
    pkt.cid = rid or hashlib.md5(f"{time.time()}".encode()).hexdigest()[:10]

    headers = session_headers(
        {
            "content-type": "application/x-protobuf",
            "locale": "zh-CN",
            "x-command": str(cmd),
            "x-command-version": "7.68.5",
            "x-appid": "161471",
            "x-lgw-os-type": "1",
            "x-lgw-req-sdk-type": "220",
            "x-lgw-terminal-type": "2",
            "x-lsc-bizid": "1",
            "x-lsc-version": "1",
            "x-request-id": pkt.cid,
            "x-source": "web",
            "x-web-version": "7.68.5",
        }
    )
    # Calendar 等非 Messenger 子应用的 gateway 命令有自己的头指纹
    # （2026-09-11 抓包：cmd 1002003 要求 x-command-version=2.7.0、
    # x-lsc-bizid=3 等，错版会被回 payloadType=5 空响应）。
    # 空字符串 = 该头在真实客户端中不存在，直接移除。
    if header_overrides:
        headers.update(header_overrides)
        for k in [k for k, v in headers.items() if v == ""]:
            headers.pop(k)
    resp = await request(
        "POST",
        f"{INTERNAL_API}/im/gateway/",
        gateway_cmd=cmd,
        headers=headers,
        content=pkt.SerializeToString(),
        timeout=timeout,
    )
    return resp.content
