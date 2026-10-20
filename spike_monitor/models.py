from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Role:
    ADMIN = "admin"
    TECHNICIAN = "technician"
    VIEWER = "viewer"

    ALL = (ADMIN, TECHNICIAN, VIEWER)
    RANK = {VIEWER: 0, TECHNICIAN: 1, ADMIN: 2}


class Severity:
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

    ALL = (CRITICAL, WARNING, INFO)
    RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}


class Brand:
    MIKROTIK = "mikrotik"
    UBIQUITI = "ubiquiti"
    MIMOSA = "mimosa"
    OTHER = "other"

    ALL = (MIKROTIK, UBIQUITI, MIMOSA, OTHER)


class DeviceType:
    UPSTREAM = "upstream"
    ROUTER = "router"
    ACCESS_POINT = "access_point"
    PTP_LINK = "ptp_link"
    STATION = "station"
    SWITCH = "switch"
    SERVER = "server"
    OTHER = "other"

    ALL = (UPSTREAM, ROUTER, ACCESS_POINT, PTP_LINK, STATION, SWITCH, SERVER, OTHER)


class Status:
    UNKNOWN = "unknown"
    UP = "up"
    WARNING = "warning"
    DOWN = "down"
    # Parent device is down, so this device cannot be reached; no separate alert.
    UNREACHABLE = "unreachable"
    PAUSED = "paused"

    ALL = (UNKNOWN, UP, WARNING, DOWN, UNREACHABLE, PAUSED)


# Default service ports; every device can override them.
DEFAULT_PORTS = {
    "winbox_port": 8291,
    "ssh_port": 22,
    "api_port": 8728,
    "api_ssl_port": 8729,
    "http_port": 80,
    "https_port": 443,
}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default=Role.VIEWER)
    phone: Mapped[str] = mapped_column(String(32), default="")  # WhatsApp number, international format
    whatsapp_alerts: Mapped[bool] = mapped_column(Boolean, default=False)
    callmebot_key_enc: Mapped[str] = mapped_column(Text, default="")  # encrypted CallMeBot API key
    alert_min_severity: Mapped[str] = mapped_column(String(16), default=Severity.WARNING)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Sites this user gets WhatsApp alerts for; empty means all sites.
    alert_sites: Mapped[list[Site]] = relationship(secondary="user_alert_sites")

    def has_role(self, minimum: str) -> bool:
        return Role.RANK.get(self.role, -1) >= Role.RANK[minimum]


user_alert_sites = Table(
    "user_alert_sites",
    Base.metadata,
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("site_id", ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True),
)


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    code: Mapped[str] = mapped_column(String(32), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # Position on the topology map (saved when someone arranges the map)
    map_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    map_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Alerts for every device in the site are not sent until this time.
    maintenance_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    devices: Mapped[list[Device]] = relationship(back_populates="site")


device_vlans = Table(
    "device_vlans",
    Base.metadata,
    Column("device_id", ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
    Column("vlan_id", ForeignKey("vlans.id", ondelete="CASCADE"), primary_key=True),
)


class Vlan(Base):
    __tablename__ = "vlans"

    id: Mapped[int] = mapped_column(primary_key=True)
    vlan_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(64), default="")
    subnet: Mapped[str] = mapped_column(String(64), default="")
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    site: Mapped[Site | None] = relationship()


class CredentialProfile(Base):
    """A reusable login (username + encrypted password) shared by many devices."""

    __tablename__ = "credential_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(64))
    password_enc: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class PortProfile(Base):
    """A named set of service ports that can be applied to many devices."""

    __tablename__ = "port_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    winbox_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["winbox_port"])
    ssh_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["ssh_port"])
    api_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["api_port"])
    api_ssl_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["api_ssl_port"])
    http_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["http_port"])
    https_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["https_port"])


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    ip_address: Mapped[str] = mapped_column(String(64), index=True)
    brand: Mapped[str] = mapped_column(String(16), default=Brand.MIKROTIK)
    device_type: Mapped[str] = mapped_column(String(16), default=DeviceType.ROUTER)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"), nullable=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)

    # Inventory details (filled by hand now; "Import from device" in phase 2)
    mac_address: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    serial_number: Mapped[str] = mapped_column(String(64), default="")
    os_version: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    # Credentials: either a shared profile or device-specific login
    credential_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("credential_profiles.id", ondelete="SET NULL"), nullable=True
    )
    username: Mapped[str] = mapped_column(String(64), default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")

    # Service ports (they may differ per device)
    winbox_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["winbox_port"])
    ssh_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["ssh_port"])
    api_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["api_port"])
    api_ssl_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["api_ssl_port"])
    http_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["http_port"])
    https_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_PORTS["https_port"])
    use_api: Mapped[bool] = mapped_column(Boolean, default=True)
    use_api_ssl: Mapped[bool] = mapped_column(Boolean, default=False)
    use_ssh: Mapped[bool] = mapped_column(Boolean, default=True)

    # Monitoring
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_warn_ms: Mapped[float] = mapped_column(Float, default=100.0)
    loss_warn_pct: Mapped[float] = mapped_column(Float, default=20.0)
    status: Mapped[str] = mapped_column(String(16), default=Status.UNKNOWN, index=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Latest values read from the device (MikroTik API / Ubiquiti SSH)
    identity: Mapped[str] = mapped_column(String(128), default="")
    board_name: Mapped[str] = mapped_column(String(64), default="")
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    voltage: Mapped[float | None] = mapped_column(Float, nullable=True)
    uptime_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wireless_clients: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_dbm: Mapped[float | None] = mapped_column(Float, nullable=True)
    noise_dbm: Mapped[float | None] = mapped_column(Float, nullable=True)
    ccq_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_polled: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    poll_error: Mapped[str] = mapped_column(Text, default="")
    # SSH host key fingerprint, remembered on first connect (trust on first use)
    ssh_host_key: Mapped[str] = mapped_column(String(128), default="")
    map_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    map_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    maintenance_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reboot_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    site: Mapped[Site | None] = relationship(back_populates="devices")
    parent: Mapped[Device | None] = relationship(remote_side="Device.id", back_populates="children")
    children: Mapped[list[Device]] = relationship(back_populates="parent")
    credential_profile: Mapped[CredentialProfile | None] = relationship()
    vlans: Mapped[list[Vlan]] = relationship(secondary=device_vlans)
    interfaces: Mapped[list[DeviceInterface]] = relationship(
        cascade="all, delete-orphan", order_by="DeviceInterface.name")
    wireless_peers: Mapped[list[WirelessPeer]] = relationship(
        cascade="all, delete-orphan", order_by="WirelessPeer.signal_dbm.desc()")

    def login(self, box) -> tuple[str, str]:
        """Username and decrypted password: device-specific first, then the profile."""
        if self.username or self.password_enc:
            return self.username, box.decrypt(self.password_enc)
        if self.credential_profile is not None:
            return self.credential_profile.username, box.decrypt(self.credential_profile.password_enc)
        return "", ""

    def in_maintenance(self, now: datetime) -> bool:
        if self.maintenance_until and self.maintenance_until > now:
            return True
        return bool(self.site and self.site.maintenance_until and self.site.maintenance_until > now)

    def descendants(self) -> list[Device]:
        found: list[Device] = []
        seen = {self.id}
        stack = list(self.children)
        while stack:
            node = stack.pop()
            if node.id in seen:
                continue
            seen.add(node.id)
            found.append(node)
            stack.extend(node.children)
        return found

    def ancestors(self) -> list[Device]:
        """Parents up the chain, nearest first. Guards against loops."""
        chain: list[Device] = []
        seen = {self.id}
        node = self.parent
        while node is not None and node.id not in seen:
            chain.append(node)
            seen.add(node.id)
            node = node.parent
        return chain


class Customer(Base):
    """Customer record. Only stored for now; monitoring comes in a later phase."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    mac_address: Mapped[str] = mapped_column(String(32), index=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"), nullable=True)
    access_point_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")

    site: Mapped[Site | None] = relationship()
    access_point: Mapped[Device | None] = relationship()


class PingResult(Base):
    __tablename__ = "ping_results"
    __table_args__ = (Index("ix_ping_device_ts", "device_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_alive: Mapped[bool] = mapped_column(Boolean)
    avg_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    loss_pct: Mapped[float] = mapped_column(Float)


class StatusEvent(Base):
    """A device status change (up -> down, etc.). Alerts are built on these."""

    __tablename__ = "status_events"
    __table_args__ = (Index("ix_event_device_ts", "device_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    old_status: Mapped[str] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(String(255), default="")

    device: Mapped[Device] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(255), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class DeviceInterface(Base):
    """Current state of one interface (refreshed on every poll)."""

    __tablename__ = "device_interfaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    if_type: Mapped[str] = mapped_column(String(32), default="")
    mac_address: Mapped[str] = mapped_column(String(32), default="")
    comment: Mapped[str] = mapped_column(String(255), default="")
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rx_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    tx_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    rx_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tx_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rx_broadcast: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rx_multicast: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bcast_pps: Mapped[float | None] = mapped_column(Float, nullable=True)  # broadcast+multicast packets/s
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WirelessPeer(Base):
    """A station connected to an AP (or the remote side of a link), refreshed on every poll."""

    __tablename__ = "wireless_peers"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    interface: Mapped[str] = mapped_column(String(64), default="")
    mac_address: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    last_ip: Mapped[str] = mapped_column(String(64), default="")
    signal_dbm: Mapped[float | None] = mapped_column(Float, nullable=True)
    ccq_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tx_rate: Mapped[str] = mapped_column(String(64), default="")
    rx_rate: Mapped[str] = mapped_column(String(64), default="")
    uptime_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MetricSample(Base):
    """Raw device health sample, one per poll."""

    __tablename__ = "metric_samples"
    __table_args__ = (Index("ix_metric_device_ts", "device_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    voltage: Mapped[float | None] = mapped_column(Float, nullable=True)
    wireless_clients: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_dbm: Mapped[float | None] = mapped_column(Float, nullable=True)
    ccq_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class TrafficSample(Base):
    """Raw interface traffic rate, one per interface per poll."""

    __tablename__ = "traffic_samples"
    __table_args__ = (Index("ix_traffic_device_if_ts", "device_id", "interface", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    interface: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    rx_bps: Mapped[float] = mapped_column(Float)
    tx_bps: Mapped[float] = mapped_column(Float)


class HourlyRollup(Base):
    """Hourly averages kept for a year, so long graphs stay fast.

    ``series`` is e.g. "ping", "metrics" or "if:ether1". Unused value columns stay NULL.
    """

    __tablename__ = "hourly_rollups"
    __table_args__ = (Index("ix_rollup_device_series_hour", "device_id", "series", "hour", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    series: Mapped[str] = mapped_column(String(80))
    hour: Mapped[datetime] = mapped_column(DateTime)
    v1: Mapped[float | None] = mapped_column(Float, nullable=True)
    v2: Mapped[float | None] = mapped_column(Float, nullable=True)
    v3: Mapped[float | None] = mapped_column(Float, nullable=True)
    v4: Mapped[float | None] = mapped_column(Float, nullable=True)
    v1_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    v2_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    samples: Mapped[int] = mapped_column(Integer, default=0)


class Neighbor(Base):
    """A device seen by one of our MikroTiks (MNDP/CDP/LLDP), used to suggest new devices."""

    __tablename__ = "neighbors"

    id: Mapped[int] = mapped_column(primary_key=True)
    seen_by_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    interface: Mapped[str] = mapped_column(String(64), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    mac_address: Mapped[str] = mapped_column(String(32), default="")
    identity: Mapped[str] = mapped_column(String(128), default="")
    platform: Mapped[str] = mapped_column(String(64), default="")
    board: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[str] = mapped_column(String(64), default="")
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    seen_by: Mapped[Device] = relationship()


class CommandJob(Base):
    """A script pushed to one or more devices over SSH."""

    __tablename__ = "command_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    username: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(128), default="")
    script: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running, done
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    results: Mapped[list[CommandResult]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="CommandResult.id")


class CommandResult(Base):
    __tablename__ = "command_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("command_jobs.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)
    device_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending, ok, error
    output: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[CommandJob] = relationship(back_populates="results")
    device: Mapped[Device | None] = relationship()


class Alert(Base):
    """A problem on a device. Open while ``resolved_at`` is empty."""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_open", "device_id", "kind", "resolved_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(String(500))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    acknowledged_by: Mapped[str] = mapped_column(String(64), default="")
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=0)
    in_maintenance: Mapped[bool] = mapped_column(Boolean, default=False)

    device: Mapped[Device] = relationship()


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    channel: Mapped[str] = mapped_column(String(16))  # whatsapp, desktop
    recipient: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")


class AppSetting(Base):
    """Key/value settings edited in the app (stored as JSON text)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
