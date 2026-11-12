"""Ask anything: the open chat on the Today tab (step 1; the user's request 2026-11-12: "HOW DO
WE MAKE THIS LIKE NORMAL REGULAR CHAT", after "What PCs had hard drive issue this week?" was
answered "no event named Hard drive" by the block chat).

The AI looks things up with general tools, as many steps as it needs (`MAX_STEPS`), sees the
records themselves (rows, with a summary of the counts), and answers in its own words. Every
step and its rows go back to the page ("What I looked at"); a number in the answer that is in
none of them is marked, not hidden (`flagged`). The tools read only what this app already holds
(the Today reader, the names, our meanings): no extra read of Aternity.

Step 2 (the user, 2026-11-12: "build step 2"): look-ups that read Aternity now, given what they
need in a `Reach` (web.py): a user's or PC's logons (chat.logins), start-ups and why they were
slow (logons.logon_report), a PC's installed software and Windows, what changed on a PC (the
change log as an investigation reads it, cause._updates), the Software tab's look-ups
(software_chat's blocks, as they are) and a whole investigation (one per question). At most
`MAX_READS` of them per question; the page runs a question as a job and shows each step.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .ai import AiClient
from .chat import LOGIN_DAYS, _kinds_named, _match, _named, _singular, allowed, logins, numbers
from .config import OS_COLUMNS, SOFTWARE_COLUMNS, SOFTWARE_OS_UPDATE, SOFTWARE_SOURCE, TOP_STATUS
from .http import ApiError, short_error
from .today import EVENT_PAD, HEALTH_DAYS, NO_APP, SLOW_PAD, Today

log = logging.getLogger("uxcorrelate")

MAX_STEPS = 8  # tool calls per question
ROWS_MAX = 150  # rows of one step, given to the AI and shown
RESULT_CHARS = 24000  # one step as given to the AI, at most
HISTORY = 6  # earlier questions and answers sent with a new one
ANSWER_MAX = 4000
KINDS_MAX = 80  # kinds of event named in the instructions, the most PCs first
FIND_MAX = 20  # names of each sort a find returns
MAX_READS = 5  # look-ups that read Aternity now, per question
MAX_INVESTIGATIONS = 1  # whole investigations per question (the slowest look-up)
INVESTIGATE_HOURS = 4  # an investigation's window when none is asked for (as Investigate ▸ on Today)
INVESTIGATE_MAX_H = 24  # an investigation's window, at most: longer ones on the Investigate tab
STARTUP_DAYS = 7  # start-ups looked at when no number of days is asked for (at most LOGIN_DAYS)
CHANGE_DAYS = 14  # a PC's change log looked at, at most (as an investigation's "what changed")
TEXT_CHARS = 12000  # an investigation's report as given to the AI, at most
PER_PC_KEEP_MIN = 60  # programs per PC, counted once and kept this long (every PC's list may be read)
PER_PC_ROWS = 50  # PCs listed, the most (or fewest) first
_per_pc: dict = {}  # Aternity's address -> (when counted, the count, its notes)


@dataclass
class Reach:
    """What the look-ups that read Aternity now are given (web.py). Without one, they say
    it is not set: the Today reader's look-ups still answer."""
    aternity_of: Callable | None = None  # -> an AternityClient, or None when Aternity is not set
    cfg: object = None  # settings.aternity
    software: object = None  # the Software tab's reader (software.Software)
    known: list = field(default_factory=list)  # application names, for the Software tab's look-ups
    investigate: Callable | None = None  # (username, device, application, start, end) -> the result
    boot_unit: str = ""  # settings.boot_unit

SYSTEM = (
    "You are the assistant in UX Correlator, a tool the organisation's IT staff use to see how its PCs, users "
    "and applications are doing, from Riverbed Aternity's records. Answer as a knowledgeable colleague would, "
    "in plain English.\n"
    "How to work:\n"
    "- Look things up with the tools before you answer, as many steps as you need. Never make up a name, a "
    "count or a time: every fact comes from a tool's result. When the data does not have something, say so "
    "plainly and say what it does have.\n"
    "- A name or word in a question (a user, a PC, an application, a kind of event): use find to see what it "
    "is in the data. Kinds of event have Aternity's own names: choose the ones that mean what was asked (a "
    "hard drive is \"HD\" there, for example).\n"
    "- Counts: when a result has a summary, take its numbers; do not count rows yourself.\n"
    "- When a question is unclear, answer what you can and ask one short question back.\n"
    "- Lead with the answer, then the few facts behind it. Name users, PCs and applications as written. "
    "Plain text; a short list with \"- \" is fine; no tables, no headings.\n"
    "- Critical, Major and Minor are Aternity's own ratings. Say \"usual\" or \"unusual\" only as a result "
    "says it.\n"
    "What the data holds, at once: health events (crashes, hangs, hardware faults and more) today with user, PC "
    "and application, and on the days before by PC and application (no user); today's actions Aternity measured "
    "(each user's PCs and number of actions, and every action Aternity rated Major with its time, PC, user, "
    "application and activity); today's overview against the usual of the days before; what this app's words "
    "mean.\n"
    f"Read from Aternity when you ask, slower (at most {MAX_READS} per question): a user's or PC's logons "
    "(logons); their start-ups, how long each took against their own usual and why (startups); a PC's installed "
    "software and Windows (pc_software), what was installed, updated or removed on it (pc_changes), and how "
    "many programs every PC has (programs_per_pc); "
    "installed software and application versions across all PCs (flags, most, versions, version_problems, "
    "compare, before_after, program_split, pcs, pcs_without_update); a whole investigation of a user or PC "
    f"(investigate: at most {MAX_INVESTIGATIONS} per question and the slowest; use it when asked why someone "
    "was slow or what went wrong for them, with the window asked about, else the last "
    f"{INVESTIGATE_HOURS} hours; its answer is the app's own, tell it as written).\n"
    "- \"Tell me about\" a user or PC: what they did today (activity) and their events first, then their "
    "logons or software when that helps; an investigation only when asked why or what went wrong.\n"
    "- NetIM (the network) is read only within an investigation."
)

# a worked-out comparison ("twice as many", "3 times worse"); "twice" alone is a count (Nemotron's
# "opened Sample Mail twice", 2 rows, was marked on stage, Nov 2026)
COMPARED = re.compile(r"\b(?:\d[\d.,]*|two|three|four|five|six|ten)\s*(?:x|times)\s+(?:more|less|fewer|worse|"
                      r"better|higher|lower|slower|faster|as)\b|\b(?:twice|double|triple|half)\s+(?:as|the|that)\b",
                      re.I)

_S = {"type": "string"}


def _tool(name: str, description: str, props: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": props}}}


TOOLS = [
    _tool("find", "What a name or word is in the data: the users, PCs, applications and kinds of event it matches "
                  "(any part of a name, any case). With no kind of event or application matched, the result "
                  "lists those there are, to choose from by meaning.", {"text": _S}),
    _tool("events", "Health events, as rows: when, user, PC, event, severity, application, with a summary (PCs "
                    "per event, per day, per application). Any filter can be left out.", {
        "kinds": {"type": "array", "items": _S, "description": "Kinds of event, by Aternity's names (from find "
                                                               "or the instructions)."},
        "app": {"type": "string", "description": "Only events of this application."},
        "user": _S, "pc": _S,
        "severity": {"type": "string", "enum": ["Critical", "Major", "Minor"]},
        "days": {"type": "integer", "description": f"How many days, today included: 1 = today, 7 = this week "
                                                   f"(at most {HEALTH_DAYS + 1})."}}),
    _tool("activity", "Today's actions Aternity measured: for a user (their PCs, how many actions, those rated "
                      "Major), a PC (who used it, its Major actions), an application (runs and Major per hour, "
                      "virtual desktops and physical PCs) or, with nothing given, everyone (per hour, the "
                      "applications with the most Major). Major actions come as rows with a summary.",
          {"user": _S, "pc": _S, "app": _S}),
    _tool("overview", "Today's tab as it stands: PCs and users with Major slow actions and the usual by this "
                      "time, per hour against usual, the applications with the most, the Critical and Major "
                      "health events with their PCs against usual, and how many users and PCs are active.", {}),
    _tool("meanings", "What a word on this app's pages means (as written by this app), e.g. Major, usual, "
                      "virtual desktop. With no term: the list of terms.", {"term": _S}),
    # step 2: read from Aternity now
    _tool("logons", f"Who logged in to a PC, or which PCs a user logged in to, from Aternity's start-up and logon "
                    f"records, read now: the latest (the last {LOGIN_DAYS} days) or one day. current=true: where "
                    "a user is working now, or who is on a PC now (the latest hour of today with an action Aternity "
                    "measured; nothing is read).", {
        "user": _S, "pc": _S, "date": {"type": "string", "description": "One day, as YYYY-MM-DD."},
        "current": {"type": "boolean"}}),
    _tool("startups", "A user's or a PC's start-ups (boots and logons), read now: how long each took against their "
                      "own usual of the last 30 days, the slowest first, the parts that took the time, why (software "
                      "changed just before, the same part slow on every PC) and since when they are slow.", {
        "user": _S, "pc": _S,
        "days": {"type": "integer", "description": f"How many days back (1 to {LOGIN_DAYS}; {STARTUP_DAYS} if not "
                                                   "given)."}}),
    _tool("pc_software", "One PC's installed software, read now: each program with its version, vendor, type "
                         f"(Windows updates are type \"{SOFTWARE_OS_UPDATE}\"), whether it is for one user only, and "
                         "the PC's Windows version.", {
        "pc": _S, "program": {"type": "string", "description": "Only the programs named like this."}}),
    _tool("pc_changes", "What was installed, updated or removed on one PC, from Aternity's change log, read now, "
                        "newest first; an update is its removal and install together.", {
        "pc": _S, "program": {"type": "string", "description": "Only the programs named like this."},
        "days": {"type": "integer", "description": f"How many days back (1 to {CHANGE_DAYS}; {CHANGE_DAYS} if not "
                                                   "given)."}}),
    _tool("programs_per_pc", "How many programs each PC has installed, across every PC, read now: the PCs with "
                             "the most (or the fewest), the typical number, Windows updates apart. kind: only virtual "
                             "desktops or only physical PCs (known for the PCs with an action today).", {
        "order": {"type": "string", "enum": ["most", "fewest"]},
        "kind": {"type": "string", "enum": ["virtual", "physical"]}}),
    _tool("investigate", "A whole investigation of a user or a PC, as the Investigate tab runs it: what the user "
                         "felt, what went wrong and why (PC, network, application, software changes, other users "
                         "and PCs), and what to do. It takes up to a few minutes. Times are local, as YYYY-MM-DD "
                         f"HH:MM; with none, the last {INVESTIGATE_HOURS} hours; at most {INVESTIGATE_MAX_H} hours.", {
        "user": _S, "pc": _S, "app": {"type": "string", "description": "Only this application's actions."},
        "start": _S, "end": _S,
        "hours": {"type": "integer", "description": "Instead of start and end: this many hours back from now."}}),
]

NOW = {"logons", "startups", "pc_software", "pc_changes", "programs_per_pc", "investigate"}  # read Aternity now (logons: unless current)


def _software_tools() -> list[dict]:
    """The Software tab's look-ups (software_chat.TOOLS), as they are, said to be about software."""
    from copy import deepcopy

    from .software_chat import TOOLS as SW
    out = []
    for t in deepcopy(SW):
        fn = t["function"]
        fn["description"] = "Installed software and application versions (the Software tab). " + fn["description"]
        out.append(t)
    return out


SOFTWARE_TOOLS = _software_tools()
SOFTWARE = {t["function"]["name"] for t in SOFTWARE_TOOLS}
# those that read Aternity now (the others answer from the Software tab's latest read)
SOFTWARE_NOW = {"version_problems", "compare", "before_after", "program_split", "pcs", "pcs_without_update"}
TOOLS += SOFTWARE_TOOLS


# -- the tools ---------------------------------------------------------------------
def _step(tool: str, args: dict, title: str, columns=(), rows=(), summary=None, notes=(), runs=None) -> dict:
    rows = list(rows)
    out = {"tool": tool, "args": args, "title": title, "columns": list(columns), "rows": rows[:ROWS_MAX],
           "total_rows": len(rows), "notes": list(notes)}
    if summary is not None:
        out["summary"] = summary
    if runs is not None:
        out["runs"] = runs[:ROWS_MAX]
    if len(rows) > ROWS_MAX:
        out["notes"].append(f"The first {ROWS_MAX} rows of {len(rows):,} (newest first); the summary counts them all.")
    return out


def _iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _like(names, text: str) -> list[str]:
    """Names with the text in them, or every one of its words (a plural as one): any case."""
    q = " ".join(str(text or "").lower().split())
    if not q:
        return []
    ws = [w for w in _singular(q).split() if w]
    return [n for n in names if q in n.lower() or (ws and all(w in n.lower() for w in ws))]


def _users(today: Today, t) -> dict:
    """Today's users (lower case) -> PCs."""
    out: dict = {}
    for h in today.hours.values():
        if h.day == t:
            for u, ds in h.user_devs.items():
                out.setdefault(u, set()).update(ds)
    return out


def _app_names(today: Today) -> Counter:
    """Every application this app holds a name of, by how often it is seen."""
    c: Counter = Counter()
    for h in today.hours.values():
        for a, runs in h.apps.items():
            c[a] += runs["runs"]
    for x in today.health:
        c[x[5]] += 1
    for kinds in today.health_apps.values():
        for (_, a), devs in kinds.items():
            c[a] += len(devs)
    for s in today.saved.values():
        for a, v in (s.get("apps") or {}).items():
            c[a] += v.get("runs", 0)
    c.pop(NO_APP, None)
    return c


def event_kinds(today: Today) -> Counter:
    """The kinds of event recorded today and the days before -> PCs (by day)."""
    pcs: Counter = Counter()
    for x in today.health:
        pcs[x[2]] += 1
    for kinds in today.health_days.values():
        for k, devs in kinds.items():
            pcs[k] += len(devs)
    return pcs


def find(today: Today, names, now: datetime, text: str = "") -> dict:
    args = {"text": text}
    with today.lock:
        kinds = event_kinds(today)
        apps = _app_names(today)
    users, pcs = [], []
    if names:
        with names.lock:
            users, pcs = list(names.seen["username"]), list(names.seen["device"])
    rows, notes = [], []
    for sort, found in (("user", _like(users, text)), ("PC", _like(pcs, text)),
                        ("application", sorted(_like(apps, text), key=lambda a: -apps[a])),
                        ("kind of event", [k[1] for k in sorted(_like_kinds(kinds, text), key=lambda k: -kinds[k])])):
        for n in found[:FIND_MAX]:
            rows.append([sort, n])
        if len(found) > FIND_MAX:
            notes.append(f"{len(found):,} {sort} names match; the first {FIND_MAX} are listed.")
    summary = {"matched": dict(Counter(r[0] for r in rows)) or "nothing by name"}
    if not any(r[0] == "kind of event" for r in rows):
        summary["kinds_of_event_there_are"] = [k[1] for k, _ in kinds.most_common(KINDS_MAX)]
    if not any(r[0] == "application" for r in rows):
        summary["applications_most_seen"] = [a for a, _ in apps.most_common(40)]
    if names:
        notes.append("Users and PCs: those seen in Aternity's activity in the last 8 days.")
    return _step("find", args, f"What \"{text}\" is in the data", ["Sort", "Name"], rows, summary, notes)


def _like_kinds(kinds, text: str) -> list:
    names = {k[1]: k for k in kinds}
    return [names[n] for n in _like(names, text)]


def _when(today: Today, t: datetime) -> str:
    return f"{today.local(t):%a %b %d %H:%M}"


def events(today: Today, now: datetime, kinds=None, app: str | None = None, user: str | None = None,
           pc: str | None = None, severity: str | None = None, days=1) -> dict:
    try:
        days = max(1, min(int(days or 1), HEALTH_DAYS + 1))
    except (TypeError, ValueError):
        days = 1
    if isinstance(kinds, str):
        kinds = [kinds]
    args = {"kinds": kinds, "app": app, "user": user, "pc": pc, "severity": severity, "days": days}
    t = today.day(now)
    first = t - timedelta(days=days - 1)
    with today.lock:
        health = [x for x in today.health if today.day(x[0]) == t]
        hd = {d: v for d, v in today.health_days.items() if first <= d < t}
        ha = {d: v for d, v in today.health_apps.items() if first <= d < t}
        read = {d for d in today.health_read if first <= d < t}
        sev = {k: c.most_common(1)[0][0] for k, c in today.health_sev.items() if c}
    notes = []
    every = {x[2] for x in health} | {k for v in hd.values() for k in v}
    wanted, loose = _kinds_named(every, " | ".join(str(k) for k in kinds if k)) if kinds else (every, [])
    notes += loose
    if severity:
        wanted = {k for k in wanted if (sev.get(k) or "").lower() == severity.lower()}
    app_names = {x[5] for x in health} | {a for v in ha.values() for (_, a) in v}
    apps = set(_match(app_names, app)) if app else None
    if app and not apps:
        notes.append(f"No application named like \"{app}\" has an event in this period.")
    pc_l = (pc or "").strip().lower()
    user_l = (user or "").strip().lower()
    rows, runs = [], []
    for x in sorted(health, key=lambda x: x[0], reverse=True):
        if x[2] not in wanted or (apps is not None and x[5] not in apps):
            continue
        if pc_l and x[1].lower() != pc_l or user_l and (x[4] or "").lower() != user_l:
            continue
        rows.append([_when(today, x[0]), x[4], x[1], x[2][1], sev.get(x[2]), x[5]])
        runs.append({"username": x[4] or "", "device": x[1], "start": _iso(x[0] - EVENT_PAD),
                     "end": _iso(min(x[0] + EVENT_PAD, now))})
    if user_l and days > 1:
        notes.append("The days before keep the PC, not the user: only today's events are matched by user.")
    for d in sorted(hd, reverse=True):
        on = {}  # (kind, PC) -> applications, that day
        for (k, a), devs in ha.get(d, {}).items():
            for dev in devs:
                on.setdefault((k, dev), set()).add(a)
        day_rows = []
        for k, devs in hd[d].items():
            if k not in wanted:
                continue
            for dev, secs in devs.items():
                a = on.get((k, dev), set())
                if apps is not None and not a & apps or pc_l and dev.lower() != pc_l or user_l:
                    continue
                when = today.midnight(d) + timedelta(seconds=secs)
                day_rows.append((when, [_when(today, when), None, dev, k[1], sev.get(k),
                                        ", ".join(sorted(a)) or None]))
        for when, r in sorted(day_rows, key=lambda x: x[0], reverse=True):
            rows.append(r)
            runs.append({"username": "", "device": r[2], "start": _iso(when - EVENT_PAD),
                         "end": _iso(when + EVENT_PAD)})
    missing = days - 1 - len(read)
    if missing > 0:
        notes.append(f"{missing} of the {days - 1} days before were not read yet (read once a day).")
    if days > 1:
        notes.append("Today: one row per event. Days before: one row per PC, kind of event and day, at its first "
                     "time that day.")
    by_event, by_day, by_app = {}, {}, {}
    for r in rows:
        by_event.setdefault(r[3], set()).add(r[2])
        by_day.setdefault(r[0][:10], set()).add(r[2])
        for a in (r[5] or NO_APP).split(", "):
            by_app.setdefault(a, set()).add(r[2])
    summary = {"rows": len(rows), "pcs": len({r[2] for r in rows}), "users_today": len({r[1] for r in rows if r[1]}),
               "pcs_by_event": {k: len(v) for k, v in sorted(by_event.items(), key=lambda kv: -len(kv[1]))},
               "pcs_by_day": {k: len(v) for k, v in by_day.items()},
               "pcs_by_application": {k: len(v) for k, v in sorted(by_app.items(), key=lambda kv: -len(kv[1]))[:10]}}
    period = "today" if days == 1 else f"{days} days, today included"
    what = ", ".join(kinds) if kinds else "all kinds"
    return _step("events", args, f"Health events: {what}, {period}", ["When", "User", "PC", "Event", "Severity",
                                                                     "Application"], rows, summary, notes, runs)


def activity(today: Today, now: datetime, user: str | None = None, pc: str | None = None,
             app: str | None = None) -> dict:
    args = {"user": user, "pc": pc, "app": app}
    t = today.day(now)
    with today.lock:
        hours = sorted((h for h in today.hours.values() if h.day == t), key=lambda h: h.start)
        users = _users(today, t)
    notes = ["Today only, from midnight; actions not rated Major are counted, not listed."]
    summary: dict = {}
    user_l, pc_l = (user or "").strip().lower(), (pc or "").strip().lower()
    if user_l and user_l not in users:
        near = [u for u in users if user_l in u][:5]
        notes.append(f"No user \"{user}\" had a measured action today" + (f"; names like it: {', '.join(near)}."
                                                                         if near else "."))
    if pc_l and not any(pc_l in {d.lower() for d in h.devs} for h in hours):
        notes.append(f"No PC \"{pc}\" had a measured action today.")
    names = {a for h in hours for a in h.apps}
    apps = set(_match(names, app)) if app else None
    if app and not apps:
        notes.append(f"No application named like \"{app}\" had a measured action today.")
    elif app and len(apps) > 1:
        notes.append(f"\"{app}\" matches {len(apps)} applications, counted together: {', '.join(sorted(apps)[:5])}.")
    rows, runs = [], []
    for h in reversed(hours):
        for r in sorted(h.top_rows, key=lambda r: r[0], reverse=True):
            if user_l and (r[2] or "").lower() != user_l or pc_l and r[1].lower() != pc_l:
                continue
            if apps is not None and r[3] not in apps:
                continue
            rows.append([_when(today, r[0]), r[2], r[1], r[3], r[4], r[5]])
            runs.append({"username": r[2] or "", "device": r[1], "application": r[3],
                         "start": _iso(r[0] - SLOW_PAD), "end": _iso(min(r[0] + SLOW_PAD, now))})
    if user_l:
        devs = sorted(users.get(user_l, set()))
        kind = {d: h.dev_kind.get(d) for h in hours for d in devs if d in h.dev_kind}
        summary.update(pcs_used=[f"{d} ({kind.get(d) or 'not known'})" for d in devs],
                       actions_measured=sum(h.user_runs.get(user_l, 0) for h in hours),
                       actions_per_hour={f"{today.local(h.start):%H}:00": h.user_runs[user_l]
                                         for h in hours if h.user_runs.get(user_l)})
    if pc_l:
        on = sorted(u for u, ds in users.items() if pc_l in {d.lower() for d in ds})
        kind = next((h.dev_kind[d] for h in hours for d in h.dev_kind if d.lower() == pc_l), None)
        summary.update(users_on_it=on, kind=kind or "not known")
        notes.append("Per PC, only its Major actions are listed: the count of all actions is kept per user.")
    if apps is not None:
        per_hour, by_kind = {}, {}
        for h in hours:
            c = Counter()
            for a in apps:
                c.update(h.apps.get(a, Counter()))
                for (a2, k), v in h.kinds.items():
                    if a2 == a:
                        by_kind.setdefault(k, Counter()).update(v)
            if c["runs"]:
                per_hour[f"{today.local(h.start):%H}:00"] = {"runs": c["runs"], TOP_STATUS: c[TOP_STATUS]}
        summary.update(runs=sum(v["runs"] for v in per_hour.values()),
                       major=sum(v[TOP_STATUS] for v in per_hour.values()), per_hour=per_hour,
                       by_pc_kind={k: {"runs": v["runs"], TOP_STATUS: v[TOP_STATUS]} for k, v in by_kind.items()},
                       pcs_with_major=len({d for h in hours for a in apps for d in h.app_devs.get(a, set())}))
    if not (user_l or pc_l or apps is not None):
        top = Counter()
        for h in hours:
            for a, c in h.apps.items():
                top[a] += c[TOP_STATUS]
        summary.update(actions_measured=sum(h.rows for h in hours),
                       major=sum(h.status[TOP_STATUS] for h in hours),
                       per_hour={f"{today.local(h.start):%H}:00": {"actions": h.rows, TOP_STATUS: h.status[TOP_STATUS]}
                                 for h in hours},
                       applications_most_major=dict((a, n) for a, n in top.most_common(10) if n))
    summary["major_rows"] = len(rows)
    summary["major_pcs"] = len({r[2] for r in rows})
    summary["major_users"] = len({r[1] for r in rows if r[1]})
    summary["major_by_application"] = dict(Counter(r[3] for r in rows).most_common(10))
    who = ", ".join(x for x in (user, pc, app) if x) or "everyone"
    return _step("activity", args, f"Today's actions: {who}", ["When", "User", "PC", "Application", "Activity",
                                                              "Seconds"], rows, summary, notes, runs)


def overview(today: Today, now: datetime) -> dict:
    v = today.view(now)
    if not v.get("ready"):
        return _step("overview", {}, "Today's overview", notes=["Still reading Aternity."])
    keep = {"date": v["date"], "kind_of_day": v["kind"], "as_of": v["as_of_local"], "slow": v["slow"],
            "per_hour": [{k: h[k] for k in ("hour", "count", "usual")} for h in v["hours"]],
            "applications": v["applications"],
            "critical_and_major_events": [{k: e[k] for k in ("name", "category", "severity", "pcs", "usual")}
                                          for e in v["tiles"]],
            "events_on_most_pcs": [{k: e[k] for k in ("name", "category", "severity", "pcs", "usual")}
                                   for e in v["events"]],
            "active": v.get("active")}
    return _step("overview", {}, f"Today's overview, as of {v['as_of_local']}", summary=keep, notes=v["notes"])


def meanings(term: str | None = None) -> dict:
    from . import explain
    every = dict(explain.COMMON)
    for terms in explain.GLOSSARY.values():
        every.update(terms)
    q = (term or "").strip().lower()
    if not q:
        return _step("meanings", {"term": term}, "The terms", ["Term"], [[k] for k in sorted(every)])
    hits = [(k, v) for k, v in every.items() if q in k.lower()] or \
        [(k, v) for k, v in every.items() if q in v.lower()][:8]
    return _step("meanings", {"term": term}, f"What \"{term}\" means here", ["Term", "Meaning"],
                 [[k, v] for k, v in hits], notes=[] if hits else [f"No term like \"{term}\"."])


# -- step 2: read from Aternity now ----------------------------------------------------
def _block(tool: str, args: dict, r: dict | None) -> dict:
    """A block of the block chats (chat._result) as a step: its rows, total and our sentence."""
    if r is None:
        return _step(tool, args, "Not run", notes=["The settings could not be used."])
    summary = {k: v for k, v in (("period", r.get("period")), ("total", r.get("total")),
                                 ("in_short", r.get("say"))) if v}
    return _step(tool, args, r["title"], r["columns"], r["rows"], summary or None, r["notes"], r.get("runs"))


def _client(reach: Reach | None):
    return reach.aternity_of() if reach and reach.aternity_of else None


NOT_SET = "Aternity is not set here (aternity.base_url in config.yaml), so nothing can be read now."


def _one(names, kind: str, value, tool: str, args: dict) -> tuple[str | None, list[str], dict | None]:
    """A username or PC name as Aternity writes it (chat._named), or a step with the names to
    choose from when several have it."""
    name, said, choices = _named(names, kind, value)
    if choices:
        return None, said, _step(tool, args, f"Which {'user' if kind == 'username' else 'PC'}?", ["Name"],
                                 [[n] for n in choices], notes=[f"No {kind} is \"{value}\"; several have it in "
                                                                "their name: pick one, or ask which."])
    return name, said, None


def _days(v, default: int, most: int) -> int:
    try:
        return max(1, min(int(v or default), most))
    except (TypeError, ValueError):
        return default


def _kind_of(today: Today, pc: str) -> str | None:
    """A PC's kind (virtual / physical) from today's actions, when it had one."""
    with today.lock:
        return next((h.dev_kind[d] for h in today.hours.values() for d in h.dev_kind if d.lower() == pc.lower()),
                    None)


def startups(today: Today, names, now: datetime, reach: Reach | None, user: str | None = None,
             pc: str | None = None, days=None) -> dict:
    from .logons import logon_report
    days = _days(days, STARTUP_DAYS, LOGIN_DAYS)
    args = {"user": user, "pc": pc, "days": days}
    if not user and not pc:
        return _step("startups", args, "Whose start-ups?", notes=[
            "Name a user or a PC: everyone's start-ups are on the Slow start-ups tab."])
    picked, notes = {}, []
    for kind, value in (("username", user), ("device", pc)):
        name, said, ask_back = _one(names, kind, value, "startups", args)
        if ask_back:
            return ask_back
        notes += said
        picked[kind] = name
    client = _client(reach)
    if client is None:
        return _step("startups", args, "Start-ups", notes=[NOT_SET])
    start = now - timedelta(days=days)
    lg = logon_report(client, start, now, username=picked["username"], device=picked["device"],
                      unit=reach.boot_unit)
    u = f" {lg['unit']}" if lg.get("unit") else ""
    who = " on ".join(x for x in (picked["username"], picked["device"]) if x)
    rows, runs = [], []
    for st in lg["starts"]:
        t = datetime.fromisoformat(st["time"].replace("Z", "+00:00"))
        took = "; ".join(f"{k} {v['duration']}{u}" + (f" (usual {v['usual']}{u})" if v["usual"] is not None else "")
                         for k, v in st["values"].items())
        parts = "; ".join(f"{c['component']} {c['duration']}{u}" + (f", {c['degradation']}{u} longer than normal"
                                                                    if c["degradation"] else "")
                          for c in st["components"][:3])
        rows.append([_when(today, t), st["username"], st["device"], took, parts or None,
                     " ".join(st.get("why") or []) or None])
        runs.append({"username": st["username"] or "", "device": st["device"], "start": _iso(t - EVENT_PAD),
                     "end": _iso(min(t + EVENT_PAD, now))})
    # "1 in 10 over" only from 10 measured: with fewer it is worked out past the slowest one
    summary = {"start_ups": lg["total"],
               "per_type": {t["type"]: f"typical {t['typical']}{u}" + (f", 1 in 10 over {t['p90']}{u}"
                                                                      if t["count"] >= 10 else "")
                            + f", {t['count']} measured" for t in lg["types"]}}
    causes = [f"{c['component']}: longer than normal on {c['pcs']} PC(s), {c['times']} time(s)"
              for c in lg.get("causes", [])[:5]]
    if causes:
        summary["main_causes"] = causes
    notes += ["Slowest first. Usual: their own start-ups of the last 30 days (a user's virtual desktops "
              "together, as one).", "Unit: " + (lg["unit"] if lg.get("unit") else "not given by Aternity") + "."]
    notes += [f"Aternity: {w}" for w in lg.get("warnings", [])]
    step = _step("startups", args, f"Start-ups of {who}, the last {days} day{'s' if days > 1 else ''}",
                 ["When", "User", "PC", "Took", "Slow parts", "Why"], rows, summary, notes, runs)
    step["open"] = {"tab": "log", "username": picked["username"] or "", "device": picked["device"] or "",
                    "start": _iso(start), "end": _iso(now)}
    return step


def pc_software(today: Today, names, now: datetime, reach: Reach | None, pc: str | None = None,
                program: str | None = None) -> dict:
    from .aternity import odata_quote
    from .models import parse_time
    from .software import LIST_ROWS, PC_LIST, _Reads, order, windows_of
    args = {"pc": pc, "program": program}
    if not pc:
        return _step("pc_software", args, "Which PC?", notes=["Name a PC."])
    name, notes, ask_back = _one(names, "device", pc, "pc_software", args)
    if ask_back:
        return ask_back
    client = _client(reach)
    if client is None:
        return _step("pc_software", args, f"{name}'s software", notes=[NOT_SET])
    S, c = SOFTWARE_SOURCE, SOFTWARE_COLUMNS
    q = _Reads(client, notes)
    keys = [k for k in PC_LIST if q.has(S, c[k])]  # as programs_per_pc counts a PC's list
    got = q.rows(S, f"{name}'s installed software", filter=f"{c['device']} eq {odata_quote(name)}",
                 select=[c[k] for k in keys], top=LIST_ROWS)
    windows = {}
    cfg = reach.cfg
    inv = cfg.entities.get("inventory") if cfg else None
    dev = cfg.fields.get("inventory", {}).get("device") if cfg else None
    if inv and dev:
        cols = [x for x in OS_COLUMNS.values() if q.has(inv, x)]
        if cols:
            w = q.rows(inv, "Its Windows", filter=f"{dev} eq {odata_quote(name)}", select=[dev] + cols, top=1)
            if w:
                windows = {"windows": windows_of(w[0])}
                when = parse_time(w[0].get(OS_COLUMNS["updated"]))
                if when:
                    windows["windows_last_updated"] = f"{today.local(when):%a %b %d, %Y}"
    if got is None:
        return _step("pc_software", args, f"{name}'s software", summary=windows or None, notes=notes)
    rows = []
    for r in got:
        v = {k: r.get(c[k]) for k in keys}
        when = parse_time(v.get("changed")) if v.get("changed") else None
        rows.append([v.get("name"), v.get("version"), v.get("vendor"), v.get("type"), v.get("scope"),
                     f"{today.local(when):%b %d, %Y}" if when else None])
    rows.sort(key=lambda r: (r[3] == SOFTWARE_OS_UPDATE, str(r[0] or "").lower(), order(r[1])))
    every = len(rows)
    if program:
        like = set(_like([r[0] for r in rows if r[0]], program))
        rows = [r for r in rows if r[0] in like]
        if not rows:
            notes.append(f"No program named like \"{program}\" on {name} ({every:,} listed).")
    summary = {**windows, "programs": sum(1 for r in rows if r[3] != SOFTWARE_OS_UPDATE),
               "windows_updates": [r[0] for r in rows if r[3] == SOFTWARE_OS_UPDATE][:40]}
    if not got:
        notes.append(f"Aternity lists no installed software for {name}: is the name right?")
    notes.append("Aternity's installed list as it last read the PC; Last change is Aternity's own date.")
    return _step("pc_software", args, f"{name}'s installed software" + (f": {program}" if program else ""),
                 ["Program", "Version", "Vendor", "Type", "For", "Last change"], rows, summary, notes)


def pc_changes(today: Today, names, now: datetime, reach: Reach | None, pc: str | None = None,
               program: str | None = None, days=None) -> dict:
    from .cause import _updates, batch_removals
    days = _days(days, CHANGE_DAYS, CHANGE_DAYS)
    args = {"pc": pc, "program": program, "days": days}
    if not pc:
        return _step("pc_changes", args, "Which PC?", notes=["Name a PC."])
    name, notes, ask_back = _one(names, "device", pc, "pc_changes", args)
    if ask_back:
        return ask_back
    client = _client(reach)
    if client is None:
        return _step("pc_changes", args, f"What changed on {name}", notes=[NOT_SET])
    start = now - timedelta(days=days)
    mark = len(client.warnings)
    changes = _updates(client.software_changes(name, start, now), start, now)
    if len(client.warnings) > mark:
        notes.append(f"Aternity's change log could not be read in full ({client.warnings[-1]}).")
    if program:
        like = set(_like({c["name"] for c in changes if c["name"]}, program))
        changes = [c for c in changes if c["name"] in like]
    batch = batch_removals(changes)
    minute = lambda c: c["time"].replace(second=0, microsecond=0)  # noqa: E731
    rows, done = [], set()
    for c in changes:
        if id(c) in batch:  # one row, as an investigation's "What changed" (answer._changed)
            if minute(c) not in done:
                done.add(minute(c))
                gone = [x["name"] for x in changes if id(x) in batch and minute(x) == minute(c)]
                rows.append([_when(today, c["time"]), f"{len(gone)} programs: {', '.join(gone[:4])}"
                             + (f" and {len(gone) - 4} more" if len(gone) > 4 else ""), c["change"], None, None,
                             "logged in the same minute, with no install logged: not removals to look at one by one"])
            continue
        said = None
        if c.get("back"):
            said = ("comes and goes in Aternity's log" if c.get("flaps") else
                    (f"until {_when(today, c['until'])}: " if c.get("until") else "") + "no change in the end")
        rows.append([_when(today, c["time"]), c["name"], c["change"], c["version"], c.get("version_before"), said])
    real = [c for c in changes if not c.get("back") and id(c) not in batch]
    summary = {"changes": len(real), "by_change": dict(Counter(c["change"] for c in real).most_common()),
               "put_back_or_coming_and_going": sum(1 for c in changes if c.get("back")),
               "removed_in_a_batch": len(batch)}
    notes.append("Newest first; a removal and an install of one program within minutes are one update. Rows put "
                 "back later, and a batch of removals in one minute, are not changes to look at one by one.")
    if _kind_of(today, name) == "virtual":
        notes.append("A virtual desktop: Aternity's log time runs late there (usually by under a day).")
    if not changes:
        notes.append(f"Aternity logged no change on {name} in the last {days} days"
                     + (f" for a program like \"{program}\"." if program else "."))
    return _step("pc_changes", args, f"What changed on {name}, the last {days} days", ["When", "Program", "Change",
                 "Version", "Version before", "Note"], rows, summary, notes)


def programs_per_pc(today: Today, names, now: datetime, reach: Reach | None, order: str | None = None,
                    kind: str | None = None) -> dict:
    from statistics import median

    from .software import programs_per_pc as count
    order = "fewest" if str(order or "").lower() == "fewest" else "most"
    kind = str(kind).lower() if str(kind or "").lower() in ("virtual", "physical") else None
    args = {"order": order, "kind": kind}
    client = _client(reach)
    if client is None:
        return _step("programs_per_pc", args, "Programs per PC", notes=[NOT_SET])
    key = getattr(getattr(client, "cfg", None), "base_url", "") or ""
    kept = _per_pc.get(key)
    if kept and time.monotonic() - kept[0] < PER_PC_KEEP_MIN * 60:
        got, notes = kept[1], list(kept[2]) + [f"Counted at {kept[3]}, kept for {PER_PC_KEEP_MIN} minutes."]
    else:
        notes = []
        got = count(client, notes)
        if got is not None:
            _per_pc[key] = (time.monotonic(), got, list(notes), f"{today.local(now):%H:%M}")
    if got is None:
        return _step("programs_per_pc", args, "Programs per PC", notes=notes)
    with today.lock:
        kinds = {d.lower(): k for h in today.hours.values() for d, k in h.dev_kind.items()}
    pcs = got["pcs"]
    if kind:
        pcs = {d: v for d, v in pcs.items() if kinds.get(d.lower()) == kind}
        notes.append(f"Only {kind} ones: the PCs Aternity marked so in today's actions ({len(pcs):,} of "
                     f"{len(got['pcs']):,} PCs in the list).")
    ranked = sorted(pcs, key=lambda d: (pcs[d]["programs"] if order == "fewest" else -pcs[d]["programs"], d))
    rows = [[d, pcs[d]["programs"], pcs[d]["windows_updates"], kinds.get(d.lower(), "no action today")]
            for d in ranked[:PER_PC_ROWS]]
    values = [v["programs"] for v in pcs.values()]
    summary = {"pcs": len(pcs), "counted": got["how"]}
    if values:
        top = ranked[0]
        summary.update(typical_programs=median(values), most_programs=max(values), fewest_programs=min(values),
                       first_listed=f"{top}: {pcs[top]['programs']} programs")
    notes.append("Programs: every row of the PC's installed list except Windows updates, as pc_software lists "
                 "them (a program with 2 versions counts twice).")
    return _step("programs_per_pc", args, f"PCs with the {order} programs installed" + (f" ({kind})" if kind else ""),
                 ["PC", "Programs", "Windows updates", "Kind (today)"], rows, summary, notes)


def _local_time(today: Today, text) -> datetime | None:
    try:
        t = datetime.strptime(" ".join(str(text).replace("T", " ").split())[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return t.replace(tzinfo=today.zone).astimezone(timezone.utc)


def investigate(today: Today, names, now: datetime, reach: Reach | None, user: str | None = None,
                pc: str | None = None, app: str | None = None, start=None, end=None, hours=None) -> dict:
    from .report import render_text
    args = {"user": user, "pc": pc, "app": app, "start": start, "end": end, "hours": hours}
    if not user and not pc:
        return _step("investigate", args, "Whom to investigate?", notes=["Name a user or a PC."])
    picked, notes = {}, []
    for kind, value in (("username", user), ("device", pc)):
        name, said, ask_back = _one(names, kind, value, "investigate", args)
        if ask_back:
            return ask_back
        notes += said
        picked[kind] = name
    if not reach or not reach.investigate:
        return _step("investigate", args, "Investigation", notes=["Investigations cannot be run from here."])
    t1 = _local_time(today, end) if end else now
    t0 = _local_time(today, start) if start else None
    if (start and t0 is None) or (end and t1 is None):
        notes.append("A time was not YYYY-MM-DD HH:MM: the window is the last hours instead.")
        t0, t1 = None, now
    t1 = min(t1, now)
    if t0 is None:
        t0 = t1 - timedelta(hours=_days(hours, INVESTIGATE_HOURS, INVESTIGATE_MAX_H))
    if t1 - t0 > timedelta(hours=INVESTIGATE_MAX_H):
        t0 = t1 - timedelta(hours=INVESTIGATE_MAX_H)
        notes.append(f"Cut to the last {INVESTIGATE_MAX_H} hours of the window asked for (the Investigate tab runs "
                     "longer ones).")
    if t0 >= t1:
        return _step("investigate", args, "Investigation", notes=notes + ["The window ends before it starts."])
    result = reach.investigate(picked["username"], picked["device"], app, t0, t1)
    a = result.get("answer") or {}
    who = " on ".join(x for x in (picked["username"], picked["device"]) if x) + (f" / {app}" if app else "")
    window = f"{today.local(t0):%a %b %d %H:%M} to {today.local(t1):%H:%M}"
    text = render_text(result)
    notes.append("The AI read the investigation's answer and problems as the report writes them"
                 + (" (cut short)." if len(text) > TEXT_CHARS else "."))
    step = _step("investigate", args, f"Investigation of {who}, {window}",
                 summary={"headline": a.get("title"), "reason": a.get("reason")}, notes=notes)
    step["text"] = text[:TEXT_CHARS]
    step["open"] = {"tab": "inv", "username": picked["username"] or "", "device": picked["device"] or "",
                    "application": app or "", "start": _iso(t0), "end": _iso(t1), "result": result}
    return step


def software(name: str, args: dict, reach: Reach | None, now: datetime) -> dict:
    """One of the Software tab's look-ups, as its chat runs it (software_chat.run_block)."""
    from .software_chat import run_block
    sw = reach.software if reach else None
    if sw is None or sw.snap is None:
        return _step(name, args, "Not read yet", notes=["The installed software is still being read (a few minutes "
                                                        "after a start): ask again soon."])
    return _block(name, args, run_block(name, args, sw, sw.view(), reach.aternity_of, reach.cfg, reach.known, now))


def _norm(v):
    """A name as the AI sent it, with Aternity's plain characters: no invisible ones, a dash
    of any kind as "-", an odd space as a space (a model may write "VD\u201177")."""
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if not isinstance(v, str):
        return v
    v = unicodedata.normalize("NFKC", v)
    v = "".join("-" if unicodedata.category(c) == "Pd" or c == "\u2212" else " " if unicodedata.category(c) == "Zs"
                else c for c in v if unicodedata.category(c) != "Cf")
    return " ".join(v.split())


def run(name: str, args: dict, today: Today, names, now: datetime, reach: Reach | None = None) -> dict:
    args = {k: _norm(v) for k, v in (args or {}).items() if v not in (None, "", [])}
    take = lambda *keys: {k: args[k] for k in keys if k in args}  # noqa: E731
    if name in NOW or name in SOFTWARE:
        try:
            if name == "logons":
                return _block(name, args, logins(today, names, now, reach.aternity_of if reach else None,
                                                 username=args.get("user"), device=args.get("pc"),
                                                 date=args.get("date"), current=args.get("current", False)))
            if name == "startups":
                return startups(today, names, now, reach, **take("user", "pc", "days"))
            if name == "pc_software":
                return pc_software(today, names, now, reach, **take("pc", "program"))
            if name == "pc_changes":
                return pc_changes(today, names, now, reach, **take("pc", "program", "days"))
            if name == "programs_per_pc":
                return programs_per_pc(today, names, now, reach, **take("order", "kind"))
            if name == "investigate":
                return investigate(today, names, now, reach, **take("user", "pc", "app", "start", "end", "hours"))
            return software(name, args, reach, now)
        except ApiError as exc:  # one look-up that fails is said; the question goes on
            log.warning("Ask anything: %s: Aternity did not answer (%s)", name, exc.short())
            return _step(name, args, "Could not be read", notes=[f"Aternity did not answer ({exc.short()})."])
        except Exception as exc:
            log.exception("Ask anything: %s failed", name)
            return _step(name, args, "Failed", notes=[f"This look-up failed ({type(exc).__name__}: "
                                                      f"{short_error(exc)}); the app's log has the details."])
    try:
        if name == "find":
            return find(today, names, now, str(args.get("text", "")))
        if name == "events":
            return events(today, now, **{k: args[k] for k in ("kinds", "app", "user", "pc", "severity", "days")
                                         if k in args})
        if name == "activity":
            return activity(today, now, **{k: args[k] for k in ("user", "pc", "app") if k in args})
        if name == "overview":
            return overview(today, now)
        if name == "meanings":
            return meanings(args.get("term"))
    except (TypeError, ValueError, KeyError) as exc:
        return _step(name, args, "Not run", notes=[f"The settings could not be used ({exc})."])
    return _step(name, args, "Not run", notes=[f"There is no tool named {name}."])


# -- the loop ----------------------------------------------------------------------
def _calls(message: dict) -> list[tuple[str, dict, str]]:
    out = []
    for c in message.get("tool_calls") or []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        a = fn.get("arguments")
        if isinstance(a, str):
            try:
                a = json.loads(a or "{}")
            except ValueError:
                a = {}
        out.append((str(fn.get("name") or ""), a if isinstance(a, dict) else {}, str(c.get("id") or "")))
    return out


def for_ai(step: dict) -> str:
    """One step as the AI is given it: no Investigate windows; cut to RESULT_CHARS."""
    given = {k: v for k, v in step.items() if k not in ("runs", "tool", "args", "open")}
    text = json.dumps(given, default=str)
    while len(text) > RESULT_CHARS and given.get("rows"):
        given["rows"] = given["rows"][: len(given["rows"]) // 2]
        given["notes"] = step["notes"] + [f"Only the first {len(given['rows'])} rows are given here (size); the "
                                          "summary counts them all."]
        text = json.dumps(given, default=str)
    return text[:RESULT_CHARS]


def system(today: Today, now: datetime) -> str:
    with today.lock:
        kinds = [k[1] for k, _ in event_kinds(today).most_common(KINDS_MAX) if k[1]]
    known = (f"\nKinds of event Aternity recorded today and the days before, the most PCs first: "
             f"{'; '.join(kinds)}.") if kinds else ""
    return f"{SYSTEM}{known}\nNow: {today.local(now):%A %b %d, %Y, %H:%M}, {today.zone_label}."


def _call(name: str, args: dict) -> str:
    return f"{name}({', '.join(f'{k}={v}' for k, v in (args or {}).items() if v not in (None, '', []))})"


def looked(steps: list[dict]) -> str:
    """The steps in one line, for the next question's history."""
    return "; ".join(_call(s["tool"], s["args"]) for s in steps)


def _reads(name: str, args: dict) -> bool:
    """Does this look-up read Aternity now?"""
    if name == "logons":
        return str(args.get("current", "")).strip().lower() not in ("true", "1", "yes", "now")
    return name in NOW or name in SOFTWARE_NOW


def ask(question: str, history: list, client: AiClient, today: Today, names, now: datetime,
        reach: Reach | None = None) -> dict:
    started = time.monotonic()
    question = " ".join(str(question or "").split())[:500]
    sys_text = system(today, now)
    turns, said, before = [], [], []
    for h in (history or [])[-HISTORY:]:
        if isinstance(h, dict) and h.get("q") and h.get("a"):
            q, a = str(h["q"])[:500], str(h["a"])[:1500]
            turns += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
            said.append(a)
            if h.get("looked"):  # in the instructions: put after an answer, the AI wrote it into its own (stage)
                before.append(f"- \"{q[:120]}\": {str(h['looked'])[:400]}")
                said.append(str(h["looked"])[:400])
    if before:
        sys_text += "\nWhat you looked up for the questions before in this chat:\n" + "\n".join(before)
    messages = [{"role": "system", "content": sys_text}, *turns, {"role": "user", "content": question}]
    steps: list[dict] = []
    note, text, told = None, "", False
    reads = ran = 0  # reads of Aternity now, investigations
    for turn in range(MAX_STEPS + 2):
        reply = client.chat(messages, TOOLS)
        calls = _calls(reply)
        if not calls:
            text = str(reply.get("content") or "").strip()
            break
        if told:  # asked to answer, and still looking
            note = f"The AI was still looking things up after {len(steps)} steps."
            break
        messages.append({"role": "assistant", "content": reply.get("content") or "",
                         "tool_calls": reply.get("tool_calls")})
        for name, args, call_id in calls:
            if len(steps) >= MAX_STEPS:
                content = json.dumps({"notes": [f"No more look-ups ({MAX_STEPS} done): answer with what you have."]})
            elif _reads(name, args) and reads >= MAX_READS:
                content = json.dumps({"notes": [f"No more reads of Aternity for this question ({MAX_READS} done): "
                                                "answer with what you have, or say what to ask next."]})
            elif name == "investigate" and ran >= MAX_INVESTIGATIONS:
                content = json.dumps({"notes": [f"One investigation per question ({MAX_INVESTIGATIONS} done): "
                                                "answer with it, or say to ask again for another."]})
            else:
                log.info("Ask anything: look-up %d, %s", len(steps) + 1, _call(name, args)[:300])
                reads += _reads(name, args)
                ran += name == "investigate"
                step = run(name, args, today, names, now, reach)
                steps.append(step)
                content = for_ai(step)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        if len(steps) >= MAX_STEPS or turn == MAX_STEPS:
            messages.append({"role": "user", "content": "Answer now with what you have found; no more look-ups."})
            told = True
    text = text[:ANSWER_MAX]
    if not text:
        note = note or "The AI gave no answer."
    given = [question, sys_text, *said] + [for_ai(s) for s in steps]
    flagged = sorted(numbers(text) - allowed(*given, small=10))
    worked = COMPARED.search(text)
    return {"question": question, "answer": text, "steps": steps, "looked": looked(steps),
            "flagged": [f"{x:g}" for x in flagged] + ([worked.group(0)] if worked else []),
            "note": note, "service": client.cfg.name, "seconds": round(time.monotonic() - started, 1)}
