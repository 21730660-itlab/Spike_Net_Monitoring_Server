"""Password-protected export / import of the whole setup (inventory, logins, settings).

File format (JSON): {"format": "spike-net-export", "version": 1, "kdf": {...}, "data": "<Fernet token>"}.
The key is derived from the password with PBKDF2-SHA256 (600,000 rounds), so the file is useless
without the password, and the server's own secret.key is not needed to read it.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .models import AppSetting, CredentialProfile, Customer, Device, PortProfile, Site, User, Vlan
from .security import SecretBox

FORMAT = "spike-net-export"
ITERATIONS = 600_000

SITE_FIELDS = ("name", "code", "address", "latitude", "longitude", "notes", "map_x", "map_y")
VLAN_FIELDS = ("vlan_id", "name", "purpose", "subnet", "notes")
PORT_FIELDS = ("name", "winbox_port", "ssh_port", "api_port", "api_ssl_port", "http_port", "https_port")
DEVICE_FIELDS = (
    "name", "ip_address", "brand", "device_type", "uplink_type", "uplink_port", "local_port", "uplink_auto",
    "mac_address", "model", "serial_number", "os_version", "notes", "username", "winbox_port", "ssh_port",
    "api_port", "api_ssl_port", "http_port", "https_port", "use_api", "use_api_ssl", "use_ssh",
    "monitoring_enabled", "latency_warn_ms", "loss_warn_pct", "ssh_host_key", "map_x", "map_y",
)
CUSTOMER_FIELDS = ("name", "mac_address", "phone", "notes")
USER_FIELDS = ("username", "full_name", "password_hash", "role", "phone", "is_active", "must_change_password",
               "whatsapp_alerts", "alert_min_severity", "telegram_alerts", "telegram_chat_id")
SECRET_SETTINGS = {"alerts": ("wa_token_enc", "tg_token_enc")}


class TransferError(ValueError):
    pass


def _key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _pick(obj, names) -> dict:
    return {n: getattr(obj, n) for n in names}


def build_payload(db: Session, box: SecretBox, include_users: bool) -> dict:
    sites = db.scalars(select(Site)).all()
    payload = {
        "sites": [_pick(s, SITE_FIELDS) for s in sites],
        "vlans": [{**_pick(v, VLAN_FIELDS), "site": v.site.name if v.site else None} for v in db.scalars(select(Vlan))],
        "port_profiles": [_pick(p, PORT_FIELDS) for p in db.scalars(select(PortProfile))],
        "credential_profiles": [{"name": c.name, "username": c.username, "password": box.decrypt(c.password_enc),
                                 "notes": c.notes} for c in db.scalars(select(CredentialProfile))],
        "devices": [{**_pick(d, DEVICE_FIELDS), "password": box.decrypt(d.password_enc),
                     "site": d.site.name if d.site else None, "parent_ip": d.parent.ip_address if d.parent else None,
                     "credential_profile": d.credential_profile.name if d.credential_profile else None,
                     "vlans": [[v.vlan_id, v.site.name if v.site else None] for v in d.vlans]}
                    for d in db.scalars(select(Device))],
        "customers": [{**_pick(c, CUSTOMER_FIELDS), "site": c.site.name if c.site else None,
                       "access_point_ip": c.access_point.ip_address if c.access_point else None}
                      for c in db.scalars(select(Customer))],
        "settings": {},
        "users": [],
    }
    for row in db.scalars(select(AppSetting)):
        try:
            value = json.loads(row.value)
        except ValueError:
            continue
        if isinstance(value, dict):
            for secret in SECRET_SETTINGS.get(row.key, ()):
                if value.get(secret):
                    value[secret] = {"plain": box.decrypt(value[secret])}
        payload["settings"][row.key] = value
    if include_users:
        payload["users"] = [{**_pick(u, USER_FIELDS), "callmebot_key": box.decrypt(u.callmebot_key_enc),
                             "alert_sites": [s.name for s in u.alert_sites]} for u in db.scalars(select(User))]
    return payload


def encrypt_export(payload: dict, password: str) -> bytes:
    if len(password) < 8:
        raise TransferError("Use a password of at least 8 characters for the export file.")
    salt = os.urandom(16)
    token = Fernet(_key(password, salt)).encrypt(json.dumps(payload, default=str).encode())
    doc = {"format": FORMAT, "version": 1, "app_version": __version__,
           "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "kdf": {"name": "pbkdf2-sha256", "iterations": ITERATIONS, "salt": base64.b64encode(salt).decode()},
           "data": token.decode()}
    return json.dumps(doc, indent=1).encode()


def decrypt_export(raw: bytes, password: str) -> dict:
    try:
        doc = json.loads(raw.decode("utf-8"))
        if doc.get("format") != FORMAT:
            raise ValueError
        salt = base64.b64decode(doc["kdf"]["salt"])
        iterations = int(doc["kdf"].get("iterations", ITERATIONS))
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise TransferError("This is not a Spike Net export file.") from exc
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
    try:
        return json.loads(Fernet(key).decrypt(doc["data"].encode()))
    except InvalidToken as exc:
        raise TransferError("Wrong password for this export file.") from exc


def import_payload(db: Session, box: SecretBox, p: dict, include_users: bool, include_settings: bool) -> dict:
    """Merge into the current data: add what is missing, update what matches (sites, profiles and users
    by name, VLANs by ID + site, devices by IP, customers by MAC). Nothing is deleted."""
    counts = {k: 0 for k in ("sites", "vlans", "port_profiles", "credential_profiles", "devices", "customers",
                             "users", "settings")}
    sites = {s.name: s for s in db.scalars(select(Site))}
    for item in p.get("sites", []):
        site = sites.get(item["name"]) or Site(name=item["name"])
        for k in SITE_FIELDS:
            if k in item:
                setattr(site, k, item[k])
        db.add(site)
        sites[site.name] = site
        counts["sites"] += 1
    db.flush()

    def site_id(name):
        return sites[name].id if name in sites else None

    vlans = {(v.vlan_id, v.site_id): v for v in db.scalars(select(Vlan))}
    for item in p.get("vlans", []):
        key = (item["vlan_id"], site_id(item.get("site")))
        vlan = vlans.get(key) or Vlan(vlan_id=item["vlan_id"], site_id=key[1], name=item.get("name", ""))
        for k in VLAN_FIELDS:
            if k in item:
                setattr(vlan, k, item[k])
        db.add(vlan)
        vlans[key] = vlan
        counts["vlans"] += 1

    ports = {x.name: x for x in db.scalars(select(PortProfile))}
    for item in p.get("port_profiles", []):
        prof = ports.get(item["name"]) or PortProfile(name=item["name"])
        for k in PORT_FIELDS:
            setattr(prof, k, item.get(k, getattr(prof, k)))
        db.add(prof)
        counts["port_profiles"] += 1

    creds = {c.name: c for c in db.scalars(select(CredentialProfile))}
    for item in p.get("credential_profiles", []):
        c = creds.get(item["name"]) or CredentialProfile(name=item["name"], username=item.get("username", ""))
        c.username, c.notes = item.get("username", ""), item.get("notes", "")
        c.password_enc = box.encrypt(item.get("password", ""))
        db.add(c)
        creds[c.name] = c
        counts["credential_profiles"] += 1
    db.flush()

    devices = {d.ip_address: d for d in db.scalars(select(Device))}
    for item in p.get("devices", []):
        d = devices.get(item["ip_address"]) or Device(ip_address=item["ip_address"], name=item.get("name", ""))
        for k in DEVICE_FIELDS:
            if k in item:
                setattr(d, k, item[k])
        d.password_enc = box.encrypt(item.get("password", ""))
        d.site_id = site_id(item.get("site"))
        cp = creds.get(item.get("credential_profile") or "")
        d.credential_profile_id = cp.id if cp else None
        d.vlans = [vlans[(vid, site_id(sname))] for vid, sname in item.get("vlans", [])
                   if (vid, site_id(sname)) in vlans]
        db.add(d)
        devices[d.ip_address] = d
        counts["devices"] += 1
    db.flush()
    for item in p.get("devices", []):  # parents after every device exists
        parent = devices.get(item.get("parent_ip") or "")
        devices[item["ip_address"]].parent_id = parent.id if parent and parent.ip_address != item["ip_address"] else None

    customers = {c.mac_address: c for c in db.scalars(select(Customer))}
    for item in p.get("customers", []):
        c = customers.get(item["mac_address"]) or Customer(mac_address=item["mac_address"], name=item.get("name", ""))
        for k in CUSTOMER_FIELDS:
            if k in item:
                setattr(c, k, item[k])
        c.site_id = site_id(item.get("site"))
        ap = devices.get(item.get("access_point_ip") or "")
        c.access_point_id = ap.id if ap else None
        db.add(c)
        customers[c.mac_address] = c
        counts["customers"] += 1

    if include_users:
        existing = {u.username.lower() for u in db.scalars(select(User))}
        for item in p.get("users", []):
            if item["username"].lower() in existing:
                continue  # never overwrite an account that already exists here
            u = User(**{k: item[k] for k in USER_FIELDS if k in item})
            u.callmebot_key_enc = box.encrypt(item.get("callmebot_key", ""))
            u.alert_sites = [sites[n] for n in item.get("alert_sites", []) if n in sites]
            db.add(u)
            counts["users"] += 1

    if include_settings:
        for key, value in p.get("settings", {}).items():
            if not isinstance(value, dict):
                continue
            for secret in SECRET_SETTINGS.get(key, ()):
                if isinstance(value.get(secret), dict):
                    value[secret] = box.encrypt(value[secret].get("plain", ""))
            row = db.get(AppSetting, key) or AppSetting(key=key, value="")
            row.value = json.dumps(value)
            db.add(row)
            counts["settings"] += 1
    db.flush()
    return counts
