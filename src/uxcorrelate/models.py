"""Normalized records shared by the data sources and the correlation engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class UserSample:
    """One Aternity activity / application measurement for a user."""

    timestamp: datetime
    username: str
    device: str
    application: str
    activity: str | None = None
    location: str | None = None
    ip_address: str | None = None
    response_time: float | None = None  # seconds
    client_time: float | None = None
    network_time: float | None = None
    backend_time: float | None = None
    status: str | None = None  # Aternity's own verdict on the activity (SLA_STATUS)


@dataclass
class DeviceHealthSample:
    """One Aternity device health measurement for the user's endpoint."""

    timestamp: datetime
    device: str
    cpu: float | None = None
    memory: float | None = None
    disk_queue: float | None = None
    free_disk: float | None = None  # OS drive, in Aternity's unit (same as EndpointInfo.total_disk)
    network_latency: float | None = None  # ms


@dataclass
class HealthEvent:
    """One Aternity device health event (crash, hang, error...)."""

    timestamp: datetime
    device: str
    application: str | None = None
    username: str | None = None
    category: str | None = None
    name: str | None = None
    component: str | None = None
    severity: str | None = None
    count: float | None = None
    component_version: str | None = None
    sub_component: str | None = None
    sub_component_version: str | None = None
    exception: str | None = None
    error: str | None = None
    details: str | None = None


@dataclass
class SoftwareChange:
    """One install, update or removal on a device, as Aternity logged it."""

    timestamp: datetime
    device: str
    name: str | None = None
    change: str | None = None  # Aternity's change type, as it names it
    version: str | None = None
    version_before: str | None = None
    vendor: str | None = None


@dataclass
class ProcessSample:
    """One process's resource use on a device at one time (Aternity's values)."""

    timestamp: datetime
    device: str
    process: str
    application: str | None = None
    cpu: float | None = None
    memory: float | None = None
    io_read: float | None = None
    io_write: float | None = None


@dataclass
class AppUsageSample:
    """Aternity per-application experience on one device: UXI, crashes, hang time."""

    timestamp: datetime
    device: str
    application: str
    uxi: float | None = None
    crashes: float | None = None
    hang_time: float | None = None


@dataclass
class BootSample:
    """One Windows boot or logon measured by Aternity (type as Aternity names it)."""

    timestamp: datetime
    device: str
    username: str | None = None
    location: str | None = None
    ip_address: str | None = None
    type: str | None = None
    duration: float | None = None  # in Aternity's units; verify prints the range
    started: datetime | None = None


@dataclass
class BootComponent:
    """One component's share of a boot or logon (a startup app, GPO, profile...)."""

    timestamp: datetime
    device: str
    type: str | None = None
    component: str | None = None
    start_type: str | None = None
    username: str | None = None
    location: str | None = None
    duration: float | None = None
    degradation: float | None = None


@dataclass
class EndpointInfo:
    device: str
    ip_address: str | None = None
    location: str | None = None
    os: str | None = None
    total_disk: float | None = None  # OS drive size, in Aternity's unit


@dataclass
class NetimDevice:
    id: str
    name: str
    ip_address: str | None = None
    device_type: str | None = None
    site: str | None = None


@dataclass
class NetimInterface:
    id: str
    device_id: str
    name: str
    ip_addresses: list[str] = field(default_factory=list)  # "a.b.c.d/len" or bare IP
    speed_bps: float | None = None


@dataclass
class MetricSample:
    object_type: str  # DEVICE or INTERFACE
    object_id: str
    metric_key: str  # NetimMetric.key
    timestamp: datetime
    value: float


@dataclass
class NetimAlert:
    object_id: str
    object_name: str
    severity: str
    message: str
    start: datetime
    end: datetime | None = None


@dataclass
class MappedObject:
    """A NetIM object pulled into an investigation and why."""

    object_type: str  # DEVICE or INTERFACE
    object_id: str
    name: str
    device_id: str
    role: str  # client-gateway | site | app-server | app-server-network | always
    reason: str


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def parse_time(value) -> datetime | None:
    """Parse ISO-8601 strings, epoch seconds or epoch milliseconds into UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return parse_time(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return utc(datetime.fromisoformat(text))


def to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
