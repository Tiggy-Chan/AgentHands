"""Read-only message resource resolution and bounded streaming downloads."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

from lark_bridge._urls import DRIVE_STREAM

from . import config, gateway, media_store, transport
from .errors import EndpointNotAdapted, ResourceError
from .message_model import public_message, public_resource


_TOKEN_RE = re.compile(r"^[^\s\x00]{1,4096}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
_MAX_OUT_NAME = 255
_KNOWN_DOWNLOAD_KINDS = frozenset({"file", "cloud_file", "document"})
_DIRECT_HOSTS = frozenset({urlsplit(DRIVE_STREAM).netloc, config.DOMAIN, "www.feishu.cn"})


def _resource_key(message: dict, resource: dict) -> str:
    rid = str(resource.get("resource_id") or "")
    private = message.get("_resource_keys") or {}
    return str(private.get(rid) or resource.get("_key") or "")


def _resource_data(message: dict, resource: dict) -> bytes:
    rid = str(resource.get("resource_id") or "")
    private = message.get("_resource_data") or {}
    value = private.get(rid) or resource.get("_data") or b""
    return value if isinstance(value, bytes) else b""


async def fetch_message(msg_id: str) -> dict:
    if not _MESSAGE_ID_RE.fullmatch(str(msg_id or "")):
        raise ResourceError("消息 ID 格式不合法")
    messages = await gateway.fetch_messages([msg_id])
    if not messages:
        raise ResourceError(f"取不到消息 {msg_id}（id 不对，或无权限，或已删除）")
    return messages[0]


async def list_resources(msg_id: str) -> dict:
    message = await fetch_message(msg_id)
    return {
        "message": public_message(message),
        "resources": [public_resource(item) for item in message.get("resources", [])],
    }


async def probe_metadata(msg_id: str) -> dict:
    """Return resource/adapter metadata without downloading bodies or media."""
    message = await fetch_message(msg_id)
    adapters = []
    for resource in message.get("resources", []):
        item = public_resource(resource)
        try:
            if _resource_data(message, resource):
                adapter = "inline"
            else:
                _url_for({**resource, "_key": _resource_key(message, resource)})
                adapter = "drive_stream" if resource.get("kind") in _KNOWN_DOWNLOAD_KINDS else "direct_url"
            item["adapter"] = adapter
            item["endpoint_status"] = "registered"
        except EndpointNotAdapted:
            item["adapter"] = None
            item["endpoint_status"] = "not_adapted"
        except ResourceError:
            item["adapter"] = None
            item["endpoint_status"] = "invalid_reference"
        adapters.append(item)
    return {
        "schema": 1,
        "scope": "message-media",
        "message_id": msg_id,
        "message_type": message.get("type"),
        "resources": adapters,
        "contains_bodies": False,
        "contains_credentials": False,
    }


def select_resource(message: dict, resource_id_value: str = "") -> dict:
    resources = list(message.get("resources", []))
    if not resources:
        raise ResourceError("这条消息没有可处理的资源")
    if resource_id_value:
        for resource in resources:
            if resource.get("resource_id") == resource_id_value:
                selected = dict(resource)
                selected["_key"] = _resource_key(message, resource)
                selected["_data"] = _resource_data(message, resource)
                if not selected["_key"] and not selected["_data"]:
                    raise EndpointNotAdapted("消息资源只有元数据，尚未取得可下载的资源 key")
                return selected
        raise ResourceError("resource_id 不属于这条消息")
    if len(resources) != 1:
        raise ResourceError("这条消息包含多个资源，请指定 --resource-id")
    selected = dict(resources[0])
    selected["_key"] = _resource_key(message, resources[0])
    selected["_data"] = _resource_data(message, resources[0])
    if not selected["_key"] and not selected["_data"]:
        raise EndpointNotAdapted("消息资源只有元数据，尚未取得可下载的资源 key")
    return selected


def _url_for(resource: dict) -> str:
    kind = str(resource.get("kind") or "")
    key = str(resource.get("_key") or "")
    if not _TOKEN_RE.fullmatch(key):
        raise ResourceError("消息资源 key 格式不合法")

    # This is the already-verified Drive stream route used for file/cloud-file
    # tokens.  Image/audio/media routes are intentionally not guessed: once a
    # tenant-specific route is observed by probe it must be added explicitly.
    if kind in _KNOWN_DOWNLOAD_KINDS:
        return f"{DRIVE_STREAM}/space/api/box/stream/download/all/{quote(key, safe='')}/"

    # A post may contain an absolute, already-rendered image URL.  Permit it
    # only for the small host set already covered by transport policy; signed
    # or third-party URLs remain unavailable rather than becoming an SSRF
    # primitive.
    parsed = urlsplit(key)
    if parsed.scheme == "https" and parsed.netloc in _DIRECT_HOSTS and not parsed.query:
        return key

    raise EndpointNotAdapted(
        f"{kind or 'unknown'} 消息资源下载接口尚未登记",
        hint="先通过受控 probe 观察真实 host/path，再加入资源 provider 白名单",
    )


def _safe_output(path_text: str) -> Path:
    target = Path(path_text).expanduser()
    if not target.name or len(target.name) > _MAX_OUT_NAME:
        raise ResourceError("输出文件名不合法")
    if target.exists():
        raise ResourceError(f"输出文件已存在，拒绝覆盖：{target}")
    if not target.parent.exists() or not target.parent.is_dir():
        raise ResourceError("输出文件的父目录不存在")
    return target


def _install_output(source: Path, target: Path) -> None:
    """Install a user-requested output atomically without overwriting it."""
    if target.exists():
        raise ResourceError(f"输出文件已存在，拒绝覆盖：{target}")
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, target)
        os.chmod(target, 0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _sniff_mime(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return ""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and b"WEBP" in head[:16]:
        return "image/webp"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if head.startswith(b"ID3") or head.startswith(b"\xff\xfb"):
        return "audio/mpeg"
    return ""


async def download_resource(
    message: dict,
    resource: dict,
    *,
    keep_media: bool = False,
    out: str = "",
) -> dict:
    """Stream one resource to a private temp file, cache, or explicit output."""
    target = _safe_output(out) if out else None
    fd, temp_name = tempfile.mkstemp(prefix="feishu-resource-", dir=str(config.HOME))
    os.close(fd)
    temp = Path(temp_name)
    os.chmod(temp, 0o600)
    total = 0
    digest = hashlib.sha256()
    content_type = ""
    try:
        inline = resource.get("_data") or b""
        if inline:
            if len(inline) > config.MESSAGE_RESOURCE_MAX_BYTES:
                raise ResourceError("消息资源超过单文件大小上限")
            temp.write_bytes(inline)
            total = len(inline)
            digest.update(inline)
            content_type = str(resource.get("mime") or "")
        else:
            url = _url_for(resource)
            headers = transport.session_headers({"accept-encoding": "identity"})
            async with transport.stream_request(
                "GET",
                url,
                headers=headers,
                timeout=config.MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
                max_response_bytes=config.MESSAGE_RESOURCE_MAX_BYTES,
            ) as response:
                if response.status_code >= 400:
                    raise ResourceError(f"消息资源下载失败（HTTP {response.status_code}）")
                if response.headers.get("content-encoding", "").lower() not in {"", "identity"}:
                    raise ResourceError("消息资源返回了不支持的压缩编码，已拒绝写入")
                content_type = str(response.headers.get("content-type", "")).split(";", 1)[0].strip()
                try:
                    declared = int(response.headers.get("content-length", "0"))
                except ValueError:
                    declared = 0
                if declared > config.MESSAGE_RESOURCE_MAX_BYTES:
                    raise ResourceError("消息资源超过单文件大小上限")
                with temp.open("wb") as f:
                    async for chunk in response.aiter_raw():
                        total += len(chunk)
                        if total > config.MESSAGE_RESOURCE_MAX_BYTES:
                            raise ResourceError("消息资源超过单文件大小上限")
                        f.write(chunk)
                        digest.update(chunk)
                    f.flush()
                    os.fsync(f.fileno())
        if total > config.MEDIA_JOB_MAX_BYTES:
            raise ResourceError("媒体任务超过总输入大小上限")
        sniffed = _sniff_mime(temp)
        result = {
            "resource_id": resource.get("resource_id"),
            "kind": resource.get("kind"),
            "status": "downloaded",
            "bytes": total,
            "sha256": digest.hexdigest(),
            "mime": content_type or resource.get("mime") or sniffed or mimetypes.guess_type(resource.get("name", ""))[0] or "application/octet-stream",
            "sniffed_mime": sniffed,
            "name": resource.get("name", ""),
        }
        if keep_media:
            cache = media_store.store(temp, str(resource.get("resource_id")))
            result["cache_path"] = str(cache)
        if target:
            _install_output(temp, target)
            result["path"] = str(target)
        return result
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def resource_from_message(message: dict, resource_id_value: str = "") -> dict:
    return select_resource(message, resource_id_value)
