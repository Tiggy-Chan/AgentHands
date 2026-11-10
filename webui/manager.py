"""Backend orchestrator and multi-platform manager for AgentHands WebUI."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict

# Add skills directories to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "bilibili" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "xianyu" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "xiaohongshu" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "feishu" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "qq" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "x" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "reddit" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "telegram" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "youtube" / "scripts"))

from _bilibili import auth as bili_auth, user_ops as bili_user
from _xianyu import auth as xianyu_auth
from _xiaohongshu import auth as xhs_auth
from _qq import auth as qq_auth
from _x import auth as x_auth
from _reddit import auth as reddit_auth
from _telegram import auth as tg_auth
from _youtube import auth as yt_auth

# QR Login Session Cache
_QR_SESSIONS: dict[str, dict[str, Any]] = {}

# Gate Environment State
_GATES_STATE: dict[str, bool] = {
    "FEISHU_ALLOW_SEND": os.environ.get("FEISHU_ALLOW_SEND") == "1",
    "FEISHU_ALLOW_WRITE": os.environ.get("FEISHU_ALLOW_WRITE") == "1",
    "BILIBILI_WRITE_ALLOWED": os.environ.get("BILIBILI_WRITE_ALLOWED") == "1",
    "XIANYU_WRITE_ALLOWED": os.environ.get("XIANYU_WRITE_ALLOWED") == "1",
    "XIAOHONGSHU_WRITE_ALLOWED": os.environ.get("XIAOHONGSHU_WRITE_ALLOWED") == "1",
    "QQ_WRITE_ALLOWED": os.environ.get("QQ_WRITE_ALLOWED") == "1",
    "X_WRITE_ALLOWED": os.environ.get("X_WRITE_ALLOWED") == "1",
    "REDDIT_WRITE_ALLOWED": os.environ.get("REDDIT_WRITE_ALLOWED") == "1",
    "TELEGRAM_WRITE_ALLOWED": os.environ.get("TELEGRAM_WRITE_ALLOWED") == "1",
    "YOUTUBE_WRITE_ALLOWED": os.environ.get("YOUTUBE_WRITE_ALLOWED") == "1",
}


async def get_all_platforms_status() -> dict[str, Any]:
    """Retrieve aggregate status for all AgentHands platforms."""
    status = {}

    # 1. Feishu
    try:
        feishu_env = Path.home() / ".hermes" / "pairing" / "feishu-approved.json"
        feishu_cookies_dir = Path.home() / ".local" / "share" / "feishu" / "cookies.enc"
        feishu_auth = feishu_env.exists() or feishu_cookies_dir.exists()
        status["feishu"] = {
            "name": "Feishu (飞书)",
            "key": "feishu",
            "authenticated": feishu_auth,
            "status_text": "Session Ready" if feishu_auth else "No Active Session",
            "details": {
                "credential_storage": "Linux Secret Service / Encrypted Pool",
                "features": ["Message Send/Receive", "Document Full Control", "Calendar Management"],
            },
        }
    except Exception as e:
        status["feishu"] = {
            "name": "Feishu (飞书)",
            "key": "feishu",
            "authenticated": False,
            "status_text": f"Error: {e}",
            "details": {},
        }

    # 2. Bilibili
    try:
        bili_creds = bili_auth.load_credentials()
        bili_has_token = bool(bili_creds.get("sessdata") and bili_creds.get("bili_jct"))
        bili_user_info = None
        if bili_has_token:
            try:
                bili_user_info = await bili_user.get_self_info()
            except Exception:
                pass

        status["bilibili"] = {
            "name": "Bilibili (哔哩哔哩)",
            "key": "bilibili",
            "authenticated": bili_has_token,
            "account": bili_user_info.get("name") if bili_user_info else None,
            "uid": bili_user_info.get("mid") if bili_user_info else None,
            "status_text": f"Logged in as {bili_user_info['name']}" if bili_user_info else ("Session Configured" if bili_has_token else "Not Logged In"),
            "details": {
                "level": bili_user_info.get("level") if bili_user_info else None,
                "coins": bili_user_info.get("coins") if bili_user_info else None,
                "vip": bili_user_info.get("vip") if bili_user_info else False,
            },
        }
    except Exception as e:
        status["bilibili"] = {
            "name": "Bilibili (哔哩哔哩)",
            "key": "bilibili",
            "authenticated": False,
            "status_text": f"Error: {e}",
            "details": {},
        }

    # 3. Xianyu
    try:
        xianyu_creds = xianyu_auth.load_credentials()
        xianyu_has_token = bool(xianyu_creds.get("cookies"))
        status["xianyu"] = {
            "name": "Xianyu (闲鱼)",
            "key": "xianyu",
            "authenticated": xianyu_has_token,
            "uid": xianyu_creds.get("user_id") or "Auto",
            "status_text": "Session Active" if xianyu_has_token else "Not Logged In",
            "details": {
                "mtop_gateway": "h5api.m.goofish.com",
                "signing": "Pure Python Local MD5",
                "features": ["Product Listings", "Order Shipping", "Marketplace Search"],
            },
        }
    except Exception as e:
        status["xianyu"] = {
            "name": "Xianyu (闲鱼)",
            "key": "xianyu",
            "authenticated": False,
            "status_text": f"Error: {e}",
            "details": {},
        }

    # 4. Xiaohongshu
    try:
        xhs_creds = xhs_auth.load_credentials()
        xhs_has_token = bool(xhs_creds.get("cookies"))
        status["xiaohongshu"] = {
            "name": "Xiaohongshu (小红书)",
            "key": "xiaohongshu",
            "authenticated": xhs_has_token,
            "status_text": "Session Ready" if xhs_has_token else "Not Logged In",
            "details": {
                "features": ["Image-Text Notes", "Video Upload", "Shadow DOM Submission", "Search Feeds"],
            },
        }
    except Exception as e:
        status["xiaohongshu"] = {
            "name": "Xiaohongshu (小红书)",
            "key": "xiaohongshu",
            "authenticated": False,
            "status_text": f"Error: {e}",
            "details": {},
        }

    # 5. QQ (OneBot 11)
    try:
        qq_c = qq_auth.load_credentials()
        status["qq"] = {
            "name": "QQ (OneBot 11)",
            "key": "qq",
            "authenticated": bool(qq_c.get("url")),
            "status_text": f"Connected to {qq_c['url']}" if qq_c.get("url") else "Not Configured",
            "details": {"url": qq_c.get("url")},
        }
    except Exception:
        status["qq"] = {"name": "QQ (OneBot 11)", "key": "qq", "authenticated": False, "status_text": "Error"}

    # 6. X (Twitter)
    try:
        xc = x_auth.load_credentials()
        x_has_auth = bool(xc.get("auth_token") and xc.get("ct0"))
        status["x"] = {
            "name": "X (Twitter)",
            "key": "x",
            "authenticated": x_has_auth,
            "status_text": "Session Ready" if x_has_auth else "Not Logged In",
            "details": {"features": ["Post Tweets", "Replies", "Likes/Retweets", "Bookmarks"]},
        }
    except Exception:
        status["x"] = {"name": "X (Twitter)", "key": "x", "authenticated": False, "status_text": "Error"}

    # 7. Reddit
    try:
        rc = reddit_auth.load_credentials()
        r_auth = bool(rc.get("session") or rc.get("token"))
        status["reddit"] = {
            "name": "Reddit",
            "key": "reddit",
            "authenticated": r_auth,
            "status_text": "Authenticated" if r_auth else "Public Read Only",
            "details": {"features": ["Subreddit Posts", "Comments", "Submissions", "Voting"]},
        }
    except Exception:
        status["reddit"] = {"name": "Reddit", "key": "reddit", "authenticated": False, "status_text": "Error"}

    # 8. Telegram
    try:
        tc = tg_auth.load_credentials()
        tg_auth_ready = bool(tc.get("session_string") or tc.get("bot_token") or tc.get("bridge_url"))
        status["telegram"] = {
            "name": "Telegram",
            "key": "telegram",
            "authenticated": tg_auth_ready,
            "status_text": "Ready" if tg_auth_ready else "Not Configured",
            "details": {"features": ["Direct Messaging", "Channel Broadcasts", "History"]},
        }
    except Exception:
        status["telegram"] = {"name": "Telegram", "key": "telegram", "authenticated": False, "status_text": "Error"}

    # 9. YouTube
    try:
        yc = yt_auth.load_credentials()
        yt_ready = bool(yc.get("cookies") or yc.get("sapisid"))
        status["youtube"] = {
            "name": "YouTube",
            "key": "youtube",
            "authenticated": yt_ready,
            "status_text": "Session Active" if yt_ready else "Not Logged In",
            "details": {"features": ["Metadata Updates", "Video Interactions", "Community Posts"]},
        }
    except Exception:
        status["youtube"] = {"name": "YouTube", "key": "youtube", "authenticated": False, "status_text": "Error"}

    return status


async def start_qr_login(platform: str) -> dict[str, Any]:
    """Initialize a real-time QR code login session."""
    session_id = str(uuid.uuid4())[:8]

    if platform == "bilibili":
        from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginChannel
        login = QrCodeLogin(platform=QrCodeLoginChannel.WEB)
        await login.generate_qrcode()
        pic = login.get_qrcode_picture()

        buf = io.BytesIO()
        pic.image.save(buf, format="PNG")
        b64_img = base64.b64encode(buf.getvalue()).decode("utf-8")
        data_uri = f"data:image/png;base64,{b64_img}"

        _QR_SESSIONS[session_id] = {
            "platform": "bilibili",
            "instance": login,
            "status": "waiting",
            "start_time": time.time(),
            "image": data_uri,
        }
        return {"session_id": session_id, "platform": platform, "qr_image": data_uri}

    raise ValueError(f"QR Login not supported for platform: {platform}")


async def poll_qr_status(session_id: str) -> dict[str, Any]:
    """Check status of an active QR code login session."""
    sess = _QR_SESSIONS.get(session_id)
    if not sess:
        return {"status": "expired", "message": "Session not found"}

    platform = sess["platform"]

    if platform == "bilibili":
        login = sess["instance"]
        from bilibili_api.login_v2 import QrCodeLoginEvents
        event = await login.check_state()

        if login.has_done():
            cred = login.get_credential()
            creds = {
                "sessdata": cred.sessdata or "",
                "bili_jct": cred.bili_jct or "",
                "buvid3": cred.buvid3 or "",
                "dedeuserid": cred.dedeuserid or "",
            }
            bili_auth.save_credentials(creds)
            sess["status"] = "confirmed"
            user_info = await bili_user.get_self_info()
            return {
                "status": "confirmed",
                "user": user_info,
                "message": f"Successfully authenticated as {user_info['name']}",
            }
        elif event == QrCodeLoginEvents.TIMEOUT:
            sess["status"] = "expired"
            return {"status": "expired", "message": "QR code expired"}
        else:
            return {"status": "waiting", "message": "Waiting for scan and confirmation"}

    return {"status": sess["status"], "message": "Polling"}


def save_manual_cookies(platform: str, cookies: str, user_id: str = "") -> dict[str, Any]:
    """Save manual cookie string for a target platform."""
    if platform == "bilibili":
        items = dict(item.strip().split("=", 1) for item in cookies.split(";") if "=" in item)
        creds = {
            "sessdata": items.get("SESSDATA", ""),
            "bili_jct": items.get("bili_jct", ""),
            "dedeuserid": items.get("DedeUserID", user_id),
            "buvid3": items.get("buvid3", ""),
        }
        bili_auth.save_credentials(creds)
        return {"status": "ok", "platform": platform}

    elif platform == "xianyu":
        xianyu_auth.save_credentials(cookies, user_id)
        return {"status": "ok", "platform": platform}

    elif platform == "xiaohongshu":
        xhs_auth.save_credentials(cookies)
        return {"status": "ok", "platform": platform}

    elif platform == "qq":
        qq_auth.save_credentials(url=cookies, token=user_id)
        return {"status": "ok", "platform": platform}

    elif platform == "x":
        ct0 = user_id
        auth_token = cookies
        if ";" in cookies:
            items = dict(item.strip().split("=", 1) for item in cookies.split(";") if "=" in item)
            auth_token = items.get("auth_token", cookies)
            ct0 = items.get("ct0", user_id)
        x_auth.save_credentials(auth_token, ct0)
        return {"status": "ok", "platform": platform}

    elif platform == "reddit":
        reddit_auth.save_credentials({"session": cookies, "token": user_id})
        return {"status": "ok", "platform": platform}

    elif platform == "telegram":
        tg_auth.save_credentials({"bot_token": cookies, "bridge_url": user_id})
        return {"status": "ok", "platform": platform}

    elif platform == "youtube":
        yt_auth.save_credentials(cookies)
        return {"status": "ok", "platform": platform}

    raise ValueError(f"Unknown platform: {platform}")


def get_write_gates() -> dict[str, bool]:
    """Return live status of all write safety gates."""
    return dict(_GATES_STATE)


def set_write_gate(gate_name: str, enabled: bool) -> dict[str, Any]:
    """Toggle a specific write safety gate."""
    if gate_name not in _GATES_STATE:
        raise KeyError(f"Unknown gate: {gate_name}")

    _GATES_STATE[gate_name] = enabled
    os.environ[gate_name] = "1" if enabled else "0"
    return {"gate": gate_name, "enabled": enabled}


def emergency_freeze_all() -> dict[str, Any]:
    """Emergency halt: revoke all write permissions across all platforms."""
    for k in _GATES_STATE:
        _GATES_STATE[k] = False
        os.environ[k] = "0"
    return {"status": "all_frozen", "gates": _GATES_STATE}
