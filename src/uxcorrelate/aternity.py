"""Riverbed Aternity Data Source REST API (OData) client.

The base URL is the tenant's OData endpoint, e.g.
    https://<tenant-odata-host>/aternity.odata/v2.0
Authentication is HTTP basic with the Aternity user name and the REST API
access token (or password) generated in Aternity settings.
"""

from __future__ import annotations

from datetime import datetime

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
            verify=cfg.verify_tls, timeout=cfg.timeout, transport=transport,
        )

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
        rows: list[dict] = []
        url: str | None = f"/{entity}"
        pages = 0
        while url and pages < self.cfg.max_pages:
            data = self.http.get_json(url, params=params if pages == 0 else None)
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
            samples.append(UserSample(
                timestamp=ts,
                username=str(row.get(f["username"], "")),
                device=str(row.get(f["device"], "")),
                application=str(row.get(f["application"], "")),
                activity=row.get(f.get("activity", "")),
                response_time=to_float(row.get(f.get("response_time", ""))),
                client_time=to_float(row.get(f.get("client_time", ""))),
                network_time=to_float(row.get(f.get("network_time", ""))),
                backend_time=to_float(row.get(f.get("backend_time", ""))),
                uxi=to_float(row.get(f.get("uxi", ""))),
            ))
        return samples

    def device_health(self, device: str, start: datetime, end: datetime) -> list[DeviceHealthSample]:
        key = "device_health"
        flt = f"{self._time_filter(key, start, end)} and {self._col(key, 'device')} eq {odata_quote(device)}"
        f = self.cfg.fields[key]
        samples = []
        for row in self.query(self.cfg.entities[key], flt):
            ts = parse_time(row.get(f["timestamp"]))
            if ts is None:
                continue
            samples.append(DeviceHealthSample(
                timestamp=ts,
                device=device,
                cpu=to_float(row.get(f.get("cpu", ""))),
                memory=to_float(row.get(f.get("memory", ""))),
                disk_queue=to_float(row.get(f.get("disk_queue", ""))),
                wifi_signal=to_float(row.get(f.get("wifi_signal", ""))),
                network_latency=to_float(row.get(f.get("network_latency", ""))),
            ))
        return samples

    def endpoint_info(self, device: str) -> EndpointInfo:
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
