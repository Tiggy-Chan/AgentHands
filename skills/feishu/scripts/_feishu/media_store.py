"""Bounded encrypted storage for explicitly retained message media."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import time
from pathlib import Path

from . import config, secure_store
from .errors import ResourceError


def _cache_name(resource_id: str) -> str:
    digest = hashlib.sha256(str(resource_id).encode("utf-8", errors="replace")).hexdigest()
    return f"{digest}.media.enc"


def cache_path(resource_id: str) -> Path:
    return config.MEDIA_CACHE_DIR / _cache_name(resource_id)


def _files() -> list[Path]:
    if not config.MEDIA_CACHE_DIR.exists():
        return []
    return sorted(
        (path for path in config.MEDIA_CACHE_DIR.glob("*.media.enc") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )


def cache_bytes() -> int:
    total = 0
    for path in _files():
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def status() -> dict:
    return {
        "directory": str(config.MEDIA_CACHE_DIR),
        "files": len(_files()),
        "bytes": cache_bytes(),
        "quota_bytes": config.MEDIA_CACHE_MAX_BYTES,
        "encrypted": secure_store.mode() == "encrypted",
    }


def store(source: Path, resource_id: str) -> Path:
    """Encrypt ``source`` into the cache after checking the global quota."""
    if secure_store.mode() != "encrypted":
        raise ResourceError(
            "媒体原件保存需要可用的加密密钥环",
            hint="不保存原件时仍可执行 OCR/ASR；不要把 FEISHU_EVENT_ENCRYPTION 关闭后保存媒体",
        )
    source = Path(source)
    if not source.is_file():
        raise ResourceError("媒体临时文件不存在")
    target = cache_path(resource_id)
    config.MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(config.MEDIA_CACHE_DIR, 0o700)
    if target.exists():
        return target
    try:
        source_size = source.stat().st_size
    except OSError as exc:
        raise ResourceError("无法读取媒体临时文件大小") from exc
    # The encrypted representation is slightly larger than the input.  Leave
    # a small authenticated-record overhead budget before writing.
    if cache_bytes() + source_size + 64 * 1024 > config.MEDIA_CACHE_MAX_BYTES:
        raise ResourceError(
            "媒体缓存已达到配额，拒绝保存原件",
            hint="清理或移动旧媒体后再使用 --keep-media",
        )
    lock_path = config.MEDIA_CACHE_DIR / ".media.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            return target
        if cache_bytes() + source_size + 64 * 1024 > config.MEDIA_CACHE_MAX_BYTES:
            raise ResourceError(
                "媒体缓存已达到配额，拒绝保存原件",
                hint="清理或移动旧媒体后再使用 --keep-media",
            )
        try:
            secure_store.encrypt_file(source, target)
        except secure_store.SecureStoreError as exc:
            raise ResourceError("媒体加密保存失败") from exc
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return target


def materialize(resource_id: str, *, max_bytes: int | None = None) -> Path:
    """Decrypt a retained media object into a private temporary file."""
    source = cache_path(resource_id)
    if not source.exists():
        raise ResourceError("媒体缓存不存在")
    fd, name = tempfile.mkstemp(prefix="feishu-media-", dir=str(config.MEDIA_CACHE_DIR))
    os.close(fd)
    target = Path(name)
    try:
        secure_store.decrypt_file(source, target, max_output_bytes=max_bytes)
    except Exception as exc:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        if isinstance(exc, ResourceError):
            raise
        raise ResourceError("媒体缓存解密失败") from exc
    os.chmod(target, 0o600)
    return target


def purge_all() -> dict:
    """Move retained media to a recoverable archive; never unlink silently."""
    files = _files()
    if not files:
        return {"removed": 0, "moved_to": None}
    archive = config.HOME / f"media.purged.{int(time.time())}"
    archive.mkdir(mode=0o700)
    moved = 0
    for path in files:
        try:
            os.replace(path, archive / path.name)
            moved += 1
        except OSError:
            continue
    return {"removed": moved, "moved_to": str(archive)}
