"""Riverbed Aternity Data Source REST API (OData) client.

The base URL is the tenant's OData endpoint, e.g.
    https://<tenant-odata-host>/aternity.odata/v2.0
Authentication is HTTP basic with the Aternity user name and the REST API
access token (or password) generated in Aternity settings.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import quote

import httpx

from .config import NO_VALUE, UNKNOWN_VALUE, AternityConfig
from .http import ApiError, RestClient
from .models import (
    AppUsageSample, BootComponent, BootSample, DeviceHealthSample, EndpointInfo, HealthEvent, ProcessSample,
    SoftwareChange, UserSample, parse_time, to_float,
)

log = logging.getLogger("uxcorrelate")


def _bare(value) -> str:
    """A value without Aternity's bracketed label: "unknown (Dll)" -> "unknown"."""
    return re.sub(r"\s*\([^()]*\)$", "", str(value or "")).strip()


def odata_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def odata_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class AternityClient:
    def __init__(self, cfg: AternityConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.http = RestClient(
            "Aternity", cfg.base_url, cfg.username, cfg.password,
            timeout=cfg.timeout, transport=transport,
        )
        self._activity_health: dict[str, list[DeviceHealthSample]] = {}
        self._endpoints: dict[str, EndpointInfo] = {}
        self.warnings: list[str] = []

    # -- generic OData -------------------------------------------------
    def query(self, entity: str, filter: str | None = None, top: int | None = None,
              orderby: str | None = None, select: list[str] | None = None, raw: bool = False) -> list[dict]:
        """Rows of a data source. Aternity's "N/A" (no value) becomes None, unless
        raw (verify shows values exactly as sent)."""
        rows = self._query(entity, filter, top, orderby, select)
        if not raw:
            for row in rows:
                for k, v in row.items():
                    if v in NO_VALUE:
                        row[k] = None
        return rows

    def _query(self, entity: str, filter: str | None, top: int | None, orderby: str | None,
               select: list[str] | None) -> list[dict]:
        params: dict[str, str] = {"$format": "json"}
        if filter:
            params["$filter"] = filter
        if select:
            params["$select"] = ",".join(select)
        if top:
            params["$top"] = str(top)
        if orderby:
            params["$orderby"] = orderby
        # Aternity rejects "%24filter": keep the "$" literal, encode only the values
        query = "&".join(f"{k}={quote(v, safe=chr(39) + '(),:')}" for k, v in params.items())
        rows: list[dict] = []
        url: str | None = f"/{entity}?{query}"
        pages = 0
        while url and pages < self.cfg.max_pages:
            data = self.http.get_json(url)
            rows.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            pages += 1
            if top and len(rows) >= top:
                return rows[:top]
        if url:
            self.warnings.append(f"Aternity {entity}: stopped after {pages} pages "
                                 f"({len(rows)} rows); the result is partial. Narrow the window "
                                 "or raise aternity.max_pages.")
        return rows

    def _col(self, entity_key: str, logical: str) -> str:
        return self.cfg.fields[entity_key][logical]

    def _time_filter(self, entity_key: str, start: datetime, end: datetime) -> str:
        ts = self._col(entity_key, "timestamp")
        return f"{ts} ge {odata_time(start)} and {ts} le {odata_time(end)}"

    # -- domain queries --------------------------------------------------
    def user_activity(self, start: datetime, end: datetime, username: str | None = None,
                      device: str | None = None, application: str | None = None) -> list[UserSample]:
        """Activities for the window. Each row also carries the device's CPU, memory,
        disk queue, round-trip time, IP and location, kept for device_health() and
        endpoint_info()."""
        key = "activities"
        clauses = [self._time_filter(key, start, end)]
        if username:
            clauses.append(f"{self._col(key, 'username')} eq {odata_quote(username)}")
        if device:
            clauses.append(f"{self._col(key, 'device')} eq {odata_quote(device)}")
        if application:
            clauses.append(f"{self._col(key, 'application')} eq {odata_quote(application)}")
        rows = self.query(self.cfg.entities[key], " and ".join(clauses))
        f = self.cfg.fields[key]
        samples = []
        for row in rows:
            ts = parse_time(row.get(f["timestamp"]))
            if ts is None:
                continue
            dev = str(row.get(f["device"]) or "")
            samples.append(UserSample(
                timestamp=ts,
                username=str(row.get(f["username"]) or ""),
                device=dev,
                application=str(row.get(f["application"]) or ""),
                location=row.get(f.get("location", "")) or None,
                activity=row.get(f.get("activity", "")),
                response_time=to_float(row.get(f.get("response_time", ""))),
                client_time=to_float(row.get(f.get("client_time", ""))),
                network_time=to_float(row.get(f.get("network_time", ""))),
                backend_time=to_float(row.get(f.get("backend_time", ""))),
                status=row.get(f.get("status", "")) or None,
            ))
            self._activity_health.setdefault(dev, []).append(DeviceHealthSample(
                timestamp=ts,
                device=dev,
                cpu=to_float(row.get(f.get("cpu", ""))),
                memory=to_float(row.get(f.get("memory", ""))),
                disk_queue=to_float(row.get(f.get("disk_queue", ""))),
                network_latency=to_float(row.get(f.get("network_latency", ""))),
            ))
            ip, loc = row.get(f.get("ip_address", "")), row.get(f.get("location", ""))
            if dev and (ip or loc) and dev not in self._endpoints:
                self._endpoints[dev] = EndpointInfo(device=dev, ip_address=ip or None,
                                                    location=loc or None,
                                                    os=row.get(f.get("os", "")) or None)
        return samples

    def device_health(self, device: str, start: datetime, end: datetime) -> list[DeviceHealthSample]:
        """Device context (CPU, memory, disk queue, round-trip time) from the activity rows."""
        return [h for h in self._activity_health.get(device, []) if start <= h.timestamp <= end]

    def app_activity(self, applications: list[str], start: datetime, end: datetime) -> list[UserSample]:
        """Every user's activities of these applications in the window (for "who else")."""
        key = "activities"
        f = self.cfg.fields[key]
        apps = " or ".join(f"{f['application']} eq {odata_quote(a)}" for a in applications)
        logical = ("timestamp", "username", "device", "application", "location", "ip_address",
                   "response_time", "client_time", "network_time", "backend_time", "status")
        select = [f[k] for k in logical if f.get(k)]
        rows = self.query(self.cfg.entities[key], f"{self._time_filter(key, start, end)} and ({apps})",
                          select=select)
        out = []
        for row in rows:
            ts = parse_time(row.get(f["timestamp"]))
            if ts is not None:
                out.append(UserSample(
                    timestamp=ts,
                    username=str(row.get(f["username"]) or ""),
                    device=str(row.get(f["device"]) or ""),
                    application=str(row.get(f["application"]) or ""),
                    location=row.get(f.get("location", "")) or None,
                    ip_address=row.get(f.get("ip_address", "")) or None,
                    response_time=to_float(row.get(f.get("response_time", ""))),
                    client_time=to_float(row.get(f.get("client_time", ""))),
                    network_time=to_float(row.get(f.get("network_time", ""))),
                    backend_time=to_float(row.get(f.get("backend_time", ""))),
                    status=row.get(f.get("status", "")) or None,
                ))
        return out

    def boots(self, start: datetime, end: datetime, username: str | None = None,
              device: str | None = None, location: str | None = None) -> list[BootSample]:
        """Boots and logons in the window, for one user/device/location or everyone."""
        key = "boots"
        f = self.cfg.fields.get(key, {})
        if not f:
            return []
        clauses = [self._time_filter(key, start, end)]
        for logical, value in (("username", username), ("device", device), ("location", location)):
            if value:
                clauses.append(f"{f[logical]} eq {odata_quote(value)}")
        out = []
        for row in self._optional(key, "boot/logon data", " and ".join(clauses),
                                  select=[c for c in f.values() if c]):
            ts = parse_time(row.get(f["timestamp"]))
            if ts is not None:
                out.append(BootSample(
                    timestamp=ts, device=str(row.get(f["device"]) or ""),
                    username=row.get(f.get("username", "")) or None,
                    location=row.get(f.get("location", "")) or None,
                    ip_address=row.get(f.get("ip_address", "")) or None,
                    type=row.get(f.get("type", "")) or None,
                    duration=to_float(row.get(f.get("duration", ""))),
                    started=parse_time(row.get(f.get("started", ""))),
                ))
        return out

    def _components(self, flt: str, what: str) -> list[BootComponent]:
        key = "boot_components"
        f = self.cfg.fields.get(key, {})
        out = []
        for row in self._optional(key, what, flt, select=[c for c in f.values() if c]):
            ts = parse_time(row.get(f["timestamp"]))
            if ts is not None:
                get = lambda k: (row.get(f[k]) or None) if f.get(k) else None  # noqa: E731
                out.append(BootComponent(
                    timestamp=ts, device=str(row.get(f["device"]) or ""), type=get("type"),
                    component=get("component"), start_type=get("start_type"),
                    duration=to_float(get("duration")), degradation=to_float(get("degradation")),
                    username=get("username"), location=get("location"),
                ))
        return out

    def boot_components(self, devices: list[str], start: datetime, end: datetime) -> list[BootComponent]:
        """What the boots/logons of these devices spent their time on."""
        f = self.cfg.fields.get("boot_components", {})
        if not f or not devices:
            return []
        devs = " or ".join(f"{f['device']} eq {odata_quote(d)}" for d in devices)
        return self._components(f"{self._time_filter('boot_components', start, end)} and ({devs})",
                                "boot/logon components")

    def boot_component_everywhere(self, component: str | None, type: str | None, start: datetime,
                                  end: datetime) -> list[BootComponent]:
        """One boot/logon part on every device (who else has it slow?). A part with
        no component name (e.g. a boot phase) is found by its type alone."""
        f = self.cfg.fields.get("boot_components", {})
        if not f or not (component and f.get("component") or type and f.get("type")):
            return []
        flt = self._time_filter('boot_components', start, end)
        if component and f.get("component"):
            flt += f" and {f['component']} eq {odata_quote(component)}"
        if type and f.get("type"):
            flt += f" and {f['type']} eq {odata_quote(type)}"
        return self._components(flt, "boot/logon components of other PCs")

    def _optional(self, key: str, what: str, flt: str, select: list[str] | None = None,
                  top: int | None = None) -> list[dict]:
        """Rows of an optional data source; a failure becomes a warning, not an abort."""
        if key not in self.cfg.entities or key not in self.cfg.fields:
            return []
        try:
            return self.query(self.cfg.entities[key], flt, select=select, top=top)
        except ApiError as exc:
            log.warning("Aternity: could not read %s: %s", what, exc)
            entity = self.cfg.entities[key]
            if "doesn't exist" in exc.reason or "does not exist" in exc.reason:
                msg = (f"{what.capitalize()} not checked: this Aternity has no {entity} data source "
                       "(`uxcorrelate verify` shows what to change).")
            else:
                msg = f"{what.capitalize()} not checked: Aternity {entity} gave an error ({exc.reason or exc.status or exc})."
            if msg not in self.warnings:
                self.warnings.append(msg)
            return []

    def _events(self, flt: str, what: str, logical: list[str] | None = None) -> list[HealthEvent]:
        key = "health_events"
        f = self.cfg.fields.get(key, {})
        if not f:
            return []
        select = [f[k] for k in (logical or f) if f.get(k)]
        out = []
        for row in self._optional(key, what, flt, select=select):
            ts = parse_time(row.get(f["timestamp"]))
            if ts is None:
                continue
            get = lambda k: (row.get(f[k]) or None) if f.get(k) else None  # noqa: E731
            known = lambda k: None if _bare(get(k)).lower() in UNKNOWN_VALUE else get(k)  # noqa: E731
            out.append(HealthEvent(
                timestamp=ts, device=str(row.get(f["device"]) or ""),
                application=get("application"), username=get("username"), category=get("category"), name=get("name"),
                component=known("component"), severity=get("severity"), count=to_float(get("count")),
                component_version=known("component_version"), sub_component=known("sub_component"),
                sub_component_version=known("sub_component_version"), exception=get("exception"),
                error=get("error"), details=get("details"),
            ))
        return out

    def health_events(self, device: str, start: datetime, end: datetime) -> list[HealthEvent]:
        """Crashes, hangs and errors on the device (all applications), with where they failed."""
        f = self.cfg.fields.get("health_events", {})
        if not f:
            return []
        return self._events(f"{self._time_filter('health_events', start, end)} and "
                            f"{f['device']} eq {odata_quote(device)}", "device health events")

    def app_health_events(self, application: str, start: datetime, end: datetime) -> list[HealthEvent]:
        """One application's health events on every device (is it only this PC?)."""
        f = self.cfg.fields.get("health_events", {})
        if not f or not f.get("application"):
            return []
        return self._events(f"{self._time_filter('health_events', start, end)} and "
                            f"{f['application']} eq {odata_quote(application)}", "health events of other PCs",
                            self.EVERYWHERE)

    # what "who else had the same failure" needs from every PC
    EVERYWHERE = ["timestamp", "device", "username", "application", "category", "name", "component", "count",
                  "component_version", "sub_component", "sub_component_version", "error"]

    def component_health_events(self, component: str, start: datetime, end: datetime) -> list[HealthEvent]:
        """One process's health events on every device, for events with no application
        (a background process): is it only this PC?"""
        f = self.cfg.fields.get("health_events", {})
        if not f or not f.get("component"):
            return []
        return self._events(f"{self._time_filter('health_events', start, end)} and "
                            f"{f['component']} eq {odata_quote(component)}", "health events of other PCs",
                            self.EVERYWHERE)

    def software_changes(self, device: str, start: datetime, end: datetime) -> list[SoftwareChange]:
        """Installs, updates and removals on the device in the window."""
        key = "software_changes"
        f = self.cfg.fields.get(key, {})
        if not f:
            return []
        out = []
        for row in self._optional(key, "software changes",
                                  f"{self._time_filter(key, start, end)} and {f['device']} eq {odata_quote(device)}",
                                  select=[c for c in f.values() if c]):
            ts = parse_time(row.get(f["timestamp"]))
            if ts is not None:
                get = lambda k: (row.get(f[k]) or None) if f.get(k) else None  # noqa: E731
                out.append(SoftwareChange(timestamp=ts, device=device, name=get("name"), change=get("change"),
                                          version=get("version"), version_before=get("version_before"),
                                          vendor=get("vendor")))
        return out

    def top_processes(self, device: str, start: datetime, end: datetime) -> list[ProcessSample]:
        """The busiest processes on the device in the window."""
        key = "top_processes"
        f = self.cfg.fields.get(key, {})
        if not f:
            return []
        out = []
        for row in self._optional(key, "top processes",
                                  f"{self._time_filter(key, start, end)} and {f['device']} eq {odata_quote(device)}",
                                  select=[c for c in f.values() if c]):
            ts = parse_time(row.get(f["timestamp"]))
            name = row.get(f.get("process", ""))
            if ts is not None and name:
                out.append(ProcessSample(
                    timestamp=ts, device=device, process=str(name), application=row.get(f.get("application", "")) or None,
                    cpu=to_float(row.get(f.get("cpu", ""))), memory=to_float(row.get(f.get("memory", ""))),
                    io_read=to_float(row.get(f.get("io_read", ""))), io_write=to_float(row.get(f.get("io_write", ""))),
                ))
        return out

    def app_usage(self, device: str, start: datetime, end: datetime,
                  application: str | None = None) -> list[AppUsageSample]:
        """Per-application UXI, crashes and hang time on the device."""
        key = "app_usage"
        f = self.cfg.fields.get(key, {})
        if not f:
            return []
        flt = f"{self._time_filter(key, start, end)} and {f['device']} eq {odata_quote(device)}"
        if application:
            flt += f" and {f['application']} eq {odata_quote(application)}"
        out = []
        for row in self._optional(key, "application UXI data", flt):
            ts = parse_time(row.get(f["timestamp"]))
            if ts is not None:
                out.append(AppUsageSample(
                    timestamp=ts, device=device,
                    application=str(row.get(f.get("application", "")) or ""),
                    uxi=to_float(row.get(f.get("uxi", ""))),
                    crashes=to_float(row.get(f.get("crashes", ""))),
                    hang_time=to_float(row.get(f.get("hang_time", ""))),
                ))
        return out

    def disk_space(self, device: str, start: datetime, end: datetime) -> list[DeviceHealthSample]:
        """Free space of the device's disk over time, in % (free_disk)."""
        key = "disk_space"
        f = self.cfg.fields.get(key, {})
        if not f or not (f.get("free_percent") or f.get("free_space")):
            return []
        size = None if f.get("free_percent") else self.disk_total(device)
        if not f.get("free_percent") and not size:
            return []
        out = []
        for row in self._optional(key, "free disk space",
                                  f"{self._time_filter(key, start, end)} and {f['device']} eq {odata_quote(device)}",
                                  select=[c for c in f.values() if c]):
            ts = parse_time(row.get(f["timestamp"]))
            free = to_float(row.get(f["free_percent"])) if f.get("free_percent") else \
                (lambda v: None if v is None else 100 * v / size)(to_float(row.get(f["free_space"])))
            if ts is not None and free is not None:
                out.append(DeviceHealthSample(timestamp=ts, device=device, free_disk=free))
        return out

    def disk_total(self, device: str) -> float | None:
        """Size of the device's OS drive, from the inventory (None if unknown)."""
        f = self.cfg.fields.get("inventory", {})
        if not f.get("total_disk"):
            return None
        rows = self._optional("inventory", "device disk size", f"{f['device']} eq {odata_quote(device)}",
                              select=[f["device"], f["total_disk"]], top=1)
        return to_float(rows[0].get(f["total_disk"])) if rows else None

    def endpoint_info(self, device: str) -> EndpointInfo:
        """IP and location as seen on the activity rows, else from the inventory."""
        if device in self._endpoints:
            return self._endpoints[device]
        key = "inventory"
        f = self.cfg.fields[key]
        rows = self.query(self.cfg.entities[key], f"{f['device']} eq {odata_quote(device)}", top=1)
        if not rows:
            return EndpointInfo(device=device)
        row = rows[0]
        return EndpointInfo(
            device=device,
            ip_address=row.get(f.get("ip_address", "")) or None,
            location=row.get(f.get("location", "")) or None,
            os=row.get(f.get("os", "")) or None,
        )
