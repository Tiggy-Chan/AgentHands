"""Video metadata and interaction operations for Bilibili."""

from __future__ import annotations

from typing import Any

from . import auth, security


async def get_video_info(bvid: str) -> dict[str, Any]:
    """Retrieve video metadata, statistics, and owner info."""
    from bilibili_api import video

    cred = auth.get_credential()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    stat = info.get("stat", {})
    owner = info.get("owner", {})
    return {
        "bvid": info.get("bvid"),
        "aid": info.get("aid"),
        "title": info.get("title"),
        "desc": info.get("desc"),
        "pubdate": info.get("pubdate"),
        "owner": {
            "mid": owner.get("mid"),
            "name": owner.get("name"),
        },
        "stats": {
            "view": stat.get("view"),
            "danmaku": stat.get("danmaku"),
            "reply": stat.get("reply"),
            "favorite": stat.get("favorite"),
            "coin": stat.get("coin"),
            "share": stat.get("share"),
            "like": stat.get("like"),
        },
    }


async def interact_video(
    bvid: str,
    like: bool = False,
    coin: int = 0,
    fav: bool = False,
    triple: bool = False,
    execute: bool = False,
    direct: bool = False,
    expected_fp: str | None = None,
) -> dict[str, Any]:
    """Perform interactions on video (like, coin, favorite, or triple combo)."""
    from bilibili_api import video

    payload = {
        "bvid": bvid,
        "like": like,
        "coin": coin,
        "favorite": fav,
        "triple": triple,
    }
    security.require_write_allowed("interact_video", payload, execute, direct, expected_fp)

    cred = auth.get_credential()
    v = video.Video(bvid=bvid, credential=cred)
    results: dict[str, Any] = {"bvid": bvid}

    if triple:
        res = await v.triple()
        results["triple"] = res
        return {"status": "ok", "action": "triple", "results": results}

    if like:
        res = await v.like(True)
        results["like"] = res

    if coin > 0:
        res = await v.pay_coin(coin)
        results["coin"] = res

    return {"status": "ok", "action": "interact", "results": results}
