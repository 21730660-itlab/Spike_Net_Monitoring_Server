"""Device detail page, history API and CSV import."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, render, require_technician, require_viewer
from ..forms import Field, choices, normalize_mac, parse_form
from ..models import (
    DEFAULT_PORTS,
    Brand,
    CredentialProfile,
    Customer,
    Device,
    DeviceType,
    PingResult,
    Site,
    StatusEvent,
    User,
    utcnow,
)
from ..monitoring.poller import can_poll
from ..security import verify_csrf

router = APIRouter(prefix="/devices")

CSV_COLUMNS = [
    "name", "ip_address", "brand", "device_type", "site", "parent", "mac_address", "model",
    "serial_number", "os_version", "credential_profile", "username", "password",
    *DEFAULT_PORTS, "notes",
]

_CSV_FIELDS = [
    Field("name", "name", required=True),
    Field("ip_address", "ip_address", kind="ip", required=True),
    Field("brand", "brand", kind="select", options=choices(Brand.ALL)),
    Field("device_type", "device_type", kind="select", options=choices(DeviceType.ALL)),
    Field("mac_address", "mac_address", kind="mac"),
    *[Field(p, p, kind="port") for p in DEFAULT_PORTS],
]


@router.get("/import")
def import_page(request: Request, user: User = Depends(require_technician)):
    return render(request, "devices/import.html", {"columns": CSV_COLUMNS, "report": None})


@router.get("/import/template.csv")
def import_template(user: User = Depends(require_technician)):
    sample = [
        CSV_COLUMNS,
        ["Core Router", "10.0.0.1", "mikrotik", "router", "Main POP", "", "", "RB4011", "", "7.15.3",
         "", "admin", "secret", "8291", "22", "8728", "8729", "80", "443", ""],
        ["Tower 1 AP", "10.0.1.2", "ubiquiti", "access_point", "Tower 1", "Core Router", "", "Rocket 5AC",
         "", "8.7.11", "", "ubnt", "secret", "8291", "22", "8728", "8729", "80", "443", ""],
    ]
    buf = io.StringIO()
    csv.writer(buf).writerows(sample)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=devices_template.csv"})


@router.post("/import", dependencies=[Depends(verify_csrf)])
async def import_devices(
    request: Request, file: UploadFile, db: Session = Depends(get_db), user: User = Depends(require_technician)
):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    box = request.app.state.secret_box

    created, updated, errors = 0, 0, []
    pending_parents: list[tuple[Device, str]] = []
    sites = {s.name.lower(): s for s in db.scalars(select(Site))}
    profiles = {p.name.lower(): p for p in db.scalars(select(CredentialProfile))}

    for line_no, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not any(row.values()):
            continue
        result = parse_form(_CSV_FIELDS, _RowForm(row))
        if not result.ok:
            errors.append(f"Line {line_no}: " + "; ".join(f"{k}: {v}" for k, v in result.errors.items()))
            continue
        v = result.values

        device = db.scalars(select(Device).where(Device.ip_address == v["ip_address"])).first()
        is_new = device is None
        if is_new:
            device = Device(ip_address=v["ip_address"])
            db.add(device)
        device.name = v["name"]
        device.brand = v["brand"] or device.brand or Brand.MIKROTIK
        device.device_type = v["device_type"] or device.device_type or DeviceType.ROUTER
        if v["mac_address"]:
            device.mac_address = normalize_mac(v["mac_address"])
        for key in ("model", "serial_number", "os_version", "notes", "username"):
            if row.get(key):
                setattr(device, key, row[key])
        for p in DEFAULT_PORTS:
            if v[p] is not None:
                setattr(device, p, v[p])
            elif is_new:
                setattr(device, p, DEFAULT_PORTS[p])
        if row.get("password"):
            device.password_enc = box.encrypt(row["password"])
        if row.get("site"):
            site = sites.get(row["site"].lower())
            if site is None:
                site = Site(name=row["site"])
                db.add(site)
                sites[row["site"].lower()] = site
            device.site = site
        if row.get("credential_profile"):
            profile = profiles.get(row["credential_profile"].lower())
            if profile is None:
                errors.append(f"Line {line_no}: credential profile '{row['credential_profile']}' not found (ignored).")
            else:
                device.credential_profile = profile
        if row.get("parent"):
            pending_parents.append((device, row["parent"]))
        created += is_new
        updated += not is_new
        db.flush()

    # Second pass so parents can appear anywhere in the file.
    for device, ref in pending_parents:
        parent = db.scalars(
            select(Device).where((func.lower(Device.name) == ref.lower()) | (Device.ip_address == ref))
        ).first()
        if parent is None or parent.id == device.id:
            errors.append(f"{device.name}: parent '{ref}' not found (ignored).")
        elif device.id in {a.id for a in parent.ancestors()}:
            errors.append(f"{device.name}: parent '{ref}' would create a loop (ignored).")
        else:
            device.parent = parent

    audit(db, request, user.username, "device.import", file.filename or "", f"created={created} updated={updated}")
    db.commit()
    report = {"created": created, "updated": updated, "errors": errors}
    return render(request, "devices/import.html", {"columns": CSV_COLUMNS, "report": report})


class _RowForm(dict):
    def getlist(self, key):  # pragma: no cover - not used by CSV fields
        return [self[key]] if self.get(key) else []


@router.get("/{device_id}")
def device_detail(device_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_viewer)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    events = db.scalars(select(StatusEvent).where(StatusEvent.device_id == device_id)
                        .order_by(StatusEvent.ts.desc()).limit(50)).all()
    since = utcnow() - timedelta(hours=24)
    stats = db.execute(
        select(func.count(), func.sum(case((PingResult.is_alive, 1), else_=0)), func.avg(PingResult.avg_ms))
        .where(PingResult.device_id == device_id, PingResult.ts >= since)
    ).one()
    total, alive, avg = stats
    uptime = (100.0 * (alive or 0) / total) if total else None
    return render(request, "devices/detail.html", {
        "device": device,
        "events": events,
        "uptime_24h": uptime,
        "avg_latency_24h": avg,
        "has_password": bool(device.password_enc or (device.credential_profile and device.credential_profile.password_enc)),
        "login_user": device.username or (device.credential_profile.username if device.credential_profile else ""),
        "customers_by_mac": {c.mac_address: c for c in db.scalars(
            select(Customer).where(Customer.mac_address.in_([p.mac_address for p in device.wireless_peers])))},
        "can_poll": can_poll(device),
    })

