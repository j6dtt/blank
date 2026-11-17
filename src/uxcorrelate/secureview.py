"""SecureView on the admin page: the devices people sit at whose name matches config.yaml's
secureview.match (the user's THIN devices for people working outside the office, 2026-11-17;
docs/admin-roadmap.md). Today: which were heard, first and last, by whom. History: every device
heard since the records began, and those silent 30, 60, 90 days or more.

How it works: the Today reader reads every activity row each hour (today.py). With SecureView
on, it also reads CLIENT_DEVICE_NAME (documented by Riverbed: the device connecting to the
virtual desktop), and every row whose device matches widens that device's day (`add_hour`):
first and last time heard, the local hours heard, the people (each with their own first and
last) and the desktops. A second read of an hour, or rows Aternity sends late, only widen a
day, never narrow it. Aternity has no connect or disconnect record: "heard" is a row of a
virtual desktop opened from the device, and a session nobody uses sends nothing.

Only devices Aternity has heard from are known (the user: "build history based on devices it's
heard"; the THIN devices are not in AD). Before the records, Aternity's daily data (kept about
90 days on the live tenant, stage Nov 10 2026) is read once a day for each day not read in
full, the device people sat at alone: which devices were heard that day, no times, no people
(`backfill`). A day is <data>/secureview/<date>.json (device names and usernames), KEEP_DAYS.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .aternity import odata_time
from .config import SECUREVIEW_DAILY
from .http import short_error
from .models import parse_time
from .today import HOUR, LATE, MIN_USUAL, USUAL_DAYS, kind_of, working, zone_of

log = logging.getLogger("uxcorrelate.secureview")

KEEP_DAYS = 400  # days kept on disk (the plan agreed, 2026-11-17)
RECENT_MIN = 30  # "heard in the last 30 min"
DAILY_DAYS = 90  # days of Aternity's daily data asked for (it keeps about 90)
DAILY_ROWS = 20_000  # rows of one day's daily read: more is not read (said), as a day has ~1,200 devices
CHART_DAYS = 90  # History's charts and a device's squares
NEW_DAYS = 30  # "first heard in the last 30 days" and "heard in the last 30 days"
SILENT_DAYS = (30, 60, 90)  # History's lists: silent this many days or more
LOOK_BACK = 28  # days looked through for the usual's days of the same kind
DAILY_AT_H = 3  # local hour the daily read runs each day (a day Aternity has not written yet: the next day)
STATE = "daily.json"


def _new_day() -> dict:
    return {"devices": {}, "final": set(), "cut": set(), "daily": False}


def _new_dev() -> dict:
    return {"first": None, "last": None, "hours": set(), "people": {}, "desktops": set()}


def _to_json(day: dict) -> dict:
    return {"devices": {n: {"first": x["first"], "last": x["last"], "hours": sorted(x["hours"]),
                            "people": x["people"], "desktops": sorted(x["desktops"])}
                        for n, x in sorted(day["devices"].items())},
            "final": sorted(day["final"]), "cut": sorted(day["cut"]), "daily": day["daily"]}


def _from_json(raw: dict) -> dict:
    day = _new_day()
    for n, x in (raw.get("devices") or {}).items():
        day["devices"][str(n)] = {"first": x.get("first"), "last": x.get("last"), "hours": set(x.get("hours") or ()),
                                  "people": {k: list(v) for k, v in (x.get("people") or {}).items()},
                                  "desktops": set(x.get("desktops") or ())}
    day["final"], day["cut"], day["daily"] = set(raw.get("final") or ()), set(raw.get("cut") or ()), \
        bool(raw.get("daily"))
    return day


class SecureView:
    def __init__(self, match: str = "", zone: str = "", store: str | os.PathLike | None = None):
        self.zone, self.zone_label, _ = zone_of(zone)
        self.match, self.rx, self.error = (match or "").strip(), None, ""
        if self.match:
            try:
                self.rx = re.compile(self.match, re.IGNORECASE)
            except re.error as exc:
                self.error = (f"secureview.match in config.yaml is not a regular expression this app can read "
                              f"({exc}), so SecureView is off.")
                log.warning("SecureView: %s", self.error)
        self.store = Path(store) / "secureview" if store else None
        self.days: dict[date, dict] = {}
        self.dirty: set[date] = set()
        # what the read of Aternity's daily data found: days read, "ok" / "none" / "failed", in words
        self.daily: dict = {"read": set(), "state": "", "note": "", "at": None}
        self.notes: dict[str, str] = {}
        self.pruned: date | None = None
        self.started = False  # the daily read's thread (one per process)
        self.lock = threading.Lock()
        if self.on():
            self.load()

    def on(self) -> bool:
        return self.rx is not None

    def matches(self, name) -> bool:
        return bool(name) and self.rx is not None and bool(self.rx.search(str(name)))

    # -- local days ----------------------------------------------------------------------
    def day(self, t: datetime) -> date:
        return t.astimezone(self.zone).date()

    def midnight(self, d: date) -> datetime:
        return datetime(d.year, d.month, d.day, tzinfo=self.zone).astimezone(timezone.utc)

    def hour_starts(self, d: date) -> list[datetime]:
        a, b = self.midnight(d), self.midnight(d + timedelta(days=1))
        return [a + HOUR * k for k in range(round((b - a) / HOUR))]

    def clock(self, epoch: float) -> float:
        """Seconds from local midnight, by the local clock (as Today.clock_secs)."""
        t = datetime.fromtimestamp(epoch, timezone.utc).astimezone(self.zone)
        return t.hour * 3600 + t.minute * 60 + t.second

    def hm(self, epoch: float | None) -> str | None:
        return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).astimezone(self.zone).strftime(
            "%H:%M")

    def complete(self, d: date) -> bool:
        """Every hour of the day read in full after it ended (the usual needs whole days)."""
        day = self.days.get(d)
        return bool(day) and not day["cut"] and all(int(s.timestamp()) in day["final"] for s in self.hour_starts(d))

    # -- the Today reader's rows ---------------------------------------------------------
    def add_hour(self, start: datetime, rows: list[dict], f: dict, now: datetime, partial: bool = False) -> None:
        """One hour of activity rows (any day): the matching devices' days widen."""
        cdev, ts, uf, df = f.get("client_device"), f["timestamp"], f.get("username"), f.get("device")
        if not cdev or not self.on():
            return
        key = int(start.timestamp())
        with self.lock:
            d0 = self.day(start)
            day0 = self.days.setdefault(d0, _new_day())
            if partial:
                day0["cut"].add(key)
            elif now >= start + HOUR + LATE:  # read again after it ended: the hour is whole
                day0["final"].add(key)
                day0["cut"].discard(key)
            self.dirty.add(d0)
            for r in rows:
                name = r.get(cdev)
                if not self.matches(name):
                    continue
                t = parse_time(r.get(ts)) or start
                d, e = self.day(t), int(t.timestamp())
                x = self.days.setdefault(d, _new_day())["devices"].setdefault(str(name), _new_dev())
                x["first"] = e if x["first"] is None else min(x["first"], e)
                x["last"] = e if x["last"] is None else max(x["last"], e)
                x["hours"].add(t.astimezone(self.zone).hour)
                user = r.get(uf) if uf else None
                if user:
                    p = x["people"].setdefault(str(user).lower(), [str(user), e, e])
                    p[1], p[2] = min(p[1], e), max(p[2], e)
                desk = r.get(df) if df else None
                if desk:
                    x["desktops"].add(str(desk))
                self.dirty.add(d)

    # -- Aternity's daily data, before the records ----------------------------------------
    def backfill(self, client, now: datetime) -> None:
        """Which matching devices Aternity's daily data has for each of the DAILY_DAYS days
        before today not read in full (newest first): days only. A day's row is the day its
        TIMEFRAME starts plus 12 hours falls on here (Aternity's daily rows start at a midnight;
        whose midnight is not documented). Asked for TIMEFRAME and CLIENT_DEVICE_NAME alone,
        Aternity adds up the rest: a row per day and device (stage, Nov 10 2026: one row per PC and
        version). A day Aternity has no row for is asked again the next day."""
        if not self.on():
            return
        src = SECUREVIEW_DAILY
        t_col, c_col = src["time"], src["client_device"]
        today = self.day(now)
        todo = [today - timedelta(days=k) for k in range(1, DAILY_DAYS + 1)]
        with self.lock:
            todo = [d for d in todo if d not in self.daily["read"] and not self.complete(d)]
        read, said = 0, None
        for d in todo:
            lo, hi = self.midnight(d) - timedelta(hours=12), self.midnight(d + timedelta(days=1)) - timedelta(hours=12)
            mark = len(client.warnings)
            try:
                rows = client.query(src["entity"], f"{t_col} ge {odata_time(lo)} and {t_col} lt {odata_time(hi)}",
                                    select=[t_col, c_col], top=DAILY_ROWS)
            except Exception as exc:
                said = ("failed", f"Aternity's daily data could not be read ({short_error(exc)}); it is tried again "
                                  "tomorrow.")
                break
            if len(client.warnings) > mark:
                said = ("failed", f"Aternity's daily data for {d:%a %b %d} came back cut short; it is tried again "
                                  "tomorrow.")
                break
            if len(rows) >= DAILY_ROWS:
                said = ("failed", f"Aternity's daily data for {d:%a %b %d} has {DAILY_ROWS:,} rows or more, more "
                                  "than one day's devices: not read.")
                break
            mine = [r for r in rows if (t := parse_time(r.get(t_col))) is not None
                    and self.day(t + timedelta(hours=12)) == d]
            if not mine:
                continue  # not written yet, or past what Aternity keeps: asked again tomorrow
            names = {str(r.get(c_col)) for r in mine if r.get(c_col)}
            if not names:
                said = ("none", "Aternity's daily data names no device people sat at (CLIENT_DEVICE_NAME is empty), "
                                "so History has only its own records.")
                break
            with self.lock:
                day = self.days.setdefault(d, _new_day())
                for n in sorted(names):
                    if self.matches(n):
                        day["devices"].setdefault(n, _new_dev())
                day["daily"] = True
                self.daily["read"].add(d)
                self.dirty.add(d)
            read += 1
        with self.lock:
            days = sorted(self.daily["read"])
            if said:
                self.daily["state"], self.daily["note"] = said
            elif days:
                self.daily["state"] = "ok"
                self.daily["note"] = (f"Days before the records, from Aternity's daily data (days only: no times, no "
                                      f"people): {len(days)}, back to {days[0]:%a %b %d}.")
            self.daily["at"] = int(now.timestamp())
            self.dirty.add(today)  # the state is saved with the next save
        log.info("SecureView: Aternity's daily data read for %d day(s)%s", read,
                 f"; {said[1]}" if said else "")

    # -- files ---------------------------------------------------------------------------
    def load(self) -> None:
        if not self.store:
            return
        bad = []
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.store.glob("*.json")):
                if path.name == STATE:
                    continue
                try:
                    d = date.fromisoformat(path.stem)
                    self.days[d] = _from_json(json.loads(path.read_text()))
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    bad.append(f"{path.name} ({short_error(exc)})")
            state = self.store / STATE
            if state.exists():
                raw = json.loads(state.read_text())
                self.daily = {"read": {date.fromisoformat(x) for x in raw.get("read") or ()},
                              "state": raw.get("state") or "", "note": raw.get("note") or "", "at": raw.get("at")}
        except (OSError, ValueError) as exc:
            bad.append(f"the folder ({short_error(exc)})")
        if bad:
            self.notes["files"] = (f"Saved days in {self.store} could not be read: {', '.join(bad[:3])}; those days are "
                                   "left out. The folder must be writable by the container (uid 1000).")
        log.info("SecureView: %d saved days loaded", len(self.days))

    def save(self, now: datetime) -> None:
        """The days changed since the last save (after each read of the Today reader)."""
        if not self.on():
            return
        self.prune(now)
        with self.lock:
            todo = {d: _to_json(self.days[d]) for d in self.dirty if d in self.days}
            self.dirty.clear()
            state = {"read": sorted(d.isoformat() for d in self.daily["read"]), "state": self.daily["state"],
                     "note": self.daily["note"], "at": self.daily["at"]}
        if not self.store or not todo:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            for name, data in [(f"{d.isoformat()}.json", x) for d, x in todo.items()] + [(STATE, state)]:
                tmp = self.store / f".{name}.tmp"
                tmp.write_text(json.dumps(data, separators=(",", ":")))
                os.replace(tmp, self.store / name)
            self.notes.pop("files", None)
        except OSError as exc:
            self.notes["files"] = (f"SecureView's days could not be saved in {self.store} ({short_error(exc)}); "
                                   "tried again at the next read. The folder must be writable by the container "
                                   "(uid 1000).")
            with self.lock:
                self.dirty |= set(todo)

    def prune(self, now: datetime) -> None:
        """Days older than KEEP_DAYS go, from memory and disk (once a day)."""
        today = self.day(now)
        if self.pruned == today:
            return
        self.pruned = today
        cut = today - timedelta(days=KEEP_DAYS)
        with self.lock:
            old = [d for d in self.days if d < cut]
            for d in old:
                del self.days[d]
                self.dirty.discard(d)
            self.daily["read"] = {d for d in self.daily["read"] if d >= today - timedelta(days=DAILY_DAYS + 1)}
        if self.store:
            for d in old:
                try:
                    (self.store / f"{d.isoformat()}.json").unlink(missing_ok=True)
                except OSError:
                    pass

    # -- the admin page --------------------------------------------------------------------
    def view(self, now: datetime, as_of: datetime | None = None, ready: bool = True, off: str | None = None) -> dict:
        out = {"on": self.on(), "match": self.match, "error": self.error, "zone": self.zone_label,
               "keep_days": KEEP_DAYS, "recent_min": RECENT_MIN, "new_days": NEW_DAYS,
               "silent_days": list(SILENT_DAYS), "chart_days": CHART_DAYS, "ready": ready, "off": off,
               "as_of": as_of.isoformat() if as_of else None, "as_of_local": self.hm(as_of.timestamp()) if as_of else None,
               "notes": list(self.notes.values())}
        if not self.on():
            return out
        with self.lock:
            out.update(self._view(now))
        return out

    def _heard(self) -> dict:
        """Device -> its heard days, oldest first."""
        per: dict = {}
        for d in sorted(self.days):
            for n in self.days[d]["devices"]:
                per.setdefault(n, []).append(d)
        return per

    def _view(self, now: datetime) -> dict:
        td = self.day(now)
        days = sorted(d for d, x in self.days.items() if x["devices"] or x["final"] or x["daily"])
        start = days[0] if days else td
        heard = self._heard()
        firsts = {n: ds[0] for n, ds in heard.items()}
        new_ok = (td - start).days >= NEW_DAYS  # "first heard" means new only with days before it
        # -- today
        rec = {n: x for n, x in self.days.get(td, _new_day())["devices"].items() if x["first"] is not None}
        devices = []
        for n, x in rec.items():
            if x["first"] is None:
                continue
            people = sorted(x["people"].values(), key=lambda p: p[1])
            devices.append({"name": n, "first": x["first"], "last": x["last"], "first_local": self.hm(x["first"]),
                            "last_local": self.hm(x["last"]), "recent": x["last"] >= now.timestamp() - RECENT_MIN * 60,
                            "people": [{"user": p[0], "first_local": self.hm(p[1]), "last_local": self.hm(p[2])}
                                       for p in people],
                            "desktops": sorted(x["desktops"]), "new": new_ok and firsts.get(n) == td})
        devices.sort(key=lambda x: (x["first"], x["name"]))
        cur = now.astimezone(self.zone).hour
        hours = [{"hour": h, "n": sum(h in x["hours"] for x in rec.values()) if h <= cur else None}
                 for h in range(24)]
        # the usual: the latest USUAL_DAYS days of today's kind read in full (as the Today tab)
        same = [d for d in (td - timedelta(days=k) for k in range(1, LOOK_BACK + 1))
                if working(d) == working(td) and self.complete(d)][:USUAL_DAYS]
        usual = {"days": len(same), "need": MIN_USUAL, "kind": kind_of(td)}
        if len(same) >= MIN_USUAL:
            by_now = [sum(1 for x in self.days[d]["devices"].values()
                          if x["first"] is not None and self.clock(x["first"]) <= self.clock(now.timestamp()))
                      for d in same]
            usual.update({"low": min(by_now), "high": max(by_now)})
            for h in hours:
                v = [sum(h["hour"] in x["hours"] for x in self.days[d]["devices"].values()) for d in same]
                h["low"], h["high"] = min(v), max(v)
        today = {"devices": devices, "hours": hours, "usual": usual,
                 "first_local": devices[0]["first_local"] if devices else None,
                 "recent": sum(x["recent"] for x in devices),
                 "people": len({p["user"].lower() for x in devices for p in x["people"]}),
                 "new": sum(x["new"] for x in devices) if new_ok else None}
        # -- history: every device heard since the records began
        rows = []
        win = td - timedelta(days=CHART_DAYS - 1)
        for n, ds in heard.items():
            last, person, person_day, lf, ll, people30 = ds[-1], None, None, None, None, set()
            for d in reversed(ds):
                x = self.days[d]["devices"][n]
                if (td - d).days < NEW_DAYS:
                    people30 |= set(x["people"])
                if person is None and x["people"]:
                    p = max(x["people"].values(), key=lambda p: p[2])
                    person, person_day, lf, ll = p[0], d, self.hm(p[1]), self.hm(p[2])
                if person is not None and (td - d).days >= NEW_DAYS:
                    break
            rows.append({"name": n, "first": ds[0].isoformat(), "last": last.isoformat(), "silent": (td - last).days,
                         "days": len(ds), "days90": sum(1 for d in ds if d >= win), "people30": len(people30),
                         "person": person, "person_day": person_day.isoformat() if person_day else None,
                         "person_first": lf, "person_last": ll})
        rows.sort(key=lambda x: (-x["silent"], x["name"]))
        per_day = []
        for k in range(CHART_DAYS - 1, -1, -1):
            d = td - timedelta(days=k)
            x = self.days.get(d)
            have = x is not None and (x["devices"] or x["final"] or x["daily"])
            per_day.append({"date": d.isoformat(), "n": len(x["devices"]) if have else None,
                            "part": bool(have) and d != td and not (x["daily"] or self.complete(d)),
                            "working": working(d)})
        full = [p["n"] for p in per_day[:-1] if p["n"] is not None and not p["part"] and p["working"]]
        # silent 30 days or more, day by day: heard on or before the day 30 days back, not since
        ords = {n: [d.toordinal() for d in ds] for n, ds in heard.items()}
        silent_by_day = []
        for p in per_day:
            d = date.fromisoformat(p["date"]).toordinal()
            if d - NEW_DAYS < start.toordinal():
                silent_by_day.append({"date": p["date"], "n": None})
                continue
            v = sum(1 for o in ords.values() if bisect_right(o, d - NEW_DAYS) > 0
                    and bisect_right(o, d) == bisect_right(o, d - NEW_DAYS))
            silent_by_day.append({"date": p["date"], "n": v})
        # days since the records began with nothing read: no hour, no daily data (today is still being read)
        unread = sum(1 for k in range((td - start).days) if not ((x := self.days.get(start + timedelta(days=k)))
                                                                and (x["devices"] or x["final"] or x["daily"])))
        hist = {"start": start.isoformat(), "span_days": (td - start).days + 1, "devices": rows, "unread": unread,
                "heard30": sum(1 for r in rows if r["silent"] < NEW_DAYS),
                "new30": sum(1 for r in rows if (td - date.fromisoformat(r["first"])).days < NEW_DAYS) if new_ok else None,
                "working": {"low": min(full), "high": max(full), "days": len(full)} if full else None,
                "per_day": per_day, "silent_by_day": silent_by_day,
                "daily": {"state": self.daily["state"], "note": self.daily["note"],
                          "days": len(self.daily["read"])}}
        return {"date": td.isoformat(), "today": today, "history": hist}

    def device(self, name: str, now: datetime) -> dict | None:
        """One device's last CHART_DAYS days, oldest first: heard or not, first and last, people."""
        td = self.day(now)
        with self.lock:
            ds = [d for d in sorted(self.days) if name in self.days[d]["devices"]]
            if not ds:
                return None
            out = []
            for k in range(CHART_DAYS - 1, -1, -1):
                d = td - timedelta(days=k)
                x = (self.days.get(d) or _new_day())["devices"].get(name)
                if x is None:
                    out.append({"date": d.isoformat(), "heard": False})
                    continue
                people = sorted(x["people"].values(), key=lambda p: p[1])
                out.append({"date": d.isoformat(), "heard": True, "day_only": x["first"] is None,
                            "part": d != td and not self.complete(d) and x["first"] is not None,
                            "first_local": self.hm(x["first"]), "last_local": self.hm(x["last"]),
                            "people": [{"user": p[0], "first_local": self.hm(p[1]), "last_local": self.hm(p[2])}
                                       for p in people], "desktops": sorted(x["desktops"])})
        return {"name": name, "first": ds[0].isoformat(), "last": ds[-1].isoformat(), "days": len(ds), "zone": self.zone_label,
                "squares": out}


def keep_backfilling(sv: SecureView, today, make_client, sleep, clock=lambda: datetime.now(timezone.utc)) -> None:
    """Runs in a background thread: once the Today reader's first read is done, Aternity's daily
    data for the days before the records, then each day at DAILY_AT_H local (a day not read in
    full while the app was down is filled the same way)."""
    while not today.ready and not today.off:
        sleep(30)
    if today.off:
        return
    while True:
        try:
            client = make_client()
            if client is not None:
                sv.backfill(client, clock())
                sv.save(clock())
        except Exception:
            log.exception("SecureView: the daily read failed with an error the code did not expect; tried again "
                          "tomorrow")
        now = clock()
        nxt = now.astimezone(sv.zone).replace(hour=DAILY_AT_H, minute=0, second=0, microsecond=0)
        if nxt <= now.astimezone(sv.zone):
            nxt = (now.astimezone(sv.zone) + timedelta(days=1)).replace(hour=DAILY_AT_H, minute=0, second=0,
                                                                         microsecond=0)
        sleep(max(60.0, (nxt.astimezone(timezone.utc) - now).total_seconds()))
