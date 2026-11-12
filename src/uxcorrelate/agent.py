"""Ask anything: the open chat on the Today tab (step 1; the user's request 2026-11-12: "HOW DO
WE MAKE THIS LIKE NORMAL REGULAR CHAT", after "What PCs had hard drive issue this week?" was
answered "no event named Hard drive" by the block chat).

The AI looks things up with general tools, as many steps as it needs (`MAX_STEPS`), sees the
records themselves (rows, with a summary of the counts), and answers in its own words. Every
step and its rows go back to the page ("What I looked at"); a number in the answer that is in
none of them is marked, not hidden (`flagged`). The tools read only what this app already holds
(the Today reader, the names, our meanings): no extra read of Aternity. Step 2 adds those that
read Aternity now (a user's logons, a PC's software, an investigation).
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta

from .ai import AiClient
from .chat import _kinds_named, _match, _singular, allowed, numbers
from .config import TOP_STATUS
from .today import EVENT_PAD, HEALTH_DAYS, NO_APP, SLOW_PAD, Today

MAX_STEPS = 8  # tool calls per question
ROWS_MAX = 150  # rows of one step, given to the AI and shown
RESULT_CHARS = 24000  # one step as given to the AI, at most
HISTORY = 6  # earlier questions and answers sent with a new one
ANSWER_MAX = 4000
KINDS_MAX = 80  # kinds of event named in the instructions, the most PCs first
FIND_MAX = 20  # names of each sort a find returns

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
    "What the data holds: health events (crashes, hangs, hardware faults and more) today with user, PC and "
    "application, and on the days before by PC and application (no user); today's actions Aternity measured "
    "(each user's PCs and number of actions, and every action Aternity rated Major with its time, PC, user, "
    "application and activity); today's overview against the usual of the days before; what this app's words "
    "mean. Not in reach yet: installed software, start-ups, logons, NetIM, investigations (the page's other "
    "tabs have them: say so when asked)."
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
]


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


def run(name: str, args: dict, today: Today, names, now: datetime) -> dict:
    args = {k: _norm(v) for k, v in (args or {}).items() if v not in (None, "", [])}
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
    given = {k: v for k, v in step.items() if k not in ("runs", "tool", "args")}
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


def looked(steps: list[dict]) -> str:
    """The steps in one line, for the next question's history."""
    return "; ".join(f"{s['tool']}({', '.join(f'{k}={v}' for k, v in s['args'].items() if v)})" for s in steps)


def ask(question: str, history: list, client: AiClient, today: Today, names, now: datetime) -> dict:
    started = time.monotonic()
    question = " ".join(str(question or "").split())[:500]
    sys_text = system(today, now)
    messages = [{"role": "system", "content": sys_text}]
    said = []
    for h in (history or [])[-HISTORY:]:
        if isinstance(h, dict) and h.get("q") and h.get("a"):
            a = str(h["a"])[:1500] + (f"\n(I looked at: {str(h['looked'])[:400]})" if h.get("looked") else "")
            messages += [{"role": "user", "content": str(h["q"])[:500]}, {"role": "assistant", "content": a}]
            said.append(a)
    messages.append({"role": "user", "content": question})
    steps: list[dict] = []
    note, text, told = None, "", False
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
            else:
                step = run(name, args, today, names, now)
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
