"""Correlation engine.

1. Pull the user's Aternity activity for the window and flag degraded time buckets.
2. Endpoint context (Aternity): CPU, memory, disk queue, round-trip time, IP and
   location from the activity rows; device health events; per-app UXI, crashes
   and hang time.
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
from .logons import logon_report
from .models import (
    AppUsageSample, DeviceHealthSample, HealthEvent, EndpointInfo, MappedObject, MetricSample, NetimAlert,
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
    # optional (a source without them simply has no events / UXI):
    #   health_events(device, start, end) -> list[HealthEvent]
    #   app_usage(device, start, end, application) -> list[AppUsageSample]
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
        log.info("NetIM: looking for the gateway of %s among %d devices", ip, len(devices))
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
    return s.response_time is not None and s.response_time > rt_limit


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


def _event_anomaly(e: HealthEvent) -> dict:
    """A device health event is evidence by itself (no threshold): it happened."""
    what = " / ".join(x for x in (e.category, e.name) if x) or "Health event"
    where = e.component or e.application
    return {"source": "Aternity event", "object": e.device,
            "metric": f"{what} ({where})" if where else what,
            "value": e.count if e.count is not None else 1, "threshold": None}


def _usage_anomalies(u: AppUsageSample) -> list[dict]:
    """Application crashes and hang time on the device."""
    out = []
    for label, value in (("App crashes", u.crashes), ("App hang time", u.hang_time)):
        if value:
            out.append({"source": "Aternity", "object": u.device, "metric": f"{label} ({u.application})",
                        "value": round(value, 2), "threshold": 0, "cmp": ">"})
    return out


def _rate(slow: int, users: int) -> float | None:
    return slow / users if users else None


def _who_else(aternity: AternitySource, req: InvestigationRequest, samples: list[UserSample],
              degraded: set[datetime], location: str | None, settings: Settings) -> dict | None:
    """Other users of the same application(s) during the user's slow intervals.

    Scope uses a plain majority: most other users at the user's location slow ->
    site; most users elsewhere slow too -> everyone; otherwise only this user.
    """
    if not degraded or not hasattr(aternity, "app_activity"):
        return None
    minutes = settings.bucket_minutes
    slow_own = [s for s in samples if _bucket(s.timestamp, minutes) in degraded and _is_degraded(s, settings)]
    apps = [req.application] if req.application else \
        [a for a, _ in Counter(s.application for s in slow_own if s.application).most_common(3)]
    if not apps:
        return None
    start, end = min(degraded), max(degraded) + timedelta(minutes=minutes)
    who = req.username or req.device
    log.info("Aternity: comparing with other users of %s, %s to %s UTC", ", ".join(apps),
             start.strftime("%H:%M"), end.strftime("%H:%M"))
    rows = aternity.app_activity(apps, start, end)
    users: dict[str, dict] = {}
    for s in rows:
        key = s.username or s.device
        if not key or key == who or (not req.username and s.device == req.device):
            continue
        if _bucket(s.timestamp, minutes) not in degraded:
            continue  # same time = the user's own slow intervals
        u = users.setdefault(key, {"username": s.username, "device": s.device, "locations": Counter(),
                                   "slow": [], "activities": 0})
        u["activities"] += 1
        if s.location:
            u["locations"][s.location] += 1
        if _is_degraded(s, settings):
            u["slow"].append(s)
    by_loc: dict[str, dict] = {}
    for u in users.values():
        u["location"] = u.pop("locations").most_common(1)[0][0] if u["locations"] else None
        g = by_loc.setdefault(u["location"] or "(unknown)", {"location": u["location"] or "(unknown)",
                                                             "users": 0, "slow": 0})
        g["users"] += 1
        g["slow"] += bool(u["slow"])
    here = [u for u in users.values() if location and u["location"] == location]
    there = [u for u in users.values() if not (location and u["location"] == location)]
    same = {"users": len(here), "slow": sum(bool(u["slow"]) for u in here)}
    other = {"users": len(there), "slow": sum(bool(u["slow"]) for u in there)}
    r_same, r_other = _rate(same["slow"], same["users"]), _rate(other["slow"], other["users"])
    loc = location or "the user's location"
    counts = (f"{same['slow']} of {same['users']} other users at {loc} and "
              f"{other['slow']} of {other['users']} elsewhere were slow at the same time")
    if not users:
        scope, text = "unknown", f"No other users of {', '.join(apps)} were active at the same time."
    elif (r_other or 0) >= 0.5 and (r_same is None or r_same >= 0.5):
        scope, text = "everyone", f"Everyone using {', '.join(apps)}: {counts}. Points at the application or its servers."
    elif r_same is not None and r_same >= 0.5:
        scope, text = "site", f"Site {loc}: {counts}. Points at {loc} or its network path."
    else:
        scope, text = "user", f"Only {who}: {counts}. Points at {who}'s device or connection."
    slow_peer_samples = [s for u in users.values() for s in u["slow"]]
    affected = sorted((u for u in users.values() if u["slow"]),
                      key=lambda u: (-len(u["slow"]), u["username"] or u["device"]))
    log.info("Aternity: %d other users active then, %d of them slow", len(users), len(affected))
    return {
        "applications": apps, "start": _iso(start), "end": _iso(end), "location": location,
        "scope": scope, "text": text, "same_location": same, "other_locations": other,
        "slow_time_split": {d: round(v, 3) for d, v in _breakdown_shares(slow_peer_samples).items()},
        "by_location": sorted(by_loc.values(), key=lambda g: (-g["slow"], -g["users"]))[:15],
        "affected_users": [{
            "username": u["username"], "device": u["device"], "location": u["location"],
            "slow_activities": len(u["slow"]), "activities": u["activities"],
            "worst_response_time": round(max(s.response_time for s in u["slow"]), 2),
        } for u in affected[:100]],
        "affected_total": len(affected),
    }


def _cause(verdict: str, evidence: list[dict], impact: dict | None) -> str | None:
    """One line: who is affected and the strongest measurement behind the verdict."""
    parts = [impact["text"]] if impact else []
    # a named NetIM device or interface says more than the endpoint's own view
    ranked = sorted((e for e in evidence if e["domain"] == verdict),
                    key=lambda e: (not e["source"].startswith("NetIM"), -e["coverage"]))
    top = ranked[0] if ranked else None
    if top:
        value = "" if top["worst"] is None else f" {top['worst']}"
        limit = "" if top["threshold"] in (None, 0) else f" (limit {top['threshold']})"
        parts.append(f"Measured: {top['object']} {top['metric']}{value}{limit} "
                     f"in {top['coverage']:.0%} of the slow intervals.")
    return " ".join(parts) or None


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
    events: list[HealthEvent] = []
    usage: list[AppUsageSample] = []
    for d in device_names:
        if hasattr(aternity, "health_events"):
            events += aternity.health_events(d, req.start, req.end)
        if hasattr(aternity, "app_usage"):
            usage += aternity.app_usage(d, req.start, req.end, req.application)
    log.info("Aternity: %d device health events, %d application UXI rows", len(events), len(usage))
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
    buckets: dict[datetime, dict] = defaultdict(lambda: {"samples": [], "uxi": [], **{d: [] for d in DOMAINS}})
    for s in samples:
        buckets[_bucket(s.timestamp, minutes)]["samples"].append(s)
    for h in health:
        for domain, a in _endpoint_anomalies(h, settings):
            buckets[_bucket(h.timestamp, minutes)][domain].append(a)
    for e in events:
        buckets[_bucket(e.timestamp, minutes)]["client"].append(_event_anomaly(e))
    for u in usage:
        b = buckets[_bucket(u.timestamp, minutes)]
        b["client"] += _usage_anomalies(u)
        if u.uxi is not None:
            b["uxi"].append(u.uxi)

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
        uxis = b["uxi"]
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
        if scores[verdict]["overlap"] == 0:
            # Aternity's time split alone is a hint, not a verdict: nothing measured lined up
            hint = max(shares, key=shares.get) if shares else None
            verdict, confidence = "inconclusive", 0.0
            headline = (f"{len(degraded)} slow interval(s), but no device, network or server "
                        "measurement lined up with them")
            headline += (f"; Aternity counts {shares[hint]:.0%} of the slow time as "
                         f"{DOMAIN_LABELS[hint].lower()} time." if hint else ".")
        elif confidence < 0.25:
            verdict = "inconclusive"
            headline = (f"{len(degraded)} degraded interval(s) found but no domain correlates strongly; "
                        "widen the window or add NetIM topology hints.")
        else:
            headline = (f"{len(degraded)} degraded interval(s); most likely cause: "
                        f"{DOMAIN_LABELS[verdict]} (score {confidence:.2f}).")

    all_uxi = [u.uxi for u in usage if u.uxi is not None]
    user_location = next((e.location for e in endpoints if e.location), None) or \
        next((s.location for s in samples if s.location), None)
    impact = _who_else(aternity, req, samples, degraded, user_location, settings)
    warnings.extend(w for w in getattr(aternity, "warnings", []) if w not in warnings)
    cause = _cause(verdict, evidence, impact)
    logons = logon_report(aternity, req.start, req.end, username=req.username, device=req.device, top=10)
    warnings.extend(w for w in logons.pop("warnings", []) if w not in warnings)
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
            # Aternity's own UXI for the window (as Aternity reports it), and event count
            "uxi_avg": round(mean(all_uxi), 1) if all_uxi else None,
            "uxi_min": round(min(all_uxi), 1) if all_uxi else None,
            "health_events": len(events),
            "cause": cause,
        },
        "impact": impact,
        "logons": logons,
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
