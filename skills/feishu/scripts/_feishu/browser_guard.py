"""Read-only network policy for pages rendered inside the Browser Worker."""

from __future__ import annotations

from urllib.parse import urlsplit

from . import config
from lark_bridge._urls import INTERNAL_API

# These commands were observed while loading Messenger, Mail and Calendar in
# the current tenant.  The worker never exposes a click/type operation for a
# write form, but the route guard still rejects the known mutation commands
# and any command not explicitly observed as part of a read page.
READ_GATEWAY_COMMANDS = frozenset({
    46, 201, 300, 3600, 3601, 3700, 3730, 3791, 3833,
    5017, 70003, 1000020, 1000041, 1000072, 1001001, 1002001,
    1010056, 1011741, 1102001, 1102002, 1102601, 1102602, 1103941,
})

_SAFE_POST_PATHS = frozenset({
    "/accounts/csrf",
    "/accounts/bff/api/sdk/meta",
    "/accounts/status/heartbeat",
    "/accounts/security/user/v2/web_list",
    "/lark/passport/permission/custom_display/page_list",
    "/web/r/token",
    "/web/common",
    "/v1/list",
})

_SAFE_POST_HOSTS = {
    urlsplit(INTERNAL_API).netloc,
    "internal-api-security.feishu.cn",
    "mcs-bd.feishu.cn",
}


def _is_safe_request(request) -> bool:
    parsed = urlsplit(request.url)
    method = request.method.upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if method != "POST":
        return False
    if parsed.path == "/im/gateway/":
        try:
            command = int(request.headers.get("x-command", "0"))
        except ValueError:
            return False
        return command in READ_GATEWAY_COMMANDS
    if parsed.path not in _SAFE_POST_PATHS:
        return False
    if parsed.netloc in _SAFE_POST_HOSTS:
        return True
    # Tenant-local account bootstrap calls use the configured tenant host.
    return parsed.netloc in {config.DOMAIN, "www.feishu.cn"} and parsed.path.startswith("/accounts/")


async def install(page) -> list[dict]:
    """Abort non-read requests before the page can send them.

    Return only bounded request metadata for diagnostics; never retain a URL
    query, header value, request body, or response body.
    """
    blocked: list[dict] = []

    async def handler(route):
        if _is_safe_request(route.request):
            await route.continue_()
        else:
            if len(blocked) < 100:
                parsed = urlsplit(route.request.url)
                blocked.append({
                    "method": route.request.method,
                    "host": parsed.netloc,
                    "path": parsed.path,
                    "x_command": route.request.headers.get("x-command", ""),
                })
            await route.abort()

    await page.route("**/*", handler)
    return blocked
