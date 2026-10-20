import asyncio
import io
import json
import urllib.error
import urllib.parse
from datetime import timedelta

import pytest
from sqlalchemy import select

from spike_monitor.alerts import whatsapp
from spike_monitor.alerts.engine import AlertManager
from spike_monitor.alerts.settings import load_settings, save_settings
from spike_monitor.collectors.common import DeviceStats
from spike_monitor.db import SessionLocal
from spike_monitor.models import (
    Alert, Device, DeviceInterface, NotificationLog, Site, Status, User, utcnow,
)
from spike_monitor.monitoring.poller import StatsPoller, store_stats

from .fake_routeros import FakeRouterOS


@pytest.fixture
def mgr(app):
    return AlertManager(SessionLocal, app.state.secret_box)


def tree():
    """core -> tower PTP -> 2 APs, in two sites."""
    with SessionLocal() as db:
        pop, t1 = Site(name="POP"), Site(name="Tower 1")
        core = Device(name="Core", ip_address="10.0.0.1", site=pop, status=Status.UP)
        ptp = Device(name="PTP T1", ip_address="10.0.1.1", site=t1, parent=core, status=Status.UP,
                     device_type="ptp_link")
        aps = [Device(name=f"AP{i}", ip_address=f"10.0.1.{10 + i}", site=t1, parent=ptp, status=Status.UP)
               for i in range(2)]
        db.add_all([pop, t1, core, ptp, *aps])
        db.commit()
        return {"core": core.id, "ptp": ptp.id, "ap0": aps[0].id, "ap1": aps[1].id, "pop": pop.id, "t1": t1.id}


def set_dev(dev_id, **kw):
    with SessionLocal() as db:
        d = db.get(Device, dev_id)
        for k, v in kw.items():
            setattr(d, k, v)
        db.commit()


def open_alerts():
    with SessionLocal() as db:
        return {(a.device.name, a.kind): a for a in db.scalars(select(Alert).where(Alert.resolved_at.is_(None)))}


def test_tower_down_one_alert_repeat_ack_and_recovery(mgr):
    ids = tree()
    assert mgr.reconcile() == []
    set_dev(ids["ptp"], status=Status.DOWN)
    set_dev(ids["ap0"], status=Status.UNREACHABLE)
    set_dev(ids["ap1"], status=Status.UNREACHABLE)
    out = mgr.reconcile()
    assert len(out) == 1 and out[0].severity == "critical"
    assert "DOWN: PTP T1 (10.0.1.1) at Tower 1" in out[0].text and "2 device(s) behind it unreachable" in out[0].text
    assert set(open_alerts()) == {("PTP T1", "down")}  # nothing for the APs behind it
    assert mgr.reconcile() == []  # no duplicates

    later = utcnow() + timedelta(minutes=61)
    out = mgr.reconcile(later)
    assert len(out) == 1 and out[0].kind == "repeat" and "STILL" in out[0].text
    with SessionLocal() as db:
        a = db.scalars(select(Alert)).one()
        a.acknowledged_at, a.acknowledged_by = utcnow(), "admin"
        db.commit()
    assert mgr.reconcile(later + timedelta(minutes=61)) == []  # acknowledged: no more reminders

    for k in ("ptp", "ap0", "ap1"):
        set_dev(ids[k], status=Status.UP)
    out = mgr.reconcile(later + timedelta(minutes=62))
    assert len(out) == 1 and out[0].kind == "recovery" and "RESOLVED DOWN: PTP T1" in out[0].text
    assert open_alerts() == {}


def test_warning_delay(mgr):
    ids = tree()
    set_dev(ids["core"], status=Status.WARNING, last_latency_ms=180.0, last_loss_pct=0.0)
    assert mgr.reconcile() == []  # degraded: wait 5 minutes
    assert ("Core", "degraded") in open_alerts()
    out = mgr.reconcile(utcnow() + timedelta(minutes=6))
    assert len(out) == 1 and "DEGRADED" in out[0].text and "latency 180 ms" in out[0].text
    # A short spike that goes away before the delay sends nothing, not even "resolved".
    set_dev(ids["ptp"], status=Status.WARNING, last_latency_ms=150.0)
    mgr.reconcile()
    set_dev(ids["ptp"], status=Status.UP)
    assert all("PTP" not in o.text for o in mgr.reconcile())


def test_maintenance_suppresses_notifications(mgr):
    ids = tree()
    with SessionLocal() as db:
        db.get(Site, ids["t1"]).maintenance_until = utcnow() + timedelta(hours=1)
        db.commit()
    set_dev(ids["ptp"], status=Status.DOWN)
    assert mgr.reconcile() == []
    assert open_alerts()[("PTP T1", "down")].in_maintenance
    out = mgr.reconcile(utcnow() + timedelta(hours=2))  # maintenance over, still down
    assert len(out) == 1 and "DOWN" in out[0].text


def test_metric_alerts_with_hysteresis_and_stale_data(mgr):
    ids = tree()
    now = utcnow()
    set_dev(ids["core"], cpu_pct=92.0, temperature=75.0, last_polled=now)
    set_dev(ids["ptp"], signal_dbm=-80.0, last_polled=now)
    set_dev(ids["ap0"], signal_dbm=-85.0, last_polled=now)  # APs: no weak-signal alert
    mgr.reconcile()
    assert {("Core", "high_cpu"), ("Core", "high_temperature"), ("PTP T1", "weak_signal")} == set(open_alerts())
    set_dev(ids["core"], cpu_pct=82.0, last_polled=now)  # below 85 but within the 5% margin: stays open
    mgr.reconcile()
    assert ("Core", "high_cpu") in open_alerts()
    set_dev(ids["core"], cpu_pct=70.0, last_polled=now - timedelta(hours=1))  # stale data: don't resolve
    mgr.reconcile()
    assert ("Core", "high_cpu") in open_alerts()
    set_dev(ids["core"], cpu_pct=70.0, last_polled=utcnow())
    mgr.reconcile()
    assert ("Core", "high_cpu") not in open_alerts()


def test_broadcast_storm_from_routeros_counters(app, mgr):
    data = dict(FakeRouterOS().data)
    data["/interface/ethernet/print"] = [{"name": "ether1", "rx-broadcast": "1000", "rx-multicast": "0"}]
    srv = FakeRouterOS(data=data)
    box = app.state.secret_box
    with SessionLocal() as db:
        d = Device(name="Core", ip_address="127.0.0.1", username="admin", password_enc=box.encrypt("secret"),
                   api_port=srv.port, status=Status.UP)
        db.add(d)
        db.commit()
        dev_id = d.id
    poller = StatsPoller(SessionLocal, box)
    assert asyncio.run(poller.poll_device(dev_id)) == ""
    with SessionLocal() as db:  # first poll was 10 s ago; 50,000 broadcasts since = 5,000/s
        eth = db.scalars(select(DeviceInterface).where(DeviceInterface.name == "ether1")).one()
        eth.updated_at -= timedelta(seconds=10)
        db.commit()
    srv.data["/interface/ethernet/print"] = [{"name": "ether1", "rx-broadcast": "51000", "rx-multicast": "0"}]
    assert asyncio.run(poller.poll_device(dev_id)) == ""
    out = mgr.reconcile()
    assert len(out) == 1 and out[0].severity == "critical"
    assert "BROADCAST STORM" in out[0].text and "ether1" in out[0].text


def test_reboot_detected(app, mgr):
    ids = tree()
    with SessionLocal() as db:
        d = db.get(Device, ids["core"])
        d.uptime_s = 100_000
        store_stats(db, d, DeviceStats(uptime_s=120), utcnow())
        db.commit()
    out = mgr.reconcile()
    assert len(out) == 1 and "REBOOTED: Core" in out[0].text
    assert mgr.reconcile() == []  # once per reboot
    assert open_alerts() == {}


def test_whatsapp_delivery_filters_and_groups(app, mgr, monkeypatch):
    ids = tree()
    box = app.state.secret_box
    with SessionLocal() as db:
        s = load_settings(db)
        s.whatsapp_enabled, s.wa_phone_number_id, s.wa_token_enc = True, "PNID", box.encrypt("TOKEN")
        s.wa_provider = "meta"
        s.desktop_enabled = False
        save_settings(db, s)
        t1 = db.get(Site, ids["t1"])
        db.add_all([
            User(username="all", password_hash="x", phone="+961 70 111 111", whatsapp_alerts=True),
            User(username="t1only", password_hash="x", phone="0096170222222", whatsapp_alerts=True, alert_sites=[t1]),
            User(username="critonly", password_hash="x", phone="70333333", whatsapp_alerts=True,
                 alert_min_severity="critical"),
            User(username="off", password_hash="x", phone="70444444", whatsapp_alerts=False),
        ])
        db.commit()
    sent = []

    def fake_send(pnid, token, payload, api_version="v21.0", timeout=15):
        assert (pnid, token) == ("PNID", "TOKEN")
        if payload["to"] == "70333333":
            raise whatsapp.WhatsAppError("WhatsApp API error 400: bad number")
        sent.append(payload)
        return "wamid.1"

    monkeypatch.setattr(whatsapp, "send", fake_send)
    set_dev(ids["core"], status=Status.DOWN)  # POP site, critical
    set_dev(ids["ptp"], status=Status.UNREACHABLE)
    out = mgr.reconcile()
    set_dev(ids["core"], status=Status.UP)
    set_dev(ids["ptp"], status=Status.WARNING, last_latency_ms=200.0)
    out += mgr.reconcile(utcnow() + timedelta(minutes=10))  # recovery (POP); degraded (Tower 1) opens
    out += mgr.reconcile(utcnow() + timedelta(minutes=16))  # degraded has lasted 6 min: sent
    asyncio.run(mgr.deliver(out))

    by_to = {p["to"]: p for p in sent}
    assert set(by_to) == {"96170111111", "96170222222"}
    body = by_to["96170111111"]["template"]["components"][0]["parameters"][0]["text"]
    assert body.startswith("3 alerts: | ") and "\n" not in body  # grouped, no new lines in template params
    t1_body = by_to["96170222222"]["template"]["components"][0]["parameters"][0]["text"]
    assert "PTP T1" in t1_body and "Core" not in t1_body  # only their site
    with SessionLocal() as db:
        logs = db.scalars(select(NotificationLog)).all()
        assert sorted(l.ok for l in logs) == [False, True, True]
        assert "bad number" in [l for l in logs if not l.ok][0].error


def test_whatsapp_payload_and_errors(monkeypatch):
    assert whatsapp.normalize_number("+961 70-123 456") == "96170123456"
    p = whatsapp.build_payload("961", "line1\nline2", "template", "spike_alert", "ar")
    assert p["template"]["language"]["code"] == "ar"
    assert p["template"]["components"][0]["parameters"][0]["text"] == "line1 | line2"
    assert whatsapp.build_payload("961", "a\nb", "text", "t", "en")["text"]["body"] == "a\nb"

    def http_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(json.dumps({"error": {"message": "Invalid OAuth access token"}}).encode()))

    monkeypatch.setattr(whatsapp.urllib.request, "urlopen", http_error)
    with pytest.raises(whatsapp.WhatsAppError, match="401: Invalid OAuth access token"):
        whatsapp.send("PNID", "bad", p)
    with pytest.raises(whatsapp.WhatsAppError, match="not configured"):
        whatsapp.send("", "", p)


def test_alert_pages_ack_feed_and_settings(app, admin, mgr, monkeypatch):
    ids = tree()
    feed = admin.get("/api/alerts/feed").json()  # first poll of a page on a fresh install
    assert feed["active"] == 0 and feed["max_id"] == 0
    start = feed["max_id"]
    set_dev(ids["core"], status=Status.DOWN)
    mgr.reconcile()
    feed = admin.get(f"/api/alerts/feed?after={start}").json()
    assert feed["active"] == 1 and feed["critical"] == 1 and feed["new"][0]["label"] == "DOWN"
    page = admin.get("/alerts").text
    assert "Core" in page and "Acknowledge" in page
    alert_id = feed["new"][0]["id"]
    assert admin.post(f"/alerts/{alert_id}/ack", data={"csrf_token": admin.token},
                      follow_redirects=False).status_code == 303
    assert "✓ admin" in admin.get("/alerts").text
    assert "Core" in admin.get("/alerts?tab=history").text
    assert "DOWN" in admin.get(f"/devices/{ids['core']}").text

    # Maintenance from the device page and the sites list
    admin.post(f"/devices/{ids['ap0']}/maintenance", data={"csrf_token": admin.token, "hours": "4"})
    admin.post(f"/sites/{ids['t1']}/maintenance", data={"csrf_token": admin.token, "hours": "1"})
    with SessionLocal() as db:
        assert db.get(Device, ids["ap0"]).maintenance_until > utcnow()
        assert db.get(Site, ids["t1"]).maintenance_until > utcnow()
    assert "End maintenance" in admin.get("/sites").text

    # Settings: token is stored encrypted and never shown
    r = admin.post("/admin/alert-settings", data={
        "csrf_token": admin.token, "cpu_pct": "90", "temperature_c": "65", "signal_dbm": "-72",
        "broadcast_pps": "800", "poll_failed_minutes": "10", "warning_delay_minutes": "3", "repeat_minutes": "30",
        "notify_recovery": "on", "whatsapp_enabled": "on", "wa_provider": "meta",
        "wa_phone_number_id": "123456789012345", "wa_token": "SECRET-TOKEN",
        "wa_mode": "template", "wa_template": "spike_alert", "wa_language": "en", "wa_api_version": "v21.0",
        "desktop_min_severity": "critical"}, follow_redirects=False)
    assert r.status_code == 303
    page = admin.get("/admin/alert-settings").text
    assert "SECRET-TOKEN" not in page and "saved" in page
    with SessionLocal() as db:
        s = load_settings(db)
        assert s.cpu_pct == 90 and s.repeat_minutes == 30 and not s.desktop_enabled and not s.notify_reboot
        assert app.state.secret_box.decrypt(s.wa_token_enc) == "SECRET-TOKEN"

    monkeypatch.setattr(whatsapp, "send", lambda *a, **k: "wamid.TEST")
    r = admin.post("/admin/alert-settings/test-whatsapp", data={"csrf_token": admin.token, "phone": "+96170000000"})
    assert "wamid.TEST" in r.text


def test_user_whatsapp_preferences(app, admin):
    ids = tree()
    r = admin.post("/admin/users/new", data={
        "csrf_token": admin.token, "username": "noc", "role": "technician", "password": "Noc123456", "is_active": "on",
        "whatsapp_alerts": "on", "phone": "", "alert_min_severity": "critical"})
    assert r.status_code == 422  # needs a number
    r = admin.post("/admin/users/new", data={
        "csrf_token": admin.token, "username": "noc", "role": "technician", "password": "Noc123456", "is_active": "on",
        "whatsapp_alerts": "on", "phone": "+96170123456", "alert_min_severity": "critical",
        "alert_site_ids": [str(ids["t1"])]}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.username == "noc")).one()
        assert u.whatsapp_alerts and u.alert_min_severity == "critical" and [s.name for s in u.alert_sites] == ["Tower 1"]


def test_viewer_cannot_ack(app, admin, mgr):
    ids = tree()
    set_dev(ids["core"], status=Status.DOWN)
    mgr.reconcile()
    admin.post("/admin/users/new", data={"username": "view", "role": "viewer", "password": "View12345",
                                          "is_active": "on", "csrf_token": admin.token})
    admin.post("/logout", data={"csrf_token": admin.token})
    from .conftest import csrf, login
    login(admin, "view", "View12345")
    assert "Acknowledge" not in admin.get("/alerts").text
    with SessionLocal() as db:
        alert_id = db.scalars(select(Alert.id)).first()
    assert admin.post(f"/alerts/{alert_id}/ack", data={"csrf_token": csrf(admin, "/alerts")}).status_code == 403


def test_schedule_runs_in_background(app, mgr):
    ids = tree()
    set_dev(ids["core"], status=Status.DOWN)

    async def scenario():
        await mgr.schedule()
        await mgr.schedule()  # already running: not started twice
        await mgr._task
    asyncio.run(scenario())
    assert ("Core", "down") in open_alerts()


def test_meta_phone_number_id_must_not_be_a_phone_number(admin):
    base = {"csrf_token": admin.token, "whatsapp_enabled": "on", "wa_provider": "meta", "wa_mode": "template",
            "desktop_min_severity": "warning"}
    r = admin.post("/admin/alert-settings", data={**base, "wa_phone_number_id": "+96179040395"})
    assert r.status_code == 422 and "not your phone number" in r.text
    r = admin.post("/admin/alert-settings", data={**base, "wa_phone_number_id": "123456789012345"},
                   follow_redirects=False)
    assert r.status_code == 303


def test_callmebot_to_personal_numbers(app, admin, mgr, monkeypatch):
    ids = tree()
    box = app.state.secret_box
    with SessionLocal() as db:
        s = load_settings(db)
        s.whatsapp_enabled, s.wa_provider, s.desktop_enabled = True, "callmebot", False
        save_settings(db, s)
        db.add_all([User(username="owner", password_hash="x", phone="+961 79 040 395", whatsapp_alerts=True,
                         callmebot_key_enc=box.encrypt("123456")),
                    User(username="nokey", password_hash="x", phone="+961 70 000 000", whatsapp_alerts=True)])
        db.commit()
    urls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout):
        urls.append(url)
        return Resp(b"<p>Message queued. You will receive it in a few seconds.</p>")

    monkeypatch.setattr(whatsapp.urllib.request, "urlopen", fake_urlopen)
    set_dev(ids["core"], status=Status.DOWN)
    asyncio.run(mgr.deliver(mgr.reconcile()))
    assert len(urls) == 1 and "phone=%2B96179040395" in urls[0] and "apikey=123456" in urls[0]
    assert "DOWN" in urllib.parse.unquote_plus(urls[0])
    with SessionLocal() as db:
        logs = {l.recipient.split()[0]: l for l in db.scalars(select(NotificationLog))}
        assert logs["owner"].ok and not logs["nokey"].ok and "No CallMeBot API key" in logs["nokey"].error

    # Test button uses the saved key of the user with that number
    r = admin.post("/admin/alert-settings/test-whatsapp", data={"csrf_token": admin.token, "phone": "+96179040395"})
    assert "Test sent to +96179040395" in r.text and "apikey=123456" in urls[-1]

    monkeypatch.setattr(whatsapp.urllib.request, "urlopen", lambda url, timeout: Resp(b"APIKey is invalid"))
    with pytest.raises(whatsapp.WhatsAppError, match="APIKey is invalid"):
        whatsapp.send_callmebot("+96179040395", "bad", "hi")


def test_user_form_saves_callmebot_key_encrypted(app, admin):
    r = admin.post("/admin/users/new", data={
        "csrf_token": admin.token, "username": "me", "role": "admin", "password": "Me1234567", "is_active": "on",
        "whatsapp_alerts": "on", "phone": "+96179040395", "callmebot_key": "987654"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.username == "me")).one()
        assert u.callmebot_key_enc and "987654" not in u.callmebot_key_enc
        assert app.state.secret_box.decrypt(u.callmebot_key_enc) == "987654"
        uid = u.id
    assert "987654" not in admin.get(f"/admin/users/{uid}/edit").text


class _JsonResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_telegram_group_and_personal_chats(app, admin, mgr, monkeypatch):
    from spike_monitor.alerts import telegram
    ids = tree()
    box = app.state.secret_box
    with SessionLocal() as db:
        s = load_settings(db)
        s.telegram_enabled, s.tg_token_enc, s.tg_group_chat_id = True, box.encrypt("123:ABC"), "-100999"
        s.tg_group_min_severity, s.desktop_enabled = "critical", False
        save_settings(db, s)
        t1 = db.get(Site, ids["t1"])
        db.add_all([User(username="tech1", password_hash="x", telegram_alerts=True, telegram_chat_id="555",
                         alert_sites=[t1]),
                    User(username="nochat", password_hash="x", telegram_alerts=True, telegram_chat_id="")])
        db.commit()
    calls = []

    def fake_urlopen(req, timeout):
        params = urllib.parse.parse_qs(req.data.decode())
        calls.append((req.full_url, params["chat_id"][0], params["text"][0]))
        return _JsonResp(json.dumps({"ok": True, "result": {"message_id": 7}}).encode())

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    set_dev(ids["core"], status=Status.DOWN)  # POP, critical -> group only (tech1 is subscribed to Tower 1)
    set_dev(ids["ptp"], status=Status.UNREACHABLE)
    asyncio.run(mgr.deliver(mgr.reconcile()))
    assert [(c[1]) for c in calls] == ["-100999"] and "bot123:ABC/sendMessage" in calls[0][0]
    assert "DOWN: Core" in calls[0][2]

    calls.clear()
    set_dev(ids["core"], status=Status.UP)
    set_dev(ids["ptp"], status=Status.DOWN)  # Tower 1, critical -> group and tech1
    asyncio.run(mgr.deliver(mgr.reconcile()))
    chats = sorted(c[1] for c in calls)
    assert chats == ["-100999", "555"]
    tech_text = [c[2] for c in calls if c[1] == "555"][0]
    assert "PTP T1" in tech_text and "Core" not in tech_text  # only Tower 1 for tech1


def test_telegram_errors_and_find_chats(app, admin, monkeypatch):
    from spike_monitor.alerts import telegram

    def forbidden(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                     io.BytesIO(b'{"ok":false,"description":"Forbidden: bot was blocked by the user"}'))

    monkeypatch.setattr(telegram.urllib.request, "urlopen", forbidden)
    with pytest.raises(telegram.TelegramError, match="press Start"):
        telegram.send("1:A", "555", "hi")
    with pytest.raises(telegram.TelegramError, match="not configured"):
        telegram.send("", "555", "hi")

    updates = {"ok": True, "result": [
        {"message": {"chat": {"id": 555, "first_name": "Ali", "username": "ali_noc", "type": "private"}}},
        {"message": {"chat": {"id": -100999, "title": "NOC team", "type": "supergroup"}}},
        {"message": {"chat": {"id": 555, "first_name": "Ali", "type": "private"}}},
    ]}
    monkeypatch.setattr(telegram.urllib.request, "urlopen",
                        lambda req, timeout: _JsonResp(json.dumps(updates).encode()))
    chats = {c["id"]: c for c in telegram.find_chats("1:A")}
    assert set(chats) == {"555", "-100999"} and chats["-100999"]["name"] == "NOC team"

    # Through the settings page
    r = admin.post("/admin/alert-settings", data={"csrf_token": admin.token, "tg_token": "not-a-token",
                                                  "desktop_min_severity": "warning", "wa_mode": "template"})
    assert r.status_code == 422 and "BotFather" in r.text
    r = admin.post("/admin/alert-settings", data={"csrf_token": admin.token, "tg_token": "123:ABC",
                                                  "telegram_enabled": "on", "tg_group_min_severity": "critical",
                                                  "desktop_min_severity": "warning", "wa_mode": "template"},
                   follow_redirects=False)
    assert r.status_code == 303
    page = admin.get("/admin/alert-settings/telegram-chats").text
    assert "NOC team" in page and "-100999" in page and "123:ABC" not in page
    with SessionLocal() as db:
        assert app.state.secret_box.decrypt(load_settings(db).tg_token_enc) == "123:ABC"
    r = admin.post("/admin/alert-settings/test-telegram", data={"csrf_token": admin.token, "chat_id": "555"})
    assert "Test sent to Telegram chat 555" in r.text


def test_user_telegram_fields(app, admin):
    base = {"csrf_token": admin.token, "username": "t", "role": "viewer", "password": "View12345", "is_active": "on",
            "telegram_alerts": "on"}
    assert admin.post("/admin/users/new", data={**base, "telegram_chat_id": ""}).status_code == 422
    assert admin.post("/admin/users/new", data={**base, "telegram_chat_id": "@ali"}).status_code == 422
    assert admin.post("/admin/users/new", data={**base, "telegram_chat_id": "555"},
                      follow_redirects=False).status_code == 303
