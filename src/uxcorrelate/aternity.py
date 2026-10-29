"""Riverbed Aternity Data Source REST API (OData) client.

The base URL is the tenant's OData endpoint, e.g.
    https://<tenant-odata-host>/aternity.odata/v2.0
Authentication is HTTP basic with the Aternity user name and the REST API
access token (or password) generated in Aternity settings.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx

from .config import AternityConfig
from .http import ApiError, RestClient
from .models import (
    AppUsageSample, BootComponent, BootSample, DeviceHealthSample, EndpointInfo, HealthEvent, UserSample, parse_time, to_float,
)


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
              orderby: str | None = None, select: list[str] | None = None) -> list[dict]:
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
            dev = str(row.get(f["device"], ""))
            samples.append(UserSample(
                timestamp=ts,
                username=str(row.get(f["username"], "")),
                device=dev,
                application=str(row.get(f["application"], "")),
                location=row.get(f.get("location", "")) or None,
                activity=row.get(f.get("activity", "")),
                response_time=to_float(row.get(f.get("response_time", ""))),
                client_time=to_float(row.get(f.get("client_time", ""))),
                network_time=to_float(row.get(f.get("network_time", ""))),
                backend_time=to_float(row.get(f.get("backend_time", ""))),
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
                   "response_time", "client_time", "network_time", "backend_time")
        select = [f[k] for k in logical if f.get(k)]
        rows = self.query(self.cfg.entities[key], f"{self._time_filter(key, start, end)} and ({apps})",
                          select=select)
        out = []
        for row in rows:
            ts = parse_time(row.get(f["timestamp"]))
            if ts is not None:
                out.append(UserSample(
                    timestamp=ts,
                    username=str(row.get(f["username"], "")),
                    device=str(row.get(f["device"], "")),
                    application=str(row.get(f["application"], "")),
                    location=row.get(f.get("location", "")) or None,
                    ip_address=row.get(f.get("ip_address", "")) or None,
                    response_time=to_float(row.get(f.get("response_time", ""))),
                    client_time=to_float(row.get(f.get("client_time", ""))),
                    network_time=to_float(row.get(f.get("network_time", ""))),
                    backend_time=to_float(row.get(f.get("backend_time", ""))),
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
                    timestamp=ts, device=str(row.get(f["device"], "")),
                    username=row.get(f.get("username", "")) or None,
                    location=row.get(f.get("location", "")) or None,
                    ip_address=row.get(f.get("ip_address", "")) or None,
                    type=row.get(f.get("type", "")) or None,
                    duration=to_float(row.get(f.get("duration", ""))),
                    started=parse_time(row.get(f.get("started", ""))),
                ))
        return out

    def boot_components(self, devices: list[str], start: datetime, end: datetime) -> list[BootComponent]:
        """What the boots/logons of these devices spent their time on."""
        key = "boot_components"
        f = self.cfg.fields.get(key, {})
        if not f or not devices:
            return []
        devs = " or ".join(f"{f['device']} eq {odata_quote(d)}" for d in devices)
        out = []
        for row in self._optional(key, "boot/logon components", f"{self._time_filter(key, start, end)} and ({devs})",
                                  select=[c for c in f.values() if c]):
            ts = parse_time(row.get(f["timestamp"]))
            if ts is not None:
                out.append(BootComponent(
                    timestamp=ts, device=str(row.get(f["device"], "")),
                    type=row.get(f.get("type", "")) or None,
                    component=row.get(f.get("component", "")) or None,
                    start_type=row.get(f.get("start_type", "")) or None,
                    duration=to_float(row.get(f.get("duration", ""))),
                    degradation=to_float(row.get(f.get("degradation", ""))),
                ))
        return out

    def _optional(self, key: str, what: str, flt: str, select: list[str] | None = None) -> list[dict]:
        """Rows of an optional data source; a failure becomes a warning, not an abort."""
        if key not in self.cfg.entities or key not in self.cfg.fields:
            return []
        try:
            return self.query(self.cfg.entities[key], flt, select=select)
        except ApiError as exc:
            self.warnings.append(f"Aternity {what} unavailable: {exc}")
            return []

    def health_events(self, device: str, start: datetime, end: datetime) -> list[HealthEvent]:
        """Crashes, hangs and errors on the device (all applications)."""
        key = "health_events"
        f = self.cfg.fields.get(key, {})
        if not f:
            return []
        rows = self._optional(key, "device health events",
                              f"{self._time_filter(key, start, end)} and {f['device']} eq {odata_quote(device)}")
        out = []
        for row in rows:
            ts = parse_time(row.get(f["timestamp"]))
            if ts is not None:
                out.append(HealthEvent(
                    timestamp=ts, device=device,
                    application=row.get(f.get("application", "")) or None,
                    category=row.get(f.get("category", "")) or None,
                    name=row.get(f.get("name", "")) or None,
                    component=row.get(f.get("component", "")) or None,
                    severity=row.get(f.get("severity", "")) or None,
                    count=to_float(row.get(f.get("count", ""))),
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
                    application=str(row.get(f.get("application", ""), "")),
                    uxi=to_float(row.get(f.get("uxi", ""))),
                    crashes=to_float(row.get(f.get("crashes", ""))),
                    hang_time=to_float(row.get(f.get("hang_time", ""))),
                ))
        return out

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
