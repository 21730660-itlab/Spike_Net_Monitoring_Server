"""Shared data shapes and value parsers for device collectors."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class CollectorError(Exception):
    """A device could not be read; the message is shown to the user."""


@dataclass
class InterfaceStat:
    name: str
    if_type: str = ""
    mac_address: str = ""
    comment: str = ""
    running: bool = False
    disabled: bool = False
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_broadcast: int | None = None  # ethernet only; used for broadcast storm / loop detection
    rx_multicast: int | None = None


@dataclass
class PeerStat:
    mac_address: str
    interface: str = ""
    name: str = ""
    last_ip: str = ""
    signal_dbm: float | None = None
    noise_dbm: float | None = None
    ccq_pct: float | None = None
    tx_rate: str = ""
    rx_rate: str = ""
    uptime_s: int | None = None


@dataclass
class NeighborStat:
    interface: str = ""
    ip_address: str = ""
    mac_address: str = ""
    identity: str = ""
    platform: str = ""
    board: str = ""
    version: str = ""


@dataclass
class DeviceStats:
    identity: str = ""
    board_name: str = ""
    model: str = ""
    serial_number: str = ""
    os_version: str = ""
    cpu_pct: float | None = None
    mem_pct: float | None = None
    temperature: float | None = None
    voltage: float | None = None
    uptime_s: int | None = None
    signal_dbm: float | None = None
    noise_dbm: float | None = None
    ccq_pct: float | None = None
    interfaces: list[InterfaceStat] = field(default_factory=list)
    peers: list[PeerStat] = field(default_factory=list)
    neighbors: list[NeighborStat] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # raw values for brand-specific use

    def summarize_peers(self) -> None:
        """Fill device-level signal/noise/CCQ from peers when the device didn't report them."""
        def avg(values):
            values = [v for v in values if v is not None]
            return round(sum(values) / len(values), 1) if values else None

        if self.signal_dbm is None:
            self.signal_dbm = avg(p.signal_dbm for p in self.peers)
        if self.noise_dbm is None:
            self.noise_dbm = avg(p.noise_dbm for p in self.peers)
        if self.ccq_pct is None:
            self.ccq_pct = avg(p.ccq_pct for p in self.peers)


@dataclass
class Inventory:
    """Configuration read by "Import from device"."""

    identity: str = ""
    model: str = ""
    serial_number: str = ""
    os_version: str = ""
    board_name: str = ""
    mac_address: str = ""
    interfaces: list[InterfaceStat] = field(default_factory=list)
    ip_addresses: list[dict] = field(default_factory=list)  # {address, interface}
    vlans: list[dict] = field(default_factory=list)  # {name, vlan_id, interface}
    bridges: list[str] = field(default_factory=list)
    wireless: list[dict] = field(default_factory=list)  # {name, ssid, frequency, mode, band}
    neighbors: list[NeighborStat] = field(default_factory=list)


def to_float(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def to_int(value) -> int | None:
    f = to_float(value)
    return int(f) if f is not None else None


_DUR_RE = re.compile(r"(\d+)([wdhms])")
_DUR_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(value) -> int | None:
    """RouterOS durations: '1w2d3h4m5s', '2d03:04:05', '03:04:05' or plain seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    total = 0
    clock = re.search(r"(\d+):(\d+):(\d+)", text)
    if clock:
        h, m, s = (int(x) for x in clock.groups())
        total += h * 3600 + m * 60 + s
        text = text[: clock.start()]
    found = False
    for num, unit in _DUR_RE.findall(re.sub(r"ms$", "", text)):
        total += int(num) * _DUR_UNITS[unit]
        found = True
    return total if (found or clock) else None


def parse_signal(value) -> float | None:
    """'-62dBm@6Mbps', '-62@HT20-7', -62 → -62.0"""
    if value is None or value == "":
        return None
    return to_float(str(value).split("@")[0])


def normalize_mac(value) -> str:
    return str(value or "").replace("-", ":").upper()


def format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


def format_bps(bps: float | None) -> str:
    if bps is None:
        return "—"
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if abs(bps) < 1000:
            return f"{bps:.1f} {unit}" if unit != "bps" else f"{bps:.0f} bps"
        bps /= 1000
    return f"{bps:.1f} Tbps"
