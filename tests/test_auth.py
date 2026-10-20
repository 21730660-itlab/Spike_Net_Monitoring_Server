from sqlalchemy import select

from spike_monitor.db import SessionLocal
from spike_monitor.models import AuditLog, User

from .conftest import csrf, login


def test_unauthenticated_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/summary").status_code == 401


def test_bootstrap_admin_must_change_password(client):
    r = login(client)
    assert r.headers["location"] == "/account/password"
    # Every other page is blocked until the password is changed.
    r = client.get("/devices", follow_redirects=False)
    assert r.headers["location"] == "/account/password"


def test_weak_or_reused_password_rejected(client):
    login(client)
    token = csrf(client, "/account/password")
    for new in ("short1", "admin@2026", "onlyletters"):
        r = client.post("/account/password", data={"current_password": "admin@2026", "new_password": new,
                                                   "confirm_password": new, "csrf_token": token})
        assert r.status_code == 422


def test_change_password_then_full_access(admin):
    assert admin.get("/devices").status_code == 200
    with SessionLocal() as db:
        u = db.scalars(select(User)).one()
        assert not u.must_change_password


def test_wrong_password_and_lockout(client):
    for _ in range(5):
        assert login(client, password="nope").status_code == 401
    assert login(client).status_code == 429
    with SessionLocal() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.action == "login.failed")).first()


def test_csrf_required(client):
    r = client.post("/login", data={"username": "admin", "password": "admin@2026"})
    assert r.status_code == 400


def test_roles(admin):
    r = admin.post("/admin/users/new", data={"username": "tech", "role": "technician", "password": "Tech12345",
                                              "is_active": "on", "csrf_token": admin.token}, follow_redirects=False)
    assert r.status_code == 303
    admin.post("/admin/users/new", data={"username": "view", "role": "viewer", "password": "View12345",
                                          "is_active": "on", "csrf_token": admin.token})
    admin.post("/logout", data={"csrf_token": admin.token})

    login(admin, "tech", "Tech12345")
    assert admin.get("/devices/new").status_code == 200
    assert admin.get("/credentials").status_code == 403
    assert admin.get("/admin/users").status_code == 403
    admin.post("/logout", data={"csrf_token": csrf(admin, "/")})

    login(admin, "view", "View12345")
    assert admin.get("/devices").status_code == 200
    assert admin.get("/devices/new").status_code == 403


def test_last_admin_cannot_be_demoted(admin):
    with SessionLocal() as db:
        uid = db.scalars(select(User)).one().id
    r = admin.post(f"/admin/users/{uid}/edit", data={"username": "admin", "role": "viewer", "is_active": "on",
                                                     "csrf_token": admin.token})
    assert r.status_code == 422
