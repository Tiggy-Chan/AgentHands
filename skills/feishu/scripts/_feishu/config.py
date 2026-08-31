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
