"""Open a device in Winbox or an SSH client on the technician's PC, already logged in.

The page hands the browser a ``spikenet://`` link with a one-time token (valid 60 s). The helper
installed on the technician's PC (spike_monitor/helper) exchanges the token for the login. The
password never appears in a link, a page or the browser history.
"""
from __future__ import annotations

import io
import json
import secrets
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, render, require_technician
from ..forms import MAC_RE
from ..models import Brand, Device, User
from ..security import verify_csrf

router = APIRouter()
TOKEN_SECONDS = 60
HELPER_DIR = Path(__file__).resolve().parent.parent / "helper"


def _tokens(request: Request) -> dict:
    if not hasattr(request.app.state, "launch_tokens"):
        request.app.state.launch_tokens = {}
    store = request.app.state.launch_tokens
    now = time.monotonic()
    for t in [t for t, v in store.items() if v["expires"] < now]:
        store.pop(t, None)
    return store


def server_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.post("/devices/{device_id}/launch/{kind}", dependencies=[Depends(verify_csrf)])
def launch(device_id: int, kind: str, request: Request, db: Session = Depends(get_db),
           user: User = Depends(require_technician)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    if kind not in ("winbox", "ssh") or (kind == "winbox" and device.brand != Brand.MIKROTIK):
        raise HTTPException(400, "This device cannot be opened that way.")
    token = secrets.token_urlsafe(24)
    _tokens(request)[token] = {"device_id": device.id, "kind": kind, "user": user.username,
                               "expires": time.monotonic() + TOKEN_SECONDS}
    audit(db, request, user.username, f"device.open_{kind}", f"Device #{device.id} {device.name}")
    db.commit()
    link = f"spikenet://open?server={quote(server_url(request), safe='')}&token={token}"
    return render(request, "devices/launch.html", {"device": device, "kind": kind, "link": link})


@router.post("/launch/winbox-mac", dependencies=[Depends(verify_csrf)])
async def launch_by_mac(request: Request, db: Session = Depends(get_db), user: User = Depends(require_technician)):
    """Open Winbox by MAC address (Layer 2, like Winbox's Neighbors tab): works without a reachable IP
    when the technician's PC is on the same network. Uses the device's login if it is in the app."""
    form = await request.form()
    mac = str(form.get("mac", "")).strip().upper().replace("-", ":")
    if not MAC_RE.match(mac):
        raise HTTPException(400, "Invalid MAC address.")
    device_id = str(form.get("device_id", ""))
    device = db.get(Device, int(device_id)) if device_id.isdigit() else None
    name = device.name if device else (str(form.get("name", "")).strip()[:128] or mac)
    token = secrets.token_urlsafe(24)
    _tokens(request)[token] = {"device_id": device.id if device else None, "mac": mac, "name": name,
                               "kind": "winbox", "user": user.username, "expires": time.monotonic() + TOKEN_SECONDS}
    audit(db, request, user.username, "device.open_winbox_mac", f"{name} {mac}")
    db.commit()
    link = f"spikenet://open?server={quote(server_url(request), safe='')}&token={token}"
    return render(request, "devices/launch.html", {"device": device or {"name": f"{name} ({mac})"},
                                                   "kind": "winbox", "link": link})


@router.get("/api/launch/{token}")
def redeem(token: str, request: Request, db: Session = Depends(get_db)):
    """Called by the helper (no browser session). Each token works once, for 60 seconds."""
    entry = _tokens(request).pop(token, None)
    if entry is None:
        return JSONResponse({"detail": "This link has expired. Click the button again."}, status_code=404)
    device = db.get(Device, entry["device_id"]) if entry["device_id"] else None
    if entry.get("mac"):  # Winbox by MAC address: no port
        username, password = device.login(request.app.state.secret_box) if device else ("admin", "")
        return {"kind": "winbox", "name": entry["name"], "host": entry["mac"], "port": None,
                "username": username, "password": password}
    if device is None:
        return JSONResponse({"detail": "Device not found"}, status_code=404)
    username, password = device.login(request.app.state.secret_box)
    port = device.winbox_port if entry["kind"] == "winbox" else device.ssh_port
    return {"kind": entry["kind"], "name": device.name, "host": device.ip_address, "port": port,
            "username": username, "password": password}


@router.get("/tools/helper.zip")
def helper_zip(request: Request, user: User = Depends(require_technician)):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("install.bat", "spikenet-helper.ps1"):
            z.write(HELPER_DIR / name, f"SpikeNetHelper/{name}")
        z.writestr("SpikeNetHelper/config.json", json.dumps({"server": server_url(request)}, indent=2))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=SpikeNetHelper.zip"})
