"""Reads health, interfaces and wireless data from devices on a schedule.

MikroTik devices are read with the RouterOS API, Ubiquiti devices over SSH.
Devices that are down (by ping) are skipped until they come back.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from ..collectors import mikrotik, ubiquiti
from ..collectors.common import CollectorError, DeviceStats, Inventory
from ..collectors.mikrotik import ApiTarget
from ..collectors.ssh import SshTarget
from ..models import (
    Brand,
    Device,
    DeviceInterface,
    MetricSample,
    Neighbor,
    Status,
    TrafficSample,
    WirelessPeer,
    utcnow,
)
from ..security import SecretBox
from .rollup import Housekeeper

log = logging.getLogger(__name__)

POLLABLE_STATUSES = (Status.UP, Status.WARNING, Status.UNKNOWN)

Collector = Callable[[Device, SecretBox], Awaitable[tuple[DeviceStats, str]]]


def api_target(device: Device, box: SecretBox, timeout: float = 10.0) -> ApiTarget:
    user, password = device.login(box)
    return ApiTarget(host=device.ip_address, username=user, password=password,
                     port=device.api_ssl_port if device.use_api_ssl else device.api_port,
                     use_ssl=device.use_api_ssl, timeout=timeout)


def ssh_target(device: Device, box: SecretBox, timeout: float = 15.0) -> SshTarget:
    user, password = device.login(box)
    return SshTarget(host=device.ip_address, username=user, password=password, port=device.ssh_port,
                     known_fingerprint=device.ssh_host_key, timeout=timeout)


def can_poll(device: Device) -> bool:
    return (device.brand == Brand.MIKROTIK and device.use_api) or (
        device.brand == Brand.UBIQUITI and device.use_ssh)


async def default_collect(device: Device, box: SecretBox) -> tuple[DeviceStats, str]:
    """Returns the stats and the SSH host key fingerprint (empty for API reads)."""
    if device.brand == Brand.MIKROTIK:
        if not device.use_api:
            raise CollectorError("RouterOS API is turned off for this device.")
        return await asyncio.to_thread(mikrotik.collect, api_target(device, box)), ""
    if device.brand == Brand.UBIQUITI:
        if not device.use_ssh:
            raise CollectorError("SSH is turned off for this device.")
        stats, result = await ubiquiti.collect(ssh_target(device, box))
        return stats, result.fingerprint
    raise CollectorError(f"Reading data from {device.brand.title()} devices is not supported yet.")


async def read_inventory(device: Device, box: SecretBox) -> tuple[Inventory, str]:
    if device.brand == Brand.MIKROTIK:
        if not device.use_api:
            raise CollectorError("RouterOS API is turned off for this device.")
        return await asyncio.to_thread(mikrotik.read_inventory, api_target(device, box)), ""
    if device.brand == Brand.UBIQUITI:
        inv, result = await ubiquiti.read_inventory(ssh_target(device, box))
        return inv, result.fingerprint
    raise CollectorError(f"Import from {device.brand.title()} devices is not supported yet.")


def _rate(new: int | None, old: int | None, seconds: float) -> float | None:
    if new is None or old is None or seconds <= 0 or new < old:  # counter reset or reboot
        return None
    return (new - old) * 8 / seconds


def store_stats(db: Session, device: Device, stats: DeviceStats, now: datetime) -> None:
    """Write one poll result: current state on the device + time series samples."""
    for key in ("identity", "board_name", "cpu_pct", "mem_pct", "temperature", "voltage", "uptime_s",
                "signal_dbm", "noise_dbm", "ccq_pct"):
        setattr(device, key, getattr(stats, key))
    has_radio = bool(stats.peers) or device.brand == Brand.UBIQUITI or any(
        i.if_type in ("wlan", "wifi", "wifiwave2") for i in stats.interfaces)
    device.wireless_clients = len(stats.peers) if has_radio else None
    device.last_polled = now
    device.poll_error = ""
    if stats.model and not device.model:
        device.model = stats.model
    if stats.os_version:
        device.os_version = stats.os_version
    if stats.serial_number and not device.serial_number:
        device.serial_number = stats.serial_number

    existing = {i.name: i for i in device.interfaces}
    seen = set()
    for st in stats.interfaces:
        seen.add(st.name)
        row = existing.get(st.name)
        if row is None:
            row = DeviceInterface(name=st.name)
            device.interfaces.append(row)
            rx_bps = tx_bps = None
        else:
            seconds = (now - row.updated_at).total_seconds() if row.updated_at else 0
            rx_bps, tx_bps = _rate(st.rx_bytes, row.rx_bytes, seconds), _rate(st.tx_bytes, row.tx_bytes, seconds)
        row.if_type, row.mac_address, row.comment = st.if_type, st.mac_address, st.comment
        row.running, row.disabled = st.running, st.disabled
        row.rx_bytes, row.tx_bytes = st.rx_bytes, st.tx_bytes
        row.rx_bps, row.tx_bps = rx_bps, tx_bps
        row.updated_at = now
        if rx_bps is not None and tx_bps is not None and st.running:
            db.add(TrafficSample(device_id=device.id, interface=st.name, ts=now, rx_bps=rx_bps, tx_bps=tx_bps))
    for name, row in existing.items():
        if name not in seen:
            device.interfaces.remove(row)

    device.wireless_peers = [
        WirelessPeer(interface=p.interface, mac_address=p.mac_address, name=p.name, last_ip=p.last_ip,
                     signal_dbm=p.signal_dbm, ccq_pct=p.ccq_pct, tx_rate=p.tx_rate, rx_rate=p.rx_rate,
                     uptime_s=p.uptime_s, updated_at=now)
        for p in stats.peers
    ]

    if stats.neighbors:
        db.execute(delete(Neighbor).where(Neighbor.seen_by_id == device.id))
        for n in stats.neighbors:
            if n.ip_address or n.mac_address:
                db.add(Neighbor(seen_by_id=device.id, interface=n.interface, ip_address=n.ip_address,
                                mac_address=n.mac_address, identity=n.identity, platform=n.platform,
                                board=n.board, version=n.version, last_seen=now))

    db.add(MetricSample(device_id=device.id, ts=now, cpu_pct=stats.cpu_pct, mem_pct=stats.mem_pct,
                        temperature=stats.temperature, voltage=stats.voltage,
                        wireless_clients=device.wireless_clients, signal_dbm=stats.signal_dbm,
                        ccq_pct=stats.ccq_pct))


class StatsPoller:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        box: SecretBox,
        interval: int = 60,
        concurrency: int = 20,
        raw_retention_days: int = 7,
        rollup_retention_days: int = 365,
        collect: Collector = default_collect,
    ):
        self.session_factory = session_factory
        self.box = box
        self.interval = interval
        self.concurrency = concurrency
        self.collect = collect
        self.housekeeper = Housekeeper(session_factory, raw_retention_days, rollup_retention_days)
        self._task: asyncio.Task | None = None
        self.last_cycle_at: datetime | None = None
        self.last_cycle_seconds: float | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="stats-poller")

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
        await asyncio.sleep(5)  # let the first ping round set statuses
        while True:
            started = loop.time()
            try:
                await self.run_cycle()
                await asyncio.to_thread(self.housekeeper.run_if_due)
            except Exception:
                log.exception("Stats poll cycle failed")
            await asyncio.sleep(max(5.0, self.interval - (loop.time() - started)))

    async def run_cycle(self) -> int:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.session_factory() as db:
            ids = [d.id for d in db.scalars(select(Device).where(Device.monitoring_enabled,
                                                                  Device.status.in_(POLLABLE_STATUSES)))
                   if can_poll(d) and d.login(self.box)[0]]
        sem = asyncio.Semaphore(self.concurrency)

        async def one(dev_id: int):
            async with sem:
                await self.poll_device(dev_id)

        await asyncio.gather(*(one(i) for i in ids))
        self.last_cycle_at = utcnow()
        self.last_cycle_seconds = loop.time() - started
        return len(ids)

    async def poll_device(self, device_id: int) -> str:
        """Poll one device now. Returns an error message, or "" on success."""
        with self.session_factory() as db:
            device = db.get(Device, device_id)
            if device is None:
                return "Device not found."
            try:
                stats, fingerprint = await self.collect(device, self.box)
            except CollectorError as exc:
                device.poll_error = str(exc)
                device.last_polled = utcnow()
                db.commit()
                return str(exc)
            except Exception as exc:
                log.exception("Polling %s failed", device.name)
                device.poll_error = f"Unexpected error: {exc}"
                db.commit()
                return device.poll_error
            if fingerprint and not device.ssh_host_key:
                device.ssh_host_key = fingerprint
            store_stats(db, device, stats, utcnow())
            db.commit()
            return ""
