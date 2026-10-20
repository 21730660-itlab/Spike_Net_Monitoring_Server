"""Alerts page, acknowledge, maintenance mode, alert settings and the browser alert feed."""
from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..alerts import desktop, telegram, whatsapp
from ..alerts.engine import KIND_LABELS
from ..alerts.settings import AlertSettings, load_settings, save_settings
from ..db import get_db
from ..deps import audit, flash, local_time, render, require_admin, require_technician, require_viewer
from ..models import Alert, Device, NotificationLog, Severity, Site, User, utcnow
from ..security import verify_csrf

router = APIRouter()

SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


def _alert_json(a: Alert) -> dict:
    return {
        "id": a.id, "kind": a.kind, "label": KIND_LABELS.get(a.kind, a.kind), "severity": a.severity,
        "message": a.message, "device_id": a.device_id, "device": a.device.name if a.device else "",
        "ip": a.device.ip_address if a.device else "", "started": local_time(a.started_at),
        "resolved": local_time(a.resolved_at) if a.resolved_at else None, "acknowledged_by": a.acknowledged_by,
        "maintenance": a.in_maintenance,
    }


@router.get("/alerts")
def alerts_page(request: Request, tab: str = "active", page: int = 1, db: Session = Depends(get_db),
                user: User = Depends(require_viewer)):
    page = max(1, page)
    if tab == "history":
        rows = db.scalars(select(Alert).options(selectinload(Alert.device).selectinload(Device.site))
                          .order_by(Alert.started_at.desc()).offset((page - 1) * 100).limit(101)).all()
        has_next = len(rows) > 100
        rows = rows[:100]
    else:
        rows = db.scalars(select(Alert).options(selectinload(Alert.device).selectinload(Device.site))
                          .where(Alert.resolved_at.is_(None))).all()
        rows = sorted(rows, key=lambda a: (SEVERITY_ORDER.get(a.severity, 9), a.started_at))
        has_next = False
    return render(request, "alerts.html", {
        "alerts": rows, "tab": tab, "page": page, "has_next": has_next, "labels": KIND_LABELS,
        "can_ack": user.has_role("technician"), "now": utcnow()})


@router.post("/alerts/{alert_id}/ack", dependencies=[Depends(verify_csrf)])
def acknowledge(alert_id: int, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_technician)):
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "Alert not found")
    if not alert.acknowledged_at:
        alert.acknowledged_at, alert.acknowledged_by = utcnow(), user.username
        audit(db, request, user.username, "alert.ack", f"Alert #{alert.id} {alert.kind}")
        db.commit()
    return RedirectResponse(request.headers.get("referer") or "/alerts", status_code=303)


@router.post("/alerts/ack-all", dependencies=[Depends(verify_csrf)])
def acknowledge_all(request: Request, db: Session = Depends(get_db), user: User = Depends(require_technician)):
    now = utcnow()
    rows = db.scalars(select(Alert).where(Alert.resolved_at.is_(None), Alert.acknowledged_at.is_(None))).all()
    for a in rows:
        a.acknowledged_at, a.acknowledged_by = now, user.username
    audit(db, request, user.username, "alert.ack_all", f"{len(rows)} alerts")
    db.commit()
    flash(request, f"{len(rows)} alert(s) acknowledged.")
    return RedirectResponse("/alerts", status_code=303)


@router.get("/api/alerts/feed")
def feed(after: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_viewer)):
    """Polled by every open page: active counts and alerts newer than ``after``.

    The first poll of a page sends no ``after`` and just learns the newest id.
    """
    counts = dict(db.execute(select(Alert.severity, func.count()).where(
        Alert.resolved_at.is_(None), Alert.in_maintenance.is_(False)).group_by(Alert.severity)).all())
    max_id = db.scalar(select(func.max(Alert.id))) or 0
    new = []
    if after is not None:
        recent = utcnow() - timedelta(minutes=15)  # don't replay old alerts to a tab opened much later
        new = db.scalars(select(Alert).options(selectinload(Alert.device))
                         .where(Alert.id > after, Alert.started_at >= recent).order_by(Alert.id).limit(20)).all()
    return {"active": sum(counts.values()), "critical": counts.get(Severity.CRITICAL, 0), "max_id": max_id,
            "new": [_alert_json(a) for a in new if not a.in_maintenance]}


# ------------------------------------------------------------------ maintenance
def _until(hours: float):
    return utcnow() + timedelta(hours=hours) if hours > 0 else None


@router.post("/devices/{device_id}/maintenance", dependencies=[Depends(verify_csrf)])
def device_maintenance(device_id: int, request: Request, hours: float = Form(0), db: Session = Depends(get_db),
                       user: User = Depends(require_technician)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    device.maintenance_until = _until(min(hours, 24 * 30))
    audit(db, request, user.username, "device.maintenance", f"Device #{device.id} {device.name}",
          f"until {local_time(device.maintenance_until)}" if device.maintenance_until else "ended")
    db.commit()
    flash(request, f"Maintenance until {local_time(device.maintenance_until)}: no alerts will be sent for "
                   f"{device.name}." if device.maintenance_until else "Maintenance ended.")
    return RedirectResponse(f"/devices/{device_id}", status_code=303)


@router.post("/sites/{site_id}/maintenance", dependencies=[Depends(verify_csrf)])
def site_maintenance(site_id: int, request: Request, hours: float = Form(0), db: Session = Depends(get_db),
                     user: User = Depends(require_technician)):
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    site.maintenance_until = _until(min(hours, 24 * 30))
    audit(db, request, user.username, "site.maintenance", f"Site #{site.id} {site.name}",
          f"until {local_time(site.maintenance_until)}" if site.maintenance_until else "ended")
    db.commit()
    flash(request, f"Site {site.name} in maintenance until {local_time(site.maintenance_until)}."
          if site.maintenance_until else f"Maintenance ended for {site.name}.")
    return RedirectResponse(request.headers.get("referer") or "/sites", status_code=303)


# ------------------------------------------------------------------ settings (admin)
NUMBER_FIELDS = {"cpu_pct", "temperature_c", "signal_dbm", "broadcast_pps"}
INT_FIELDS = {"poll_failed_minutes", "warning_delay_minutes", "repeat_minutes"}
BOOL_FIELDS = {"notify_recovery", "notify_reboot", "desktop_enabled", "whatsapp_enabled", "telegram_enabled"}
TEXT_FIELDS = {"desktop_min_severity", "tg_group_chat_id", "tg_group_min_severity", "wa_provider", "wa_phone_number_id", "wa_mode", "wa_template", "wa_language", "wa_api_version"}


def _settings_page(request, db, s: AlertSettings, error=None, status=200, chats=None):
    logs = db.scalars(select(NotificationLog).order_by(NotificationLog.ts.desc()).limit(25)).all()
    recipients = db.scalars(select(User).where(User.whatsapp_alerts, User.is_active)).all()
    tg_recipients = db.scalars(select(User).where(User.telegram_alerts, User.is_active)).all()
    return render(request, "admin/alert_settings.html", {
        "s": s, "error": error, "logs": logs, "recipients": recipients, "has_token": bool(s.wa_token_enc),
        "tg_recipients": tg_recipients, "has_tg_token": bool(s.tg_token_enc), "chats": chats},
        status_code=status)


@router.get("/admin/alert-settings")
def settings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _settings_page(request, db, load_settings(db))


@router.post("/admin/alert-settings", dependencies=[Depends(verify_csrf)])
async def save(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = await request.form()
    s = load_settings(db)
    try:
        for f in fields(AlertSettings):
            if f.name in NUMBER_FIELDS:
                setattr(s, f.name, float(form.get(f.name, getattr(s, f.name))))
            elif f.name in INT_FIELDS:
                setattr(s, f.name, max(0, int(form.get(f.name, getattr(s, f.name)))))
            elif f.name in BOOL_FIELDS:
                setattr(s, f.name, form.get(f.name) == "on")
            elif f.name in TEXT_FIELDS:
                setattr(s, f.name, str(form.get(f.name, getattr(s, f.name))).strip()[:128])
    except ValueError:
        return _settings_page(request, db, s, "Numbers only in the threshold and timing fields.", 422)
    if (s.wa_mode not in ("template", "text") or s.desktop_min_severity not in Severity.ALL
            or s.wa_provider not in ("callmebot", "meta") or s.tg_group_min_severity not in Severity.ALL):
        return _settings_page(request, db, s, "Invalid choice.", 422)
    s.wa_phone_number_id = s.wa_phone_number_id.replace(" ", "")
    if s.wa_provider == "meta" and (problem := whatsapp.phone_number_id_problem(s.wa_phone_number_id)):
        return _settings_page(request, db, s, problem, 422)
    token = str(form.get("wa_token", "")).strip()
    if token:
        s.wa_token_enc = request.app.state.secret_box.encrypt(token)
    if form.get("clear_token") == "on":
        s.wa_token_enc = ""
    tg_token = str(form.get("tg_token", "")).strip()
    if tg_token:
        if ":" not in tg_token:
            return _settings_page(request, db, s, "That is not a Telegram bot token. It looks like "
                                  "123456789:AAH... and comes from @BotFather.", 422)
        s.tg_token_enc = request.app.state.secret_box.encrypt(tg_token)
    if form.get("clear_tg_token") == "on":
        s.tg_token_enc = ""
    save_settings(db, s)
    audit(db, request, user.username, "alert_settings.update")
    db.commit()
    flash(request, "Alert settings saved.")
    return RedirectResponse("/admin/alert-settings", status_code=303)


@router.post("/admin/alert-settings/test-whatsapp", dependencies=[Depends(verify_csrf)])
async def test_whatsapp(request: Request, phone: str = Form(""), apikey: str = Form(""),
                        db: Session = Depends(get_db), user: User = Depends(require_admin)):
    s = load_settings(db)
    box = request.app.state.secret_box
    number = whatsapp.normalize_number(phone or user.phone)
    if not number:
        flash(request, "Enter a phone number to send the test to.", "error")
        return RedirectResponse("/admin/alert-settings", status_code=303)
    text = f"Test message from Spike Net Monitor, sent by {user.username}."
    try:
        if s.wa_provider == "callmebot":
            if not apikey:  # use the key saved for the user with this number
                owner = next((u for u in db.scalars(select(User))
                              if whatsapp.normalize_number(u.phone) == number and u.callmebot_key_enc), None)
                apikey = box.decrypt(owner.callmebot_key_enc) if owner else ""
            await asyncio.to_thread(whatsapp.send_callmebot, number, apikey.strip(), text)
            result = "queued"
        else:
            payload = whatsapp.build_payload(number, text, s.wa_mode, s.wa_template, s.wa_language)
            result = await asyncio.to_thread(whatsapp.send, s.wa_phone_number_id, box.decrypt(s.wa_token_enc),
                                             payload, s.wa_api_version)
        db.add(NotificationLog(channel="whatsapp", recipient=number, message=text))
        flash(request, f"Test sent to +{number} ({result}). Check the phone.")
    except whatsapp.WhatsAppError as exc:
        db.add(NotificationLog(channel="whatsapp", recipient=number, message=text, ok=False, error=str(exc)))
        flash(request, str(exc), "error")
    db.commit()
    return RedirectResponse("/admin/alert-settings", status_code=303)


@router.get("/admin/alert-settings/telegram-chats")
async def telegram_chats(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    s = load_settings(db)
    try:
        chats = await asyncio.to_thread(telegram.find_chats, request.app.state.secret_box.decrypt(s.tg_token_enc))
    except telegram.TelegramError as exc:
        return _settings_page(request, db, s, str(exc), 200)
    return _settings_page(request, db, s, chats=chats)


@router.post("/admin/alert-settings/test-telegram", dependencies=[Depends(verify_csrf)])
async def test_telegram(request: Request, chat_id: str = Form(""), db: Session = Depends(get_db),
                        user: User = Depends(require_admin)):
    s = load_settings(db)
    chat_id = chat_id.strip() or user.telegram_chat_id or s.tg_group_chat_id
    text = f"✅ Test message from Spike Net Monitor, sent by {user.username}."
    try:
        await asyncio.to_thread(telegram.send, request.app.state.secret_box.decrypt(s.tg_token_enc), chat_id, text)
        db.add(NotificationLog(channel="telegram", recipient=chat_id, message=text))
        flash(request, f"Test sent to Telegram chat {chat_id}. Check Telegram.")
    except telegram.TelegramError as exc:
        db.add(NotificationLog(channel="telegram", recipient=chat_id, message=text, ok=False, error=str(exc)))
        flash(request, str(exc), "error")
    db.commit()
    return RedirectResponse("/admin/alert-settings", status_code=303)


@router.post("/admin/alert-settings/test-desktop", dependencies=[Depends(verify_csrf)])
def test_desktop(request: Request, user: User = Depends(require_admin)):
    if desktop.show("Spike Net test", f"Desktop alerts work. Sent by {user.username}.", critical=False):
        flash(request, "Test popup sent to the server PC's desktop.")
    else:
        flash(request, "Desktop popups only work when the server runs on Windows in a logged-in session.", "error")
    return RedirectResponse("/admin/alert-settings", status_code=303)
