"""The assessment for executives, at the top of the Today tab (`/api/assess`).

The user's request (2026-11-11): "an AI overall assessment for executive hourly or 3-hour
(what new in the last x hour and overall today up to the hour)". Choices taken ("all
recommended"): every 3 hours in working hours (`HOURS`), counts and application names only
(no user, no PC), one summary of the day written after midnight and shown the next morning
as "Yesterday" (written once, so the two never differ), kept `KEEP_DAYS` days.

Our code works the facts out, as plain lines; the AI words them (`chat.SHAPE`) and every
number in its text must be in those lines, as in the chats; otherwise the lines are shown
as they are. Written once per period and saved in `<data>/assess/<date>.json`, so everyone
reads the same text and it costs one AI call a period. The verdict and the "look first at"
line are by fixed rules (`verdict`, `first`), never the AI's.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .chat import RATIO, SHAPE, allowed, clean, numbers
from .config import SEVERITY_ORDER, TOP_STATUS
from .http import ApiError, short_error
from .models import plural
from .today import MIN_USUAL, USUAL_DAYS, kind_of

HOURS = (9, 12, 15, 18)  # local time: an assessment at each; the first read after it writes it
KEEP_DAYS = 8
TOP = 3  # events, applications named in one assessment
MIN_ACTIONS = 50  # an hour with fewer measured actions has no share (one slow action would move it a lot)
MIN_STARTS = 5  # an hour, or a type, with fewer start-ups has no typical time
BOOTS_EVERY = timedelta(minutes=55)  # today's start-ups are read again after this (the charts are "up to the hour")
VERDICT_PCS = 5  # fewer PCs than this do not make the day's verdict (as software.MIN_PROBLEM)
SOURCES = ("Aternity", "NetIM")  # a connection down that an executive must know of
ATTENTION, WORSE, NORMAL, UNKNOWN = ("Needs attention", "Worse than usual", "A normal day so far",
                                     "No usual to compare with yet")
log = logging.getLogger("uxcorrelate")

SYSTEM = ("You write a short assessment for executives of how the company's PC users are doing today, from "
          "Riverbed Aternity. You are given the verdict and the facts as lines: what is new since the last "
          "assessment, the day so far, what is still open, and what to look at first. Write it " + SHAPE + ". "
          "The first sentence gives the verdict as it is given and why, in plain words. The points say what is new and "
          "what matters in the day so far, by name, most serious first; leave out what does not matter to an "
          "executive. The last line is the given \"look first at\". Use only numbers that are in the lines, written "
          "exactly as given, and do not work out new ones (no sums, no percentages, no \"twice as many\"). Say "
          "more, fewer or as usual only where a line says so. Never name a user or a PC. No greeting, no sign-off.")


def _usual(u: dict | None) -> str | None:
    """A count's own verdict against the days before, in words; None when not known yet."""
    if not u or not u.get("enough"):
        return None
    return {"more": "more than usual", "fewer": "fewer than usual"}.get(u.get("verdict"), "as usual")


def _rank(e: dict) -> tuple:
    """Most serious first: Aternity's severity, then a crash, a hang, then the PCs."""
    name = (e.get("name") or "").lower()
    sev = SEVERITY_ORDER.index(e["severity"]) if e.get("severity") in SEVERITY_ORDER else len(SEVERITY_ORDER)
    return (sev, 0 if "crash" in name else 1 if "hang" in name else 2, e.get("usual") != "more than usual",
            -e["pcs"])


def snapshot(today_view: dict, care_view: dict | None, status_view: dict | None, boots: dict | None = None) -> dict:
    """What an assessment rests on, as counts and names of events and applications: no user,
    no PC. Saved with it, so the next one can say what is new."""
    s = today_view["slow"]
    events = [{"name": e["name"], "category": e["category"], "severity": e.get("severity"), "pcs": e["pcs"],
               "usual": _usual(e.get("usual"))} for e in today_view.get("events", []) if e["pcs"]]
    users = (care_view or {}).get("users", [])
    return {
        "active": {k: today_view["active"]["today"][k] for k in ("users", "pcs", "virtual", "physical")},
        "events": sorted(events, key=_rank),
        "slow": {"pcs": s["pcs"], "actions": s["actions"], "users": s["users"], "usual": _usual(s.get("usual"))},
        "apps": {a["application"]: a["actions"] for a in today_view.get("applications", [])},
        "care": {"listed": len(users), "with_issue": sum(1 for u in users if u.get("status") == "issue"),
                 "issues": sum(len(u.get("issues", [])) + len(u.get("cleared", [])) for u in users)},
        "down": sorted(i["name"] for i in (status_view or {}).get("items", []) if i.get("state") == "down"),
        # start-ups today, per Aternity type: how many, the typical time, the days before (boots_of)
        "boots": boots,
    }


def _judged(snap: dict) -> tuple[str, list[str]]:
    """The verdict and why, by fixed rules. A count rated Critical or Major that is more than
    usual on `VERDICT_PCS` PCs or more makes the day worse than usual (Critical: needs
    attention), as does a connection to Aternity or NetIM being down. Fewer PCs than that are
    listed, but 2 PCs against a usual 0 to 1 are not the company's day (stage, Nov 2026)."""
    counts = [dict(e, what=e["name"]) for e in snap["events"]] + [
        dict(snap["slow"], severity=TOP_STATUS, what=f"{TOP_STATUS} slow actions")]
    top = [c for c in counts if c.get("severity") in SEVERITY_ORDER[:2]]
    more = [c for c in top if c["usual"] == "more than usual" and c["pcs"] >= VERDICT_PCS]
    why = [f"{c['what']} ({c['severity']}) is on {plural(c['pcs'], 'PC')}, more than usual" for c in more]
    down = [n for n in snap["down"] if n in SOURCES]
    if down:
        return ATTENTION, [f"the connection to {' and '.join(down)} is down"] + why
    if any(c["severity"] == "Critical" for c in more):
        return ATTENTION, why
    if more:
        return WORSE, why
    if not any(c["usual"] for c in top):
        return UNKNOWN, ["the days before are not saved yet for any count rated Critical or Major"]
    small = [c for c in top if c["usual"] == "more than usual"]
    return NORMAL, [f"nothing rated Critical or Major is more than usual on {VERDICT_PCS} PCs or more"
                    + (f" ({', '.join(c['what'] + ': ' + plural(c['pcs'], 'PC') for c in small)} more than usual)"
                       if small else "")]


def verdict(snap: dict) -> str:
    return _judged(snap)[0]


def first(snap: dict) -> str:
    """What to look at first: a connection down, else the most serious event (Critical or
    Major), else the slow actions when more than usual, else nothing to act on."""
    down = [n for n in snap["down"] if n in SOURCES]  # an AI service down is said, never what to look at first
    if down:
        return (f"the connection to {' and '.join(down)}, which is down (the lights at the top right of "
                "the page say since when)")
    top = [e for e in snap["events"] if e.get("severity") in SEVERITY_ORDER[:2]]
    if top:
        e = top[0]
        return (f"{e['name']} on {plural(e['pcs'], 'PC')} ({e['severity']}"
                + (f", {e['usual']}" if e["usual"] else "") + "): on the Today tab, click its tile for who has it")
    if snap["slow"]["usual"] == "more than usual":
        app = max(snap["apps"], key=snap["apps"].get, default=None)
        return (f"{TOP_STATUS} slow actions, more than usual" + (f"; the application with the most is {app}" if app else "")
                + ": on the Today tab, click it for who has them")
    return "nothing needs action now; the next assessment says what changes"


def _event_line(e: dict) -> str:
    return (f"{e['name']} ({e['severity'] or 'no severity'}): {plural(e['pcs'], 'PC')}"
            + (f", {e['usual']}" if e["usual"] else ""))


def today_lines(snap: dict) -> list[str]:
    """The day so far."""
    a, s = snap["active"], snap["slow"]
    out = [f"Active: {plural(a['users'], 'user')} on {plural(a['pcs'], 'PC')} ({a['virtual']:,} virtual desktops, "
           f"{a['physical']:,} physical PCs).",
           f"{TOP_STATUS} slow actions: {s['actions']:,} on {plural(s['pcs'], 'PC')}"
           + (f", {s['usual']}" if s["usual"] else ", no usual to compare with yet") + "."]
    rated = [e for e in snap["events"] if e.get("severity") in SEVERITY_ORDER[:2]]
    # the most serious, and every one that is more than usual: what the verdict rests on is always shown
    top = rated[:TOP] + [e for e in rated[TOP:] if e["usual"] == "more than usual"]
    out += [f"{_event_line(e)}." for e in top] or ["No crash or event rated Critical or Major."]
    if len(rated) > len(top):
        out.append(f"{plural(len(rated) - len(top), 'other kind')} of event rated Critical or Major, none more "
                   "than usual.")
    apps = sorted(snap["apps"].items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]
    if apps:
        out.append(f"Applications with the most {TOP_STATUS} slow actions: "
                   + ", ".join(f"{a} ({n:,})" for a, n in apps) + ".")
    c = snap["care"]
    if c["listed"]:
        out.append(f"Care list: {c['with_issue']:,} of the {plural(c['listed'], 'user')} watched "
                   f"{'has' if c['with_issue'] == 1 else 'have'} an open issue.")
    out += boot_lines(snap.get("boots"))
    return out


def boot_lines(boots: dict | None) -> list[str]:
    """Start-up and logon times today, each of Aternity's types against the days before."""
    if not boots or not boots.get("types"):
        return []
    unit, out = boots.get("unit", "s"), []
    for name, x in list(boots["types"].items())[:TOP]:
        u = x.get("usual")
        out.append(f"{name}: {plural(x['n'], 'start-up')} today, typically {x['typical']:g} {unit}"
                   + (f" (the {plural(u['days'], 'day')} before: {u['low']:g} to {u['high']:g} {unit})" if u
                      else " (no usual to compare with yet)") + ".")
    return out


def new_lines(snap: dict, before: dict | None) -> list[str]:
    """What is new since the assessment before (None: the first of the day, nothing to compare)."""
    if before is None:
        return []
    out = []
    was = {(e["category"], e["name"]): e for e in before["events"]}
    for e in snap["events"]:
        old = was.get((e["category"], e["name"]))
        if old is None:
            out.append(f"New: {_event_line(e)}.")
        elif e["pcs"] > old["pcs"]:
            out.append(f"{_event_line(e)}; it was {old['pcs']:,} at the last assessment.")
    s, b = snap["slow"], before["slow"]
    if s["pcs"] > b["pcs"]:
        out.append(f"{TOP_STATUS} slow actions: now on {plural(s['pcs'], 'PC')}; it was {b['pcs']:,} at the last "
                   "assessment" + (f"; {s['usual']}" if s["usual"] else "") + ".")
    risen = sorted(((a, n - before["apps"].get(a, 0)) for a, n in snap["apps"].items()
                    if n > before["apps"].get(a, 0)), key=lambda kv: (-kv[1], kv[0]))[:TOP]
    if risen:
        out.append(f"{TOP_STATUS} slow actions added since the last assessment, by application: "
                   + ", ".join(f"{a} ({n:,})" for a, n in risen) + ".")
    more = snap["care"]["issues"] - before["care"]["issues"]
    if more > 0:
        out.append(f"Care list: {plural(more, 'new issue')} for the users watched.")
    for n in snap["down"]:
        if n not in before["down"]:
            out.append(f"The connection to {n} went down.")
    for n in before["down"]:
        if n not in snap["down"]:
            out.append(f"The connection to {n} is back.")
    return out


def open_lines(snap: dict, before: dict | None) -> list[str]:
    """What an earlier assessment already had and is still there (not said as new again)."""
    if before is None:
        return []
    was = {(e["category"], e["name"]) for e in before["events"] if e.get("severity") in SEVERITY_ORDER[:2]}
    return [f"{_event_line(e)}." for e in snap["events"]
            if (e["category"], e["name"]) in was and e.get("severity") in SEVERITY_ORDER[:2]][:TOP]


def facts(snap: dict, before: dict | None, since: str | None) -> dict:
    new = new_lines(snap, before)
    said, why = _judged(snap)
    return {"verdict": said, "why": why, "since": since,
            "new": new if before is not None else [],
            "nothing_new": before is not None and not new,
            "today": today_lines(snap), "open": open_lines(snap, before), "first": first(snap)}


def given(f: dict, label: str) -> str:
    """The facts as the lines the AI is given (and the numbers its text is held to)."""
    head = (f"What is new since {f['since']}:" if f["since"] else "The first assessment of the day (nothing to "
            "compare with yet).")
    new = f["new"] or (["Nothing new."] if f["since"] else [])
    because = f" Because: {'; '.join(f['why'])}." if f.get("why") else ""
    return "\n".join([f"Assessment at {label}.", f"Verdict: {f['verdict']}.{because}", head, *new, "The day so far:",
                      *f["today"], *(["Still open from earlier:"] + f["open"] if f["open"] else []),
                      f"Look first at: {f['first']}."])


def refused(text: str, lines: str) -> str | None:
    """Why the AI's text is not shown (None: fine): empty, a number that is not in the lines, a
    worked-out comparison, or a user or PC named (it is given none, so none can be right)."""
    if not text:
        return "it gave no text"
    extra = numbers(text) - allowed(lines, SYSTEM)
    if extra:
        return "a number that is not in the facts (" + ", ".join(f"{x:g}" for x in sorted(extra)[:3]) + ")"
    ratio = RATIO.search(text)
    if ratio:
        return f"a comparison the facts do not give (\"{ratio.group(0)}\")"
    return None


def word(client, f: dict, label: str, service: str = "") -> tuple[str | None, str | None]:
    """The AI's text for these facts, and a note when it is not shown."""
    if client is None:
        return None, "No AI service is set (ai.services in config.yaml): these are the facts as counted."
    lines = given(f, label)
    try:
        text = clean(client.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": lines}])
                     .get("content"))
    except ApiError as exc:
        return None, f"The AI service {service or 'set'} did not answer ({exc.short()}): these are the facts as counted."
    why = refused(text, lines)
    if why:
        return None, f"The AI's text had {why}: these are the facts as counted."
    return text, None


def boots_of(rows: list, local) -> dict:
    """Start-ups of a day (or of today so far) per Aternity type: how many, the typical time
    (the middle value), and the typical time of each local hour with `MIN_STARTS` or more.
    Names and numbers only: no PC, no user."""
    from statistics import median
    per: dict[str, dict] = {}
    for r in rows:
        if r.type is None or r.duration is None:
            continue
        x = per.setdefault(r.type, {"all": [], "hours": {}})
        x["all"].append(r.duration)
        x["hours"].setdefault(local(r.timestamp).hour, []).append(r.duration)
    return {name: {"n": len(x["all"]), "typical": round(median(x["all"]), 1),
                   "hours": {str(h): round(median(v), 1) for h, v in sorted(x["hours"].items()) if len(v) >= MIN_STARTS}}
            for name, x in per.items()}


def summary_lines(day: date, saved: dict | None, events: dict, sev: dict, periods: list[dict]) -> list[str] | None:
    """A finished day in lines, from its saved counts (None: not saved yet)."""
    if not saved:
        return None
    out = [f"{day:%A %b %d}: {saved.get('pcs', 0):,} PCs active; {TOP_STATUS} slow actions on "
           f"{plural(saved.get('top_pcs', 0), 'PC')}."]
    top = sorted(((k, len(devs)) for k, devs in events.items() if sev.get(k) in SEVERITY_ORDER[:2] and devs),
                 key=lambda kv: (SEVERITY_ORDER.index(sev[kv[0]]), -kv[1], kv[0]))[:TOP]
    out += [f"{k[1]} ({sev[k]}): {plural(n, 'PC')}." for k, n in top] or ["No crash or event rated Critical or Major."]
    apps = sorted(((a, c.get(TOP_STATUS, 0)) for a, c in saved.get("apps", {}).items() if c.get(TOP_STATUS)),
                  key=lambda kv: (-kv[1], kv[0]))[:TOP]
    if apps:
        out.append(f"Applications with the most {TOP_STATUS} slow actions: "
                   + ", ".join(f"{a} ({n:,})" for a, n in apps) + ".")
    if periods:
        out.append("Assessments of that day: " + "; ".join(f"{p['label']}: {p['verdict']}" for p in periods) + ".")
    if not saved.get("complete"):
        out.append("Not every hour of that day was read, so its counts can be low.")
    return out


class Assessments:
    """The saved assessments and the one written when a period is due (`tick`, called by the
    Today reader after each read)."""

    def __init__(self, today, care=None, status=None, store: str | os.PathLike | None = None, aternity_of=None,
                 unit_of=lambda: "s"):
        self.today, self.care, self.status = today, care, status
        self.aternity_of, self.unit_of = aternity_of, unit_of  # the start-ups are read from Aternity (boots_of)
        self.boots_at: datetime | None = None
        self.store = Path(store) / "assess" if store else None
        self.days: dict[str, dict] = {}
        self.note: str | None = None
        self.lock = threading.Lock()
        self.load()

    # -- the files --------------------------------------------------------------
    def load(self) -> None:
        if not self.store or not self.store.is_dir():
            return
        for path in sorted(self.store.glob("*.json")):
            try:
                self.days[path.stem] = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                log.warning("Assessment: %s could not be read (%s)", path.name, short_error(exc))

    def _save(self, day: str, now: datetime) -> None:
        keep = (self.today.day(now) - timedelta(days=KEEP_DAYS - 1)).isoformat()
        for d in [d for d in self.days if d < keep]:
            del self.days[d]
        if not self.store:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            tmp = self.store / f".{day}.tmp"
            tmp.write_text(json.dumps(self.days[day], sort_keys=True))
            os.replace(tmp, self.store / f"{day}.json")
            for path in self.store.glob("*.json"):
                if path.stem < keep:
                    path.unlink()
            self.note = None
        except OSError as exc:
            self.note = (f"The assessments could not be saved in {self.store} ({short_error(exc)}). The folder must "
                         "be writable by the container (uid 1000).")
            log.warning("Assessment: %s", self.note)

    # -- writing ----------------------------------------------------------------
    def due(self, now: datetime) -> int | None:
        """The hour of `HOURS` an assessment is owed for now: the latest one passed today that
        has none yet (after a restart at 14:30 that is 12:00, written with the time it is)."""
        local = self.today.local(now)
        passed = [h for h in HOURS if local.hour >= h]
        if not passed:
            return None
        done = {p["slot"] for p in self.days.get(self.today.day(now).isoformat(), {}).get("periods", [])}
        return passed[-1] if passed[-1] not in done else None

    def tick(self, now: datetime, client_of=lambda: (None, "")) -> None:
        """After each read of the Today reader: write what is due. Never raises."""
        try:
            if not self.today.ready:
                return
            with self.lock:
                self._boots(now)
                self._yesterday(now, client_of)
                slot = self.due(now)
                if slot is not None:
                    self._write(now, slot, client_of)
        except Exception as exc:  # the reader goes on; the next read tries again
            log.warning("Assessment: could not be written (%s)", short_error(exc))

    def _write(self, now: datetime, slot: int, client_of) -> None:
        day = self.today.day(now).isoformat()
        rec = self.days.setdefault(day, {"date": day, "periods": [], "summary": None})
        before = rec["periods"][-1] if rec["periods"] else None
        snap = snapshot(self.today.view(now), self.care.view(now) if self.care else None,
                        self.status.view() if self.status else None, self.boots_view(now))
        label = self.today.local(now).strftime("%H:%M")
        f = facts(snap, before["snap"] if before else None, before["label"] if before else None)
        client, service = client_of()
        text, note = word(client, f, label, service)
        rec["periods"].append({"slot": slot, "label": label, "at": now.isoformat().replace("+00:00", "Z"), **f,
                               "text": text, "note": note, "service": service if text else "", "snap": snap})
        self._save(day, now)
        log.info("Assessment written at %s: %s; %d new since %s; %s", label, f["verdict"], len(f["new"]),
                 f["since"] or "the start of the day", f"worded by {service}" if text else note)

    def _yesterday(self, now: datetime, client_of) -> None:
        """The day before, once, when its counts are saved (after midnight, late rows in)."""
        t = self.today.day(now)
        y = t - timedelta(days=1)
        rec = self.days.get(y.isoformat())
        if rec and rec.get("summary"):
            return
        with self.today.lock:
            saved = self.today.saved.get(y)
            events = {k: set(v) for k, v in self.today.health_days.get(y, {}).items()}
            sev = {k: c.most_common(1)[0][0] for k, c in self.today.health_sev.items() if c}
        lines = summary_lines(y, saved, events, sev, (rec or {}).get("periods", []))
        if lines is None:
            return
        rec = self.days.setdefault(y.isoformat(), {"date": y.isoformat(), "periods": [], "summary": None})
        f = {"verdict": "The day in short", "why": [], "since": None, "new": [], "nothing_new": False, "today": lines,
             "open": [], "first": "today's first assessment, for what carries over"}
        client, service = client_of()
        text, note = word(client, f, f"the end of {y:%A %b %d}", service)
        rec["summary"] = {"label": f"{y:%a %b %d}", "lines": lines, "text": text, "note": note,
                          "service": service if text else ""}
        self._save(y.isoformat(), now)
        log.info("Assessment: the summary of %s written (%s)", y.strftime("%b %d"),
                 f"worded by {service}" if text else note)

    # -- start-ups (boots and logons) ---------------------------------------------
    def _same_days(self, t: date) -> list[date]:
        """The days today is compared with: the same kind (working / weekend), latest first."""
        return [d for d in (t - timedelta(days=n) for n in range(1, 15)) if kind_of(d) == kind_of(t)][:USUAL_DAYS]

    def _boots(self, now: datetime) -> None:
        """Read today's start-ups again when the last read is old, and once each day before
        that the usual needs. Aternity not set, or not answering: left as it is, tried again."""
        if self.aternity_of is None or (self.boots_at and now - self.boots_at < BOOTS_EVERY):
            return
        client = self.aternity_of()
        if client is None:
            return
        t = self.today.day(now)
        for d in self._same_days(t)[::-1] + [t]:
            rec = self.days.setdefault(d.isoformat(), {"date": d.isoformat(), "periods": [], "summary": None})
            if d != t and rec.get("boots") is not None:
                continue
            start = self.today.midnight(d)
            mark = len(client.warnings)
            rows = client.boots(start, now if d == t else start + timedelta(days=1))
            if len(client.warnings) > mark:  # not read (or cut short): no typical from part of a day
                log.warning("Assessment: the start-ups of %s could not be read (%s)", d.strftime("%b %d"),
                            client.warnings[-1])
                return
            rec["boots"] = boots_of(rows, self.today.local)
            self._save(d.isoformat(), now)
        self.boots_at = now
        log.info("Assessment: start-ups read up to %s (%d today)", self.today.local(now).strftime("%H:%M"),
                 sum(x["n"] for x in self.days[t.isoformat()]["boots"].values()))

    def boots_view(self, now: datetime) -> dict | None:
        """Today's start-ups per type, most first, each with the days before: the lowest and
        highest typical time of those days (`MIN_USUAL` needed)."""
        t = self.today.day(now)
        mine = self.days.get(t.isoformat(), {}).get("boots")
        if not mine:
            return None
        before = [self.days.get(d.isoformat(), {}).get("boots") or {} for d in self._same_days(t)]
        types = {}
        for name, x in sorted(mine.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
            if x["n"] < MIN_STARTS:
                continue
            past = [b[name]["typical"] for b in before if name in b and b[name]["n"] >= MIN_STARTS]
            types[name] = {"n": x["n"], "typical": x["typical"],
                           "usual": {"low": min(past), "high": max(past), "days": len(past)}
                           if len(past) >= MIN_USUAL else None}
        return {"unit": self.unit_of(), "types": types} if types else None

    # -- the charts: today up to this hour against the days before ------------------
    def charts(self, now: datetime) -> list[dict]:
        """Three lines, one point per hour of today so far, each with the range of the same hour
        on the days before (`MIN_USUAL` of them needed for a range)."""
        t, today = self.today.day(now), self.today
        cur = today.local(now).hour
        with today.lock:
            hours = {h.hour: h for h in today.hours.values() if h.day == t}
            saved = dict(today.saved)
            crashes = [(x[0], x[1]) for x in today.health if today.day(x[0]) == t and "crash" in x[2][1].lower()]
            health_days = {d: v for d, v in today.health_days.items() if d < t}
        same = [d for d in self._same_days(t) if saved.get(d, {}).get("complete")]

        def band(values: list) -> tuple:
            got = [v for v in values if v is not None]
            return (min(got), max(got)) if len(got) >= MIN_USUAL else (None, None)

        def share(counts: dict | None):
            total = sum(counts.values()) if counts else 0
            return round(100 * counts.get(TOP_STATUS, 0) / total, 1) if total >= MIN_ACTIONS else None
        share_pts = [{"h": h, "v": share(dict(hours[h].status)) if h in hours and h <= cur else None,
                      **dict(zip(("lo", "hi"), band([share(saved[d].get("hours", {}).get(str(h))) for d in same])))}
                     for h in range(24)]

        def so_far(first: dict, h: int) -> int:  # PCs whose first crash of the day was before the end of hour h
            return sum(1 for secs in first.values() if secs < (h + 1) * 3600)
        mine: dict = {}
        for when, dev in crashes:
            secs = (when - today.midnight(t)).total_seconds()
            mine[dev] = min(mine.get(dev, secs), secs)
        past = []
        for d in [d for d in self._same_days(t) if d in health_days]:
            first: dict = {}
            for k, devs in health_days[d].items():
                if "crash" in k[1].lower():
                    for dev, secs in devs.items():
                        first[dev] = min(first.get(dev, secs), secs)
            past.append(first)
        crash_pts = [{"h": h, "v": so_far(mine, h) if h <= cur else None,
                      **dict(zip(("lo", "hi"), band([so_far(f, h) for f in past])))} for h in range(24)]

        out = [{"key": "share", "title": f"Share of actions rated {TOP_STATUS}, per hour", "unit": "%", "points": share_pts},
               {"key": "crash", "title": "PCs with a crash, so far today", "unit": "PCs", "points": crash_pts}]
        boots = self.days.get(t.isoformat(), {}).get("boots") or {}
        name = next((n for n in sorted(boots, key=lambda n: -boots[n]["n"]) if "logon" in n.lower()),
                    max(boots, key=lambda n: boots[n]["n"], default=None))
        if name:
            before = [self.days.get(d.isoformat(), {}).get("boots") or {} for d in self._same_days(t)]
            out.append({"key": "logon", "title": f"Typical {name} time, per hour", "unit": self.unit_of(), "points": [
                {"h": h, "v": boots[name]["hours"].get(str(h)) if h <= cur else None,
                 **dict(zip(("lo", "hi"), band([b.get(name, {}).get("hours", {}).get(str(h)) for b in before])))}
                for h in range(24)]})
        for c in out:  # the last hour with a value: the number beside the chart
            last = next((p for p in reversed(c["points"]) if p["v"] is not None), None)
            c["now"] = last
        return out

    def kpis(self, now: datetime, charts: list[dict]) -> list[dict]:
        """The four numbers under the banner."""
        v = self.today.view(now)
        if not v.get("ready"):
            return []
        r, s = v["active"]["recent"], v["slow"]
        out = [{"label": f"Active since {r['since']}", "big": f"{plural(r['users'], 'user')}",
                "sub": f"{plural(r['pcs'], 'PC')}: {r['virtual']:,} virtual, {r['physical']:,} physical"},
               {"label": f"{TOP_STATUS} slow actions today", "big": f"{s['actions']:,}",
                "sub": f"on {plural(s['pcs'], 'PC')}", "usual": _usual(s.get("usual"))}]
        crash = next((c["now"] for c in charts if c["key"] == "crash"), None)
        if crash:
            lo, hi = crash["lo"], crash["hi"]
            usual = None if lo is None else ("more than usual" if crash["v"] > hi else "fewer than usual"
                                             if crash["v"] < lo else "as usual")
            out.append({"label": "PCs with a crash today", "big": f"{crash['v']:,}", "usual": usual,
                        "sub": f"the days before by now: {lo:,} to {hi:,}" if lo is not None else ""})
        b = self.boots_view(now)
        if b:
            name, x = next(((n, x) for n, x in b["types"].items() if "logon" in n.lower()), next(iter(b["types"].items())))
            u = x["usual"]
            out.append({"label": f"Typical {name}", "big": f"{x['typical']:g} {b['unit']}",
                        "sub": plural(x["n"], "start-up") + " today"
                        + (f"; the days before: {u['low']:g} to {u['high']:g} {b['unit']}" if u else ""),
                        "usual": None if not u else "longer than usual" if x["typical"] > u["high"] else
                        "shorter than usual" if x["typical"] < u["low"] else "as usual"})
        return out

    # -- the page ---------------------------------------------------------------
    def view(self, now: datetime) -> dict:
        """What /api/assess returns: today's assessments (latest first), yesterday's summary."""
        t = self.today.day(now)
        with self.lock:
            rec = self.days.get(t.isoformat(), {})
            y = self.days.get((t - timedelta(days=1)).isoformat(), {})
            periods = [{k: v for k, v in p.items() if k != "snap"} for p in rec.get("periods", [])][::-1]
        nxt = next((h for h in HOURS if self.today.local(now).hour < h), None)
        charts = self.charts(now) if self.today.ready else []
        return {"date": t.isoformat(), "zone": self.today.zone_label, "periods": periods,
                "yesterday": y.get("summary"), "hours": list(HOURS),
                "next": f"{nxt:02d}:00" if nxt is not None else None, "note": self.note,
                # the live numbers and charts are as of the Today reader's last read, not of the text's hour
                "now": self.today.local(self.today.as_of or now).strftime("%H:%M"), "charts": charts,
                "kpis": self.kpis(now, charts) if self.today.ready else []}


# -- what `verify --ai` measures with (made-up names and numbers) --------------------------------
SAMPLE = {"verdict": WORSE, "why": ["Sample Viewer crash (Major) is on 41 PCs, more than usual"], "since": "09:00",
          "nothing_new": False,
          "new": ["Sample Viewer crash (Major): 41 PCs, more than usual; it was 12 at the last assessment.",
                  "Major slow actions added since the last assessment, by application: Sample ERP (212), "
                  "Sample Mail (96)."],
          "today": ["Active: 3,310 users on 3,905 PCs (2,650 virtual desktops, 1,255 physical PCs).",
                    "Major slow actions: 1,870 on 644 PCs, as usual.",
                    "Sample Agent crash (Critical): 7 PCs, as usual.",
                    "Applications with the most Major slow actions: Sample ERP (540), Sample Mail (410)."],
          "open": ["Sample Agent crash (Critical): 7 PCs, as usual."],
          "first": "Sample Viewer crash on 41 PCs (Major, more than usual): on the Today tab, click its tile for who "
                   "has it"}
SAMPLE_WORDS = ("sample viewer", "41")  # a right text has what is new
