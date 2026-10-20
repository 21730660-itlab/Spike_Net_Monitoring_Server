import re

import pytest
from fastapi.testclient import TestClient

from spike_monitor.config import Settings
from spike_monitor.main import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    from spike_monitor.routers import auth
    auth._failures.clear()
    monkeypatch.setenv("SPIKE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPIKE_MONITORING_ENABLED", "0")
    return create_app(Settings())


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def csrf(client, url="/login"):
    html = client.get(url).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, username="admin", password="admin@2026"):
    token = csrf(client)
    return client.post("/login", data={"username": username, "password": password, "csrf_token": token},
                       follow_redirects=False)


@pytest.fixture
def admin(client):
    """Client logged in as admin, with the forced password change done."""
    login(client)
    token = csrf(client, "/account/password")
    r = client.post("/account/password", data={
        "current_password": "admin@2026", "new_password": "Str0ngPass", "confirm_password": "Str0ngPass",
        "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    client.token = csrf(client, "/")
    return client
