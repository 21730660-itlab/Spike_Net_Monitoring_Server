"""Network map: sites and the links between them, as a topology diagram and on a real map."""
from __future__ import annotations

from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..deps import audit, render, require_technician, require_viewer
from ..models import Device, DeviceType, Site, Status, User
from ..security import verify_csrf

router = APIRouter()

# Worst first: the colour of a site or link is the worst status inside it.
SEVERITY = [Status.DOWN, Status.UNREACHABLE, Status.WARNING, Status.UNKNOWN, Status.UP, Status.PAUSED]


def worst(statuses) -> str:
    statuses = set(statuses)
    for s in SEVERITY:
        if s in statuses:
            return s
    return Status.UNKNOWN


def build_map(db: Session) -> dict:
    sites = db.scalars(select(Site).order_by(Site.name)).all()
    devices = db.scalars(select(Device).options(selectinload(Device.parent))).all()
    by_site: dict[int | None, list[Device]] = defaultdict(list)
    for d in devices:
        by_site[d.site_id].append(d)

    def site_json(site_id, name, site=None):
        members = by_site.get(site_id, [])
        counts = Counter(d.status for d in members)
        return {
            "id": site_id if site_id is not None else 0,
            "name": name,
            "lat": site.latitude if site else None,
            "lon": site.longitude if site else None,
            "x": site.map_x if site else None,
            "y": site.map_y if site else None,
            "total": len(members),
            "counts": dict(counts),
            "status": worst(d.status for d in members) if members else Status.UNKNOWN,
            "upstream": any(d.device_type == DeviceType.UPSTREAM for d in members),
            "devices": [{"id": d.id, "name": d.name, "status": d.status} for d in members],
        }

    site_list = [site_json(s.id, s.name, s) for s in sites]
    if by_site.get(None):
        site_list.append(site_json(None, "No site"))

    # Links between sites come from device parents that sit in another site.
    links: dict[tuple[int, int], list[Device]] = defaultdict(list)
    for d in devices:
        if d.parent is not None and d.parent.site_id != d.site_id:
            a, b = d.parent.site_id or 0, d.site_id or 0
            links[(a, b)].append(d)
    site_links = [
        {
            "from": a, "to": b, "count": len(items),
            "status": worst(d.status for d in items),
            "label": ", ".join(d.name for d in items[:3]) + (f" +{len(items) - 3}" if len(items) > 3 else ""),
            "signal": min((d.signal_dbm for d in items if d.signal_dbm is not None), default=None),
        }
        for (a, b), items in links.items()
    ]

    device_list = [
        {
            "id": d.id, "name": d.name, "ip": d.ip_address, "site_id": d.site_id or 0, "parent_id": d.parent_id,
            "status": d.status, "type": d.device_type, "brand": d.brand, "x": d.map_x, "y": d.map_y,
            "latency": d.last_latency_ms, "signal": d.signal_dbm, "clients": d.wireless_clients,
        }
        for d in devices
    ]
    return {"sites": site_list, "site_links": site_links, "devices": device_list}


@router.get("/map")
def map_page(request: Request, user: User = Depends(require_viewer)):
    return render(request, "map.html", {"can_edit": user.has_role("technician")})


@router.get("/api/map")
def map_data(db: Session = Depends(get_db), user: User = Depends(require_viewer)):
    return build_map(db)


@router.post("/api/map/layout", dependencies=[Depends(verify_csrf)])
async def save_layout(request: Request, db: Session = Depends(get_db), user: User = Depends(require_technician)):
    """Body: {"kind": "site" | "device", "positions": {"<id>": [x, y]}}"""
    body = await request.json()
    model = {"site": Site, "device": Device}.get(body.get("kind"))
    if model is None or not isinstance(body.get("positions"), dict):
        raise HTTPException(400, "Bad layout data")
    saved = 0
    for key, pos in body["positions"].items():
        if not str(key).isdigit() or not isinstance(pos, list) or len(pos) != 2:
            continue
        obj = db.get(model, int(key))
        if obj is not None:
            obj.map_x, obj.map_y = float(pos[0]), float(pos[1])
            saved += 1
    audit(db, request, user.username, "map.layout", f"{body['kind']} positions", f"{saved} saved")
    db.commit()
    return {"saved": saved}


@router.post("/api/map/sites/{site_id}/location", dependencies=[Depends(verify_csrf)])
async def save_location(site_id: int, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_technician)):
    body = await request.json()
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Bad coordinates")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Bad coordinates")
    site.latitude, site.longitude = round(lat, 6), round(lon, 6)
    audit(db, request, user.username, "site.location", f"Site #{site.id} {site.name}", f"{lat:.6f},{lon:.6f}")
    db.commit()
    return {"lat": site.latitude, "lon": site.longitude}
