"""Paths and environment knobs for the feishu skill.

Everything user-specific lives under FEISHU_HOME (default ~/.local/share/feishu).
Nothing here hardcodes a particular user's home directory; the smoke test
checks that.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("FEISHU_HOME", Path.home() / ".local/share/feishu")).expanduser()
VENV = HOME / "venv"
PROFILE = HOME / "profile"            # persistent Chromium profile (login)
COOKIES_FILE = HOME / "cookies.json"  # 0600
CONFIG_FILE = HOME / "config.json"    # 0600
EVENTS_FILE = HOME / "events.jsonl"
EVENTS_ENCRYPTED_FILE = HOME / "events.v2.jsonl.enc"
EVENTS_LEGACY_FILE = HOME / "events.v1.jsonl"
EVENTS_MAX_BYTES = int(os.environ.get("FEISHU_EVENTS_MAX_BYTES", 50 * 1024 * 1024))
EVENTS_KEEP = 3                        # events.jsonl.1 .. .3
EVENTS_TOTAL_MAX_BYTES = int(os.environ.get("FEISHU_EVENTS_TOTAL_MAX_BYTES", 10 * 1024 * 1024 * 1024))
EVENTS_WARN_RATIO = 0.8
MAX_SEEN_MSG_IDS = max(1, int(os.environ.get("FEISHU_MAX_SEEN_MSG_IDS", 20_000)))
SEARCH_MAX_PAGES = max(1, int(os.environ.get("FEISHU_SEARCH_MAX_PAGES", 8)))
HISTORY_TIMEOUT_SECONDS = float(os.environ.get("FEISHU_HISTORY_TIMEOUT", 45))
MAX_HISTORY_LIMIT = int(os.environ.get("FEISHU_MAX_HISTORY_LIMIT", 200))
PROCESS_MEMORY_MAX_MB = min(max(int(os.environ.get("FEISHU_PROCESS_MEMORY_MAX_MB", 1024)), 256), 4096)
QUERY_TIMEOUT_SECONDS = float(os.environ.get("FEISHU_QUERY_TIMEOUT", 60))
AUTH_TIMEOUT_SECONDS = float(os.environ.get("FEISHU_AUTH_TIMEOUT", 30))
WALK_TIMEOUT_SECONDS = float(os.environ.get("FEISHU_WALK_TIMEOUT", 600))
MAX_THREAD_WINDOW_MINUTES = int(os.environ.get("FEISHU_MAX_THREAD_WINDOW", 120))
QUERY_LOCK = HOME / "query.lock"
CONTEXT_MAX_MESSAGES = int(os.environ.get("FEISHU_CONTEXT_MAX_MESSAGES", 200))
CONTEXT_MAX_BYTES = int(os.environ.get("FEISHU_CONTEXT_MAX_BYTES", 2 * 1024 * 1024))
CONTEXT_TIMEOUT_SECONDS = float(os.environ.get("FEISHU_CONTEXT_TIMEOUT", 45))
MAX_MENTION_CHATS = max(1, int(os.environ.get("FEISHU_MAX_MENTION_CHATS", 100)))
MAX_EVENT_READ_LIMIT = max(1, int(os.environ.get("FEISHU_MAX_EVENT_READ_LIMIT", 10_000)))
MAX_EVENT_DEDUP_IDS = max(1_000, int(os.environ.get("FEISHU_MAX_EVENT_DEDUP_IDS", 100_000)))
MAX_BROWSER_REQUESTS = max(10, int(os.environ.get("FEISHU_MAX_BROWSER_REQUESTS", 1_000)))
MAX_BROWSER_CARDS = max(10, int(os.environ.get("FEISHU_MAX_BROWSER_CARDS", 500)))
MAX_BROWSER_TEXT_BYTES = max(256, int(os.environ.get("FEISHU_MAX_BROWSER_TEXT_BYTES", 4096)))
MAX_LOCAL_DOC_BYTES = max(1024, int(os.environ.get("FEISHU_MAX_LOCAL_DOC_BYTES", 16 * 1024 * 1024)))
MESSAGE_RESOURCE_MAX_BYTES = max(
    1024 * 1024,
    int(os.environ.get("FEISHU_MESSAGE_RESOURCE_MAX_MB", 256)) * 1024 * 1024,
)
MEDIA_JOB_MAX_BYTES = max(
    MESSAGE_RESOURCE_MAX_BYTES,
    int(os.environ.get("FEISHU_MEDIA_JOB_MAX_MB", 512)) * 1024 * 1024,
)
MEDIA_MAX_PIXELS = max(1_000_000, int(os.environ.get("FEISHU_MEDIA_MAX_PIXELS", 40_000_000)))
MEDIA_MAX_DURATION_SECONDS = max(60, int(os.environ.get("FEISHU_MEDIA_MAX_DURATION", 2 * 60 * 60)))
MEDIA_MAX_VIDEO_FRAMES = max(1, int(os.environ.get("FEISHU_MEDIA_MAX_VIDEO_FRAMES", 32)))
MEDIA_OUTPUT_MAX_BYTES = max(64 * 1024, int(os.environ.get("FEISHU_MEDIA_OUTPUT_MAX_MB", 4)) * 1024 * 1024)
MEDIA_CACHE_DIR = HOME / "media-cache"
MEDIA_CACHE_MAX_BYTES = max(
    MEDIA_JOB_MAX_BYTES,
    int(os.environ.get("FEISHU_MEDIA_CACHE_MAX_GB", 2)) * 1024 * 1024 * 1024,
)
MEDIA_MEMORY_HIGH = os.environ.get("FEISHU_MEDIA_MEMORY_HIGH", "768M")
MEDIA_MEMORY_MAX = os.environ.get("FEISHU_MEDIA_MEMORY_MAX", "1G")
MEDIA_TASKS_MAX = os.environ.get("FEISHU_MEDIA_TASKS_MAX", "64")
MEDIA_TIMEOUT_SECONDS = float(os.environ.get("FEISHU_MEDIA_TIMEOUT", 600))
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = float(os.environ.get("FEISHU_MEDIA_DOWNLOAD_TIMEOUT", 90))
MEDIA_ASR_MODEL = os.environ.get("FEISHU_ASR_MODEL", "base")
MEDIA_ASR_DEVICE = os.environ.get("FEISHU_ASR_DEVICE", "cpu")
MEDIA_ASR_COMPUTE_TYPE = os.environ.get("FEISHU_ASR_COMPUTE_TYPE", "int8")
MEDIA_MODELS_DIR = HOME / "models"
BROWSER_TIMEOUT_SECONDS = float(os.environ.get("FEISHU_BROWSER_TIMEOUT", 120))
BROWSER_QUERY_TIMEOUT_SECONDS = max(
    QUERY_TIMEOUT_SECONDS, BROWSER_TIMEOUT_SECONDS + 20
)
BROWSER_MEMORY_HIGH = os.environ.get("FEISHU_BROWSER_MEMORY_HIGH", "384M")
BROWSER_MEMORY_MAX = os.environ.get("FEISHU_BROWSER_MEMORY_MAX", "512M")
BROWSER_TASKS_MAX = os.environ.get("FEISHU_BROWSER_TASKS_MAX", "128")
BROWSER_ARGS = [
    "--no-proxy-server",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--renderer-process-limit=4",
    "--num-raster-threads=1",
    "--renderer-process-limit=1",
    "--no-zygote",
    "--single-process",
]
MAIL_URL = os.environ.get("FEISHU_MAIL_URL", "")
CALENDAR_URL = os.environ.get("FEISHU_CALENDAR_URL", "")
DOCS_CACHE_DIR = HOME / "docs-cache"
DAEMON_PID = HOME / "daemon.pid"
DAEMON_LOCK = HOME / "daemon.lock"
DAEMON_LOG = HOME / "daemon.log"
DAEMON_STATE = HOME / "daemon.state.json"
DOCS_DIR = HOME / "docs"               # exported document cache
DOCS_INDEX = HOME / "docs-index.jsonl"
CACHE_DIR = HOME / "cache"
NAMES_CACHE = CACHE_DIR / "names.json"
NAME_LOOKUP_TIMEOUT_SECONDS = float(os.environ.get("FEISHU_NAME_LOOKUP_TIMEOUT", 4))
NAME_LOOKUP_CONCURRENCY = max(1, int(os.environ.get("FEISHU_NAME_LOOKUP_CONCURRENCY", 4)))

# User-facing tenant domain. www.feishu.cn works for every tenant once the
# session cookie (domain=.feishu.cn) is present; override for vanity domains.
DOMAIN = os.environ.get("FEISHU_DOMAIN", "www.feishu.cn")

# Corporate CA escape hatch. Default stays strict TLS — never False.
CA_BUNDLE = os.environ.get("FEISHU_CA_BUNDLE", "")

LOGIN_WAIT_SECONDS = 300
WS_KEEPALIVE_SECONDS = 300  # daemon: periodic account touch so the web session
                            # does not die from inactivity (12-24h)


def ensure_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
