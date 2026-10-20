"""MikroTik RouterOS API reader (works with RouterOS v6 and v7).

librouteros is synchronous; callers run these functions in a worker thread.
"""
from __future__ import annotations

import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass

from .common import (
    CollectorError,
    DeviceStats,
    InterfaceStat,
    Inventory,
    NeighborStat,
    PeerStat,
    normalize_mac,
    parse_duration,
    parse_signal,
    to_float,
    to_int,
)


@dataclass
class ApiTarget:
    host: str
    username: str
    password: str
    port: int = 8728
    use_ssl: bool = False
    timeout: float = 10.0


def _ssl_wrapper() -> Callable:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        # RouterOS API-SSL without a certificate uses anonymous Diffie-Hellman.
        ctx.set_ciphers("DEFAULT:ADH:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    return ctx.wrap_socket


def connect(target: ApiTarget):
    """Log in, trying the modern method first and the pre-6.43 method second."""
    import librouteros
    from librouteros.exceptions import LibRouterosError, MultiTrapError, TrapError
    from librouteros.login import plain, token

    if not target.username:
        raise CollectorError("No username set for this device (add a login or a credential profile).")

    last_error: Exception | None = None
    for method in (plain, token):
        try:
            api = librouteros.connect(
                target.host,
                target.username,
                target.password,
                port=target.port,
                timeout=target.timeout,
                encoding="latin-1",
                login_method=method,
                ssl_wrapper=_ssl_wrapper() if target.use_ssl else None,
            )
        except TrapError as exc:
            msg = str(exc).lower()
            if "invalid user" in msg or "cannot log in" in msg or "password" in msg:
                raise CollectorError("API login failed: wrong username or password.") from exc
            last_error = exc
            continue
        except (socket.timeout, TimeoutError) as exc:
            raise CollectorError(f"API port {target.port} timed out. Is the API service enabled and the port right?") from exc
        except ConnectionRefusedError as exc:
            raise CollectorError(f"API port {target.port} refused the connection. Enable /ip service api"
                                 f"{'-ssl' if target.use_ssl else ''} or check the port.") from exc
        except (OSError, LibRouterosError) as exc:
            last_error = exc
            continue
        try:
            tuple(api.path("system", "identity"))
            return api
        except (TrapError, MultiTrapError) as exc:  # plain login on RouterOS < 6.43 is silently ignored
            last_error = exc
            api.close()
    raise CollectorError(f"API connection failed: {last_error}")


def _rows(api, *path: str) -> list[dict]:
    """Read a menu; menus missing on this board / RouterOS version / package return [].

    RouterOS reports a missing menu with two traps ("no such command or directory"
    and "no such command prefix"), which librouteros raises as MultiTrapError.
    """
    from librouteros.exceptions import MultiTrapError, TrapError

    try:
        return [dict(r) for r in api.path(*path)]
    except (TrapError, MultiTrapError):
        return []


def _first(api, *path: str) -> dict:
    rows = _rows(api, *path)
    return rows[0] if rows else {}


def parse_health(rows: list[dict]) -> tuple[float | None, float | None]:
    """Temperature and voltage from /system/health (v6: one row of fields, v7: name/value rows)."""
    temperature = voltage = None
    by_name = {str(r.get("name")): r.get("value") for r in rows if "name" in r and "value" in r}
    if by_name:
        for key in ("temperature", "cpu-temperature", "board-temperature1", "board-temperature"):
            if key in by_name:
                temperature = to_float(by_name[key])
                break
        voltage = to_float(by_name.get("voltage"))
    elif rows:
        row = rows[0]
        temperature = to_float(row.get("temperature", row.get("cpu-temperature")))
        voltage = to_float(row.get("voltage"))
    return temperature, voltage


def parse_resource(row: dict) -> dict:
    total = to_float(row.get("total-memory"))
    free = to_float(row.get("free-memory"))
    return {
        "cpu_pct": to_float(row.get("cpu-load")),
        "mem_pct": round(100 * (total - free) / total, 1) if total and free is not None else None,
        "uptime_s": parse_duration(row.get("uptime")),
        "os_version": str(row.get("version", "")).split(" ")[0],
        "board_name": str(row.get("board-name", "")),
    }


def parse_interfaces(rows: list[dict]) -> list[InterfaceStat]:
    return [
        InterfaceStat(
            name=str(r.get("name", "")),
            if_type=str(r.get("type", "")),
            mac_address=normalize_mac(r.get("mac-address")),
            comment=str(r.get("comment", "")),
            running=bool(r.get("running", False)),
            disabled=bool(r.get("disabled", False)),
            rx_bytes=to_int(r.get("rx-byte")),
            tx_bytes=to_int(r.get("tx-byte")),
        )
        for r in rows
        if r.get("name")
    ]


def parse_registrations(rows: list[dict]) -> list[PeerStat]:
    """Legacy wireless (/interface/wireless) and v7 wifi/wifiwave2 registration tables."""
    peers = []
    for r in rows:
        signal = parse_signal(r.get("signal-strength", r.get("signal")))
        snr = to_float(r.get("signal-to-noise"))
        peers.append(PeerStat(
            mac_address=normalize_mac(r.get("mac-address")),
            interface=str(r.get("interface", "")),
            name=str(r.get("radio-name", r.get("ssid", ""))),
            last_ip=str(r.get("last-ip", "")),
            signal_dbm=signal,
            noise_dbm=(signal - snr) if signal is not None and snr is not None else None,
            ccq_pct=to_float(r.get("tx-ccq")),
            tx_rate=str(r.get("tx-rate", "")),
            rx_rate=str(r.get("rx-rate", "")),
            uptime_s=parse_duration(r.get("uptime")),
        ))
    return peers


def parse_neighbors(rows: list[dict]) -> list[NeighborStat]:
    return [
        NeighborStat(
            interface=str(r.get("interface", "")).split(",")[0],
            ip_address=str(r.get("address", r.get("address4", ""))),
            mac_address=normalize_mac(r.get("mac-address")),
            identity=str(r.get("identity", "")),
            platform=str(r.get("platform", "")),
            board=str(r.get("board", "")),
            version=str(r.get("version", "")).split(" ")[0],
        )
        for r in rows
    ]


def _ethernet_counters(api) -> dict[str, tuple[int | None, int | None]]:
    """Broadcast / multicast receive counters per ethernet port (for loop detection)."""
    from librouteros.exceptions import LibRouterosError

    try:
        rows = [dict(r) for r in api.rawCmd("/interface/ethernet/print", "=stats=")]
    except (LibRouterosError, OSError):
        return {}
    return {str(r.get("name")): (to_int(r.get("rx-broadcast")), to_int(r.get("rx-multicast")))
            for r in rows if r.get("name")}


def _registrations(api) -> list[PeerStat]:
    rows = []
    for path in (("interface", "wireless", "registration-table"),
                 ("interface", "wifi", "registration-table"),
                 ("interface", "wifiwave2", "registration-table")):
        rows.extend(_rows(api, *path))
    return parse_registrations(rows)


def collect(target: ApiTarget, with_neighbors: bool = True) -> DeviceStats:
    api = connect(target)
    try:
        res = parse_resource(_first(api, "system", "resource"))
        board = _first(api, "system", "routerboard")
        temperature, voltage = parse_health(_rows(api, "system", "health"))
        stats = DeviceStats(
            identity=str(_first(api, "system", "identity").get("name", "")),
            board_name=res["board_name"],
            model=str(board.get("model", "")),
            serial_number=str(board.get("serial-number", "")),
            os_version=res["os_version"],
            cpu_pct=res["cpu_pct"],
            mem_pct=res["mem_pct"],
            uptime_s=res["uptime_s"],
            temperature=temperature,
            voltage=voltage,
            interfaces=parse_interfaces(_rows(api, "interface")),
            peers=_registrations(api),
            neighbors=parse_neighbors(_rows(api, "ip", "neighbor")) if with_neighbors else [],
        )
        counters = _ethernet_counters(api)
        for iface in stats.interfaces:
            if iface.name in counters:
                iface.rx_broadcast, iface.rx_multicast = counters[iface.name]
        stats.summarize_peers()
        return stats
    finally:
        api.close()


def read_inventory(target: ApiTarget) -> Inventory:
    api = connect(target)
    try:
        res = parse_resource(_first(api, "system", "resource"))
        board = _first(api, "system", "routerboard")
        interfaces = parse_interfaces(_rows(api, "interface"))
        ether = next((i for i in interfaces if i.if_type == "ether" and i.mac_address), None)
        wireless = [
            {"name": r.get("name", ""), "ssid": r.get("ssid", ""), "frequency": r.get("frequency", ""),
             "mode": r.get("mode", ""), "band": r.get("band", "")}
            for r in _rows(api, "interface", "wireless")
        ] + [
            {"name": r.get("name", ""), "ssid": r.get("configuration.ssid", r.get("ssid", "")),
             "frequency": r.get("channel.frequency", ""), "mode": r.get("configuration.mode", ""),
             "band": r.get("channel.band", "")}
            for r in _rows(api, "interface", "wifi")
        ]
        return Inventory(
            identity=str(_first(api, "system", "identity").get("name", "")),
            model=str(board.get("model", "")) or res["board_name"],
            serial_number=str(board.get("serial-number", "")),
            os_version=res["os_version"],
            board_name=res["board_name"],
            mac_address=ether.mac_address if ether else "",
            interfaces=interfaces,
            ip_addresses=[{"address": r.get("address", ""), "interface": r.get("interface", "")}
                          for r in _rows(api, "ip", "address")],
            vlans=[{"name": r.get("name", ""), "vlan_id": to_int(r.get("vlan-id")), "interface": r.get("interface", "")}
                   for r in _rows(api, "interface", "vlan")],
            bridges=[str(r.get("name", "")) for r in _rows(api, "interface", "bridge")],
            wireless=wireless,
            neighbors=parse_neighbors(_rows(api, "ip", "neighbor")),
        )
    finally:
        api.close()
