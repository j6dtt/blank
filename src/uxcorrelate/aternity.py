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
from .http import RestClient
from .models import DeviceHealthSample, EndpointInfo, UserSample, parse_time, to_float


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
              orderby: str | None = None) -> list[dict]:
        params: dict[str, str] = {"$format": "json"}
        if filter:
            params["$filter"] = filter
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
                activity=row.get(f.get("activity", "")),
                response_time=to_float(row.get(f.get("response_time", ""))),
                client_time=to_float(row.get(f.get("client_time", ""))),
                network_time=to_float(row.get(f.get("network_time", ""))),
                backend_time=to_float(row.get(f.get("backend_time", ""))),
                uxi=to_float(row.get(f.get("uxi", ""))),
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
