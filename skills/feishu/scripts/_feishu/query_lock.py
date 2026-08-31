"""Per-profile lock for expensive read operations."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager

from . import config
from .errors import QueryBusy


@contextmanager
def acquire(operation: str):
    config.ensure_home()
    fd = os.open(config.QUERY_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise QueryBusy(
                f"已有重查询正在运行（{config.QUERY_LOCK.name}）",
                hint=f"等待当前任务结束后再运行 {operation}",
            ) from exc
        os.fchmod(fd, 0o600)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
