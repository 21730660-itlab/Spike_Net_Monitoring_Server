"""Configuration backups: list, take now, view, compare, download, settings."""
from __future__ import annotations

import asyncio
import difflib
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..backups.service import BackupSettings, can_backup, load_backup_settings, normalized, save_backup_settings
from ..db import get_db
from ..deps import audit, flash, local_time, render, require_admin, require_technician
from ..models import ConfigBackup, Device, User
from ..security import verify_csrf

router = APIRouter()
KIND_LABELS = {"export": "Export (.rsc)", "binary": "Full backup (.backup)", "ubnt": "airOS config"}
TEXT_KINDS = ("export", "ubnt")


def _backup(db: Session, backup_id: int) -> ConfigBackup:
    b = db.get(ConfigBackup, backup_id)
    if b is None or not b.ok:
        raise HTTPException(404, "Backup not found")
    return b


@router.get("/backups")
def backups_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_technician)):
    devices = [d for d in db.scalars(select(Device).options(selectinload(Device.site)).order_by(Device.name))
               if can_backup(d)]
    latest = {}
    for b in db.scalars(select(ConfigBackup).order_by(ConfigBackup.created_at)):
        latest.setdefault(b.device_id, {})["last"] = b
        if b.ok:
            latest[b.device_id]["last_ok"] = b
            if b.changed:
                latest[b.device_id]["last_change"] = b
    counts = dict(db.execute(select(ConfigBackup.device_id, func.count()).where(ConfigBackup.ok)
                             .group_by(ConfigBackup.device_id)).all())
    return render(request, "backups/list.html", {
        "devices": devices, "latest": latest, "counts": counts, "s": load_backup_settings(db),
        "running": request.app.state.backups.running, "labels": KIND_LABELS})


@router.post("/backups/settings", dependencies=[Depends(verify_csrf)])
async def save_settings(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = await request.form()
    s = BackupSettings(
        enabled=form.get("enabled") == "on",
        time=str(form.get("time", "02:00")).strip(),
        binary=form.get("binary") == "on",
    )
    try:
        s.keep = max(1, min(int(form.get("keep", 30)), 1000))
        s.concurrency = max(1, min(int(form.get("concurrency", 5)), 20))
    except ValueError:
        flash(request, "Numbers only for 'keep' and 'at the same time'.", "error")
        return RedirectResponse("/backups", status_code=303)
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s.time):
        flash(request, "Time must look like 02:00.", "error")
        return RedirectResponse("/backups", status_code=303)
    save_backup_settings(db, s)
    audit(db, request, user.username, "backups.settings", "", str(s))
    db.commit()
    flash(request, "Backup settings saved.")
    return RedirectResponse("/backups", status_code=303)


@router.post("/backups/run", dependencies=[Depends(verify_csrf)])
async def run_all(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    manager = request.app.state.backups
    if manager.running:
        flash(request, "Backups are already running.", "error")
    else:
        task = asyncio.create_task(manager.run_all(user.username))
        request.app.state.background_tasks.add(task)
        task.add_done_callback(request.app.state.background_tasks.discard)
        audit(db, request, user.username, "backups.run_all")
        db.commit()
        flash(request, "Backing up all devices in the background. Refresh this page in a minute.")
    return RedirectResponse("/backups", status_code=303)


@router.post("/devices/{device_id}/backup", dependencies=[Depends(verify_csrf)])
async def backup_now(device_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_technician)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    error = await request.app.state.backups.backup_device(device_id, user.username)
    audit(db, request, user.username, "device.backup", f"Device #{device.id} {device.name}", error or "ok")
    db.commit()
    flash(request, f"Backup of {device.name} failed: {error}" if error else f"Backup of {device.name} saved.",
          "error" if error else "success")
    return RedirectResponse(f"/devices/{device_id}#backups", status_code=303)


def _filename(b: ConfigBackup) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", b.device.name if b.device else "device")
    ext = b.path.rsplit(".", 1)[-1] if "." in b.path else "txt"
    return f"{name}-{b.created_at.strftime('%Y%m%d-%H%M')}.{ext}"


@router.get("/backups/{backup_id}/download")
def download(backup_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_technician)):
    b = _backup(db, backup_id)
    path = request.app.state.backups.file(b)
    if not path.exists():
        raise HTTPException(404, "The backup file is missing on the server.")
    audit(db, request, user.username, "backup.download", f"Backup #{b.id} of {b.device.name}")
    db.commit()
    return FileResponse(path, filename=_filename(b), media_type="application/octet-stream")


def _text(request: Request, b: ConfigBackup) -> str:
    path = request.app.state.backups.file(b)
    if not path.exists():
        raise HTTPException(404, "The backup file is missing on the server.")
    return path.read_bytes().decode("utf-8", errors="replace")


@router.get("/backups/{backup_id}/view")
def view(backup_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_technician)):
    b = _backup(db, backup_id)
    if b.kind not in TEXT_KINDS:
        raise HTTPException(400, "Binary backups cannot be shown; download it instead.")
    return render(request, "backups/view.html", {"b": b, "text": _text(request, b), "labels": KIND_LABELS})


@router.get("/backups/{backup_id}/diff")
def diff(backup_id: int, request: Request, against: int | None = None, db: Session = Depends(get_db),
         user: User = Depends(require_technician)):
    new = _backup(db, backup_id)
    if new.kind not in TEXT_KINDS:
        raise HTTPException(400, "Only text backups can be compared.")
    if against:
        old = _backup(db, against)
    else:  # the previous backup with a different content
        old = db.scalars(select(ConfigBackup).where(
            ConfigBackup.device_id == new.device_id, ConfigBackup.kind == new.kind, ConfigBackup.ok,
            ConfigBackup.created_at < new.created_at, ConfigBackup.checksum != new.checksum)
            .order_by(ConfigBackup.created_at.desc())).first()
    choices = db.scalars(select(ConfigBackup).where(
        ConfigBackup.device_id == new.device_id, ConfigBackup.kind == new.kind, ConfigBackup.ok,
        ConfigBackup.id != new.id).order_by(ConfigBackup.created_at.desc()).limit(100)).all()
    table, added, removed = "", 0, 0
    if old is not None:
        a = normalized(_text(request, old)).splitlines()
        b = normalized(_text(request, new)).splitlines()
        for line in difflib.unified_diff(a, b, lineterm="", n=0):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        table = difflib.HtmlDiff(wrapcolumn=70).make_table(
            a, b, fromdesc=f"{local_time(old.created_at)}", todesc=f"{local_time(new.created_at)}", context=True,
            numlines=3)
    return render(request, "backups/diff.html", {"new": new, "old": old, "table": table, "added": added,
                                                 "removed": removed, "choices": choices})


@router.post("/backups/{backup_id}/delete", dependencies=[Depends(verify_csrf)])
def delete(backup_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    b = db.get(ConfigBackup, backup_id)
    if b is None:
        raise HTTPException(404, "Backup not found")
    device_id = b.device_id
    request.app.state.backups.delete(db, b)
    audit(db, request, user.username, "backup.delete", f"Backup #{backup_id}")
    db.commit()
    flash(request, "Backup deleted.")
    return RedirectResponse(f"/devices/{device_id}#backups", status_code=303)
