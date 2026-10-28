"""Configuration loading.

Settings come from a YAML file (see config.example.yaml) and can be overridden
with environment variables for secrets:

    ATERNITY_URL, ATERNITY_USERNAME, ATERNITY_PASSWORD
    NETIM_URL, NETIM_USERNAME, NETIM_PASSWORD

Aternity column names and NetIM endpoint paths / metric names differ between
product versions and tenants, so every one of them is configurable. Use the
discovery commands (`uxcorrelate aternity-sample`, `uxcorrelate
netim-metric-classes`) to confirm the names in your environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_ATERNITY_ENTITIES = {
    "activities": "BUSINESS_ACTIVITIES_RAW",
    "device_health": "DEVICE_HEALTH_RAW",
    "inventory": "DEVICE_INVENTORY",
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
        "client_time": "CLIENT_TIME",
        "network_time": "NETWORK_TIME",
        "backend_time": "BACKEND_TIME",
        "uxi": "UXI",
    },
    "device_health": {
        "timestamp": "TIMEFRAME",
        "device": "DEVICE_NAME",
        "cpu": "CPU_UTILIZATION",
        "memory": "MEMORY_UTILIZATION",
        "disk_queue": "DISK_QUEUE_LENGTH",
        "wifi_signal": "WIFI_SIGNAL_STRENGTH",
        "network_latency": "NETWORK_LATENCY",
    },
    "inventory": {
        "device": "DEVICE_NAME",
        "ip_address": "IP_ADDRESS",
        "location": "LOCATION",
        "os": "OS_NAME",
    },
}

# Default NetIM metrics. object_type is DEVICE or INTERFACE. metric_class and
# metric are the names NetIM reports; confirm with `netim-metric-classes`.
DEFAULT_NETIM_METRICS = [
    {"key": "if_util_in", "label": "Interface utilization in (%)", "object_type": "INTERFACE",
     "metric_class": "IF_UTIL", "metric": "utilizationIn", "threshold": 80.0},
    {"key": "if_util_out", "label": "Interface utilization out (%)", "object_type": "INTERFACE",
     "metric_class": "IF_UTIL", "metric": "utilizationOut", "threshold": 80.0},
    {"key": "if_errors", "label": "Interface errors", "object_type": "INTERFACE",
     "metric_class": "IF_ERRORS", "metric": "inErrors", "threshold": 10.0},
    {"key": "if_discards", "label": "Interface discards", "object_type": "INTERFACE",
     "metric_class": "IF_ERRORS", "metric": "outDiscards", "threshold": 50.0},
    {"key": "dev_cpu", "label": "Device CPU (%)", "object_type": "DEVICE",
     "metric_class": "CPU_UTIL", "metric": "cpuUtilization", "threshold": 85.0},
    {"key": "dev_mem", "label": "Device memory (%)", "object_type": "DEVICE",
     "metric_class": "MEM_UTIL", "metric": "memoryUtilization", "threshold": 90.0},
    {"key": "dev_latency", "label": "Response time / latency (ms)", "object_type": "DEVICE",
     "metric_class": "AVAILABILITY", "metric": "responseTime", "threshold": 150.0},
]


@dataclass
class AternityConfig:
    base_url: str = ""
    username: str = ""
    password: str = ""
    verify_tls: bool | str = True
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
    verify_tls: bool | str = True
    timeout: float = 60.0
    api_prefix: str = "/api/netim/v1"
    metric_data_path: str = "/swarm/NETIM_NETWORK_METRIC_DATA_SERVICE/api/v1/network-metric-data"
    alerts_path: str = "/api/netim/v1/alerts"
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
    # ...or UXI (0-100 style score) falls below this.
    uxi_min: float = 70.0
    device_cpu: float = 85.0
    device_memory: float = 90.0
    disk_queue: float = 2.0
    wifi_signal_dbm: float = -70.0  # weaker (lower) than this is anomalous
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
