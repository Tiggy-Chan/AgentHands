"""Output formatting and secret redaction for Xiaohongshu skill."""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Iterable


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> SystemExit:
    print(msg, file=sys.stderr, flush=True)
    return SystemExit(code)


def emit_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def emit_jsonl(rows: Iterable[Any]) -> None:
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))


def emit(text: str) -> None:
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")


# --- untrusted content delimiters ---

_LT_RUN = re.compile(r"<+")
_TAG_OK = re.compile(r"[^A-Za-z0-9:_.?-]+")


def untrusted(text: str, tag: str) -> str:
    """Enclose third-party content in explicit delimiters."""
    text = str(text or "")
    longest = max((len(m.group(0)) for m in _LT_RUN.finditer(text)), default=0)
    fence = "<" * max(3, longest + 1)
    safe_tag = _TAG_OK.sub("", str(tag))[:80] or "unknown"
    return f"{fence}untrusted:xiaohongshu:{safe_tag}\n{text}\n{fence}end"


def safe_label(value: Any, limit: int = 48) -> str:
    """Neutralize structural characters in short labels."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for ch in ("*", "`", "|", "\\"):
        text = text.replace(ch, "")
    text = text.replace("<", "‹").replace(">", "›")
    return text[:limit] or "-"


# --- secret scrubbing ---

_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    """Register token or credential value to prevent trace leakage."""
    if value and len(value) >= 4 and value not in _SECRETS:
        _SECRETS.append(value)


def scrub(text: str) -> str:
    """Redact registered secrets with asterisks."""
    out = str(text or "")
    for s in _SECRETS:
        out = out.replace(s, "***")
    return out


def scrubbed_exc(e: BaseException) -> str:
    return scrub(f"{type(e).__name__}: {e}")
