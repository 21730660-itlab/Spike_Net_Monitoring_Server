"""Owner's change requests, round 2: port lists by type, wireless-only between sites,
link speed and interface IPs, choosing which neighbors to import."""
import asyncio

from sqlalchemy import select

from spike_monitor.collectors.common import interface_kind
from spike_monitor.db import SessionLocal
from spike_monitor.models import Customer, Device, DeviceInterface, Neighbor, Site, Status, WirelessPeer
from spike_monitor.monitoring.poller import StatsPoller

from .fake_routeros import FakeRouterOS

FORM = {"brand": "mikrotik", "device_type": "router", "winbox_port": "8291", "ssh_port": "22", "api_port": "8728",
        "api_ssl_port": "8729", "http_port": "80", "https_port": "443", "latency_warn_ms": "100",
        "loss_warn_pct": "20", "monitoring_enabled": "on"}


def setup_pair(same_site=True):
    with SessionLocal() as db:
        s1, s2 = Site(name="POP"), Site(name="Tower")
        db.add_all([s1, s2])
        parent = Device(name="Core", ip_address="10.0.0.1", site=s1, status=Status.UP)
        parent.interfaces = [DeviceInterface(name="ether3", if_type="ether", link_speed="1Gbps", ip_addresses="10.0.0.1/24"),
                             DeviceInterface(name="wlan1", if_type="wlan", ip_addresses="10.9.0.1/24"),
                             DeviceInterface(name="bridge", if_type="bridge")]
        db.add(parent)
        db.commit()
        return parent.id, s1.id, (s1.id if same_site else s2.id)


def test_interface_kinds():
    assert interface_kind("ether1", "ether") == "wired"
    assert interface_kind("wlan1", "wlan") == interface_kind("wifi1", "wifi") == interface_kind("ath0") == "wireless"
    assert interface_kind("sfp-sfpplus1", "ether") == "fiber"
    assert interface_kind("bridge", "bridge") == interface_kind("pppoe-out1", "pppoe-out") == interface_kind("wg1", "wg") == ""


def test_port_lists_carry_type_speed_and_ip(app, admin):
    parent_id, _, _ = setup_pair()
    data = admin.get(f"/api/devices/{parent_id}/interfaces").json()["interfaces"]
    by = {i["name"]: i for i in data}
    assert by["ether3"]["kind"] == "wired" and by["ether3"]["speed"] == "1Gbps" and by["wlan1"]["kind"] == "wireless"
    assert [i["name"] for i in data][-1] == "bridge"  # logical interfaces last
    with SessionLocal() as db:
        child = Device(name="PTP", ip_address="10.0.0.2", parent_id=parent_id, status=Status.UP)
        db.add(child)
        db.commit()
        child_id = child.id
    form = admin.get(f"/devices/{child_id}/edit").text
    assert 'data-port-select' in form and 'data-kind="wireless"' in form and "ether3 (ether · 1Gbps · 10.0.0.1/24)" in form
    assert "Other (type it)" in form


def test_different_sites_must_be_wireless(app, admin):
    parent_id, _, other_site = setup_pair(same_site=False)
    base = {**FORM, "csrf_token": admin.token, "name": "PTP", "ip_address": "10.0.0.2", "parent_id": str(parent_id),
            "site_id": str(other_site)}
    r = admin.post("/devices/new", data={**base, "uplink_type": "wired", "uplink_port": "ether3"})
    assert r.status_code == 422 and "different sites can only be connected wireless" in r.text
    r = admin.post("/devices/new", data={**base, "uplink_type": "fiber"})
    assert r.status_code == 422
    r = admin.post("/devices/new", data={**base, "uplink_type": "wireless", "uplink_port": "wlan1"}, follow_redirects=False)
    assert r.status_code == 303


def test_port_must_match_connection_type_and_other(app, admin):
    parent_id, site, _ = setup_pair()
    base = {**FORM, "csrf_token": admin.token, "name": "AP", "ip_address": "10.0.0.3", "parent_id": str(parent_id),
            "site_id": str(site)}
    r = admin.post("/devices/new", data={**base, "uplink_type": "wired", "uplink_port": "wlan1"})
    assert r.status_code == 422 and "wlan1 is a wireless interface" in r.text
    r = admin.post("/devices/new", data={**base, "uplink_type": "wireless", "uplink_port": "ether3"})
    assert r.status_code == 422 and "ether3 is a wired port" in r.text
    # A port the app doesn't know yet can be typed with "Other"
    r = admin.post("/devices/new", data={**base, "uplink_type": "wired", "uplink_port": "__other__",
                                         "uplink_port_other": "ether7", "local_port": "__other__",
                                         "local_port_other": "ether1"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        ap = db.scalars(select(Device).where(Device.name == "AP")).one()
        assert (ap.uplink_port, ap.local_port) == ("ether7", "ether1")


def test_speed_and_ips_read_from_routeros_and_shown(app, admin):
    data = dict(FakeRouterOS().data)
    data["/ip/address/print"] = [{"address": "10.0.0.1/24", "interface": "ether1"},
                                 {"address": "10.9.0.1/24", "interface": "wlan1"}]
    data["/interface/ethernet/monitor"] = [{"name": "ether1", "status": "link-ok", "rate": "1Gbps"}]
    srv = FakeRouterOS(data=data)
    box = app.state.secret_box
    with SessionLocal() as db:
        core = Device(name="Core", ip_address="127.0.0.1", username="admin", password_enc=box.encrypt("secret"),
                      api_port=srv.port, status=Status.UP)
        wired = Device(name="Tower2", ip_address="10.0.0.9", parent=core, status=Status.UP)  # neighbor on ether1
        cpe = Device(name="CPE", ip_address="10.10.0.50", mac_address="11:22:33:44:55:66", parent=core, status=Status.UP)
        db.add_all([core, wired, cpe])
        db.commit()
        ids = core.id, wired.id, cpe.id
    assert asyncio.run(StatsPoller(SessionLocal, box).poll_device(ids[0])) == ""
    with SessionLocal() as db:
        eth = db.scalars(select(DeviceInterface).where(DeviceInterface.name == "ether1")).one()
        assert (eth.link_speed, eth.ip_addresses) == ("1Gbps", "10.0.0.1/24")
        w, c = db.get(Device, ids[1]), db.get(Device, ids[2])
        assert w.link_info() == {"speed": "1Gbps", "parent_ip": "10.0.0.1/24", "local_ip": ""}
        assert c.link_info()["speed"] == "130Mbps / 117Mbps" and c.link_info()["parent_ip"] == "10.9.0.1/24"
    page = admin.get(f"/devices/{ids[1]}").text
    assert "⚡ Speed: 1Gbps" in page and "Parent port IP: 10.0.0.1/24" in page
    assert "1Gbps" in admin.get(f"/devices/{ids[0]}").text  # interfaces table
    dev = next(d for d in admin.get("/api/map").json()["devices"] if d["name"] == "CPE")
    assert dev["speed"] == "130Mbps / 117Mbps"


def test_unknown_speed_stays_blank(app):
    with SessionLocal() as db:
        p = Device(name="P", ip_address="10.0.0.1", status=Status.UP)
        c = Device(name="C", ip_address="10.0.0.2", parent=p, uplink_type="wired", uplink_port="ether2", status=Status.UP)
        db.add_all([p, c])
        db.commit()
        assert db.get(Device, c.id).link_info() == {"speed": "", "parent_ip": "", "local_ip": ""}


def test_cross_site_detection_is_wireless(app):
    srv = FakeRouterOS()  # sees neighbor 10.0.0.9 on ether1
    box = app.state.secret_box
    with SessionLocal() as db:
        a, b = Site(name="A"), Site(name="B")
        core = Device(name="Core", ip_address="127.0.0.1", site=a, username="admin", password_enc=box.encrypt("secret"),
                      api_port=srv.port, status=Status.UP)
        far = Device(name="Far", ip_address="10.0.0.9", site=b, parent=core, status=Status.UP)
        db.add_all([a, b, core, far])
        db.commit()
        ids = core.id, far.id
    asyncio.run(StatsPoller(SessionLocal, box).poll_device(ids[0]))
    with SessionLocal() as db:
        assert db.get(Device, ids[1]).uplink_type == "wireless"


def test_choose_which_neighbors_to_import(app, admin):
    with SessionLocal() as db:
        site = Site(name="Tower")
        ap = Device(name="AP", ip_address="10.0.0.1", site=site, status=Status.UP)
        db.add_all([site, ap])
        db.flush()
        db.add_all([Neighbor(seen_by_id=ap.id, interface="wlan1", identity="cs abdhalim manra",
                             mac_address="08:55:31:47:22:3B", platform="MikroTik", board="RBSXTsq5nD"),
                    Neighbor(seen_by_id=ap.id, interface="ether2", identity="Switch", ip_address="10.0.0.20",
                             mac_address="AA:AA:AA:AA:AA:20", platform="MikroTik", board="CRS326"),
                    Neighbor(seen_by_id=ap.id, interface="wlan1", identity="ma_adel", mac_address="08:55:31:C2:E3:4C")])
        db.commit()
        ap_id, site_id = ap.id, site.id
    page = admin.get("/discovery").text
    assert page.count("value=\"1\" data-nb-sel>") == 3 and "Import selected" in page
    rows = {}
    for i in (1, 2, 3):
        for f in ("name", "ip", "mac", "board", "platform", "version", "parent", "interface", "as"):
            marker = f'name="nb_{i}_{f}" value="'
            if marker in page:
                rows[f"nb_{i}_{f}"] = page.split(marker, 1)[1].split('"', 1)[0]
    names = {i: rows[f"nb_{i}_name"] for i in (1, 2, 3)}
    idx = {v: k for k, v in names.items()}
    a, s, m = idx["cs abdhalim manra"], idx["Switch"], idx["ma_adel"]
    data = {"csrf_token": admin.token, **rows,
            f"nb_{a}_sel": "1", f"nb_{a}_as": "customer",
            f"nb_{s}_sel": "1", f"nb_{s}_as": "device",
            f"nb_{m}_as": "customer"}  # ma_adel not ticked: nothing happens
    r = admin.post("/discovery/import", data=data)
    assert "Imported 1 device(s) and 1 customer(s)." in r.text
    with SessionLocal() as db:
        cust = db.scalars(select(Customer)).one()
        assert (cust.name, cust.mac_address, cust.access_point_id, cust.site_id) == (
            "cs abdhalim manra", "08:55:31:47:22:3B", ap_id, site_id)
        sw = db.scalars(select(Device).where(Device.name == "Switch")).one()
        assert (sw.parent_id, sw.site_id, sw.uplink_port, sw.model, sw.brand) == (ap_id, site_id, "ether2", "CRS326", "mikrotik")
    page = admin.get("/discovery").text  # imported ones disappear from the list
    assert "cs abdhalim manra" not in page and 'value="Switch"' not in page and "ma_adel" in page

    # A device needs an IP
    r = admin.post("/discovery/import", data={"csrf_token": admin.token, "nb_1_sel": "1", "nb_1_as": "device",
                                              "nb_1_name": "ma_adel", "nb_1_mac": "08:55:31:C2:E3:4C",
                                              "nb_1_parent": str(ap_id)})
    assert "needs an IP address" in r.text


def test_import_preview_neighbors_are_optional(app, admin, monkeypatch):
    from spike_monitor.collectors.common import Inventory, NeighborStat
    with SessionLocal() as db:
        d = Device(name="AP", ip_address="10.0.0.1", status=Status.UP)
        db.add(d)
        db.commit()
        dev_id = d.id

    async def fake_inventory(device, box):
        return Inventory(identity="AP", neighbors=[NeighborStat("wlan1", "", "08:55:31:70:1E:21", "cs hikmat mamyi",
                                                                "MikroTik", "RBSXTsq5nD")]), ""

    monkeypatch.setattr("spike_monitor.routers.device_actions.read_inventory", fake_inventory)
    page = admin.get(f"/devices/{dev_id}/import").text
    assert "cs hikmat mamyi" in page and 'name="nb_1_sel"' in page
    # Import without ticking: no customer
    admin.post(f"/devices/{dev_id}/import", data={"csrf_token": admin.token, "nb_1_as": "customer",
                                                 "nb_1_name": "cs hikmat mamyi", "nb_1_mac": "08:55:31:70:1E:21",
                                                 "nb_1_parent": str(dev_id)})
    with SessionLocal() as db:
        assert db.scalars(select(Customer)).first() is None
    r = admin.post(f"/devices/{dev_id}/import", data={"csrf_token": admin.token, "nb_1_sel": "1", "nb_1_as": "customer",
                                                     "nb_1_name": "cs hikmat mamyi", "nb_1_mac": "08:55:31:70:1E:21",
                                                     "nb_1_parent": str(dev_id)})
    assert "1 customer(s)" in r.text
    with SessionLocal() as db:
        assert db.scalars(select(Customer)).one().name == "cs hikmat mamyi"
