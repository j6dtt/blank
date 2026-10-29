"""Riverbed NetIM REST API client.

Base URL is the NetIM core, https://<netim-core>:8543. Paths, request bodies
and response layouts follow Riverbed's own steelscript-netim SDK:
  devices / interfaces / metric classes  GET  {api_prefix}/...  (items + meta paging)
  metric data   POST {metric_data_path}  body elementIds, elementType (VNES_OE for
                devices, VNES_INTERFACE), metricClass, metrics, startTime, endTime;
                reply metricData.metricElementDataList[].timestampToValuesMap
The alerts path is not in any public Riverbed source, so it is off by default.
`uxcorrelate verify` checks all of this against a live NetIM.
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


ELEMENT_TYPES = {"DEVICE": "VNES_OE", "INTERFACE": "VNES_INTERFACE"}


def metric_request_body(metric: NetimMetric, object_ids: list[str], start: datetime, end: datetime) -> dict:
    """Network Metric Data request, as built by steelscript-netim's _get_metric_data."""
    return {
        "elementIds": [int(i) if str(i).isdigit() else i for i in object_ids],
        "elementType": ELEMENT_TYPES[metric.object_type],
        "metricClass": metric.metric_class,
        "metrics": [metric.metric],
        "startTime": _ms(start),
        "endTime": _ms(end),
        "includeSamples": True,
        "metricEpochEnum": "RAW",
        "pageSize": 1000,
        "rollupCriterias": ["aggregateAvgRollup"],
        "sortOrder": "ASCENDING",
        "aggregations": [],
    }


def _parse_riverbed_metric_data(data: dict, metric: NetimMetric, object_type: str) -> list[MetricSample]:
    """The layout steelscript-netim reads: metricNames index each timestamp's value list."""
    md = data.get("metricData") or {}
    names = md.get("metricNames") or []
    id_map = data.get("elementIdToObjectInfoMap") or {}
    samples = []
    for el in md.get("metricElementDataList") or []:
        info = id_map.get(str(el.get("elementId"))) or {}
        obj_id = info.get("objectId", el.get("elementId"))
        for ts, values in (el.get("timestampToValuesMap") or {}).items():
            for name, value in zip(names, values or []):
                t, v = parse_time(ts), to_float(value)
                if name == metric.metric and obj_id is not None and t is not None and v is not None:
                    samples.append(MetricSample(object_type, str(obj_id), metric.key, t, v))
    return samples


def parse_metric_response(data: Any, metric: NetimMetric, object_type: str) -> list[MetricSample]:
    """Riverbed's metricData layout first; flat or nested series as a fallback."""
    if isinstance(data, dict) and isinstance(data.get("metricData"), dict):
        return _parse_riverbed_metric_data(data, metric, object_type)
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
            timeout=cfg.timeout, transport=transport,
        )
        self._devices: list[NetimDevice] | None = None
        self._interfaces: dict[str, list[NetimInterface]] = {}
        self.warnings: list[str] = []

    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        """Follow meta.next_offset like steelscript-netim; fall back to short pages."""
        out: list[dict] = []
        offset = 0
        while True:
            q = dict(params or {}, offset=offset, limit=self.cfg.page_size)
            data = self.http.get_json(path, params=q)
            batch = _items(data)
            out.extend(batch)
            meta = data.get("meta") if isinstance(data, dict) else None
            if isinstance(meta, dict) and "next_offset" in meta:
                nxt, total = meta.get("next_offset"), meta.get("total")
                if nxt is None or (total is not None and nxt >= total) or not batch:
                    return out
                offset = nxt
            elif len(batch) < self.cfg.page_size:
                return out
            else:
                offset += len(batch)

    def devices(self) -> list[NetimDevice]:
        if self._devices is None:
            rows = self._paged(f"{self.cfg.api_prefix}/devices")
            self._devices = [
                NetimDevice(
                    id=str(r.get("id")),
                    name=str(_first(r, "deviceName", "name", "displayName", "sysName", default=r.get("id"))),
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
                    name=str(_first(r, "displayName", "name", "ifName", "ifDescr", default=r.get("id"))),
                    ip_addresses=_parse_ip_entries(
                        _first(r, "ipAddresses", "ipAddressList", "addresses", "ipAddress")
                    ),
                    speed_bps=to_float(_first(r, "speed", "ifSpeed", "ifHighSpeed")),
                )
                for r in rows
            ]
        return self._interfaces[device_id]

    def metric_classes(self) -> Any:
        return {"items": self._paged(f"{self.cfg.api_prefix}/metric-classes")}

    def metric_class(self, class_id: str) -> Any:
        """One metric class with its `metrics` list."""
        return self.http.get_json(f"{self.cfg.api_prefix}/metric-classes/{class_id}")

    def metrics(self, metric: NetimMetric, object_ids: Iterable[str],
                start: datetime, end: datetime) -> list[MetricSample]:
        ids = list(object_ids)
        if not ids:
            return []
        try:
            data = self.http.post_json(self.cfg.metric_data_path, metric_request_body(metric, ids, start, end))
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
