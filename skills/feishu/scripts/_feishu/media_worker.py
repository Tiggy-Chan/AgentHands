"""Systemd-cgroup worker for message media download and local processing."""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ""):
    _SCRIPTS = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_SCRIPTS))
    sys.path.insert(0, str(_SCRIPTS / "vendor"))
    __package__ = "_feishu"

from . import config, media_processing, message_resources
from .errors import MediaWorkerError, ResourceIsolationUnavailable
from .message_model import public_resource
from .output import scrub, untrusted


_OPERATIONS = frozenset({"metadata", "download", "inspect", "ocr", "transcribe"})


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
    return result.returncode == 0 and result.stdout.strip() in {"running", "degraded"}


def _worker_python() -> str:
    candidate = config.VENV / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def _worker_script() -> str:
    return str(Path(__file__).resolve())


def _unit_name(operation: str) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in operation)
    return f"feishu-media-{safe}-{os.getpid()}-{time.monotonic_ns()}"


def build_command(operation: str, unit: str, timeout: float) -> list[str]:
    if operation not in _OPERATIONS:
        raise MediaWorkerError(f"未允许的媒体操作：{operation}")
    return [
        "systemd-run", "--user", "--wait", "--pipe", "--collect", "--quiet",
        f"--unit={unit}",
        f"--property=MemoryHigh={config.MEDIA_MEMORY_HIGH}",
        f"--property=MemoryMax={config.MEDIA_MEMORY_MAX}",
        f"--property=TasksMax={config.MEDIA_TASKS_MAX}",
        "--property=OOMPolicy=kill",
        "--property=KillMode=control-group",
        f"--property=RuntimeMaxSec={max(1, math.ceil(timeout))}s",
        # 资源之外的隔离：cgroup 管的是「别吃垮宿主」，这些管的是「打穿了
        # ffmpeg/tesseract 也别拿到完整用户权限」。不用 ProtectHome——输入与
        # 产物都在 FEISHU_HOME（$HOME 下），protect 了会把自己饿死。
        "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes",
        "--property=ProtectSystem=full",
        f"--setenv=FEISHU_HOME={config.HOME}",
        "--setenv=FEISHU_MEDIA_WORKER=1",
        _worker_python(), _worker_script(), "--worker", "--operation", operation,
    ]


def _decode_output(stdout: bytes) -> dict:
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        try:
            value = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise MediaWorkerError("Media Worker 没有返回有效 JSON")


def _write_user_output(path_text: str, content: bytes) -> str:
    target = message_resources._safe_output(path_text)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        os.chmod(target, 0o600)
        return str(target)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


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


async def run(
    action: str,
    msg_id: str,
    *,
    resource_id: str = "",
    keep_media: bool = False,
    out: str = "",
) -> dict:
    """Run one media action in a transient cgroup and reap all descendants."""
    if action not in _OPERATIONS:
        raise MediaWorkerError(f"未知媒体操作：{action}")
    if not _systemd_user_available():
        raise ResourceIsolationUnavailable(
            "当前环境没有可用的 systemd user/cgroup，已拒绝启动媒体处理进程"
        )
    timeout = config.MEDIA_TIMEOUT_SECONDS
    unit = _unit_name(action)
    proc = await asyncio.create_subprocess_exec(
        *build_command(action, unit, timeout),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = json.dumps(
        {
            "msg_id": msg_id,
            "resource_id": resource_id,
            "keep_media": bool(keep_media),
            "out": out,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode() + b"\n"
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload), timeout=timeout + 15
        )
    except asyncio.TimeoutError as exc:
        await _stop_unit(unit)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        raise MediaWorkerError("Media Worker 超时，已终止整个媒体进程树") from exc
    except asyncio.CancelledError:
        await _stop_unit(unit)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        raise
    if proc.returncode != 0:
        detail = scrub(stderr.decode("utf-8", errors="replace"))[-1200:]
        raise MediaWorkerError(
            f"Media Worker 失败（退出码 {proc.returncode}）" + (f"：{detail}" if detail else "")
        )
    return _decode_output(stdout)


async def _execute(operation: str, payload: dict) -> dict:
    msg_id = str(payload.get("msg_id") or "")
    if not msg_id:
        raise MediaWorkerError("缺少 msg_id")
    from . import auth

    await auth.prepare()
    if operation == "metadata":
        return await message_resources.list_resources(msg_id)

    message = await message_resources.fetch_message(msg_id)
    resource = message_resources.select_resource(message, str(payload.get("resource_id") or ""))
    keep_media = bool(payload.get("keep_media"))
    out = str(payload.get("out") or "")
    if operation == "download":
        if not keep_media and not out:
            raise MediaWorkerError("download 需要 --out 或 --keep-media，否则结果不会保留")
        result = await message_resources.download_resource(
            message, resource, keep_media=keep_media, out=out
        )
        return {"msg_id": msg_id, "resource": public_resource(resource), **result}

    fd, name = tempfile.mkstemp(prefix="feishu-media-input-", dir=str(config.HOME))
    os.close(fd)
    staged = Path(name)
    staged.unlink()
    try:
        downloaded = await message_resources.download_resource(
            message, resource, keep_media=keep_media, out=str(staged)
        )
        if downloaded.get("sniffed_mime") and not resource.get("mime"):
            resource["mime"] = downloaded["sniffed_mime"]
        processed = media_processing.process(staged, operation, resource)
        result = {
            "msg_id": msg_id,
            "resource": public_resource(resource),
            "download": {
                key: value
                for key, value in downloaded.items()
                if key not in {"path"}
            },
            **processed,
        }
        if out:
            target = message_resources._safe_output(out)
            if operation in {"ocr", "transcribe"}:
                output_bytes = str(processed.get("text") or "").encode("utf-8")
            else:
                output_bytes = json.dumps(processed, ensure_ascii=False, indent=2).encode("utf-8")
            # _safe_output 上面已拒绝覆盖，_write_user_output 里还会原子地
            # 复核一次——这里不需要也不允许先 unlink（那会删掉别人刚刚
            # 创建的文件，把不覆盖契约变成 TOCTOU 删除）。
            result["output_path"] = _write_user_output(out, output_bytes)
        return result
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def worker_main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--operation", choices=sorted(_OPERATIONS), required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("media worker payload must be an object")
        from . import transport

        transport.configure()
        result = asyncio.run(_execute(args.operation, payload))
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(scrub(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        hint = getattr(exc, "hint", "")
        if hint:
            print(scrub(f"hint: {hint}"), file=sys.stderr)
        return getattr(exc, "exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv[1:]))


def render_markdown(result: dict) -> str:
    """Compact human-readable rendering; JSON remains the lossless contract."""
    lines = [
        f"# 媒体处理：{result.get('action', '?')}",
        f"状态：{result.get('status', result.get('download', {}).get('status', '?'))}",
    ]
    resource = result.get("resource") or {}
    if resource:
        lines.append(
            f"资源：{resource.get('resource_id', '?')}"
            f"｜类型：{resource.get('kind', '?')}"
            f"｜名称：{resource.get('name') or '-'}"
        )
    if result.get("text"):
        # OCR/ASR 输出是别人发来的图/语音里的文字，完全不可信：
        # 显式定界，绝不以顶层文本的身份进 agent 上下文。
        rid = (result.get("resource") or {}).get("resource_id", "?")
        lines.append("\n" + untrusted(str(result["text"]), f"media-text:{rid}"))
    for key in ("path", "cache_path", "engine", "model", "language", "sha256", "bytes"):
        if result.get(key) not in (None, ""):
            lines.append(f"- {key}: {result[key]}")
    if result.get("download"):
        download = result["download"]
        if download.get("cache_path"):
            lines.append(f"- cache_path: {download['cache_path']}")
        if download.get("bytes") is not None:
            lines.append(f"- bytes: {download['bytes']}")
    if result.get("error"):
        lines.append(f"- error: {result['error']}")
    return "\n".join(lines)
