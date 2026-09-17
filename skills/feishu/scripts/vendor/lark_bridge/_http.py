"""Shared httpx client factory — agenthands patch seam.

Upstream lark-bridge created ``httpx.AsyncClient(verify=False, ...)`` inline at
every call site. Vendored modules now build clients through ``make_client`` so
TLS verification and send-gating live in exactly one place.

``_feishu.transport`` calls ``configure()`` at import time to install the CA
policy, and ``install_send_gate()`` to arm the send check. Both default to the
safe posture (strict TLS, send denied) if nobody configures anything.
"""

from __future__ import annotations

import httpx
import zlib

# True = system CA bundle; a str path selects a custom CA (FEISHU_CA_BUNDLE).
_VERIFY: "bool | str" = True

# A malformed or unexpectedly large internal response must not be allowed to
# consume the host.  Document/file downloads opt into the larger bound at the
# call site; protobuf and JSON control-plane responses use this default.
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
LARGE_MAX_RESPONSE_BYTES = 256 * 1024 * 1024

# Callable that must raise to block a send. None = not installed = deny.
_SEND_CHECK = None
_WRITE_CHECK = None
_REQUEST_POLICY = None


class SendBlockedError(RuntimeError):
    """Raised when a write operation is attempted without the send gate open."""


class WriteDisabledError(RuntimeError):
    """Raised by vendor write operations agenthands disables outright."""


def configure(verify: "bool | str" = True) -> None:
    global _VERIFY
    _VERIFY = verify


def install_send_gate(check) -> None:
    """check() must raise SendBlockedError when sending is not allowed."""
    global _SEND_CHECK
    _SEND_CHECK = check


def install_write_gate(check) -> None:
    """Install the one-shot gate for future mail/calendar writes."""
    global _WRITE_CHECK
    _WRITE_CHECK = check


def install_request_policy(check) -> None:
    """Install a callback ``(method, url, gateway_cmd)`` → None/raise."""
    global _REQUEST_POLICY
    _REQUEST_POLICY = check


def require_send_allowed() -> None:
    if _SEND_CHECK is None:
        raise SendBlockedError(
            "send gate not installed; sending is disabled in this build"
        )
    _SEND_CHECK()


def require_write_allowed() -> None:
    if _WRITE_CHECK is None:
        raise SendBlockedError(
            "write gate not installed; non-message writes are disabled in this build"
        )
    _WRITE_CHECK()


def write_disabled(what: str) -> None:
    raise WriteDisabledError(
        f"{what} is disabled in the agenthands vendor build (read-only policy)"
    )


class ResponseTooLarge(RuntimeError):
    """Raised before an HTTP response can exhaust process memory."""


def _decode_bounded(raw: bytes, encoding: str, limit: int) -> bytes:
    """Decode gzip/deflate while bounding decompressed output.

    A compressed response can be small on the wire but expand to gigabytes.
    Never call an unbounded one-shot decompressor on an untrusted response.
    """
    encoding = (encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return raw
    if encoding == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decoder = zlib.decompressobj()
    else:
        raise ResponseTooLarge(f"unsupported content-encoding: {encoding}")

    out = bytearray()
    for start in range(0, len(raw), 64 * 1024):
        part = decoder.decompress(raw[start:start + 64 * 1024], limit + 1 - len(out))
        out.extend(part)
        if len(out) > limit:
            raise ResponseTooLarge(f"decoded response exceeded {limit} bytes")
    tail = decoder.flush(limit + 1 - len(out))
    out.extend(tail)
    if len(out) > limit:
        raise ResponseTooLarge(f"decoded response exceeded {limit} bytes")
    return bytes(out)


class BoundedAsyncClient(httpx.AsyncClient):
    """AsyncClient that bounds non-streaming response bodies.

    httpx's normal ``request`` path eagerly reads the complete body.  The
    internal Feishu endpoints occasionally return malformed/oversized bodies,
    so we stream, count, and rebuild a normal Response for existing callers.
    Explicit streaming callers retain the original streaming response.
    """

    def __init__(self, *args, max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES, **kwargs):
        self._enforce_policy = kwargs.pop("enforce_policy", False)
        super().__init__(*args, **kwargs)
        self._max_response_bytes = max_response_bytes

    async def send(self, request, *, stream=False, auth=httpx.USE_CLIENT_DEFAULT,
                   follow_redirects=httpx.USE_CLIENT_DEFAULT):
        if self._enforce_policy and _REQUEST_POLICY is not None:
            try:
                gateway_cmd = int(request.headers.get("x-command", "0"))
            except ValueError:
                gateway_cmd = 0
            # sender.py has already consumed the one-shot gate before opening
            # its client; applying the policy twice would consume it twice.
            if gateway_cmd != 5:
                _REQUEST_POLICY(request.method, str(request.url), gateway_cmd)
        response = await super().send(
            request, stream=True, auth=auth, follow_redirects=follow_redirects
        )
        if stream:
            return response

        chunks = []
        total = 0
        try:
            try:
                declared = int(response.headers.get("content-length", "0"))
            except ValueError:
                declared = 0
            if declared > self._max_response_bytes:
                raise ResponseTooLarge(
                    f"declared response exceeded {self._max_response_bytes} bytes"
                )
            if response.is_stream_consumed:
                # Mock/custom transports may return an already-materialized
                # response.  Real network responses from ``send(stream=True)``
                # take the raw-stream branch below.
                body = response.content
                if len(body) > self._max_response_bytes:
                    raise ResponseTooLarge(
                        f"raw response exceeded {self._max_response_bytes} bytes"
                    )
            else:
                # Read raw bytes first.  ``aiter_bytes`` transparently
                # decompresses and a compression bomb could allocate before
                # our size check.
                async for chunk in response.aiter_raw():
                    total += len(chunk)
                    if total > self._max_response_bytes:
                        raise ResponseTooLarge(
                            f"raw response exceeded {self._max_response_bytes} bytes"
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
            # ``aiter_bytes`` returns transparently decompressed bytes.  Do
            # not leave the original encoding/length headers on the rebuilt
            # response or httpx/json consumers will try to decompress twice.
            headers = httpx.Headers(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            if not response.is_stream_consumed:
                body = _decode_bounded(
                    body,
                    response.headers.get("content-encoding", ""),
                    self._max_response_bytes,
                )
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=body,
                request=request,
                extensions=response.extensions,
            )
        finally:
            await response.aclose()


def make_client(
    timeout: float = 15,
    headers: dict | None = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    trust_env: bool = False,
) -> httpx.AsyncClient:
    request_headers = dict(headers or {})
    # Prefer an uncompressed response.  _decode_bounded remains necessary for
    # servers/proxies that ignore this request header.
    request_headers.setdefault("accept-encoding", "identity")
    return BoundedAsyncClient(
        verify=_VERIFY,
        timeout=timeout,
        headers=request_headers,
        max_response_bytes=max_response_bytes,
        enforce_policy=True,
        # 带 session cookie 的流量绝不跟系统代理走（与浏览器侧的
        # --no-proxy-server 对齐）；仅无 Cookie 的公共媒体下载显式开启 trust_env
        # 以继承调用方环境的 HTTP(S)_PROXY。
        trust_env=trust_env,
    )
