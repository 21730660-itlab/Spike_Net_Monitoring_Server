from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
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
    phone: Mapped[str] = mapped_column(String(32), default="")  # WhatsApp number (phase 3)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def has_role(self, minimum: str) -> bool:
        return Role.RANK.get(self.role, -1) >= Role.RANK[minimum]


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    code: Mapped[str] = mapped_column(String(32), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    site: Mapped[Site | None] = relationship(back_populates="devices")
    parent: Mapped[Device | None] = relationship(remote_side="Device.id", back_populates="children")
    children: Mapped[list[Device]] = relationship(back_populates="parent")
    credential_profile: Mapped[CredentialProfile | None] = relationship()
    vlans: Mapped[list[Vlan]] = relationship(secondary=device_vlans)

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
