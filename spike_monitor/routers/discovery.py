"""Neighbors seen by MikroTik devices that are not in the inventory yet."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..deps import render, require_viewer
from ..models import Brand, Device, Neighbor, User

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


@router.get("/discovery")
def discovery(request: Request, db: Session = Depends(get_db), user: User = Depends(require_viewer)):
    known_ips = set(db.scalars(select(Device.ip_address)))
    known_macs = {m for m in db.scalars(select(Device.mac_address)) if m}
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
    return render(request, "discovery.html", {"items": list(found.values())})
