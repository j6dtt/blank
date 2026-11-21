"""The admin page's counts (docs/admin-roadmap.md; the user's request of 2026-11-15): who uses the
app and how, and what it asks of Riverbed and the AI services.

- Each use is recorded as it happens (a tab opened, an investigation, a question, a Care list
  change, the other platform's status poll) with the caller's address and its time; the calls
  made to Aternity, NetIM and the AI services are counted by http.METER, under what the app was
  doing (http.PURPOSE).
- Who: the main app has no login, so a caller's address is looked up in Aternity's activity
  (DEVICE_IP_ADDRESS, USERNAME, DEVICE_NAME), which the Today reader reads anyway: it keeps the
  rows of the addresses that opened the app (Today.watch, Today.at_address). The user on that
  address nearest the visit, within MATCH, is the person; a pooled desktop gives whoever was on it
  then. Most people use the app on virtual desktops (the user, 2026-11-15), which have their own
  address. Several users on it at that time (a server desktop shared by many): "one of N". No row
  from that address within GIVE_UP: "not known". Every visit from one address (a proxy in front of
  the app hides the desktops): said, never guessed.
- A day is saved in <data>/usage/<date>.json (local date), KEEP_DAYS, then deleted: usernames,
  desktop names and Ask anything's questions (the user's choices, 2026-11-15).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from .http import short_error

log = logging.getLogger("uxcorrelate")

KEEP_DAYS = 30  # usage files kept, then deleted (the user's choice, 2026-11-15)
NOW_MIN = 15  # "now": visits in the last 15 minutes
MATCH = timedelta(hours=1)  # a visit and an action from its address at most this far apart
GIVE_UP = timedelta(hours=2)  # Aternity sends rows late: a visit not placed by then is "not known"
ONE_ADDRESS = 10  # at least this many visits, all from one address: people cannot be told apart
SAVE_EVERY_S = 60  # the day's file is written at most this often (and after each read of Aternity)
TABS = {"today": "Today", "care": "Care list", "investigate": "Investigate", "startups": "Start-ups",
        "software": "Software", "inventory": "Inventory"}
# what an investigation found first (answer["found"][0]["issue"], correlate._issue), in words
FOUND = {"disk": "a failing disk", "crash": "an app crash", "hang": "an app hang", "system": "a system event",
         "background": "a background crash", "limit": "a PC limit (CPU, memory, disk queue)",
         "startup": "a slow start-up", "infra": "network or servers (NetIM)"}
NOTHING = "nothing wrong found"
FAILED_STEPS = ("Could not be read", "Failed")  # agent.run's titles of a look-up that failed


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def addr_of(request) -> str | None:
    """The caller's address as Aternity writes a PC's (an IPv4 address sent over IPv6 loses its prefix)."""
    host = request.client.host if getattr(request, "client", None) else None
    return host[7:] if host and host.startswith("::ffff:") else host


def found_of(result) -> str:
    """What an investigation found first, in words."""
    answer = (result or {}).get("answer") or {} if isinstance(result, dict) else {}
    found = answer.get("found") or []
    kind = found[0].get("issue") if found and isinstance(found[0], dict) else None
    return FOUND.get(kind, kind) if kind else NOTHING


def _new_day(d: date) -> dict:
    return {"date": d.isoformat(), "visits": [], "runs": [],
            "care": {"changes": [], "lit": 0, "runs": 0, "listed": 0},
            "polls": {"n": 0, "last": None, "a": None}, "calls": {}, "calls_hour": {}, "reads": [], "starts": []}


def _counts(by_source: dict, source: str) -> list:
    """One source's counts: calls, rows, seconds, retries, failed, cut short, calls whose reply had
    rows. A day saved before Nov 18 2026 has the first six only (its rows say whether it had any)."""
    c = by_source.setdefault(source, [0, 0, 0.0, 0, 0, 0, 0])
    c.extend([0] * (7 - len(c)))
    return c

class Usage:
    def __init__(self, today, store: str | os.PathLike | None = None):
        self.today = today
        self.dir = Path(store) / "usage" if store else None
        self.lock = threading.Lock()
        self.days: dict[str, dict] = {}  # local date -> its counts (today, and the day before at midnight)
        self.dirty: set[str] = set()
        self.saved_at: datetime | None = None
        if self.dir:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("Admin page: its folder %s could not be made (%s): usage is kept until a restart only",
                            self.dir, short_error(exc))

    # -- days and files ---------------------------------------------------------
    def _file(self, d: str) -> Path | None:
        return self.dir / f"{d}.json" if self.dir else None

    def _read(self, d: str) -> dict | None:
        f = self._file(d)
        if f is None or not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Admin page: %s could not be read (%s): that day is left out", f.name, short_error(exc))
            return None

    def _day(self, now: datetime) -> dict:
        """Today's counts (local date), loaded from its file after a restart; the addresses that
        opened the app today are watched by the Today reader."""
        d = self.today.day(now).isoformat()
        day = self.days.get(d)
        if day is None:
            day = self._read(d) or _new_day(self.today.day(now))
            for k, v in _new_day(self.today.day(now)).items():  # a file of an older build: what it lacks
                day.setdefault(k, v)
            self.days[d] = day
            self.today.watch = {v["a"] for v in day["visits"] if v.get("a")}
            for old in [k for k in self.days if k < d]:  # the day before: written, then out of memory
                self._write(old)
                if old not in self.dirty:
                    del self.days[old]
            self._prune(self.today.day(now))
        return day

    def _write(self, d: str) -> None:
        f = self._file(d)
        if f is None or d not in self.dirty:
            return
        try:
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.days[d], separators=(",", ":")), encoding="utf-8")
            tmp.replace(f)
            self.dirty.discard(d)
        except OSError as exc:
            log.warning("Admin page: %s could not be written (%s); trying again later", f.name, short_error(exc))

    def _prune(self, today: date) -> None:
        if not self.dir:
            return
        cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
        for f in self.dir.glob("*.json"):
            if f.stem < cut:
                try:
                    f.unlink()
                    log.info("Admin page: %s deleted (kept %d days)", f.name, KEEP_DAYS)
                except OSError:
                    pass

    def save(self, now: datetime | None = None, force: bool = False) -> None:
        now = now or datetime.now(timezone.utc)
        with self.lock:
            if not force and self.saved_at and (now - self.saved_at).total_seconds() < SAVE_EVERY_S:
                return
            self.saved_at = now
            for d in list(self.dirty):
                self._write(d)

    def _changed(self, day: dict, now: datetime) -> None:
        self.dirty.add(day["date"])

    # -- what happened ----------------------------------------------------------
    def seen(self, addr: str | None, tab: str, now: datetime | None = None) -> None:
        """A tab of the main app opened (the page's beacon, at load and at each switch)."""
        now = now or datetime.now(timezone.utc)
        if tab not in TABS:
            return
        with self.lock:
            day = self._day(now)
            day["visits"].append({"t": _iso(now), "a": addr, "tab": tab})
            if addr and addr not in self.today.watch:
                self.today.watch = self.today.watch | {addr}  # a new set: the reader may be reading it
            self._changed(day, now)
        self.save(now)

    def run(self, what: str, addr: str | None, now: datetime | None = None, **fields) -> None:
        """One use: an investigation, a start-ups report, a question, an Explain, a Software click."""
        now = now or datetime.now(timezone.utc)
        with self.lock:
            day = self._day(now)
            day["runs"].append({"t": _iso(now), "a": addr, "what": what,
                                **{k: v for k, v in fields.items() if v is not None}})
            self._changed(day, now)
        self.save(now)

    def investigation(self, addr, by: str, seconds: float, result=None, error=None, now=None) -> None:
        self.run("investigation", addr, now, by=str(by)[:40], s=round(seconds, 1), ok=error is None,
                 found=found_of(result) if error is None else None)

    def asked(self, addr, question: str, seconds: float, answer=None, error=None, tab=None, now=None) -> None:
        steps = (answer or {}).get("steps") or []
        self.run("ask", addr, now, q=" ".join(str(question).split())[:300], s=round(seconds, 1),
                 ok=error is None, steps=len(steps), tab=tab,
                 failed=sum(1 for x in steps if isinstance(x, dict) and x.get("title") in FAILED_STEPS),
                 flagged=len((answer or {}).get("flagged") or []))

    def care_change(self, addr, what: str, ok: bool, now: datetime | None = None) -> None:
        """A change of the Care list on the page; ok False: a wrong PIN."""
        now = now or datetime.now(timezone.utc)
        with self.lock:
            day = self._day(now)
            day["care"]["changes"].append({"t": _iso(now), "a": addr, "what": what[:80], "ok": ok})
            self._changed(day, now)
        self.save(now)

    def care_counts(self, now: datetime | None = None, lit: int = 0, ran: int = 0,
                    listed: int | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self.lock:
            day = self._day(now)
            day["care"]["lit"] += lit
            day["care"]["runs"] += ran
            if listed is not None:
                day["care"]["listed"] = listed
            self._changed(day, now)

    def poll(self, addr: str | None, now: datetime | None = None) -> None:
        """The other platform's GET /api/MSS/care/status."""
        now = now or datetime.now(timezone.utc)
        with self.lock:
            day = self._day(now)
            p = day["polls"]
            p.update(n=p["n"] + 1, last=_iso(now), a=addr)
            self._changed(day, now)

    def call(self, system: str, purpose: str, source: str, seconds: float, rows, retries: int, ok: bool) -> None:
        """http.METER: one call to Aternity, NetIM or an AI service."""
        now = datetime.now(timezone.utc)
        with self.lock:
            day = self._day(now)
            c = _counts(day["calls"].setdefault(system, {}).setdefault(purpose, {}), source)
            c[0] += 1
            c[1] += rows or 0
            c[6] += rows is not None  # calls whose reply has rows: an AI service's never do
            c[2] = round(c[2] + seconds, 1)
            c[3] += retries
            c[4] += not ok
            hour = f"{self.today.local(now).hour:02d}"
            by = day["calls_hour"].setdefault(system, {})
            by[hour] = by.get(hour, 0) + 1
            self._changed(day, now)

    def cut(self, system: str, purpose: str, source: str) -> None:
        """http.CUT: a read Aternity cut short (aternity.max_pages)."""
        now = datetime.now(timezone.utc)
        with self.lock:
            day = self._day(now)
            c = _counts(day["calls"].setdefault(system, {}).setdefault(purpose, {}), source)
            c[5] += 1
            self._changed(day, now)

    def read_done(self, now: datetime | None = None) -> None:
        """After each read of the Today reader: how long it took, who is placed now, the file."""
        now = now or datetime.now(timezone.utc)
        took, first = self.today.read_took, self.today.start_took
        with self.lock:
            day = self._day(now)
            if took is not None:
                day["reads"].append([_iso(now), round(took, 1)])
                self.today.read_took = None
            if first is not None:  # the read at start (the last 24 hours): not one of the regular reads
                day["starts"].append([_iso(now), round(first, 1)])
                self.today.start_took = None
            self._place(day, now)
            self._changed(day, now)
        self.save(now, force=True)

    # -- who --------------------------------------------------------------------
    def _who(self, addr: str | None, t: datetime) -> tuple[dict | None, bool]:
        """(the person on that address at t, final): the user Aternity saw there nearest t within
        MATCH; several on it at t (a shared server desktop): one of them, with the others."""
        if not addr:
            return None, True
        seen = self.today.at_address(addr, t - MATCH, t + MATCH)
        if not seen:
            return None, False

        def apart(e) -> float:
            return 0.0 if e[3] <= t <= e[4] else min(abs((t - e[3]).total_seconds()), abs((t - e[4]).total_seconds()))
        ranked = sorted(seen.values(), key=lambda e: (apart(e), e[0].lower()))
        best = apart(ranked[0])
        same = [e for e in ranked if apart(e) == best]
        p = {"u": same[0][0], "d": same[0][1], "k": same[0][2]}
        if len(same) > 1:
            p["n"] = sorted(e[0] for e in same)
        return p, best == 0

    def _place(self, day: dict, now: datetime) -> None:
        """Puts a person on each use of the day that has none yet, for good once Aternity has a row
        from that address at that time, or GIVE_UP after it ("not known")."""
        for e in day["visits"] + day["runs"] + day["care"]["changes"]:
            if "p" in e or not e.get("a"):
                continue
            t = _when(e["t"])
            p, final = self._who(e["a"], t)
            if p is not None and (final or now - t >= GIVE_UP):
                e["p"] = p
            elif p is None and now - t >= GIVE_UP:
                e["p"] = {"x": True}

    def _person(self, e: dict) -> dict | None:
        """A use's person: as placed, or for now (not yet for good)."""
        if "p" in e:
            return None if e["p"].get("x") else e["p"]
        return self._who(e.get("a"), _when(e["t"]))[0] if e.get("a") else None

    # -- the page -----------------------------------------------------------------
    def dates(self, now: datetime) -> list[str]:
        have = {f.stem for f in self.dir.glob("*.json")} if self.dir and self.dir.exists() else set()
        have.add(self.today.day(now).isoformat())
        return sorted(have, reverse=True)[:KEEP_DAYS + 1]

    def view(self, now: datetime, d: str | None = None) -> dict:
        today = self.today
        t = today.day(now).isoformat()
        d = d or t
        with self.lock:
            if d == t:
                day = json.loads(json.dumps(self._day(now)))  # a copy: the page's work outside the lock
            else:
                day = self.days.get(d) or self._read(d)
                day = json.loads(json.dumps(day)) if day else None
        if day is None:
            return {"date": d, "dates": self.dates(now), "missing": True}
        for k, v in _new_day(date.fromisoformat(d)).items():
            day.setdefault(k, v)
        local = lambda s: today.local(_when(s)).strftime("%H:%M")  # noqa: E731

        def who(e: dict) -> dict:
            p = self._person(e)
            if p is None:
                a = e.get("a")
                if "p" not in e and a and now - _when(e["t"]) < GIVE_UP:  # looked up again at each read
                    return {"key": f"wait {a}", "who": None, "addr": a, "waiting": True}
                return {"key": f"addr {a or '?'}", "who": None, "addr": a}
            return {"key": p["u"].lower(), "who": p["u"], "pc": p.get("d"), "kind": p.get("k"), "others": p.get("n")}

        people: dict[str, dict] = {}
        for v in day["visits"]:
            w = who(v)
            x = people.setdefault(w["key"], dict(w, first=v["t"], last=v["t"], tabs=Counter(), pcs=set(), runs=Counter()))
            x["first"], x["last"] = min(x["first"], v["t"]), max(x["last"], v["t"])
            x["tabs"][v["tab"]] += 1
            if w.get("pc"):
                x["pcs"].add(w["pc"])
            x["tab_now"] = v["tab"] if v["t"] >= x.get("tab_at", "") else x.get("tab_now")
            x["tab_at"] = max(x.get("tab_at", ""), v["t"])
        for r in day["runs"]:
            if not r.get("a"):
                continue
            w = who(r)
            x = people.setdefault(w["key"], dict(w, first=r["t"], last=r["t"], tabs=Counter(), pcs=set(), runs=Counter()))
            x["runs"][r["what"]] += 1
            x["last"] = max(x["last"], r["t"])
            if w.get("pc"):
                x["pcs"].add(w["pc"])
        rows = []
        for x in sorted(people.values(), key=lambda x: x["last"], reverse=True):
            rows.append({"who": x["who"], "addr": x.get("addr"), "waiting": bool(x.get("waiting")),
                         "pcs": sorted(x["pcs"]), "kind": x.get("kind"),
                         "others": x.get("others"), "first_local": local(x["first"]), "last_local": local(x["last"]),
                         "last": x["last"], "tabs": [TABS[k] for k, _ in x["tabs"].most_common() if k in TABS],
                         "tab_now": TABS.get(x.get("tab_now")), "investigations": x["runs"]["investigation"],
                         "questions": x["runs"]["ask"], "startups": x["runs"]["startups"],
                         "explains": x["runs"]["explain"], "software": x["runs"]["software"]})
        cut_now = _iso(now - timedelta(minutes=NOW_MIN))
        active = [r for r in rows if d == t and r["last"] >= cut_now]
        addrs = {v.get("a") for v in day["visits"] if v.get("a")}
        note = None
        if len(day["visits"]) >= ONE_ADDRESS and len(addrs) == 1:
            note = (f"All {len(day['visits'])} visits came from one address ({next(iter(addrs))}): something in "
                    "front of the app (a proxy) hides the desktops, so people cannot be told apart.")

        runs = lambda what: [r for r in day["runs"] if r["what"] == what]  # noqa: E731

        def secs(xs: list[dict]) -> dict | None:
            s = [r["s"] for r in xs if r.get("s") is not None and r.get("ok", True)]
            return {"least": min(s), "typical": round(median(s), 1), "most": max(s)} if s else None
        inv, asks = runs("investigation"), runs("ask")
        by_hour = Counter(today.local(_when(v["t"])).hour for v in day["visits"])
        polls = day["polls"]
        reads = [s for _, s in day["reads"]]
        load = []
        def rows_of(c: list):  # None: no reply had rows ("0 rows" was said of every AI service)
            return c[1] if c[6] or c[1] else None
        for system, by_purpose in sorted(day["calls"].items()):
            tot = [0, 0, 0.0, 0, 0, 0, 0]
            purposes, sources = [], Counter()
            for purpose, by_source in by_purpose.items():
                p = [0, 0, 0.0, 0, 0, 0, 0]
                for source in by_source:
                    c = _counts(by_source, source)
                    p = [a + b for a, b in zip(p, c)]
                    sources[source] += c[0]
                purposes.append({"purpose": purpose, "calls": p[0], "rows": rows_of(p), "seconds": round(p[2]),
                                 "retries": p[3], "failed": p[4], "cut": p[5]})
                tot = [a + b for a, b in zip(tot, p)]
            hours = day["calls_hour"].get(system, {})
            load.append({"system": system, "calls": tot[0], "rows": rows_of(tot), "seconds": round(tot[2]),
                         "retries": tot[3], "failed": tot[4], "cut": tot[5],
                         "purposes": sorted(purposes, key=lambda p: -p["calls"]),
                         "sources": [{"source": s, "calls": n} for s, n in sources.most_common(8)],
                         "by_hour": [hours.get(f"{h:02d}", 0) for h in range(24)]})
        return {
            "date": d, "today": d == t, "dates": self.dates(now), "zone": today.zone_label,
            "as_of_local": today.local(now).strftime("%H:%M"), "note": note, "now_min": NOW_MIN, "match_min": 60,
            "give_up_h": round(GIVE_UP.total_seconds() / 3600),
            "next_read_local": today.local(today.next_read).strftime("%H:%M") if today.next_read else None,
            "active": active, "people": rows,
            "pages": {"total": len(day["visits"]), "by_hour": [by_hour.get(h, 0) for h in range(24)],
                      "by_tab": [{"tab": TABS[k], "n": n} for k, n in Counter(v["tab"] for v in day["visits"]).most_common()
                                 if k in TABS]},
            "investigations": {"n": len(inv), "failed": sum(1 for r in inv if not r.get("ok", True)),
                               "by": Counter(r.get("by") or "?" for r in inv).most_common(),
                               "found": Counter(r["found"] for r in inv if r.get("found")).most_common(),
                               "seconds": secs(inv)},
            "startups": {"n": len(runs("startups")), "failed": sum(1 for r in runs("startups") if not r.get("ok", True)),
                         "seconds": secs(runs("startups"))},
            "software": {"n": len(runs("software")),
                         "by": Counter(r.get("by") or "?" for r in runs("software")).most_common()},
            "explain": {"n": len(runs("explain")),
                        "by": Counter(TABS.get(r.get("tab"), r.get("tab") or "?") for r in runs("explain")).most_common()},
            "ask": {"n": len(asks), "failed": sum(1 for r in asks if not r.get("ok", True)),
                    "steps": sum(r.get("steps", 0) for r in asks),
                    "failed_lookups": sum(r.get("failed", 0) for r in asks),
                    "flagged": sum(1 for r in asks if r.get("flagged")), "seconds": secs(asks),
                    "questions": [{"at_local": local(r["t"]), "who": who(r)["who"], "addr": r.get("a"),
                                   "waiting": bool(who(r).get("waiting")),
                                   "q": r.get("q", ""), "steps": r.get("steps", 0), "ok": r.get("ok", True)}
                                  for r in reversed(asks)]},
            "care": {"listed": day["care"]["listed"], "lit": day["care"]["lit"], "runs": day["care"]["runs"],
                     "changes": [{"at_local": local(c["t"]), "who": who(c)["who"], "addr": c.get("a"),
                                  "waiting": bool(who(c).get("waiting")),
                                  "what": c["what"], "ok": c["ok"]} for c in reversed(day["care"]["changes"])],
                     "wrong_pins": sum(1 for c in day["care"]["changes"] if not c["ok"]),
                     "polls": {"n": polls["n"], "last": polls["last"], "last_local": local(polls["last"]) if polls["last"] else None,
                               "from": polls["a"]}},
            "load": load,
            "reads": {"n": len(reads), "typical": round(median(reads), 1) if reads else None,
                      "longest": max(reads) if reads else None, "every_min": today.refresh_min,
                      "over": sum(1 for s in reads if s > today.refresh_min * 60),
                      "starts": [{"at_local": local(t), "seconds": s} for t, s in day["starts"]]},
            "days": self._days(now),
        }

    def _days(self, now: datetime) -> list[dict]:
        """One row per day kept: people, pages, investigations, questions (the strip of bars)."""
        out = []
        for d in self.dates(now):
            day = self.days.get(d) or self._read(d)
            if not day:
                continue
            keys = {self._key(e) for e in day.get("visits", [])}
            named = sum(1 for k in keys if not k.startswith("addr "))  # an address with no name is not a person
            out.append({"date": d, "people": named, "unnamed": len(keys) - named, "pages": len(day.get("visits", [])),
                        "investigations": sum(1 for r in day.get("runs", []) if r["what"] == "investigation"),
                        "questions": sum(1 for r in day.get("runs", []) if r["what"] == "ask")})
        return sorted(out, key=lambda x: x["date"])

    def _key(self, e: dict) -> str:
        p = e.get("p")
        if p and not p.get("x"):
            return p["u"].lower()
        if "p" not in e and e.get("a"):
            q = self._who(e["a"], _when(e["t"]))[0]
            if q:
                return q["u"].lower()
        return f"addr {e.get('a')}"
