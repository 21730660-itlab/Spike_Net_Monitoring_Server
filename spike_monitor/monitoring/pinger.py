"""ICMP ping with a fallback to the system ``ping`` command.

Raw ICMP sockets need administrator rights on Windows. When the server runs
as a Windows service (LocalSystem) icmplib works directly; when it is started
by a normal user we fall back to ``ping.exe``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class PingOutcome:
    is_alive: bool
    avg_ms: float | None
    loss_pct: float


_use_system_ping = False


async def ping(host: str, count: int = 3, timeout: float = 1.0) -> PingOutcome:
    global _use_system_ping
    if not _use_system_ping:
        try:
            return await _icmplib_ping(host, count, timeout)
        except PermissionError as exc:
            log.warning("Raw ICMP not permitted (%s); falling back to the system ping command.", exc)
            _use_system_ping = True
        except Exception as exc:  # name resolution errors etc.
            log.debug("icmplib ping to %s failed: %s", host, exc)
            return PingOutcome(False, None, 100.0)
    return await system_ping(host, count, timeout)


async def _icmplib_ping(host: str, count: int, timeout: float) -> PingOutcome:
    from icmplib import async_ping
    from icmplib.exceptions import SocketPermissionError

    privileged = sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0)
    try:
        result = await async_ping(host, count=count, interval=0.2, timeout=timeout, privileged=privileged)
    except SocketPermissionError as exc:
        raise PermissionError(str(exc)) from exc
    return PingOutcome(result.is_alive, result.avg_rtt if result.is_alive else None, result.packet_loss * 100)


_TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


def parse_ping_output(output: str, count: int) -> PingOutcome:
    """Parse Windows or Linux ``ping`` output."""
    times = [float(t) for t in _TIME_RE.findall(output)]
    received = len(times)
    loss = 100.0 * (count - received) / count if count else 100.0
    loss = min(max(loss, 0.0), 100.0)
    avg = sum(times) / received if received else None
    return PingOutcome(received > 0, avg, loss)


async def system_ping(host: str, count: int, timeout: float) -> PingOutcome:
    if sys.platform == "win32":
        cmd = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), host]
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    else:
        cmd = ["ping", "-c", str(count), "-W", str(max(1, int(timeout))), "-i", "0.2", host]
        kwargs = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **kwargs
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=count * (timeout + 1) + 5)
    except (OSError, asyncio.TimeoutError) as exc:
        log.debug("system ping to %s failed: %s", host, exc)
        return PingOutcome(False, None, 100.0)
    return parse_ping_output(out.decode(errors="replace"), count)
