from sqlalchemy import select

from spike_monitor.db import SessionLocal
from spike_monitor.models import CredentialProfile, Device, Site


def add_device(c, **kw):
    data = {"name": "dev", "ip_address": "10.0.0.1", "brand": "mikrotik", "device_type": "router",
            "winbox_port": "8291", "ssh_port": "22", "api_port": "8728", "api_ssl_port": "8729",
            "http_port": "80", "https_port": "443", "latency_warn_ms": "100", "loss_warn_pct": "20",
            "monitoring_enabled": "on", "csrf_token": c.token}
    data.update(kw)
    return c.post("/devices/new", data=data, follow_redirects=False)


def test_site_crud(admin):
    r = admin.post("/sites/new", data={"name": "Tower 1", "csrf_token": admin.token}, follow_redirects=False)
    assert r.status_code == 303
    assert admin.post("/sites/new", data={"name": "tower 1", "csrf_token": admin.token}).status_code == 422
    assert "Tower 1" in admin.get("/sites").text


def test_device_with_custom_ports_and_encrypted_password(admin, app):
    r = add_device(admin, name="Core", ip_address="10.0.0.1", ssh_port="2222", winbox_port="9291",
                   username="admin", password="s3cret!")
    assert r.status_code == 303
    with SessionLocal() as db:
        d = db.scalars(select(Device)).one()
        assert (d.ssh_port, d.winbox_port) == (2222, 9291)
        assert d.password_enc and "s3cret" not in d.password_enc
        assert app.state.secret_box.decrypt(d.password_enc) == "s3cret!"
    page = admin.get(f"/devices/{d.id}").text
    assert "s3cret" not in page and "2222" in page

    # Editing without a password keeps the stored one.
    r = add_device(admin, name="Core2", ip_address="10.0.0.1")
    assert r.status_code == 422  # duplicate IP
    r = admin.post(f"/devices/{d.id}/edit", data={
        "name": "Core", "ip_address": "10.0.0.1", "brand": "mikrotik", "device_type": "router",
        "winbox_port": "9291", "ssh_port": "2222", "api_port": "8728", "api_ssl_port": "8729", "http_port": "80",
        "https_port": "443", "latency_warn_ms": "100", "loss_warn_pct": "20", "csrf_token": admin.token},
        follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        d = db.get(Device, d.id)
        assert app.state.secret_box.decrypt(d.password_enc) == "s3cret!"
        assert d.status == "paused"  # monitoring checkbox was off


def test_invalid_port_and_ip(admin):
    assert add_device(admin, ssh_port="70000").status_code == 422
    assert add_device(admin, ip_address="999.1.1.1").status_code == 422


def test_parent_loop_rejected(admin):
    add_device(admin, name="A", ip_address="10.0.0.1")
    with SessionLocal() as db:
        a = db.scalars(select(Device)).one().id
    add_device(admin, name="B", ip_address="10.0.0.2", parent_id=str(a))
    with SessionLocal() as db:
        b = db.scalars(select(Device).where(Device.name == "B")).one().id
    r = admin.post(f"/devices/{a}/edit", data={
        "name": "A", "ip_address": "10.0.0.1", "brand": "mikrotik", "device_type": "router", "parent_id": str(b),
        "winbox_port": "8291", "ssh_port": "22", "api_port": "8728", "api_ssl_port": "8729", "http_port": "80",
        "https_port": "443", "latency_warn_ms": "100", "loss_warn_pct": "20", "csrf_token": admin.token})
    assert r.status_code == 422


def test_port_profile_applied(admin):
    admin.post("/port-profiles/new", data={"name": "Custom", "winbox_port": "9000", "ssh_port": "2200",
                                            "api_port": "8000", "api_ssl_port": "8001", "http_port": "8080",
                                            "https_port": "8443", "csrf_token": admin.token})
    with SessionLocal() as db:
        from spike_monitor.models import PortProfile
        pid = db.scalars(select(PortProfile)).one().id
    add_device(admin, port_profile_id=str(pid))
    with SessionLocal() as db:
        d = db.scalars(select(Device)).one()
        assert (d.winbox_port, d.ssh_port, d.api_port) == (9000, 2200, 8000)


def test_credential_profile_password_hidden(admin):
    admin.post("/credentials/new", data={"name": "Std", "username": "noc", "password": "Hidden99",
                                          "csrf_token": admin.token})
    page = admin.get("/credentials").text
    assert "Hidden99" not in page and "noc" in page
    with SessionLocal() as db:
        assert db.scalars(select(CredentialProfile)).one().password_enc


def test_vlan_and_customer(admin):
    assert admin.post("/vlans/new", data={"vlan_id": "100", "name": "MGMT", "purpose": "management",
                                          "csrf_token": admin.token}, follow_redirects=False).status_code == 303
    assert admin.post("/vlans/new", data={"vlan_id": "100", "name": "dup",
                                          "csrf_token": admin.token}).status_code == 422
    assert admin.post("/vlans/new", data={"vlan_id": "5000", "name": "bad",
                                          "csrf_token": admin.token}).status_code == 422
    r = admin.post("/customers/new", data={"name": "Ali", "mac_address": "aa-bb-cc-dd-ee-ff",
                                           "csrf_token": admin.token}, follow_redirects=False)
    assert r.status_code == 303
    assert "AA:BB:CC:DD:EE:FF" in admin.get("/customers").text


def test_csv_import(admin, app):
    csv_text = (
        "name,ip_address,brand,device_type,site,parent,password,ssh_port\n"
        "Tower AP,10.0.1.2,ubiquiti,access_point,Tower 1,Core,pw1,2222\n"
        "Core,10.0.0.1,mikrotik,router,Main,,pw2,\n"
        "Broken,not-an-ip!,mikrotik,router,,,,\n"
    )
    r = admin.post("/devices/import", data={"csrf_token": admin.token},
                   files={"file": ("d.csv", csv_text, "text/csv")})
    assert r.status_code == 200
    assert "<b>2</b> created" in r.text and "Line 4" in r.text
    with SessionLocal() as db:
        ap = db.scalars(select(Device).where(Device.name == "Tower AP")).one()
        assert ap.parent.name == "Core" and ap.ssh_port == 2222 and ap.site.name == "Tower 1"
        assert app.state.secret_box.decrypt(ap.password_enc) == "pw1"
        assert db.scalars(select(Site)).all()


def test_dashboard_and_summary(admin):
    add_device(admin)
    assert admin.get("/").status_code == 200
    s = admin.get("/api/summary").json()
    assert s["total"] == 1


def test_404_page(admin):
    assert admin.get("/devices/999").status_code == 404
    assert admin.get("/nope").status_code == 404
