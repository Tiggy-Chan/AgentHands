"""Dual-gate safety mechanism for QQ writes."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from . import config


class WriteBlockedError(Exception):
    """Raised when an operation is blocked by write gates."""
    pass


def fingerprint(action: str, payload: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 fingerprint for write intentions."""
    raw = json.dumps(
        {"action": action, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def make_draft(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Generate structured draft receipt for user verification."""
    fp = fingerprint(action, payload)
    return {
        "status": "draft",
        "action": action,
        "fingerprint": fp,
        "payload": payload,
        "instructions": (
            f"Review the draft above. To execute, pass: --execute --fingerprint {fp} "
            f"or set {config.WRITE_ALLOWED_ENV}=1"
        ),
    }


def require_write_allowed(
    action: str,
    payload: dict[str, Any],
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> None:
    """Enforce physical environment gate and fingerprint match."""
    current_fp = fingerprint(action, payload)

    if direct:
        return

    env_allowed = os.environ.get(config.WRITE_ALLOWED_ENV) == "1"

    if expected_fp and expected_fp != current_fp:
        raise WriteBlockedError(
            f"Fingerprint mismatch! Content changed. Expected: {expected_fp}, Actual: {current_fp}"
        )

    if not execute and not env_allowed:
        draft = make_draft(action, payload)
        raise WriteBlockedError(
            f"Write action '{action}' requires explicit confirmation. "
            f"Draft fingerprint: {current_fp}. Set {config.WRITE_ALLOWED_ENV}=1 or pass --execute --fingerprint {current_fp}"
        )
