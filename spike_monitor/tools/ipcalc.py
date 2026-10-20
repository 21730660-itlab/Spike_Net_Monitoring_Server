"""IP / subnet calculator and VLSM planner (IPv4 and IPv6)."""
from __future__ import annotations

import ipaddress
import math


class CalcError(ValueError):
    pass


def parse_interface(text: str) -> ipaddress.IPv4Interface | ipaddress.IPv6Interface:
    """'192.168.1.10/24', '192.168.1.10 255.255.255.0', '10.0.0.0/8', '2001:db8::1/64' or a bare address."""
    text = " ".join(text.strip().split())
    if not text:
        raise CalcError("Enter an address, e.g. 192.168.1.10/24")
    if " " in text:
        addr, mask = text.split(" ", 1)
        text = f"{addr}/{mask}"
    try:
        return ipaddress.ip_interface(text)
    except ValueError as exc:
        raise CalcError(f"Not a valid address or mask: {text}") from exc


def calculate(text: str) -> dict:
    iface = parse_interface(text)
    net = iface.network
    v4 = net.version == 4
    total = net.num_addresses
    if v4 and net.prefixlen <= 30:
        first, last, usable = net.network_address + 1, net.broadcast_address - 1, total - 2
    else:  # /31 and /32 (and IPv6) use every address
        first, last, usable = net.network_address, net.broadcast_address, total
    result = {
        "address": str(iface.ip),
        "version": net.version,
        "network": str(net),
        "prefix": net.prefixlen,
        "netmask": str(net.netmask),
        "wildcard": str(net.hostmask),
        "network_address": str(net.network_address),
        "broadcast": str(net.broadcast_address) if v4 else "—",
        "first_host": str(first),
        "last_host": str(last),
        "total": f"{total:,}",
        "usable": f"{usable:,}",
        "private": net.is_private,
        "ip_class": _class(net) if v4 else "",
        "binary_mask": ".".join(f"{int(o):08b}" for o in str(net.netmask).split(".")) if v4 else "",
    }
    return result


def _class(net: ipaddress.IPv4Network) -> str:
    first = int(str(net.network_address).split(".")[0])
    if first < 128:
        return "A"
    if first < 192:
        return "B"
    if first < 224:
        return "C"
    return "D (multicast)" if first < 240 else "E (reserved)"


def split(base: str, new_prefix: int, limit: int = 256) -> list[str]:
    net = parse_interface(base).network
    if not net.prefixlen < new_prefix <= net.max_prefixlen:
        raise CalcError(f"The new prefix must be between /{net.prefixlen + 1} and /{net.max_prefixlen}.")
    subnets = []
    for i, sub in enumerate(net.subnets(new_prefix=new_prefix)):
        if i >= limit:
            break
        subnets.append(str(sub))
    return subnets


def vlsm(base: str, needs: list[tuple[str, int]]) -> list[dict]:
    """Allocate subnets for (name, hosts) needs, biggest first, inside ``base`` (IPv4)."""
    net = parse_interface(base).network
    if net.version != 4:
        raise CalcError("VLSM planning works with IPv4 networks.")
    pointer = int(net.network_address)
    end = int(net.broadcast_address)
    plan = []
    for name, hosts in sorted(needs, key=lambda x: -x[1]):
        if hosts < 1:
            raise CalcError(f"{name}: hosts must be at least 1.")
        prefix = 32 - max(2, math.ceil(math.log2(hosts + 2)))
        size = 2 ** (32 - prefix)
        pointer = (pointer + size - 1) // size * size  # align to the block size
        if pointer + size - 1 > end:
            raise CalcError(f"Not enough room in {net} for {name} ({hosts} hosts).")
        sub = ipaddress.ip_network((pointer, prefix))
        plan.append({"name": name, "hosts": hosts, "subnet": str(sub), "mask": str(sub.netmask),
                     "first": str(sub.network_address + 1), "last": str(sub.broadcast_address - 1),
                     "usable": size - 2})
        pointer += size
    return plan


def parse_needs(text: str) -> list[tuple[str, int]]:
    """'Tower1 50, Tower2 20, mgmt 10' or one per line; names are optional."""
    needs = []
    for i, part in enumerate(x for x in text.replace("\n", ",").split(",") if x.strip()):
        bits = part.split()
        try:
            hosts = int(bits[-1])
        except ValueError:
            raise CalcError(f"'{part.strip()}': end each entry with the number of hosts, e.g. 'Tower1 50'.")
        needs.append((" ".join(bits[:-1]) or f"Subnet {i + 1}", hosts))
    if not needs:
        raise CalcError("Enter at least one subnet, e.g. 'Tower1 50, Tower2 20'.")
    return needs
