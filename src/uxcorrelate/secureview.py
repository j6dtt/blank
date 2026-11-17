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
heard"; the THIN devices are not in AD). Before the records, Aternity is read once a day for each
day not read in full (`backfill`; the user, 2026-11-17: history of people and "first heard and
last heard also for all dates"): its hourly data while it has rows (each device's and person's
first and last hour, the desktops; how long it keeps them is not known), then its daily data
(kept about 90 days on the live tenant, stage Nov 10 2026: the people and desktops, no times).
That is kept apart from the records (`back`), never moving a time SecureView saw itself; a day
shows the records' minute unless Aternity's hour is earlier (later): "08:xx" (`ends`). A day is
<data>/secureview/<date>.json (device names and usernames), KEEP_DAYS.
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
from .config import SECUREVIEW_DAILY, SECUREVIEW_HOURLY
from .http import short_error
from .models import parse_time
from .today import HOUR, LATE, MIN_USUAL, USUAL_DAYS, kind_of, working, zone_of

log = logging.getLogger("uxcorrelate.secureview")

KEEP_DAYS = 400  # days kept on disk (the plan agreed, 2026-11-17)
RECENT_MIN = 30  # "heard in the last 30 min"
DAILY_DAYS = 90  # days of Aternity's daily data asked for (it keeps about 90)
DAILY_ROWS = 50_000  # rows of one day's daily read: more is not read (said); a row per device, person and desktop
HOURLY_SPAN = 6  # hours of Aternity's hourly data in one read (a read cut short: hour by hour)
HOURLY_ROWS = 50_000  # rows of one read of it: more is not read (that day: its daily data)
NO_DEVICE = "no device"  # the daily data's rows name no device people sat at
STATE_V = 2  # the saved state's version: days read before it (devices only) are read again with the people
CHART_DAYS = 90  # History's charts and a device's squares
NEW_DAYS = 30  # "first heard in the last 30 days" and "heard in the last 30 days"
SILENT_DAYS = (30, 60, 90)  # History's lists: silent this many days or more
LOOK_BACK = 28  # days looked through for the usual's days of the same kind
DAILY_AT_H = 3  # local hour the daily read runs each day (a day Aternity has not written yet: the next day)
STATE = "daily.json"
SEARCH_MIN = 2  # characters a search needs
SEARCH_MAX = 20  # people, devices and desktops listed each (more: "type more")


def _new_day() -> dict:
    return {"devices": {}, "final": set(), "cut": set(), "daily": False}


def _new_dev() -> dict:
    return {"first": None, "last": None, "hours": set(), "people": {}, "desktops": set()}


def _to_json(day: dict) -> dict:
    return {"devices": {n: {"first": x["first"], "last": x["last"], "hours": sorted(x["hours"]),
                            "people": x["people"], "desktops": sorted(x["desktops"])}
                        | ({"back": x["back"]} if x.get("back") else {})
                        for n, x in sorted(day["devices"].items())},
            "final": sorted(day["final"]), "cut": sorted(day["cut"]), "daily": day["daily"]}


def _from_json(raw: dict) -> dict:
    day = _new_day()
    for n, x in (raw.get("devices") or {}).items():
        day["devices"][str(n)] = {"first": x.get("first"), "last": x.get("last"), "hours": set(x.get("hours") or ()),
                                  "people": {k: list(v) for k, v in (x.get("people") or {}).items()},
                                  "desktops": set(x.get("desktops") or ())}
        back = x.get("back")
        if isinstance(back, dict):
            day["devices"][str(n)]["back"] = {"by": back.get("by") or "day", "h": back.get("h"),
                                              "people": {k: list(v) for k, v in (back.get("people") or {}).items()}}
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
        self.daily: dict = {"read": set(), "hour": set(), "state": "", "note": "", "at": None}
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

    def hx(self, epoch: float) -> str:
        """An hour known only as the hour (Aternity's hourly data): "08:xx"."""
        return datetime.fromtimestamp(epoch, timezone.utc).astimezone(self.zone).strftime("%H:xx")

    def _hour_of(self, epoch: float) -> int:
        t = datetime.fromtimestamp(epoch, timezone.utc).astimezone(self.zone)
        return int(t.replace(minute=0, second=0, microsecond=0).timestamp())

    def ends(self, own1, own2, h1, h2) -> tuple:
        """A day's first and last as shown, how they are known, and their times (to sort by): SecureView's
        own minute, unless Aternity's hourly data has an earlier first (later last) hour: "08:xx" then
        (by "hour"); neither: None, None (by "day": its daily data, or nothing)."""
        f = l = tf = tl = None
        by = "day"
        if own1 is not None:
            f, l, tf, tl, by = self.hm(own1), self.hm(own2), own1, own2, "minute"
        if h1 is not None and (own1 is None or h1 < self._hour_of(own1)):
            f, tf, by = self.hx(h1), h1, "hour"
        if h2 is not None and (own2 is None or h2 > self._hour_of(own2)):
            l, tl, by = self.hx(h2), h2 + 3599, "hour"
        return f, l, by, tf, tl

    def _times(self, x: dict) -> tuple:
        h = (x.get("back") or {}).get("h") or (None, None)
        return self.ends(x["first"], x["last"], h[0], h[1])

    @staticmethod
    def _keys(x: dict) -> set:
        """A device-day's people (lower case): SecureView's own and Aternity's."""
        return set(x["people"]) | set((x.get("back") or {}).get("people") or {})

    def _people(self, x: dict) -> list[dict]:
        """A device-day's people, each once, the first heard first: {"key", "user", "first_local",
        "last_local", "by", "f", "l"} (f, l: times to sort by, None when not known)."""
        own, back = x["people"], (x.get("back") or {}).get("people") or {}
        out = []
        for k in self._keys(x):
            o, b = own.get(k), back.get(k)
            f, l, by, tf, tl = self.ends(o[1] if o else None, o[2] if o else None, b[1] if b else None,
                                         b[2] if b else None)
            out.append({"key": k, "user": (o or b)[0], "first_local": f, "last_local": l, "by": by, "f": tf, "l": tl})
        out.sort(key=lambda p: (p["f"] is None, p["f"] or 0, p["user"].lower()))
        return out

    @staticmethod
    def _shown(people: list[dict]) -> list[dict]:
        return [{"user": p["user"], "first_local": p["first_local"], "last_local": p["last_local"]} for p in people]

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
        """The days before the records (each of the DAILY_DAYS days before today not read yet and not
        read in full by the Today reader, newest first), from Aternity: its hourly data while it has
        rows (each device's and person's first and last hour, the desktops), then its daily data (the
        people and desktops, no times). Asked for those columns alone, Aternity adds up the rest: a row
        per hour (day), device, person and desktop. A day's daily row is the day its TIMEFRAME starts
        plus 12 hours falls on here (they start at a midnight, whose is not documented); an hourly
        row's hour is its TIMEFRAME (the hour's start, as a daily row's is its day's). What Aternity
        gives is kept apart from the records (`back`): it never moves a time SecureView saw itself. A
        day with no row in either is asked again the next day."""
        if not self.on():
            return
        today = self.day(now)
        todo = [today - timedelta(days=k) for k in range(1, DAILY_DAYS + 1)]
        with self.lock:
            todo = [d for d in todo if d not in self.daily["read"] and not self.complete(d)]
        n_hour = n_day = 0
        said, hour_ok, hour_note, found, misses = None, True, "", False, 0
        for d in todo:
            got = None
            if hour_ok:
                got, why = self._hourly(client, d)
                found = found or got is not None
                if got is None:
                    misses += 1
                    hour_note = why
                    # past what Aternity keeps by the hour, a read that failed, or two days in a row with no
                    # row (one may not be written yet): its daily data from here
                    if found or misses >= 2 or not why.startswith("no row"):
                        hour_ok = False
            by = "hour"
            if got is None:
                by = "day"
                got, why = self._daily(client, d)
                if got is None and why != NO_DEVICE:
                    if why:  # failed, cut short or too big: said, tried again tomorrow
                        said = ("failed", why)
                        break
                    continue  # not written yet, or past what Aternity keeps: asked again tomorrow
            if why == NO_DEVICE:
                said = ("none", "Aternity's daily data names no device people sat at (CLIENT_DEVICE_NAME is empty), "
                                "so History has only its own records.")
                break
            with self.lock:
                day = self.days.setdefault(d, _new_day())
                for name, b in got.items():
                    x = day["devices"].setdefault(name, _new_dev())
                    x["desktops"] |= b.pop("desktops")
                    x["back"] = b
                day["daily"] = True
                self.daily["read"].add(d)
                self.daily["hour"].discard(d)
                if by == "hour":
                    self.daily["hour"].add(d)
                self.dirty.add(d)
            n_hour, n_day = n_hour + (by == "hour"), n_day + (by == "day")
        with self.lock:
            days, hours = sorted(self.daily["read"]), sorted(self.daily["hour"])
            if said:
                self.daily["state"], self.daily["note"] = said
            elif days:
                self.daily["state"] = "ok"
                self.daily["note"] = (f"Days before the records, from Aternity: {len(days)}, back to {days[0]:%a %b %d}. "
                                      + (f"{len(hours)} with each person's first and last hour (its hourly data, back to "
                                         f"{hours[0]:%a %b %d})" if hours else "None with the hour (its hourly data: "
                                         + (hour_note or "no row") + ")")
                                      + (f"; {len(days) - len(hours)} day{'s' if len(days) - len(hours) > 1 else ''} only, "
                                         "with the people and desktops (its daily data)." if len(days) > len(hours) else "."))
            self.daily["at"] = int(now.timestamp())
            self.dirty.add(today)  # the state is saved with the next save
        log.info("SecureView: the days before the records read from Aternity: %d by the hour, %d days only%s",
                 n_hour, n_day, f"; {said[1]}" if said else "")

    def _rows(self, client, src: dict, lo: datetime, hi: datetime, top: int) -> tuple[list[dict] | None, str]:
        """(rows, "") or (None, why): failed, cut short, or `top` rows or more."""
        t_col = src["time"]
        mark = len(client.warnings)
        try:
            rows = client.query(src["entity"], f"{t_col} ge {odata_time(lo)} and {t_col} lt {odata_time(hi)}",
                                select=[src[k] for k in ("time", "client_device", "username", "device")], top=top)
        except Exception as exc:
            return None, f"could not be read ({short_error(exc)})"
        if len(client.warnings) > mark:
            return None, "came back cut short"
        if len(rows) >= top:
            return None, f"has {top:,} rows or more"
        return rows, ""

    def _add(self, got: dict, src: dict, r: dict, h1, h2) -> None:
        """One added-up row into got: device -> {"by", "h", "people", "desktops"} (h1, h2: its hour, or None)."""
        name = r.get(src["client_device"])
        if not self.matches(name):
            return
        b = got.setdefault(str(name), {"by": "hour" if h1 is not None else "day", "h": None, "people": {},
                                       "desktops": set()})
        if h1 is not None:
            b["h"] = [h1, h2] if b["h"] is None else [min(b["h"][0], h1), max(b["h"][1], h2)]
        user = r.get(src["username"])
        if user:
            p = b["people"].setdefault(str(user).lower(), [str(user), h1, h2])
            if h1 is not None:
                p[1], p[2] = min(p[1], h1), max(p[2], h2)
        desk = r.get(src["device"])
        if desk:
            b["desktops"].add(str(desk))

    def _hourly(self, client, d: date) -> tuple[dict | None, str]:
        """Day d from Aternity's hourly data, HOURLY_SPAN hours a read (a read cut short: hour by hour):
        (device -> what it gives, "") ; (None, why) with no row that day, or a read that failed."""
        src, got, seen, named = SECUREVIEW_HOURLY, {}, False, False
        starts = self.hour_starts(d)
        for i in range(0, len(starts), HOURLY_SPAN):
            part = starts[i:i + HOURLY_SPAN]
            spans = [(part[0], part[-1] + HOUR)]
            rows, why = self._rows(client, src, *spans[0], HOURLY_ROWS)
            if rows is None and why == "came back cut short" and len(part) > 1:
                rows = []
                for s in part:  # hour by hour
                    one, why = self._rows(client, src, s, s + HOUR, HOURLY_ROWS)
                    if one is None:
                        break
                    rows += one
                else:
                    why = ""
                if why:
                    rows = None
            if rows is None:
                return None, f"{d:%a %b %d} {why}"
            for r in rows:
                t = parse_time(r.get(src["time"]))
                if t is None or self.day(t) != d:
                    continue
                seen = True
                named = named or bool(r.get(src["client_device"]))
                h = int(t.astimezone(self.zone).replace(minute=0, second=0, microsecond=0).timestamp())
                self._add(got, src, r, h, h)
        if not seen:
            return None, f"no row for {d:%a %b %d}"
        if not named:  # its rows name no device people sat at: the daily data, which does (stage, Nov 17)
            return None, f"names no device people sat at on {d:%a %b %d}"
        return got, ""

    def _daily(self, client, d: date) -> tuple[dict | None, str]:
        """Day d from Aternity's daily data: (device -> what it gives, ""); (None, "") with no row that
        day; (None, why) when the read failed, was cut short or is too big."""
        src = SECUREVIEW_DAILY
        lo, hi = self.midnight(d) - timedelta(hours=12), self.midnight(d + timedelta(days=1)) - timedelta(hours=12)
        rows, why = self._rows(client, src, lo, hi, DAILY_ROWS)
        if rows is None:
            return None, f"Aternity's daily data for {d:%a %b %d} {why}; it is tried again tomorrow."
        mine = [r for r in rows if (t := parse_time(r.get(src["time"]))) is not None
                and self.day(t + timedelta(hours=12)) == d]
        if not mine:
            return None, ""
        if not any(r.get(src["client_device"]) for r in mine):
            return None, NO_DEVICE
        got: dict = {}
        for r in mine:
            self._add(got, src, r, None, None)
        return got, ""

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
                if raw.get("v") == STATE_V:  # else read before the people were asked for: read again
                    self.daily = {"read": {date.fromisoformat(x) for x in raw.get("read") or ()},
                                  "hour": {date.fromisoformat(x) for x in raw.get("hour") or ()},
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
            state = {"v": STATE_V, "read": sorted(d.isoformat() for d in self.daily["read"]),
                     "hour": sorted(d.isoformat() for d in self.daily["hour"]), "state": self.daily["state"],
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
            self.daily["hour"] &= self.daily["read"]
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
                keys = self._keys(x)
                if (td - d).days < NEW_DAYS:
                    people30 |= keys
                if person is None and keys:
                    p = max(self._people(x), key=lambda p: p["l"] or 0)  # the last heard (none known: the first listed)
                    person, person_day, lf, ll = p["user"], d, p["first_local"], p["last_local"]
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
                f, l, by, _, _ = self._times(x)
                out.append({"date": d.isoformat(), "heard": True, "day_only": by == "day", "by": by,
                            "part": by == "minute" and d != td and not self.complete(d),
                            "first_local": f, "last_local": l, "people": self._shown(self._people(x)),
                            "desktops": sorted(x["desktops"])})
        return {"name": name, "first": ds[0].isoformat(), "last": ds[-1].isoformat(), "days": len(ds), "zone": self.zone_label,
                "squares": out}

    def search(self, q: str, now: datetime, sams=()) -> dict:
        """Every day kept of the people, devices and desktops whose name has q (any case), latest first
        (the user, 2026-11-17: "I need to be able to search usage history"). sams: AD account names
        (lower case) found by a real name: those people too. Every day is a row (the user, 2026-11-17:
        "first heard and last heard also for all dates"): times to the minute from SecureView's own
        records, "08:xx" from Aternity's hourly data, none on a day from its daily data. At most
        SEARCH_MAX of each kind."""
        q, sams = (q or "").strip().lower(), {str(x).lower() for x in sams}
        people: dict[str, dict] = {}
        devices: dict[str, dict] = {}
        desks: dict[str, dict] = {}
        with self.lock:
            since = min((d for d, day in self.days.items() if any(x["first"] is not None for x in day["devices"].values())),
                        default=None)
            for d in sorted(self.days, reverse=True):
                day_s = d.isoformat()
                for name, x in self.days[d]["devices"].items():
                    dev = q in name.lower()
                    keys = self._keys(x)
                    hit_p = [k for k in keys if q in k or (sams and _sam(k) in sams)]
                    hit_k = [k for k in x["desktops"] if q in k.lower()]
                    if not (dev or hit_p or hit_k):
                        continue
                    f, l, by, _, _ = self._times(x)
                    ppl = self._people(x)
                    shown = self._shown(ppl)
                    row = {"date": day_s, "day_only": by == "day", "by": by, "first_local": f, "last_local": l}
                    if dev:
                        m = devices.setdefault(name, {"name": name, "rows": [], "people": set()})
                        m["rows"].append(row | {"people": shown, "desktops": sorted(x["desktops"])})
                        m["people"].update(keys)
                    for p in ppl:
                        if p["key"] not in hit_p:
                            continue
                        m = people.setdefault(p["key"], {"name": p["user"], "rows": [], "devices": set()})
                        m["rows"].append({"date": day_s, "device": name, "first_local": p["first_local"],
                                          "last_local": p["last_local"], "by": p["by"],
                                          "day_only": p["by"] == "day", "desktops": sorted(x["desktops"]),
                                          "others": len(keys) - 1})
                        m["devices"].add(name)
                    for desk in hit_k:
                        m = desks.setdefault(desk, {"name": desk, "rows": [], "devices": set(), "people": set()})
                        m["rows"].append(row | {"device": name, "people": shown})
                        m["devices"].add(name)
                        m["people"].update(keys)

        def listed(found: dict, extra) -> tuple[list[dict], int]:
            out = []
            for m in found.values():
                m["rows"].sort(key=lambda r: r.get("device") or "")
                m["rows"].sort(key=lambda r: r["date"], reverse=True)  # latest day first, devices by name in a day
                dates = sorted({r["date"] for r in m["rows"]})
                out.append({"name": m["name"], "days": len(dates), "first": dates[0], "last": dates[-1],
                            **extra(m), "rows": m["rows"]})
            out.sort(key=lambda m: (m["name"].lower() != q, -date.fromisoformat(m["last"]).toordinal(), m["name"].lower()))
            return out[:SEARCH_MAX], max(0, len(out) - SEARCH_MAX)
        ps, pmore = listed(people, lambda m: {"devices": len(m["devices"])})
        ds, dmore = listed(devices, lambda m: {"people": len(m["people"])})
        ks, kmore = listed(desks, lambda m: {"devices": len(m["devices"]), "people": len(m["people"])})
        return {"q": q, "people": ps, "devices": ds, "desktops": ks, "more": {"people": pmore, "devices": dmore,
                "desktops": kmore}, "max": SEARCH_MAX, "records_since": since.isoformat() if since else None,
                "date": self.day(now).isoformat(), "zone": self.zone_label, "keep_days": KEEP_DAYS}


def _sam(user: str) -> str:
    """The account part of a username as Aternity writes it (DOMAIN\\name, name@domain, name), lower case:
    what AD's sAMAccountName is compared with."""
    return str(user).split("\\")[-1].split("@")[0].lower()


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
