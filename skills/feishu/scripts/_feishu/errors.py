"""Exit-code-bearing errors for the feishu skill.

Exit codes (kept stable, scripts and Claude rely on them):
  1  general failure
  2  auth missing / session expired (--unattended + notify)
  3  send blocked — draft produced, confirmation gates not satisfied
  4  endpoint not adapted yet (protocol gap, needs live capture)
  5  transport policy violation (a write that must never happen was attempted)
"""


class FeishuError(Exception):
    exit_code = 1

    def __init__(self, msg: str, *, hint: str = ""):
        super().__init__(msg)
        self.hint = hint


class AuthRequired(FeishuError):
    exit_code = 2


class SendBlocked(FeishuError):
    exit_code = 3


class EndpointNotAdapted(FeishuError):
    exit_code = 4


class PolicyViolation(FeishuError):
    exit_code = 5


class QueryBusy(FeishuError):
    exit_code = 6


class ResourceIsolationUnavailable(FeishuError):
    """A browser operation cannot run without a host-level resource boundary."""

    exit_code = 7


class BrowserWorkerError(FeishuError):
    """The bounded browser worker failed or exceeded its deadline."""

    exit_code = 7


class ResourceError(FeishuError):
    """A message resource is unavailable, invalid, or exceeded its limits."""

    exit_code = 4


class MediaWorkerError(FeishuError):
    """The bounded local media worker failed or exceeded its deadline."""

    exit_code = 7
