"""Phase 4: backups, export/import, tools, Winbox/SSH launcher, CSV export."""
import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import asyncssh
import pytest
from sqlalchemy import delete, select

from spike_monitor.backups.service import BackupManager, load_backup_settings, normalized, save_backup_settings
from spike_monitor.db import SessionLocal
from spike_monitor.models import (
    AppSetting, ConfigBackup, CredentialProfile, Customer, Device, PortProfile, Site, Status, User, Vlan,
)
from spike_monitor.tools import ipcalc
from spike_monitor.transfer import TransferError, build_payload, decrypt_export, encrypt_export, import_payload


# ------------------------------------------------------------------ a fake RouterOS / airOS SSH server
class FakeRouter:
    def __init__(self, root: Path):
        self.root = root
        self.config = "/interface bridge\nadd name=bridge1\n/ip address\nadd address=10.0.0.1/24 interface=bridge1\n"
        self.stamp = 0

    def export(self) -> str:
        self.stamp += 1  # the date line changes on every export
        return f"# 2026-10-20 02:00:{self.stamp:02d} by RouterOS 7.15.3\n# software id = ABCD-1234\n#\n" + self.config

    async def handle(self, process):
        cmd = process.command or ""
        if cmd == "/export":
            process.stdout.write(self.export())
        elif cmd.startswith("/system backup save"):
            (self.root / "spike-auto.backup").write_bytes(b"\x88\xac\xa1\xb1BINARY-BACKUP" + self.config.encode())
            process.stdout.write("Configuration backup saved\n")
        elif cmd.startswith("/file remove"):
            (self.root / "spike-auto.backup").unlink(missing_ok=True)
        elif cmd == "cat /tmp/system.cfg":
            process.stdout.write("aaa.status=disabled\nwireless.1.ssid=SpikeNet\n")
        process.exit(0)


class _Auth(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return (username, password) == ("admin", "secret")


async def start_router(root: Path):
    router = FakeRouter(root)
    server = await asyncssh.create_server(
        _Auth, "127.0.0.1", 0, server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        process_factory=router.handle, sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root)))
    return router, server, server.sockets[0].getsockname()[1]


def add_device(app, port, brand="mikrotik", name="Core"):
    with SessionLocal() as db:
        d = Device(name=name, ip_address="127.0.0.1", brand=brand, ssh_port=port,
                   username="admin", password_enc=app.state.secret_box.encrypt("secret"), status=Status.UP)
        db.add(d)
        db.commit()
        return d.id


def test_normalized_ignores_date_header():
    a = "# 2026-10-20 02:00:01 by RouterOS 7.15.3\n# software id = X\n/ip address\n"
    b = "# 2026-10-21 02:00:09 by RouterOS 7.15.3\n# software id = X\n/ip address\n"
    assert normalized(a) == normalized(b)


def test_backups_end_to_end(app, admin, tmp_path):
    root = tmp_path / "router"
    root.mkdir()
    loop = asyncio.new_event_loop()
    router, server, port = loop.run_until_complete(start_router(root))
    try:
        dev_id = add_device(app, port)
        mgr = BackupManager(SessionLocal, app.state.secret_box, app.state.settings.data_dir)
        run = lambda: loop.run_until_complete(mgr.backup_device(dev_id, "admin"))  # noqa: E731
        assert run() == ""
        assert run() == ""  # same configuration again
        router.config += "/ip dns\nset servers=1.1.1.1\n"
        assert run() == ""
        with SessionLocal() as db:
            exports = db.scalars(select(ConfigBackup).where(ConfigBackup.kind == "export")
                                 .order_by(ConfigBackup.created_at, ConfigBackup.id)).all()
            binaries = db.scalars(select(ConfigBackup).where(ConfigBackup.kind == "binary")).all()
            assert [b.changed for b in exports] == [False, False, True]
            assert exports[0].path == exports[1].path != exports[2].path  # unchanged: file shared, not duplicated
            assert len(binaries) == 3 and mgr.file(binaries[-1]).read_bytes().startswith(b"\x88\xac\xa1\xb1")
            assert not (root / "spike-auto.backup").exists()  # cleaned up on the router
            first, last = exports[0].id, exports[2].id
            assert db.get(Device, dev_id).ssh_host_key.startswith("SHA256:")

        page = admin.get(f"/backups/{last}/diff").text
        assert "+2 line(s)" in page
        assert "servers=1.1.1.1" in page and "diff_add" in page  # HtmlDiff writes spaces as &nbsp;
        assert "/ip dns" in admin.get(f"/backups/{last}/view").text
        r = admin.get(f"/backups/{last}/download")
        assert r.status_code == 200 and b"set servers=1.1.1.1" in r.content and ".rsc" in r.headers["content-disposition"]
        assert "No differences" in admin.get(f"/backups/{first + 2}/diff?against={first}").text  # 2nd export vs 1st
        assert "Configuration backups" in admin.get(f"/devices/{dev_id}").text
        listing = admin.get("/backups").text
        assert "Core" in listing and "Back up now" in listing

        # Keep only 2 per type: older rows and unused files go away
        with SessionLocal() as db:
            s = load_backup_settings(db)
            s.keep = 2
            save_backup_settings(db, s)
            db.commit()
        assert run() == ""
        with SessionLocal() as db:
            exports = db.scalars(select(ConfigBackup).where(ConfigBackup.kind == "export")).all()
            assert len(exports) == 2
            assert all(mgr.file(b).exists() for b in exports)
    finally:
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()


def test_ubiquiti_backup_and_failure(app, admin, tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    loop = asyncio.new_event_loop()
    router, server, port = loop.run_until_complete(start_router(root))
    try:
        ub = add_device(app, port, brand="ubiquiti", name="Rocket")
        mgr = BackupManager(SessionLocal, app.state.secret_box, app.state.settings.data_dir)
        assert loop.run_until_complete(mgr.backup_device(ub, "schedule")) == ""
        with SessionLocal() as db:
            b = db.scalars(select(ConfigBackup)).one()
            assert b.kind == "ubnt" and b"wireless.1.ssid=SpikeNet" in mgr.file(b).read_bytes()
            d = db.get(Device, ub)
            d.password_enc = app.state.secret_box.encrypt("wrong")
            db.commit()
        err = loop.run_until_complete(mgr.backup_device(ub, "schedule"))
        assert "wrong username or password" in err
        with SessionLocal() as db:
            assert db.scalars(select(ConfigBackup).where(ConfigBackup.ok.is_(False))).one().error == err
        assert "failed" in admin.get("/backups").text
    finally:
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()


def test_backup_schedule_and_settings(app, admin):
    mgr = app.state.backups
    with SessionLocal() as db:
        s = load_backup_settings(db)
        assert (s.enabled, s.time, s.keep) == (True, "02:00", 30)
    assert not mgr.due(datetime(2026, 10, 21, 1, 59))
    assert mgr.due(datetime(2026, 10, 21, 2, 0))
    mgr.mark_ran(datetime(2026, 10, 21, 2, 0))
    assert not mgr.due(datetime(2026, 10, 21, 23, 0))  # once a day
    assert mgr.due(datetime(2026, 10, 22, 2, 5))
    r = admin.post("/backups/settings", data={"csrf_token": admin.token, "enabled": "on", "time": "03:30", "keep": "10",
                                              "concurrency": "3"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        s = load_backup_settings(db)
        assert (s.time, s.keep, s.binary) == ("03:30", 10, False)
    assert "Time must look like" in admin.post("/backups/settings", data={"csrf_token": admin.token, "time": "25:00",
                                                                         "keep": "5", "concurrency": "1"}).text


# ------------------------------------------------------------------ export / import
def seed_inventory(app):
    box = app.state.secret_box
    with SessionLocal() as db:
        site = Site(name="Tower 1", latitude=33.9)
        vlan = Vlan(vlan_id=100, name="mgmt", site=site)
        cp = CredentialProfile(name="std", username="noc", password_enc=box.encrypt("Pr0file!"))
        core = Device(name="Core", ip_address="10.0.0.1", site=site, password_enc=box.encrypt("CoreSecret"),
                      username="admin", ssh_port=2222, vlans=[vlan])
        ap = Device(name="AP", ip_address="10.0.0.2", site=site, parent=core, credential_profile=cp,
                    uplink_type="wireless", uplink_port="wlan1")
        db.add_all([site, vlan, cp, core, ap, PortProfile(name="custom", ssh_port=2200),
                    Customer(name="Ali", mac_address="AA:BB:CC:DD:EE:FF", site=site, access_point=ap),
                    User(username="tech", password_hash="hash", role="technician", telegram_chat_id="555")])
        db.add(AppSetting(key="alerts", value=json.dumps({"telegram_enabled": True, "tg_token_enc": box.encrypt("123:TOK")})))
        db.commit()


def test_export_import_roundtrip(app, admin):
    seed_inventory(app)
    box = app.state.secret_box
    r = admin.post("/admin/transfer/export", data={"csrf_token": admin.token, "password": "Exp0rtPass",
                                                   "confirm": "Exp0rtPass", "users": "on"})
    assert r.status_code == 200 and r.headers["content-disposition"].endswith(".spike")
    raw = r.content
    assert b"CoreSecret" not in raw and b"Pr0file!" not in raw  # encrypted
    with pytest.raises(TransferError, match="Wrong password"):
        decrypt_export(raw, "nope-nope")
    assert decrypt_export(raw, "Exp0rtPass")["devices"][0]["password"] in ("CoreSecret", "")

    # Simulate a new, empty server
    with SessionLocal() as db:
        for model in (Customer, Device, Vlan, CredentialProfile, PortProfile, Site):
            db.execute(delete(model))
        db.execute(delete(User).where(User.username == "tech"))
        db.execute(delete(AppSetting))
        db.commit()
    r = admin.post("/admin/transfer/import", data={"csrf_token": admin.token, "password": "Exp0rtPass", "users": "on",
                                                   "settings": "on"}, files={"file": ("x.spike", raw)})
    assert "Imported:" in r.text and "2 devices" in r.text
    with SessionLocal() as db:
        core = db.scalars(select(Device).where(Device.ip_address == "10.0.0.1")).one()
        ap = db.scalars(select(Device).where(Device.ip_address == "10.0.0.2")).one()
        assert box.decrypt(core.password_enc) == "CoreSecret" and core.ssh_port == 2222
        assert [v.vlan_id for v in core.vlans] == [100] and core.site.name == "Tower 1"
        assert ap.parent_id == core.id and ap.uplink_port == "wlan1"
        assert box.decrypt(ap.credential_profile.password_enc) == "Pr0file!"
        assert db.scalars(select(Customer)).one().access_point_id == ap.id
        assert db.scalars(select(User).where(User.username == "tech")).one().telegram_chat_id == "555"
        settings = json.loads(db.get(AppSetting, "alerts").value)
        assert box.decrypt(settings["tg_token_enc"]) == "123:TOK"

    # Importing again updates instead of duplicating, and never touches existing users
    r = admin.post("/admin/transfer/import", data={"csrf_token": admin.token, "password": "Exp0rtPass", "users": "on"},
                   files={"file": ("x.spike", raw)})
    with SessionLocal() as db:
        assert len(db.scalars(select(Device)).all()) == 2 and len(db.scalars(select(Site)).all()) == 1
    assert "Wrong password" in admin.post("/admin/transfer/import", data={"csrf_token": admin.token, "password": "bad"},
                                          files={"file": ("x.spike", raw)}).text
    assert "not a Spike Net export" in admin.post("/admin/transfer/import", data={"csrf_token": admin.token,
                                                  "password": "x"}, files={"file": ("x.spike", b"hello")}).text


def test_export_password_rules(app, admin):
    r = admin.post("/admin/transfer/export", data={"csrf_token": admin.token, "password": "short", "confirm": "short"})
    assert "at least 8" in r.text
    r = admin.post("/admin/transfer/export", data={"csrf_token": admin.token, "password": "Abcdefgh1", "confirm": "x"})
    assert "do not match" in r.text


def test_csv_export_has_no_passwords(app, admin):
    seed_inventory(app)
    r = admin.get("/devices/export.csv")
    text = r.content.decode("utf-8-sig")
    assert r.status_code == 200 and "Core,10.0.0.1" in text and "CoreSecret" not in text
    assert text.splitlines()[0].startswith("name,ip_address,brand")
    # it imports back
    r = admin.post("/devices/import", data={"csrf_token": admin.token}, files={"file": ("d.csv", text, "text/csv")})
    assert "<b>2</b> updated" in r.text


# ------------------------------------------------------------------ tools
def test_ipcalc():
    c = ipcalc.calculate("192.168.1.10/24")
    assert (c["network"], c["broadcast"], c["first_host"], c["last_host"], c["usable"]) == (
        "192.168.1.0/24", "192.168.1.255", "192.168.1.1", "192.168.1.254", "254")
    assert ipcalc.calculate("10.0.0.5 255.255.255.252")["usable"] == "2"
    assert ipcalc.calculate("10.0.0.0/31")["usable"] == "2"
    assert ipcalc.calculate("2001:db8::1/64")["prefix"] == 64
    with pytest.raises(ipcalc.CalcError):
        ipcalc.calculate("300.1.1.1/24")
    plan = ipcalc.vlsm("10.10.0.0/22", ipcalc.parse_needs("Jounieh 120, Aley 60\nmgmt 14, ptp 2"))
    assert [p["subnet"] for p in plan] == ["10.10.0.0/25", "10.10.0.128/26", "10.10.0.192/28", "10.10.0.208/30"]
    with pytest.raises(ipcalc.CalcError, match="Not enough room"):
        ipcalc.vlsm("10.0.0.0/28", [("big", 100)])
    assert ipcalc.split("10.0.0.0/24", 26) == ["10.0.0.0/26", "10.0.0.64/26", "10.0.0.128/26", "10.0.0.192/26"]


def test_tools_pages(app, admin, monkeypatch):
    assert "192.168.1.254" in admin.get("/tools?ip=192.168.1.10/24").text
    assert "Not a valid" in admin.get("/tools?ip=abc").text
    assert "10.10.0.128/26" in admin.get("/tools/plan?base=10.10.0.0/22&needs=A+120,B+60").text
    assert "10.0.0.64/26" in admin.get("/tools/plan?base=10.0.0.0/24&prefix=26").text
    from spike_monitor.monitoring.pinger import PingOutcome
    from spike_monitor.tools.trace import Hop

    async def fake_ping(host, count, timeout):
        return PingOutcome(True, 4.2, 0.0)

    async def fake_trace(host, max_hops=30):
        return [Hop(1, "10.0.0.1", 1.1, 0.0), Hop(2, "8.8.8.8", 20.5, 0.0)], ""

    monkeypatch.setattr("spike_monitor.routers.tools.ping", fake_ping)
    monkeypatch.setattr("spike_monitor.routers.tools.traceroute", fake_trace)
    r = admin.post("/tools/network", data={"csrf_token": admin.token, "host": "8.8.8.8", "count": "3", "action": "ping"})
    assert "4.2" in r.text and "Reply" in r.text
    r = admin.post("/tools/network", data={"csrf_token": admin.token, "host": "8.8.8.8", "action": "trace"})
    assert "20.5 ms" in r.text
    r = admin.post("/tools/network", data={"csrf_token": admin.token, "host": "-n 1; rm", "action": "ping"})
    assert "Enter an IP address" in r.text


# ------------------------------------------------------------------ Winbox / SSH launcher
def test_launch_token_is_single_use(app, admin):
    with SessionLocal() as db:
        d = Device(name="Core", ip_address="10.0.0.1", brand="mikrotik", winbox_port=9291, ssh_port=2222,
                   username="admin", password_enc=app.state.secret_box.encrypt("W1nbox!"))
        u = Device(name="Rocket", ip_address="10.0.0.2", brand="ubiquiti")
        db.add_all([d, u])
        db.commit()
        dev_id, ub_id = d.id, u.id
    assert "🖥️ Winbox" in admin.get(f"/devices/{dev_id}").text
    page = admin.post(f"/devices/{dev_id}/launch/winbox", data={"csrf_token": admin.token}).text
    assert "spikenet://open?server=" in page and "W1nbox!" not in page
    token = page.split("token=", 1)[1].split('"', 1)[0]
    info = admin.get(f"/api/launch/{token}").json()
    assert info == {"kind": "winbox", "name": "Core", "host": "10.0.0.1", "port": 9291, "username": "admin",
                    "password": "W1nbox!"}
    assert admin.get(f"/api/launch/{token}").status_code == 404  # used once
    page = admin.post(f"/devices/{dev_id}/launch/ssh", data={"csrf_token": admin.token}).text
    token = page.split("token=", 1)[1].split('"', 1)[0]
    assert admin.get(f"/api/launch/{token}").json()["port"] == 2222
    assert admin.post(f"/devices/{ub_id}/launch/winbox", data={"csrf_token": admin.token}).status_code == 400
    # expired tokens are refused
    page = admin.post(f"/devices/{dev_id}/launch/winbox", data={"csrf_token": admin.token}).text
    token = page.split("token=", 1)[1].split('"', 1)[0]
    app.state.launch_tokens[token]["expires"] -= 120
    assert admin.get(f"/api/launch/{token}").status_code == 404


def test_helper_zip(app, admin):
    r = admin.get("/tools/helper.zip")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(z.namelist()) == {"SpikeNetHelper/install.bat", "SpikeNetHelper/spikenet-helper.ps1",
                                 "SpikeNetHelper/config.json"}
    assert json.loads(z.read("SpikeNetHelper/config.json"))["server"] == "http://testserver"
    assert b"HKCU:\\Software\\Classes\\spikenet" in z.read("SpikeNetHelper/spikenet-helper.ps1")


def test_viewer_cannot_use_phase4_tools(app, admin):
    admin.post("/admin/users/new", data={"username": "view", "role": "viewer", "password": "View12345",
                                          "is_active": "on", "csrf_token": admin.token})
    admin.post("/logout", data={"csrf_token": admin.token})
    from .conftest import login
    login(admin, "view", "View12345")
    for url in ("/backups", "/tools", "/tools/helper.zip", "/admin/transfer", "/devices/export.csv"):
        assert admin.get(url).status_code == 403, url
