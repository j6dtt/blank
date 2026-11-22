"""The "Inventory" tab, sixth (`/api/inventory`, `#inventory`; the user's "build it", Nov 21 2026;
proposal 2 in docs/roadmap.md, built on what the live Aternity fills: verify --inventory --columns).

One background reader (`keep_reading`) reads Aternity's device list every READ_EVERY_MIN (5,211
PCs in 1.4 s on live), one row per PC (its latest), and which PCs ran an application in the last
USE_DAYS (daily app usage, each PC once). All of it is held in memory: no PC, user or serial
number is written to disk. The lists opened by a click and "Find a PC" answer from that read, so
they cost Aternity nothing.

First cut: the tiles, "Needs a look" (`LINES`), last heard from with the agent's status, retired
PCs (the Software tab's count) and PCs nobody uses, Find a PC. Aternity's words are taken as it
sends them (config.INVENTORY_WORDS); every limit is a constant below.

Desktop usage, on the first panel (the user, Nov 22 2026: "Looking for daily/monthly usage",
"Add percentage of usage", "Build it"; design E; built on verify --usage's stage run of Nov 21):
the PCs an application ran on, each finished day and each month, by Aternity's kind of device,
against the PCs of that kind in its device list (`read_usage`, after each read of the list).
What the live Aternity does: a day's rows carry one time, local midnight; it has no row for
today until the day is over; it keeps about 92 days. So the last bar is yesterday, a day is read
again until it is SETTLE_DAYS old and then kept for good, and a month is counted by Aternity
itself in one read (the PC only, a row per PC) while it keeps the month whole, then kept by the
tab. Kept in `<data>/inventory/usage.json`: counts per kind of device, never a PC or user name.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median_high

from .aternity import odata_time
from .config import INVENTORY_COLUMNS, INVENTORY_FILLED, INVENTORY_ROLES, INVENTORY_WORDS, VERSION_SOURCES
from .http import short_error
from .models import parse_time, to_float
from .software import _partial, version_key
from .today import zone_of

log = logging.getLogger("uxcorrelate")

# ---- settings -----------------------------------------------------------------------
READ_EVERY_MIN = 60  # the device list is read this often (the user's choice, Nov 21 2026: hourly)
RETRY_MIN = 15  # ... and this soon again when Aternity did not answer
ROWS = 50_000  # rows of the device list asked for at most (5,211 PCs on live)
SCHEMA_HOURS = 24  # Aternity's list of columns is asked for again after this long
# the limits of "Needs a look" (the user: "you recommendations", Nov 21 2026)
SILENT_DAYS = 8  # not heard from: this many days or more (Aternity's own 'Disconnected' ends at 7)
OLD_DAYS = 31  # the last age group: this many days or more
USE_DAYS = 8  # not used: no application ran in these days (Aternity keeps activity about 8 days)
RESTART_DAYS = 14  # not restarted: the last start-up is more than this many days ago
STABILITY_LOW = 5  # Microsoft's stability index (1 to 10) under this
FREE_LOW_GB = 10  # little free space: Aternity's bands that end at this or under
PCS_SHOWN = 200  # PCs listed by a click
FIND_MIN, FIND_MAX = 2, 20  # Find a PC: characters needed, PCs listed
# Desktop usage (the charts on the first panel)
CHART_DAYS = 30  # the daily chart: this many finished days, yesterday last
CHART_MONTHS = 12  # the monthly chart: this many months at most, this month last
SETTLE_DAYS = 2  # a finished day is read again at each read until it is this many days old (Aternity
#                  adds a day's rows after the day is over; how late its last rows come is not known)
KEEP_DAYS = 400  # a day's saved counts are kept this long (a month's busiest day comes from them)
KEEP_MONTHS = 24  # ... and a month's
LOOK_BACK = 400  # days looked back for the oldest day of daily app usage Aternity keeps (92 on live)
FAILS_STOP = 3  # this many days in a row that cannot be read end a pass: tried again at the next read
USAGE_FILE = "usage.json"  # in <data>/inventory
# -------------------------------------------------------------------------------------

R, W = INVENTORY_ROLES, INVENTORY_WORDS
LAST, STATUS = INVENTORY_COLUMNS["last"], INVENTORY_COLUMNS["status"]
# "Needs a look": key -> the line's name, most PCs first on the page
LINES = {"agent": "On an older agent than most", "bitlocker": "BitLocker off on a physical PC",
         "unused": f"Not used in {USE_DAYS} days", "restart": f"Not restarted in over {RESTART_DAYS} days",
         "silent": f"Not heard from in {SILENT_DAYS} days or more", "stopped": "Agent stopped",
         "stability": "Low stability index", "free": "Little free space on the system disk",
         "spinning": "Still on a spinning hard disk"}
NO_KIND = "kind not given"  # a PC Aternity gives no kind of device for
ONE_KIND = "PCs"  # every PC, when Aternity does not publish the kind of device
ALL = "all"  # the charts' key for every kind together
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB)", re.I)
_OPEN = re.compile(r"\b(?:over|more than|above|greater than)\b|[>+]", re.I)
_GB = {"tb": 1024.0, "gb": 1.0, "mb": 1 / 1024, "kb": 1 / 1024 / 1024}


def band_top_gb(text) -> float | None:
    """The upper end, in GB, of one of Aternity's bands of free space ('10 GB to 20 GB' -> 20,
    'Less than 5 GB' -> 5). None for a band with no upper end ('Over 500 GB') or words that hold
    no size: such a band is never "little free space"."""
    s = str(text or "")
    sizes = [float(n) * _GB[u.lower()] for n, u in _SIZE.findall(s)]
    return None if not sizes or _OPEN.search(s) else max(sizes)


def is_virtual(kind) -> bool:
    """Aternity's kind of device says a virtual desktop ('Virtual Desktop')."""
    return W["virtual"] in str(kind or "").lower()


def local_midnight(d: date, zone) -> datetime:
    """The midnight that starts local day d, in UTC."""
    return datetime(d.year, d.month, d.day, tzinfo=zone).astimezone(timezone.utc)


def daily_span(first: date, after: date, zone) -> tuple[datetime, datetime]:
    """The span to ask Aternity's daily rows of local days `first` up to the day before `after`.
    A daily row carries one time a day, at an hour that is Aternity's own (local midnight on
    live: verify --usage, stage, Nov 21 2026), so a day is asked for from 12 hours before its
    local midnight and a row is of the day its time + 12 hours falls on (`daily_day`; SecureView
    reads the same kind of rows so). verify --usage uses these two as well: one rule."""
    return local_midnight(first, zone) - timedelta(hours=12), local_midnight(after, zone) - timedelta(hours=12)


def daily_day(value, zone) -> date | None:
    t = parse_time(value)
    return (t + timedelta(hours=12)).astimezone(zone).date() if t else None


def _month_before(m: date) -> date:
    return (m - timedelta(days=1)).replace(day=1)


def _month_after(m: date) -> date:
    return (m + timedelta(days=32)).replace(day=1)


class Inventory:
    def __init__(self, zone: str = "", store: str | None = None, clock=lambda: datetime.now(timezone.utc)):
        self.zone, self.zone_label, _ = zone_of(zone)
        self.clock = clock
        self.store = Path(store) / "inventory" if store else None
        self.lock = threading.Lock()
        self.pcs: dict[str, dict] = {}  # the PC's name in small letters -> its latest row (memory only)
        self.used: set[str] | None = None  # PCs that ran an application in USE_DAYS; None: not known
        self.have: set[str] = set()  # the columns of the device list this Aternity publishes
        self.as_of: datetime | None = None
        self.took: float | None = None
        self.notes: list[str] = []
        self.status = "Starting"
        self.off: str | None = None
        self.reading = False
        self.started = False
        self._schema: tuple[datetime, dict | None] | None = None
        self.partial = False  # the device list's reply was cut short: nothing is counted against it
        # Desktop usage: counts only, saved in <data>/inventory/usage.json
        self.days: dict[str, dict] = {}  # local day (ISO) -> {"used": {kind: PCs}, "list": {kind: PCs}, "out": n,
        #                                  "final": bool}, or {"none": True, ...}: Aternity has no rows for it
        self.months: dict[str, dict] = {}  # "2026-10" -> the same for the month, with "through" and "read" (days)
        self.filled: str | None = None  # the local day counts were first saved: earlier days are against its list
        self.oldest: date | None = None  # the oldest day of daily app usage Aternity keeps
        self.oldest_asked: date | None = None  # ... asked on this local day
        self.use_off: str | None = None  # why there is nothing to chart
        self.use_state = ""  # what the reader is doing, during a long first read
        self.use_notes: list[str] = []
        self.use_files: list[str] = []  # what could not be loaded or saved
        self._load_usage()

    # -- the read ---------------------------------------------------------------------
    def schema(self, client, now: datetime) -> dict | None:
        """Aternity's own list of columns (asked again after SCHEMA_HOURS); None: could not be read."""
        if self._schema and now - self._schema[0] < timedelta(hours=SCHEMA_HOURS) and self._schema[1] is not None:
            return self._schema[1]
        try:
            from .verify import parse_odata_metadata
            text = client.http.request("GET", "/$metadata", headers={"Accept": "application/xml"}).text
            self._schema = (now, parse_odata_metadata(text))
        except Exception as exc:
            log.warning("Inventory: Aternity's list of columns could not be read (%s)", short_error(exc))
            self._schema = (now, None)
        return self._schema[1]

    def read(self, client, cfg, now: datetime) -> None:
        """One read. An error of the device list is raised (the last read stays on the tab); app
        usage that cannot be read whole only leaves "used or not" unknown."""
        inv, dev = cfg.entities.get("inventory"), cfg.fields.get("inventory", {}).get("device")
        if not inv or not dev:
            raise ValueError("no device list is set (aternity.entities.inventory in config.yaml)")
        schema, notes = self.schema(client, now), []
        if schema is None:  # which columns to ask for is not known: asking for one it lacks is refused whole
            raise ValueError("Aternity's list of columns could not be read")
        if dev not in schema.get(inv, ()):
            raise ValueError(f"this Aternity does not publish its device list ({inv})")
        want = list(dict.fromkeys([LAST, STATUS] + [c for _, items in INVENTORY_FILLED for _, c, _ in items]))
        have = [c for c in want if c in schema[inv]]
        mark, t0 = len(client.warnings), time.monotonic()
        rows = client.query(inv, select=[dev] + have, top=ROWS)
        partial = _partial(client, mark, rows, ROWS)
        if partial:
            notes.append(f"Aternity's reply for the device list was cut short ({len(rows):,} rows): these counts are "
                         "partial.")
        pcs: dict[str, dict] = {}
        for r in rows:
            name = str(r.get(dev) or "").strip()
            if not name:
                continue
            t, seen = parse_time(r.get(LAST)) if LAST in have else None, pcs.get(name.lower())
            if seen is None or (t and (seen["heard"] is None or t > seen["heard"])):  # a PC listed twice: its latest
                pcs[name.lower()] = {"name": name, "heard": t, "row": r}
        used = self._used(client, schema, now, notes)
        with self.lock:
            self.pcs, self.used, self.have, self.as_of, self.notes = pcs, used, set(have), now, notes
            self.took, self.partial = time.monotonic() - t0, partial

    def _used(self, client, schema: dict, now: datetime, notes: list[str]) -> set[str] | None:
        """The PCs that ran an application in the last USE_DAYS (daily app usage, each PC once,
        added up by Aternity); None when it cannot be said: not published, failed or cut short."""
        u = VERSION_SOURCES["usage"]
        if not {u["device"], u["time"]} <= schema.get(u["entity"], set()):
            notes.append(f"This Aternity does not publish daily app usage ({u['entity']}): whether a PC is in use is "
                         "not known.")
            return None
        mark = len(client.warnings)
        try:
            got = client.query(u["entity"], f"{u['time']} ge {odata_time(now - timedelta(days=USE_DAYS))}",
                               select=[u["device"]], top=ROWS)
        except Exception as exc:
            notes.append(f"App usage could not be read ({short_error(exc)}): whether a PC is in use is not known.")
            return None
        if _partial(client, mark, got, ROWS):
            notes.append("Aternity's reply for app usage was cut short: whether a PC is in use is not known.")
            return None
        return {str(r.get(u["device"])).strip().lower() for r in got if r.get(u["device"])}

    # -- Desktop usage: the PCs in use each finished day and month, against the list ---------
    def _kind(self, pc: dict) -> str:
        """Aternity's kind of device of a PC, as it sends it."""
        return str(pc["row"].get(R["kind"]) or NO_KIND) if R["kind"] in self.have else ONE_KIND

    def _load_usage(self) -> None:
        """The saved counts. A file that cannot be read is set aside, not written over: the days
        Aternity still keeps are read again, the months before them are lost."""
        if not self.store:
            return
        path = self.store / USAGE_FILE
        try:
            if not path.exists():
                return
            got = json.loads(path.read_text())
            days = {k: v for k, v in got["days"].items() if date.fromisoformat(k) and isinstance(v, dict)}
            months = {k: v for k, v in got["months"].items() if date.fromisoformat(k + "-01") and isinstance(v, dict)}
            self.days, self.months, self.filled = days, months, got.get("filled")
            self.oldest = date.fromisoformat(got["oldest"]) if got.get("oldest") else None  # asked again at the first read
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            said = (f"The saved usage counts ({path}) could not be read ({short_error(exc)}): the days Aternity "
                    "still keeps are read again, the months before them are lost.")
            try:
                os.replace(path, path.with_suffix(".unreadable"))
                said += f" The file is kept as {path.with_suffix('.unreadable').name}."
            except OSError:
                pass
            self.use_files = [said]
            log.warning("Inventory: %s", said)

    def _save_usage(self) -> None:
        if not self.store:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            tmp = self.store / ".usage.tmp"
            tmp.write_text(json.dumps({"v": 1, "filled": self.filled, "days": self.days, "months": self.months,
                                       "oldest": self.oldest.isoformat() if self.oldest else None}, sort_keys=True))
            os.replace(tmp, self.store / USAGE_FILE)
            self.use_files = []
        except OSError as exc:
            said = (f"The usage counts could not be saved in {self.store} ({short_error(exc)}). The folder must be "
                    "writable by the container (uid 1000). Until then a restart reads the days Aternity keeps again "
                    "and loses the months before them.")
            if self.use_files != [said]:
                log.warning("Inventory: %s", said)
            self.use_files = [said]

    def read_usage(self, client, now: datetime) -> None:
        """The usage charts' counts, after a read of the device list. It never raises: what could
        not be read is a note on the tab, the counts held stay, and it is tried at the next read."""
        self.use_state = "Reading the PCs in use"  # a day not counted yet is "not read yet" meanwhile, not "could not"
        try:
            self._read_usage(client, now)
        except Exception as exc:
            log.warning("Inventory: the usage counts could not be read (%s)", short_error(exc))
            self.use_notes = [f"The PCs in use could not be read ({short_error(exc)}); tried again at the next read."]
        finally:
            self.use_state = ""

    def _names(self, client, first: date, after: date, said: str, notes: list[str]) -> set[str] | None:
        """The PCs with daily app usage on local days `first` up to the day before `after`, names
        in small letters. None: the read failed or was cut short, said in a note. Both reads are
        made the way verify --usage made them on stage (Nov 21 2026), nothing else is assumed of
        Aternity: one day is asked for with each row's time, and a row counts only when its own
        time is of that day (`daily_day`); a month is asked for with the PC only, so Aternity
        adds its days up (a row per PC on stage)."""
        u = VERSION_SOURCES["usage"]
        D, T = u["device"], u["time"]
        one_day = after - first == timedelta(days=1)
        lo, hi = daily_span(first, after, self.zone)
        mark = len(client.warnings)
        try:
            rows = client.query(u["entity"], f"{T} ge {odata_time(lo)} and {T} lt {odata_time(hi)}",
                                select=[D, T] if one_day else [D], top=ROWS)
        except Exception as exc:
            notes.append(f"The app usage of {said} could not be read ({short_error(exc)}): tried again at the next read.")
            return None
        if _partial(client, mark, rows, ROWS):
            notes.append(f"Aternity's reply for the app usage of {said} was cut short: not counted, tried again at the "
                         "next read.")
            return None
        return {str(r.get(D)).strip().lower() for r in rows
                if r.get(D) and (not one_day or daily_day(r.get(T), self.zone) == first)}

    @staticmethod
    def _counted(names: set[str], kinds: dict[str, str], sizes: dict[str, int]) -> dict:
        """PCs in use per kind of device, of those in the device list; the others are only counted
        (`out`). No row at all is "none": Aternity has nothing for the day, which is not 0 in use."""
        if not names:
            return {"none": True}
        return {"used": dict(Counter(kinds[n] for n in names if n in kinds)), "list": dict(sizes),
                "out": sum(1 for n in names if n not in kinds)}

    def _read_usage(self, client, now: datetime) -> None:
        u = VERSION_SOURCES["usage"]
        E, T = u["entity"], u["time"]
        schema, notes = self.schema(client, now), []
        with self.lock:
            kinds = {name: self._kind(pc) for name, pc in self.pcs.items()}
            partial = self.partial
        if schema is None:
            self.use_notes = ["Aternity's list of columns could not be read: the PCs in use were not counted at this read."]
            return
        if not {u["device"], T} <= schema.get(E, set()):
            self.use_off = f"This Aternity does not publish daily app usage ({E}): there is nothing to chart."
            return
        self.use_off = None
        if partial or not kinds:
            self.use_notes = ["Aternity's device list was not read whole, so the PCs in use were not counted against it "
                              "at this read."]
            return
        sizes, today, moved_back = dict(Counter(kinds.values())), now.astimezone(self.zone).date(), False
        if self.oldest_asked != today:  # how far back Aternity keeps daily app usage: asked once a day
            try:
                got = client.query(E, f"{T} ge {odata_time(now - timedelta(days=LOOK_BACK))}", select=[T],
                                   orderby=f"{T} asc", top=1)
                first = daily_day(got[0].get(T), self.zone) if got else None
                if first:
                    moved_back = first != self.oldest  # kept with the counts, so a restart knows it at once
                    self.oldest, self.oldest_asked = first, today
                else:
                    notes.append("Aternity gave no row of daily app usage: how far back it keeps it is not known.")
            except Exception as exc:
                notes.append(f"How far back Aternity keeps daily app usage could not be read ({short_error(exc)}).")
        oldest = self.oldest
        back = max(oldest, today - timedelta(days=KEEP_DAYS)) if oldest else today - timedelta(days=CHART_DAYS)
        want = [d for d in (today - timedelta(days=k) for k in range(1, (today - back).days + 1))
                if not (self.days.get(d.isoformat()) or {}).get("final")]  # yesterday first: today is not over
        moved: set[str] = set()  # months with a day whose PCs in use changed at this read
        dirty, fails = moved_back, 0
        for i, d in enumerate(want):
            if len(want) > SETTLE_DAYS + 1:
                self.use_state = f"Reading the days Aternity keeps: {i + 1:,} of {len(want):,}"
            names = self._names(client, d, d + timedelta(days=1), f"{d:%a %b %d}", notes)
            if names is None:
                fails += 1
                if fails >= FAILS_STOP:
                    left = len(want) - i - 1
                    if left:
                        notes.append(f"{left:,} more day{'s were' if left != 1 else ' was'} not tried at this read.")
                    break
                continue
            fails = 0
            rec, key = self._counted(names, kinds, sizes), d.isoformat()
            rec["final"] = (today - d).days > SETTLE_DAYS
            old = self.days.get(key)
            if old != rec:
                if old is None or old.get("used") != rec.get("used"):
                    moved.add(key[:7])
                with self.lock:
                    self.days[key] = rec
                dirty = True
        # a month: Aternity adds its days up itself, while it keeps the month whole
        if oldest and self.oldest_asked == today:
            m = today.replace(day=1)
            for _ in range(CHART_MONTHS):
                key, after = f"{m:%Y-%m}", _month_after(m)
                rec, end = self.months.get(key), min(after, today)  # its days that are over
                if not (rec and rec.get("final")):
                    if m < oldest:
                        break  # Aternity no longer has its first days: this month and those before cannot be counted
                    if end > m and (rec is None or key in moved or rec.get("read") != today.isoformat()):
                        names = self._names(client, m, end, f"{m:%B}", notes)
                        if names is not None:
                            new = self._counted(names, kinds, sizes)
                            have = [k for k, r in self.days.items() if k.startswith(key) and "used" in r]
                            new.update(read=today.isoformat(),  # through: its last day Aternity has rows for
                                       through=max(have, default=(end - timedelta(days=1)).isoformat()),
                                       final=after <= today and (today - after).days >= SETTLE_DAYS)
                            with self.lock:
                                self.months[key] = new
                            dirty = True
                m = _month_before(m)
        if dirty:
            with self.lock:
                self.filled = self.filled or today.isoformat()
                cut_day = (today - timedelta(days=KEEP_DAYS)).isoformat()
                cut_month = today.replace(day=1)
                for _ in range(KEEP_MONTHS):
                    cut_month = _month_before(cut_month)
                self.days = {k: v for k, v in self.days.items() if k >= cut_day}
                self.months = {k: v for k, v in self.months.items() if k >= f"{cut_month:%Y-%m}"}
            self._save_usage()
        self.use_notes = notes

    def _usage_view(self, now: datetime) -> dict:
        """What the page draws (called with the lock held): per kind of device and for all, the
        finished days and the months with their counts, and the four figures."""
        today = now.astimezone(self.zone).date()
        sizes = Counter(self._kind(pc) for pc in self.pcs.values())
        order = [k for k, _ in sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))]
        keys = [ALL] + (order if len(order) > 1 else [])
        count = lambda rec, part, k: sum(rec[part].values()) if k == ALL else rec[part].get(k, 0)  # noqa: E731
        cell = lambda rec, k: {"n": count(rec, "used", k), "of": count(rec, "list", k)}  # noqa: E731
        days = []
        for ago in range(CHART_DAYS, 0, -1):
            d = today - timedelta(days=ago)
            rec, row = self.days.get(d.isoformat()), {"day": d.isoformat(), "weekend": d.weekday() >= 5}
            if rec and "used" in rec:
                row.update(by={k: cell(rec, k) for k in keys}, out=rec.get("out", 0))
            elif rec:
                row["why"] = ("Aternity has no rows for this day yet." if ago <= SETTLE_DAYS else
                              "Aternity has no rows for this day.")
            elif self.oldest and d < self.oldest:
                row["why"] = "Aternity no longer keeps this day."
            else:
                row["why"] = "Not read yet." if self.use_state else "Could not be read; tried again at the next read."
            days.append(row)
        months, m = [], today.replace(day=1)
        for _ in range(CHART_MONTHS):
            key = f"{m:%Y-%m}"
            rec = self.months.get(key)
            if rec and "used" in rec:
                own = [(k2, r) for k2, r in self.days.items() if k2.startswith(key) and "used" in r]
                best = {}
                for k in keys if own else ():
                    day, r = max(own, key=lambda kr: (count(kr[1], "used", k), kr[0]))
                    best[k] = {"n": count(r, "used", k), "day": day}
                months.append({"month": key, "so_far": m == today.replace(day=1), "through": rec.get("through"),
                               "by": {k: cell(rec, k) for k in keys}, "out": rec.get("out", 0), "busiest": best})
            m = _month_before(m)
        months.reverse()
        whole, figures = [r for r in days if "by" in r], {}
        for k in keys:
            f: dict = {}
            for name, rows in (("work", [r for r in whole if not r["weekend"]]), ("weekend", [r for r in whole if r["weekend"]])):
                if rows:  # typical = the middle value, the higher one of two: a day that was counted
                    mid = median_high([r["by"][k]["n"] for r in rows])
                    f[name] = dict([r for r in rows if r["by"][k]["n"] == mid][-1]["by"][k], days=len(rows))
            if whole:
                top = max(whole, key=lambda r: (r["by"][k]["n"], r["day"]))
                f["busiest"] = dict(top["by"][k], day=top["day"])
            if months and months[-1]["so_far"]:
                f["month"] = dict(months[-1]["by"][k], month=months[-1]["month"], through=months[-1]["through"])
            figures[k] = f
        return {"off": self.use_off, "state": self.use_state, "notes": self.use_notes + self.use_files,
                "kinds": [{"key": k, "n": sum(sizes.values()) if k == ALL else sizes[k]} for k in keys],
                "open": next((k for k in keys[1:] if is_virtual(k)), ALL),
                "days": days, "months": months, "figures": figures,
                "oldest": self.oldest.isoformat() if self.oldest else None, "filled": self.filled,
                "chart_days": CHART_DAYS, "chart_months": CHART_MONTHS}

    # -- one PC -------------------------------------------------------------------------
    def age(self, t: datetime | None, now: datetime) -> int | None:
        """Whole local days since a time (0 = today); None: no time."""
        return None if t is None else max((now.astimezone(self.zone).date() - t.astimezone(self.zone).date()).days, 0)

    def _brief(self, pc: dict, what: str | None = None) -> dict:
        r = pc["row"]
        out = {"name": pc["name"], "kind": r.get(R["kind"]), "virtual": is_virtual(r.get(R["kind"])),
               "username": r.get(R["user"]), "department": r.get(R["department"]),
               "heard": pc["heard"].isoformat() if pc["heard"] else None}
        return dict(out, what=what) if what is not None else out

    # -- the rules ----------------------------------------------------------------------
    def _judged(self, now: datetime) -> dict:
        """Every "Needs a look" line from the read held: key -> {"pcs": [(PC, its own value in
        words)], "of": how many PCs it is counted among, "text": what it means; "unknown": why
        it cannot be said}. Called under the lock."""
        pcs = list(self.pcs.values())
        val = lambda pc, role: pc["row"].get(R[role]) if R[role] in self.have else None  # noqa: E731
        physical = [pc for pc in pcs if not is_virtual(val(pc, "kind"))]
        out: dict[str, dict] = {}

        def line(key, hits, of, text, unknown=None):
            out[key] = {"pcs": hits, "of": of, "text": text, "unknown": unknown}

        def missing(key, role_or_column, what):
            c = R.get(role_or_column, role_or_column)
            if c in self.have:
                return False
            line(key, [], 0, "", f"this Aternity does not publish {what}")
            return True

        # an older agent than the version most PCs have
        if not missing("agent", "agent", "the agent's version"):
            vs = Counter(str(val(pc, "agent")) for pc in pcs if val(pc, "agent"))
            most = max(vs, key=lambda v: (vs[v], version_key(v) or ())) if vs else None
            mk = version_key(most)
            older = [(pc, str(val(pc, "agent"))) for pc in pcs if val(pc, "agent") and mk is not None
                     and (version_key(val(pc, "agent")) or mk) < mk]
            line("agent", older, sum(vs.values()),
                 f"An Aternity agent older than {most}, the version most PCs have." if most else "")
        # BitLocker off, physical PCs only (a virtual desktop's disk is not the PC's to encrypt)
        if not missing("bitlocker", "bitlocker", "BitLocker's status") and not missing("bitlocker", "kind",
                                                                                     "the kind of device"):
            on = sum(1 for pc in pcs if val(pc, "bitlocker") and val(pc, "bitlocker") not in W["bitlocker_off"])
            line("bitlocker", [(pc, str(val(pc, "bitlocker"))) for pc in physical if val(pc, "bitlocker") in W["bitlocker_off"]],
                 len(physical), f"As the PC reports it to Aternity. {on:,} PCs in all report it on.")
        # in the list and heard from, but no application ran on it
        alive = [pc for pc in pcs if (self.age(pc["heard"], now) or 0) < SILENT_DAYS]
        if self.used is None:
            line("unused", [], 0, "", "whether a PC is in use could not be read (see the notes)")
        else:
            idle = [pc for pc in alive if pc["name"].lower() not in self.used]
            line("unused", [(pc, "virtual desktop" if is_virtual(val(pc, "kind")) else "physical PC") for pc in idle],
                 len(alive), f"In the list and not silent, but no application ran on it in {USE_DAYS} days. "
                 f"{sum(1 for pc in idle if is_virtual(val(pc, 'kind'))):,} of them are virtual desktops.")
        # the last start-up is long ago
        if not missing("restart", "boot", "the last start-up"):
            boots = [(pc, parse_time(val(pc, "boot"))) for pc in pcs]
            timed = [(pc, t) for pc, t in boots if t]
            late = [(pc, t) for pc, t in timed if (self.age(t, now) or 0) > RESTART_DAYS]
            line("restart", [(pc, f"last start-up {self.day(t)}, {self.age(t, now)} days ago") for pc, t in
                             sorted(late, key=lambda x: x[1])], len(timed),
                 "The last start-up Aternity has for the PC."
                 + (f" {len(pcs) - len(timed):,} {'PC has' if len(pcs) - len(timed) == 1 else 'PCs have'} none and "
                    f"{'is' if len(pcs) - len(timed) == 1 else 'are'} not judged." if len(pcs) > len(timed) else ""))
        # not heard from
        if not missing("silent", LAST, "when it last heard from a PC"):
            dated = [pc for pc in pcs if pc["heard"]]
            quiet = sorted((pc for pc in dated if self.age(pc["heard"], now) >= SILENT_DAYS), key=lambda pc: pc["heard"])
            line("silent", [(pc, f"last heard {self.day(pc['heard'])}, {self.age(pc['heard'], now)} days ago") for pc in quiet],
                 len(dated), "Still in Aternity's list. It seems to take a PC off the list about a month after it last "
                 "hears from it.")
        # the agent does not run
        if not missing("stopped", STATUS, "the agent's status"):
            line("stopped", [(pc, str(pc["row"].get(STATUS))) for pc in pcs if pc["row"].get(STATUS) in W["stopped"]],
                 len(pcs), "Aternity's own status of the PC's agent: " + ", ".join(f"'{w}'" for w in W["stopped"])
                 + ". It sends nothing until it runs again.")
        # Microsoft's stability index
        if not missing("stability", "stability", "Microsoft's stability index"):
            scored = [(pc, to_float(val(pc, "stability"))) for pc in pcs]
            scored = [(pc, x) for pc, x in scored if x is not None]
            line("stability", [(pc, f"{x:g}") for pc, x in sorted(scored, key=lambda y: y[1]) if x < STABILITY_LOW],
                 len(scored), f"Microsoft's own score of how stable the PC is, 1 to 10. Under {STABILITY_LOW}.")
        # little free space: Aternity's own bands
        if not missing("free", "free", "the free space"):
            banded = [(pc, str(val(pc, "free"))) for pc in pcs if val(pc, "free")]
            low = [(pc, b) for pc, b in banded if (band_top_gb(b) if band_top_gb(b) is not None else FREE_LOW_GB + 1) <= FREE_LOW_GB]
            bands = sorted({b for _, b in low}, key=lambda b: band_top_gb(b) or 0)
            line("free", sorted(low, key=lambda x: band_top_gb(x[1]) or 0), len(banded),
                 f"Aternity gives free space as bands; those that end at {FREE_LOW_GB} GB or under"
                 + (": " + ", ".join(f"'{b}'" for b in bands) + "." if bands else "."))
        # a spinning hard disk
        if not missing("spinning", "disk", "the kind of disk"):
            hdd = [(pc, str(val(pc, "disk"))) for pc in pcs if str(val(pc, "disk") or "").upper().startswith(W["spinning"])]
            line("spinning", hdd, sum(1 for pc in pcs if val(pc, "disk") and not is_virtual(val(pc, "kind"))),
                 "Aternity's kind of disk starts with '" + W["spinning"] + "'"
                 + (": " + ", ".join(f"'{k}'" for k in sorted({k for _, k in hdd})) + "." if hdd else "."))
        return out

    def day(self, t: datetime) -> str:
        return t.astimezone(self.zone).strftime("%b %d").replace(" 0", " ")

    # -- the tab ------------------------------------------------------------------------
    def view(self, now: datetime | None = None, retired: dict | None = None) -> dict:
        now = now or self.clock()
        with self.lock:
            out = {"ready": self.as_of is not None, "reading": self.reading, "status": self.status, "off": self.off,
                   "zone": self.zone_label, "every_min": READ_EVERY_MIN, "retired": retired}
            if self.as_of is None:
                return out
            pcs = list(self.pcs.values())
            judged = self._judged(now)
            kinds = Counter(self._kind(pc) for pc in pcs) if R["kind"] in self.have else {}
            ages = [self.age(pc["heard"], now) for pc in pcs]
            n = lambda lo, hi=None: sum(1 for a in ages if a is not None and a >= lo and (hi is None or a <= hi))  # noqa: E731
            dated = [pc for pc in pcs if pc["heard"]]
            oldest = min(dated, key=lambda pc: pc["heard"]) if dated else None
            lines = [{"key": k, "label": LINES[k], "text": j["text"], "n": len(j["pcs"]), "of": j["of"]}
                     for k, j in judged.items() if not j["unknown"]]
            alive = [pc for pc in pcs if (self.age(pc["heard"], now) or 0) < SILENT_DAYS]
            idle = len(judged["unused"]["pcs"])
            out.update({
                "as_of": self.as_of.isoformat(),
                "as_of_local": self.as_of.astimezone(self.zone).strftime("%b %d, %H:%M"),
                "pcs": len(pcs), "virtual": sum(1 for pc in pcs if is_virtual(pc["row"].get(R["kind"]))) if kinds else None,
                "kinds": [{"kind": k, "n": v} for k, v in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))],
                "heard": {"known": LAST in self.have, "today": n(0, 0), "week": n(1, SILENT_DAYS - 1),
                          "silent": n(SILENT_DAYS, OLD_DAYS - 1), "old": n(OLD_DAYS), "none": len(pcs) - len(dated),
                          "longest": ({"days": self.age(oldest["heard"], now), "day": self.day(oldest["heard"])}
                                      if oldest else None)},
                "statuses": ([{"status": k, "n": v} for k, v in Counter(
                    str(pc["row"].get(STATUS) or "no status") for pc in pcs).most_common()] if STATUS in self.have else []),
                "lines": sorted((x for x in lines if x["n"]), key=lambda x: (-x["n"], x["label"])),
                "quiet": [x["label"] for x in lines if not x["n"]],
                "unknown": [{"label": LINES[k], "why": j["unknown"]} for k, j in judged.items() if j["unknown"]],
                "used": ({"known": True, "of": len(alive), "used": len(alive) - idle, "unused": idle}
                         if self.used is not None else {"known": False}),
                "notes": list(self.notes),
                "usage": self._usage_view(now),
                "rules": {"silent_days": SILENT_DAYS, "old_days": OLD_DAYS, "use_days": USE_DAYS,
                          "restart_days": RESTART_DAYS, "stability_low": STABILITY_LOW, "free_low_gb": FREE_LOW_GB,
                          "find_min": FIND_MIN},
            })
        return out

    def line_pcs(self, key: str, now: datetime | None = None) -> dict | None:
        """The PCs behind one "Needs a look" line, from the read held (no read of Aternity)."""
        now = now or self.clock()
        with self.lock:
            j = self._judged(now).get(key) if self.as_of else None
            if j is None or j["unknown"]:
                return None
            return {"what": LINES[key], "text": j["text"], "total": len(j["pcs"]), "of": j["of"],
                    "as_of_local": self.as_of.astimezone(self.zone).strftime("%b %d, %H:%M"),
                    "rows": [self._brief(pc, what) for pc, what in j["pcs"][:PCS_SHOWN]]}

    def find(self, text: str) -> dict:
        """PCs whose name, last user or serial number has the text (any case), names that start
        with it first."""
        q = str(text or "").strip().lower()
        if len(q) < FIND_MIN:
            return {"rows": [], "more": 0, "short": True}
        with self.lock:
            hits = [pc for pc in self.pcs.values() if q in pc["name"].lower()
                    or q in str(pc["row"].get(R["user"]) or "").lower() or q in str(pc["row"].get(R["serial"]) or "").lower()]
            hits.sort(key=lambda pc: (not pc["name"].lower().startswith(q), pc["name"].lower()))
            return {"rows": [self._brief(pc) for pc in hits[:FIND_MAX]], "more": max(len(hits) - FIND_MAX, 0),
                    "short": False}

    def card(self, name: str, now: datetime | None = None) -> dict | None:
        """Everything Aternity's device list holds about one PC, as it sends it, and the "Needs a
        look" lines the PC is on."""
        now = now or self.clock()
        with self.lock:
            pc = self.pcs.get(str(name or "").strip().lower())
            if pc is None:
                return None
            r = pc["row"]
            def said(value, how) -> str:  # a time in the page's time zone; everything else as Aternity sends it
                t = parse_time(value) if how == "date" else None
                return t.astimezone(self.zone).strftime("%b %d, %Y, %H:%M") if t else str(value)
            fields = [{"group": group, "label": label, "value": said(r.get(c), how)} for group, items in INVENTORY_FILLED
                      for label, c, how in items if c in self.have and r.get(c) not in (None, "")]
            boot = parse_time(r.get(R["boot"])) if R["boot"] in self.have else None
            judged = self._judged(now)
            return dict(self._brief(pc), status=r.get(STATUS) if STATUS in self.have else None,
                        heard_days=self.age(pc["heard"], now), boot_days=self.age(boot, now),
                        used=(pc["name"].lower() in self.used) if self.used is not None else None,
                        use_days=USE_DAYS, fields=fields,
                        lines=[LINES[k] for k, j in judged.items() if any(p is pc for p, _ in j["pcs"])],
                        as_of_local=self.as_of.astimezone(self.zone).strftime("%b %d, %H:%M"))


def keep_reading(inv: Inventory, make_client, cfg_of, sleep, clock=lambda: datetime.now(timezone.utc)) -> None:
    """Runs in a background thread: a read at start, then every READ_EVERY_MIN minutes; when
    Aternity does not answer, the last read stays on the tab and it is tried again after RETRY_MIN."""
    while True:
        wait = READ_EVERY_MIN * 60
        try:
            client = make_client()
            if client is None:
                inv.off = "Aternity is not set, so there is nothing to show (aternity.base_url in config.yaml)."
                log.info("Inventory: %s", inv.off)
                return
            inv.off, inv.reading, started = None, True, clock()
            inv.read(client, cfg_of(), started)
            inv.status = "Read"
            log.info("Inventory: read in %.1f s: %s PCs%s", inv.took or 0, f"{len(inv.pcs):,}",
                     f"; {len(inv.notes)} notes" if inv.notes else "")
            for n in inv.notes:
                log.info("Inventory: note: %s", n)
            held = len(inv.days)
            inv.read_usage(client, clock())  # the usage charts: the days and months not kept yet
            if len(inv.days) != held or inv.use_notes:
                log.info("Inventory: PCs in use are kept for %s days and %s months%s", f"{len(inv.days):,}",
                         len(inv.months), f"; {len(inv.use_notes)} notes" if inv.use_notes else "")
            for n in inv.use_notes:
                log.info("Inventory: note: %s", n)
        except Exception as exc:  # said on the tab; the last read stays
            inv.status = (f"Aternity's device list could not be read ({short_error(exc)}); trying again in "
                          f"{RETRY_MIN} minutes.")
            log.warning("Inventory: %s", inv.status)
            wait = RETRY_MIN * 60
        finally:
            inv.reading = False
        sleep(wait)
