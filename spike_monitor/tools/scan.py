"""Find devices on the server's own network cards, like the Neighbors tab in Winbox.

Two Layer 2 broadcast protocols are used, so devices are found even when their IP is in another
subnet (or they have no IP at all):

* MikroTik MNDP (UDP 5678): a 4-byte request makes every RouterOS device answer; they also
  announce themselves every minute. CDP/LLDP-only devices are not seen this way.
* Ubiquiti discovery (UDP 10001): airOS / UniFi devices answer a broadcast request.
"""
from __future__ import annotations

import ipaddress
import select
import socket
import struct
import time
from dataclasses import dataclass, field

MNDP_PORT = 5678
UBNT_PORT = 10001
MNDP_REQUEST = b"\x00\x00\x00\x00"
UBNT_REQUEST = b"\x01\x00\x00\x00"


@dataclass
class Card:
    """One IPv4 address on one of the server's network cards."""
    name: str
    ip: str
    netmask: str

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.ip}/{self.netmask}", strict=False)

    @property
    def broadcast(self) -> str:
        return str(self.network.broadcast_address)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.ip}/{self.network.prefixlen})"


@dataclass
class Found:
    mac: str = ""
    ip: str = ""
    identity: str = ""
    platform: str = ""
    board: str = ""
    version: str = ""
    uptime: int | None = None
    interface: str = ""  # the device's own port the answer came from (MNDP)
    card: str = ""  # the server's card that heard it
    protocols: list[str] = field(default_factory=list)


def list_cards() -> list[Card]:
    """IPv4 addresses of the server's network cards that are up (loopback and APIPA left out)."""
    cards: list[Card] = []
    try:
        import psutil

        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            for a in addrs:
                if a.family == socket.AF_INET and a.address and a.netmask:
                    cards.append(Card(name, a.address, a.netmask))
    except ImportError:  # pragma: no cover - psutil is in requirements.txt
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                cards.append(Card("Network card", info[4][0], "255.255.255.0"))
        except OSError:
            pass
    seen, out = set(), []
    for c in cards:
        ip = ipaddress.IPv4Address(c.ip)
        if ip.is_loopback or ip.is_link_local or c.ip in seen:
            continue
        seen.add(c.ip)
        out.append(c)
    return out


def _mac(raw: bytes) -> str:
    return ":".join(f"{b:02X}" for b in raw) if len(raw) == 6 else ""


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip("\x00").strip()


def parse_mndp(data: bytes) -> Found | None:
    """MNDP answer: 4-byte header, then TLVs (type: 2 bytes, length: 2 bytes, big endian)."""
    if len(data) < 8:
        return None
    f = Found(protocols=["MNDP"])
    pos = 4
    while pos + 4 <= len(data):
        kind, length = struct.unpack_from(">HH", data, pos)
        value = data[pos + 4:pos + 4 + length]
        pos += 4 + length
        if kind == 1:
            f.mac = _mac(value)
        elif kind == 5:
            f.identity = _text(value)
        elif kind == 7:
            f.version = _text(value)
        elif kind == 8:
            f.platform = _text(value)
        elif kind == 10 and len(value) == 4:
            f.uptime = struct.unpack("<I", value)[0]
        elif kind == 12:
            f.board = _text(value)
        elif kind == 16:
            f.interface = _text(value)
        elif kind == 17 and len(value) == 4:
            f.ip = socket.inet_ntoa(value)
    return f if f.mac else None


def parse_ubnt(data: bytes) -> Found | None:
    """Ubiquiti answer: version 1, command, 2-byte length, then TLVs (type: 1 byte, length: 2 bytes)."""
    if len(data) < 7 or data[0] != 1:  # a request is the same header with no TLVs
        return None
    f = Found(platform="Ubiquiti", protocols=["Ubiquiti"])
    end = min(len(data), 4 + struct.unpack_from(">H", data, 2)[0])
    pos = 4
    while pos + 3 <= end:
        kind = data[pos]
        length = struct.unpack_from(">H", data, pos + 1)[0]
        value = data[pos + 3:pos + 3 + length]
        pos += 3 + length
        if kind == 0x01 and not f.mac:
            f.mac = _mac(value)
        elif kind == 0x02 and len(value) == 10:
            f.mac = f.mac or _mac(value[:6])
            ip = socket.inet_ntoa(value[6:])
            if not f.ip and ip != "0.0.0.0" and not ip.startswith("169.254."):
                f.ip = ip
        elif kind == 0x03:
            f.version = _text(value)
        elif kind == 0x0A and len(value) == 4:
            f.uptime = struct.unpack(">I", value)[0]
        elif kind == 0x0B:
            f.identity = _text(value)
        elif kind == 0x0C and not f.board:
            f.board = _text(value)
        elif kind == 0x14:
            f.board = _text(value)  # full model name wins over the short code
    return f if f.mac else None


def _merge(found: dict[str, Found], new: Found) -> None:
    old = found.get(new.mac)
    if old is None:
        found[new.mac] = new
        return
    for name in ("ip", "identity", "platform", "board", "version", "interface", "card"):
        if not getattr(old, name):
            setattr(old, name, getattr(new, name))
    if old.uptime is None:
        old.uptime = new.uptime
    old.protocols += [p for p in new.protocols if p not in old.protocols]


def _socket(ip: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind((ip, port))
    s.setblocking(False)
    return s


def _owner(src: str, cards: list[Card]) -> Card | None:
    """The card whose subnet holds ``src`` (the most specific one)."""
    try:
        ip = ipaddress.IPv4Address(src)
    except ValueError:
        return None
    matches = [c for c in cards if ip in c.network]
    return max(matches, key=lambda c: c.network.prefixlen) if matches else None


def _in(address: str, subnets: list[ipaddress.IPv4Network]) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return not ip.is_unspecified and any(ip in n for n in subnets)


def parse_subnets(text: str) -> list[ipaddress.IPv4Network]:
    """'10.0.5.0/24, 192.168.88.0/24' (or one IP with its mask, e.g. 10.0.5.7/24)."""
    nets = []
    for part in text.replace(";", ",").replace(" ", ",").split(","):
        if part.strip():
            try:
                nets.append(ipaddress.IPv4Network(part.strip(), strict=False))
            except ValueError as exc:
                raise ValueError(f"Not a valid IPv4 subnet: {part.strip()} (example: 10.0.5.0/24)") from exc
    return nets


def scan(cards: list[Card], seconds: float = 5.0, mndp_port: int = MNDP_PORT, ubnt_port: int = UBNT_PORT,
         targets: list[str] | None = None, all_cards: list[Card] | None = None,
         subnets: list[ipaddress.IPv4Network] | None = None) -> tuple[list[Found], list[str]]:
    """Ask on ``cards`` and collect answers for ``seconds``. Returns (devices, problems).

    On a Layer 2 network one card hears devices of every subnet on the wire. With ``subnets``, only
    devices with an address in one of them are kept. Without it, an answer whose address is in the
    subnet of a card that was not chosen (from ``all_cards``) is left out, because broadcasts reach
    the listening sockets from every card. ``targets`` replaces the broadcast addresses (tests).
    """
    all_cards = all_cards or cards
    problems: list[str] = []
    socks: dict[socket.socket, tuple[str, Card | None]] = {}  # socket -> (protocol, card)
    for card in cards:
        dests = targets or sorted({card.broadcast, "255.255.255.255"})
        for proto, port, request in (("MNDP", mndp_port, MNDP_REQUEST), ("Ubiquiti", ubnt_port, UBNT_REQUEST)):
            try:
                # MNDP answers and announcements go to port 5678; take it on this card if we can.
                try:
                    s = _socket(card.ip, mndp_port if proto == "MNDP" else 0)
                except OSError:
                    s = _socket(card.ip, 0)
            except OSError as exc:
                problems.append(f"{card.label}: cannot use this card ({exc}).")
                break
            socks[s] = (proto, card)
            for dest in dests:
                try:
                    s.sendto(request, (dest, port))
                except OSError as exc:
                    problems.append(f"{card.label}: could not send to {dest} ({exc}).")
    # Linux only delivers broadcasts to sockets bound to the wildcard address.
    try:
        socks[_socket("", mndp_port)] = ("MNDP", None)
    except OSError:
        pass

    found: dict[str, Found] = {}
    deadline = time.monotonic() + seconds
    try:
        while socks and (left := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select(list(socks), [], [], min(left, 0.5))
            for s in ready:
                proto, card = socks[s]
                try:
                    while True:
                        data, (src, _) = s.recvfrom(4096)
                        if data in (MNDP_REQUEST, UBNT_REQUEST):
                            continue  # our own request (or another Winbox) echoed back
                        f = parse_mndp(data) if proto == "MNDP" else parse_ubnt(data)
                        if f is None:
                            continue
                        f.ip = f.ip or src
                        owner = _owner(src, all_cards)
                        if subnets is not None:
                            if not any(_in(a, subnets) for a in (f.ip, src)):
                                continue  # outside the chosen subnet(s)
                            if f.ip not in ("", "0.0.0.0") and not _in(f.ip, subnets):
                                f.ip = src  # show the address that matched
                        elif owner is not None and owner not in cards:
                            continue  # heard from another card's network
                        if owner is None and card is None and len(cards) < len(all_cards):
                            continue  # broadcast from an unknown subnet: cannot tell which card heard it
                        heard = owner or card
                        f.card = heard.label if heard else ""
                        _merge(found, f)
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError:
                    pass  # e.g. Windows "connection reset" after an ICMP port unreachable
    finally:
        for s in socks:
            s.close()
    return sorted(found.values(), key=lambda f: (f.identity.lower() or "~", f.mac)), problems
