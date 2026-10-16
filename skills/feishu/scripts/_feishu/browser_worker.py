"""Run every Feishu browser operation inside a bounded systemd cgroup.

The parent process starts this module through ``systemd-run --user``.  The
worker accepts only a fixed operation name and a JSON payload on stdin; it
never evaluates caller-supplied JavaScript.  A transient service is deliberate:
Python, Playwright's Node driver, and every Chromium child then share the same
MemoryMax/TasksMax boundary and are all reaped together on timeout.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

if __package__ in (None, ""):
    _SCRIPTS = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_SCRIPTS))
    sys.path.insert(0, str(_SCRIPTS / "vendor"))
    __package__ = "_feishu"

from . import config  # noqa: E402
from .errors import BrowserWorkerError, ResourceIsolationUnavailable  # noqa: E402
from .output import scrub  # noqa: E402

_OPERATIONS = frozenset({
    "login",
    "chats",
    "docs-search",
    "doc-replace",
    "doc-linkify",
    "doc-read",
    "doc-screenshot",
    "doc-insert-images",
    "doc-insert-file",
    "doc-block-edit",
    "doc-block-map",
    "doc-clipboard-inspect",
    "probe",
    "message-media-probe",
    "media-file-download",
    "media-observe",
    "mail-list",
    "mail-search",
    "mail-get",
    "calendar-list",
    "calendar-get",
    "calendar-calendars",
})


def _systemd_user_available() -> bool:
    if not shutil.which("systemd-run") or not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # is-system-running exits non-zero for "degraded" (any failed user unit,
    # e.g. feishu-daemon itself).  Degraded still runs transient services
    # fine, so decide on the reported state, not the exit code.
    return result.stdout.strip() in {"running", "degraded"}


def _worker_python() -> str:
    candidate = config.VENV / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def _worker_script() -> str:
    return str(Path(__file__).resolve())


def _unit_name(operation: str) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in operation)
    return f"feishu-browser-{safe}-{os.getpid()}-{time.monotonic_ns()}"


def build_command(operation: str, unit: str, timeout: float) -> list[str]:
    """Build the transient-service command; kept pure for contract tests."""
    if operation not in _OPERATIONS:
        raise BrowserWorkerError(f"未允许的 Browser Worker 操作：{operation}")
    cmd = [
        "systemd-run", "--user", "--wait", "--pipe", "--collect", "--quiet",
        f"--unit={unit}",
        f"--property=MemoryHigh={config.BROWSER_MEMORY_HIGH}",
        f"--property=MemoryMax={config.BROWSER_MEMORY_MAX}",
        f"--property=TasksMax={config.BROWSER_TASKS_MAX}",
        "--property=OOMPolicy=kill",
        "--property=KillMode=control-group",
        f"--property=RuntimeMaxSec={max(1, math.ceil(timeout))}s",
        f"--setenv=FEISHU_HOME={config.HOME}",
        "--setenv=FEISHU_BROWSER_WORKER=1",
    ]
    # Headed operations (login, --capture) need the user's display; a
    # transient systemd unit does not reliably inherit it (2026-09-10: the
    # headed capture window never appeared and the browser died headless).
    for var in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "FEISHU_CAPTURE_WINDOW"):
        value = os.environ.get(var)
        if value:
            cmd.append(f"--setenv={var}={value}")
    cmd += [_worker_python(), _worker_script(), "--worker", "--operation", operation]
    return cmd


def _decode_worker_output(stdout: bytes) -> dict:
    # systemd --pipe should leave stdout untouched.  Parse the last JSON line
    # defensively so a future launcher warning cannot turn into fake data.
    # Split ONLY on \n: str.splitlines() also breaks on U+2028/U+2029/NEL,
    # which json.dumps leaves raw inside strings (captured page text carries
    # them) — that shattered a perfectly valid 150KB result (2026-09-11).
    for raw_line in reversed(stdout.split(b"\n")):
        try:
            line = raw_line.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise BrowserWorkerError("Browser Worker 没有返回有效 JSON")


async def _stop_unit(unit: str) -> None:
    if not unit:
        return
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["systemctl", "--user", "stop", unit],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


async def run(operation: str, payload: dict | None = None, *, timeout: float | None = None) -> dict:
    """Execute one fixed browser operation in a transient bounded service."""
    if operation not in _OPERATIONS:
        raise BrowserWorkerError(f"未允许的 Browser Worker 操作：{operation}")
    if not _systemd_user_available():
        raise ResourceIsolationUnavailable(
            "当前环境没有可用的 systemd user/cgroup，已拒绝启动无上限浏览器",
            hint="恢复 systemd --user 后再运行该浏览器功能；HTTP/WebSocket 查询不受影响",
        )

    timeout = float(timeout or config.BROWSER_TIMEOUT_SECONDS)
    attempt = 0
    while True:
        attempt += 1
        unit = _unit_name(operation)
        cmd = build_command(operation, unit, timeout)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        request = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(request), timeout=timeout + 10)
        except asyncio.TimeoutError as exc:
            await _stop_unit(unit)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            raise BrowserWorkerError(
                f"Browser Worker 超过 {timeout:g} 秒，已终止整个浏览器进程树"
            ) from exc
        except asyncio.CancelledError:
            # A caller-level deadline can cancel this coroutine before the
            # worker's own timeout. Stop the transient unit first; otherwise the
            # systemd service and its Chromium descendants could outlive the CLI.
            await _stop_unit(unit)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            raise
        # systemd-run exits 255 with no worker output when systemd-oomd
        # SIGKILLs the unit (session-level PSI trip — observed 2026-09-10 on
        # a busy desktop; the service journal shows status=9/KILL).  The kill
        # is racy, so retry once into (hopefully) a quieter window.  All
        # browser operations here are idempotent reads or clear-then-write
        # replace flows, so a cold restart is safe.
        if proc.returncode == 255 and not stdout.strip() and attempt == 1:
            continue
        if proc.returncode != 0:
            detail = scrub(stderr.decode("utf-8", errors="replace"))[-1200:]
            raise BrowserWorkerError(
                f"Browser Worker 失败（退出码 {proc.returncode}）" + (f"：{detail}" if detail else "")
            )
        try:
            return _decode_worker_output(stdout)
        except BrowserWorkerError:
            try:
                (config.HOME / ".last-worker-stdout.bin").write_bytes(stdout)
                (config.HOME / ".last-worker-stderr.txt").write_bytes(stderr)
            except Exception:
                pass
            raise


async def _dispatch(operation: str, payload: dict) -> dict:
    if operation == "login":
        from .auth import _login_playwright_impl
        return await _login_playwright_impl()
    if operation == "chats":
        from .chats import _list_from_browser_impl
        return {"items": await _list_from_browser_impl()}
    if operation == "docs-search":
        from .docs_search import _browser_search_impl
        return await _browser_search_impl(
            str(payload.get("keyword", "")), int(payload.get("limit", 30)),
            debug=bool(payload.get("debug")))
    if operation == "doc-replace":
        from .documents import _replace_document_impl
        return await _replace_document_impl(
            str(payload.get("ref", "")), str(payload.get("content_file", ""))
        )
    if operation == "doc-linkify":
        from .documents import _linkify_document_impl
        return await _linkify_document_impl(
            str(payload.get("ref", "")), str(payload.get("payload_file", ""))
        )
    if operation == "doc-read":
        from .documents import _read_document_impl
        return await _read_document_impl(str(payload.get("ref", "")))
    if operation == "doc-screenshot":
        from .documents import _screenshot_document_impl
        return await _screenshot_document_impl(
            str(payload.get("ref", "")), str(payload.get("out_dir", "")))
    if operation == "doc-insert-images":
        from .documents import _insert_images_impl
        return await _insert_images_impl(
            str(payload.get("ref", "")), str(payload.get("payload_file", "")))
    if operation == "doc-insert-file":
        from .documents import _insert_file_impl
        return await _insert_file_impl(
            str(payload.get("ref", "")), str(payload.get("payload_file", "")))
    if operation == "doc-block-edit":
        from .documents import _block_edit_impl
        return await _block_edit_impl(
            str(payload.get("ref", "")), str(payload.get("payload_file", "")))
    if operation == "doc-block-map":
        from .documents import _block_map_impl
        return await _block_map_impl(str(payload.get("ref", "")))
    if operation == "doc-clipboard-inspect":
        from .documents import _inspect_clipboard_impl
        return await _inspect_clipboard_impl(str(payload.get("ref", "")))
    if operation == "probe":
        from .probe import _run_impl
        return await _run_impl(
            str(payload.get("scope", "all")),
            str(payload.get("keyword", "测试")),
            bool(payload.get("headed", False)),
        )
    if operation == "message-media-probe":
        from . import auth, message_resources
        await auth.prepare()
        return await message_resources.probe_metadata(str(payload.get("msg_id", "")))
    if operation == "media-file-download":
        from . import message_resources
        return await message_resources._download_file_browser_impl(
            str(payload.get("msg_id", "")), str(payload.get("chat_id", "")),
            str(payload.get("file_name", "")), bool(payload.get("headed", False)))
    if operation == "media-observe":
        from . import probe as probe_mod
        return await probe_mod._media_observe_impl(
            str(payload.get("msg_id", "")), bool(payload.get("headed", False)),
            bool(payload.get("interact", False)))
    if operation.startswith("mail-"):
        from . import mail
        return await mail.browser_operation(operation[5:], payload)
    if operation.startswith("calendar-"):
        from . import calendar_ops as calendar
        return await calendar.browser_operation(operation[9:], payload)
    raise BrowserWorkerError(f"未实现的 Browser Worker 操作：{operation}")


def worker_main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--operation", choices=sorted(_OPERATIONS), required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("worker payload must be an object")
        from . import transport
        transport.configure()
        result = asyncio.run(_dispatch(args.operation, payload))
        line = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        try:
            # Side-channel for diagnosing "systemd-run ate my stdout" cases
            # (2026-09-11: capture runs exit 0 with the JSON never arriving).
            (config.HOME / ".last-worker-result.json").write_text(line)
        except Exception:
            pass
        print(line)
        return 0
    except Exception as exc:
        # Keep worker diagnostics concise: Playwright call logs can include
        # URLs or page text, so never forward a full traceback to the caller.
        print(scrub(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        hint = getattr(exc, "hint", "")
        if hint:
            print(scrub(f"hint: {hint}"), file=sys.stderr)
        return getattr(exc, "exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv[1:]))
