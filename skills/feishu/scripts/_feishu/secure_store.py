"""Local protection for long-lived Feishu data.

The preferred mode stores one per-``FEISHU_HOME`` AES-256-GCM key in the
user's Secret Service keyring.  If the keyring is unavailable, callers may
fall back to ordinary files under the already-private 0700/0600 directory.
The fallback is explicit in :func:`status` and never pretends to be encrypted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import tempfile
from pathlib import Path

from . import config

_SERVICE = "agenthands.feishu"
_AAD = b"agenthands/feishu/v2"
_key: bytes | None = None
_mode: str | None = None
_backend: str = "unavailable"
_error: str = ""


class SecureStoreError(RuntimeError):
    """A requested encrypted operation could not be completed."""


def _account() -> str:
    # The account name is not a secret; it keeps separate FEISHU_HOME profiles
    # from sharing a key while avoiding the raw path in the keyring UI.
    return hashlib.sha256(str(config.HOME).encode()).hexdigest()[:24]


def _load_key() -> bytes | None:
    global _key, _mode, _backend, _error
    if _mode is not None:
        return _key

    preference = os.environ.get("FEISHU_EVENT_ENCRYPTION", "auto").lower()
    if preference == "off":
        _mode = "plaintext_permissions"
        _backend = "disabled_by_configuration"
        return None

    # 测试/CI 逃生门：进程级一次性密钥，绝不碰系统 keyring。代价是加密内容
    # 跨进程不可读——密钥随进程消失，仅限单进程内写完即读的场景（单测）。
    if os.environ.get("FEISHU_EPHEMERAL_KEY") == "1":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = secrets.token_bytes(32)
        AESGCM(key)  # 同 _load_key 的 keyring 分支：先构造，提早暴露坏安装
        _key = key
        _mode = "encrypted"
        _backend = "ephemeral_no_keyring"
        return _key

    try:
        import keyring
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        _backend = type(keyring.get_keyring()).__name__
        account = _account()
        encoded = keyring.get_password(_SERVICE, account)
        if encoded:
            key = base64.urlsafe_b64decode(encoded.encode())
        else:
            key = secrets.token_bytes(32)
            keyring.set_password(_SERVICE, account, base64.urlsafe_b64encode(key).decode())
        if len(key) != 32:
            raise SecureStoreError("keyring key has an invalid length")
        # Force a construction here so a broken cryptography installation is
        # detected before a file is marked encrypted.
        AESGCM(key)
        _key = key
        _mode = "encrypted"
        return _key
    except Exception as exc:
        _error = type(exc).__name__
        if preference == "on":
            raise SecureStoreError(
                "FEISHU_EVENT_ENCRYPTION=on 但 Secret Service/cryptography 不可用"
            ) from exc
        _mode = "plaintext_permissions"
        if _backend == "unavailable":
            _backend = "keyring_unavailable"
        return None


def mode() -> str:
    _load_key()
    return _mode or "plaintext_permissions"


def status() -> dict:
    _load_key()
    return {
        "mode": _mode,
        "encrypted": _mode == "encrypted",
        "keyring_backend": _backend,
        "keyring_error": _error or None,
    }


def encrypt_record(record: dict) -> str:
    key = _load_key()
    if key is None:
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    plain = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
    cipher = AESGCM(key).encrypt(nonce, plain, _AAD)
    return json.dumps(
        {
            "v": 2,
            "alg": "AES-256-GCM",
            "nonce": base64.urlsafe_b64encode(nonce).decode(),
            "ciphertext": base64.urlsafe_b64encode(cipher).decode(),
        },
        separators=(",", ":"),
    )


def decrypt_record(line: str) -> dict:
    envelope = json.loads(line)
    if envelope.get("v") != 2:
        if isinstance(envelope, dict):
            return envelope
        raise SecureStoreError("invalid event record")
    key = _load_key()
    if key is None:
        raise SecureStoreError("encrypted event key unavailable")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = base64.urlsafe_b64decode(envelope["nonce"])
    cipher = base64.urlsafe_b64decode(envelope["ciphertext"])
    plain = AESGCM(key).decrypt(nonce, cipher, _AAD)
    value = json.loads(plain.decode())
    if not isinstance(value, dict):
        raise SecureStoreError("decrypted record is not an object")
    return value


def encrypt_blob(data: bytes, *, aad: bytes = _AAD) -> bytes:
    key = _load_key()
    if key is None:
        raise SecureStoreError("encrypted blob requested while keyring is unavailable")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    cipher = AESGCM(key).encrypt(nonce, data, aad)
    return json.dumps(
        {
            "v": 2,
            "alg": "AES-256-GCM",
            "nonce": base64.urlsafe_b64encode(nonce).decode(),
            "ciphertext": base64.urlsafe_b64encode(cipher).decode(),
        },
        separators=(",", ":"),
    ).encode()


def decrypt_blob(data: bytes, *, aad: bytes = _AAD) -> bytes:
    envelope = json.loads(data.decode())
    key = _load_key()
    if key is None:
        raise SecureStoreError("encrypted blob key unavailable")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = base64.urlsafe_b64decode(envelope["nonce"])
    cipher = base64.urlsafe_b64decode(envelope["ciphertext"])
    return AESGCM(key).decrypt(nonce, cipher, aad)


_MEDIA_MAGIC = b"AH-MEDIA-CHUNKED-1\n"
_MEDIA_CHUNK_SIZE = 1024 * 1024


def _chunk_aad(aad: bytes, index: int) -> bytes:
    return aad + b"/chunk/" + str(index).encode("ascii")


def encrypt_file(source: Path, target: Path, *, aad: bytes = _AAD, chunk_size: int = _MEDIA_CHUNK_SIZE) -> int:
    """Encrypt a file incrementally so media never needs a giant byte buffer."""
    key = _load_key()
    if key is None:
        raise SecureStoreError("encrypted file requested while keyring is unavailable")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    chunk_size = max(64 * 1024, min(int(chunk_size), 8 * 1024 * 1024))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    tmp = Path(name)
    total = 0
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            header = json.dumps(
                {"v": 3, "alg": "AES-256-GCM-CHUNKED", "chunk_size": chunk_size},
                separators=(",", ":"),
            ).encode()
            out.write(_MEDIA_MAGIC)
            out.write(header + b"\n")
            for index in range(1 << 31):
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                nonce = secrets.token_bytes(12)
                cipher = AESGCM(key).encrypt(nonce, chunk, _chunk_aad(aad, index))
                out.write(struct.pack(">I", len(cipher)))
                out.write(nonce)
                out.write(cipher)
                total += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        return total
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def decrypt_file(
    source: Path,
    target: Path,
    *,
    aad: bytes = _AAD,
    max_output_bytes: int | None = None,
) -> int:
    """Decrypt a chunked media file incrementally with an optional output cap."""
    key = _load_key()
    if key is None:
        raise SecureStoreError("encrypted file key unavailable")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    tmp = Path(name)
    total = 0
    try:
        os.fchmod(fd, 0o600)
        with source.open("rb") as src, os.fdopen(fd, "wb") as out:
            if src.readline() != _MEDIA_MAGIC:
                raise SecureStoreError("invalid encrypted media header")
            try:
                header = json.loads(src.readline().decode("utf-8"))
                if header.get("v") != 3 or header.get("alg") != "AES-256-GCM-CHUNKED":
                    raise ValueError
                chunk_size = int(header.get("chunk_size", _MEDIA_CHUNK_SIZE))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise SecureStoreError("invalid encrypted media metadata") from exc
            for index in range(1 << 31):
                length_bytes = src.read(4)
                if not length_bytes:
                    break
                if len(length_bytes) != 4:
                    raise SecureStoreError("truncated encrypted media length")
                length = struct.unpack(">I", length_bytes)[0]
                if length < 16 or length > chunk_size + 16:
                    raise SecureStoreError("invalid encrypted media chunk size")
                nonce = src.read(12)
                cipher = src.read(length)
                if len(nonce) != 12 or len(cipher) != length:
                    raise SecureStoreError("truncated encrypted media chunk")
                plain = AESGCM(key).decrypt(nonce, cipher, _chunk_aad(aad, index))
                total += len(plain)
                if max_output_bytes is not None and total > max_output_bytes:
                    raise SecureStoreError("decrypted media exceeded the configured limit")
                out.write(plain)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        return total
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def atomic_write(path: Path, data: bytes) -> None:
    """Write a protected cache file without exposing a partial record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def reset_for_tests() -> None:
    global _key, _mode, _backend, _error
    _key = None
    _mode = None
    _backend = "unavailable"
    _error = ""
