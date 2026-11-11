"""Today's issues: the overview on the "Today" tab (/api/today).

What Aternity itself rated, counted for today (from midnight in `timezone`):
- health events Aternity rates Critical or Major (TILE_SEVERITIES), as PCs,
  and the event kinds on the most PCs, each with its last 7 days;
- actions with Aternity's worst status (TOP_STATUS, "Major"): PCs and users,
  per hour and per application.
Each count is told against the same kind of day before (working days Mon-Fri
with working days, weekends with weekends) at the same time of day: more than
on any of them, fewer than on any of them, or as usual (within their range).
Counts only; the status and severity words are Aternity's.

Reading, by one background thread (`keep_reading`):
- activity an hour at a time: at start the last START_HOURS (also the name
  list's history), then every REFRESH_MIN the current hour, and each hour
  once more LATE_MIN after it ends, for rows Aternity sends late (the log
  says how many came late);
- health events: today's every REFRESH_MIN (about 1,400 rows a day on the
  live tenant), and the HEALTH_DAYS before once a day (kept 90 days).
Activity is kept only about 8 days, so the usual for actions comes from the
counts saved at the end of each day in <data>/days/<date>.json (counts and
application names only, no user or PC): it builds up over the first week.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .aternity import is_virtual, odata_time
from .config import SEVERITY_ORDER, TILE_SEVERITIES, TOP_STATUS
from .http import short_error
from .models import parse_time, to_float

log = logging.getLogger("uxcorrelate")
REFRESH_MIN = 15
LATE_MIN = 15  # an hour is read once more this long after it ends
START_HOURS = 24
HEALTH_DAYS = 14  # 7 days of small bars, and the same kind of day for the usual
USUAL_DAYS = 5  # days of the same kind compared with
MIN_USUAL = 3  # days needed to say what is usual
KEEP_FILES_DAYS = 60
TOP = 10  # rows in each chart
WHO = 50  # rows in a list opened by a click
SLOW_PAD = timedelta(minutes=30)  # Investigate: from 30 min before a user's first Major action to 30 min after the last
NO_APP = "(no application)"
HOUR = timedelta(hours=1)
EVENT_PAD = HOUR  # and an hour around an event
LATE = timedelta(minutes=LATE_MIN)


def zone_of(name: str):
    """(time zone, its name, a note when the setting is missing or wrong)."""
    if not name:
        return timezone.utc, "UTC", ("The day runs from midnight UTC: set timezone in config.yaml "
                                     "(e.g. America/Chicago) for your own.")
    try:
        return ZoneInfo(name), name, None
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc, "UTC", (f"timezone '{name}' in config.yaml is not a known time zone: "
                                     "the day runs from midnight UTC.")


def pc_kind(value) -> str:
    """Aternity's VIRTUALIZATION value as "virtual", "physical" or "not known"."""
    v = is_virtual(value)
    return "not known" if v is None else "virtual" if v else "physical"


def working(d: date) -> bool:
    return d.weekday() < 5


def kind_of(d: date) -> str:
    return "working days" if working(d) else "weekend days"


def usual(values: list[int], now: int) -> dict:
    """Today's count against the same kind of day before: the range and the verdict."""
    if len(values) < MIN_USUAL:
        return {"enough": False, "days": len(values)}
    low, high = min(values), max(values)
    verdict = "more" if now > high else "fewer" if now < low else "usual"
    return {"enough": True, "days": len(values), "low": low, "high": high, "verdict": verdict}


@dataclass
class Hour:
    """One local hour of activity, as read from Aternity."""
    start: datetime  # UTC
    day: date  # local
    hour: int  # local hour of day
    rows: int = 0
    status: Counter = field(default_factory=Counter)
    apps: dict = field(default_factory=dict)  # app -> Counter(runs, TOP_STATUS)
    app_devs: dict = field(default_factory=dict)  # app -> PCs with TOP_STATUS
    top_devs: set = field(default_factory=set)
    top_users: set = field(default_factory=set)
    devs: set = field(default_factory=set)
    top_rows: list = field(default_factory=list)  # (time, PC, user, app, activity, seconds) of TOP_STATUS actions
    kinds: dict = field(default_factory=dict)  # (app, PC kind) -> Counter(runs, TOP_STATUS); kind: virtual/physical/not known
    kind_devs: dict = field(default_factory=dict)  # PC kind -> PCs active
    kind_top_devs: dict = field(default_factory=dict)  # PC kind -> PCs with TOP_STATUS
    user_devs: dict = field(default_factory=dict)  # username (lower case) -> PCs used (for the Care list)
    user_runs: Counter = field(default_factory=Counter)  # username (lower case) -> actions measured
    dev_kind: dict = field(default_factory=dict)  # PC -> PC kind
    read_at: datetime | None = None
    partial: bool = False
    final: bool = False  # read LATE_MIN or more after it ended


class Today:
    def __init__(self, zone: str = "", store: str | os.PathLike | None = None):
        self.zone, self.zone_label, self.zone_note = zone_of(zone)
        self.store = Path(store) / "days" if store else None
        self.hours: dict[datetime, Hour] = {}
        self.saved: dict[date, dict] = {}  # summaries of past days, from the files
        self.health: list[tuple] = []  # today's: (time, device, kind, severity, user, application)
        self.health_days: dict[date, dict] = {}  # day -> kind -> {device: first second of the day}
        self.health_apps: dict[date, dict] = {}  # day before -> (kind, application) -> devices
        self.health_sev: dict = {}  # kind -> Aternity's severity
        self.health_for: date | None = None  # the day the days before were read for
        self.health_read: set[date] = set()  # days before that were read (some may have no event)
        self.notes: dict[str, str] = {}  # what went wrong while reading, by source
        self.status = "Starting"
        self.ready = False
        self.off: str | None = None
        self.started = False  # the background reader (one per process)
        self.as_of: datetime | None = None
        self.lock = threading.Lock()
        self.load()

    # -- local days and hours ----------------------------------------------
    def local(self, t: datetime) -> datetime:
        return t.astimezone(self.zone)

    def day(self, t: datetime) -> date:
        return self.local(t).date()

    def midnight(self, d: date) -> datetime:
        return datetime(d.year, d.month, d.day, tzinfo=self.zone).astimezone(timezone.utc)

    def hour_starts(self, d: date) -> list[datetime]:
        """The local hours of a day, as UTC starts (23 or 25 on a clock change)."""
        a, b = self.midnight(d), self.midnight(d + timedelta(days=1))
        return [a + HOUR * k for k in range(round((b - a) / HOUR))]

    def hour_of(self, t: datetime) -> datetime:
        a = self.midnight(self.day(t))
        return a + HOUR * int((t - a) // HOUR)

    # -- activity ----------------------------------------------------------
    def add_hour(self, start: datetime, rows: list[dict], f: dict, read_at: datetime,
                 partial: bool = False) -> int | None:
        """Count one hour of activity rows (replacing an earlier read); the rows that read had."""
        h = Hour(start=start, day=self.day(start), hour=self.local(start).hour, rows=len(rows),
                 read_at=read_at, partial=partial, final=read_at >= start + HOUR + LATE)
        for r in rows:
            status, dev, user = r.get(f["status"]), r.get(f["device"]), r.get(f["username"])
            app = r.get(f["application"]) or NO_APP
            pc = pc_kind(r.get(f["virtualization"]) if f.get("virtualization") else None)
            h.status[str(status) if status is not None else "(none)"] += 1
            if dev:
                h.devs.add(dev)
                h.kind_devs.setdefault(pc, set()).add(dev)
                h.dev_kind[dev] = pc
            if user:
                h.user_runs[user.lower()] += 1
                if dev:
                    h.user_devs.setdefault(user.lower(), set()).add(dev)
            runs = h.apps.setdefault(app, Counter())
            runs["runs"] += 1
            by_kind = h.kinds.setdefault((app, pc), Counter())
            by_kind["runs"] += 1
            if status == TOP_STATUS:
                runs[TOP_STATUS] += 1
                by_kind[TOP_STATUS] += 1
                if dev:
                    h.kind_top_devs.setdefault(pc, set()).add(dev)
                if dev:  # its own time; the hour's start if it has none, so the list matches the counts
                    h.top_rows.append((parse_time(r.get(f["timestamp"])) or start, dev, user, app,
                                       r.get(f.get("activity", "")), to_float(r.get(f.get("response_time", "")))))
                if dev:
                    h.top_devs.add(dev)
                    h.app_devs.setdefault(app, set()).add(dev)
                if user:
                    h.top_users.add(user)
        with self.lock:
            old = self.hours.get(start)
            self.hours[start] = h
        return old.rows if old else None

    def hours_due(self, now: datetime) -> list[datetime]:
        """The hours to read now: the current one, those not yet read again after they
        ended, and any of today's that could not be read."""
        cur = self.hour_of(now)
        due = [cur]
        for start, h in sorted(self.hours.items()):
            if start != cur and not h.final and start + HOUR + LATE <= now:
                due.append(start)
        due += [s for s in self.hour_starts(self.day(now)) if s < cur and s not in self.hours]
        return list(dict.fromkeys(due))

    def summary(self, d: date) -> dict:
        """A day's counts, as saved: no user or PC names."""
        starts = self.hour_starts(d)
        hours = [self.hours[s] for s in starts if s in self.hours]
        by_hour: dict[str, Counter] = {}
        first: dict[str, int] = {}  # PC -> local hour of its first TOP_STATUS action
        apps: dict[str, Counter] = {}
        app_devs: dict[str, set] = {}
        devs, users = set(), set()
        kinds: dict[str, Counter] = {}
        kind_devs: dict[str, set] = {}
        kind_top: dict[str, set] = {}
        for h in hours:
            for (_, pc), c in h.kinds.items():
                kinds.setdefault(pc, Counter()).update(c)
            for pc, ds in h.kind_devs.items():
                kind_devs.setdefault(pc, set()).update(ds)
            for pc, ds in h.kind_top_devs.items():
                kind_top.setdefault(pc, set()).update(ds)
            by_hour.setdefault(str(h.hour), Counter()).update(h.status)
            for dev in h.top_devs:
                first[dev] = min(first.get(dev, 24), h.hour)
            users |= h.top_users
            devs |= h.devs
            for app, c in h.apps.items():
                apps.setdefault(app, Counter()).update(c)
            for app, ds in h.app_devs.items():
                app_devs.setdefault(app, set()).update(ds)
        top = sorted((a for a in apps if apps[a][TOP_STATUS]), key=lambda a: -apps[a][TOP_STATUS])[:50]
        return {"date": d.isoformat(), "zone": self.zone_label, "top_status": TOP_STATUS,
                "complete": all(s in self.hours and self.hours[s].final and not self.hours[s].partial
                                for s in starts),
                "rows": sum(h.rows for h in hours), "pcs": len(devs), "top_pcs": len(first),
                "top_users": len(users),
                "hours": {k: dict(v) for k, v in by_hour.items()},
                # PCs with a TOP_STATUS action by the end of each local hour (for "by 14:00")
                "top_pcs_by_hour": [sum(1 for v in first.values() if v <= hh) for hh in range(24)],
                "apps": {a: {"runs": apps[a]["runs"], TOP_STATUS: apps[a][TOP_STATUS],
                             "pcs": len(app_devs.get(a, ()))} for a in top},
                # virtual desktops / physical PCs (VIRTUALIZATION): runs, TOP_STATUS, PCs active, PCs with TOP_STATUS
                "kinds": {pc: {"runs": c["runs"], TOP_STATUS: c[TOP_STATUS], "pcs": len(kind_devs.get(pc, ())),
                               "top_pcs": len(kind_top.get(pc, ()))} for pc, c in kinds.items()}}

    def finish(self, now: datetime) -> None:
        """Save each day before today once its last hour was read again (or 2 hours on)."""
        today = self.day(now)
        for d in sorted({h.day for h in self.hours.values() if h.day < today}):
            mine = [h for h in self.hours.values() if h.day == d]
            if not all(h.final for h in mine) and now < self.midnight(d + timedelta(days=1)) + 2 * HOUR:
                continue
            s = self.summary(d)
            self.save(d, s)
            log.info("Today: %s saved: %d activity rows, %d PCs with a %s action%s", d.strftime("%b %d"),
                     s["rows"], s["top_pcs"], TOP_STATUS,
                     "" if s["complete"] else " (not every hour was read: not used for the usual)")
            with self.lock:
                for h in mine:
                    del self.hours[h.start]

    # -- files ---------------------------------------------------------------
    def load(self) -> None:
        if not self.store:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            cut = date.today() - timedelta(days=KEEP_FILES_DAYS)
            for path in sorted(self.store.glob("*.json")):
                d = date.fromisoformat(path.stem)
                if d < cut:
                    path.unlink()
                    continue
                self.saved[d] = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            self.note("files", f"The saved daily counts in {self.store} could not be read ({short_error(exc)}): "
                               "the usual for actions cannot build up. The folder must be writable by the "
                               "container (uid 1000).")

    def save(self, d: date, summary: dict) -> None:
        with self.lock:
            self.saved[d] = summary
        if not self.store:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            tmp = self.store / f".{d.isoformat()}.tmp"
            tmp.write_text(json.dumps(summary, indent=1, sort_keys=True))
            os.replace(tmp, self.store / f"{d.isoformat()}.json")
            self.notes.pop("files", None)
        except OSError as exc:
            self.note("files", f"Today's counts could not be saved in {self.store} ({short_error(exc)}): the usual "
                               "for actions cannot build up. The folder must be writable by the container "
                               "(uid 1000).")

    def note(self, key: str, text: str) -> None:
        if self.notes.get(key) != text:
            log.warning("Today: %s", text)
        self.notes[key] = text

    # -- health events -----------------------------------------------------
    def set_health(self, rows: list[dict], f: dict, today: bool, partial: bool = False,
                   days_read: tuple = ()) -> dict:
        """Today's health event rows, or those of the days before (days_read: the days they cover)."""
        days: dict[date, dict] = {}
        apps: dict[date, dict] = {}
        parsed, sevs = [], {}
        for r in rows:
            t, dev = parse_time(r.get(f["timestamp"])), r.get(f["device"])
            if t is None or not dev:
                continue
            kind = (r.get(f["category"]) or "", r.get(f["name"]) or "")
            sev = r.get(f["severity"])
            if sev:
                sevs.setdefault(kind, Counter())[sev] += 1
            app = (r.get(f["application"]) if f.get("application") else None) or NO_APP
            parsed.append((t, dev, kind, sev, r.get(f["username"]) if f.get("username") else None, app))
            secs = (t - self.midnight(self.day(t))).total_seconds()
            first = days.setdefault(self.day(t), {}).setdefault(kind, {})
            first[dev] = min(first.get(dev, secs), secs)
            apps.setdefault(self.day(t), {}).setdefault((kind, app), set()).add(dev)
        with self.lock:
            for kind, c in sevs.items():
                self.health_sev[kind] = c  # Aternity gives one severity per event kind (Nov 2026)
            if today:
                self.health = parsed
            else:
                self.health_days.update(days)
                self.health_apps.update(apps)
                for d in days_read:
                    self.health_days.setdefault(d, {})
                    self.health_apps.setdefault(d, {})
                    self.health_read.add(d)
        if partial:
            self.note("health", "Aternity's reply for health events was cut short: some may be missing.")
        return days


    # -- the page ------------------------------------------------------------
    def view(self, now: datetime) -> dict:
        """What /api/today returns."""
        if self.off or not self.ready:
            return {"ready": False, "off": self.off, "status": self.status, "zone": self.zone_label}
        t = self.day(now)
        midnight = self.midnight(t)
        secs = (now - midnight).total_seconds()
        with self.lock:
            hours = sorted((h for h in self.hours.values() if h.day == t), key=lambda h: h.start)
            by_start = {h.start: h for h in hours}
            health = [x for x in self.health if self.day(x[0]) == t]
            health_days = dict(self.health_days)
            saved = dict(self.saved)
            sev = {k: c.most_common(1)[0][0] for k, c in self.health_sev.items() if c}
        kind = kind_of(t)

        # actions Aternity rated TOP_STATUS
        same = sorted((d for d, s in saved.items() if d < t and kind_of(d) == kind and s.get("complete")),
                      reverse=True)[:USUAL_DAYS]
        cut = self.hour_of(now)  # whole hours only: "by 14:00"
        by = self.local(cut).hour - 1 if cut > midnight else None
        top_devs = set().union(*(h.top_devs for h in hours)) if hours else set()
        by_now = len(set().union(*(h.top_devs for h in hours if h.start < cut))) if hours else 0
        slow = {"status": TOP_STATUS, "pcs": len(top_devs),
                "users": len(set().union(*(h.top_users for h in hours))) if hours else 0,
                "actions": sum(h.status[TOP_STATUS] for h in hours),
                "active_pcs": len(set().union(*(h.devs for h in hours))) if hours else 0,
                "by": self.local(cut).strftime("%H:%M") if by is not None else None, "by_pcs": by_now,
                "usual": usual([saved[d]["top_pcs_by_hour"][by] for d in same], by_now)
                if by is not None else {"enough": False, "days": len(same)}}
        per_hour = []
        for s in self.hour_starts(t):
            if s > cut:
                break
            h = by_start.get(s)
            past = [saved[d].get("hours", {}).get(str(self.local(s).hour), {}).get(TOP_STATUS, 0) for d in same]
            per_hour.append({"hour": self.local(s).strftime("%H:%M"), "count": h.status[TOP_STATUS] if h else None,
                             "rows": h.rows if h else None, "now": s == cut,
                             "usual": median(past) if len(past) >= MIN_USUAL else None})
        apps: dict[str, list] = {}
        for h in hours:
            for app, c in h.apps.items():
                a = apps.setdefault(app, [0, 0, set()])
                a[0] += c[TOP_STATUS]
                a[1] += c["runs"]
                a[2] |= h.app_devs.get(app, set())
        app_rows = [{"application": app, "actions": a[0], "runs": a[1], "pcs": len(a[2])}
                    for app, a in sorted(apps.items(), key=lambda kv: (-kv[1][0], kv[0])) if a[0]][:TOP]

        # health events, by kind
        pcs: dict = {}
        for x in health:
            pcs.setdefault(x[2], set()).add(x[1])
        before = sorted((d for d in health_days if d < t and kind_of(d) == kind), reverse=True)[:USUAL_DAYS]
        week = [t - timedelta(days=n) for n in range(7, 0, -1)]

        def event(k) -> dict:
            n = len(pcs.get(k, ()))
            return {"category": k[0], "name": k[1], "severity": sev.get(k), "pcs": n,
                    "usual": usual([sum(1 for s in health_days[d].get(k, {}).values() if s <= secs)
                                    for d in before], n),
                    "days": [{"date": d.isoformat(), "pcs": len(health_days[d].get(k, {}))
                              if d in health_days else None} for d in week]}
        rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        tiles = [event(k) for k in sorted((k for k in pcs if sev.get(k) in TILE_SEVERITIES),
                                          key=lambda k: (rank.get(sev.get(k), 9), -len(pcs[k]), k))]
        none = [s for s in TILE_SEVERITIES if not any(x["severity"] == s for x in tiles)]
        events = [event(k) for k in sorted(pcs, key=lambda k: (-len(pcs[k]), k))[:TOP]]

        notes = list(self.notes.values())
        if self.zone_note:
            notes.insert(0, self.zone_note)
        if len(same) < MIN_USUAL:
            notes.append(f"Usual for {TOP_STATUS} actions: {len(same)} of the {MIN_USUAL} {kind} needed are saved "
                         "so far (Aternity keeps activity only about 8 days, so the counts are saved each day).")
        if any(h.partial for h in hours):
            notes.append("Aternity's reply for some hours of activity was cut short: some actions may be missing.")
        return {"ready": True, "as_of": (self.as_of or now).isoformat().replace("+00:00", "Z"),
                "as_of_local": self.local(self.as_of or now).strftime("%H:%M"),
                "zone": self.zone_label, "date": t.isoformat(), "kind": kind, "slow": slow, "tiles": tiles,
                "none": none, "hours": per_hour, "applications": app_rows, "events": events,
                "health_days": len(before), "notes": notes, "refresh_minutes": REFRESH_MIN,
                # active: today so far, and the last full hour with this one so far
                "active": {"today": self.active(hours),
                           "recent": dict(self.active([h for h in hours if h.start >= cut - HOUR]),
                                          since=self.local(cut - HOUR).strftime("%H:%M"))}}

    @staticmethod
    def active(hours: list) -> dict:
        """Users and PCs with at least one action Aternity measured in these hours (any rating),
        each once; PCs by Aternity's own virtual / physical mark, users by the PCs they used."""
        kind: dict = {}
        users: dict = {}
        devs: set = set()
        for h in hours:
            devs |= h.devs
            kind.update(h.dev_kind)
            for u, ds in h.user_devs.items():
                users.setdefault(u, set()).update(ds)
        of = lambda k: sum(1 for d in devs if kind.get(d, "not known") == k)  # noqa: E731
        on = {u: {kind.get(d, "not known") for d in ds} for u, ds in users.items()}
        return {"users": len(users), "pcs": len(devs), "virtual": of("virtual"), "physical": of("physical"),
                "not_known": of("not known"),
                "users_virtual": sum(1 for k in on.values() if "virtual" in k),
                "users_physical": sum(1 for k in on.values() if "physical" in k),
                "users_both": sum(1 for k in on.values() if {"virtual", "physical"} <= k)}

    def who(self, now: datetime, kind: str, name: str = "", category: str = "", kinds: set | None = None,
            apps: set | None = None) -> dict:
        """The users and PCs behind a tile or a row (kind: "slow", "app" or "event"), with
        the time span to investigate. Most first, WHO at most."""
        t = self.day(now)
        with self.lock:
            if kind == "event":
                want = kinds or {(category, name)}  # the chat: several kinds, some applications only
                rows = [(x[0], x[1], x[4], x[2][1]) for x in self.health
                        if self.day(x[0]) == t and x[2] in want and (apps is None or x[5] in apps)]
            else:
                rows = [r for h in self.hours.values() if h.day == t for r in h.top_rows
                        if kind == "slow" or r[3] == name]
        groups: dict = {}
        for r in rows:
            groups.setdefault((r[2] or None, r[1]), []).append(r)
        pad = EVENT_PAD if kind == "event" else SLOW_PAD
        out = []
        for (user, pc), rs in groups.items():
            rs.sort(key=lambda r: r[0])
            first, last = rs[0][0], rs[-1][0]
            row = {"username": user, "device": pc, "count": len(rs),
                   "first": self.local(first).strftime("%H:%M"), "last": self.local(last).strftime("%H:%M"),
                   "start": (first - pad).isoformat().replace("+00:00", "Z"),
                   "end": min(last + pad, now).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
            if kind == "event":
                row["events"] = sorted({r[3] for r in rs})
            else:
                worst = max(rs, key=lambda r: r[5] if r[5] is not None else -1)
                row.update(application=worst[3], activity=worst[4], seconds=worst[5])
            out.append(row)
        out.sort(key=lambda r: (-r["count"], -(r.get("seconds") or 0), r["last"]))
        named = sorted(k[1] for k in kinds) if kinds else []
        event = (f"{name} ({category})" if not named else named[0] if len(named) == 1 else
                 f"{len(named)} kinds of event") + (f", of {', '.join(sorted(apps)[:3])}" if apps else "")
        what = (event if kind == "event" else
                f"{TOP_STATUS} slow actions" + (f" in {name}" if kind == "app" else ""))
        return {"kind": kind, "what": what, "kinds": named, "application": name if kind == "app" and name != NO_APP else None,
                "total": len(out), "pcs": len({r["device"] for r in out}),
                "users": len({r["username"] for r in out if r["username"]}), "rows": out[:WHO],
                "zone": self.zone_label}


# -- the background reader ---------------------------------------------------
def _read_hour(today: Today, names, client, start: datetime, now: datetime) -> None:
    entity, f = client.cfg.entities["activities"], client.cfg.fields["activities"]
    ts = f["timestamp"]
    mark = len(client.warnings)
    try:
        rows = client.query(entity, f"{ts} ge {odata_time(start)} and {ts} lt {odata_time(start + HOUR)}",
                            select=[f[k] for k in ("timestamp", "username", "device", "application", "status",
                                                   "activity", "response_time", "virtualization") if f.get(k)])
    except Exception as exc:  # Aternity down: the next read tries again
        today.note("activity", f"Aternity activity could not be read ({short_error(exc)}); trying again in "
                               f"{REFRESH_MIN} min.")
        return
    today.notes.pop("activity", None)
    partial = len(client.warnings) > mark
    if names is not None:
        names.add(rows, f, start, min(start + HOUR, now), partial)
    span = f"{today.local(start):%b %d %H:%M}-{today.local(start + HOUR):%H:%M}"
    if today.day(start) != today.day(now) and start not in today.hours:
        log.info("Today: read %d activity rows for %s (for the name list)", len(rows), span)
        return
    before = today.add_hour(start, rows, f, now, partial)
    late = f"; {len(rows) - before} more than at the last read (rows Aternity sent late)" \
        if before is not None and now >= start + HOUR and len(rows) > before else ""
    log.info("Today: read %d activity rows for %s%s%s", len(rows), span, late,
             " (Aternity's reply was cut short)" if partial else "")


def _read_health(today: Today, client, now: datetime) -> None:
    entity, f = client.cfg.entities["health_events"], client.cfg.fields["health_events"]
    ts, cols = f["timestamp"], [f[k] for k in ("timestamp", "device", "category", "name", "severity", "username",
                                               "application") if f.get(k)]
    t = today.day(now)
    spans = []
    if today.health_for != t:  # the days before, once a day
        spans = [(t - timedelta(days=n), False) for n in range(HEALTH_DAYS, 0, -1)]
    spans.append((t, True))
    for d, is_today in spans:
        start = today.midnight(d)
        end = now if is_today else today.midnight(d + timedelta(days=1))
        mark = len(client.warnings)
        try:
            rows = client.query(entity, f"{ts} ge {odata_time(start)} and {ts} lt {odata_time(end)}", select=cols)
        except Exception as exc:
            today.note("health", f"Aternity health events could not be read ({short_error(exc)}); trying again "
                                 f"in {REFRESH_MIN} min.")
            return
        today.notes.pop("health", None)
        today.set_health(rows, f, is_today, len(client.warnings) > mark, () if is_today else (d,))
        if not is_today:
            log.info("Today: read %d health event rows for %s", len(rows), d.strftime("%b %d"))
    if spans[0][1] is False:
        today.health_for = t
    log.info("Today: %d health event rows so far today", len(today.health))


def keep_reading(today: Today, names, make_client, sleep, clock=lambda: datetime.now(timezone.utc),
                 care=None, after=None) -> None:
    """Runs in a background thread: the last 24 hours first, then every REFRESH_MIN.
    After each read, the Care list's users are checked against what was read, then
    `after(now)` runs (the assessment, written when a period is due)."""
    try:
        client = make_client()
    except Exception as exc:  # a broken config.yaml: investigations say so themselves
        today.off = f"The settings could not be read ({short_error(exc)})."
        log.warning("Today: %s", today.off)
        return
    if client is None:
        today.off = "Aternity is not set, so there is nothing to show (aternity.base_url in config.yaml)."
        log.info("Today: %s The name boxes take any name typed.", today.off)
        return
    now = clock()
    cur = today.hour_of(now)
    starts = [cur - HOUR * k for k in range(START_HOURS + 1)]  # newest first: the name list fills from now back
    for i, start in enumerate(starts):
        today.status = f"Reading Aternity's activity: {i} of {len(starts)} hours read"
        _read_hour(today, names, client, start, clock())
    today.status = "Reading Aternity's health events"
    _read_health(today, client, clock())
    today.as_of = clock()
    today.ready = True
    log.info("Today ready: %d hours of activity read; %d usernames and %d devices known",
             len(starts), len(names.seen["username"]) if names else 0, len(names.seen["device"]) if names else 0)
    if care is not None:
        care.check(client, clock())
    if after is not None:
        after(clock())
    while True:
        sleep(REFRESH_MIN * 60)
        try:
            client = make_client() or client  # a new client: its list of cut-short reads starts empty
        except Exception:
            pass
        now = clock()
        for start in today.hours_due(now):
            _read_hour(today, names, client, start, now)
        today.finish(now)
        _read_health(today, client, now)
        today.as_of = now
        if care is not None:
            care.check(client, clock())
        if after is not None:
            after(clock())
