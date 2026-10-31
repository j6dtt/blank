"""Correlation engine.

1. Pull the user's Aternity activity for the window. An activity is slow when
   Aternity's own status says so (thresholds.slow_status), else when it is over
   the configured response time.
2. Problems on the PC (Aternity): CPU, memory, disk queue, round-trip time from
   the activity rows (incl. free disk space); device health events; per-app
   crashes and hang time.
3. Map the user and application onto NetIM objects:
     client-gateway      L3 interface whose subnet contains the endpoint IP
     site                devices configured for the endpoint's Aternity location
     app-server          NetIM device matching an application server name/IP
     app-server-network  L3 interface whose subnet contains an application server IP
     always              devices configured in topology.always_include
4. Pull NetIM metrics and alerts for those objects; values over their limit are
   problems on the network or the application's servers.
5. A problem counts for a slow action when it was recorded within
   `bucket_minutes` before or after it. A crash, hang or health event of one
   application counts only for that application's slow actions. The answer is
   plain counts: "Outlook hung during 3 of 4 slow actions". No scores.
6. For the problems at the most slow actions, cause.py looks for what caused
   them: where the application failed, whether other PCs fail the same way,
   what changed on the PC before, and the busiest processes.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Protocol

from .config import NetimMetric, Settings
from .answer import build_answer, problem_text
from .cause import explain
from .impact import who_else
from .logons import logon_report
from .models import (
    DeviceHealthSample, HealthEvent, EndpointInfo, MappedObject, MetricSample, NetimAlert,
    NetimDevice, NetimInterface, UserSample, parse_time,
)

log = logging.getLogger("uxcorrelate")

KINDS = ("pc", "network", "server")  # where a problem is
ROLE_DOMAIN = {
    "client-gateway": "network",
    "site": "network",
    "app-server-network": "network",
    "always": "network",
    "app-server": "server",
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
def is_slow(s: UserSample, settings: Settings) -> bool:
    th = settings.thresholds
    if th.slow_status:
        return s.status in th.slow_status
    limit = th.response_time_s_by_app.get(s.application, th.response_time_s)
    return s.response_time is not None and s.response_time > limit


def slow_rule(settings: Settings) -> str:
    th = settings.thresholds
    if th.slow_status:
        return f"Aternity marked it {' or '.join(th.slow_status)}"
    return (f"it took over {th.response_time_s:g} s"
            + (" (other limits for some apps)" if th.response_time_s_by_app else "")
            + "; set thresholds.slow_status to use Aternity's own status")


def _problem(t, kind, source, obj, what, value=None, limit=None, app=None, until=None) -> dict:
    return {"t": t, "until": until or t, "kind": kind, "source": source, "object": obj, "what": what,
            "value": value, "limit": limit, "app": app}


def _pc_problems(h: DeviceHealthSample, settings: Settings) -> list[dict]:
    """CPU, memory, disk queue, free disk space and round trip, over (or under) their limits."""
    th = settings.thresholds
    out = []
    if h.free_disk is not None and h.free_disk < th.free_disk_percent:
        out.append(_problem(h.timestamp, "pc", "Aternity", h.device, "Free disk space %",
                            round(h.free_disk, 1), th.free_disk_percent))
    for kind, value, limit, what in (("pc", h.cpu, th.device_cpu, "CPU %"),
                                     ("pc", h.memory, th.device_memory, "Memory %"),
                                     ("pc", h.disk_queue, th.disk_queue, "Disk queue"),
                                     # measured from the PC, but it is the network's round trip
                                     ("network", h.network_latency, th.client_network_latency_ms,
                                      "Network round trip ms")):
        if value is not None and value > limit:
            out.append(_problem(h.timestamp, kind, "Aternity", h.device, what, round(value, 2), limit))
    return out


START_UP = "Aternity start-up"
SECONDS = {"s": 1, "ms": 0.001, "min": 60}


def _startup_problems(logons: dict, unit: str) -> dict:
    """Each start-up (boot or logon) of the PC, from its start to its end: what
    the user runs during it or just after it is slowed by it. Keyed by id()."""
    out = {}
    for st in logons.get("starts", []):
        t = parse_time(st["time"])
        if t is None or not st["values"]:
            continue
        u = logons.get("unit") or ""
        longest = max(v["duration"] for v in st["values"].values())
        parts = ", ".join(f"{typ} {v['duration']}{' ' + u if u else ''}"
                          + (f" (usual {v['usual']}{' ' + u if u else ''})" if v["usual"] is not None else "")
                          for typ, v in st["values"].items())
        p = _problem(t, "pc", START_UP, st["device"], f"was starting up: {parts}", longest,
                     until=t + timedelta(seconds=longest * SECONDS.get(unit, 1)))
        p["_start"] = st
        out[id(p)] = p
    return out.values()


def _startup_why(g: dict, started_up, logons: dict) -> dict:
    """Why the start-up was slow: what logons.py found (the slowest part, what
    changed before it, and how many PCs had that part slow)."""
    st = next(p["_start"] for p in started_up if p["object"] == g["object"] and p["what"] == g["what"])
    text = [w for w in st["why"] if not w.startswith("Same cause as main cause")]
    todo = []
    if not text and st["components"]:
        text.append("What took the time: " + ", ".join(
            f"{c['component']} {c['duration']}" for c in st["components"][:3]) + ".")
    slow = next((c for c in st["components"] if c["degradation"]), None)
    if slow:
        todo.append(f"On {g['object']}: look at {slow['component']}"
                    + (f" ({slow['type']})" if slow["type"] != slow["component"] else "") + ", the slowest part of "
                    f"its start-up; the Slow start-ups tab shows it on every PC.")
    if st["cause"] is not None:
        c = logons["causes"][st["cause"]]
        where = ", ".join(f"{k} {n}" for k, n in c["by_location"].items())
        text.append(f"{c['component']} was longer than normal on {c['pcs']} PC(s) in this window"
                    + (f" ({where})" if where else "") + ": the Slow start-ups tab lists them.")
    if not text:
        text.append("Aternity recorded no breakdown for this start-up.")
    return {"failure": None, "others": None, "changes": [], "processes": [], "text": text, "todo": todo}


def _event_problem(e: HealthEvent) -> dict:
    """A device health event is a problem by itself: it happened. One with an
    application counts only for that application's slow actions."""
    what = " / ".join(x for x in (e.category, e.name) if x) or "Health event"
    where = e.component or e.application
    return _problem(e.timestamp, "pc", "Aternity event", e.device, f"{what} ({where})" if where else what,
                    e.count if e.count is not None else 1, app=e.application)


def _matches(p: dict, s: UserSample, window: timedelta) -> bool:
    return (p["app"] is None or p["app"] == s.application) and \
        p["t"] - window <= s.timestamp <= p["until"] + window


def investigate(req: InvestigationRequest, aternity: AternitySource, netim: NetimSource | None,
                settings: Settings) -> dict:
    req.validate()
    minutes = settings.bucket_minutes
    window = timedelta(minutes=minutes)
    warnings: list[str] = []
    started = time.monotonic()
    who = ", ".join(f"{k} {v}" for k, v in (("user", req.username), ("device", req.device),
                                            ("app", req.application)) if v)
    log.info("Investigating %s, %s to %s UTC", who, req.start.strftime("%Y-%m-%d %H:%M"),
             req.end.strftime("%Y-%m-%d %H:%M"))

    # 1. user experience
    samples = sorted(aternity.user_activity(req.start, req.end, username=req.username,
                                            device=req.device, application=req.application),
                     key=lambda s: s.timestamp)
    if req.device:
        device_names = [req.device]
    else:
        device_names = [d for d, _ in Counter(s.device for s in samples if s.device).most_common(3)]
    slow = [s for s in samples if is_slow(s, settings)]
    log.info("Aternity: %d actions, %d slow, device(s): %s", len(samples), len(slow),
             ", ".join(device_names) or "none")
    if settings.thresholds.slow_status and samples and not any(s.status for s in samples):
        warnings.append("Aternity gave no status for these activities, so none counts as slow. "
                        "Check aternity.fields.activities.status with uxcorrelate verify.")

    # 2. problems on the PC
    endpoints = [aternity.endpoint_info(d) for d in device_names]
    problems: list[dict] = []
    for d in device_names:
        health = aternity.device_health(d, req.start, req.end)
        if hasattr(aternity, "disk_space"):
            health += aternity.disk_space(d, req.start, req.end)
        for h in health:
            problems += _pc_problems(h, settings)
    events: list[HealthEvent] = []
    uxi: list[float] = []
    for d in device_names:
        if hasattr(aternity, "health_events"):
            events += aternity.health_events(d, req.start, req.end)
        if hasattr(aternity, "app_usage"):
            for u in aternity.app_usage(d, req.start, req.end, req.application):
                for what, value in (("crashed", u.crashes), ("not responding (s)", u.hang_time)):
                    if value:
                        problems.append(_problem(u.timestamp, "pc", "Aternity", u.device,
                                                 f"{u.application} {what}", round(value, 2), app=u.application))
                if u.uxi is not None:
                    uxi.append(u.uxi)
    problems += [_event_problem(e) for e in events]
    log.info("Aternity: %d device health events", len(events))
    warnings.extend(getattr(aternity, "warnings", []))

    # 3/4. infrastructure
    mapped: list[MappedObject] = []
    if netim is not None:
        mapped = map_netim_objects(netim, endpoints, req.application, settings)
        log.info("NetIM: %d objects on the user's path; reading %d metrics",
                 len(mapped), len(settings.netim.metrics))
        obj_index = {(m.object_type, m.object_id): m for m in mapped}
        for metric in settings.netim.metrics:
            ids = [m.object_id for m in mapped if m.object_type == metric.object_type]
            for ms in netim.metrics(metric, ids, req.start, req.end):
                obj = obj_index.get((ms.object_type, ms.object_id))
                if obj is not None and metric.is_anomalous(ms.value):
                    problems.append(_problem(ms.timestamp, ROLE_DOMAIN[obj.role], "NetIM", obj.name,
                                             metric.label, round(ms.value, 2), metric.threshold))
        ids = {m.device_id for m in mapped} | {m.object_id for m in mapped}
        roles = {m.device_id: m.role for m in mapped if m.object_type == "DEVICE"}
        for al in netim.alerts(req.start, req.end):
            if al.object_id in ids:
                problems.append(_problem(al.start, ROLE_DOMAIN[roles.get(al.object_id, "always")], "NetIM alert",
                                         al.object_name, f"{al.severity}: {al.message}", until=al.end or req.end))
        warnings.extend(netim.warnings)
        netim.warnings.clear()
    else:
        warnings.append("NetIM is not configured: the network was not checked.")
    if not samples:
        warnings.append("Aternity returned no activity for this user/device/application in the window.")

    warnings.extend(w for w in getattr(aternity, "warnings", []) if w not in warnings)
    logons = logon_report(aternity, req.start, req.end, username=req.username, device=req.device,
                          unit=settings.boot_unit)
    warnings.extend(w for w in logons.pop("warnings", []) if w not in warnings)
    started_up = _startup_problems(logons, settings.boot_unit)

    # 5. which problems happened at the slow actions
    for p in problems + list(started_up):
        p["slow"] = {i for i, s in enumerate(slow) if _matches(p, s, window)}
    # a start-up counts only when the user was slow during it or just after it
    problems += [p for p in started_up if p["slow"]]
    groups = _group(problems, len(slow))
    for g in groups:
        if g["source"] == START_UP:
            g["why"] = _startup_why(g, started_up, logons)
    explain(aternity, groups, events, window, req.start, req.end)
    for g in groups:
        g.pop("_times")
    by_kind = {k: len({i for p in problems if p["kind"] == k for i in p["slow"]}) for k in KINDS}

    user_location = next((e.location for e in endpoints if e.location), None) or \
        next((s.location for s in samples if s.location), None)
    user_ip = next((e.ip_address for e in endpoints if e.ip_address), None)
    bucket = lambda t: _bucket(t, minutes)  # noqa: E731
    apps = [req.application] if req.application else \
        [a for a, _ in Counter(s.application for s in slow if s.application).most_common(3)]
    impact = who_else(aternity, netim, apps, {bucket(s.timestamp) for s in slow}, minutes, bucket,
                      lambda s: is_slow(s, settings), req.username or req.device,
                      None if req.username else req.device, user_ip, user_location)
    warnings.extend(w for w in getattr(aternity, "warnings", []) if w not in warnings)
    answer = build_answer(groups, samples, slow, req.username or req.device, impact, netim is not None,
                          mapped, user_ip, logons, minutes, _time_split(slow))
    log.info("Done in %.0f s: %s", time.monotonic() - started, answer["title"])
    return {
        "request": {
            "username": req.username, "device": req.device, "application": req.application,
            "start": _iso(req.start), "end": _iso(req.end), "bucket_minutes": minutes,
        },
        "summary": {
            "actions": len(samples), "slow": len(slow), "slow_rule": slow_rule(settings),
            "explained_by": by_kind,
            # Aternity's own experience score for the window, as Aternity reports it
            "uxi_avg": round(mean(uxi), 1) if uxi else None,
            "uxi_min": round(min(uxi), 1) if uxi else None,
        },
        "answer": answer,
        "problems": groups,
        "impact": impact,
        "timeline": {
            "actions": [[_iso(s.timestamp), s.response_time, is_slow(s, settings), s.application]
                        for s in samples],
            "marks": [[_iso(p["t"]), p["kind"], f"{p['object']}: {p['what']}"
                       + (f" {p['value']}" if p["value"] is not None else ""), bool(p["slow"])]
                      for p in sorted(problems, key=lambda p: p["t"])][:5000],
        },
        "endpoints": [asdict(e) for e in endpoints],
        "mapped_objects": [asdict(m) for m in mapped],
        "warnings": warnings,
    }


def _time_split(slow: list[UserSample]) -> dict | None:
    """Aternity's own split of the slow actions' time: share on the PC, network, server."""
    acc = {k: [] for k in KINDS}
    for s in slow:
        parts = {"pc": s.client_time, "network": s.network_time, "server": s.backend_time}
        total = sum(v for v in parts.values() if v is not None)
        if any(v is None for v in parts.values()) or total <= 0:
            continue
        for k in KINDS:
            acc[k].append(parts[k] / total)
    return {k: round(mean(v), 3) for k, v in acc.items()} if acc["pc"] else None


def _group(problems: list[dict], slow_total: int) -> list[dict]:
    """One row per object + measurement: how many slow actions it was at, and when."""
    grouped: dict[tuple, dict] = {}
    for p in sorted(problems, key=lambda p: p["t"]):
        key = (p["kind"], p["source"], p["object"], p["what"])
        g = grouped.setdefault(key, {
            "kind": p["kind"], "source": p["source"], "object": p["object"], "what": p["what"],
            "app": p["app"], "limit": p["limit"], "worst": None, "slow": set(), "times": 0,
            "first": p["t"], "last": p["until"], "_times": [], "why": None,
        })
        g["slow"] |= p["slow"]
        if p["slow"]:  # when it was at a slow action; cause.py looks there
            g["_times"].append((p["t"], p["until"]))
        g["times"] += 1
        g["last"] = max(g["last"], p["until"])
        if p["value"] is not None:
            pick = min if p["what"].startswith("Free disk") else max  # less free space is worse
            g["worst"] = p["value"] if g["worst"] is None else pick(g["worst"], p["value"])
    out = []
    for g in grouped.values():
        g["slow_actions"] = len(g.pop("slow"))
        g["of_slow"] = slow_total
        g["first"], g["last"] = _iso(g["first"]), _iso(g["last"])
        g["text"] = problem_text(g)
        out.append(g)
    # at the most slow actions first; a named NetIM object says more than the PC's own view
    out.sort(key=lambda g: (-g["slow_actions"], not g["source"].startswith("NetIM"), -g["times"]))
    return out
