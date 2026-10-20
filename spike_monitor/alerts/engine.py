"""Turns device state into alerts and decides which notifications to send.

``reconcile`` compares what is wrong right now with the open alerts: new
problems open an alert, fixed problems resolve it. It runs after every ping
round, so it also picks up the latest CPU / signal / traffic values read by
the poller.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from ..collectors.common import format_uptime
from ..models import Alert, Device, DeviceType, NotificationLog, Severity, Status, User, utcnow
from ..security import SecretBox
from . import desktop, whatsapp
from .settings import AlertSettings, load_settings

log = logging.getLogger(__name__)

KIND_LABELS = {
    "down": "DOWN",
    "degraded": "DEGRADED",
    "high_cpu": "HIGH CPU",
    "high_temperature": "HIGH TEMPERATURE",
    "weak_signal": "WEAK SIGNAL",
    "broadcast_storm": "BROADCAST STORM / POSSIBLE LOOP",
    "poll_failed": "CANNOT READ DEVICE",
    "rebooted": "REBOOTED",
}
METRIC_KINDS = {"high_cpu", "high_temperature", "weak_signal", "broadcast_storm"}
INSTANT_KINDS = {"rebooted"}  # notified once, no "resolved" message
SIGNAL_TYPES = (DeviceType.PTP_LINK, DeviceType.STATION, DeviceType.UPSTREAM)
ICONS = {Severity.CRITICAL: "🔴", Severity.WARNING: "🟠", Severity.INFO: "🔵"}


@dataclass
class Condition:
    kind: str
    severity: str
    detail: str = ""
    value: float | None = None


@dataclass
class Outgoing:
    alert_id: int
    kind: str  # new, repeat, recovery
    severity: str
    site_id: int | None
    text: str


def _name(d: Device) -> str:
    site = f" at {d.site.name}" if d.site else ""
    return f"{d.name} ({d.ip_address}){site}"


def device_conditions(d: Device, s: AlertSettings, open_kinds: set[str], now: datetime,
                      fresh_after: datetime, affected: int = 0) -> list[Condition]:
    """What is wrong with this device right now. ``open_kinds`` adds hysteresis so alerts don't flap."""
    conds: list[Condition] = []
    if d.status == Status.DOWN:
        parts = ["upstream ISP link"] if d.device_type == DeviceType.UPSTREAM else []
        if affected:
            parts.append(f"{affected} device(s) behind it unreachable")
        conds.append(Condition("down", Severity.CRITICAL, ", ".join(parts)))
        return conds
    if d.status == Status.UNREACHABLE:
        return conds  # covered by the alert on the parent that is down
    if d.status == Status.WARNING:
        lat = f"latency {d.last_latency_ms:.0f} ms" if d.last_latency_ms is not None else ""
        loss = f"loss {d.last_loss_pct:.0f}%" if d.last_loss_pct else ""
        conds.append(Condition("degraded", Severity.WARNING, ", ".join(x for x in (lat, loss) if x),
                               d.last_latency_ms))

    fresh = d.last_polled is not None and d.last_polled >= fresh_after and not d.poll_error
    if d.poll_error and d.status in (Status.UP, Status.WARNING):
        conds.append(Condition("poll_failed", Severity.INFO, d.poll_error[:200]))
    if not fresh:
        # No recent data: keep metric alerts as they are rather than guessing.
        conds.extend(Condition(k, "", "") for k in open_kinds & METRIC_KINDS)
        return conds

    def over(kind, value, limit, margin):
        if value is None:
            return False
        return value >= (limit - margin if kind in open_kinds else limit)

    if over("high_cpu", d.cpu_pct, s.cpu_pct, 5):
        conds.append(Condition("high_cpu", Severity.WARNING, f"{d.cpu_pct:.0f}%", d.cpu_pct))
    if over("high_temperature", d.temperature, s.temperature_c, 3):
        conds.append(Condition("high_temperature", Severity.WARNING, f"{d.temperature:.0f}°C", d.temperature))
    if d.device_type in SIGNAL_TYPES and d.signal_dbm is not None:
        limit = s.signal_dbm + (3 if "weak_signal" in open_kinds else 0)
        if d.signal_dbm <= limit:
            conds.append(Condition("weak_signal", Severity.WARNING, f"{d.signal_dbm:.0f} dBm", d.signal_dbm))
    storm = [(i.name, i.bcast_pps) for i in d.interfaces
             if i.bcast_pps is not None and over("broadcast_storm", i.bcast_pps, s.broadcast_pps, s.broadcast_pps / 2)]
    if storm:
        top = max(p for _, p in storm)
        conds.append(Condition("broadcast_storm", Severity.CRITICAL,
                               ", ".join(f"{n} {p:.0f} pkt/s" for n, p in storm), top))
    return conds


def alert_text(alert: Alert, device: Device) -> str:
    label = KIND_LABELS.get(alert.kind, alert.kind.upper())
    detail = f" – {alert.message}" if alert.message else ""
    return f"{ICONS.get(alert.severity, '')} {label}: {_name(device)}{detail}".strip()


class AlertManager:
    def __init__(self, session_factory: sessionmaker[Session], box: SecretBox, stats_interval: int = 60):
        self.session_factory = session_factory
        self.box = box
        self.stats_interval = stats_interval
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def schedule(self, *_ignored) -> None:
        """Ping-engine listener: check alerts in the background so slow sends never delay pinging."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._safe_run(), name="alerts")

    async def _safe_run(self) -> None:
        try:
            await self.run()
        except Exception:
            log.exception("Alert check failed")

    async def run(self, *_ignored) -> list[Outgoing]:
        """Reconcile and deliver."""
        async with self._lock:
            outgoing = await asyncio.to_thread(self.reconcile)
            if outgoing:
                await self.deliver(outgoing)
            return outgoing

    # ------------------------------------------------------------------ detect
    def reconcile(self, now: datetime | None = None) -> list[Outgoing]:
        now = now or utcnow()
        with self.session_factory() as db:
            s = load_settings(db)
            devices = db.scalars(select(Device).options(selectinload(Device.site),
                                                        selectinload(Device.interfaces))).all()
            by_id = {d.id: d for d in devices}
            children = defaultdict(list)
            for d in devices:
                if d.parent_id:
                    children[d.parent_id].append(d.id)

            def affected(dev_id: int) -> int:
                count, stack, seen = 0, list(children[dev_id]), {dev_id}
                while stack:
                    cid = stack.pop()
                    if cid in seen:
                        continue
                    seen.add(cid)
                    if by_id[cid].status == Status.UNREACHABLE:
                        count += 1
                    stack.extend(children[cid])
                return count

            open_alerts: dict[tuple[int, str], Alert] = {
                (a.device_id, a.kind): a
                for a in db.scalars(select(Alert).where(Alert.resolved_at.is_(None)))
            }
            open_by_device = defaultdict(set)
            for dev_id, kind in open_alerts:
                open_by_device[dev_id].add(kind)

            fresh_after = now - timedelta(seconds=max(5 * self.stats_interval, 600))
            outgoing: list[Outgoing] = []
            seen_keys: set[tuple[int, str]] = set()

            for d in devices:
                if not d.monitoring_enabled:
                    continue
                maintenance = d.in_maintenance(now)
                for c in device_conditions(d, s, open_by_device[d.id], now, fresh_after, affected(d.id)):
                    key = (d.id, c.kind)
                    seen_keys.add(key)
                    alert = open_alerts.get(key)
                    if alert is None:
                        alert = Alert(device_id=d.id, kind=c.kind, severity=c.severity, message=c.detail[:500],
                                      value=c.value, started_at=now, in_maintenance=maintenance)
                        db.add(alert)
                        alert.device = d
                        open_alerts[key] = alert
                    elif c.severity:  # "" = carried over without fresh data
                        alert.message, alert.value, alert.severity = c.detail[:500], c.value, c.severity
                    alert.in_maintenance = maintenance

                # Reboot: a one-off alert per restart.
                if d.last_reboot_at and now - d.last_reboot_at < timedelta(hours=1):
                    exists = db.scalars(select(Alert.id).where(
                        Alert.device_id == d.id, Alert.kind == "rebooted",
                        Alert.started_at >= d.last_reboot_at)).first()
                    if not exists:
                        alert = Alert(device_id=d.id, kind="rebooted", severity=Severity.INFO,
                                      message=f"uptime now {format_uptime(d.uptime_s)}", started_at=now,
                                      resolved_at=now, in_maintenance=maintenance)
                        alert.device = d
                        db.add(alert)
                        db.flush()
                        if s.notify_reboot and not maintenance:
                            self._mark(alert, now)
                            outgoing.append(Outgoing(alert.id, "new", alert.severity, d.site_id, alert_text(alert, d)))

            # Resolve what is no longer wrong (or belongs to devices no longer monitored / deleted).
            for key, alert in open_alerts.items():
                if key in seen_keys or alert.resolved_at is not None:
                    continue
                alert.resolved_at = now
                d = by_id.get(alert.device_id)
                if d and alert.notified_at and s.notify_recovery and alert.kind not in INSTANT_KINDS:
                    lasted = format_uptime(int((now - alert.started_at).total_seconds()))
                    label = KIND_LABELS.get(alert.kind, alert.kind)
                    outgoing.append(Outgoing(alert.id, "recovery", alert.severity, d.site_id,
                                             f"✅ RESOLVED {label}: {_name(d)} (lasted {lasted})"))
            db.flush()

            # Decide what to send for alerts that are still open.
            for key, alert in open_alerts.items():
                if alert.resolved_at is not None or alert.in_maintenance:
                    continue
                d = by_id[alert.device_id]
                age = now - alert.started_at
                if alert.notified_at is None:
                    delay = {Severity.CRITICAL: 0, Severity.WARNING: s.warning_delay_minutes,
                             Severity.INFO: s.poll_failed_minutes}.get(alert.severity, 0)
                    if age >= timedelta(minutes=delay):
                        self._mark(alert, now)
                        outgoing.append(Outgoing(alert.id, "new", alert.severity, d.site_id, alert_text(alert, d)))
                elif (alert.severity == Severity.CRITICAL and not alert.acknowledged_at and s.repeat_minutes > 0
                      and now - alert.last_notified_at >= timedelta(minutes=s.repeat_minutes)):
                    self._mark(alert, now)
                    outgoing.append(Outgoing(alert.id, "repeat", alert.severity, d.site_id,
                                             f"🔁 STILL {format_uptime(int(age.total_seconds()))}: "
                                             + alert_text(alert, d)))
            db.commit()
            return outgoing

    @staticmethod
    def _mark(alert: Alert, now: datetime) -> None:
        alert.notified_at = alert.notified_at or now
        alert.last_notified_at = now
        alert.notify_count = (alert.notify_count or 0) + 1

    # ------------------------------------------------------------------ deliver
    async def deliver(self, outgoing: list[Outgoing]) -> None:
        with self.session_factory() as db:
            s = load_settings(db)
            users = db.scalars(select(User).options(selectinload(User.alert_sites))
                               .where(User.is_active, User.whatsapp_alerts)).all()
            recipients = [(u.username, whatsapp.normalize_number(u.phone), u.alert_min_severity,
                           {site.id for site in u.alert_sites}, self.box.decrypt(u.callmebot_key_enc))
                          for u in users if u.phone]
        logs: list[NotificationLog] = []

        if s.desktop_enabled:
            min_rank = Severity.RANK.get(s.desktop_min_severity, 1)
            items = [o for o in outgoing if Severity.RANK.get(o.severity, 0) >= min_rank]
            if items:
                critical = any(o.severity == Severity.CRITICAL and o.kind != "recovery" for o in items)
                title = "Spike Net alert" if len(items) == 1 else f"Spike Net: {len(items)} alerts"
                body = "\n".join(o.text for o in items[:5]) + (f"\n…and {len(items) - 5} more" if len(items) > 5 else "")
                if await asyncio.to_thread(desktop.show, title, body, critical):
                    logs.append(NotificationLog(channel="desktop", recipient="server PC", message=body))

        if s.whatsapp_enabled and recipients:
            token = self.box.decrypt(s.wa_token_enc)
            for username, number, min_sev, site_ids, callmebot_key in recipients:
                mine = [o for o in outgoing
                        if Severity.RANK.get(o.severity, 0) >= Severity.RANK.get(min_sev, 1)
                        and (not site_ids or o.site_id in site_ids)]
                if not mine:
                    continue
                text = mine[0].text if len(mine) == 1 else f"{len(mine)} alerts:\n" + "\n".join(o.text for o in mine)
                try:
                    if s.wa_provider == "callmebot":
                        await asyncio.to_thread(whatsapp.send_callmebot, number, callmebot_key, text)
                    else:
                        payload = whatsapp.build_payload(number, text, s.wa_mode, s.wa_template, s.wa_language)
                        await asyncio.to_thread(whatsapp.send, s.wa_phone_number_id, token, payload,
                                                s.wa_api_version)
                    logs.append(NotificationLog(channel="whatsapp", recipient=f"{username} ({number})", message=text))
                except whatsapp.WhatsAppError as exc:
                    log.warning("WhatsApp to %s failed: %s", username, exc)
                    logs.append(NotificationLog(channel="whatsapp", recipient=f"{username} ({number})", message=text,
                                                ok=False, error=str(exc)))
        if logs:
            with self.session_factory() as db:
                db.add_all(logs)
                db.commit()
