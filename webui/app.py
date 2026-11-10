"""AgentHands Management WebUI FastAPI Application."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import manager

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="AgentHands Console",
    description="Unified Human Governance & Platform Console for AgentHands",
    version="0.2.0",
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    """Serve the single page application."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(str(index_file))


@app.get("/api/status")
async def api_status():
    """Retrieve aggregate platforms status."""
    return await manager.get_all_platforms_status()


class QRStartRequest(BaseModel):
    platform: str


@app.post("/api/login/qr/start")
async def api_qr_start(req: QRStartRequest):
    """Start QR code login session."""
    try:
        return await manager.start_qr_login(req.platform)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/login/qr/status/{session_id}")
async def api_qr_poll(session_id: str):
    """Poll status of QR code scan."""
    return await manager.poll_qr_status(session_id)


class ManualLoginRequest(BaseModel):
    platform: str
    cookies: str
    user_id: str = ""


@app.post("/api/login/manual")
async def api_manual_login(req: ManualLoginRequest):
    """Save raw cookies directly."""
    try:
        return manager.save_manual_cookies(req.platform, req.cookies, req.user_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/gates")
async def api_get_gates():
    """Retrieve status of write safety gates."""
    return manager.get_write_gates()


class GateToggleRequest(BaseModel):
    gate: str
    enabled: bool


@app.post("/api/gates/toggle")
async def api_toggle_gate(req: GateToggleRequest):
    """Toggle a specific write safety gate."""
    try:
        return manager.set_write_gate(req.gate, req.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/gates/freeze")
async def api_freeze_all():
    """Emergency freeze all write operations."""
    return manager.emergency_freeze_all()
