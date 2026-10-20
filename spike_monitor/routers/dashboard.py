from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..deps import local_time, render, require_viewer
from ..models import Device, Site, Status, StatusEvent, User

router = APIRouter()

PROBLEM_STATUSES = (Status.DOWN, Status.UNREACHABLE, Status.WARNING)


def build_summary(db: Session, engine) -> dict:
    devices = db.scalars(select(Device).options(selectinload(Device.site))).all()
    counts = Counter(d.status for d in devices)
    per_site: dict[str, Counter] = {}
    for d in devices:
        per_site.setdefault(d.site.name if d.site else "No site", Counter())[d.status] += 1
    problems = sorted(
        (d for d in devices if d.status in PROBLEM_STATUSES),
        key=lambda d: (PROBLEM_STATUSES.index(d.status), d.status_changed_at or d.created_at),
    )
    return {
        "total": len(devices),
        "counts": {s: counts.get(s, 0) for s in Status.ALL},
        "sites": [
            {"name": name, "total": sum(c.values()), "counts": {s: c.get(s, 0) for s in Status.ALL}}
            for name, c in sorted(per_site.items())
        ],
        "problems": [
            {
                "id": d.id,
                "name": d.name,
                "ip": d.ip_address,
                "site": d.site.name if d.site else "",
                "status": d.status,
                "since": local_time(d.status_changed_at),
                "latency": d.last_latency_ms,
                "loss": d.last_loss_pct,
            }
            for d in problems
        ],
        "engine": {
            "last_cycle_at": local_time(engine.last_cycle_at) if engine and engine.last_cycle_at else None,
            "last_cycle_seconds": engine.last_cycle_seconds if engine else None,
            "interval": engine.interval if engine else None,
        },
    }


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_viewer)):
    events = db.scalars(
        select(StatusEvent).options(selectinload(StatusEvent.device)).order_by(StatusEvent.ts.desc()).limit(25)
    ).all()
    summary = build_summary(db, request.app.state.engine)
    site_count = len(db.scalars(select(Site.id)).all())
    return render(request, "dashboard.html", {"summary": summary, "events": events, "site_count": site_count})


@router.get("/api/summary")
def api_summary(request: Request, db: Session = Depends(get_db), user: User = Depends(require_viewer)):
    return build_summary(db, request.app.state.engine)
