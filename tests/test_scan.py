"""Scan network (like Winbox): MNDP / Ubiquiti discovery on the server's cards, Winbox by MAC."""
import socket
import struct
import threading

from sqlalchemy import select

from spike_monitor.db import SessionLocal
from spike_monitor.models import Customer, Device, Site
from spike_monitor.tools import scan as netscan


def mndp_packet(mac=b"\x4c\x5e\x0c\x11\x22\x33", identity=b"Tower-2 AP", ip=b"\x0a\x00\x05\x02"):
    def tlv(kind, value):
        return struct.pack(">HH", kind, len(value)) + value
    return (b"\x00\x00\x00\x01" + tlv(1, mac) + tlv(5, identity) + tlv(7, b"6.49.10 (long-term)")
            + tlv(8, b"MikroTik") + tlv(10, struct.pack("<I", 3661)) + tlv(12, b"RBSXTsq5nD") + tlv(16, b"wlan1")
            + tlv(17, ip))


def ubnt_packet():
    def tlv(kind, value):
        return struct.pack(">BH", kind, len(value)) + value
    body = (tlv(0x02, b"\x24\xa4\x3c\xaa\xbb\xcc" + b"\xa9\xfe\x01\x02") + tlv(0x02, b"\x24\xa4\x3c\xaa\xbb\xcc" + b"\x0a\x00\x06\x09")
            + tlv(0x03, b"XC.qca955x.v6.3.6") + tlv(0x0A, struct.pack(">I", 120)) + tlv(0x0B, b"Rocket-North")
            + tlv(0x0C, b"R5AC") + tlv(0x14, b"Rocket 5AC Prism"))
    return b"\x01\x00" + struct.pack(">H", len(body)) + body


def test_parse_mndp():
    f = netscan.parse_mndp(mndp_packet())
    assert (f.mac, f.identity, f.ip, f.board, f.version, f.uptime, f.interface, f.platform) == (
        "4C:5E:0C:11:22:33", "Tower-2 AP", "10.0.5.2", "RBSXTsq5nD", "6.49.10 (long-term)", 3661, "wlan1", "MikroTik")
    assert netscan.parse_mndp(netscan.MNDP_REQUEST) is None
    assert netscan.parse_mndp(b"\x00\x00\x00\x00\x00\x05\x00\x02ab") is None  # no MAC


def test_parse_ubnt_skips_link_local_ip():
    f = netscan.parse_ubnt(ubnt_packet())
    assert (f.mac, f.ip, f.identity, f.board, f.version, f.uptime) == (
        "24:A4:3C:AA:BB:CC", "10.0.6.9", "Rocket-North", "Rocket 5AC Prism", "XC.qca955x.v6.3.6", 120)
    assert netscan.parse_ubnt(netscan.UBNT_REQUEST) is None


def _responder(answer: bytes):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(5)

    def run():
        try:
            data, addr = s.recvfrom(100)
            s.sendto(b"garbage", addr)
            s.sendto(answer, addr)
        except OSError:
            pass
        finally:
            s.close()
    threading.Thread(target=run, daemon=True).start()
    return s.getsockname()[1]


def test_scan_collects_answers():
    mndp_port = _responder(mndp_packet(ip=b""))  # no IP in the answer: the sender's address is used
    ubnt_port = _responder(ubnt_packet())
    card = netscan.Card("Ethernet", "127.0.0.1", "255.0.0.0")
    found, problems = netscan.scan([card], seconds=1.5, mndp_port=mndp_port, ubnt_port=ubnt_port,
                                   targets=["127.0.0.1"])
    assert problems == []
    by_mac = {f.mac: f for f in found}
    assert by_mac["4C:5E:0C:11:22:33"].ip == "127.0.0.1"
    assert by_mac["4C:5E:0C:11:22:33"].card == "Ethernet (127.0.0.1/8)"
    assert by_mac["24:A4:3C:AA:BB:CC"].protocols == ["Ubiquiti"]


def test_list_cards_leaves_out_loopback():
    assert all(not c.ip.startswith("127.") for c in netscan.list_cards())


def test_scan_page_import_and_winbox_by_mac(app, admin, monkeypatch):
    with SessionLocal() as db:
        site = Site(name="Tower 2")
        known = Device(name="Core", ip_address="10.0.0.1", brand="mikrotik", mac_address="4C:5E:0C:00:00:01",
                       username="admin", password_enc=app.state.secret_box.encrypt("pw"))
        db.add_all([site, known])
        db.commit()
        site_id, known_id = site.id, known.id
    card = netscan.Card("Ethernet", "10.0.0.5", "255.255.255.0")
    monkeypatch.setattr(netscan, "list_cards", lambda: [card])
    seen = {}

    def fake_scan(cards, seconds, all_cards=None, subnets=None):
        seen["args"] = ([c.ip for c in cards], seconds)
        seen["subnets"] = subnets
        new = netscan.parse_mndp(mndp_packet())
        new.card = card.label
        old = netscan.Found(mac="4C:5E:0C:00:00:01", ip="10.0.0.1", identity="Core", protocols=["MNDP"])
        return [new, old], ["Ethernet: example problem"]
    monkeypatch.setattr(netscan, "scan", fake_scan)

    page = admin.get("/discovery/scan").text
    assert "Ethernet (10.0.0.5/24)" in page and "Found" not in page
    r = admin.post("/discovery/scan", data={"csrf_token": admin.token, "card": "10.0.0.5", "seconds": "99"})
    assert r.status_code == 200 and seen["args"] == (["10.0.0.5"], 30)
    assert [str(n) for n in seen["subnets"]] == ["10.0.0.0/24"]  # default: the card's own subnet
    page = r.text
    assert "Found 2 device(s)" in page and "Tower-2 AP" in page and "example problem" in page
    assert f'href="/devices/{known_id}"' in page  # already in the app: linked, not offered for import
    assert page.count('_sel" value="1"') == 1 and "its port wlan1" in page

    admin.post("/discovery/import", data={"csrf_token": admin.token, "nb_site": str(site_id),
                                          "nb_1_sel": "1", "nb_1_as": "device", "nb_1_name": "Tower-2 AP",
                                          "nb_1_ip": "10.0.5.2", "nb_1_mac": "4C:5E:0C:11:22:33",
                                          "nb_1_board": "RBSXTsq5nD", "nb_1_platform": "MikroTik",
                                          "nb_1_parent": "", "nb_1_interface": ""})
    with SessionLocal() as db:
        d = db.scalars(select(Device).where(Device.ip_address == "10.0.5.2")).one()
        assert (d.site_id, d.brand, d.uplink_port) == (site_id, "mikrotik", "")
        assert db.scalars(select(Customer)).first() is None
    assert admin.get("/discovery/scan").text.count('_sel" value="1"') == 0  # now in the app

    # Winbox by MAC: a new device (no login) and a known one (its login)
    page = admin.post("/launch/winbox-mac", data={"csrf_token": admin.token, "mac": "4c-5e-0c-11-22-99",
                                                  "name": "New AP"}).text
    token = page.split("token=", 1)[1].split('"', 1)[0]
    assert admin.get(f"/api/launch/{token}").json() == {"kind": "winbox", "name": "New AP", "host": "4C:5E:0C:11:22:99",
                                                        "port": None, "username": "admin", "password": ""}
    assert "Winbox (MAC)" in admin.get(f"/devices/{known_id}").text
    page = admin.post("/launch/winbox-mac", data={"csrf_token": admin.token, "mac": "4C:5E:0C:00:00:01",
                                                  "device_id": str(known_id)}).text
    token = page.split("token=", 1)[1].split('"', 1)[0]
    info = admin.get(f"/api/launch/{token}").json()
    assert (info["host"], info["port"], info["password"]) == ("4C:5E:0C:00:00:01", None, "pw")
    assert admin.post("/launch/winbox-mac", data={"csrf_token": admin.token, "mac": "nope"}).status_code == 400


def test_chosen_card_leaves_out_other_cards_networks(monkeypatch):
    """An announcement from the second card's subnet must not show up when only the first card is scanned."""
    lan = netscan.Card("LAN", "10.0.0.5", "255.255.255.0")
    wan = netscan.Card("WAN", "127.0.0.1", "255.0.0.0")
    port = _responder(mndp_packet())
    found, _ = netscan.scan([lan], seconds=1, mndp_port=port, targets=["127.0.0.1"], all_cards=[lan, wan])
    assert found == []
    port = _responder(mndp_packet())
    found, _ = netscan.scan([wan], seconds=1, mndp_port=port, targets=["127.0.0.1"], all_cards=[lan, wan])
    assert [f.card for f in found] == ["WAN (127.0.0.1/8)"]


def test_ip_calculator_shows_used_addresses(app, admin):
    from spike_monitor.models import DeviceInterface
    with SessionLocal() as db:
        core = Device(name="Core", ip_address="10.0.0.1", brand="mikrotik")
        db.add(core)
        db.flush()
        db.add(DeviceInterface(device_id=core.id, name="ether2", ip_addresses="10.0.0.2/24, 192.168.88.1/24"))
        db.add(Device(name="AP", ip_address="10.0.0.3"))
        db.commit()
        core_id = core.id
    page = admin.get("/tools", params={"ip": "10.0.0.2/24"}).text
    assert "10.0.0.2 is already in use" in page and f'href="/devices/{core_id}"' in page and "interface ether2" in page
    assert "First free address: <b>10.0.0.4</b>" in page and "3 address(es)" in page
    page = admin.get("/tools", params={"ip": "10.0.0.9/24"}).text
    assert "is not used by any device" in page and "10.0.0.4" in page
    page = admin.get("/tools", params={"ip": "10.0.0.1/32"}).text
    assert "no free address left" in page
    # and the device form refuses the duplicate IP
    from tests.test_changes_round1 import DEVICE_FORM
    r = admin.post("/devices/new", data={**DEVICE_FORM, "csrf_token": admin.token, "name": "Dup",
                                         "ip_address": "10.0.0.3"})
    assert "Another device already uses this IP address." in r.text


def test_subnet_filter():
    """One card on a Layer 2 network hears every subnet: only the chosen subnet is kept."""
    card = netscan.Card("LAN", "127.0.0.1", "255.0.0.0")
    port = _responder(mndp_packet(ip=b"\x0a\x00\x05\x02"))  # reports 10.0.5.2
    found, _ = netscan.scan([card], seconds=1, mndp_port=port, targets=["127.0.0.1"],
                            subnets=netscan.parse_subnets("192.168.88.0/24"))
    assert found == []
    port = _responder(mndp_packet(ip=b"\x0a\x00\x05\x02"))
    found, _ = netscan.scan([card], seconds=1, mndp_port=port, targets=["127.0.0.1"],
                            subnets=netscan.parse_subnets("192.168.88.0/24, 10.0.5.7/24"))
    assert [f.ip for f in found] == ["10.0.5.2"]
    port = _responder(mndp_packet(ip=b""))  # no IP in the answer: the sender's address counts
    found, _ = netscan.scan([card], seconds=1, mndp_port=port, targets=["127.0.0.1"],
                            subnets=netscan.parse_subnets("127.0.0.0/8"))
    assert [f.ip for f in found] == ["127.0.0.1"]
    import pytest
    with pytest.raises(ValueError):
        netscan.parse_subnets("10.0.5.0/24, nonsense")


def test_scan_subnet_choice_and_clear(app, admin, monkeypatch):
    card = netscan.Card("Ethernet", "10.0.0.5", "255.255.255.0")
    monkeypatch.setattr(netscan, "list_cards", lambda: [card])
    seen = {}

    def fake_scan(cards, seconds, all_cards=None, subnets=None):
        seen["subnets"] = subnets
        return [netscan.Found(mac="4C:5E:0C:11:22:33", ip="10.0.5.2", identity="AP", protocols=["MNDP"])], []
    monkeypatch.setattr(netscan, "scan", fake_scan)
    page = admin.post("/discovery/scan", data={"csrf_token": admin.token, "card": "", "scope": "custom",
                                               "subnets": "10.0.5.0/24 192.168.88.0/24"}).text
    assert [str(n) for n in seen["subnets"]] == ["10.0.5.0/24", "192.168.88.0/24"]
    assert "10.0.5.0/24, 192.168.88.0/24" in page and "Clear results" in page
    assert 'value="custom" selected' in page  # the choice is remembered
    admin.post("/discovery/scan", data={"csrf_token": admin.token, "scope": "all"})
    assert seen["subnets"] is None
    page = admin.post("/discovery/scan", data={"csrf_token": admin.token, "scope": "custom", "subnets": "bad"}).text
    assert "Not a valid IPv4 subnet" in page
    page = admin.post("/discovery/scan/clear", data={"csrf_token": admin.token}).text
    assert "Scan results cleared." in page and "Found" not in page


def test_banner_when_files_are_newer_than_the_server(app, admin, monkeypatch):
    from spike_monitor import deps
    assert "still running the old version" not in admin.get("/discovery/scan").text
    monkeypatch.setitem(deps._started, "mtime", 0.0)
    monkeypatch.setitem(deps._started, "checked", 0.0)
    assert "still running the old version" in admin.get("/discovery/scan").text
