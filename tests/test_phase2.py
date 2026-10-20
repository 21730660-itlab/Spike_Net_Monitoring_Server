import asyncio
import time
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy import select

from spike_monitor.collectors.common import DeviceStats, InterfaceStat, Inventory, NeighborStat, PeerStat
from spike_monitor.db import SessionLocal, init_engine
from spike_monitor.models import (
    CommandJob, Customer, Device, DeviceInterface, HourlyRollup, MetricSample, Neighbor, Status,
    TrafficSample, Vlan, WirelessPeer, utcnow,
)
from spike_monitor.monitoring import poller as poller_mod
from spike_monitor.monitoring.poller import StatsPoller
from spike_monitor.monitoring.rollup import Housekeeper, hour_floor

from .fake_routeros import FakeRouterOS


def make_device(app, **kw):
    box = app.state.secret_box
    with SessionLocal() as db:
        d = Device(name=kw.pop("name", "Core"), ip_address=kw.pop("ip_address", "127.0.0.1"), brand="mikrotik",
                   username="admin", password_enc=box.encrypt("secret"), status=Status.UP, **kw)
        db.add(d)
        db.commit()
        return d.id


def stats(rx, peers=True):
    return DeviceStats(
        identity="Core", cpu_pct=30, mem_pct=40, temperature=45, voltage=24, uptime_s=100,
        interfaces=[InterfaceStat("ether1", "ether", running=True, rx_bytes=rx, tx_bytes=rx // 2),
                    InterfaceStat("wlan1", "wlan", running=True, rx_bytes=0, tx_bytes=0)],
        peers=[PeerStat("AA:BB:CC:DD:EE:FF", "wlan1", signal_dbm=-60)] if peers else [],
        neighbors=[NeighborStat("ether1", "10.0.0.9", "DE:AD:BE:EF:00:09", "Tower2", "MikroTik", "RB912")])


def test_poll_against_fake_routeros(app, admin):
    srv = FakeRouterOS()
    dev_id = make_device(app, api_port=srv.port)
    error = asyncio.run(app.state.poller.poll_device(dev_id))
    assert error == ""
    with SessionLocal() as db:
        d = db.get(Device, dev_id)
        assert d.identity == "Core-RB4011" and d.cpu_pct == 12 and d.wireless_clients == 1
        assert {i.name for i in d.interfaces} == {"ether1", "wlan1", "vlan100"}
    page = admin.get(f"/devices/{dev_id}").text
    assert "Customer-CPE" in page and "41°" in page


def test_poll_error_is_shown(app, admin):
    dev_id = make_device(app, api_port=1)
    error = asyncio.run(app.state.poller.poll_device(dev_id))
    assert "refused" in error
    assert "Could not read device data" in admin.get(f"/devices/{dev_id}").text


def test_rates_peers_neighbors_and_discovery(app, admin):
    dev_id = make_device(app)
    seq = iter([stats(1_000_000), stats(1_750_000)])

    async def fake_collect(device, box):
        return next(seq), ""

    p = StatsPoller(SessionLocal, app.state.secret_box, collect=fake_collect)
    asyncio.run(p.poll_device(dev_id))
    with SessionLocal() as db:  # pretend the first poll was 60 s ago
        for i in db.scalars(select(DeviceInterface)):
            i.updated_at -= timedelta(seconds=60)
        db.add(Customer(name="Ali Customer", mac_address="AA:BB:CC:DD:EE:FF"))
        db.commit()
    asyncio.run(p.poll_device(dev_id))
    with SessionLocal() as db:
        eth = db.scalars(select(DeviceInterface).where(DeviceInterface.name == "ether1")).one()
        assert abs(eth.rx_bps - 100_000) < 1000  # 750 kB in ~60 s
        assert db.scalars(select(TrafficSample)).first() is not None
        assert len(db.scalars(select(WirelessPeer)).all()) == 1
        assert db.scalars(select(Neighbor)).one().identity == "Tower2"
        assert len(db.scalars(select(MetricSample)).all()) == 2
    page = admin.get(f"/devices/{dev_id}").text
    assert "Ali Customer" in page and "Kbps" in page
    disc = admin.get("/discovery").text
    assert "Tower2" in disc and "ip_address=10.0.0.9" in disc and f"parent_id={dev_id}" in disc
    # Prefilled add form from the discovery link
    form = admin.get("/devices/new?name=Tower2&ip_address=10.0.0.9").text
    assert 'value="Tower2"' in form and 'value="10.0.0.9"' in form
    m = admin.get(f"/devices/{dev_id}/metrics?hours=1").json()
    assert m["cpu"] == [30, 30]
    t = admin.get(f"/devices/{dev_id}/traffic?interface=ether1&hours=1").json()
    assert len(t["rx"]) == 1


def test_down_devices_are_not_polled(app):
    dev_up = make_device(app, name="up", ip_address="10.0.0.1")
    make_device(app, name="down", ip_address="10.0.0.2")
    with SessionLocal() as db:
        db.scalars(select(Device).where(Device.name == "down")).one().status = Status.DOWN
        db.commit()
    polled = []

    async def fake_collect(device, box):
        polled.append(device.id)
        return stats(1), ""

    asyncio.run(StatsPoller(SessionLocal, app.state.secret_box, collect=fake_collect).run_cycle())
    assert polled == [dev_up]


def test_import_from_device(app, admin, monkeypatch):
    dev_id = make_device(app, name="old name")

    async def fake_inventory(device, box):
        return Inventory(identity="Core-RB4011", model="RB4011", serial_number="S1", os_version="7.15.3",
                         mac_address="AA:BB:CC:00:00:01", vlans=[{"name": "mgmt", "vlan_id": 10, "interface": "ether1"}],
                         neighbors=[NeighborStat("ether1", "10.0.0.9", "DE:AD:BE:EF:00:09", "Tower2")]), ""

    monkeypatch.setattr("spike_monitor.routers.device_actions.read_inventory", fake_inventory)
    page = admin.get(f"/devices/{dev_id}/import").text
    assert "Core-RB4011" in page and "will be created" in page and "Tower2" in page
    r = admin.post(f"/devices/{dev_id}/import", data={
        "csrf_token": admin.token, "apply_name": "on", "value_name": "Core-RB4011",
        "apply_serial_number": "on", "value_serial_number": "S1", "value_model": "RB4011",  # model not ticked
        "vlan": "10:mgmt"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        d = db.get(Device, dev_id)
        assert d.name == "Core-RB4011" and d.serial_number == "S1" and d.model == ""
        assert [v.vlan_id for v in d.vlans] == [10]
        assert db.scalars(select(Vlan)).one().name == "mgmt"


def test_push_commands(app, admin, monkeypatch):
    ok_id = make_device(app, name="r1", ip_address="10.0.0.1")
    bad_id = make_device(app, name="r2", ip_address="10.0.0.2")
    off_id = make_device(app, name="r3", ip_address="10.0.0.3", use_ssh=False)

    async def fake_run(target, command, command_timeout=60):
        from spike_monitor.collectors.ssh import SshResult
        if target.host == "10.0.0.2":
            return SshResult("failure: already have such address", 0, "SHA256:x")
        return SshResult(f"executed {command}", 0, "SHA256:x")

    monkeypatch.setattr("spike_monitor.routers.commands.run_command", fake_run)
    assert f'value="{ok_id}" checked' in admin.get(f"/commands/new?device={ok_id}").text
    ids = [str(ok_id), str(bad_id), str(off_id)]
    page = admin.post("/commands/preview", data={"csrf_token": admin.token, "script": "/ip dns set servers=1.1.1.1",
                                                 "device_ids": ids}).text
    assert "Will run on (2)" in page and "SSH turned off" in page
    assert admin.post("/commands/run", data={"csrf_token": admin.token, "script": "x",
                                             "device_ids": ids}).status_code == 400  # no confirmation
    r = admin.post("/commands/run", data={"csrf_token": admin.token, "script": "/ip dns set servers=1.1.1.1",
                                          "device_ids": [str(ok_id), str(bad_id)], "confirm": "yes"},
                   follow_redirects=False)
    assert r.status_code == 303
    for _ in range(50):
        with SessionLocal() as db:
            job = db.scalars(select(CommandJob)).one()
            if job.status == "done":
                statuses = {res.device_name.split()[0]: res.status for res in job.results}
                break
        time.sleep(0.1)
    assert statuses == {"r1": "ok", "r2": "warning"}
    assert "executed" in admin.get(r.headers["location"]).text


def test_push_is_admin_only(app, admin):
    admin.post("/admin/users/new", data={"username": "tech", "role": "technician", "password": "Tech12345",
                                          "is_active": "on", "csrf_token": admin.token})
    admin.post("/logout", data={"csrf_token": admin.token})
    from .conftest import login
    login(admin, "tech", "Tech12345")
    assert admin.get("/commands/new").status_code == 403


def test_hourly_rollup(app, admin):
    dev_id = make_device(app)
    hour = hour_floor(utcnow()) - timedelta(hours=2)
    with SessionLocal() as db:
        for i, cpu in enumerate([10, 30]):
            db.add(MetricSample(device_id=dev_id, ts=hour + timedelta(minutes=10 * i), cpu_pct=cpu, mem_pct=50))
            db.add(TrafficSample(device_id=dev_id, interface="ether1", ts=hour + timedelta(minutes=10 * i),
                                 rx_bps=1000 * (i + 1), tx_bps=10))
        db.commit()
    Housekeeper(SessionLocal).run(hour_floor(utcnow()))
    with SessionLocal() as db:
        m = db.scalars(select(HourlyRollup).where(HourlyRollup.series == "metrics")).one()
        assert m.v1 == 20 and m.v1_max == 30
        t = db.scalars(select(HourlyRollup).where(HourlyRollup.series == "if:ether1")).one()
        assert t.v1 == 1500 and t.v1_max == 2000
    data = admin.get(f"/devices/{dev_id}/metrics?hours=720").json()
    assert data["hourly"] and data["cpu"] == [20]


def test_upgrade_adds_new_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = sa.create_engine(url)
    with eng.begin() as c:  # a phase-1 style devices table without the new columns
        c.execute(sa.text("CREATE TABLE devices (id INTEGER PRIMARY KEY, name VARCHAR(128), ip_address VARCHAR(64))"))
        c.execute(sa.text("INSERT INTO devices (name, ip_address) VALUES ('old', '10.0.0.1')"))
    init_engine(url)
    cols = {c["name"] for c in sa.inspect(sa.create_engine(url)).get_columns("devices")}
    assert {"cpu_pct", "poll_error", "ssh_host_key"} <= cols
    with SessionLocal() as db:
        assert db.scalars(select(Device)).one().poll_error == ""
