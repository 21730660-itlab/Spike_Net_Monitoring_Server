from sqlalchemy import select

from spike_monitor.db import SessionLocal
from spike_monitor.models import Device, Site, Status


def build(app):
    with SessionLocal() as db:
        pop, t1, t2 = Site(name="Main POP", latitude=33.9, longitude=35.5), Site(name="Tower 1"), Site(name="Tower 2")
        db.add_all([pop, t1, t2])
        db.flush()
        up = Device(name="Upstream", ip_address="10.0.0.254", device_type="upstream", site=pop, status=Status.UP)
        core = Device(name="Core", ip_address="10.0.0.1", site=pop, parent=up, status=Status.UP)
        ptp = Device(name="PTP-T1", ip_address="10.0.1.1", site=t1, parent=core, status=Status.UP, signal_dbm=-58)
        ap = Device(name="AP-T1", ip_address="10.0.1.2", site=t1, parent=ptp, status=Status.WARNING)
        t2dev = Device(name="PTP-T2", ip_address="10.0.2.1", site=t2, parent=core, status=Status.DOWN)
        db.add_all([up, core, ptp, ap, t2dev])
        db.commit()
        return pop.id, t1.id, t2.id, core.id


def test_map_data(app, admin):
    pop, t1, t2, core = build(app)
    assert admin.get("/map").status_code == 200
    data = admin.get("/api/map").json()
    sites = {s["name"]: s for s in data["sites"]}
    assert sites["Main POP"]["status"] == "up" and sites["Main POP"]["upstream"]
    assert sites["Tower 1"]["status"] == "warning" and sites["Tower 2"]["status"] == "down"
    links = {(l["from"], l["to"]): l for l in data["site_links"]}
    assert set(links) == {(pop, t1), (pop, t2)}  # only links that cross sites
    assert links[(pop, t1)]["signal"] == -58 and links[(pop, t2)]["status"] == "down"
    assert len(data["devices"]) == 5


def test_save_layout_and_location(app, admin):
    pop, t1, t2, core = build(app)
    headers = {"X-CSRF-Token": admin.token}
    r = admin.post("/api/map/layout", json={"kind": "site", "positions": {str(t1): [10, -20]}}, headers=headers)
    assert r.json() == {"saved": 1}
    r = admin.post("/api/map/layout", json={"kind": "device", "positions": {str(core): [5, 6]}}, headers=headers)
    assert r.json() == {"saved": 1}
    r = admin.post(f"/api/map/sites/{t2}/location", json={"lat": 34.1, "lon": 35.65}, headers=headers)
    assert r.status_code == 200
    assert admin.post(f"/api/map/sites/{t2}/location", json={"lat": 200, "lon": 0}, headers=headers).status_code == 400
    assert admin.post("/api/map/layout", json={"kind": "site", "positions": {}}).status_code == 400  # no CSRF
    with SessionLocal() as db:
        assert (db.get(Site, t1).map_x, db.get(Site, t1).map_y) == (10, -20)
        assert db.get(Site, t2).latitude == 34.1
        assert db.get(Device, core).map_x == 5
