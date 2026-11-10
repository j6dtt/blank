"""What we know about each issue: where it failed, who else, what changed.

Only Aternity's own records are used:
- where an application failed: the component and sub-component (e.g. the
  program and the module inside it), their versions and Aternity's error text,
  from its crash/hang events (DEVICE_HEALTH_RAW);
- whether other PCs had the same failure, and on which versions;
- what was installed, updated or removed on the PC in the days before
  (INSTALLED_SOFTWARE_CHANGE_LOG);
- for CPU, memory or disk problems: the busiest processes at those times
  (top_processes: on the live tenant APPLICATION_RESOURCES_HOURLY, rows of up
  to an hour, CPU and memory only).
Nothing is guessed: each finding is a sentence about a record, and when a
source has nothing, the sentence says so. A change on the PC is a "possible
cause" of an issue only when it matches the failing part (same name or same
version), came before it, and other PCs do not fail the same way on another
version (then the failure is not new with this change); every other change is
listed once per PC. On physical PCs the log's time holds; on virtual desktops
it can run late (live: 1 in 4 failures ran a version before its log time, most
by under a day), so there "logged after the failure" does not clear a change.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import timedelta

from .config import (DISK_PARTS, NO_PROGRAM, PROCESS_ROW_SPAN_MIN, SOFTWARE_REMOVED, SOFTWARE_UPDATE_GAP_MIN,
                     WINDOWS_MODULES)
from .models import day as _local_day
from .models import clock, local_date, parse_time, part_label, plain, plural

log = logging.getLogger("uxcorrelate")
CHANGES_BEFORE = timedelta(days=14)  # how far back "what changed" looks
MAX_EXPLAIN = 25  # problems explained per investigation (a few Aternity calls each)
MAX_HISTORY = 5  # failures whose start is looked for on every PC (a wide read each)
TREND_RATIO = 1.25  # one week against the week before: this much more (or less) is a trend
TREND_MIN_PCS = 5  # PCs per working day: below this a trend is noise
UNKNOWN_VERSION = "unknown version"
EVENT = "Aternity event"  # a device health event (correlate._event_problem)
# what is told first: what the user felt, then the rest; limits passed last
# a failing disk first: it can slow or stop everything on its PC, and it has to be replaced
ISSUE_ORDER = {"disk": -1, "hang": 0, "crash": 0, "system": 1, "startup": 1, "background": 2, "limit": 3}
DISK_BACK = timedelta(days=14)  # a failing disk's earlier records on the PC are looked for this far back
FAULT_OFFSET = re.compile(r"Fault Offset:\s*\S+", re.IGNORECASE)  # means nothing to a reader
METRIC = {"CPU %": ("CPU", "cpu", "%"), "Memory %": ("memory", "memory", "%"), "Disk queue": ("disk I/O", "io", "")}


# only a file's ending is cut from a name: "Microsoft.Notes" is not "Microsoft" with an ending.
# Cutting at the last dot made every Microsoft update a "possible cause" of a Microsoft.Notes
# crash (live, Nov 2026)
FILE_ENDING = re.compile(r"\.(?:exe|dll|sys|msi|com|ocx|cpl|scr)$", re.IGNORECASE)


def name_words(name: str | None) -> tuple[str, ...]:
    """A program or change name as its words: "Microsoft.Notes.exe" -> ("microsoft", "notes")."""
    return tuple(re.findall(r"[a-z0-9+#]+", FILE_ENDING.sub("", plain(name).lower())))


def same_name(part: tuple[str, ...], change: str | None) -> bool:
    """Every word of one name is in the other: "Microsoft.Notes" is "Microsoft Sticky Notes",
    never "Microsoft Edge"; "OUTLOOK.EXE" is "Microsoft Outlook"."""
    a, b = set(part), set(name_words(change))
    return bool(a and b) and (a <= b or b <= a)


def _parts(names) -> list[tuple[str, ...]]:
    """The failing part's names as words, without what is no program or only reports a failure."""
    skip = set(NO_PROGRAM) | {FILE_ENDING.sub("", m) for m in WINDOWS_MODULES}
    return [name_words(n) for n in names if n and FILE_ENDING.sub("", plain(n).lower()) not in skip]


def is_disk(e) -> bool:
    """A health event whose failing part Aternity labels a disk or drive (DISK_PARTS)."""
    label = part_label(e.component)
    return bool(label) and label.lower() in {p.lower() for p in DISK_PARTS}


def _unlabelled(component: str | None) -> str:
    return re.sub(r"\s*\([^()]+\)\s*$", "", component or "")


def _iso(dt) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _day(dt) -> str:
    return _local_day(dt)  # in config.yaml's timezone, as the page's lists


def _near(t, times, window) -> bool:
    return any(a - window <= t <= b + window for a, b in times)


def _where(e) -> tuple:
    return (e.component, e.component_version, e.sub_component, e.sub_component_version)


def _module(where: tuple) -> str:
    comp, cver, sub, sver = (plain(x) for x in where)
    if sub:
        return f"{sub}{f' {sver}' if sver else ''}" + (f" (inside {comp}{f' {cver}' if cver else ''})" if comp else "")
    return f"{comp}{f' {cver}' if cver else ''}" if comp else "an unnamed component"


def explain(aternity, groups: list[dict], events: list, window: timedelta, start, end) -> dict:
    """Adds g["why"] to every problem on the PC: those at the slow actions first,
    then what the user felt, then the rest (a crash is an issue by itself). Up to
    MAX_EXPLAIN. Returns what changed on each of those PCs, newest first."""
    done: dict[tuple, dict] = {}
    aternity = _Once(aternity)
    todo = [g for g in groups if not (g["why"] or g["source"].startswith("NetIM")
                                      or g["what"].startswith("Network round trip"))]  # NetIM and "who else"
    todo.sort(key=lambda g: (not g["slow_actions"], ISSUE_ORDER.get(g["issue"], 9)))
    todo = todo[:MAX_EXPLAIN]
    changed = {dev: _pc_changes(aternity, dev, start, end) for dev in dict.fromkeys(g["object"] for g in todo)}
    virtual = set()
    if hasattr(aternity, "virtual_devices"):
        for dev in changed:
            virtual |= aternity.virtual_devices(start, end, device=dev)
    history = set()
    for g in todo:
        if g["app"] and (g["object"], g["app"]) in done:  # a hang and its health event: one cause
            g["why"] = {**done[(g["object"], g["app"])], "same_as_above": True}
            continue
        log.info("Looking for the cause: %s", g["text"])
        why = {"failure": None, "others": None, "changes": [], "processes": [], "text": [], "todo": [], "possible": []}
        times = g["_times"] or g["_all"]  # at the slow actions, else whenever it happened
        if g["issue"] == "disk":
            _disk(aternity, g, events, start, end, why)
        elif g["app"] or g["source"] == EVENT:
            _failure(aternity, g, events, times, window, start, end, why, history)
        if g["source"] == EVENT and "memory" in g["what"].lower():
            _processes(aternity, g, times, window, why, "Memory %")
        elif not (g["app"] or g["source"] == EVENT):
            _processes(aternity, g, times, window, why, g["what"])
        _changes(changed[g["object"]], g, min(a for a, _ in times), why, g["object"] in virtual)
        why["text"] += g.get("notes", [])
        g["why"] = why
        if g["app"]:
            done[(g["object"], g["app"])] = why
    return changed


class _Once:
    """The Aternity source, asking each question once per investigation (e.g. the
    other PCs' events of one app, for several problems of that app)."""
    CACHED = ("app_health_events", "component_health_events", "named_health_events", "software_changes",
              "app_usage_everywhere", "known_pcs")

    def __init__(self, source):
        self._source, self._seen = source, {}

    def __getattr__(self, name):
        fn = getattr(self._source, name)
        if name not in self.CACHED:
            return fn

        def once(*args):
            if (name, args) not in self._seen:
                self._seen[(name, args)] = fn(*args)
            return self._seen[(name, args)]
        return once


def _event_what(e) -> str:
    """The problem text of a health event, as correlate._event_problem builds it."""
    from .correlate import _event_problem
    return _event_problem(e)["what"]


def _same_failure(found, dev, where, kind, error) -> list:
    """Other PCs' events of the same kind (Aternity's category and name, e.g. a hang), in the
    same process, the same module when Aternity knows it, the same error when both have one."""
    return [e for e in found
            if e.device != dev and e.component == where[0] and (e.category, e.name) == kind
            and (where[2] is None or e.sub_component == where[2])
            and (error is None or e.error is None or e.error == error)]


def _audience(aternity, app, dev, start, end) -> tuple:
    """(how many other PCs could have had it, of what): the PCs using the app, else
    every PC in the inventory."""
    if app and hasattr(aternity, "app_usage_everywhere"):
        using = {u.device for u in aternity.app_usage_everywhere(app, start, end)} - {dev}
        if using:
            return len(using), f"PCs using {app}"
    if not app and hasattr(aternity, "known_pcs"):
        return aternity.known_pcs(), "PCs in Aternity's inventory"
    return None, None


def _pcs_per_day(items, first, last) -> dict:
    """(time, PC) pairs as {day: PCs that day}, with every day from first to last."""
    days: dict = {}
    for t, device in items:
        days.setdefault(local_date(t), set()).add(device)
    span = range((last - first).days + 1)
    return {(first + timedelta(days=i)).isoformat(): len(days.get(first + timedelta(days=i), ())) for i in span}


def _trend(per_day: dict, start) -> str | None:
    """The last 7 days against the 6 before, PCs per working day: "growing", "falling",
    "steady", or None when too few PCs to tell."""
    def mean(lo, hi):
        days = [local_date(start) - timedelta(days=k) for k in range(lo, hi + 1)]
        counts = [per_day.get(d.isoformat(), 0) for d in days if d.weekday() < 5]
        return sum(counts) / len(counts) if counts else 0
    now, before = mean(1, 7), mean(8, 13)
    if max(now, before) < TREND_MIN_PCS:
        return None
    return "growing" if now >= before * TREND_RATIO else "falling" if before >= now * TREND_RATIO else "steady"


def _since(aternity, read, dev, where, kind, error, start) -> dict | None:
    """Since when other PCs fail the same way: their events in the 14 days before the
    window. {"since": first day seen, or None when none of those days had it; PCs per
    day (the 13 whole days) and the trend}."""
    warned = len(getattr(aternity, "warnings", []))
    rows = _same_failure(read(start - CHANGES_BEFORE, start), dev, where, kind, error)
    if len(getattr(aternity, "warnings", [])) != warned:
        return None  # a partial read cannot say when it started
    first = min((e.timestamp for e in rows), default=None)
    per_day = _pcs_per_day(((e.timestamp, e.device) for e in rows), local_date(start) - timedelta(days=13),
                           local_date(start) - timedelta(days=1))
    return {"since": _iso(first) if first else None, "days": CHANGES_BEFORE.days,
            "from_start": bool(first) and first - (start - CHANGES_BEFORE) < timedelta(days=1),
            "per_day": per_day, "trend": _trend(per_day, start)}


def _usage_days_before(aternity, app, dev, hang, start) -> dict | None:
    """How long before the window other PCs already had it in their app usage. Aternity
    keeps these rows about 8 days, so this says "already", never when it began."""
    if not hasattr(aternity, "app_failures_everywhere"):
        return None
    warned = len(getattr(aternity, "warnings", []))
    rows = aternity.app_failures_everywhere(app, start - CHANGES_BEFORE, start)
    if len(getattr(aternity, "warnings", [])) != warned:
        return None  # a partial read says nothing
    rows = [u for u in rows if u.device != dev and (u.hang_time if hang else u.crashes)]
    first = min((u.timestamp for u in rows), default=None)
    days = (start - first).days if first else 0
    if not days:
        return None  # no trend: about 8 days kept, not two weeks to compare
    return {"since": _iso(first), "days": days, "from_start": True, "trend": None,
            "per_day": _pcs_per_day(((u.timestamp, u.device) for u in rows), local_date(first),
                                    local_date(start) - timedelta(days=1))}


def _usage_others(aternity, g, start, end, why, history) -> None:
    """A hang or crash Aternity has no details of: the other PCs where its app usage
    shows the same, out of the PCs using the app."""
    if not hasattr(aternity, "app_usage_everywhere"):
        return
    dev, app = g["object"], g["app"]
    hang = g["issue"] == "hang"
    rows = aternity.app_usage_everywhere(app, start, end)
    pcs: dict[str, dict] = {}
    for u in sorted(rows, key=lambda u: u.timestamp):
        if u.device != dev and (u.hang_time if hang else u.crashes):
            p = pcs.setdefault(u.device, {"device": u.device, "username": None, "version": "", "times": 0,
                                          "first": _iso(u.timestamp)})
            p["times"] += 1
            p["last"] = _iso(u.timestamp)
    using = {u.device for u in rows} - {dev}
    if using:
        wide = pcs and (app in history or len(history) < MAX_HISTORY)
        if wide:
            history.add(app)
        why["others"] = {"app": app, "what": f"{app} {'not responding' if hang else 'crashed'}", "pcs": len(pcs),
                         "users": None, "of": len(using), "of_what": f"PCs using {app}", "versions": {},
                         "this_version": None,
                         "window_days": _pcs_per_day(((u.timestamp, u.device) for u in rows if u.device != dev
                                                      and (u.hang_time if hang else u.crashes)), local_date(start),
                                                     local_date(end)),
                         "history": _usage_days_before(aternity, app, dev, hang, start) if wide else None,
                         "list": sorted(pcs.values(), key=lambda p: (-p["times"], p["device"]))[:100]}


def _failure(aternity, g, events, times, window, start, end, why, history) -> None:
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
        why["text"].append(f"Aternity has no details of this on {dev}, only that it happened.")
        _usage_others(aternity, g, start, end, why, history)
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
    err = plain(last.exception or error)
    what = kind[1] or kind[0] or "failure"
    subject = f"{app} failed" if app else f"Aternity recorded {what} on {dev}"
    place = f" in {_module(where)}" if where[0] or where[2] else ""
    if app or place or err:  # else the issue's own line says it all
        why["text"].append(f"{subject}{place}" + (f": {err}" if err else "")
                           + ("" if near or not g["of_slow"] else " (from its events in this window, not at the slow actions)")
                           + ".")
    if last.details and not FAULT_OFFSET.fullmatch(" ".join(last.details.split())):
        why["text"].append(f"Aternity's details: {' '.join(last.details.split())[:300]}")

    # what to look at: not a name that isn't a program, nor the Windows file that reported it
    module = plain(where[2] or where[0])
    look, name = _module(where), module or app
    if module.lower() in NO_PROGRAM:
        why["text"].append(f"{module} is not a program: Windows could not tell which program failed.")
        look = name = None
    elif where[2] and module.lower() in WINDOWS_MODULES:
        comp = plain(where[0])
        why["text"].append(f"{module} is a Windows file: it reports a failure, the cause is usually in "
                           f"{comp or app or 'the program it ran in'}.")
        look, name = _module((where[0], where[1], None, None)) if comp else None, comp or app
    step = None
    if look and module:
        step = (f"On {dev}: look at {look}, where {app} failed (repair or update it; if it is an add-in, "
                "try without it)." if app else f"On {dev}: look at {look}, the process that failed.")

    # only this PC, or others too? Found by the app, else by the process that failed.
    if app and hasattr(aternity, "app_health_events"):
        read = lambda a, b: aternity.app_health_events(app, a, b)  # noqa: E731
    elif not app and where[0] and hasattr(aternity, "component_health_events"):
        read = lambda a, b: aternity.component_health_events(where[0], a, b)  # noqa: E731
    elif not app and kind[1] and hasattr(aternity, "named_health_events"):  # e.g. low memory: no process
        read = lambda a, b: aternity.named_health_events(kind[1], a, b)  # noqa: E731
    else:
        why["todo"] += [step] if step else []
        return
    same = _same_failure(read(start, end), dev, where, kind, error)
    version = lambda e: plain(e.sub_component_version if where[2] else e.component_version) or UNKNOWN_VERSION  # noqa: E731
    pcs: dict[str, dict] = {}
    for e in sorted(same, key=lambda e: e.timestamp):
        p = pcs.setdefault(e.device, {"device": e.device, "username": None, "version": version(e),
                                      "times": 0, "first": _iso(e.timestamp)})
        p["username"] = e.username or p["username"]
        p["version"] = version(e)
        p["times"] += int(e.count or 1)
        p["last"] = _iso(e.timestamp)
    versions = Counter(p["version"] for p in pcs.values())
    of, of_what = _audience(aternity, app, dev, start, end)
    wide = len(pcs) >= 2 and (key := app or where[0] or kind[1]) and (key in history or len(history) < MAX_HISTORY)
    if wide:
        history.add(key)
    # said once, on the issue ("Who else")
    named = f"{app} {what}" if app else what
    why["others"] = {"app": app, "what": f"{named}{place}" + (f", error {plain(error)}" if error else ""), "pcs": len(pcs),
                     "users": len({e.username for e in same if e.username}) or None, "of": of, "of_what": of_what,
                     "this_version": plain(where[3] if where[2] else where[1]) or UNKNOWN_VERSION,
                     "versions": dict(versions.most_common(5)),
                     "window_days": _pcs_per_day(((e.timestamp, e.device) for e in same), local_date(start),
                                                 local_date(end)),
                     "history": _since(aternity, read, dev, where, kind, error, start) if wide else None,
                     "list": sorted(pcs.values(), key=lambda p: (-p["times"], p["device"]))[:100]}
    if len(pcs) < 2:  # one other PC does not make it more than this PC's problem
        why["todo"] += [step] if step else []
    elif name:
        why["todo"].append(f"{len(pcs)} other PCs fail the same way: treat it as a {name} problem "
                           f"(version, update, vendor), not this PC alone.")
    elif where[0]:
        why["todo"].append(f"{len(pcs)} other PCs fail the same way and Windows cannot name the program: "
                           "see when it started, and what was rolled out then.")


def _disk(aternity, g, events, start, end, why) -> None:
    """The PC's disk is failing: what Aternity recorded on it, in this window and the
    DISK_BACK days before (correlate._disk_state), other PCs recording the same now,
    and the step: back up the user's data and replace the disk."""
    dev = g["object"]
    mine = sorted((e for e in events if e.device == dev and is_disk(e)), key=lambda e: e.timestamp)
    kinds: dict[tuple, list] = {}
    for e in mine:
        kinds.setdefault((e.name or e.category or "a health event", e.component), []).append(e)
    why["text"].append(f"Aternity recorded the disk failing on {dev}: " + "; ".join(
        f"{name} in {comp} " + (f"at {clock(es[0].timestamp)}" if len(es) == 1 else
                                f"{len(es)} times, first at {clock(es[0].timestamp)}")
        for (name, comp), es in kinds.items()) + ".")
    state = g.get("disk_state") or {}
    if state.get("before_days"):
        why["text"].append(f"It was failing before this window too: Aternity recorded it on "
                           f"{plural(state['before_days'], 'day')} of the {DISK_BACK.days} days before, first at "
                           f"{clock(parse_time(state['first']))}.")
    elif state.get("before_read"):
        why["text"].append(f"Nothing about this disk in the {DISK_BACK.days} days before this window.")
    # other PCs recording the same now (many at once would point at the records, not the disks)
    names = list(dict.fromkeys(e.name for e in mine if e.name))
    pcs: dict[str, dict] = {}
    same = []
    if names and hasattr(aternity, "named_health_events"):
        same = [e for n in names for e in aternity.named_health_events(n, start, end) if e.device != dev and is_disk(e)]
        for e in sorted(same, key=lambda e: e.timestamp):
            p = pcs.setdefault(e.device, {"device": e.device, "username": None, "version": "", "times": 0,
                                          "first": _iso(e.timestamp)})
            p["username"] = e.username or p["username"]
            p["times"] += int(e.count or 1)
            p["last"] = _iso(e.timestamp)
        of, of_what = _audience(aternity, None, dev, start, end)
        why["others"] = {"app": None, "what": f"the disk failing ({', '.join(names)})", "pcs": len(pcs),
                         "users": len({e.username for e in same if e.username}) or None, "of": of,
                         "of_what": of_what, "this_version": UNKNOWN_VERSION, "versions": {},
                         "window_days": _pcs_per_day(((e.timestamp, e.device) for e in same), local_date(start),
                                                     local_date(end)),
                         "history": None, "list": sorted(pcs.values(), key=lambda p: (-p["times"], p["device"]))[:100]}
    model = next((_unlabelled(e.component) for e in mine if (part_label(e.component) or "").lower() == "disk"), None)
    disk = f"the disk ({model})" if model else "the disk"
    if len(pcs) >= 2:
        why["todo"].append(f"On {dev}: back up the user's data. {len(pcs)} other PCs record the same in this window: "
                           f"see Who else before replacing {disk}.")
    else:
        why["todo"].append(f"On {dev}: back up the user's data and replace {disk}: Aternity recorded it failing.")


def _processes(aternity, g, times, window, why, metric) -> None:
    """The busiest processes on the PC around the problem. A process row may cover
    up to an hour (PROCESS_ROW_SPAN_MIN), so rows that close are read. Something to
    look at only when it touched the user: not for a limit passed at other times."""
    if metric not in METRIC or not hasattr(aternity, "top_processes"):
        return
    label, key, unit = METRIC[metric]
    dev = g["object"]
    near = max(window, timedelta(minutes=PROCESS_ROW_SPAN_MIN))
    rows = [p for p in aternity.top_processes(dev, min(a for a, _ in times) - near, max(b for _, b in times) + near)
            if _near(p.timestamp, times, near)]
    if key == "memory" and not any(p.memory is not None for p in rows):
        key, unit = "memory_bytes", " MB"  # the hourly source gives bytes
    best: dict[str, tuple] = {}
    for p in rows:
        v = (None if p.io_read is None and p.io_write is None else (p.io_read or 0) + (p.io_write or 0)) \
            if key == "io" else getattr(p, key)
        if v is not None:
            v = v / 1048576 if key == "memory_bytes" else v
            if p.process not in best or v > best[p.process][0]:
                best[p.process] = (v, p.application)
    top = sorted(best.items(), key=lambda kv: -kv[1][0])[:3]
    why["processes"] = [{"process": n, "value": round(v, 1)} for n, (v, _) in top]
    if not top:
        why["text"].append(f"Aternity has no {label} per process for {dev} at those times."
                           if rows else f"Aternity has no process data for {dev} at those times.")
        return
    app = lambda n, a: f"{n} ({a})" if a and a.lower() != n.lower() else n  # noqa: E731
    listed = ", ".join(f"{app(n, a)} {label} up to {v:.1f}{unit}" for n, (v, a) in top)
    if near != window:
        # one row per program and hour: it cannot show which program it was at that minute,
        # so it is said so, and no program is named as the one to look at
        why["text"].append(f"Aternity keeps {label} per program only per hour, so it cannot show which program "
                           f"it was at that moment. In the hours around it, the highest per program: {listed}.")
        # one program named only when its hourly figure alone is half the PC's reading or more
        # (CPU: both in %); a few % cannot explain a PC near its limit
        # an event (e.g. low memory) says the PC ran short: its biggest user is where to look
        if g["source"] == EVENT:
            why["todo"].append(f"On {dev}: look at {top[0][0]}: {label} up to {top[0][1][0]:.1f}{unit} in the hours "
                               "around it, the most of any program.")
        elif unit == "%" and g.get("worst") and top[0][1][0] * 2 >= g["worst"] and g["_times"]:
            why["todo"].append(f"On {dev}: look at {top[0][0]}: {label} up to {top[0][1][0]:.1f}% in the hours "
                               f"around it, against the PC's {g['worst']}%.")
        return
    why["text"].append(f"Busiest programs then: {listed}.")
    if g["_times"] or g["source"] == EVENT:
        why["todo"].append(f"On {dev}: look at {top[0][0]}, the busiest program at those times.")


def changes_before(aternity, device: str, when, names, part: str, event: str) -> dict:
    """What was installed, updated or removed on `device` in the days before `when`.
    A change whose name matches one of `names` (an app, program or module) is marked."""
    out = {"changes": [], "text": None, "todo": None}
    if not hasattr(aternity, "software_changes"):
        return out
    # read past `when` too: a removal put back later is not a change (see _updates)
    rows = _updates(aternity.software_changes(device, when - CHANGES_BEFORE, when + CHANGES_BEFORE),
                    when - CHANGES_BEFORE, when)
    parts = _parts(names)
    for c in rows[:8]:
        related = bool(c["name"]) and not c.get("back") and any(same_name(p, c["name"]) for p in parts)
        out["changes"].append({**_as_iso(c), "related": related})
    if not rows:
        out["text"] = (f"Nothing was installed, updated or removed on {device} in the "
                       f"{CHANGES_BEFORE.days} days before {_day(when)}.")
        return out
    shown = sorted(out["changes"], key=lambda c: not c["related"])[:3]
    out["text"] = (f"Changed on {device} in the {CHANGES_BEFORE.days} days before {_day(when)}: " + "; ".join(
        change_text(c) + (f" (same name as the {part})" if c["related"] else "") for c in shown)
        + (f" (+{len(rows) - 3} more)" if len(rows) > 3 else "") + ".")
    rel = next((c for c in out["changes"] if c["related"]), None)
    if rel:
        out["todo"] = (f"Check the {rel['name']} change of {_day(parse_time(rel['time']))} on {device}: it came "
                       f"before the {event}.")
    return out


def _as_iso(c: dict) -> dict:
    return {**c, "time": _iso(c["time"]), **({"until": _iso(c["until"])} if c.get("until") else {})}


def change_text(c: dict) -> str:
    """One change as a phrase: "Oct 26 Teams updated 1.0 → 2.0"."""
    t = c["time"] if not isinstance(c["time"], str) else parse_time(c["time"])
    until = c.get("until")
    if until is not None:
        until = parse_time(until) if isinstance(until, str) else until
    return (f"{_day(t)} {c['name']} {c['change'] or 'changed'}"
            + (f" {c['version_before']} → {c['version']}" if c["version_before"] and c["version"] else
               f" {c['version']}" if c["version"] else "")
            + (f" ({_day(until)})" if until else "")
            + (" (it comes and goes in Aternity's log)" if c.get("flaps") else ""))


def changes_between(aternity, device: str, after, before) -> dict:
    """What changed on `device` between the last normal time and the first slow one:
    {"changes": [...], "text": sentence}."""
    if not hasattr(aternity, "software_changes"):
        return {"changes": [], "count": 0, "text": None}
    rows = _updates(aternity.software_changes(device, after - CHANGES_BEFORE, before + CHANGES_BEFORE),
                    after, before)
    span = f"between {_day(after)} and {_day(before)}" if _day(after) != _day(before) else f"on {_day(after)}"
    if not rows:
        return {"changes": [], "count": 0, "text": f"Nothing was installed, updated or removed on {device} {span}."}
    # many removals with no install in one minute: one line, never a change to check
    batch = batch_removals(rows)
    real = [c for c in rows if id(c) not in batch]
    told = [change_text(c) for c in real[:4]] + ([f"{len(batch)} programs logged as uninstalled in the same "
                                                  "minute, with no install logged (not changes to check)"]
                                                 if batch else [])
    # "changes" = what is different afterwards; a same-version put-back is only told
    return {"changes": [_as_iso(c) for c in real if not c.get("back")][:8],
            "count": sum(1 for c in real if not c.get("back")),
            "text": f"Changed on {device} {span}: " + "; ".join(told)
            + (f" (+{len(real) - 4} more)" if len(real) > 4 else "") + "."}


REMOVED_TOGETHER = 5  # this many "Uninstalled" rows in one minute, none installed: one batch


def batch_removals(rows: list[dict]) -> set[int]:
    """ids of the removals with no install logged that come REMOVED_TOGETHER or more in
    one minute. On live virtual desktops (Nov 2026) such a minute lists programs still in
    use afterwards (Microsoft 365 Apps among them): not removals to look at one by one."""
    lone = [c for c in rows if c["change"] in SOFTWARE_REMOVED and not c.get("back")]
    per = Counter(c["time"].replace(second=0, microsecond=0) for c in lone)
    return {id(c) for c in lone if per[c["time"].replace(second=0, microsecond=0)] >= REMOVED_TOGETHER}


def _updates(rows, start=None, end=None) -> list[dict]:
    """The change log as changes: real changes first, newest first.
    - A removal and an install of one software within SOFTWARE_UPDATE_GAP_MIN are
      one change: "updated" (old -> new version), or "reinstalled" (same version).
    - A removal and a later install of the same version (or an install and a later
      removal), further apart, change nothing in the end: one line marked "back",
      listed last. On a live Aternity these come on many PCs at the same minutes.
    - A software whose same version comes and goes like that on this PC (a live
      Aternity logs Chrome removed one day, back the next, removed again) is noise:
      its other rows at that version are told last too, never a change to check.
    - Anything else stays as Aternity logged it.
    Rows after `end` (or before `start`) are read only to find a row's other half:
    only changes in [start, end] are returned."""
    gap = timedelta(minutes=SOFTWARE_UPDATE_GAP_MIN)
    rows = sorted(rows, key=lambda c: c.timestamp)
    used: set[int] = set()
    real, back = [], []

    def other_half(c, near: bool):
        removed = c.change in SOFTWARE_REMOVED
        return next((o for o in rows if id(o) not in used and o is not c and o.name == c.name
                     and o.change != "Updated" and c.change != "Updated"
                     and (o.change in SOFTWARE_REMOVED) != removed and o.timestamp >= c.timestamp
                     and (o.timestamp - c.timestamp <= gap if near else o.version == c.version)), None)

    for near in (True, False):
        for c in rows:
            if id(c) in used:
                continue
            o = other_half(c, near)
            if o is None:
                continue
            used |= {id(c), id(o)}
            old, new = (c, o) if c.change in SOFTWARE_REMOVED else (o, c)
            if near:
                same = old.version == new.version
                real.append({"time": o.timestamp, "name": c.name, "change": "reinstalled" if same else "updated",
                             "version": new.version, "version_before": None if same else old.version})
            else:
                back.append({"time": c.timestamp, "until": o.timestamp, "name": c.name, "back": True,
                             "change": ("removed, then put back" if c.change in SOFTWARE_REMOVED
                                        else "installed, then removed again") + " at",
                             "version": c.version, "version_before": None})
    flaps = {(c["name"], c["version"]) for c in back}
    for c in rows:
        if id(c) in used:
            continue
        row = {"time": c.timestamp, "name": c.name, "change": c.change, "version": c.version,
               "version_before": c.version_before}
        if (c.name, c.version) in flaps:
            back.append({**row, "back": True, "flaps": True})
        else:
            real.append(row)

    def inside(c) -> bool:
        return any(t is not None and (start is None or t >= start) and (end is None or t <= end)
                   for t in (c["time"], c.get("until")))
    real = sorted(filter(inside, real), key=lambda c: c["time"], reverse=True)
    return real + sorted(filter(inside, back), key=lambda c: c["time"], reverse=True)


def _pc_changes(aternity, device: str, start, end) -> list[dict]:
    """What changed on the PC from 14 days before the window to its end, read once."""
    if not hasattr(aternity, "software_changes"):
        return []
    # read past the end too: a removal put back later is not a change (see _updates)
    return _updates(aternity.software_changes(device, start - CHANGES_BEFORE, end + CHANGES_BEFORE),
                    start - CHANGES_BEFORE, end)


def _matches(c: dict, parts: list[tuple[str, ...]], versions: set[str]) -> str | None:
    """How a change matches the failing part: its name (same_name), or the version it brought."""
    if c.get("back") or not c["name"]:
        return None
    if any(same_name(p, c["name"]) for p in parts):
        return "name"
    return "version" if c["version"] in versions and c["change"] not in SOFTWARE_REMOVED else None


def _other_version(c: dict, others: dict | None) -> str | None:
    """A version other PCs fail the same way on that this change did not bring."""
    if not c["version"]:
        return None
    found = [v for v in (others or {}).get("versions", {}) if v not in (UNKNOWN_VERSION, c["version"])]
    return found[0] if found else None


def _changes(rows: list[dict], g, first, why, virtual=False) -> None:
    """A change is told on an issue only when it matches the failing part. Before the
    issue, and with no other PC failing the same way on another version, it is a possible
    cause; otherwise what rules it out, or leaves it open, is said."""
    f = why["failure"] or {}
    names = _parts((g["app"], f.get("component"), f.get("sub_component")))
    versions = {plain(f.get(k)) for k in ("component_version", "sub_component_version")} - {""}
    batch = batch_removals(rows)  # told once in What changed, never one by one
    for c in rows:
        match = _matches(c, names, versions)
        before = c["time"] <= first
        if before and len(why["changes"]) < 8:
            why["changes"].append({**_as_iso(c), "related": bool(match)})
        if not match:
            continue
        if c["change"] in SOFTWARE_REMOVED:
            # paired removals are updates by now: this one has no install logged. The
            # software failed after it, so it was still there: not a removal, not a cause
            if before and id(c) not in batch:
                why["text"].append(f"Aternity logged {c['name']} {c['version'] or ''} as uninstalled on "
                                   f"{_day(c['time'])} with no install logged, but it failed after that, so it was "
                                   "still there: that row is not a removal.".replace("  ", " "))
            continue
        other = _other_version(c, why["others"])
        if other:
            why["text"].append(f"{change_text(c)}: same {match} as the failing part, but other PCs fail the same "
                               f"way on {other}, so this is not new with this change.")
        elif before:
            why["possible"].append(f"{change_text(c)} (same {match} as the failing part, before it).")
            if not any("Check the" in t for t in why["todo"]):
                why["todo"].append(f"Check the {c['name']} change of {_day(c['time'])} on {g['object']}: "
                                   "it came before the first failure.")
        else:
            why["text"].append(f"{change_text(c)}: same {match} as the failing part, but Aternity logged it after "
                               "this failure" + (". On a virtual desktop the log can run late, so that does not "
                                                 "clear it." if virtual else "."))
