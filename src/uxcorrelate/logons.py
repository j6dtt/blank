"""Slow boots and logons from Aternity (WINDOWS_MACHINE_BOOTS + MACHINE_BOOT_COMPONENTS).

No "slow" threshold is assumed: each boot/logon is compared with the median of
its own type (as Aternity names it) in the same search, and, for one user or
device, with that device's own last 30 days. Durations are in Aternity's units;
`uxcorrelate verify` prints their range and the measurement types it sees.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median, quantiles

from .models import BootComponent, BootSample

log = logging.getLogger("uxcorrelate")
BASELINE_DAYS = 30
COMPONENTS_FOR = 10  # look up what slowed the N slowest
MATCH_WINDOW = timedelta(hours=1)  # a component row belongs to the nearest boot within this


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _stats(values: list[float]) -> dict:
    p90 = quantiles(values, n=10)[-1] if len(values) >= 2 else values[0]
    return {"count": len(values), "median": round(median(values), 2),
            "p90": round(p90, 2), "max": round(max(values), 2)}


def _by_type(samples: list[BootSample]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for b in samples:
        if b.duration is not None:
            out[b.type or "(no type)"].append(b.duration)
    return out


def _components_of(b: BootSample, comps: list[BootComponent], top: int = 5) -> list[dict]:
    mine = [c for c in comps if c.device == b.device and c.type == b.type
            and abs(c.timestamp - b.timestamp) <= MATCH_WINDOW]
    if not mine:
        return []
    nearest = min(mine, key=lambda c: abs(c.timestamp - b.timestamp)).timestamp
    mine = [c for c in mine if c.timestamp == nearest]
    mine.sort(key=lambda c: (-(c.degradation or 0), -(c.duration or 0)))
    return [{"component": c.component, "start_type": c.start_type,
             "duration": c.duration, "degradation": c.degradation} for c in mine[:top]]


def logon_report(aternity, start: datetime, end: datetime, username: str | None = None,
                 device: str | None = None, location: str | None = None, top: int = 100) -> dict:
    """The slowest boots/logons in the window, per-type and per-location medians,
    and what slowed the slowest ones."""
    if not hasattr(aternity, "boots"):
        return {"total": 0, "types": [], "slowest": [], "by_location": []}
    scope = ", ".join(f"{k} {v}" for k, v in (("user", username), ("device", device),
                                              ("location", location)) if v) or "everyone"
    log.info("Aternity: reading boots and logons for %s", scope)
    samples = [b for b in aternity.boots(start, end, username=username, device=device, location=location)
               if b.duration is not None]
    types = _by_type(samples)
    medians = {t: median(v) for t, v in types.items()}

    usual: dict[tuple[str, str], float] = {}
    if username or device:  # one person: compare with their own last 30 days
        history = aternity.boots(start - timedelta(days=BASELINE_DAYS), start, username=username, device=device)
        hist: dict[tuple[str, str], list[float]] = defaultdict(list)
        for b in history:
            if b.duration is not None:
                hist[(b.device, b.type or "(no type)")].append(b.duration)
        usual = {k: median(v) for k, v in hist.items()}

    slowest = sorted(samples, key=lambda b: -b.duration)[:top]
    comps = []
    if slowest and hasattr(aternity, "boot_components"):
        devices = list(dict.fromkeys(b.device for b in slowest[:COMPONENTS_FOR]))
        log.info("Aternity: reading what slowed the %d slowest", min(len(slowest), COMPONENTS_FOR))
        comps = aternity.boot_components(devices, start - MATCH_WINDOW, end + MATCH_WINDOW)

    items = []
    for i, b in enumerate(slowest):
        t = b.type or "(no type)"
        u = usual.get((b.device, t))
        items.append({
            "time": _iso(b.started or b.timestamp), "username": b.username, "device": b.device,
            "location": b.location, "type": t, "duration": round(b.duration, 2),
            "x_median": round(b.duration / medians[t], 1) if medians.get(t) else None,
            "usual": None if u is None else round(u, 2),
            "x_usual": round(b.duration / u, 1) if u else None,
            "components": _components_of(b, comps) if i < COMPONENTS_FOR else [],
        })

    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for b in samples:
        groups[(b.type or "(no type)", b.location or "(unknown)")].append(b.duration)
    by_location = sorted(({"type": t, "location": loc, **_stats(v)} for (t, loc), v in groups.items()),
                         key=lambda g: (g["type"], -g["median"]))
    log.info("Aternity: %d boots/logons found", len(samples))
    return {
        "start": _iso(start), "end": _iso(end),
        "filters": {"username": username, "device": device, "location": location},
        "total": len(samples),
        "types": [{"type": t, **_stats(v)} for t, v in sorted(types.items())],
        "slowest": items,
        "by_location": by_location,
        "baseline_days": BASELINE_DAYS if (username or device) else None,
        "warnings": list(getattr(aternity, "warnings", [])),
    }
