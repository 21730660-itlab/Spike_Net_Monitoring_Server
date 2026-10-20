"""Background monitoring loop: pings every device and updates its status."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from ..models import Device, PingResult, Status, StatusEvent, utcnow
from .pinger import PingOutcome, ping

log = logging.getLogger(__name__)

PingFunc = Callable[[str, int, float], Awaitable[PingOutcome]]
EventListener = Callable[[list[StatusEvent]], Awaitable[None]]


def raw_status(device: Device, outcome: PingOutcome) -> str:
    if not outcome.is_alive:
        return Status.DOWN
    if outcome.loss_pct >= device.loss_warn_pct or (
        outcome.avg_ms is not None and outcome.avg_ms >= device.latency_warn_ms
    ):
        return Status.WARNING
    return Status.UP


def resolve_statuses(devices: dict[int, Device], raw: dict[int, str]) -> dict[int, str]:
    """Apply parent awareness: a down device behind a down parent is 'unreachable'.

    Only the nearest *monitored* ancestor matters; unmonitored devices in the
    chain are skipped.
    """
    final: dict[int, str] = {}

    def effective(dev_id: int, seen: frozenset[int]) -> str:
        if dev_id in final:
            return final[dev_id]
        status = raw[dev_id]
        if status == Status.DOWN:
            node = devices[dev_id].parent
            while node is not None and node.id not in seen and node.id not in raw:
                node = node.parent
            if node is not None and node.id in raw and node.id not in seen:
                if effective(node.id, seen | {dev_id}) in (Status.DOWN, Status.UNREACHABLE):
                    status = Status.UNREACHABLE
        final[dev_id] = status
        return status

    for dev_id in raw:
        effective(dev_id, frozenset({dev_id}))
    return final


class MonitorEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        interval: int = 30,
        count: int = 3,
        timeout: float = 1.0,
        concurrency: int = 100,
        down_after: int = 2,
        retention_days: int = 7,
        ping_func: PingFunc = ping,
    ):
        self.session_factory = session_factory
        self.interval = interval
        self.count = count
        self.timeout = timeout
        self.concurrency = concurrency
        self.down_after = max(1, down_after)
        self.retention_days = retention_days
        self.ping_func = ping_func
        self.listeners: list[EventListener] = []
        self._fail_streak: dict[int, int] = {}
        self._task: asyncio.Task | None = None
        self._last_cleanup = utcnow() - timedelta(days=1)
        self.last_cycle_at = None
        self.last_cycle_seconds: float | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="monitor-engine")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await self.run_cycle()
            except Exception:
                log.exception("Monitoring cycle failed")
            await asyncio.sleep(max(1.0, self.interval - (loop.time() - started)))

    # ------------------------------------------------------------ one cycle
    async def run_cycle(self) -> list[StatusEvent]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.session_factory() as db:
            targets = [(d.id, d.ip_address) for d in db.scalars(select(Device).where(Device.monitoring_enabled))]

        sem = asyncio.Semaphore(self.concurrency)

        async def one(dev_id: int, host: str) -> tuple[int, PingOutcome]:
            async with sem:
                try:
                    return dev_id, await self.ping_func(host, self.count, self.timeout)
                except Exception:
                    log.exception("Ping to %s failed", host)
                    return dev_id, PingOutcome(False, None, 100.0)

        outcomes = dict(await asyncio.gather(*(one(i, h) for i, h in targets)))
        events = self._store(outcomes)
        self.last_cycle_at = utcnow()
        self.last_cycle_seconds = loop.time() - started

        for listener in self.listeners:
            try:
                await listener(events)
            except Exception:
                log.exception("Status listener failed")
        return events

    def _store(self, outcomes: dict[int, PingOutcome]) -> list[StatusEvent]:
        now = utcnow()
        events: list[StatusEvent] = []
        with self.session_factory() as db:
            devices = {d.id: d for d in db.scalars(select(Device).where(Device.id.in_(outcomes)))}
            raw: dict[int, str] = {}
            for dev_id, outcome in outcomes.items():
                dev = devices.get(dev_id)
                if dev is None:  # deleted while we were pinging
                    continue
                status = raw_status(dev, outcome)
                if status == Status.DOWN:
                    streak = self._fail_streak.get(dev_id, 0) + 1
                    self._fail_streak[dev_id] = streak
                    # Not down long enough yet: keep the previous state.
                    if streak < self.down_after and dev.status not in (Status.DOWN, Status.UNREACHABLE):
                        status = dev.status if dev.status in (Status.UP, Status.WARNING) else Status.UNKNOWN
                else:
                    self._fail_streak.pop(dev_id, None)
                raw[dev_id] = status
                db.add(PingResult(device_id=dev_id, ts=now, is_alive=outcome.is_alive,
                                  avg_ms=outcome.avg_ms, loss_pct=outcome.loss_pct))
                dev.last_checked = now
                dev.last_latency_ms = outcome.avg_ms
                dev.last_loss_pct = outcome.loss_pct
                if outcome.is_alive:
                    dev.last_seen = now

            for dev_id, status in resolve_statuses(devices, raw).items():
                dev = devices[dev_id]
                if status != dev.status:
                    event = StatusEvent(device_id=dev_id, ts=now, old_status=dev.status, new_status=status,
                                        message=_event_message(dev, status))
                    db.add(event)
                    events.append(event)
                    dev.status = status
                    dev.status_changed_at = now

            if now - self._last_cleanup > timedelta(hours=1):
                cutoff = now - timedelta(days=self.retention_days)
                db.execute(delete(PingResult).where(PingResult.ts < cutoff))
                self._last_cleanup = now
            db.commit()
        return events


def _event_message(dev: Device, status: str) -> str:
    if status == Status.DOWN:
        return f"{dev.name} ({dev.ip_address}) is DOWN"
    if status == Status.UNREACHABLE:
        parent = dev.parent.name if dev.parent else "parent"
        return f"{dev.name} ({dev.ip_address}) is unreachable because {parent} is down"
    if status == Status.WARNING:
        lat = f"{dev.last_latency_ms:.0f} ms" if dev.last_latency_ms is not None else "n/a"
        return f"{dev.name} ({dev.ip_address}) is degraded: latency {lat}, loss {dev.last_loss_pct:.0f}%"
    if status == Status.UP:
        return f"{dev.name} ({dev.ip_address}) is UP"
    return f"{dev.name} status: {status}"
