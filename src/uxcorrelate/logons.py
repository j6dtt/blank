"""Slow start-ups (boots and logons) from Aternity (WINDOWS_MACHINE_BOOTS +
MACHINE_BOOT_COMPONENTS).

Aternity stores one row per measurement (its MEASUREMENT_TYPE names them, e.g.
a boot, a logon, a total). Rows of the same device within an hour of each
other, with no type repeated, are one start-up: one row on the page with a
column per type. No "slow" threshold is assumed: each type shows its typical
value (median) and 1-in-10 slowest (90th percentile), and, for one user or
device, each value is shown next to that device's own last 30 days.

Why (the slowest start-ups): the part Aternity measured as most "longer than
normal" (its degradation time), what was installed or updated on the PC in
the days before, and who else is affected: the other PCs where that same
part was longer than normal in the window, by location. Those are the main
causes at the top of the page.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median, quantiles

from .cause import changes_before
from .models import BootComponent, BootSample, parse_time

log = logging.getLogger("uxcorrelate")
BASELINE_DAYS = 30
COMPONENTS_FOR = 20  # look up what took the time for the N slowest start-ups
WHY_FOR = 10  # look for the cause of the N slowest
MAIN_CAUSES = 3
SAME_START = timedelta(hours=1)  # rows of one device this close are one start-up


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _stats(values: list[float]) -> dict:
    p90 = quantiles(values, n=10)[-1] if len(values) >= 2 else values[0]
    return {"count": len(values), "typical": round(median(values), 1),
            "p90": round(p90, 1), "max": round(max(values), 1)}


def _when(b: BootSample) -> datetime:
    return b.started or b.timestamp


def _type(b) -> str:
    return b.type or "(no type)"


def group_starts(samples: list[BootSample]) -> list[list[BootSample]]:
    """Rows of one device within SAME_START of the first, no type twice, as one start-up."""
    starts: list[list[BootSample]] = []
    for b in sorted(samples, key=lambda b: (b.device, _when(b))):
        cur = starts[-1] if starts else None
        if (cur and cur[0].device == b.device and _when(b) - _when(cur[0]) <= SAME_START
                and _type(b) not in {_type(x) for x in cur}):
            cur.append(b)
        else:
            starts.append([b])
    return starts


def _part_name(c: BootComponent) -> str:
    return c.component or c.type or "(unnamed part)"


def parts_of(starts: list[list[BootSample]], comps: list[BootComponent]) -> dict[int, list[BootComponent]]:
    """Which start-up each part belongs to: the latest start-up row of the same PC
    at or before the part's time, within SAME_START. Aternity gives a start-up's
    parts its own timestamp (phases, drivers), or a few minutes after it (services);
    their types are not the start-up's types, so types are not compared."""
    rows = sorted(((b.timestamp, i) for i, st in enumerate(starts) for b in st),
                  key=lambda x: x[0])
    by_dev: dict[str, list[tuple]] = defaultdict(list)
    for t, i in rows:
        by_dev[starts[i][0].device].append((t, i))
    out: dict[int, list[BootComponent]] = defaultdict(list)
    for c in comps:
        before = [(t, i) for t, i in by_dev.get(c.device, []) if t <= c.timestamp <= t + SAME_START]
        if before:
            out[max(before)[1]].append(c)
    return out


def _components_of(mine: list[BootComponent], top: int = 8) -> list[dict]:
    """A start-up's parts, most "longer than normal" first, then the longest."""
    mine = sorted(mine, key=lambda c: (-(c.degradation or 0), -(c.duration or 0)))
    return [{"type": _type(c), "component": _part_name(c), "duration": c.duration,
             "degradation": c.degradation} for c in mine[:top]]


def logon_report(aternity, start: datetime, end: datetime, username: str | None = None,
                 device: str | None = None, location: str | None = None, top: int = 100,
                 unit: str = "") -> dict:
    """Start-ups in the window, slowest first, with what took the time."""
    empty = {"start": _iso(start), "end": _iso(end), "unit": unit, "total": 0, "types": [], "starts": [],
             "by_location": [], "causes": [], "filters": {"username": username, "device": device, "location": location},
             "warnings": list(getattr(aternity, "warnings", []))}
    if not hasattr(aternity, "boots"):
        return empty
    scope = ", ".join(f"{k} {v}" for k, v in (("user", username), ("device", device),
                                              ("location", location)) if v) or "everyone"
    log.info("Aternity: reading boots and logons for %s", scope)
    samples = [b for b in aternity.boots(start, end, username=username, device=device, location=location)
               if b.duration is not None]
    if not samples:
        empty["warnings"] = list(getattr(aternity, "warnings", []))
        return empty

    per_type: dict[str, list[float]] = defaultdict(list)
    for b in samples:
        per_type[_type(b)].append(b.duration)
    # smallest typical first: the parts before the total
    types = sorted(per_type, key=lambda t: median(per_type[t]))
    sort_type = types[-1]

    usual: dict[tuple[str, str], float] = {}
    if username or device:  # one person: compare with their own last 30 days
        hist: dict[tuple[str, str], list[float]] = defaultdict(list)
        for b in aternity.boots(start - timedelta(days=BASELINE_DAYS), start, username=username, device=device):
            if b.duration is not None:
                hist[(b.device, _type(b))].append(b.duration)
        usual = {k: median(v) for k, v in hist.items()}

    def key(st):  # slowest by the type with the largest typical value, else by its largest value
        by = {_type(b): b.duration for b in st}
        return (-(by.get(sort_type) or 0), -max(by.values()))

    starts = sorted(group_starts(samples), key=key)
    shown = starts[:top]
    comps: list[BootComponent] = []
    if shown and hasattr(aternity, "boot_components"):
        devices = list(dict.fromkeys(st[0].device for st in shown[:COMPONENTS_FOR]))
        log.info("Aternity: reading what slowed the %d slowest start-ups", min(len(shown), COMPONENTS_FOR))
        comps = aternity.boot_components(devices, start - SAME_START, end + SAME_START)

    parts = parts_of(starts, comps)
    items = []
    for i, st in enumerate(shown):
        first = min(st, key=_when)
        values = {}
        for b in st:
            u = usual.get((b.device, _type(b)))
            values[_type(b)] = {"duration": round(b.duration, 1), "usual": None if u is None else round(u, 1)}
        items.append({
            "time": _iso(_when(first)), "username": next((b.username for b in st if b.username), None),
            "device": first.device, "location": next((b.location for b in st if b.location), None),
            "values": values, "components": _components_of(parts.get(i, [])) if i < COMPONENTS_FOR else [],
            "why": [], "cause": None,
        })

    causes = _causes(aternity, items[:WHY_FOR], start, end, unit)

    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for b in samples:
        groups[b.location or "(no location)"][_type(b)].append(b.duration)
    by_location = sorted(({"location": loc, "count": max(len(v) for v in tv.values()),
                           "typical": {t: round(median(v), 1) for t, v in tv.items()}}
                          for loc, tv in groups.items()),
                         key=lambda g: -(g["typical"].get(sort_type) or 0))
    log.info("Aternity: %d start-ups (%d measurements) found", len(starts), len(samples))
    return {
        **empty,
        "total": len(starts), "measurements": len(samples),
        "types": [{"type": t, **_stats(per_type[t])} for t in types],
        "sort_type": sort_type, "starts": items, "by_location": by_location[:20], "causes": causes,
        "baseline_days": BASELINE_DAYS if (username or device) else None,
        "warnings": list(getattr(aternity, "warnings", [])),
    }


def _causes(aternity, starts: list[dict], start: datetime, end: datetime, unit: str) -> list[dict]:
    """Why each of the slowest start-ups was slow, and the parts behind several of them."""
    u = f" {unit}" if unit else ""
    keys: Counter = Counter()
    for st in starts:
        slow = next((c for c in st["components"] if c["degradation"]), None)
        if not st["components"]:
            st["why"].append("Aternity recorded no breakdown for this start-up.")
        elif slow is None:
            c = max(st["components"], key=lambda c: c["duration"] or 0)
            st["why"].append(f"Aternity measured no part as longer than normal; the longest was "
                             f"{c['component']} ({c['duration']}{u}).")
        else:
            st["why"].append(f"Most of the extra time: {slow['component']} took {slow['duration']}{u}, "
                             f"{slow['degradation']}{u} longer than normal"
                             + (f" ({slow['type']})." if slow["type"] != slow["component"] else "."))
            keys[(slow["type"], slow["component"])] += 1
            st["cause_key"] = (slow["type"], slow["component"])
        ch = changes_before(aternity, st["device"], parse_time(st["time"]),
                            [slow["component"]] if slow else [], "slow part", "slow start-up")
        if ch["text"]:
            st["why"].append(ch["text"])

    causes = []
    for (typ, comp), n in keys.most_common(MAIN_CAUSES):
        if not hasattr(aternity, "boot_component_everywhere"):
            break
        log.info("Aternity: who else had %s slower than normal", comp)
        named = None if comp == typ else comp  # a part without a component name goes by its type
        rows = [c for c in aternity.boot_component_everywhere(named, typ, start, end) if c.degradation]
        pcs: dict[str, dict] = {}
        for c in sorted(rows, key=lambda c: c.timestamp):
            p = pcs.setdefault(c.device, {"device": c.device, "username": None, "location": None, "times": 0,
                                          "worst": 0.0})
            p["username"] = c.username or p["username"]
            p["location"] = c.location or p["location"]
            p["times"] += 1
            p["worst"] = round(max(p["worst"], c.degradation), 1)
            p["last"] = _iso(c.timestamp)
        where = Counter(p["location"] or "(no location)" for p in pcs.values())
        causes.append({"type": typ, "component": comp, "slowest_starts": n, "times": len(rows), "pcs": len(pcs),
                       "by_location": dict(where.most_common(5)),
                       "list": sorted(pcs.values(), key=lambda p: (-p["times"], -p["worst"]))[:100]})
    for st in starts:
        key = st.pop("cause_key", None)
        st["cause"] = next((i for i, c in enumerate(causes) if (c["type"], c["component"]) == key), None)
        if st["cause"] is not None:
            c = causes[st["cause"]]
            st["why"].append(f"Same cause as main cause {st['cause'] + 1} above ({c['component']}): "
                             "see there who else is affected.")
    return causes
