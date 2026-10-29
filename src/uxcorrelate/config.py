"""Configuration loading.

Settings come from a YAML file (see config.example.yaml) and can be overridden
with environment variables for secrets:

    ATERNITY_URL, ATERNITY_USERNAME, ATERNITY_PASSWORD
    NETIM_URL, NETIM_USERNAME, NETIM_PASSWORD

Aternity column names and NetIM endpoint paths / metric names differ between
product versions and tenants, so every one of them is configurable. Run
`uxcorrelate verify` to check every name against your live systems.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Aternity data sources and columns. Every name below is in a real Aternity
# API 2.0 $metadata (tests/fixtures/aternity_metadata_v2.xml); the contract
# tests fail if one is not. Each activity row also carries the device's CPU,
# memory, disk queue, round-trip time, IP and location at that moment.
# health_events: crashes, hangs and errors on the device (one row per event).
# app_usage: per-application UXI, crashes and hang time.
# boots: one row per Windows boot or logon (MEASUREMENT_TYPE says which).
# boot_components: what each boot/logon spent its time on.
DEFAULT_ATERNITY_ENTITIES = {
    "activities": "BUSINESS_ACTIVITIES_RAW",
    "inventory": "DEVICE_INVENTORY",
    "health_events": "DEVICE_HEALTH_RAW",
    "app_usage": "APPLICATIONS_RAW",
    "boots": "WINDOWS_MACHINE_BOOTS",
    "boot_components": "MACHINE_BOOT_COMPONENTS",
}

# logical name -> Aternity OData column
DEFAULT_ATERNITY_FIELDS = {
    "activities": {
        "timestamp": "TIMEFRAME",
        "username": "USERNAME",
        "device": "DEVICE_NAME",
        "application": "APPLICATION_NAME",
        "activity": "ACTIVITY_NAME",
        "response_time": "ACTIVITY_RESPONSE_TIME",
        "client_time": "ACTIVITY_CLIENT_TIME",
        "network_time": "ACTIVITY_NETWORK_TIME",
        "backend_time": "ACTIVITY_BACKEND_TIME",
        "cpu": "HRC_CPU_UTIL",
        "memory": "HRC_PHYSICAL_MEMORY_UTIL",
        "disk_queue": "HRC_DISK_QUEUE_LENGTH",
        "network_latency": "NETWORK_RTT_AVG",
        "ip_address": "DEVICE_IP_ADDRESS",
        "location": "BUSINESS_LOCATION",
        "os": "OS_NAME",
    },
    "inventory": {
        "device": "DEVICE_NAME",
        "ip_address": "DEVICE_IP_ADDRESS",
        "location": "BUSINESS_LOCATION",
        "os": "OS_NAME",
    },
    "health_events": {
        "timestamp": "TIMEFRAME",
        "device": "DEVICE_NAME",
        "application": "APPLICATION_NAME",
        "category": "HEALTH_EVENT_CATEGORY",
        "name": "HEALTH_EVENT_NAME",
        "component": "HEALTH_EVENT_COMPONENT",
        "severity": "HEALTH_EVENT_SEVERITY",
        "count": "HEALTH_EVENT_VOLUME",
    },
    "app_usage": {
        "timestamp": "TIMEFRAME",
        "device": "DEVICE_NAME",
        "application": "APPLICATION_NAME",
        "uxi": "APPLICATION_UXI_AVG",
        "crashes": "APPLICATION_CRASHES_TOTAL",
        "hang_time": "APPLICATION_HANG_TIME_TOTAL",
    },
    "boots": {
        "timestamp": "TIMEFRAME",
        "started": "MEASUREMENT_START_TIMESTAMP",
        "username": "USERNAME",
        "device": "DEVICE_NAME",
        "location": "BUSINESS_LOCATION",
        "ip_address": "DEVICE_IP_ADDRESS",
        "type": "MEASUREMENT_TYPE",
        "duration": "MEASUREMENT_DURATION_AVG",
    },
    "boot_components": {
        "timestamp": "TIMEFRAME",
        "device": "DEVICE_NAME",
        "type": "MEASUREMENT_TYPE",
        "component": "MEASUREMENT_COMPONENT",
        "duration": "MEASUREMENT_DURATION_AVG",
        "degradation": "MEASUREMENT_DEGRADATION_TIME",
    },
}

# Default NetIM metrics: only names confirmed by Riverbed (IFC_UTIL with
# utilizationIn/Out in the steelscript-netim SDK, CPU_UTIL with cpuUtil in
# the NetIM docs). `uxcorrelate verify` lists every class and metric your
# NetIM has, so you can add more (errors, discards, memory, latency).
DEFAULT_NETIM_METRICS = [
    {"key": "if_util_in", "label": "Interface utilization in (%)", "object_type": "INTERFACE",
     "metric_class": "IFC_UTIL", "metric": "utilizationIn", "threshold": 80.0},
    {"key": "if_util_out", "label": "Interface utilization out (%)", "object_type": "INTERFACE",
     "metric_class": "IFC_UTIL", "metric": "utilizationOut", "threshold": 80.0},
    {"key": "dev_cpu", "label": "Device CPU (%)", "object_type": "DEVICE",
     "metric_class": "CPU_UTIL", "metric": "cpuUtil", "threshold": 85.0},
]


@dataclass
class AternityConfig:
    base_url: str = ""
    username: str = ""
    password: str = ""
    timeout: float = 60.0
    max_pages: int = 50
    entities: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ATERNITY_ENTITIES))
    fields: dict[str, dict[str, str]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_ATERNITY_FIELDS.items()}
    )


@dataclass
class NetimMetric:
    key: str
    label: str
    object_type: str
    metric_class: str
    metric: str
    threshold: float
    comparison: str = ">"  # ">" or "<"

    def is_anomalous(self, value: float) -> bool:
        return value < self.threshold if self.comparison == "<" else value > self.threshold


@dataclass
class NetimConfig:
    base_url: str = ""
    username: str = ""
    password: str = ""
    timeout: float = 60.0
    api_prefix: str = "/api/netim/v1"
    metric_data_path: str = "/swarm/NETIM_NETWORK_METRIC_DATA_SERVICE/api/v1/network-metric-data"
    # No public Riverbed source documents an alerts read API; set it once
    # NetIM's HELP > API Documentation shows the path.
    alerts_path: str = ""
    page_size: int = 500
    metrics: list[NetimMetric] = field(
        default_factory=lambda: [NetimMetric(**m) for m in DEFAULT_NETIM_METRICS]
    )


@dataclass
class Thresholds:
    # A user sample is degraded if response time exceeds this (seconds)...
    response_time_s: float = 3.0
    # ...or the application-specific value below, if present.
    response_time_s_by_app: dict[str, float] = field(default_factory=dict)
    device_cpu: float = 85.0
    device_memory: float = 90.0
    disk_queue: float = 2.0
    client_network_latency_ms: float = 150.0


@dataclass
class Topology:
    """Static hints that tie Aternity users/apps to NetIM objects."""

    # application name -> list of server hostnames / IPs
    applications: dict[str, list[str]] = field(default_factory=dict)
    # Aternity location -> NetIM device names (e.g. branch WAN routers)
    sites: dict[str, list[str]] = field(default_factory=dict)
    # NetIM device names always included in an investigation (e.g. core, firewall)
    always_include: list[str] = field(default_factory=list)


@dataclass
class Settings:
    aternity: AternityConfig = field(default_factory=AternityConfig)
    netim: NetimConfig = field(default_factory=NetimConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    topology: Topology = field(default_factory=Topology)
    bucket_minutes: int = 5


def _merge_dataclass(obj: Any, data: dict[str, Any]) -> None:
    for key, value in (data or {}).items():
        if not hasattr(obj, key):
            raise ValueError(f"Unknown config key '{key}' for {type(obj).__name__}")
        current = getattr(obj, key)
        if isinstance(current, dict) and isinstance(value, dict):
            for k, v in value.items():
                if isinstance(current.get(k), dict) and isinstance(v, dict):
                    current[k].update(v)
                else:
                    current[k] = v
        else:
            setattr(obj, key, value)


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    settings = Settings()
    path = path or os.environ.get("UXCORRELATE_CONFIG")
    if path:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        metrics = (raw.get("netim") or {}).pop("metrics", None)
        _merge_dataclass(settings.aternity, raw.get("aternity"))
        _merge_dataclass(settings.netim, raw.get("netim"))
        _merge_dataclass(settings.thresholds, raw.get("thresholds"))
        _merge_dataclass(settings.topology, raw.get("topology"))
        if "bucket_minutes" in raw:
            settings.bucket_minutes = int(raw["bucket_minutes"])
        if metrics is not None:
            settings.netim.metrics = [NetimMetric(**m) for m in metrics]

    env = os.environ
    settings.aternity.base_url = env.get("ATERNITY_URL", settings.aternity.base_url)
    settings.aternity.username = env.get("ATERNITY_USERNAME", settings.aternity.username)
    settings.aternity.password = env.get("ATERNITY_PASSWORD", settings.aternity.password)
    settings.netim.base_url = env.get("NETIM_URL", settings.netim.base_url)
    settings.netim.username = env.get("NETIM_USERNAME", settings.netim.username)
    settings.netim.password = env.get("NETIM_PASSWORD", settings.netim.password)
    return settings
