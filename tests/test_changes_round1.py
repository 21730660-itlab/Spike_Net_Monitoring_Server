"""Owner's change requests: link type/ports on the map, customer device by site, clear history."""
import asyncio
from datetime import timedelta

from sqlalchemy import select

from spike_monitor.db import SessionLocal
from spike_monitor.models import (
    Alert, AuditLog, CommandJob, CommandResult, Customer, Device, MetricSample, Neighbor, NotificationLog,
    PingResult, Site, Status, StatusEvent, utcnow,
)
from spike_monitor.monitoring.poller import StatsPoller

from .conftest import csrf, login
from .fake_routeros import FakeRouterOS

DEVICE_FORM = {"brand": "mikrotik", "device_type": "router", "winbox_port": "8291", "ssh_port": "22",
               "api_port": "8728", "api_ssl_port": "8729", "http_port": "80", "https_port": "443",
               "latency_warn_ms": "100", "loss_warn_pct": "20", "monitoring_enabled": "on"}


def add(db, **kw):
    d = Device(status=Status.UP, **kw)
    db.add(d)
    db.flush()
    return d


# ------------------------------------------------------------------ 1. connection type and ports
def test_manual_connection_fields_and_detail_page(app, admin):
    with SessionLocal() as db:
        core = add(db, name="Core", ip_address="10.0.0.1")
        db.commit()
        core_id = core.id
    r = admin.post("/devices/new", data={**DEVICE_FORM, "csrf_token": admin.token, "name": "PTP", "ip_address": "10.0.0.2",
                                         "parent_id": str(core_id), "uplink_type": "wireless", "uplink_port": "wlan1",
                                         "local_port": "wlan1"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        ptp = db.scalars(select(Device).where(Device.name == "PTP")).one()
        assert (ptp.uplink_type, ptp.uplink_port, ptp.local_port, ptp.uplink_auto) == ("wireless", "wlan1", "wlan1", False)
        ptp_id = ptp.id
    page = admin.get(f"/devices/{ptp_id}").text
    assert "📶 Wireless" in page and "<b>wlan1</b>" in page
    assert "wlan1 · wireless" in admin.get(f"/devices/{core_id}").text  # shown in the parent's connected devices
    # Clearing the fields hands them back to auto-detection
    admin.post(f"/devices/{ptp_id}/edit", data={**DEVICE_FORM, "csrf_token": admin.token, "name": "PTP",
                                                "ip_address": "10.0.0.2", "parent_id": str(core_id)})
    with SessionLocal() as db:
        ptp = db.get(Device, ptp_id)
        assert ptp.uplink_auto and ptp.uplink_type == ""
    # Ports without a parent make no sense and are dropped
    admin.post(f"/devices/{ptp_id}/edit", data={**DEVICE_FORM, "csrf_token": admin.token, "name": "PTP",
                                                "ip_address": "10.0.0.2", "uplink_port": "ether9"})
    with SessionLocal() as db:
        assert db.get(Device, ptp_id).uplink_port == ""
    assert admin.get(f"/api/devices/{core_id}/interfaces").json() == {"site_id": None, "interfaces": []}


def test_detects_ports_from_parent_and_child(app):
    srv = FakeRouterOS()  # neighbor 10.0.0.9 on ether1, wireless client 11:22:33:44:55:66 on wlan1
    box = app.state.secret_box
    with SessionLocal() as db:
        core = add(db, name="Core", ip_address="127.0.0.1", username="admin", password_enc=box.encrypt("secret"),
                   api_port=srv.port)
        wired = add(db, name="Tower2", ip_address="10.0.0.9", parent=core)
        radio = add(db, name="CPE", ip_address="10.10.0.50", mac_address="11:22:33:44:55:66", parent=core)
        manual = add(db, name="Manual", ip_address="10.0.0.99", mac_address="11:22:33:44:55:66", parent=core,
                     uplink_type="fiber", uplink_port="sfp1", uplink_auto=False)
        db.commit()
        ids = core.id, wired.id, radio.id, manual.id
    assert asyncio.run(StatsPoller(SessionLocal, box).poll_device(ids[0])) == ""
    with SessionLocal() as db:
        w, r, m = (db.get(Device, i) for i in ids[1:])
        assert (w.uplink_type, w.uplink_port) == ("wired", "ether1")
        assert (r.uplink_type, r.uplink_port) == ("wireless", "wlan1")
        assert (m.uplink_type, m.uplink_port) == ("fiber", "sfp1")  # typed by hand: untouched

    # The child's own neighbor table gives its local port towards the parent.
    data = dict(srv.data)
    data["/ip/neighbor/print"] = [{"interface": "ether5,bridge", "address": "10.0.0.1", "mac-address": "AA:00:00:00:00:01",
                                   "identity": "Core", "platform": "MikroTik"}]
    child_srv = FakeRouterOS(data=data)
    with SessionLocal() as db:
        parent = add(db, name="POP router", ip_address="10.0.0.1")
        child = add(db, name="Switch", ip_address="127.0.0.1", username="admin",
                    password_enc=box.encrypt("secret"), api_port=child_srv.port, parent=parent)
        db.get(Device, ids[0]).ip_address = "127.0.0.2"  # keep IPs unique
        db.commit()
        child_id = child.id
    assert asyncio.run(StatsPoller(SessionLocal, box).poll_device(child_id)) == ""
    with SessionLocal() as db:
        c = db.get(Device, child_id)
        assert (c.local_port, c.uplink_type) == ("ether5", "wired")


def test_map_api_includes_link_details(app, admin):
    with SessionLocal() as db:
        pop, t1 = Site(name="POP"), Site(name="T1")
        db.add_all([pop, t1])
        core = add(db, name="Core", ip_address="10.0.0.1", site=pop)
        add(db, name="PTP", ip_address="10.0.0.2", site=t1, parent=core, uplink_type="wireless",
            uplink_port="wlan1", local_port="wlan1", signal_dbm=-58)
        db.commit()
    data = admin.get("/api/map").json()
    ptp = next(d for d in data["devices"] if d["name"] == "PTP")
    assert (ptp["uplink_type"], ptp["uplink_port"], ptp["local_port"], ptp["parent_name"]) == ("wireless", "wlan1", "wlan1", "Core")
    link = data["site_links"][0]
    assert link["type"] == "wireless" and link["links"][0]["parent_port"] == "wlan1"
    assert "Wireless" in admin.get("/map").text  # legend


def test_changing_parent_drops_detected_ports(app, admin):
    with SessionLocal() as db:
        a, b = add(db, name="A", ip_address="10.0.0.1"), add(db, name="B", ip_address="10.0.0.2")
        c = add(db, name="C", ip_address="10.0.0.3", parent=a, uplink_type="wired", uplink_port="ether2")
        db.commit()
        ids = a.id, b.id, c.id
    admin.post(f"/devices/{ids[2]}/edit", data={**DEVICE_FORM, "csrf_token": admin.token, "name": "C", "ip_address": "10.0.0.3",
                                                "parent_id": str(ids[1]), "uplink_type": "wired", "uplink_port": "ether2"})
    with SessionLocal() as db:
        c = db.get(Device, ids[2])
        assert c.parent_id == ids[1] and c.uplink_port == "" and c.uplink_auto


def test_csv_import_connection_columns(app, admin):
    text = ("name,ip_address,parent,uplink_type,uplink_port,local_port\n"
            "Core,10.0.0.1,,,,\nAP,10.0.0.2,Core,wireless,wlan2,ath0\nBad,10.0.0.3,Core,laser,,\n")
    r = admin.post("/devices/import", data={"csrf_token": admin.token}, files={"file": ("d.csv", text, "text/csv")})
    assert "<b>2</b> created" in r.text and "Line 4" in r.text
    with SessionLocal() as db:
        ap = db.scalars(select(Device).where(Device.name == "AP")).one()
        assert (ap.uplink_type, ap.uplink_port, ap.local_port, ap.uplink_auto) == ("wireless", "wlan2", "ath0", False)


# ------------------------------------------------------------------ 2. customer device limited to the site
def test_customer_device_must_be_in_site(app, admin):
    with SessionLocal() as db:
        s1, s2 = Site(name="S1"), Site(name="S2")
        db.add_all([s1, s2])
        d1, d2 = add(db, name="AP1", ip_address="10.0.0.1", site=s1), add(db, name="AP2", ip_address="10.0.0.2", site=s2)
        db.commit()
        ids = s1.id, s2.id, d1.id, d2.id
    form = admin.get("/customers/new").text
    assert f'data-site="{ids[0]}"' in form and f'data-site="{ids[1]}"' in form
    base = {"csrf_token": admin.token, "name": "Ali", "mac_address": "AA:BB:CC:DD:EE:01"}
    r = admin.post("/customers/new", data={**base, "site_id": str(ids[0]), "access_point_id": str(ids[3])})
    assert r.status_code == 422 and "not in the chosen site" in r.text
    r = admin.post("/customers/new", data={**base, "site_id": str(ids[0]), "access_point_id": str(ids[2])},
                   follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.scalars(select(Customer)).one().access_point_id == ids[2]


# ------------------------------------------------------------------ 3. clear history (admin only)
def test_clear_history_everywhere(app, admin):
    now = utcnow()
    with SessionLocal() as db:
        d = add(db, name="Core", ip_address="10.0.0.1")
        other = add(db, name="Other", ip_address="10.0.0.2")
        db.add_all([
            StatusEvent(device_id=d.id, old_status="up", new_status="down"),
            StatusEvent(device_id=other.id, old_status="up", new_status="down"),
            PingResult(device_id=d.id, is_alive=True, avg_ms=1, loss_pct=0),
            MetricSample(device_id=d.id, cpu_pct=5),
            Alert(device_id=d.id, kind="down", severity="critical", message="", resolved_at=now),
            Alert(device_id=d.id, kind="high_cpu", severity="warning", message=""),  # still open
            NotificationLog(channel="telegram", message="x"),
            Neighbor(seen_by_id=d.id, ip_address="10.0.0.9"),
        ])
        done = CommandJob(username="admin", script="x", status="done")
        done.results = [CommandResult(device_name="Core", status="ok")]
        db.add_all([done, CommandJob(username="admin", script="y", status="running")])
        db.commit()
        dev_id, other_id = d.id, other.id

    page = admin.get(f"/devices/{dev_id}").text
    assert 'data-countdown="Clear the status history of this device?' in page and "Clear graph history" in page
    for url in ("/", "/alerts?tab=history", "/admin/audit", "/admin/alert-settings", "/commands", "/discovery"):
        assert "data-countdown=" in admin.get(url).text, url

    def clear(kind, **extra):
        r = admin.post(f"/history/clear/{kind}", data={"csrf_token": admin.token, **extra}, follow_redirects=False)
        assert r.status_code == 303, (kind, r.status_code)

    clear("device_events", device_id=str(dev_id))
    with SessionLocal() as db:
        assert [e.device_id for e in db.scalars(select(StatusEvent))] == [other_id]
    clear("events")
    clear("device_data", device_id=str(dev_id))
    clear("alerts")
    clear("notifications")
    clear("commands")
    clear("neighbors")
    clear("audit")
    with SessionLocal() as db:
        assert db.scalars(select(StatusEvent)).first() is None
        assert db.scalars(select(PingResult)).first() is None and db.scalars(select(MetricSample)).first() is None
        assert [a.kind for a in db.scalars(select(Alert))] == ["high_cpu"]  # open alert kept
        assert db.scalars(select(NotificationLog)).first() is None
        assert [j.status for j in db.scalars(select(CommandJob))] == ["running"]
        assert db.scalars(select(CommandResult)).first() is None
        assert db.scalars(select(Neighbor)).first() is None
        audit = db.scalars(select(AuditLog)).all()
        assert [a.action for a in audit] == ["history.clear.audit"]  # who cleared it is still recorded
    assert admin.post("/history/clear/everything", data={"csrf_token": admin.token}).status_code == 404


def test_only_admin_can_clear(app, admin):
    admin.post("/admin/users/new", data={"username": "tech", "role": "technician", "password": "Tech12345",
                                          "is_active": "on", "csrf_token": admin.token})
    admin.post("/logout", data={"csrf_token": admin.token})
    login(admin, "tech", "Tech12345")
    token = csrf(admin, "/")
    assert "data-countdown=" not in admin.get("/").text
    assert admin.post("/history/clear/events", data={"csrf_token": token}).status_code == 403
