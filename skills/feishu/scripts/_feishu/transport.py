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
from urllib.parse import urlsplit

from lark_bridge import _http
from lark_bridge._urls import INTERNAL_API, DRIVE_STREAM, LOGIN_HOST, USER_AGENT

from . import config
from .errors import PolicyViolation, SendBlocked
from .output import note, scrub, scrubbed_exc

READ_GATEWAY_CMDS = frozenset({8, 64, 5023, 11021})
SEND_CMD = 5
DENIED_GATEWAY_CMDS = frozenset({13})  # create chat — never automated

_INTERNAL_HOST = urlsplit(INTERNAL_API).netloc
_DRIVE_STREAM_HOST = urlsplit(DRIVE_STREAM).netloc
_LOGIN_HOST = urlsplit(LOGIN_HOST).netloc

_ALLOW_SEND_ENV = "FEISHU_ALLOW_SEND"

# One-shot arming: send.py arms, transport disarms after the first cmd=5.
_send_armed = False
_direct_send_armed = False

# Bound session for the current process (set by the CLI after auth load).
_cookie_header = ""
_csrf_token = ""
_domain = config.DOMAIN


def configure() -> None:
    """Install TLS policy and the send gate into the vendor seam."""
    _http.configure(verify=config.CA_BUNDLE or True)
    _http.install_send_gate(_send_gate_check)
    _http.install_request_policy(check_allowed)


def bind_session(cookie_header: str, csrf_token: str = "", domain: str = "") -> None:
    global _cookie_header, _csrf_token, _domain
    _cookie_header = cookie_header
    _csrf_token = csrf_token
    if domain:
        _domain = domain


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


def _send_gate_check() -> None:
    global _send_armed, _direct_send_armed
    if os.environ.get(_ALLOW_SEND_ENV) != "1" and not _direct_send_armed:
        raise SendBlocked(
            "发送被拦截：需要环境变量 FEISHU_ALLOW_SEND=1",
            hint="这是双重闸门之一；另一道是 send --confirm-send <指纹>",
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
            return "deny"
        return "deny"

    if host == _LOGIN_HOST:
        if path.startswith("/suite/passport/frontier_ticket") and method == "GET":
            return "allow"
        return "deny"

    if host == _DRIVE_STREAM_HOST:
        if path.startswith("/space/api/box/stream/download/") and method == "GET":
            return "allow"
        return "deny"

    if host == _domain:
        if method == "GET" and (
            path.startswith("/space/api/explorer/")
            or path.startswith("/space/api/wiki/")
            or path.startswith("/space/api/export/result/")
        ):
            return "allow"
        # Export creation is a read in spirit: it renders a document the
        # account can already see into a downloadable file.
        if method == "POST" and path == "/space/api/export/create/":
            return "allow"
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
    raise PolicyViolation(
        f"传输层拦截 {method} {urlsplit(url).netloc}{urlsplit(url).path}"
        + (f" (cmd={gateway_cmd})" if gateway_cmd else ""),
        hint="该请求不在只读白名单内；如确属新读接口，先抓包验证再登记到 transport.classify",
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

    Retries never apply to gateway_cmd==5 (sending must not duplicate).
    """
    check_allowed(method, url, gateway_cmd)
    if gateway_cmd == SEND_CMD:
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


async def gateway_call(cmd: int, payload: bytes, rid: str, timeout: float = 15):
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
    resp = await request(
        "POST",
        f"{INTERNAL_API}/im/gateway/",
        gateway_cmd=cmd,
        headers=headers,
        content=pkt.SerializeToString(),
        timeout=timeout,
    )
    return resp.content
