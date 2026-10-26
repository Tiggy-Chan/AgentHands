"""WS daemon: stream every message into the local JSONL event pool.

Management prefers systemd --user (auto-restart, journald logs); falls back
to nohup + pidfile. Single instance is always enforced by flock, independent
of which manager started us. The daemon NEVER sends: the send gate is not
armed here and transport denies cmd=5 without FEISHU_ALLOW_SEND=1 anyway.
"""

from __future__ import annotations

import asyncio
from collections import deque
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import auth, config, event_store, transport
from .errors import AuthRequired, FeishuError, QueryBusy
from .message_decode import decode_message
from .message_model import build_envelope, event_message
from .output import note, register_secret, scrubbed_exc

_UNIT = "feishu-daemon.service"


def _notify(msg: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "飞书 feishu skill", msg], check=False)
    note(f"!! {msg}")


def _write_state(**kw) -> None:
    try:
        state = {}
        if config.DAEMON_STATE.exists():
            state = json.loads(config.DAEMON_STATE.read_text())
        state.update(kw)
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        config.DAEMON_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        os.chmod(config.DAEMON_STATE, 0o600)
    except OSError:
        pass


def _build_event(m: dict, my_id: str) -> dict:
    raw = m.get("raw") or {}
    dec = decode_message(raw) if raw else {
        "text": m.get("text", ""), "mention_user_ids": [], "decode_status": "unknown",
        "semantic_status": "opaque", "msg_type": m.get("type", 0),
        "structured": {}, "resources": [], "nested_messages": [], "errors": [],
    }
    sources = []
    if m.get("at_me"):
        sources.append("push.messagesAtMe")
    if my_id and my_id in (m.get("at_out_chatter_ids") or []):
        sources.append("message.atOutChatterIds")
    if my_id and my_id in dec.get("mention_user_ids", []):
        sources.append("rich_text")
    at_all = "all" in (dec.get("mention_user_ids") or [])
    if sources:
        at_me, confidence = True, "confirmed"
    elif dec.get("decode_status") == "undecoded":
        at_me, confidence = None, "unknown"
    else:
        at_me, confidence = False, "none"
    if at_all:
        sources.append("rich_text.at_all")
    if not raw:
        raw = {
            "type": m.get("type", 0),
            "fromId": m.get("from_id", ""),
            "chatId": m.get("chat_id", ""),
            "chatType": m.get("chat_type", 0),
            "createTime": m.get("create_time", 0),
            "parentId": m.get("parent_id", ""),
            "rootId": m.get("root_id", ""),
            "threadId": m.get("thread_id", ""),
            "isRemoved": m.get("is_removed", False),
            "isTruncated": m.get("is_truncated", False),
            "atOutChatterIds": m.get("at_out_chatter_ids", []),
        }
    envelope = event_message(build_envelope(m.get("msg_id", ""), raw, dec, at_me_server=m.get("at_me")))
    return {
        "schema": 2,
        "kind": "message",
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "msg_id": m.get("msg_id", ""),
        "chat_id": m.get("chat_id", ""),
        "chat_type": m.get("chat_type", 0),
        "from_id": m.get("from_id", ""),
        "type": m.get("type", 0),
        "create_time": m.get("create_time", 0),
        "parent_id": m.get("parent_id", ""),
        "root_id": m.get("root_id", ""),
        "thread_id": m.get("thread_id", ""),
        "is_removed": m.get("is_removed", False),
        "text": dec.get("text") or m.get("text", ""),
        "mention_user_ids": dec.get("mention_user_ids", []),
        "at_me": at_me,
        "at_me_confidence": confidence,
        "at_me_sources": sources,
        "at_all": at_all,
        "is_from_me": bool(my_id) and m.get("from_id") == my_id,
        "decode_status": dec.get("decode_status", "unknown"),
        "resources": envelope.get("resources", []),
        "nested_messages": envelope.get("nested_messages", []),
        "decode": envelope.get("decode", {}),
        "complete": envelope.get("complete", False),
        "message": envelope,
        "context_status": "pending",
    }


def _should_store(m: dict, my_id: str) -> bool:
    """Persist only inbound P2P or group/topic messages that address me.

    @所有人 decodes to the literal mention id ``"all"`` (verified 2026-09-10:
    ``<at user_id="all">@所有人</at>``); it addresses every member, so those
    messages are stored too — an @all announcement is exactly the kind of
    event a user expects ``events`` to surface.
    """
    if not m.get("msg_id") or m.get("from_id") == my_id or m.get("is_removed"):
        return False
    chat_type = int(m.get("chat_type") or 0)
    if chat_type == 1:  # every inbound P2P message is addressed to this user
        return True
    if chat_type not in (2, 3):
        return False
    raw = m.get("raw") or {}
    dec = decode_message(raw) if raw else {"mention_user_ids": [], "decode_status": "unknown"}
    return bool(
        m.get("at_me")
        or my_id in (m.get("at_out_chatter_ids") or [])
        or my_id in dec.get("mention_user_ids", [])
        or "all" in dec.get("mention_user_ids", [])
    )


async def _context_worker(queue: asyncio.Queue, stop: asyncio.Event, my_id: str) -> None:
    """Fetch mention context off the listener path so ACKs stay prompt."""
    from . import context

    while not stop.is_set():
        try:
            trigger = await asyncio.wait_for(queue.get(), timeout=1)
        except asyncio.TimeoutError:
            continue
        try:
            ctx = await _context_with_query_lock(context, trigger)
            bundle = context.compact_context(ctx)
            event_store.append_event({
                "schema": 2,
                "kind": "mention_context",
                "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "trigger_msg_id": trigger["msg_id"],
                "chat_id": trigger.get("chat_id", ""),
                "from_id": trigger.get("from_id", ""),
                "context": bundle,
                "complete": bundle["complete"],
                "truncated": bundle["truncated"],
                "missing_parent_ids": bundle["missing_parent_ids"],
            })
        except Exception as exc:
            # The trigger event is already durable. Keep a durable failure
            # marker instead of blocking the listener or silently losing state.
            try:
                event_store.append_event({
                    "schema": 2,
                    "kind": "mention_context_error",
                    "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "trigger_msg_id": trigger.get("msg_id", ""),
                    "chat_id": trigger.get("chat_id", ""),
                    "complete": False,
                    "truncated": False,
                    "error": type(exc).__name__,
                })
            except Exception:
                pass
        finally:
            queue.task_done()


async def _context_with_query_lock(context, trigger: dict) -> dict:
    """Run one context query without competing with a CLI query.

    The listener remains independent of this worker, so waiting for the lock
    cannot delay WebSocket ACKs.  A busy lock is retried only within the same
    bounded context deadline; it is never allowed to queue unbounded work.
    """
    from . import query_lock

    deadline = asyncio.get_running_loop().time() + config.CONTEXT_TIMEOUT_SECONDS
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("context query deadline exceeded while waiting for query lock")
        try:
            with query_lock.acquire("context"):
                return await asyncio.wait_for(
                    context.thread_context(
                        trigger["msg_id"],
                        window_min=min(15, config.MAX_THREAD_WINDOW_MINUTES),
                        limit=config.CONTEXT_MAX_MESSAGES,
                    ),
                    timeout=remaining,
                )
        except QueryBusy:
            await asyncio.sleep(min(0.5, remaining))


async def _keepalive(cookies: dict, domain: str, stop: asyncio.Event) -> None:
    """Touch the account endpoint so the web session never idles out."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.WS_KEEPALIVE_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await auth.touch(cookies, domain)
            _write_state(keepalive_ok=True)
        except AuthRequired:
            raise
        except Exception as e:
            note(f"  ! keepalive 失败（不致命）：{scrubbed_exc(e)}")


class _StorageLocked(Exception):
    """Encryption keyring locked (e.g. daemon started before desktop login).

    Handled by dropping the WebSocket (un-ACKed messages are re-pushed on
    reconnect) and waiting for the key instead of exiting — systemd is
    configured to *not* restart on exit 3, so exiting here used to leave the
    daemon dead until someone noticed.
    """


async def _wait_for_storage_unlock(stop: asyncio.Event) -> None:
    """Poll until the encrypted event store becomes writable; give up after
    ~20 minutes (then exit 3 so the failure is loud, not silent)."""
    for _ in range(40):
        if stop.is_set():
            return
        storage = event_store.prepare_storage()
        if storage.get("encrypted") or not storage.get("encrypted_store_locked"):
            note("  ✓ 事件池密钥已恢复，继续写入")
            _write_state(last_error="")
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
    raise SystemExit(3)


async def run_async() -> None:
    config.ensure_home()
    lock_fd = open(config.DAEMON_LOCK, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise FeishuError("daemon 已在运行（daemon.lock 被持有）", hint="feishu.py daemon status")
    os.chmod(config.DAEMON_LOCK, 0o600)

    data = auth.load_cookies()
    cookies = data["cookies"]
    try:
        cookies = await auth.fetch_csrf(cookies)
        me = await auth.whoami(cookies)
    except AuthRequired:
        _notify("飞书会话已过期，请重新运行 feishu.py login")
        raise
    auth.save_cookies(cookies, data.get("domain") or config.DOMAIN)
    my_id = me["user_id"]
    storage = event_store.prepare_storage()
    if not storage.get("encrypted"):
        if storage.get("encrypted_store_locked"):
            note("  ! 加密事件池密钥不可用，daemon 将暂停事件写入（可运行 events status 查看）")
        else:
            note("  ! 事件池未启用密钥环加密，当前使用 0600 权限保护（可运行 events status 查看）")
    _write_state(pid=os.getpid(), my_user_id=my_id, my_name=me["name"], events_written=0)

    transport.configure()
    from lark_bridge import listener  # patched vendor listener

    cookie_header = auth.cookie_header(cookies)
    register_secret(cookie_header)
    seen = event_store.recent_msg_ids(config.MAX_SEEN_MSG_IDS)
    seen_order = deque(seen, maxlen=config.MAX_SEEN_MSG_IDS)
    written = 0
    stop = asyncio.Event()
    context_queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    ka = asyncio.create_task(_keepalive(cookies, data.get("domain") or config.DOMAIN, stop))
    context_task = asyncio.create_task(_context_worker(context_queue, stop, my_id))
    try:
        while not stop.is_set():
            try:
                async for m in listener.listen(
                    cookie_header, cookies, None, data.get("domain") or config.DOMAIN, ""
                ):
                    if m["msg_id"] in seen:
                        continue
                    seen.add(m["msg_id"])
                    seen_order.append(m["msg_id"])
                    while len(seen) > config.MAX_SEEN_MSG_IDS:
                        seen.discard(seen_order.popleft())
                    if m.get("is_removed"):
                        continue  # tombstones carry no content
                    if not _should_store(m, my_id):
                        continue
                    ev = _build_event(m, my_id)
                    if event_store.append_event(ev) is False:
                        storage_state = event_store.storage_status()
                        if storage_state.get("encrypted_store_locked"):
                            _write_state(ws_connected=False,
                                         last_error="event encryption key unavailable")
                            _notify("飞书事件池密钥不可用，daemon 等待密钥环解锁（不退出）")
                            raise _StorageLocked()
                        _write_state(ws_connected=False, last_error="event storage quota reached")
                        _notify("飞书事件池已达到磁盘配额，daemon 暂停写入")
                        raise SystemExit(3)
                    written += 1
                    _write_state(
                        last_event_at=ev["received_at"], events_written=written, ws_connected=True
                    )
                    if ev["at_me"]:
                        _notify(f"有人在群里 @ 了你：{ev['text'][:80] or '[非文本消息]'}")
                    elif ev.get("at_all"):
                        _notify(f"群里有人 @ 了所有人：{ev['text'][:80] or '[非文本消息]'}")
                    if ev["at_me"] or ev.get("at_all") or ev.get("chat_type") == 1:
                        try:
                            context_queue.put_nowait(ev)
                        except asyncio.QueueFull:
                            event_store.append_event({
                                "schema": 2,
                                "kind": "mention_context_error",
                                "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                "trigger_msg_id": ev["msg_id"],
                                "chat_id": ev.get("chat_id", ""),
                                "complete": False,
                                "truncated": False,
                                "error": "context_queue_full",
                            })
            except _StorageLocked:
                # listener.listen exited → WS closed → un-ACKed messages will
                # be re-pushed when we reconnect after the key comes back.
                await _wait_for_storage_unlock(stop)
                continue
    except AuthRequired:
        _notify("飞书会话已过期，请重新运行 feishu.py login")
        _write_state(ws_connected=False, last_error="auth expired")
        stop.set()
        raise SystemExit(2)
    finally:
        stop.set()
        ka.cancel()
        context_task.cancel()
        await asyncio.gather(ka, context_task, return_exceptions=True)


def run() -> None:
    try:
        asyncio.run(run_async())
    except KeyboardInterrupt:
        pass


# --- process management -------------------------------------------------------

def _venv_python() -> str:
    p = config.VENV / "bin" / "python"
    return str(p) if p.exists() else sys.executable


def _feishu_py() -> str:
    return str((Path(__file__).resolve().parent.parent / "feishu.py"))


def _systemd_ok() -> bool:
    if not shutil.which("systemctl"):
        return False
    r = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True, text=True, timeout=5,
    )
    return r.stdout.strip() in ("running", "degraded")


def _unit_path():
    return Path.home() / ".config" / "systemd" / "user" / _UNIT


def _write_unit() -> None:
    unit = f"""[Unit]
Description=feishu skill WS daemon (AgentHands)

[Service]
ExecStart={_venv_python()} {_feishu_py()} daemon run
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=2 3
MemoryHigh=384M
MemoryMax=512M
TasksMax=128
KillMode=control-group
OOMPolicy=kill
Environment=FEISHU_HOME={config.HOME}

[Install]
WantedBy=default.target
"""
    _unit_path().parent.mkdir(parents=True, exist_ok=True)
    _unit_path().write_text(unit)


def start() -> None:
    if _systemd_ok():
        _write_unit()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", _UNIT], check=True)
        note(f"✓ 已通过 systemd --user 启动（{_UNIT}）")
        return
    # fallback: nohup-style detached child; flock in run() enforces singleton
    log = open(config.DAEMON_LOG, "ab")
    proc = subprocess.Popen(
        [_venv_python(), _feishu_py(), "daemon", "run"],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "FEISHU_HOME": str(config.HOME)},
    )
    config.DAEMON_PID.write_text(str(proc.pid))
    os.chmod(config.DAEMON_PID, 0o600)
    note(f"✓ 已后台启动（pid {proc.pid}，日志 {config.DAEMON_LOG}）")


def _live_pid() -> int:
    try:
        pid = int(config.DAEMON_PID.read_text().strip())
    except (OSError, ValueError):
        return 0
    try:
        cmdline = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")
    except OSError:
        return 0
    # never signal a pid that isn't clearly our own daemon
    if "feishu.py" in cmdline and "daemon" in cmdline:
        return pid
    return 0


def stop() -> None:
    if _systemd_ok():
        subprocess.run(["systemctl", "--user", "stop", _UNIT], check=False)
        subprocess.run(["systemctl", "--user", "disable", _UNIT], check=False)
        note("✓ 已停止（systemd）")
        return
    pid = _live_pid()
    if not pid:
        note("daemon 没在运行")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not os.path.exists(f"/proc/{pid}"):
            break
        time.sleep(0.1)
    note(f"✓ 已停止（pid {pid}）")


def status() -> dict:
    state = {}
    if config.DAEMON_STATE.exists():
        try:
            state = json.loads(config.DAEMON_STATE.read_text())
        except Exception:
            pass
    mgr = "none"
    running = False
    if _systemd_ok():
        mgr = "systemd"
        r = subprocess.run(
            ["systemctl", "--user", "is-active", _UNIT],
            capture_output=True, text=True,
        )
        running = r.stdout.strip() == "active"
    else:
        mgr = "pidfile"
        running = bool(_live_pid())
    return {"manager": mgr, "running": running, **state}


def tail(lines: int = 50) -> None:
    if _systemd_ok():
        subprocess.run(
            ["journalctl", "--user", "-u", _UNIT, "-n", str(lines), "--no-pager"],
            check=False,
        )
        return
    subprocess.run(["tail", "-n", str(lines), str(config.DAEMON_LOG)], check=False)
