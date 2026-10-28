"""Riverbed NetIM REST API client.

Base URL is the NetIM core, e.g. https://netim.example.com:8543
Model data (devices, interfaces) comes from the core API under `api_prefix`;
metric data from the Network Metric Data API at `metric_data_path`; active
alerts from `alerts_path`. Paths and payload field names vary by NetIM
release, so they are configurable and the response parsing below accepts the
common shapes. Browse https://<netim>:8543/api-docs to confirm yours.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import httpx

from .config import NetimConfig, NetimMetric
from .http import ApiError, RestClient
from .models import (
    MetricSample, NetimAlert, NetimDevice, NetimInterface, parse_time, to_float,
)


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _items(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "data", "results", "value"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _parse_ip_entries(raw) -> list[str]:
    """Accept ['10.0.0.1/24'], [{'ipAddress':..,'prefixLength':..}], or a bare string."""
    if raw is None:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    out = []
    for entry in raw:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            ip = _first(entry, "ipAddress", "address", "ip")
            if not ip:
                continue
            prefix = _first(entry, "prefixLength", "prefix", "maskLength")
            mask = _first(entry, "subnetMask", "netmask", "mask")
            if prefix is not None:
                out.append(f"{ip}/{prefix}")
            elif mask:
                out.append(f"{ip}/{mask}")
            else:
                out.append(ip)
    return out


def parse_metric_response(data: Any, metric: NetimMetric, object_type: str) -> list[MetricSample]:
    """Normalize flat (one point per item) or nested (series per object) responses."""
    samples: list[MetricSample] = []

    def add(obj_id, ts, value):
        t, v = parse_time(ts), to_float(value)
        if obj_id is not None and t is not None and v is not None:
            samples.append(MetricSample(object_type, str(obj_id), metric.key, t, v))

    for item in _items(data):
        obj = item.get("object") if isinstance(item.get("object"), dict) else {}
        obj_id = _first(item, "objectId", "deviceId", "interfaceId", "id", default=obj.get("id"))
        name = _first(item, "metric", "metricName", "name")
        if name and name != metric.metric:
            continue
        series = _first(item, "samples", "dataPoints", "datapoints", "values", "data")
        if isinstance(series, list):
            for point in series:
                if isinstance(point, dict):
                    add(obj_id, _first(point, "timestamp", "time", "ts"),
                        _first(point, "value", "avg", metric.metric))
                elif isinstance(point, (list, tuple)) and len(point) >= 2:
                    add(obj_id, point[0], point[1])
        else:
            add(obj_id, _first(item, "timestamp", "time", "ts"),
                _first(item, "value", "avg", metric.metric))
    return samples


class NetimClient:
    def __init__(self, cfg: NetimConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.http = RestClient(
            "NetIM", cfg.base_url, cfg.username, cfg.password,
            verify=cfg.verify_tls, timeout=cfg.timeout, transport=transport,
        )
        self._devices: list[NetimDevice] | None = None
        self._interfaces: dict[str, list[NetimInterface]] = {}
        self.warnings: list[str] = []

    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            q = dict(params or {}, offset=offset, limit=self.cfg.page_size)
            batch = _items(self.http.get_json(path, params=q))
            out.extend(batch)
            if len(batch) < self.cfg.page_size:
                return out
            offset += len(batch)

    def devices(self) -> list[NetimDevice]:
        if self._devices is None:
            rows = self._paged(f"{self.cfg.api_prefix}/devices")
            self._devices = [
                NetimDevice(
                    id=str(r.get("id")),
                    name=str(_first(r, "name", "deviceName", "displayName", default=r.get("id"))),
                    ip_address=_first(r, "accessAddress", "ipAddress", "primaryAddress"),
                    device_type=_first(r, "deviceType", "type"),
                    site=_first(r, "siteName", "site", "location"),
                )
                for r in rows
            ]
        return self._devices

    def interfaces(self, device_id: str) -> list[NetimInterface]:
        if device_id not in self._interfaces:
            rows = self._paged(f"{self.cfg.api_prefix}/devices/{device_id}/interfaces")
            self._interfaces[device_id] = [
                NetimInterface(
                    id=str(r.get("id")),
                    device_id=device_id,
                    name=str(_first(r, "name", "ifName", "ifDescr", "displayName", default=r.get("id"))),
                    ip_addresses=_parse_ip_entries(
                        _first(r, "ipAddresses", "ipAddressList", "addresses", "ipAddress")
                    ),
                    speed_bps=to_float(_first(r, "speed", "ifSpeed", "ifHighSpeed")),
                )
                for r in rows
            ]
        return self._interfaces[device_id]

    def metric_classes(self) -> Any:
        return self.http.get_json(f"{self.cfg.api_prefix}/metric-classes")

    def metrics(self, metric: NetimMetric, object_ids: Iterable[str],
                start: datetime, end: datetime) -> list[MetricSample]:
        ids = list(object_ids)
        if not ids:
            return []
        body = {
            "objectType": metric.object_type,
            "objectIds": ids,
            "metricClass": metric.metric_class,
            "metrics": [metric.metric],
            "startTime": _ms(start),
            "endTime": _ms(end),
        }
        try:
            data = self.http.post_json(self.cfg.metric_data_path, body)
        except ApiError as exc:
            self.warnings.append(f"NetIM metric {metric.key} unavailable: {exc}")
            return []
        return parse_metric_response(data, metric, metric.object_type)

    def alerts(self, start: datetime, end: datetime) -> list[NetimAlert]:
        if not self.cfg.alerts_path:
            return []
        try:
            data = self.http.get_json(
                self.cfg.alerts_path, params={"startTime": _ms(start), "endTime": _ms(end)}
            )
        except ApiError as exc:
            self.warnings.append(f"NetIM alerts unavailable: {exc}")
            return []
        alerts = []
        for r in _items(data):
            started = parse_time(_first(r, "startTime", "creationTime", "timestamp"))
            if started is None:
                continue
            alerts.append(NetimAlert(
                object_id=str(_first(r, "objectId", "deviceId", "elementId", default="")),
                object_name=str(_first(r, "objectName", "deviceName", "elementName", default="")),
                severity=str(_first(r, "severity", "level", default="unknown")),
                message=str(_first(r, "message", "description", "name", default="")),
                start=started,
                end=parse_time(_first(r, "endTime", "clearTime")),
            ))
        return alerts
