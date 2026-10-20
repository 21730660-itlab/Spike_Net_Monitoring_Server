"""Traceroute with icmplib (raw ICMP) and a fallback to Windows tracert / Linux traceroute."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class Hop:
    distance: int
    address: str
    avg_ms: float | None
    loss_pct: float


async def traceroute(host: str, max_hops: int = 30) -> tuple[list[Hop], str]:
    """Returns (hops, raw text). One of them may be empty depending on the method used."""
    try:
        from icmplib import traceroute as icmp_trace

        privileged = sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0)
        hops = await asyncio.to_thread(icmp_trace, host, count=2, interval=0.05, timeout=1, max_hops=max_hops,
                                       fast=True, privileged=privileged)
        return [Hop(h.distance, h.address, h.avg_rtt, h.packet_loss * 100) for h in hops], ""
    except Exception:
        pass
    if sys.platform == "win32":
        cmd = ["tracert", "-d", "-w", "1000", "-h", str(max_hops), host]
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    else:
        cmd = ["traceroute", "-n", "-w", "1", "-m", str(max_hops), host]
        kwargs = {}
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, **kwargs)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=max_hops * 4 + 10)
        return [], out.decode(errors="replace")
    except (OSError, asyncio.TimeoutError) as exc:
        return [], f"Traceroute is not available on this server: {exc}"
