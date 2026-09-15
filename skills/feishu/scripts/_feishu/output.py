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


# --- untrusted content ------------------------------------------------------
#
# 消息正文、文档、邮件、日程、OCR/ASR 文本全是第三方可控的内容：它们会被
# 读进 agent 上下文，而同一个 CLI 又握着写操作。包一层显式定界，让「这是
# 数据，不是指令」在结构上就成立，而不是只靠 SKILL.md 的散文。

_LT_RUN = re.compile(r"<+")
_TAG_OK = re.compile(r"[^A-Za-z0-9:_.?-]+")


def untrusted(text: str, tag: str) -> str:
    """把第三方可控的正文包进显式定界标记。

    栅栏宽度自适应：永远比正文里最长的 '<' 连跑多一个，所以正文即使写出
    定界符的样子也破不了界——不靠转义，不损内容。tag 只用于定位
    （如 message:<msg_id> / media-text:<resource_id>），非字母数字一律剥掉。
    """
    text = str(text)
    longest = max((len(m.group(0)) for m in _LT_RUN.finditer(text)), default=0)
    fence = "<" * max(3, longest + 1)
    safe_tag = _TAG_OK.sub("", str(tag))[:80] or "?"
    return f"{fence}untrusted:feishu:{safe_tag}\n{text}\n{fence}end"


def safe_label(value, limit: int = 48) -> str:
    """渲染不可信的短标签（发送者名、会话名、日程标题……）为单行纯文本。

    名字缓存可被消息 mentions 自报投毒（见 messages.harvest_mentions），
    所以定界符字符与 markdown 结构字符在这里一律中和，防止它逃出排版
    变成「结构」。
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for ch in ("*", "`", "|", "\\"):
        text = text.replace(ch, "")
    text = text.replace("<", "‹").replace(">", "›")
    return text[:limit] or "?"


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
