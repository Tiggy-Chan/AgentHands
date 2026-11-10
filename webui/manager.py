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

from _bilibili import auth as bili_auth, user_ops as bili_user, config as bili_config
from _xianyu import auth as xianyu_auth, user_ops as xianyu_user, config as xianyu_config
from _xiaohongshu import auth as xhs_auth, creator_ops as xhs_creator, config as xhs_config

# QR Login Session Cache
_QR_SESSIONS: dict[str, dict[str, Any]] = {}

# Gate Environment State
_GATES_STATE: dict[str, bool] = {
    "FEISHU_ALLOW_SEND": os.environ.get("FEISHU_ALLOW_SEND") == "1",
    "FEISHU_ALLOW_WRITE": os.environ.get("FEISHU_ALLOW_WRITE") == "1",
    "BILIBILI_WRITE_ALLOWED": os.environ.get("BILIBILI_WRITE_ALLOWED") == "1",
    "XIANYU_WRITE_ALLOWED": os.environ.get("XIANYU_WRITE_ALLOWED") == "1",
    "XIAOHONGSHU_WRITE_ALLOWED": os.environ.get("XIAOHONGSHU_WRITE_ALLOWED") == "1",
}


async def get_all_platforms_status() -> dict[str, Any]:
    """Retrieve aggregate status for all four AgentHands platforms."""
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

    return status


async def start_qr_login(platform: str) -> dict[str, Any]:
    """Initialize a real-time QR code login session."""
    session_id = str(uuid.uuid4())[:8]

    if platform == "bilibili":
        from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginChannel
        login = QrCodeLogin(platform=QrCodeLoginChannel.WEB)
        await login.generate_qrcode()
        pic = login.get_qrcode_picture()

        # Convert picture to base64 data URI
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

    elif platform == "feishu":
        # Placeholder for Feishu QR flow
        _QR_SESSIONS[session_id] = {
            "platform": "feishu",
            "status": "waiting",
            "start_time": time.time(),
            "image": "",
        }
        return {"session_id": session_id, "platform": platform, "qr_image": ""}

    elif platform == "xianyu":
        _QR_SESSIONS[session_id] = {
            "platform": "xianyu",
            "status": "waiting",
            "start_time": time.time(),
            "image": "",
        }
        return {"session_id": session_id, "platform": platform, "qr_image": ""}

    elif platform == "xiaohongshu":
        _QR_SESSIONS[session_id] = {
            "platform": "xiaohongshu",
            "status": "waiting",
            "start_time": time.time(),
            "image": "",
        }
        return {"session_id": session_id, "platform": platform, "qr_image": ""}

    raise ValueError(f"Unsupported platform: {platform}")


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
        # Parse sessdata and bili_jct
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
