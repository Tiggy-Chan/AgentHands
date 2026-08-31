"""Output discipline: data on stdout, progress on stderr, secrets nowhere."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> "SystemExit":
    print(msg, file=sys.stderr, flush=True)
    return SystemExit(code)


def emit_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def emit_jsonl(rows) -> None:
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))


def emit(text: str) -> None:
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")


# --- time -----------------------------------------------------------------

def fmt_ts(epoch) -> str:
    """Epoch seconds or milliseconds (auto-detected) → local ISO string."""
    try:
        v = float(epoch)
    except (TypeError, ValueError):
        return str(epoch)
    if v > 1e12:  # milliseconds
        v /= 1000.0
    if v <= 0:
        return "-"
    return datetime.fromtimestamp(v).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


_DURATION_RE = re.compile(r"^([+-]?)(\d+)\s*([smhd])$", re.I)
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_when(spec: str):
    """'30m'/'2h'/'7d' → epoch ms relative to now; ISO string or bare epoch
    (s or ms) passed through. Returns None for empty input."""
    if not spec:
        return None
    spec = spec.strip()
    m = _DURATION_RE.match(spec)
    if m:
        seconds = int(m.group(2)) * _UNIT_S[m.group(3).lower()]
        return now_ms() + (seconds if m.group(1) == "+" else -seconds) * 1000
    try:
        dt = datetime.fromisoformat(spec)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    try:
        v = int(spec)
        return v if v > 1e12 else v * 1000
    except ValueError:
        raise ValueError(f"无法解析时间: {spec!r}（支持 30m/2h/7d、ISO 日期、epoch 秒/毫秒）")


# --- secret scrubbing -------------------------------------------------------

_SECRETS: list[str] = []


def register_secret(value: str) -> None:
    """Register a value that must never reach stdout/stderr/logs."""
    if value and len(value) >= 4 and value not in _SECRETS:
        _SECRETS.append(value)


def scrub(text: str) -> str:
    """Replace registered secrets with ***. Applied to every error path."""
    out = str(text)
    for s in _SECRETS:
        out = out.replace(s, "***")
    # WS ticket / access_key travel in the URL query — never log queries.
    out = re.sub(r"(\?)[^\s]*", r"?…", out) if "ticket=" in out else out
    return out


def scrubbed_exc(e: BaseException) -> str:
    return scrub(f"{type(e).__name__}: {e}")
