"""Admin page: encrypted export / import of everything, and a CSV export of devices."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, flash, render, require_admin, require_technician
from ..models import Device, User
from ..security import verify_csrf
from ..transfer import TransferError, build_payload, decrypt_export, encrypt_export, import_payload
from .devices import CSV_COLUMNS

router = APIRouter()


@router.get("/admin/transfer")
def transfer_page(request: Request, user: User = Depends(require_admin)):
    return render(request, "admin/transfer.html", {})


@router.post("/admin/transfer/export", dependencies=[Depends(verify_csrf)])
async def export(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = await request.form()
    password, confirm = str(form.get("password", "")), str(form.get("confirm", ""))
    if password != confirm:
        flash(request, "The two passwords do not match.", "error")
        return RedirectResponse("/admin/transfer", status_code=303)
    try:
        data = encrypt_export(build_payload(db, request.app.state.secret_box, form.get("users") == "on"), password)
    except TransferError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/admin/transfer", status_code=303)
    audit(db, request, user.username, "transfer.export", "", f"users={form.get('users') == 'on'}")
    db.commit()
    name = f"spike-net-export-{datetime.now().strftime('%Y%m%d-%H%M')}.spike"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@router.post("/admin/transfer/import", dependencies=[Depends(verify_csrf)])
async def import_file(request: Request, file: UploadFile, db: Session = Depends(get_db),
                      user: User = Depends(require_admin)):
    form = await request.form()
    raw = await file.read()
    try:
        payload = decrypt_export(raw, str(form.get("password", "")))
        counts = import_payload(db, request.app.state.secret_box, payload, form.get("users") == "on",
                                form.get("settings") == "on")
    except TransferError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/admin/transfer", status_code=303)
    audit(db, request, user.username, "transfer.import", file.filename or "",
          ", ".join(f"{k}={v}" for k, v in counts.items()))
    db.commit()
    flash(request, "Imported: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items() if v) + ".")
    return RedirectResponse("/admin/transfer", status_code=303)


@router.get("/devices/export.csv")
def export_csv(request: Request, db: Session = Depends(get_db), user: User = Depends(require_technician)):
    """All devices in the CSV import format, without passwords."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for d in db.scalars(select(Device).order_by(Device.name)):
        row = {
            "name": d.name, "ip_address": d.ip_address, "brand": d.brand, "device_type": d.device_type,
            "site": d.site.name if d.site else "", "parent": d.parent.name if d.parent else "",
            "uplink_type": d.uplink_type, "uplink_port": d.uplink_port, "local_port": d.local_port,
            "mac_address": d.mac_address, "model": d.model, "serial_number": d.serial_number,
            "os_version": d.os_version, "credential_profile": d.credential_profile.name if d.credential_profile else "",
            "username": d.username, "password": "", "notes": d.notes,
            **{p: getattr(d, p) for p in ("winbox_port", "ssh_port", "api_port", "api_ssl_port", "http_port", "https_port")},
        }
        w.writerow([row.get(c, "") for c in CSV_COLUMNS])
    audit(db, request, user.username, "devices.export_csv")
    db.commit()
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=devices-{datetime.now():%Y%m%d}.csv"})
