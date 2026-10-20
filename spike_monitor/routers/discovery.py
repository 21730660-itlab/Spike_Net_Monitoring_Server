"""Neighbors seen by MikroTik devices that are not in the inventory yet."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..deps import audit, flash, render, require_technician, require_viewer
from ..forms import MAC_RE
from ..models import Brand, Customer, Device, DeviceType, Neighbor, Status, User
from ..security import verify_csrf

router = APIRouter()


def guess_brand(platform: str) -> str:
    p = platform.lower()
    if "mikrotik" in p:
        return Brand.MIKROTIK
    if "ubiquiti" in p or "airos" in p or "ubnt" in p:
        return Brand.UBIQUITI
    if "mimosa" in p:
        return Brand.MIMOSA
    return Brand.OTHER


def known_addresses(db: Session) -> tuple[set[str], set[str]]:
    """IPs and MACs already in the app (devices and customers)."""
    ips = set(db.scalars(select(Device.ip_address)))
    macs = {m for m in db.scalars(select(Device.mac_address)) if m}
    macs |= {m for m in db.scalars(select(Customer.mac_address)) if m}
    return ips, macs


def import_neighbors(db: Session, form) -> tuple[int, int, list[str]]:
    """Create the ticked neighbor rows (fields nb_<i>_*) as devices or customers.

    Returns (devices created, customers created, problems).
    """
    devices = customers = 0
    problems: list[str] = []
    ips, macs = known_addresses(db)
    for key in form.keys():
        if not (key.startswith("nb_") and key.endswith("_sel")):
            continue
        i = key[3:-4]
        get = lambda f: str(form.get(f"nb_{i}_{f}", "")).strip()  # noqa: E731
        name, ip, mac = get("name")[:128], get("ip"), get("mac").replace("-", ":").upper()
        parent = db.get(Device, int(get("parent"))) if get("parent").isdigit() else None
        label = name or mac or ip or "?"
        if mac and not MAC_RE.match(mac):
            problems.append(f"{label}: invalid MAC address.")
            continue
        if get("as") == "customer":
            if not mac:
                problems.append(f"{label}: a customer needs a MAC address.")
                continue
            if mac in macs:
                problems.append(f"{label}: MAC {mac} is already in the app.")
                continue
            db.add(Customer(name=name or mac, mac_address=mac, site_id=parent.site_id if parent else None,
                            access_point_id=parent.id if parent else None,
                            notes=f"Imported from neighbors of {parent.name}" if parent else "Imported"))
            macs.add(mac)
            customers += 1
            continue
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            problems.append(f"{label}: a device needs an IP address (type it, or import it as a customer).")
            continue
        if ip in ips:
            problems.append(f"{label}: IP {ip} is already used by another device.")
            continue
        board = get("board")
        db.add(Device(
            name=name or ip, ip_address=ip, mac_address=mac, model=board[:64], os_version=get("version")[:64],
            brand=guess_brand(get("platform") or board), device_type=DeviceType.STATION if board else DeviceType.OTHER,
            site_id=parent.site_id if parent else None, parent_id=parent.id if parent else None,
            uplink_port=get("interface")[:64], status=Status.UNKNOWN,
        ))
        ips.add(ip)
        if mac:
            macs.add(mac)
        devices += 1
    return devices, customers, problems


@router.post("/discovery/import", dependencies=[Depends(verify_csrf)])
async def discovery_import(request: Request, db: Session = Depends(get_db),
                           user: User = Depends(require_technician)):
    form = await request.form()
    devices, customers, problems = import_neighbors(db, form)
    audit(db, request, user.username, "neighbors.import", "", f"devices={devices} customers={customers}")
    db.commit()
    flash(request, f"Imported {devices} device(s) and {customers} customer(s)."
          + (f" Skipped {len(problems)}: " + " ".join(problems[:10]) if problems else ""),
          "error" if problems and not (devices or customers) else "success")
    return RedirectResponse(request.headers.get("referer") or "/discovery", status_code=303)


@router.get("/discovery")
def discovery(request: Request, db: Session = Depends(get_db), user: User = Depends(require_viewer)):
    known_ips, known_macs = known_addresses(db)
    rows = db.scalars(select(Neighbor).options(selectinload(Neighbor.seen_by)).order_by(Neighbor.identity)).all()
    found: dict[str, dict] = {}
    for n in rows:
        if (n.ip_address and n.ip_address in known_ips) or (n.mac_address and n.mac_address in known_macs):
            continue
        key = n.mac_address or n.ip_address
        if key in found:
            found[key]["seen_by"].append(n.seen_by)
            continue
        params = {k: v for k, v in {
            "name": n.identity, "ip_address": n.ip_address, "mac_address": n.mac_address, "model": n.board,
            "os_version": n.version, "brand": guess_brand(n.platform), "parent_id": n.seen_by_id,
            "site_id": n.seen_by.site_id or "",
        }.items() if v not in ("", None)}
        found[key] = {"n": n, "seen_by": [n.seen_by], "add_url": "/devices/new?" + urlencode(params)}
    items = list(found.values())
    nb_rows = [{"name": it["n"].identity, "ip": it["n"].ip_address, "mac": it["n"].mac_address,
                "platform": it["n"].platform, "board": it["n"].board, "version": it["n"].version,
                "interface": it["n"].interface, "parent_id": it["n"].seen_by_id,
                "parent_name": ", ".join(d.name for d in it["seen_by"])} for it in items]
    return render(request, "discovery.html", {"items": items, "nb_rows": nb_rows,
                                              "can_import": user.has_role("technician")})
