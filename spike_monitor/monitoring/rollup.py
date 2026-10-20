"""Hourly averages and data retention.

Raw samples are kept for a few days; hourly averages for a year. Long graphs
(30 days, 1 year) read the hourly table so they stay fast with hundreds of devices.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from ..models import HourlyRollup, MetricSample, PingResult, TrafficSample, utcnow

log = logging.getLogger(__name__)

MAX_CATCHUP_HOURS = 72


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _max(values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def hour_floor(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def rollup_hour(db: Session, hour: datetime) -> int:
    """Write hourly averages for [hour, hour+1h). Returns the number of rows written."""
    end = hour + timedelta(hours=1)
    rows: list[HourlyRollup] = []

    ping = defaultdict(list)
    for r in db.execute(select(PingResult.device_id, PingResult.is_alive, PingResult.avg_ms, PingResult.loss_pct)
                        .where(PingResult.ts >= hour, PingResult.ts < end)):
        ping[r.device_id].append(r)
    for dev_id, items in ping.items():
        rows.append(HourlyRollup(
            device_id=dev_id, series="ping", hour=hour,
            v1=_avg(i.avg_ms for i in items), v2=_avg(i.loss_pct for i in items),
            v3=100.0 * sum(1 for i in items if i.is_alive) / len(items),
            v1_max=_max(i.avg_ms for i in items), samples=len(items)))

    metrics = defaultdict(list)
    for r in db.scalars(select(MetricSample).where(MetricSample.ts >= hour, MetricSample.ts < end)):
        metrics[r.device_id].append(r)
    for dev_id, items in metrics.items():
        rows.append(HourlyRollup(
            device_id=dev_id, series="metrics", hour=hour,
            v1=_avg(i.cpu_pct for i in items), v2=_avg(i.mem_pct for i in items),
            v3=_avg(i.temperature for i in items), v4=_avg(i.signal_dbm for i in items),
            v1_max=_max(i.cpu_pct for i in items), v2_max=_avg(i.wireless_clients for i in items),
            samples=len(items)))

    traffic = defaultdict(list)
    for r in db.execute(select(TrafficSample.device_id, TrafficSample.interface, TrafficSample.rx_bps,
                               TrafficSample.tx_bps).where(TrafficSample.ts >= hour, TrafficSample.ts < end)):
        traffic[(r.device_id, r.interface)].append(r)
    for (dev_id, name), items in traffic.items():
        rows.append(HourlyRollup(
            device_id=dev_id, series=f"if:{name}"[:80], hour=hour,
            v1=_avg(i.rx_bps for i in items), v2=_avg(i.tx_bps for i in items),
            v1_max=_max(i.rx_bps for i in items), v2_max=_max(i.tx_bps for i in items), samples=len(items)))

    db.execute(delete(HourlyRollup).where(HourlyRollup.hour == hour))
    db.add_all(rows)
    return len(rows)


class Housekeeper:
    def __init__(self, session_factory: sessionmaker[Session], raw_retention_days: int = 7,
                 rollup_retention_days: int = 365):
        self.session_factory = session_factory
        self.raw_retention_days = raw_retention_days
        self.rollup_retention_days = rollup_retention_days
        self._last_run_hour: datetime | None = None

    def run_if_due(self) -> None:
        current = hour_floor(utcnow())
        if self._last_run_hour == current:
            return
        self.run(current)
        self._last_run_hour = current

    def run(self, current_hour: datetime) -> None:
        with self.session_factory() as db:
            last = db.scalar(select(func.max(HourlyRollup.hour)))
            start = current_hour - timedelta(hours=MAX_CATCHUP_HOURS)
            hour = max(last + timedelta(hours=1), start) if last else start
            while hour < current_hour:
                rollup_hour(db, hour)
                hour += timedelta(hours=1)
            raw_cutoff = utcnow() - timedelta(days=self.raw_retention_days)
            db.execute(delete(MetricSample).where(MetricSample.ts < raw_cutoff))
            db.execute(delete(TrafficSample).where(TrafficSample.ts < raw_cutoff))
            db.execute(delete(HourlyRollup).where(
                HourlyRollup.hour < utcnow() - timedelta(days=self.rollup_retention_days)))
            db.commit()
