"""Why: for each problem seen at the user's slow actions, what caused it.

Only Aternity's own records are used:
- where an application failed: the component and sub-component (e.g. the
  program and the module inside it), their versions and Aternity's error text,
  from its crash/hang events (DEVICE_HEALTH_RAW);
- whether other PCs had the same failure, and on which versions;
- what was installed, updated or removed on the PC in the days before
  (INSTALLED_SOFTWARE_CHANGE_LOG);
- for CPU, memory or disk problems: the busiest processes at those times
  (RAW_DATA_TOP_PROCESSES).
Nothing is guessed: each finding is a sentence about a record, and when a
source has nothing, the sentence says so.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from .config import SOFTWARE_REMOVED
from .models import parse_time

log = logging.getLogger("uxcorrelate")
CHANGES_BEFORE = timedelta(days=14)  # how far back "what changed" looks
EXPLAIN = 3  # the problems at the most slow actions get explained
EVENT = "Aternity event"  # a device health event (correlate._event_problem)
METRIC = {"CPU %": ("CPU", "cpu", "%"), "Memory %": ("memory", "memory", "%"), "Disk queue": ("disk I/O", "io", "")}


def _iso(dt) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _day(dt) -> str:
    return dt.strftime("%b %d").replace(" 0", " ")


def _near(t, times, window) -> bool:
    return any(a - window <= t <= b + window for a, b in times)


def _where(e) -> tuple:
    return (e.component, e.component_version, e.sub_component, e.sub_component_version)


def _module(where: tuple) -> str:
    comp, cver, sub, sver = where
    if sub:
        return f"{sub}{f' {sver}' if sver else ''}" + (f" (inside {comp}{f' {cver}' if cver else ''})" if comp else "")
    return f"{comp}{f' {cver}' if cver else ''}" if comp else "an unnamed component"


def explain(aternity, groups: list[dict], events: list, window: timedelta, start, end) -> None:
    """Adds g["why"] to the problems at the most slow actions."""
    done: dict[tuple, dict] = {}
    for g in [g for g in groups if g["slow_actions"]][:EXPLAIN]:
        if g["why"] or g["source"].startswith("NetIM") or g["what"].startswith("Network round trip"):
            continue  # the network side is explained by NetIM and "who else"
        if g["app"] and (g["object"], g["app"]) in done:  # a hang and its health event: one cause
            g["why"] = {**done[(g["object"], g["app"])], "same_as_above": True}
            continue
        log.info("Looking for the cause: %s", g["text"])
        why = {"failure": None, "others": None, "changes": [], "processes": [], "text": [], "todo": []}
        times = g["_times"]
        if g["app"] or g["source"] == EVENT:
            _failure(aternity, g, events, times, window, start, end, why)
        else:
            _processes(aternity, g, times, window, why)
        _changes(aternity, g, min(a for a, _ in times), why)
        g["why"] = why
        if g["app"]:
            done[(g["object"], g["app"])] = why


def _event_what(e) -> str:
    """The problem text of a health event, as correlate._event_problem builds it."""
    from .correlate import _event_problem
    return _event_problem(e)["what"]


def _failure(aternity, g, events, times, window, start, end, why) -> None:
    """Where it failed, from the health events behind the problem: for an app, its
    crashes and hangs; for any other event (a background process, low memory, a
    blue screen, a failing disk), that event itself."""
    dev, app = g["object"], g["app"]
    if g["source"] == EVENT:
        mine = [e for e in events if e.device == dev and e.application == app and _event_what(e) == g["what"]]
    else:
        mine = [e for e in events if e.device == dev and e.application == app]
    near = [e for e in mine if _near(e.timestamp, times, window)]
    if not mine:
        why["text"].append(f"Aternity recorded no crash or hang details for {app} on {dev}, only the count.")
        return
    use = near or mine
    where = Counter(_where(e) for e in use).most_common(1)[0][0]
    kind = Counter((e.category, e.name) for e in use if _where(e) == where).most_common(1)[0][0]
    these = [e for e in use if _where(e) == where and (e.category, e.name) == kind]
    last = max(these, key=lambda e: e.timestamp)
    error = Counter(e.error for e in these if e.error).most_common(1)
    error = error[0][0] if error else None
    why["failure"] = {"category": kind[0], "name": kind[1], "component": where[0], "component_version": where[1], "sub_component": where[2],
                      "sub_component_version": where[3], "exception": last.exception, "error": error,
                      "details": (last.details or "")[:300] or None, "events": len(use), "at_slow_actions": bool(near)}
    err = last.exception or error
    what = " / ".join(x for x in kind if x) or "failure"
    subject = f"{app} failed" if app else f"Aternity recorded {what} on {dev}"
    place = f" in {_module(where)}" if where[0] or where[2] else ""
    why["text"].append(f"{subject}{place}" + (f": {err}" if err else "")
                       + ("" if near else " (from its events in this window, not at the slow actions)") + ".")
    if last.details:
        why["text"].append(f"Aternity's details: {' '.join(last.details.split())[:300]}")
    module = where[2] or where[0]
    if module:
        why["todo"].append(f"On {dev}: look at {_module(where)}, where {app or what} failed "
                           "(repair or update it; if it is an add-in, try without it).")

    # only this PC, or others too? Found by the app, else by the process that failed.
    if app and hasattr(aternity, "app_health_events"):
        found = aternity.app_health_events(app, start, end)
    elif not app and where[0] and hasattr(aternity, "component_health_events"):
        found = aternity.component_health_events(where[0], start, end)
    else:
        return
    # the same failure: the same kind (Aternity's category and name, e.g. a hang), the same
    # process, the same module when Aternity knows it, the same error when both have one
    same = [e for e in found
            if e.device != dev and e.component == where[0] and (e.category, e.name) == kind
            and (where[2] is None or e.sub_component == where[2])
            and (error is None or e.error is None or e.error == error)]
    version = lambda e: (e.sub_component_version if where[2] else e.component_version) or "unknown version"  # noqa: E731
    pcs: dict[str, dict] = {}
    for e in sorted(same, key=lambda e: e.timestamp):
        p = pcs.setdefault(e.device, {"device": e.device, "username": None, "version": version(e),
                                      "times": 0, "first": _iso(e.timestamp)})
        p["username"] = e.username or p["username"]
        p["version"] = version(e)
        p["times"] += int(e.count or 1)
        p["last"] = _iso(e.timestamp)
    versions = Counter(p["version"] for p in pcs.values())
    # shown under "Who else is affected?" (one place), not in the why lines
    named = f"{app} {what}" if app else what
    why["others"] = {"app": app, "what": f"{named}{place}" + (f", error {error}" if error else ""), "pcs": len(pcs),
                     "this_version": (where[3] if where[2] else where[1]) or "unknown version",
                     "versions": dict(versions.most_common(5)),
                     "list": sorted(pcs.values(), key=lambda p: (-p["times"], p["device"]))[:100]}
    if not pcs:
        return
    why["todo"].append(f"{len(pcs)} other PCs fail the same way: treat it as a {module or app} problem "
                       f"(version, update, vendor), not this PC alone.")


def _processes(aternity, g, times, window, why) -> None:
    if g["what"] not in METRIC or not hasattr(aternity, "top_processes"):
        return
    label, key, unit = METRIC[g["what"]]
    dev = g["object"]
    rows = aternity.top_processes(dev, min(a for a, _ in times) - window, max(b for _, b in times) + window)
    best: dict[str, float] = {}
    for p in rows:
        if not _near(p.timestamp, times, window):
            continue
        v = ((p.io_read or 0) + (p.io_write or 0)) if key == "io" else getattr(p, key)
        if v is not None:
            best[p.process] = max(best.get(p.process, v), v)
    top = sorted(best.items(), key=lambda kv: -kv[1])[:3]
    why["processes"] = [{"process": n, "value": round(v, 1)} for n, v in top]
    if not top:
        why["text"].append(f"Aternity has no process data for {dev} at those times.")
        return
    why["text"].append(f"Busiest processes then: " + ", ".join(f"{n} ({label} {v:.1f}{unit})" for n, v in top) + ".")
    why["todo"].append(f"On {dev}: look at {top[0][0]}, the busiest process at those times.")


def changes_before(aternity, device: str, when, names, part: str, event: str) -> dict:
    """What was installed, updated or removed on `device` in the days before `when`.
    A change whose name matches one of `names` (an app, program or module) is marked."""
    out = {"changes": [], "text": None, "todo": None}
    if not hasattr(aternity, "software_changes"):
        return out
    rows = _updates(aternity.software_changes(device, when - CHANGES_BEFORE, when))
    names = {n.lower().rsplit(".", 1)[0] for n in names if n}
    for c in rows[:8]:
        related = bool(c["name"]) and any(n in c["name"].lower() or c["name"].lower() in n for n in names)
        out["changes"].append({**c, "time": _iso(c["time"]), "related": related})
    if not rows:
        out["text"] = (f"Nothing was installed, updated or removed on {device} in the "
                       f"{CHANGES_BEFORE.days} days before {_day(when)}.")
        return out
    shown = sorted(out["changes"], key=lambda c: not c["related"])[:3]
    out["text"] = (f"Changed on {device} in the {CHANGES_BEFORE.days} days before {_day(when)}: " + "; ".join(
        f"{_day(parse_time(c['time']))} {c['name']} {c['change'] or 'changed'}"
        + (f" {c['version_before']} → {c['version']}" if c["version_before"] and c["version"] else
           f" {c['version']}" if c["version"] else "")
        + (f" (same name as the {part})" if c["related"] else "") for c in shown)
        + (f" (+{len(rows) - 3} more)" if len(rows) > 3 else "") + ".")
    rel = next((c for c in out["changes"] if c["related"]), None)
    if rel:
        out["todo"] = f"Check the {rel['name']} change of {rel['time'][:10]} on {device}: it came before the {event}."
    return out


def _updates(rows) -> list[dict]:
    """The change log as changes, newest first: a removal and an install of the same
    software at the same second are one update (old version -> new version)."""
    out, used = [], set()
    for c in sorted(rows, key=lambda c: c.timestamp, reverse=True):
        if id(c) in used:
            continue
        removed = c.change in SOFTWARE_REMOVED
        pair = next((o for o in rows if id(o) not in used and o is not c and o.name == c.name
                     and o.timestamp == c.timestamp and (o.change in SOFTWARE_REMOVED) != removed), None)
        if pair:
            old, new = (c, pair) if removed else (pair, c)
            used |= {id(c), id(pair)}
            out.append({"time": c.timestamp, "name": c.name, "change": "updated",
                        "version": new.version, "version_before": old.version})
        else:
            used.add(id(c))
            out.append({"time": c.timestamp, "name": c.name, "change": c.change,
                        "version": c.version, "version_before": c.version_before})
    return out


def _changes(aternity, g, first, why) -> None:
    f = why["failure"] or {}
    ch = changes_before(aternity, g["object"], first, (g["app"], f.get("component"), f.get("sub_component")),
                        "failing part", "first failure")
    why["changes"] = ch["changes"]
    if ch["text"]:
        why["text"].append(ch["text"])
    if ch["todo"]:
        why["todo"].append(ch["todo"])
