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
# memory, disk queue, free disk space, round-trip time, IP and location at
# that moment; the inventory has the OS drive's size.
# health_events: crashes, hangs and errors on the device (one row per event).
# app_usage: per-application UXI, crashes and hang time.
# boots: one row per Windows boot or logon (MEASUREMENT_TYPE says which).
# boot_components: what each boot/logon spent its time on.
# software_changes: installs, updates and removals per device (the "what changed").
# top_processes: the busiest processes on a device over time (who used the CPU/memory).
# The live tenant has no RAW_DATA_TOP_PROCESSES; APPLICATION_RESOURCES_HOURLY has
# CPU and memory per process, one row per process and hour, no disk IO.
# disk_space: free space of a device's disk over time (a nearly full disk). Off:
# the live tenant publishes no such source (only a "now" value in the inventory).
# To turn it on, set aternity.entities.disk_space; verify then checks it.
# remote_display: on a virtual desktop (VDI), the delay the user feels between
# their own device and the desktop, per session (protocol, client device and
# its subnet). Read only by `verify` so far.
DEFAULT_ATERNITY_ENTITIES = {
    "activities": "BUSINESS_ACTIVITIES_RAW",
    "inventory": "DEVICE_INVENTORY",
    "health_events": "DEVICE_HEALTH_RAW",
    "app_usage": "APPLICATIONS_RAW",
    "boots": "WINDOWS_MACHINE_BOOTS",
    "boot_components": "MACHINE_BOOT_COMPONENTS",
    "software_changes": "INSTALLED_SOFTWARE_CHANGE_LOG",
    "top_processes": "APPLICATION_RESOURCES_HOURLY",
    "remote_display": "REMOTE_DISPLAY_LATENCY_RAW",
}

# VDI and desktop-management columns on the sources already read (all in a real
# $metadata). Only `verify` reads them, to show how a live tenant fills them.
VDI_COLUMNS = {
    "activities": {"virtualization": "VIRTUALIZATION", "client_device": "CLIENT_DEVICE_NAME",
                   "client_type": "CLIENT_DEVICE_TYPE", "display_latency": "ACTIVITY_REMOTE_DISPLAY_LATENCY_AVG",
                   "used_remotely": "DEVICE_USED_REMOTELY", "image_build": "DEVICE_IMAGE_BUILD_NUMBER"},
    "boots": {"image_build": "DEVICE_IMAGE_BUILD_NUMBER"},
    "inventory": {"sccm_client": "SCCM_CLIENT_VERSION", "image_build": "DEVICE_IMAGE_BUILD_NUMBER"},
}
# Aternity's own words, seen on the live tenant (verify --overview, Nov 2026):
# activity SLA_STATUS Normal / Undefined / Minor / Major (no Critical), health
# event severity Minor / Major / Critical. The "Today" tab shows them as given.
TOP_STATUS = "Major"  # the worst SLA_STATUS of an activity
SEVERITY_ORDER = ("Critical", "Major", "Minor")  # worst first; others after them
TILE_SEVERITIES = ("Critical", "Major")  # the health events at the top of "Today"

# Aternity's own incidents and alerts (names from Riverbed's $metadata), read only
# by `verify --overview` so far: does the live tenant publish them, and what do
# their state / impact values say. No user or device column is listed here.
OVERVIEW_SOURCES = {
    "INCIDENTS": {"time": "INCIDENT_CREATION_TIMESTAMP",
                  "values": ("INCIDENT_STATE", "INCIDENT_LIVE_IMPACT", "APPLICATION_NAME", "ACTIVITY_NAME")},
    "SERVICE_DESK_ALERTS_RAW": {"time": "TIMEFRAME", "values": ("SD_ALERT_RULE_NAME", "SD_ALERT_RULE_EVENT_NAME")},
    "ANOMALY_DETECTION_INCIDENTS": {"time": "INCIDENT_CREATION_TIMESTAMP",
                                    "values": ("INCIDENT_STATE", "INCIDENT_TYPE", "INCIDENT_OBJECT_TYPE")},
}
# `verify` lists every published data source with a process column (which ones
# could tell the busiest processes), and its time column
PROCESS_COLUMN = "PROCESS_NAME"
TIME_COLUMNS = ("TIMEFRAME", "MEASUREMENT_START_TIMESTAMP")

# Aternity's free-disk columns, for `verify` to find another source of them when
# disk_space's own is not published (e.g. OS_FREE_DISK_SPACE comes with many
# data sources, in the unit of inventory.total_disk)
DISK_FREE_COLUMNS = {"free_percent": "FREE_SPACE_PERCENT", "free_space": "OS_FREE_DISK_SPACE"}

# Values seen in a live INSTALLED_SOFTWARE_CHANGE_LOG: "N/A" where a version is
# missing; an update is an "Uninstalled" row (the old version) and an
# "Installed" row (the new one) of the same software on the same PC, at the
# same second. `uxcorrelate verify` counts those pairs.
NO_VALUE = ("N/A",)
# A live DEVICE_HEALTH_RAW names a module it could not identify "unknown (Dll)",
# version "0.0.0.0 (DLL Version)": the part before the bracketed label is checked.
UNKNOWN_VALUE = ("unknown", "0.0.0.0")
SOFTWARE_REMOVED = ("Uninstalled",)
# A live change log writes an update as two rows, the removal of the old version
# and the install of the new one, up to a few minutes apart (Edge: 2 min); some
# updates are one "Updated" row. Rows of one software this close are one change.
SOFTWARE_UPDATE_GAP_MIN = 10
# A process row covers up to an hour (APPLICATION_RESOURCES_HOURLY); its time is
# at any minute (live: :04, :08, ...), not said to be the hour's start or end, so
# rows within this many minutes before or after a slow action are read.
PROCESS_ROW_SPAN_MIN = 60
# VIRTUALIZATION values that mean a virtual desktop (a live tenant sends 'true' /
# 'false'; some data sources type it as a number)
VIRTUAL_VALUES = ("true", "1")

# Names in a health event that are not the software at fault (community sources
# agree; Microsoft documents neither). "bad_module_info": Windows could not tell
# which program failed. The Windows files: where a failure is reported, usually
# not its cause, so the program they ran in is looked at instead.
NO_PROGRAM = ("bad_module_info",)
WINDOWS_MODULES = ("kernelbase.dll", "ntdll.dll", "kernel32.dll")
# a health event of this kind runs in the background: the user may not feel it
BACKGROUND_EVENT = "background"

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
        "status": "SLA_STATUS",
        "cpu": "HRC_CPU_UTIL",
        "memory": "HRC_PHYSICAL_MEMORY_UTIL",
        "disk_queue": "HRC_DISK_QUEUE_LENGTH",
        "network_latency": "NETWORK_RTT_AVG",
        "ip_address": "DEVICE_IP_ADDRESS",
        "location": "BUSINESS_LOCATION",
        "os": "OS_NAME",
        # a virtual desktop (VDI) or not, and the device the user sat at
        "virtualization": "VIRTUALIZATION",
        "client_device": "CLIENT_DEVICE_NAME",
    },
    "inventory": {
        "device": "DEVICE_NAME",
        "ip_address": "DEVICE_IP_ADDRESS",
        "location": "BUSINESS_LOCATION",
        "os": "OS_NAME",
        "total_disk": "OS_TOTAL_DISK_SPACE",
    },
    "health_events": {
        "timestamp": "TIMEFRAME",
        "device": "DEVICE_NAME",
        "username": "USERNAME",
        "application": "APPLICATION_NAME",
        "category": "HEALTH_EVENT_CATEGORY",
        "name": "HEALTH_EVENT_NAME",
        "component": "HEALTH_EVENT_COMPONENT",
        "severity": "HEALTH_EVENT_SEVERITY",
        "count": "HEALTH_EVENT_VOLUME",
        # where it failed: the component (e.g. the program) and the sub-component
        # (e.g. the module inside it), their versions, and Aternity's error text
        "component_version": "HEALTH_EVENT_COMPONENT_VERSION",
        "sub_component": "HEALTH_EVENT_SUB_COMPONENT",
        "sub_component_version": "HEALTH_EVENT_SUB_COMPONENT_VERSION",
        "exception": "HEALTH_EVENT_EXCEPTION_TYPE",
        "error": "HEALTH_EVENT_ERROR",
        "details": "HEALTH_EVENT_DETAILS",
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
        "username": "USERNAME",
        "location": "BUSINESS_LOCATION",
        "type": "MEASUREMENT_TYPE",
        "component": "MEASUREMENT_COMPONENT",
        "duration": "MEASUREMENT_DURATION_AVG",
        "degradation": "MEASUREMENT_DEGRADATION_TIME",
    },
    "software_changes": {
        "timestamp": "INSTALLED_SW_CHANGE_TIMESTAMP",
        "device": "DEVICE_NAME",
        "name": "INSTALLED_SW_NAME",
        "change": "INSTALLED_SW_CHANGE_TYPE",
        "version": "INSTALLED_SW_VERSION",
        "version_before": "INSTALLED_SW_VERSION_BEFORE_CHANGE",
        "vendor": "INSTALLED_SW_VENDOR",
    },
    "top_processes": {
        # live values: CPU 0 to 30 over 1000 rows (a percent, like HRC_CPU_UTIL);
        # "memory util" 241664 to 8.2e9: bytes, not a percent
        "timestamp": "TIMEFRAME",
        "device": "DEVICE_NAME",
        "process": "PROCESS_NAME",
        "application": "APPLICATION_NAME",
        "cpu": "PRC_CPU_UTIL_MAX",
        "memory": "",
        "memory_bytes": "PRC_PHYSICAL_MEMORY_UTIL_MAX",
        "io_read": "",
        "io_write": "",
    },
    "remote_display": {
        "timestamp": "TIMEFRAME",
        "device": "DEVICE_NAME",
        "username": "USERNAME",
        "client_device": "CLIENT_DEVICE_NAME",
        "client_type": "CLIENT_DEVICE_TYPE",
        "client_subnet": "CLIENT_DEVICE_SUBNET",
        "virtualization": "VIRTUALIZATION",
        "protocol": "REMOTE_DISPLAY_PROTOCOL",
        "latency": "REMOTE_DISPLAY_LATENCY_AVG",
        "status": "SLA_STATUS",
    },
    # free_percent, or free_space in the unit of inventory.total_disk (e.g. a
    # data source with OS_FREE_DISK_SPACE); `uxcorrelate verify` says which
    "disk_space": {
        "timestamp": "MEASUREMENT_START_TIMESTAMP",
        "device": "DEVICE_NAME",
        "free_percent": "FREE_SPACE_PERCENT",
        "free_space": "",
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
    # NetIM has no call for every device's interfaces, only one per device (as in
    # steelscript-netim); this many are read at once to find gateways
    parallel_reads: int = 8
    metrics: list[NetimMetric] = field(
        default_factory=lambda: [NetimMetric(**m) for m in DEFAULT_NETIM_METRICS]
    )


@dataclass
class AiService:
    """One self-hosted AI service for the chat (OpenAI-style chat API)."""
    name: str = ""       # your label for it
    base_url: str = ""   # up to and including /v1 (the part before /chat/completions)
    model: str = ""      # the model's name on that service
    key_env: str = "AI_API_KEY"  # the .env line that holds its key (the key itself never goes in config.yaml)
    login: str = ""      # "bearer" or "api-key"; empty: verify finds which one the service takes
    chat_path: str = "/chat/completions"
    models_path: str = "/models"
    query: dict[str, str] = field(default_factory=dict)  # extra URL values, e.g. an api-version
    timeout: float = 120.0


@dataclass
class AiConfig:
    services: list[AiService] = field(default_factory=list)
    chat: str = ""  # the name of the service the chat uses; empty: the first one set


@dataclass
class Thresholds:
    # An activity is slow when Aternity's own status for it (SLA_STATUS) is one
    # of these values. `uxcorrelate verify` lists the values your tenant uses.
    slow_status: list[str] = field(default_factory=list)
    # Only while slow_status is empty: slow = response time above this (seconds)
    # or the application-specific value below.
    response_time_s: float = 3.0
    response_time_s_by_app: dict[str, float] = field(default_factory=dict)
    device_cpu: float = 85.0
    device_memory: float = 90.0
    disk_queue: float = 2.0
    # OS drive free space (activity row) / its size (inventory), in percent
    free_disk_percent: float = 10.0
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
    ai: AiConfig = field(default_factory=AiConfig)
    # a problem counts for a slow action recorded up to this many minutes before or after it
    bucket_minutes: int = 5
    # unit of WINDOWS_MACHINE_BOOTS durations, shown next to them
    boot_unit: str = "s"
    # where "Today" starts and ends (midnight there), e.g. America/New_York; empty: UTC
    timezone: str = ""


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
        if "boot_unit" in raw:
            settings.boot_unit = str(raw["boot_unit"])
        if raw.get("timezone"):
            settings.timezone = str(raw["timezone"]).strip()
        if metrics is not None:
            settings.netim.metrics = [NetimMetric(**m) for m in metrics]
        settings.ai.chat = str((raw.get("ai") or {}).get("chat") or "").strip()
        for entry in (raw.get("ai") or {}).get("services") or []:
            service = AiService()
            _merge_dataclass(service, entry)
            settings.ai.services.append(service)

    env = os.environ
    settings.aternity.base_url = env.get("ATERNITY_URL", settings.aternity.base_url)
    settings.aternity.username = env.get("ATERNITY_USERNAME", settings.aternity.username)
    settings.aternity.password = env.get("ATERNITY_PASSWORD", settings.aternity.password)
    settings.netim.base_url = env.get("NETIM_URL", settings.netim.base_url)
    settings.netim.username = env.get("NETIM_USERNAME", settings.netim.username)
    settings.netim.password = env.get("NETIM_PASSWORD", settings.netim.password)
    return settings
