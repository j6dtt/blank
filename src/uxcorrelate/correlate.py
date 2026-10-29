"""Correlation engine.

1. Pull the user's Aternity activity for the window and flag degraded time buckets.
2. Endpoint context (Aternity): CPU, memory, disk queue, round-trip time, IP and
   location from the activity rows.
3. Map the user and application onto NetIM objects:
     client-gateway      L3 interface whose subnet contains the endpoint IP
     site                devices configured for the endpoint's Aternity location
     app-server          NetIM device matching an application server name/IP
     app-server-network  L3 interface whose subnet contains an application server IP
     always              devices configured in topology.always_include
4. Pull NetIM metrics and alerts for those objects and flag anomalous buckets.
5. Score each domain (client device / network / application backend) by how well
   its anomalies line up with the degraded buckets, blended with Aternity's
   client/network/backend response-time breakdown.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Protocol

from .config import NetimMetric, Settings
from .models import (
    DeviceHealthSample, EndpointInfo, MappedObject, MetricSample, NetimAlert,
    NetimDevice, NetimInterface, UserSample,
)

log = logging.getLogger("uxcorrelate")

DOMAINS = ("client", "network", "backend")
DOMAIN_LABELS = {
    "client": "Client device",
    "network": "Network",
    "backend": "Application backend",
}
ROLE_DOMAIN = {
    "client-gateway": "network",
    "site": "network",
    "app-server-network": "network",
    "always": "network",
    "app-server": "backend",
}
RECOMMENDATIONS = {
    "client": [
        "Review processes consuming CPU/memory on the endpoint (Aternity device health / process view).",
        "Check for recent software, patch or security agent changes on the device.",
    ],
    "network": [
        "Inspect the flagged NetIM interfaces for saturation, errors and discards during the window.",
        "Check QoS policy and top talkers on the congested link (NetProfiler / AppResponse if available).",
        "Compare with other users at the same location to confirm a site-wide impact.",
    ],
    "backend": [
        "Check application server and database health for the window (APM / server monitoring).",
        "Confirm whether other users of the application were affected at the same time.",
        "Review recent application deployments or changes.",
    ],
}


class AternitySource(Protocol):
    def user_activity(self, start: datetime, end: datetime, username: str | None = None,
                      device: str | None = None, application: str | None = None) -> list[UserSample]: ...
    def device_health(self, device: str, start: datetime, end: datetime) -> list[DeviceHealthSample]: ...
    def endpoint_info(self, device: str) -> EndpointInfo: ...


class NetimSource(Protocol):
    warnings: list[str]
    def devices(self) -> list[NetimDevice]: ...
    def interfaces(self, device_id: str) -> list[NetimInterface]: ...
    def metrics(self, metric: NetimMetric, object_ids, start: datetime, end: datetime) -> list[MetricSample]: ...
    def alerts(self, start: datetime, end: datetime) -> list[NetimAlert]: ...


@dataclass
class InvestigationRequest:
    start: datetime
    end: datetime
    username: str | None = None
    device: str | None = None
    application: str | None = None

    def validate(self) -> None:
        if not (self.username or self.device):
            raise ValueError("Provide a username or a device name")
        if self.end <= self.start:
            raise ValueError("end must be after start")


# ---------------------------------------------------------------- helpers
def _bucket(ts: datetime, minutes: int) -> datetime:
    ts = ts.astimezone(timezone.utc)
    return ts.replace(minute=ts.minute - ts.minute % minutes, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _networks(entries: list[str]) -> list[ipaddress.IPv4Interface | ipaddress.IPv6Interface]:
    out = []
    for e in entries:
        try:
            out.append(ipaddress.ip_interface(e))
        except ValueError:
            continue
    return out


def _subnet_contains(entries: list[str], ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n.network for n in _networks(entries))


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- mapping
def map_netim_objects(netim: NetimSource, endpoints: list[EndpointInfo], application: str | None,
                      settings: Settings) -> list[MappedObject]:
    topo = settings.topology
    devices = netim.devices()
    by_name = {d.name.lower(): d for d in devices}
    by_ip = {d.ip_address: d for d in devices if d.ip_address}
    mapped: dict[tuple[str, str], MappedObject] = {}

    def add(obj: MappedObject) -> None:
        mapped.setdefault((obj.object_type, obj.object_id), obj)

    def add_device(dev: NetimDevice, role: str, reason: str, with_l3_interfaces: bool) -> None:
        add(MappedObject("DEVICE", dev.id, dev.name, dev.id, role, reason))
        if with_l3_interfaces:
            for itf in netim.interfaces(dev.id):
                if itf.ip_addresses:
                    add(MappedObject("INTERFACE", itf.id, f"{dev.name}:{itf.name}", dev.id, role, reason))

    def add_gateways_for(ip: str, role: str, reason: str) -> None:
        log.info("NetIM: looking for the gateway of %s on %d devices (one call each, can take minutes)",
                 ip, len(devices))
        for n, dev in enumerate(devices, 1):
            if n % 25 == 0:
                log.info("NetIM: checked %d of %d devices", n, len(devices))
            for itf in netim.interfaces(dev.id):
                if _subnet_contains(itf.ip_addresses, ip):
                    # keep an earlier role for the device (e.g. the app server itself)
                    existing = mapped.get(("DEVICE", dev.id))
                    r = existing.role if existing else role
                    add(MappedObject("DEVICE", dev.id, dev.name, dev.id, r, reason))
                    add(MappedObject("INTERFACE", itf.id, f"{dev.name}:{itf.name}", dev.id, r, reason))

    for ep in endpoints:
        if ep.ip_address:
            add_gateways_for(ep.ip_address, "client-gateway",
                             f"subnet contains endpoint {ep.device} ({ep.ip_address})")
        if ep.location:
            for name in topo.sites.get(ep.location, []):
                dev = by_name.get(name.lower())
                if dev:
                    add_device(dev, "site", f"site device for location '{ep.location}'", True)

    if application:
        for server in topo.applications.get(application, []):
            dev = by_ip.get(server) or by_name.get(server.lower())
            if dev:
                add_device(dev, "app-server", f"server for application '{application}'", False)
            server_ip = server if _is_ip(server) else (dev.ip_address if dev else None)
            if server_ip:
                add_gateways_for(server_ip, "app-server-network",
                                 f"subnet contains '{application}' server {server_ip}")

    for name in topo.always_include:
        dev = by_name.get(name.lower())
        if dev:
            add_device(dev, "always", "always included (topology.always_include)", True)

    return list(mapped.values())


# ---------------------------------------------------------------- engine
def _is_degraded(s: UserSample, settings: Settings) -> bool:
    th = settings.thresholds
    rt_limit = th.response_time_s_by_app.get(s.application, th.response_time_s)
    if s.response_time is not None and s.response_time > rt_limit:
        return True
    return s.uxi is not None and s.uxi < th.uxi_min


def _endpoint_anomalies(h: DeviceHealthSample, settings: Settings) -> list[tuple[str, dict]]:
    """Aternity device-health anomalies as (domain, anomaly) pairs."""
    th = settings.thresholds
    checks = [
        ("client", h.cpu, th.device_cpu, ">", "CPU %"),
        ("client", h.memory, th.device_memory, ">", "Memory %"),
        ("client", h.disk_queue, th.disk_queue, ">", "Disk queue"),
        # latency measured from the endpoint points at the network, not the device
        ("network", h.network_latency, th.client_network_latency_ms, ">", "Endpoint network latency ms"),
    ]
    out = []
    for domain, value, limit, cmp, label in checks:
        if value is None:
            continue
        if (cmp == ">" and value > limit) or (cmp == "<" and value < limit):
            out.append((domain, {"source": "Aternity", "object": h.device, "metric": label,
                                 "value": round(value, 2), "threshold": limit, "cmp": cmp}))
    return out


def investigate(req: InvestigationRequest, aternity: AternitySource, netim: NetimSource | None,
                settings: Settings) -> dict:
    req.validate()
    minutes = settings.bucket_minutes
    warnings: list[str] = []
    started = time.monotonic()
    who = ", ".join(f"{k} {v}" for k, v in (("user", req.username), ("device", req.device),
                                            ("app", req.application)) if v)
    log.info("Investigating %s, %s to %s UTC", who, req.start.strftime("%Y-%m-%d %H:%M"),
             req.end.strftime("%H:%M"))

    # 1. user experience
    samples = aternity.user_activity(req.start, req.end, username=req.username,
                                     device=req.device, application=req.application)
    if req.device:
        device_names = [req.device]
    else:
        device_names = [d for d, _ in Counter(s.device for s in samples if s.device).most_common(3)]

    log.info("Aternity: %d activity samples, device(s): %s", len(samples), ", ".join(device_names) or "none")

    # 2. endpoint
    endpoints = [aternity.endpoint_info(d) for d in device_names]
    health = [h for d in device_names for h in aternity.device_health(d, req.start, req.end)]
    warnings.extend(getattr(aternity, "warnings", []))

    # 3/4. infrastructure
    mapped: list[MappedObject] = []
    metric_samples: list[MetricSample] = []
    alerts: list[NetimAlert] = []
    if netim is not None:
        mapped = map_netim_objects(netim, endpoints, req.application, settings)
        log.info("NetIM: %d objects on the user's path; reading %d metrics",
                 len(mapped), len(settings.netim.metrics))
        for metric in settings.netim.metrics:
            ids = [m.object_id for m in mapped if m.object_type == metric.object_type]
            metric_samples.extend(netim.metrics(metric, ids, req.start, req.end))
        mapped_device_ids = {m.device_id for m in mapped}
        alerts = [a for a in netim.alerts(req.start, req.end)
                  if a.object_id in mapped_device_ids or a.object_id in {m.object_id for m in mapped}]
        warnings.extend(netim.warnings)
        netim.warnings.clear()
        if not mapped:
            warnings.append("No NetIM objects could be mapped to this user/application; "
                            "network evidence is limited. Add topology hints in the config.")
    else:
        warnings.append("NetIM not configured; network evidence comes from Aternity only.")
    if not samples:
        warnings.append("Aternity returned no activity for this user/device/application in the window.")

    # bucketize
    buckets: dict[datetime, dict] = defaultdict(lambda: {"samples": [], **{d: [] for d in DOMAINS}})
    for s in samples:
        buckets[_bucket(s.timestamp, minutes)]["samples"].append(s)
    for h in health:
        for domain, a in _endpoint_anomalies(h, settings):
            buckets[_bucket(h.timestamp, minutes)][domain].append(a)

    metric_defs = {m.key: m for m in settings.netim.metrics}
    obj_index = {(m.object_type, m.object_id): m for m in mapped}
    for ms in metric_samples:
        metric = metric_defs[ms.metric_key]
        obj = obj_index.get((ms.object_type, ms.object_id))
        if obj is None or not metric.is_anomalous(ms.value):
            continue
        buckets[_bucket(ms.timestamp, minutes)][ROLE_DOMAIN[obj.role]].append({
            "source": "NetIM", "object": obj.name, "role": obj.role, "metric": metric.label,
            "value": round(ms.value, 2), "threshold": metric.threshold, "cmp": metric.comparison,
        })
    device_roles = {m.device_id: m.role for m in mapped if m.object_type == "DEVICE"}
    for al in alerts:
        role = device_roles.get(al.object_id, "always")
        t = _bucket(al.start, minutes)
        stop = al.end or req.end
        while t <= stop and t <= req.end:
            buckets[t][ROLE_DOMAIN[role]].append({
                "source": "NetIM alert", "object": al.object_name, "role": role,
                "metric": f"{al.severity}: {al.message}", "value": None, "threshold": None,
            })
            t += timedelta(minutes=minutes)

    # timeline
    timeline = []
    degraded: set[datetime] = set()
    for t in sorted(buckets):
        b = buckets[t]
        ss = b["samples"]
        rts = [s.response_time for s in ss if s.response_time is not None]
        uxis = [s.uxi for s in ss if s.uxi is not None]
        is_deg = any(_is_degraded(s, settings) for s in ss)
        if is_deg:
            degraded.add(t)
        timeline.append({
            "time": _iso(t),
            "samples": len(ss),
            "avg_response_time": round(mean(rts), 3) if rts else None,
            "avg_uxi": round(mean(uxis), 1) if uxis else None,
            "degraded": is_deg,
            **{d: b[d] for d in DOMAINS},
        })

    # scoring
    deg_samples = [s for s in samples if _is_degraded(s, settings)]
    shares = _breakdown_shares(deg_samples)
    scores = {}
    for d in DOMAINS:
        anomalous = {t for t, b in buckets.items() if b[d]}
        overlap = len(degraded & anomalous) / len(degraded) if degraded else 0.0
        noise = len(anomalous - degraded) / len(anomalous) if anomalous else 0.0
        corr = overlap * (1 - 0.3 * noise)
        share = shares.get(d)
        score = corr if share is None else 0.5 * corr + 0.5 * share
        scores[d] = {
            "label": DOMAIN_LABELS[d],
            "score": round(score, 3),
            "overlap": round(overlap, 3),
            "anomalous_buckets": len(anomalous),
            "breakdown_share": None if share is None else round(share, 3),
        }

    evidence = _evidence(buckets, degraded)
    if not degraded:
        verdict, confidence = "none", 0.0
        headline = "No degraded user experience found in the selected window."
    else:
        verdict = max(DOMAINS, key=lambda d: scores[d]["score"])
        confidence = scores[verdict]["score"]
        if confidence < 0.25:
            verdict = "inconclusive"
            headline = (f"{len(degraded)} degraded interval(s) found but no domain correlates strongly; "
                        "widen the window or add NetIM topology hints.")
        else:
            headline = (f"{len(degraded)} degraded interval(s); most likely cause: "
                        f"{DOMAIN_LABELS[verdict]} (score {confidence:.2f}).")

    log.info("Done in %.0f s: %s", time.monotonic() - started, headline)
    return {
        "request": {
            "username": req.username, "device": req.device, "application": req.application,
            "start": _iso(req.start), "end": _iso(req.end), "bucket_minutes": minutes,
        },
        "summary": {
            "verdict": verdict,
            "verdict_label": DOMAIN_LABELS.get(verdict, verdict.title()),
            "confidence": round(confidence, 3),
            "headline": headline,
            "samples": len(samples),
            "degraded_samples": len(deg_samples),
            "degraded_intervals": len(degraded),
        },
        "scores": scores,
        "evidence": evidence,
        "recommendations": RECOMMENDATIONS.get(verdict, []),
        "endpoints": [asdict(e) for e in endpoints],
        "mapped_objects": [asdict(m) for m in mapped],
        "timeline": timeline,
        "warnings": warnings,
    }


def _breakdown_shares(samples: list[UserSample]) -> dict[str, float]:
    """Average fraction of response time spent in client / network / backend."""
    acc = {d: [] for d in DOMAINS}
    for s in samples:
        parts = {"client": s.client_time, "network": s.network_time, "backend": s.backend_time}
        if any(v is None for v in parts.values()):
            continue
        total = sum(parts.values())
        if total <= 0:
            continue
        for d in DOMAINS:
            acc[d].append(parts[d] / total)
    return {d: mean(v) for d, v in acc.items() if v}


def _evidence(buckets: dict, degraded: set[datetime]) -> list[dict]:
    """Group anomalies seen during degraded intervals by object+metric."""
    grouped: dict[tuple, dict] = {}
    for t in sorted(degraded):
        for d in DOMAINS:
            for a in buckets[t][d]:
                key = (d, a["source"], a["object"], a["metric"])
                g = grouped.setdefault(key, {
                    "domain": d, "source": a["source"], "object": a["object"],
                    "role": a.get("role"), "metric": a["metric"], "threshold": a["threshold"],
                    "worst": None, "intervals": set(), "first": t, "last": t,
                })
                g["intervals"].add(t)
                g["last"] = t
                if a["value"] is not None:
                    pick = min if a.get("cmp") == "<" else max
                    g["worst"] = a["value"] if g["worst"] is None else pick(g["worst"], a["value"])
    out = []
    for g in grouped.values():
        n = len(g.pop("intervals"))
        g["degraded_intervals_matched"] = n
        g["coverage"] = round(n / len(degraded), 3)
        g["first"], g["last"] = _iso(g["first"]), _iso(g["last"])
        out.append(g)
    out.sort(key=lambda g: (-g["coverage"], g["domain"]))
    return out
