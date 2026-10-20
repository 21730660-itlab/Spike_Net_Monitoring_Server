"""Device actions that talk to the device: poll now, import from device, history graphs."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..collectors.common import CollectorError
from ..db import get_db
from ..deps import audit, flash, render, require_admin, require_technician, require_viewer
from ..forms import MAC_RE
from ..models import Device, HourlyRollup, MetricSample, PingResult, TrafficSample, User, Vlan, utcnow
from ..monitoring.poller import read_inventory
from .discovery import import_neighbors, known_addresses
from ..security import verify_csrf

router = APIRouter(prefix="/devices")

# Ranges longer than this read hourly averages instead of raw samples.
RAW_MAX_HOURS = 48
IMPORT_FIELDS = [
    ("name", "identity", "Name (from identity)"),
    ("model", "model", "Model"),
    ("serial_number", "serial_number", "Serial number"),
    ("os_version", "os_version", "Firmware / RouterOS version"),
    ("mac_address", "mac_address", "MAC address"),
]


def _device(db: Session, device_id: int) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    return device


@router.post("/{device_id}/poll", dependencies=[Depends(verify_csrf)])
async def poll_now(device_id: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_technician)):
    device = _device(db, device_id)
    error = await request.app.state.poller.poll_device(device.id)
    if error:
        flash(request, f"Could not read {device.name}: {error}", "error")
    else:
        flash(request, f"{device.name} read successfully.")
    return RedirectResponse(f"/devices/{device_id}", status_code=303)


@router.post("/{device_id}/forget-ssh-key", dependencies=[Depends(verify_csrf)])
def forget_ssh_key(device_id: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    device = _device(db, device_id)
    device.ssh_host_key = ""
    audit(db, request, user.username, "device.forget_ssh_key", f"Device #{device.id} {device.name}")
    db.commit()
    flash(request, "SSH host key forgotten. The next connection will remember the new key.")
    return RedirectResponse(f"/devices/{device_id}", status_code=303)


# ------------------------------------------------------------------ import from device
@router.get("/{device_id}/import")
async def import_preview(device_id: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(require_technician)):
    device = _device(db, device_id)
    try:
        inv, fingerprint = await read_inventory(device, request.app.state.secret_box)
    except CollectorError as exc:
        flash(request, f"Could not read {device.name}: {exc}", "error")
        return RedirectResponse(f"/devices/{device_id}", status_code=303)
    if fingerprint and not device.ssh_host_key:
        device.ssh_host_key = fingerprint
        db.commit()

    changes = []
    for field, inv_key, label in IMPORT_FIELDS:
        new = str(getattr(inv, inv_key) or "")
        old = str(getattr(device, field) or "")
        if new and new != old:
            changes.append({"field": field, "label": label, "old": old, "new": new})

    site_vlans = db.scalars(select(Vlan).where((Vlan.site_id == device.site_id) | Vlan.site_id.is_(None))).all()
    known_ids = {v.vlan_id for v in site_vlans}
    attached = {v.vlan_id for v in device.vlans}
    vlans = [dict(v, exists=v["vlan_id"] in known_ids, attached=v["vlan_id"] in attached)
             for v in inv.vlans if v.get("vlan_id")]

    known_ips, known_macs = known_addresses(db)
    neighbors = [n for n in inv.neighbors
                 if n.ip_address not in known_ips and (not n.mac_address or n.mac_address not in known_macs)]
    nb_rows = [{"name": n.identity, "ip": n.ip_address, "mac": n.mac_address, "platform": n.platform,
                "board": n.board, "version": n.version, "interface": n.interface, "parent_id": device.id,
                "parent_name": device.name} for n in neighbors]
    return render(request, "devices/import_preview.html", {
        "device": device, "inv": inv, "changes": changes, "vlans": vlans, "nb_rows": nb_rows})


@router.post("/{device_id}/import", dependencies=[Depends(verify_csrf)])
async def import_apply(device_id: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_technician)):
    device = _device(db, device_id)
    form = await request.form()
    applied = []
    for field, _, label in IMPORT_FIELDS:
        if form.get(f"apply_{field}") and form.get(f"value_{field}"):
            value = str(form.get(f"value_{field}"))[:128]
            if field == "mac_address":
                if not MAC_RE.match(value):
                    continue
                value = value.replace("-", ":").upper()
            setattr(device, field, value)
            applied.append(label)

    added_vlans = 0
    for entry in form.getlist("vlan"):
        vid_text, _, name = str(entry).partition(":")
        if not vid_text.isdigit() or not 1 <= int(vid_text) <= 4094:
            continue
        vid = int(vid_text)
        vlan = db.scalars(select(Vlan).where(Vlan.vlan_id == vid, (Vlan.site_id == device.site_id)
                                             | Vlan.site_id.is_(None))).first()
        if vlan is None:
            vlan = Vlan(vlan_id=vid, name=name[:64] or f"VLAN {vid}", site_id=device.site_id,
                        notes=f"Imported from {device.name}")
            db.add(vlan)
        if vlan not in device.vlans:
            device.vlans.append(vlan)
            added_vlans += 1

    new_devices, new_customers, problems = import_neighbors(db, form)
    audit(db, request, user.username, "device.import_settings", f"Device #{device.id} {device.name}",
          f"fields={','.join(applied)} vlans={added_vlans} devices={new_devices} customers={new_customers}")
    db.commit()
    msg = (f"Imported {len(applied)} field(s), {added_vlans} VLAN(s), {new_devices} neighbor device(s) "
           f"and {new_customers} customer(s).")
    if problems:
        msg += f" Skipped {len(problems)}: " + " ".join(problems[:10])
    flash(request, msg, "error" if problems else "success")
    return RedirectResponse(f"/devices/{device_id}", status_code=303)


# ------------------------------------------------------------------ graphs
def _range(hours: int) -> tuple[int, bool]:
    hours = max(1, min(hours, 24 * 365))
    return hours, hours > RAW_MAX_HOURS


def _rollups(db: Session, device_id: int, series: str, since):
    return db.scalars(select(HourlyRollup).where(HourlyRollup.device_id == device_id, HourlyRollup.series == series,
                                                 HourlyRollup.hour >= since).order_by(HourlyRollup.hour)).all()


@router.get("/{device_id}/history")
def ping_history(device_id: int, hours: int = 24, db: Session = Depends(get_db),
                 user: User = Depends(require_viewer)):
    hours, hourly = _range(hours)
    since = utcnow() - timedelta(hours=hours)
    if hourly:
        rows = _rollups(db, device_id, "ping", since)
        return {"ts": [r.hour.isoformat() for r in rows], "latency": [r.v1 for r in rows],
                "loss": [r.v2 for r in rows], "hourly": True}
    rows = db.execute(select(PingResult.ts, PingResult.avg_ms, PingResult.loss_pct)
                      .where(PingResult.device_id == device_id, PingResult.ts >= since)
                      .order_by(PingResult.ts)).all()
    return {"ts": [r.ts.isoformat() for r in rows], "latency": [r.avg_ms for r in rows],
            "loss": [r.loss_pct for r in rows], "hourly": False}


@router.get("/{device_id}/metrics")
def metric_history(device_id: int, hours: int = 24, db: Session = Depends(get_db),
                   user: User = Depends(require_viewer)):
    hours, hourly = _range(hours)
    since = utcnow() - timedelta(hours=hours)
    if hourly:
        rows = _rollups(db, device_id, "metrics", since)
        return {"ts": [r.hour.isoformat() for r in rows], "cpu": [r.v1 for r in rows], "mem": [r.v2 for r in rows],
                "temperature": [r.v3 for r in rows], "signal": [r.v4 for r in rows],
                "clients": [r.v2_max for r in rows], "hourly": True}
    rows = db.scalars(select(MetricSample).where(MetricSample.device_id == device_id, MetricSample.ts >= since)
                      .order_by(MetricSample.ts)).all()
    return {"ts": [r.ts.isoformat() for r in rows], "cpu": [r.cpu_pct for r in rows], "mem": [r.mem_pct for r in rows],
            "temperature": [r.temperature for r in rows], "signal": [r.signal_dbm for r in rows],
            "clients": [r.wireless_clients for r in rows], "hourly": False}


@router.get("/{device_id}/traffic")
def traffic_history(device_id: int, interface: str, hours: int = 24, db: Session = Depends(get_db),
                    user: User = Depends(require_viewer)):
    hours, hourly = _range(hours)
    since = utcnow() - timedelta(hours=hours)
    if hourly:
        rows = _rollups(db, device_id, f"if:{interface}"[:80], since)
        return {"ts": [r.hour.isoformat() for r in rows], "rx": [r.v1 for r in rows], "tx": [r.v2 for r in rows],
                "hourly": True}
    rows = db.execute(select(TrafficSample.ts, TrafficSample.rx_bps, TrafficSample.tx_bps)
                      .where(TrafficSample.device_id == device_id, TrafficSample.interface == interface,
                             TrafficSample.ts >= since).order_by(TrafficSample.ts)).all()
    return {"ts": [r.ts.isoformat() for r in rows], "rx": [r.rx_bps for r in rows], "tx": [r.tx_bps for r in rows],
            "hourly": False}

