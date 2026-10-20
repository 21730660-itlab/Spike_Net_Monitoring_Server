"""Clear history / logs (admin only). The page asks for confirmation with a 5 second countdown."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, flash, require_admin
from ..models import (
    Alert,
    AuditLog,
    CommandJob,
    Device,
    HourlyRollup,
    MetricSample,
    Neighbor,
    NotificationLog,
    PingResult,
    StatusEvent,
    TrafficSample,
    User,
)
from ..security import verify_csrf

router = APIRouter()


def _count(db: Session, stmt) -> int:
    return db.execute(stmt).rowcount or 0


def _device_data(db: Session, device_id: int) -> int:
    total = 0
    for model in (PingResult, MetricSample, TrafficSample, HourlyRollup):
        total += _count(db, delete(model).where(model.device_id == device_id))
    return total


# kind -> (label, function(db, device_id) -> rows deleted)
CLEARERS: dict[str, tuple[str, Callable[[Session, int | None], int]]] = {
    "events": ("status events", lambda db, _: _count(db, delete(StatusEvent))),
    "device_events": ("status events of this device",
                      lambda db, dev: _count(db, delete(StatusEvent).where(StatusEvent.device_id == dev))),
    "device_data": ("graph history of this device", lambda db, dev: _device_data(db, dev)),
    # Open alerts are live problems and stay; only closed ones are history.
    "alerts": ("closed alerts", lambda db, _: _count(db, delete(Alert).where(Alert.resolved_at.is_not(None)))),
    "notifications": ("notification log", lambda db, _: _count(db, delete(NotificationLog))),
    "commands": ("finished push-settings jobs",
                 lambda db, _: _count(db, delete(CommandJob).where(CommandJob.status == "done"))),
    "neighbors": ("discovered devices list", lambda db, _: _count(db, delete(Neighbor))),
    "audit": ("audit log", lambda db, _: _count(db, delete(AuditLog))),
}


@router.post("/history/clear/{kind}", dependencies=[Depends(verify_csrf)])
def clear(kind: str, request: Request, device_id: int | None = Form(None), db: Session = Depends(get_db),
          user: User = Depends(require_admin)):
    if kind not in CLEARERS:
        raise HTTPException(404, "Unknown history")
    label, fn = CLEARERS[kind]
    target = ""
    if kind.startswith("device_"):
        device = db.get(Device, device_id) if device_id else None
        if device is None:
            raise HTTPException(404, "Device not found")
        target = f"Device #{device.id} {device.name}"
    if kind == "commands":  # results go with their job (ORM cascade is not used by bulk delete)
        from ..models import CommandResult
        done = [j.id for j in db.query(CommandJob.id).filter(CommandJob.status == "done")]
        if done:
            db.execute(delete(CommandResult).where(CommandResult.job_id.in_(done)))
    removed = fn(db, device_id)
    # Recorded after clearing, so even a cleared audit log shows who cleared it.
    audit(db, request, user.username, f"history.clear.{kind}", target, f"{removed} rows removed")
    db.commit()
    flash(request, f"Cleared the {label} ({removed} entries).")
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
