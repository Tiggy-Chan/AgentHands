"""Bounded local inspection, OCR, ASR, and video frame processing."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import config
from .errors import MediaWorkerError


def _run(
    command: list[str],
    *,
    timeout: float,
    output_limit: int = config.MEDIA_OUTPUT_MAX_BYTES,
) -> bytes:
    error_stream = tempfile.TemporaryFile()
    try:
        try:
            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=error_stream,
                timeout=max(1, int(timeout)),
                check=False,
            )
        except FileNotFoundError as exc:
            raise MediaWorkerError(f"本机缺少媒体工具：{command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaWorkerError(f"媒体工具超时：{command[0]}") from exc
        error_stream.seek(max(0, error_stream.tell() - 4096))
        error_detail = error_stream.read().decode("utf-8", errors="replace")[-600:]
        if proc.returncode != 0:
            raise MediaWorkerError(
                f"媒体工具失败：{command[0]}" + (f"：{error_detail}" if error_detail else "")
            )
        if len(proc.stdout) > output_limit:
            raise MediaWorkerError(f"媒体工具输出超过 {output_limit} 字节上限")
        return proc.stdout
    finally:
        error_stream.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_video(resource: dict[str, Any], mime: str) -> bool:
    return mime.startswith("video/") or resource.get("kind") in {"media", "video_chat"}


def _is_audio(resource: dict[str, Any], mime: str) -> bool:
    return mime.startswith("audio/") or resource.get("kind") == "audio"


def _is_image(resource: dict[str, Any], mime: str) -> bool:
    return mime.startswith("image/") or resource.get("kind") == "image"


def _probe(path: Path, resource: dict[str, Any]) -> dict[str, Any]:
    """Use ffprobe only for media files; all output is size bounded."""
    mime = str(resource.get("mime") or "").lower()
    result: dict[str, Any] = {
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "mime": mime or "application/octet-stream",
    }
    if not (_is_video(resource, mime) or _is_audio(resource, mime) or _is_image(resource, mime)):
        return result
    raw = _run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        timeout=60,
        output_limit=1024 * 1024,
    )
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise MediaWorkerError("ffprobe 返回了无效 JSON") from exc
    fmt = data.get("format") or {}
    result["format"] = {
        key: fmt.get(key)
        for key in ("format_name", "format_long_name", "duration", "bit_rate")
        if fmt.get(key) not in (None, "")
    }
    streams = []
    for stream in (data.get("streams") or [])[:8]:
        item = {
            key: stream.get(key)
            for key in (
                "index", "codec_name", "codec_type", "width", "height",
                "sample_rate", "channels", "duration", "avg_frame_rate",
            )
            if stream.get(key) not in (None, "")
        }
        streams.append(item)
    result["streams"] = streams
    _check_probe_limits(result)
    return result


def _check_probe_limits(info: dict[str, Any]) -> None:
    fmt = info.get("format") or {}
    try:
        duration = float(fmt.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration > config.MEDIA_MAX_DURATION_SECONDS:
        raise MediaWorkerError("音视频超过 2 小时处理上限")
    for stream in info.get("streams", []):
        try:
            width = int(stream.get("width", 0) or 0)
            height = int(stream.get("height", 0) or 0)
        except (TypeError, ValueError):
            width = height = 0
        if width and height and width * height > config.MEDIA_MAX_PIXELS:
            raise MediaWorkerError("图片/视频帧超过像素上限")


def inspect(path: Path, resource: dict[str, Any]) -> dict[str, Any]:
    return {"action": "inspect", "status": "processed", "info": _probe(path, resource)}


def _pdf_text(path: Path) -> str:
    raw = _run(["pdftotext", "-layout", str(path), "-"], timeout=120)
    return raw.decode("utf-8", errors="replace")[: config.MEDIA_OUTPUT_MAX_BYTES]


def _ocr_image(path: Path) -> str:
    raw = _run(
        ["tesseract", str(path), "stdout", "-l", "chi_sim+eng", "--oem", "1"],
        timeout=180,
    )
    return raw.decode("utf-8", errors="replace")[: config.MEDIA_OUTPUT_MAX_BYTES]


def _video_frames(path: Path, resource: dict[str, Any]) -> list[Path]:
    info = _probe(path, resource)
    try:
        duration = float((info.get("format") or {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        duration = config.MEDIA_MAX_DURATION_SECONDS
    fps = max(1 / duration, config.MEDIA_MAX_VIDEO_FRAMES / duration)
    frames_dir = Path(tempfile.mkdtemp(prefix="feishu-frames-", dir=str(config.HOME)))
    try:
        _run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
                "-vf", f"fps={fps:.8f},scale=1920:-2",
                "-frames:v", str(config.MEDIA_MAX_VIDEO_FRAMES),
                str(frames_dir / "frame-%03d.jpg"),
            ],
            timeout=240,
            output_limit=256 * 1024,
        )
        return sorted(frames_dir.glob("frame-*.jpg"))[: config.MEDIA_MAX_VIDEO_FRAMES]
    except Exception:
        shutil.rmtree(frames_dir, ignore_errors=True)
        raise


def ocr(path: Path, resource: dict[str, Any]) -> dict[str, Any]:
    mime = str(resource.get("mime") or "").lower()
    if mime == "application/pdf" or path.suffix.lower() == ".pdf":
        text = _pdf_text(path)
        return {"action": "ocr", "status": "processed", "text": text, "engine": "pdftotext"}
    if _is_video(resource, mime):
        frames = _video_frames(path, resource)
        try:
            parts = []
            for frame in frames:
                text = _ocr_image(frame).strip()
                if text:
                    parts.append(text)
                if sum(len(x.encode("utf-8")) for x in parts) >= config.MEDIA_OUTPUT_MAX_BYTES:
                    break
            return {
                "action": "ocr", "status": "processed", "engine": "tesseract",
                "frames": len(frames), "text": "\n\n".join(parts)[: config.MEDIA_OUTPUT_MAX_BYTES],
            }
        finally:
            if frames:
                shutil.rmtree(frames[0].parent, ignore_errors=True)
    if not _is_image(resource, mime):
        raise MediaWorkerError("该资源类型不支持 OCR")
    return {"action": "ocr", "status": "processed", "engine": "tesseract", "text": _ocr_image(path)}


def _extract_audio(path: Path, resource: dict[str, Any]) -> Path:
    audio_fd, audio_name = tempfile.mkstemp(prefix="feishu-audio-", suffix=".wav", dir=str(config.HOME))
    os.close(audio_fd)
    audio = Path(audio_name)
    try:
        _run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
                "-t", str(config.MEDIA_MAX_DURATION_SECONDS), "-vn",
                "-ac", "1", "-ar", "16000", str(audio),
            ],
            timeout=300,
            output_limit=256 * 1024,
        )
        return audio
    except Exception:
        try:
            audio.unlink()
        except FileNotFoundError:
            pass
        raise


def transcribe(path: Path, resource: dict[str, Any]) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise MediaWorkerError(
            "未安装 faster-whisper，无法执行本机语音转写",
            hint="安装 requirements-media.txt 后重试",
        ) from exc
    mime = str(resource.get("mime") or "").lower()
    source = path
    temporary_audio = None
    if _is_video(resource, mime):
        temporary_audio = _extract_audio(path, resource)
        source = temporary_audio
    try:
        config.MEDIA_MODELS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        model = WhisperModel(
            config.MEDIA_ASR_MODEL,
            device=config.MEDIA_ASR_DEVICE,
            compute_type=config.MEDIA_ASR_COMPUTE_TYPE,
            download_root=str(config.MEDIA_MODELS_DIR),
        )
        segments, info = model.transcribe(str(source), beam_size=5, vad_filter=True)
        parts: list[str] = []
        used = 0
        for segment in segments:
            line = f"[{segment.start:.1f}-{segment.end:.1f}] {str(segment.text).strip()}"
            encoded = line.encode("utf-8")
            if used + len(encoded) > config.MEDIA_OUTPUT_MAX_BYTES:
                break
            parts.append(line)
            used += len(encoded) + 1
        return {
            "action": "transcribe",
            "status": "processed",
            "engine": "faster-whisper",
            "model": config.MEDIA_ASR_MODEL,
            "language": getattr(info, "language", ""),
            "text": "\n".join(parts),
        }
    finally:
        if temporary_audio:
            try:
                temporary_audio.unlink()
            except FileNotFoundError:
                pass


def process(path: Path, action: str, resource: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        raise MediaWorkerError("媒体输入文件不存在")
    if path.stat().st_size > config.MEDIA_JOB_MAX_BYTES:
        raise MediaWorkerError("媒体任务超过总输入大小上限")
    if action == "inspect":
        return inspect(path, resource)
    if action == "ocr":
        return ocr(path, resource)
    if action == "transcribe":
        if not (_is_audio(resource, str(resource.get("mime") or "").lower()) or _is_video(resource, str(resource.get("mime") or "").lower())):
            raise MediaWorkerError("该资源类型不支持语音转写")
        return transcribe(path, resource)
    raise MediaWorkerError(f"未知媒体处理动作：{action}")
