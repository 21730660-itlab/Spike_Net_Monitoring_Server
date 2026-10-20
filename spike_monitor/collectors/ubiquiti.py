"""Ubiquiti airOS (v6 and v8) reader over SSH using ``mca-status`` and ``wstalist``."""
from __future__ import annotations

import json

from .common import DeviceStats, InterfaceStat, Inventory, PeerStat, normalize_mac, to_float, to_int
from .ssh import SshResult, SshTarget, run_command

SEPARATOR = "-----SPIKE-WSTALIST-----"
COMMAND = f"mca-status; echo '{SEPARATOR}'; wstalist 2>/dev/null"


def parse_mca_status(text: str) -> dict[str, str]:
    """``mca-status`` prints one comma separated line, then key=value lines."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split(",") if line.count("=") > 1 and "," in line else [line]
        for part in parts:
            if "=" in part:
                key, _, value = part.partition("=")
                values[key.strip()] = value.strip()
    return values


def parse_wstalist(text: str) -> list[PeerStat]:
    try:
        rows = json.loads(text.strip() or "[]")
    except json.JSONDecodeError:
        return []
    peers = []
    for r in rows if isinstance(rows, list) else []:
        remote = r.get("remote") or {}
        signal = to_float(r.get("signal"))
        noise = to_float(r.get("noisefloor"))
        ccq = to_float(r.get("ccq"))
        if ccq is None and isinstance(r.get("airmax"), dict):
            ccq = to_float(r["airmax"].get("quality"))
        peers.append(PeerStat(
            mac_address=normalize_mac(r.get("mac")),
            name=str(r.get("name") or remote.get("hostname") or ""),
            last_ip=str(r.get("lastip", "")),
            signal_dbm=signal,
            noise_dbm=noise,
            ccq_pct=ccq,
            tx_rate=str(r.get("tx") or r.get("tx_rate") or ""),
            rx_rate=str(r.get("rx") or r.get("rx_rate") or ""),
            uptime_s=to_int(r.get("uptime")),
        ))
    return peers


def parse_output(output: str) -> DeviceStats:
    status_text, _, sta_text = output.partition(SEPARATOR)
    v = parse_mca_status(status_text)

    cpu = to_float(v.get("cpuUsage"))
    if cpu is None and to_float(v.get("cpuTotal")):
        cpu = round(100 * (to_float(v.get("cpuBusy")) or 0) / to_float(v.get("cpuTotal")), 1)
    mem_total, mem_free = to_float(v.get("memTotal")), to_float(v.get("memFree"))
    interfaces = []
    for name in ("lan", "wlan"):
        rx, tx = to_int(v.get(f"{name}RxBytes")), to_int(v.get(f"{name}TxBytes"))
        if rx is not None or tx is not None:
            interfaces.append(InterfaceStat(name=name, if_type=name, running=True, rx_bytes=rx, tx_bytes=tx))

    stats = DeviceStats(
        identity=v.get("deviceName", ""),
        model=v.get("platform", ""),
        board_name=v.get("platform", ""),
        os_version=v.get("firmwareVersion", ""),
        cpu_pct=cpu,
        mem_pct=round(100 * (mem_total - mem_free) / mem_total, 1) if mem_total and mem_free is not None else None,
        uptime_s=to_int(v.get("uptime")),
        signal_dbm=to_float(v.get("signal")),
        noise_dbm=to_float(v.get("noise")),
        ccq_pct=to_float(v.get("ccq")),
        interfaces=interfaces,
        peers=parse_wstalist(sta_text),
    )
    # airOS reports ccq in tenths of a percent on some firmware (e.g. 954 = 95.4%).
    if stats.ccq_pct is not None and stats.ccq_pct > 100:
        stats.ccq_pct = round(stats.ccq_pct / 10, 1)
    stats.summarize_peers()
    stats.extra = v
    return stats


async def collect(target: SshTarget) -> tuple[DeviceStats, SshResult]:
    result = await run_command(target, COMMAND, command_timeout=30)
    return parse_output(result.output), result


async def read_inventory(target: SshTarget) -> tuple[Inventory, SshResult]:
    stats, result = await collect(target)
    v = stats.extra
    inv = Inventory(
        identity=stats.identity,
        model=stats.model,
        os_version=stats.os_version,
        board_name=stats.board_name,
        mac_address=normalize_mac(v.get("deviceId", "")),
        interfaces=stats.interfaces,
        wireless=[{"name": "wlan0", "ssid": v.get("essid", ""), "frequency": v.get("freq", ""),
                   "mode": v.get("wlanOpmode", ""), "band": ""}] if v.get("essid") else [],
    )
    return inv, result
