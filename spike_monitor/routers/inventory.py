"""Inventory pages: sites, VLANs, credential and port profiles, customers, devices."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..crud import Crud
from ..forms import Field, choices
from ..models import (
    DEFAULT_PORTS,
    Brand,
    CredentialProfile,
    Customer,
    Device,
    DeviceType,
    LinkType,
    PortProfile,
    Role,
    Site,
    Vlan,
)
from ..security import SecretBox

PORT_FIELDS = list(DEFAULT_PORTS)
PORT_LABELS = {
    "winbox_port": "Winbox port",
    "ssh_port": "SSH port",
    "api_port": "API port",
    "api_ssl_port": "API-SSL port",
    "http_port": "HTTP port",
    "https_port": "HTTPS port",
}


def site_options(db: Session):
    return [(str(s.id), s.name) for s in db.scalars(select(Site).order_by(Site.name))]


def vlan_options(db: Session):
    return [(str(v.id), f"{v.vlan_id} – {v.name}" + (f" ({v.site.name})" if v.site else ""))
            for v in db.scalars(select(Vlan).order_by(Vlan.vlan_id))]


def credential_options(db: Session):
    return [(str(c.id), f"{c.name} ({c.username})")
            for c in db.scalars(select(CredentialProfile).order_by(CredentialProfile.name))]


def port_profile_options(db: Session):
    return [(str(p.id), p.name) for p in db.scalars(select(PortProfile).order_by(PortProfile.name))]


def device_options(db: Session):
    return [(str(d.id), f"{d.name} ({d.ip_address})") for d in db.scalars(select(Device).order_by(Device.name))]


def _fk_exists(db: Session, model, value) -> bool:
    return value is None or db.get(model, value) is not None


def _unique_name(model, label: str):
    def check(db: Session, values: dict, obj) -> dict[str, str]:
        stmt = select(model).where(func.lower(model.name) == values["name"].lower())
        if obj is not None:
            stmt = stmt.where(model.id != obj.id)
        return {"name": f"A {label} with this name already exists."} if db.scalars(stmt).first() else {}

    return check


# ---------------------------------------------------------------- Sites
sites = Crud(
    model=Site,
    prefix="/sites",
    title="Sites",
    singular="Site",
    description="Towers, POPs, cabinets or any location. Add as many as you need.",
    order_by=Site.name,
    fields=[
        Field("name", "Name", required=True),
        Field("code", "Short code", placeholder="e.g. TWR-01"),
        Field("address", "Address / location"),
        Field("latitude", "Latitude", kind="float", min_value=-90, max_value=90),
        Field("longitude", "Longitude", kind="float", min_value=-180, max_value=180),
        Field("notes", "Notes", kind="textarea"),
    ],
    columns=[
        ("Name", lambda s: s.name),
        ("Code", lambda s: s.code),
        ("Address", lambda s: s.address),
        ("Devices", lambda s: len(s.devices)),
    ],
    validate=_unique_name(Site, "site"),
    row_actions="generic/site_actions.html",
)


# ---------------------------------------------------------------- VLANs
def _validate_vlan(db: Session, values: dict, obj) -> dict[str, str]:
    errors = {}
    if not _fk_exists(db, Site, values.get("site_id")):
        errors["site_id"] = "Unknown site."
    site_id = values.get("site_id")
    same_site = Vlan.site_id.is_(None) if site_id is None else Vlan.site_id == site_id
    stmt = select(Vlan).where(Vlan.vlan_id == values["vlan_id"], same_site)
    if obj is not None:
        stmt = stmt.where(Vlan.id != obj.id)
    if db.scalars(stmt).first():
        errors["vlan_id"] = "This VLAN ID already exists for this site."
    return errors


vlans = Crud(
    model=Vlan,
    prefix="/vlans",
    title="VLANs",
    singular="VLAN",
    description="Create VLANs globally or per site, then attach them to devices.",
    order_by=Vlan.vlan_id,
    fields=[
        Field("vlan_id", "VLAN ID", kind="int", required=True, min_value=1, max_value=4094),
        Field("name", "Name", required=True),
        Field("purpose", "Purpose", kind="select",
              options=choices(["management", "customers", "pppoe", "uplink", "voice", "other"])),
        Field("subnet", "Subnet", placeholder="e.g. 10.10.0.0/24"),
        Field("site_id", "Site", kind="select", options=site_options, help="Leave empty for a network-wide VLAN."),
        Field("notes", "Notes", kind="textarea"),
    ],
    columns=[
        ("VLAN ID", lambda v: v.vlan_id),
        ("Name", lambda v: v.name),
        ("Purpose", lambda v: (v.purpose or "").title()),
        ("Subnet", lambda v: v.subnet),
        ("Site", lambda v: v.site.name if v.site else "All sites"),
    ],
    validate=_validate_vlan,
)


# ---------------------------------------------------------------- Credential profiles
def _apply_credentials(db: Session, obj: CredentialProfile, values: dict, box: SecretBox) -> None:
    obj.name = values["name"]
    obj.username = values["username"]
    obj.notes = values["notes"]
    if values.get("password"):
        obj.password_enc = box.encrypt(values["password"])


credentials = Crud(
    model=CredentialProfile,
    prefix="/credentials",
    title="Credential profiles",
    singular="Credential profile",
    description="Reusable logins. Passwords are stored encrypted and are never shown in the app.",
    order_by=CredentialProfile.name,
    view_role=Role.ADMIN,
    edit_role=Role.ADMIN,
    fields=[
        Field("name", "Profile name", required=True),
        Field("username", "Username", required=True),
        Field("password", "Password", kind="password", help="Leave empty to keep the current password."),
        Field("notes", "Notes", kind="textarea"),
    ],
    columns=[
        ("Name", lambda c: c.name),
        ("Username", lambda c: c.username),
        ("Password", lambda c: "•••••• (set)" if c.password_enc else "not set"),
    ],
    validate=_unique_name(CredentialProfile, "profile"),
    apply=_apply_credentials,
    initial=lambda c: {"name": c.name, "username": c.username, "password": "", "notes": c.notes},
)


# ---------------------------------------------------------------- Port profiles
ports = Crud(
    model=PortProfile,
    prefix="/port-profiles",
    title="Port profiles",
    singular="Port profile",
    description="Named sets of service ports (Winbox, SSH, API…). Apply one to a device instead of typing ports.",
    order_by=PortProfile.name,
    fields=[Field("name", "Profile name", required=True)]
    + [Field(p, PORT_LABELS[p], kind="port", required=True, default=DEFAULT_PORTS[p]) for p in PORT_FIELDS],
    columns=[("Name", lambda p: p.name)] + [(PORT_LABELS[k].replace(" port", ""), (lambda k: lambda p: getattr(p, k))(k))
                                             for k in PORT_FIELDS],
    validate=_unique_name(PortProfile, "profile"),
)


# ---------------------------------------------------------------- Customers
def _validate_customer(db: Session, values: dict, obj) -> dict[str, str]:
    errors = {}
    if not _fk_exists(db, Site, values.get("site_id")):
        errors["site_id"] = "Unknown site."
    ap = db.get(Device, values["access_point_id"]) if values.get("access_point_id") else None
    if values.get("access_point_id") and ap is None:
        errors["access_point_id"] = "Unknown device."
    elif ap is not None and values.get("site_id") and ap.site_id != values["site_id"]:
        errors["access_point_id"] = "This device is not in the chosen site."
    return errors


def device_sites(db: Session) -> dict[str, dict[str, str]]:
    return {str(d.id): {"site": str(d.site_id or "")} for d in db.scalars(select(Device))}


customers = Crud(
    model=Customer,
    prefix="/customers",
    title="Customers",
    singular="Customer",
    description="Customer MAC list. Stored for reference now; customer monitoring comes in a later phase.",
    order_by=Customer.name,
    fields=[
        Field("name", "Name", required=True),
        Field("mac_address", "MAC address", kind="mac", required=True),
        Field("phone", "Phone"),
        Field("site_id", "Site", kind="select", options=site_options),
        Field("access_point_id", "Access point / device", kind="select", options=device_options,
              option_attrs=device_sites, help="Shows only the devices of the chosen site."),
        Field("notes", "Notes", kind="textarea"),
    ],
    columns=[
        ("Name", lambda c: c.name),
        ("MAC", lambda c: c.mac_address),
        ("Phone", lambda c: c.phone),
        ("Site", lambda c: c.site.name if c.site else ""),
        ("Access point", lambda c: c.access_point.name if c.access_point else ""),
    ],
    validate=_validate_customer,
    templates={"form_script": "customers/form_script.html"},
)


# ---------------------------------------------------------------- Devices
def _descendant_ids(device: Device) -> set[int]:
    found: set[int] = set()
    stack = list(device.children)
    while stack:
        node = stack.pop()
        if node.id in found:
            continue
        found.add(node.id)
        stack.extend(node.children)
    return found


def _parent_options_for(device: Device | None):
    def options(db: Session):
        blocked = {device.id} | _descendant_ids(device) if device else set()
        return [(k, v) for k, v in device_options(db) if int(k) not in blocked]

    return options


DEVICE_FIELDS_BASE = [
    Field("name", "Name", required=True, group="General"),
    Field("ip_address", "IP address / hostname", kind="ip", required=True, group="General"),
    Field("brand", "Brand", kind="select", required=True, options=choices(Brand.ALL),
          default=Brand.MIKROTIK, group="General"),
    Field("device_type", "Device type", kind="select", required=True, options=choices(DeviceType.ALL),
          default=DeviceType.ROUTER, group="General"),
    Field("site_id", "Site", kind="select", options=site_options, group="General"),
]

DEVICE_FIELDS_REST = [
    Field("vlan_ids", "VLANs", kind="multiselect", options=vlan_options, group="General",
          help="Hold Ctrl to select several."),
    Field("mac_address", "MAC address", kind="mac", group="Details"),
    Field("model", "Model", group="Details", placeholder="e.g. RB4011, LiteBeam 5AC"),
    Field("serial_number", "Serial number", group="Details"),
    Field("os_version", "Firmware / RouterOS version", group="Details", placeholder="e.g. 7.15.3 or 6.49.17"),
    Field("notes", "Notes", kind="textarea", group="Details"),
    Field("credential_profile_id", "Credential profile", kind="select", options=credential_options, group="Login",
          help="Use a shared login, or fill username/password below for this device only."),
    Field("username", "Username (this device only)", group="Login"),
    Field("password", "Password (this device only)", kind="password", group="Login",
          help="Leave empty to keep the current password."),
    Field("clear_password", "Remove the device-specific password", kind="bool", group="Login"),
    Field("port_profile_id", "Apply port profile", kind="select", options=port_profile_options, group="Ports",
          help="Choosing a profile overwrites the ports below when you save."),
] + [Field(p, PORT_LABELS[p], kind="port", required=True, default=DEFAULT_PORTS[p], group="Ports")
     for p in PORT_FIELDS] + [
    Field("use_api", "Use RouterOS API", kind="bool", default=True, group="Access methods"),
    Field("use_api_ssl", "Use API-SSL instead of plain API", kind="bool", default=False, group="Access methods"),
    Field("use_ssh", "Use SSH", kind="bool", default=True, group="Access methods"),
    Field("monitoring_enabled", "Monitoring enabled", kind="bool", default=True, group="Monitoring"),
    Field("latency_warn_ms", "Latency warning (ms)", kind="float", required=True, default=100.0, min_value=1,
          group="Monitoring"),
    Field("loss_warn_pct", "Packet loss warning (%)", kind="float", required=True, default=20.0, min_value=0,
          max_value=100, group="Monitoring"),
]


COMMON_PORTS = ["ether1", "ether2", "ether3", "ether4", "ether5", "sfp1", "sfp-sfpplus1", "combo1",
                "wlan1", "wlan2", "wifi1", "wifi2", "bridge", "eth0", "ath0", "br0"]
UPLINK_FIELDS = ("uplink_type", "uplink_port", "local_port")


def _device_extra_fields(db: Session, obj: Device | None) -> list[Field]:
    parent = Field("parent_id", "Parent device (uplink)", kind="select", options=_parent_options_for(obj),
                   group="Connection", help="The device this one connects through. Used for the map and smart alerts.")
    own_ports = [i.name for i in obj.interfaces] if obj else []
    parent_ports = [i.name for i in obj.parent.interfaces] if obj and obj.parent else []
    connection = [
        Field("uplink_type", "Connected by", kind="select", group="Connection",
              options=[(LinkType.WIRED, "Wired (cable)"), (LinkType.WIRELESS, "Wireless"), (LinkType.FIBER, "Fiber")],
              help="Leave empty to detect it automatically from the parent MikroTik."),
        Field("uplink_port", "Parent's port / interface", kind="combo", group="Connection",
              placeholder="e.g. ether3 or wlan1", suggestions=parent_ports or COMMON_PORTS,
              help="Where the cable or radio link plugs into the parent."),
        Field("local_port", "This device's port / interface", kind="combo", group="Connection",
              placeholder="e.g. ether1", suggestions=own_ports or COMMON_PORTS),
    ]
    return [parent] + connection + DEVICE_FIELDS_REST


def _validate_device(db: Session, values: dict, obj: Device | None) -> dict[str, str]:
    errors = {}
    if not _fk_exists(db, Site, values.get("site_id")):
        errors["site_id"] = "Unknown site."
    if not _fk_exists(db, CredentialProfile, values.get("credential_profile_id")):
        errors["credential_profile_id"] = "Unknown profile."
    if not _fk_exists(db, PortProfile, values.get("port_profile_id")):
        errors["port_profile_id"] = "Unknown profile."
    parent_id = values.get("parent_id")
    if parent_id is not None:
        if db.get(Device, parent_id) is None:
            errors["parent_id"] = "Unknown device."
        elif obj is not None and (parent_id == obj.id or parent_id in _descendant_ids(obj)):
            errors["parent_id"] = "A device cannot be connected through itself or its own children."
    known = set(db.scalars(select(Vlan.id)))
    if any(v not in known for v in values.get("vlan_ids", [])):
        errors["vlan_ids"] = "Unknown VLAN."
    stmt = select(Device).where(Device.ip_address == values["ip_address"])
    if obj is not None:
        stmt = stmt.where(Device.id != obj.id)
    if db.scalars(stmt).first():
        errors["ip_address"] = "Another device already uses this IP address."
    return errors


def _apply_device(db: Session, obj: Device, values: dict, box: SecretBox) -> None:
    skip = {"vlan_ids", "password", "clear_password", "port_profile_id"}
    if values.get("parent_id") is None:
        values.update(uplink_type="", uplink_port="", local_port="")
    new_uplink = tuple((values.get(k) or "").strip() for k in UPLINK_FIELDS)
    old_uplink = tuple((getattr(obj, k, None) or "") for k in UPLINK_FIELDS)
    if obj.uplink_auto and obj.parent_id != values.get("parent_id") and new_uplink == old_uplink:
        new_uplink = ("", "", "")  # detected ports belonged to the old parent
    if not any(new_uplink):
        obj.uplink_auto = True  # empty again: let detection fill it
    elif new_uplink != old_uplink or obj.parent_id != values.get("parent_id"):
        obj.uplink_auto = False  # set by hand: detection leaves it alone
    obj.uplink_type, obj.uplink_port, obj.local_port = new_uplink
    for f in DEVICE_FIELDS_BASE + DEVICE_FIELDS_REST + [Field("parent_id", "")]:
        if f.name not in skip:
            setattr(obj, f.name, values.get(f.name))
    if values.get("clear_password"):
        obj.password_enc = ""
    elif values.get("password"):
        obj.password_enc = box.encrypt(values["password"])
    if values.get("port_profile_id"):
        profile = db.get(PortProfile, values["port_profile_id"])
        for p in PORT_FIELDS:
            setattr(obj, p, getattr(profile, p))
    obj.vlans = [db.get(Vlan, v) for v in values.get("vlan_ids", [])]
    if not obj.monitoring_enabled:
        obj.status = "paused"
    elif obj.status == "paused":
        obj.status = "unknown"


def _device_initial(d: Device) -> dict:
    values = {f.name: getattr(d, f.name, None) for f in DEVICE_FIELDS_BASE + DEVICE_FIELDS_REST}
    values.update(parent_id=d.parent_id, vlan_ids=[v.id for v in d.vlans], password="",
                  clear_password=False, port_profile_id=None,
                  uplink_type=d.uplink_type or None, uplink_port=d.uplink_port, local_port=d.local_port)
    return values


devices = Crud(
    model=Device,
    prefix="/devices",
    title="Devices",
    singular="Device",
    order_by=Device.name,
    fields=DEVICE_FIELDS_BASE,
    extra_fields=_device_extra_fields,
    columns=[],
    validate=_validate_device,
    apply=_apply_device,
    initial=_device_initial,
    detail_url=lambda d: f"/devices/{d.id}",
    templates={"list": "devices/list.html", "form_script": "devices/form_script.html"},
)

ALL_CRUDS = [sites, vlans, credentials, ports, customers, devices]
