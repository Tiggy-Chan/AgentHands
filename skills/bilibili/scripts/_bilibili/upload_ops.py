"""Video submission and upload operations for Bilibili."""

from __future__ import annotations

import os
from typing import Any

from . import auth, security


async def upload_video(
    file_path: str,
    title: str,
    desc: str,
    tid: int,
    tags: list[str] | None = None,
    cover_path: str | None = None,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Upload video file to Bilibili with metadata and safety gates."""
    from bilibili_api import Picture
    from bilibili_api.video_uploader import VideoMeta, VideoUploader, VideoUploaderPage

    tags = tags or ["原创"]
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Video file not found: {file_path}")

    payload = {
        "file_path": file_path,
        "title": title,
        "desc": desc,
        "tid": tid,
        "tags": tags,
        "cover_path": cover_path or "",
    }
    security.require_write_allowed("upload_video", payload, execute, direct, expected_fp)

    cred = auth.get_credential()
    meta = VideoMeta(
        title=title,
        desc=desc,
        tid=tid,
        tags=tags,
    )
    if cover_path and os.path.exists(cover_path):
        meta.cover = Picture(cover_path, cred)

    pages = [VideoUploaderPage(path=file_path, title=title)]
    uploader = VideoUploader(pages=pages, meta=meta, credential=cred)
    res = await uploader.start()
    return {"status": "ok", "action": "upload_video", "title": title, "result": res}
