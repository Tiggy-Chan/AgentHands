"""Versioned, bounded and optionally encrypted JSONL event storage.

Relevant events are long-lived company data. The active store uses the
Secret-Service-backed AES-GCM mode when available and otherwise falls back to
0600 files under the 0700 FEISHU_HOME directory. Legacy plaintext files are
never deleted during migration.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import config, secure_store
from .output import note

_warned_key_error = False


def _time_ms(value) -> int:
    """Normalize protobuf JSON numbers (often strings) to epoch milliseconds."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(v if v > 1e12 else v * 1000) if v else 0


def _event_time_ms(event: dict) -> int:
    value = _time_ms(event.get("create_time"))
    if value:
        return value
    received = event.get("received_at")
    if received:
        try:
            return int(datetime.strptime(received, "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000)
        except (TypeError, ValueError, OverflowError):
            pass
    return 0


def event_time_ms(event: dict) -> int:
    """Public normalized event timestamp for other local consumers."""
    return _event_time_ms(event)


def _rotated(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _rotated_paths(path: Path) -> list[Path]:
    """Return every numeric generation, oldest first, without dropping any."""
    found = []
    prefix = path.name + "."
    for candidate in path.parent.glob(prefix + "*"):
        suffix = candidate.name[len(prefix):]
        if suffix.isdigit():
            found.append((int(suffix), candidate))
    found.sort()  # .1 is oldest; the highest generation is newest
    return [p for _, p in found] + ([path] if path.exists() else [])


def _active_path() -> Path:
    return config.EVENTS_ENCRYPTED_FILE if secure_store.mode() == "encrypted" else config.EVENTS_FILE


def _encrypted_paths() -> list[Path]:
    return _rotated_paths(config.EVENTS_ENCRYPTED_FILE)


def _plain_paths() -> list[Path]:
    # v1 files are migration archives. The ordinary names remain supported for
    # a fresh installation where the keyring is unavailable.
    paths = _rotated_paths(config.EVENTS_LEGACY_FILE)
    paths += _rotated_paths(config.EVENTS_FILE)
    return paths


def _all_storage_files() -> list[Path]:
    return [p for p in _plain_paths() + _encrypted_paths() if p.exists()]


def _write_lines(path: Path, events: list[dict], encrypted: bool) -> None:
    """Stream records to an fsynced temporary file and atomically install it."""
    _write_event_iter(path, events, encrypted)


def _write_event_iter(path: Path, events, encrypted: bool) -> int:
    """Write an iterator without materializing a long-lived event archive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(name)
    count = 0
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for event in events:
                line = secure_store.encrypt_record(event) if encrypted else json.dumps(
                    event, ensure_ascii=False, separators=(",", ":")
                )
                f.write(line + "\n")
                count += 1
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return count


def _read_plain_file(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    yield event
            except json.JSONDecodeError:
                note(f"  ! 跳过损坏行 {path.name}:{ln}（多半是上次崩溃的半行）")


def _read_plain_file_ordered():
    # Do not include the same path twice if a future config uses overlapping
    # names.
    seen: set[Path] = set()
    for path in _plain_paths():
        if path in seen:
            continue
        seen.add(path)
        yield from _read_plain_file(path)


def _migrate_legacy() -> dict:
    """Copy v1 plaintext records into the encrypted active store."""
    if secure_store.mode() != "encrypted" or config.EVENTS_ENCRYPTED_FILE.exists():
        return {"migrated": 0, "archive": None}
    legacy = [p for p in _plain_paths() if p.exists()]
    if not legacy:
        return {"migrated": 0, "archive": None}
    migrated = _write_event_iter(
        config.EVENTS_ENCRYPTED_FILE, _read_plain_file_ordered(), encrypted=True
    )
    # Rename only after the encrypted copy is complete; this is recoverable.
    moved = 0
    for path in legacy:
        if path == config.EVENTS_FILE:
            dest = config.EVENTS_LEGACY_FILE
        elif path.name.startswith(config.EVENTS_FILE.name + "."):
            suffix = path.name[len(config.EVENTS_FILE.name):]
            dest = config.EVENTS_LEGACY_FILE.with_name(config.EVENTS_LEGACY_FILE.name + suffix)
        else:
            continue
        if dest.exists():
            dest = dest.with_name(dest.name + f".{int(time.time())}")
        os.replace(path, dest)
        os.chmod(dest, 0o600)
        moved += 1
    return {"migrated": migrated, "archive": str(config.EVENTS_LEGACY_FILE) if moved else None}


def prepare_storage() -> dict:
    """Ensure the active store is ready; daemon calls this at startup."""
    config.ensure_home()
    result = _migrate_legacy()
    return {**secure_store.status(), **result, "active_file": str(_active_path())}


def append_event(ev: dict) -> bool:
    """Append one record with an exclusive lock and fsync before returning."""
    config.ensure_home()
    current = sum(p.stat().st_size for p in _all_storage_files() if p.exists())
    if config.EVENTS_TOTAL_MAX_BYTES and current >= config.EVENTS_TOTAL_MAX_BYTES:
        note("  ! 事件池已达到磁盘配额，暂停写入；请运行 events purge 明确清理旧数据")
        return False
    if secure_store.mode() == "encrypted" and not config.EVENTS_ENCRYPTED_FILE.exists():
        _migrate_legacy()
    path = _active_path()
    encrypted = secure_store.mode() == "encrypted"
    line = (secure_store.encrypt_record(ev) if encrypted else json.dumps(
        ev, ensure_ascii=False, separators=(",", ":")
    )) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        os.chmod(path, 0o600)
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    maybe_rotate(path)
    _check_quota()
    return True


def maybe_rotate(path: Path | None = None) -> None:
    path = path or _active_path()
    try:
        if not path.exists() or path.stat().st_size < config.EVENTS_MAX_BYTES:
            return
        generations = [
            int(p.name[len(path.name) + 1:])
            for p in path.parent.glob(path.name + ".*")
            if p.name[len(path.name) + 1:].isdigit()
        ]
        next_generation = max(generations, default=0) + 1
        os.replace(path, _rotated(path, next_generation))
    except OSError as e:
        note(f"  ! 事件池轮转失败：{type(e).__name__}")


def _iter_encrypted():
    global _warned_key_error
    for path in _encrypted_paths():
        if not path.exists():
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = secure_store.decrypt_record(line)
                    if isinstance(event, dict):
                        yield event
                except Exception as exc:
                    if not _warned_key_error:
                        note(f"  ! 加密事件读取失败：{type(exc).__name__}（检查用户密钥环）")
                        _warned_key_error = True


def _iter_lines():
    """Yield all records, tolerating torn tails and unavailable old stores."""
    # Once an encrypted active store exists it already contains the legacy
    # records copied during migration. Reading both would duplicate every
    # message and make long-term scans unnecessarily expensive.
    if secure_store.mode() == "encrypted" and _encrypted_paths():
        yield from _iter_encrypted()
    else:
        yield from _read_plain_file_ordered()


def read_events(
    since_ms: int = 0,
    until_ms: int = 0,
    chat_id: str = "",
    mentions_only: bool = False,
    include_mine: bool = False,
    limit: int = 200,
) -> list[dict]:
    """Filtered, msg_id-deduped, chronological events."""
    seen: set[str] = set()
    out: list[dict] = []
    for ev in _iter_lines():
        mid = ev.get("msg_id")
        if mid and mid in seen:
            continue
        ts = _event_time_ms(ev)
        if since_ms and ts and ts < since_ms:
            continue
        if until_ms and ts and ts > until_ms:
            continue
        if chat_id and ev.get("chat_id") != chat_id:
            continue
        if mentions_only and not ev.get("at_me"):
            continue
        if not include_mine and ev.get("is_from_me"):
            continue
        if mid:
            seen.add(mid)
        out.append(ev)
    out.sort(key=lambda e: (_event_time_ms(e), e.get("msg_id") or e.get("trigger_msg_id") or ""))
    return out[-limit:] if limit else out


def recent_msg_ids(n: int = 2000) -> set[str]:
    """Tail of the pool for daemon-side dedupe after reconnect."""
    ids: list[str] = []
    for ev in _iter_lines():
        if ev.get("msg_id"):
            ids.append(ev["msg_id"])
    return set(ids[-max(0, n):])


def chat_summaries(max_chats: int = 20_000) -> dict[str, dict]:
    """Build bounded type/time summaries without materializing all events."""
    summaries: dict[str, dict] = {}
    for ev in _iter_lines():
        if ev.get("kind", "message") != "message":
            continue
        cid = ev.get("chat_id")
        if not cid:
            continue
        if cid not in summaries and len(summaries) >= max_chats:
            continue
        current = summaries.setdefault(cid, {
            "chat_id": cid,
            "chat_type": ev.get("chat_type", 0),
            "last_time": 0,
            "last_text": "",
        })
        stamp = _event_time_ms(ev)
        if stamp >= current["last_time"]:
            current["last_time"] = stamp
            current["last_text"] = (ev.get("text") or "")[:200]
            current["chat_type"] = ev.get("chat_type", current["chat_type"])
    return summaries


def storage_status() -> dict:
    config.ensure_home()
    status = secure_store.status()
    total = sum(p.stat().st_size for p in _all_storage_files() if p.exists())
    status.update({
        "active_file": str(_active_path()),
        "bytes": total,
        "quota_bytes": config.EVENTS_TOTAL_MAX_BYTES,
        "quota_ratio": round(total / config.EVENTS_TOTAL_MAX_BYTES, 4)
        if config.EVENTS_TOTAL_MAX_BYTES else 0,
    })
    return status


def _check_quota() -> None:
    status = storage_status()
    if status["quota_ratio"] >= config.EVENTS_WARN_RATIO:
        note(f"  ! 事件池已使用 {status['quota_ratio']:.0%} 磁盘配额；长期保留模式不会自动删除")


def purge_before(before_ms: int) -> dict:
    """Move old storage aside and rewrite records newer than ``before_ms``."""
    if before_ms <= 0:
        raise ValueError("purge 需要有效的 before 时间")
    config.ensure_home()
    active = _active_path()
    encrypted = secure_store.mode() == "encrypted"
    fd, name = tempfile.mkstemp(prefix=f".{active.name}.purge.", dir=str(active.parent))
    tmp = Path(name)
    kept = removed = total = 0
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for event in _iter_lines():
                total += 1
                if _event_time_ms(event) < before_ms:
                    removed += 1
                    continue
                line = secure_store.encrypt_record(event) if encrypted else json.dumps(
                    event, ensure_ascii=False, separators=(",", ":")
                )
                f.write(line + "\n")
                kept += 1
            f.flush()
            os.fsync(f.fileno())
        archive = config.HOME / f"events.purged.{int(time.time())}"
        archive.mkdir(mode=0o700)
        for path in _all_storage_files():
            if path.exists():
                shutil.move(str(path), str(archive / path.name))
        os.replace(tmp, active)
        os.chmod(active, 0o600)
        return {"kept": kept, "moved_to": str(archive), "removed": removed, "total": total}
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
