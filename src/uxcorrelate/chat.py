"""The chat on the Today tab: a question in plain words, answered with our own counts.

The AI never reads Aternity and never counts. It picks one building block (a
"tool") and its settings; our code counts from what the Today reader already
holds (no extra load on Aternity); the AI words the answer in a sentence or
two. Every number in its sentence must be one of the counts it was given (or
in the question): otherwise our own sentence is shown, and says so. The
counts are always shown under the sentence.

Building blocks (TOOLS):
- slow_actions: actions Aternity rated Major, by application, hour, activity,
  day or PC kind (virtual desktop / physical PC). Today in detail; earlier
  days from the counts saved each night (no names; a day's top 50 apps).
- events: PCs with crashes and other health events, by event, day or
  application: today and the HEALTH_DAYS before.
- who: the users and PCs behind today's slow actions or an event.
- investigate: one user or PC: a button that runs the Investigate tab.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta

from .ai import AiClient, tool_call
from .config import TOP_STATUS, AiService, Settings
from .today import HEALTH_DAYS, KEEP_FILES_DAYS, MIN_USUAL, NO_APP, USUAL_DAYS, Today, kind_of

ROWS = 10  # rows given to the AI and shown, most first (hours: all of today's)
HISTORY = 3  # earlier questions and answers sent with a new one
DEFAULT_HOURS = 4  # investigate: how far back when the user had no slow action today
SAY_ROWS = 3  # rows in our own sentence


def _tool(name: str, description: str, props: dict, required: tuple = ()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "required": list(required), "properties": props}}}


APP = {"type": "string", "description": "Only this application, e.g. Microsoft Outlook."}


def _days(most: int) -> dict:
    return {"type": "integer", "description": f"How many days, today included: 1 = today only (at most {most})."}


TOOLS = [
    _tool("slow_actions", "Count the actions Aternity rated Major (slow) on the company's PCs.", {
        "group_by": {"type": "string", "enum": ["app", "hour", "activity", "day", "pc_kind"],
                     "description": "One row per application, per hour of today, per activity, per day, or per "
                                    "PC kind (virtual desktops against physical PCs)."},
        "app": APP, "days": _days(KEEP_FILES_DAYS)}),
    _tool("events", "Count the PCs with crashes and other health events (Aternity's own severity: Critical, "
                    "Major, Minor).", {
        "group_by": {"type": "string", "enum": ["event", "day", "app"],
                     "description": "One row per kind of event, per day, or per application."},
        "name": {"type": "string", "description": "Only events whose name has this, e.g. Application Crash."},
        "severity": {"type": "string", "enum": ["Critical", "Major", "Minor"]},
        "days": _days(HEALTH_DAYS + 1)}),
    _tool("who", "List today's users and PCs with the most Major slow actions, or with an event.", {
        "what": {"type": "string", "enum": ["slow", "event"]}, "app": APP,
        "name": {"type": "string", "description": "For what=event: the event's name, e.g. Application Crash."}},
        ("what",)),
    _tool("investigate", "Investigate one user or PC: why they were slow, or what went wrong.", {
        "username": {"type": "string"}, "device": {"type": "string", "description": "The PC's name."},
        "app": APP, "hours": {"type": "integer", "description": "How many hours back from now."}}),
]

SYSTEM = ("You answer questions from IT staff about the experience of the company's PC users, from Aternity. "
          "Numbers come only from the tools: call exactly one tool to answer. If no tool fits the question, say "
          "so in one sentence and call no tool. Never state a number a tool did not give, and do not work out new "
          "ones (no sums, no percentages). When the counts come back, answer in one or two plain sentences: no "
          "lists, no tables, no markdown.")
NO_COUNT = ("I can only give numbers from the counts: slow actions, crashes and events, who had them today, or an "
            "investigation of one user or PC.")
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# a comparison worked out from the counts ("3 times worse", "twice as many"): a new number in words
RATIO = re.compile(r"\b(?:\d[\d.,]*|two|three|four|five|six|ten)\s*(?:x|times)\s+(?:more|less|fewer|worse|better|"
                   r"higher|lower|slower|faster|as|the)\b|\b(?:twice|double|triple|half as|half the)\b", re.I)


def chat_service(settings: Settings) -> AiService | None:
    """The AI service the chat uses: ai.chat by name, else the first one set."""
    usable = [s for s in settings.ai.services if s.base_url and s.model]
    named = [s for s in usable if settings.ai.chat and s.name == settings.ai.chat]
    return (named or usable or [None])[0]


# -- the building blocks ---------------------------------------------------------
def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _has(text: str | None, part: str | None) -> bool:
    return not part or part.strip().lower() in (text or "").lower()


def _share(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def _period(today: Today, t: date, days: int) -> str:
    if days == 1:
        return f"today ({t:%a %b %d, %Y}) so far, {today.zone_label}"
    return f"{t - timedelta(days=days - 1):%b %d} to today ({t:%b %d, %Y}), {today.zone_label}"


def _result(block: str, args: dict, title: str, period: str, columns: list, rows: list, notes: list,
            total: str | None = None, runs: list | None = None, action: dict | None = None,
            label: tuple = (0,), value: int = 1, say: str | None = None) -> dict:
    """A block's counts, and our own sentence for them (shown when the AI's cannot be)."""
    if say is None:
        lead = [r for r in rows if r and len(r) > value and r[value] is not None][:SAY_ROWS]
        shown = "; ".join(f"{' on '.join(str(r[i]) for i in label)}: {_say(r[value])} {columns[value]}"
                          for r in lead)
        say = f"{title}, {period}: " + (shown + (f" (of {total})" if total else "") if lead else "none") + "."
    return {"block": block, "args": args, "title": title, "period": period, "columns": columns, "rows": rows,
            "total": total, "notes": notes, "say": say, "runs": runs, "action": action}


def _n(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _say(v) -> str:
    return f"{v:,}" if isinstance(v, int) else str(v)


def _usual_note(view: dict) -> list[str]:
    """The Major slow actions tile's comparison, in words."""
    s = view.get("slow") or {}
    u = s.get("usual") or {}
    if not s.get("by"):
        return []
    if not u.get("enough"):
        return [f"Usual: {u.get('days', 0)} of the {MIN_USUAL} {view['kind']} needed are saved so far."]
    word = {"more": "More than usual", "fewer": "Fewer than usual", "usual": "As usual"}[u["verdict"]]
    return [f"{word}: {s['by_pcs']:,} PCs with a {TOP_STATUS} action by {s['by']}; the {u['days']} {view['kind']} "
            f"before had {u['low']:,} to {u['high']:,}."]


def slow_actions(today: Today, now: datetime, group_by: str = "app", app: str | None = None, days=1) -> dict:
    t = today.day(now)
    days = _clamp(days, 1, KEEP_FILES_DAYS, 1)
    group_by = group_by if group_by in ("app", "hour", "activity", "day", "pc_kind") else "app"
    with today.lock:
        hours = sorted((h for h in today.hours.values() if h.day == t), key=lambda h: h.start)
        saved = dict(today.saved)
    before = [t - timedelta(days=n) for n in range(days - 1, 0, -1)]
    have = [d for d in before if d in saved]
    notes = []
    if len(have) < len(before):
        notes.append(f"{len(before) - len(have)} of the {len(before)} days before have no saved counts (they are "
                     "saved each night, from the first night this ran).")
    used = {"group_by": group_by, "app": app, "days": days}
    apps_today = {a for h in hours for a in h.apps}
    matched = sorted(a for a in apps_today | {a for d in have for a in saved[d].get("apps", {})} if _has(a, app))
    if app:
        if not matched:
            notes.append(f"No application named like \"{app}\" had activity in this time.")
        elif len(matched) > 1:
            notes.append(f"\"{app}\" matches {len(matched)} applications, counted together: {', '.join(matched[:5])}"
                         f"{' …' if len(matched) > 5 else ''}.")
    keep = set(matched)
    period = _period(today, t, days)
    in_app = f" in {', '.join(matched[:2])}{' …' if len(matched) > 2 else ''}" if app and matched else ""

    if group_by in ("hour", "activity") and days > 1:
        notes.append(f"Per {group_by} is for today only (earlier days keep counts per application and hour).")
        period = _period(today, t, 1)

    if group_by == "app":
        acts: dict[str, list] = {}
        for h in hours:
            for a, c in h.apps.items():
                x = acts.setdefault(a, [0, 0, set()])
                x[0] += c[TOP_STATUS]
                x[1] += c["runs"]
                x[2] |= h.app_devs.get(a, set())
        for d in have:
            for a, v in saved[d].get("apps", {}).items():
                x = acts.setdefault(a, [0, 0, set()])
                x[0] += v.get(TOP_STATUS, 0)
                x[1] += v.get("runs", 0)
        if have:
            notes.append("Earlier days keep only each day's 50 applications with the most Major actions.")
        top = sorted(((a, x) for a, x in acts.items() if x[0] and (not app or a in keep)),
                     key=lambda kv: (-kv[1][0], kv[0]))
        cols = ["Application", f"{TOP_STATUS} slow actions", "Runs", f"{TOP_STATUS} share %"]
        rows = [[a, x[0], x[1], _share(x[0], x[1])] + ([len(x[2])] if days == 1 else []) for a, x in top[:ROWS]]
        if days == 1:
            cols.append(f"PCs with {TOP_STATUS}")
        if not app and days == 1:
            notes += _usual_note(today.view(now))
        return _result("slow_actions", used, f"{TOP_STATUS} slow actions by application", period, cols, rows,
                       notes, f"{sum(x[0] for _, x in top):,} {TOP_STATUS} slow actions in {len(top)} applications")

    if group_by == "hour":
        kind = kind_of(t)
        same = sorted((d for d, s in saved.items() if d < t and kind_of(d) == kind and s.get("complete")),
                      reverse=True)[:USUAL_DAYS]
        rows = []
        for h in hours:
            n = (sum(1 for r in h.top_rows if r[3] in keep) if app else h.status[TOP_STATUS])
            past = sorted(saved[d].get("hours", {}).get(str(h.hour), {}).get(TOP_STATUS, 0) for d in same)
            mid = past[len(past) // 2] if len(past) >= MIN_USUAL and not app else None
            rows.append([today.local(h.start).strftime("%H:%M"), n, mid])
        if app:
            notes.append("No usual per hour for one application (the saved counts are per hour for all).")
        elif len(same) < MIN_USUAL:
            notes.append(f"Usual per hour: {len(same)} of the {MIN_USUAL} {kind} needed are saved so far.")
        return _result("slow_actions", used, f"{TOP_STATUS} slow actions per hour{in_app}", period,
                       ["Hour", f"{TOP_STATUS} slow actions", f"Usual (middle of the {kind} before)"], rows, notes,
                       f"{sum(r[1] for r in rows):,} today")

    if group_by == "activity":
        acts = {}
        for h in hours:
            for r in h.top_rows:
                if app and r[3] not in keep:
                    continue
                x = acts.setdefault((r[3], r[4] or "(no activity name)"), [0, set(), None])
                x[0] += 1
                x[1].add(r[1])
                if r[5] is not None:
                    x[2] = max(x[2] or 0, r[5])
        top = sorted(acts.items(), key=lambda kv: (-kv[1][0], kv[0]))
        rows = [[f"{a} {act}", x[0], len(x[1]), x[2]] for (a, act), x in top[:ROWS]]
        return _result("slow_actions", used, f"{TOP_STATUS} slow actions by activity{in_app}", period,
                       ["Application and activity", f"{TOP_STATUS} slow actions", "PCs", "Slowest (s)"], rows, notes,
                       f"{sum(x[0] for x in acts.values()):,} {TOP_STATUS} slow actions in {len(acts)} activities")

    if group_by == "day":
        rows = []
        for d in before + [t]:
            if d == t:
                if app:
                    n = sum(h.apps.get(a, {}).get(TOP_STATUS, 0) for h in hours for a in keep)
                    pcs = len(set().union(*(h.app_devs.get(a, set()) for h in hours for a in keep)))
                else:
                    n = sum(h.status[TOP_STATUS] for h in hours)
                    pcs = len(set().union(*(h.top_devs for h in hours))) if hours else 0
                active = len(set().union(*(h.devs for h in hours))) if hours else 0
                rows.append([f"{d:%a %b %d} (so far)", n, pcs, active])
            elif d in saved:
                s = saved[d]
                if app:
                    got = [v for a, v in s.get("apps", {}).items() if a in keep]
                    n, pcs = sum(v.get(TOP_STATUS, 0) for v in got), sum(v.get("pcs", 0) for v in got)
                else:
                    n, pcs = sum(v.get(TOP_STATUS, 0) for v in s.get("hours", {}).values()), s.get("top_pcs")
                label = f"{d:%a %b %d}" + ("" if s.get("complete") else " (not every hour read)")
                rows.append([label, n, pcs, s.get("pcs")])
            else:
                rows.append([f"{d:%a %b %d}", None, None, None])
        if app and len(keep) > 1:
            notes.append("PCs are added up over the matching applications: a PC using two is counted twice.")
        return _result("slow_actions", used, f"{TOP_STATUS} slow actions per day{in_app}", period,
                       ["Day", f"{TOP_STATUS} slow actions", f"PCs with {TOP_STATUS}", "PCs active"], rows[::-1],
                       notes)

    # pc_kind: virtual desktops against physical PCs
    kinds: dict[str, list] = {}
    for h in hours:
        for (a, pc), c in h.kinds.items():
            if app and a not in keep:
                continue
            x = kinds.setdefault(pc, [0, 0])
            x[0] += c["runs"]
            x[1] += c[TOP_STATUS]
    for d in have:
        for pc, v in saved[d].get("kinds", {}).items():
            if app:
                continue
            x = kinds.setdefault(pc, [0, 0])
            x[0] += v.get("runs", 0)
            x[1] += v.get(TOP_STATUS, 0)
    if app and have:
        notes.append("Earlier days keep virtual and physical counts for all applications only: this is today's.")
        period = _period(today, t, 1)
    elif have and any("kinds" not in saved[d] for d in have):
        notes.append("Some earlier days were saved before virtual and physical were counted: they are left out.")
    label = {"virtual": "Virtual desktops", "physical": "Physical PCs", "not known": "Not known"}
    order = ["virtual", "physical", "not known"]
    cols = ["PCs", "Runs", f"{TOP_STATUS} slow actions", f"{TOP_STATUS} share %"]
    rows = []
    for pc in sorted(kinds, key=lambda k: order.index(k) if k in order else 9):
        runs, major = kinds[pc]
        row = [label.get(pc, pc), runs, major, _share(major, runs)]
        if days == 1 and not app:
            active = len(set().union(*(h.kind_devs.get(pc, set()) for h in hours)))
            top = len(set().union(*(h.kind_top_devs.get(pc, set()) for h in hours)))
            row += [active, top]
        rows.append(row)
    if days == 1 and not app:
        cols += ["PCs active", f"PCs with {TOP_STATUS}"]
    return _result("slow_actions", used, f"{TOP_STATUS} slow actions, virtual desktops and physical PCs{in_app}",
                   period, cols, rows, notes, value=2)


def events(today: Today, now: datetime, group_by: str = "event", name: str | None = None,
           severity: str | None = None, days=1) -> dict:
    t = today.day(now)
    days = _clamp(days, 1, HEALTH_DAYS + 1, 1)
    group_by = group_by if group_by in ("event", "day", "app") else "event"
    first = t - timedelta(days=days - 1)
    with today.lock:
        health = [x for x in today.health if today.day(x[0]) == t]
        hd = {d: v for d, v in today.health_days.items() if first <= d < t}
        ha = {d: v for d, v in today.health_apps.items() if first <= d < t}
        read = {d for d in today.health_read if first <= d < t}
        sev = {k: c.most_common(1)[0][0] for k, c in today.health_sev.items() if c}
    used = {"group_by": group_by, "name": name, "severity": severity, "days": days}

    def keep(k) -> bool:
        return _has(k[1], name) and (not severity or (sev.get(k) or "").lower() == severity.strip().lower())

    per_day: dict[date, dict] = {t: {}}
    per_app: dict[date, dict] = {t: {}}
    for x in health:
        if keep(x[2]):
            per_day[t].setdefault(x[2], set()).add(x[1])
            per_app[t].setdefault((x[2], x[5]), set()).add(x[1])
    for d, kinds in hd.items():
        per_day[d] = {k: set(v) for k, v in kinds.items() if keep(k)}
    for d, kinds in ha.items():
        per_app[d] = {k: v for k, v in kinds.items() if keep(k[0])}
    before = [t - timedelta(days=n) for n in range(days - 1, 0, -1)]
    notes = []
    missing = [d for d in before if d not in read]
    if missing:
        notes.append(f"{len(missing)} of the {len(before)} days before were not read yet (read once a day).")
    what = " ".join(w for w in (severity, f"\"{name}\"" if name else None) if w)
    period = _period(today, t, days)

    if group_by == "event":
        pcs: dict = {}
        for kinds in per_day.values():
            for k, devs in kinds.items():
                pcs.setdefault(k, set()).update(devs)
        verdict = {}
        if days == 1:
            view = today.view(now)
            word = {"more": "more than usual", "fewer": "fewer than usual", "usual": "as usual"}
            for e in view.get("tiles", []) + view.get("events", []):
                u = e["usual"]
                verdict[(e["category"], e["name"])] = (
                    f"{word[u['verdict']]} ({u['low']:,} to {u['high']:,})" if u.get("enough") else "not enough days")
        top = sorted(pcs.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        rows = [[k[1], k[0], sev.get(k), len(v)] + ([verdict.get(k)] if days == 1 else []) for k, v in top[:ROWS]]
        cols = ["Event", "Category", "Severity", "PCs"] + (["Usual by this time"] if days == 1 else [])
        return _result("events", used, f"PCs with {what + ' ' if what else ''}events, by event", period, cols, rows,
                       notes, f"{len(top)} kinds of event")

    if group_by == "day":
        rows = []
        for d in before + [t]:
            if d != t and d not in read:
                rows.append([f"{d:%a %b %d}", None])
                continue
            devs = set().union(*per_day.get(d, {}).values()) if per_day.get(d) else set()
            rows.append([f"{d:%a %b %d}" + (" (so far)" if d == t else ""), len(devs)])
        return _result("events", used, f"PCs with {what + ' ' if what else ''}events, per day", period,
                       ["Day", "PCs"], rows[::-1], notes)

    apps: dict = {}
    for kinds in per_app.values():
        for (k, a), devs in kinds.items():
            x = apps.setdefault(a or NO_APP, [set(), set()])
            x[0].update(devs)
            x[1].add(k[1])
    top = sorted(apps.items(), key=lambda kv: (-len(kv[1][0]), kv[0]))
    rows = [[a, len(x[0]), ", ".join(sorted(x[1])[:3]) + (" …" if len(x[1]) > 3 else "")] for a, x in top[:ROWS]]
    if NO_APP in apps:
        notes.append(f"{NO_APP}: events Aternity gives no application for (e.g. system events).")
    return _result("events", used, f"PCs with {what + ' ' if what else ''}events, by application", period,
                   ["Application", "PCs", "Events"], rows, notes, f"{len(top)} applications")


def who(today: Today, now: datetime, what: str = "slow", app: str | None = None, name: str | None = None) -> dict:
    t = today.day(now)
    used = {"what": what, "app": app, "name": name}
    period = _period(today, t, 1)
    notes = []
    if what == "event":
        with today.lock:
            counts: dict = {}
            for x in today.health:
                if today.day(x[0]) == t and _has(x[2][1], name):
                    counts.setdefault(x[2], set()).add(x[1])
        if not counts:
            return _result("who", used, f"Users and PCs with \"{name or 'any'}\" events", period, [], [],
                           [f"No event named like \"{name}\" today."])
        kind = max(counts, key=lambda k: (len(counts[k]), k))
        if len(counts) > 1:
            notes.append(f"\"{name or ''}\" matches {len(counts)} kinds of event; this is the one on the most PCs.")
        w = today.who(now, "event", kind[1], kind[0])
    else:
        name_of = None
        if app:
            with today.lock:
                totals: dict = {}
                for h in today.hours.values():
                    if h.day == t:
                        for a, c in h.apps.items():
                            if _has(a, app) and c[TOP_STATUS]:
                                totals[a] = totals.get(a, 0) + c[TOP_STATUS]
            if not totals:
                return _result("who", used, f"Users and PCs with {TOP_STATUS} slow actions in \"{app}\"", period,
                               [], [], [f"No application named like \"{app}\" had a {TOP_STATUS} action today."])
            name_of = max(totals, key=lambda a: (totals[a], a))
            if len(totals) > 1:
                notes.append(f"\"{app}\" matches {len(totals)} applications; this is the one with the most.")
        w = today.who(now, "app", name_of) if name_of else today.who(now, "slow")
    event = w["kind"] == "event"
    rows, runs = [], []
    for r in w["rows"][:ROWS]:
        slowest = None if event else " ".join(str(v) for v in (r.get("application"), r.get("activity")) if v)
        if not event and r.get("seconds") is not None:
            slowest = f"{slowest} {r['seconds']:.1f} s"
        rows.append([r["username"] or "—", r["device"], r["count"]] + ([] if event else [slowest])
                    + [r["first"] if r["first"] == r["last"] else f"{r['first']}–{r['last']}"])
        runs.append({"username": r["username"], "device": r["device"], "application": w.get("application"),
                     "start": r["start"], "end": r["end"]})
    cols = ["User", "PC", "Times" if event else f"{TOP_STATUS} slow"] + ([] if event else ["Slowest"]) + ["When"]
    return _result("who", used, f"Users and PCs: {w['what']}", period, cols, rows, notes,
                   f"{_n(w['pcs'], 'PC')} and {_n(w['users'], 'user')}", runs=runs, label=(0, 1), value=2)


def investigate(today: Today, names, now: datetime, username: str | None = None, device: str | None = None,
                app: str | None = None, hours=None) -> dict:
    used = {"username": username, "device": device, "app": app, "hours": hours}
    picked, notes = {}, []
    for kind, value in (("username", username), ("device", device)):
        value = (value or "").strip()
        if not value:
            continue
        if names is None:
            picked[kind] = value
            continue
        with names.lock:
            exact = [n for n in names.seen[kind] if n.lower() == value.lower()]
        if exact:
            picked[kind] = exact[0]
            continue
        m = names.match(kind, value)
        if len(m["names"]) == 1:
            picked[kind] = m["names"][0]
            notes.append(f"\"{value}\" is taken as {m['names'][0]}, the one {kind} with it in its name.")
        elif not m["count"]:
            picked[kind] = value
            notes.append(f"The list of names is still filling: \"{value}\" is used as typed.")
        else:
            rows = [[n] for n in m["names"][:ROWS]]
            more = len(m["names"]) - len(rows) + m["more"]
            return _result("investigate", used, f"Which {kind}?", "names seen in Aternity's activity",
                           [kind.capitalize()], rows, [f"No {kind} is \"{value}\". "
                           + (f"Names with it: ask again with one of them{f' ({more} more)' if more else ''}."
                              if rows else "No name has it.")],
                           say=f"No {kind} is \"{value}\"; {len(rows) + more} have it in their name: ask again "
                               "with one of them.")
    if not picked:
        return _result("investigate", used, "Who to investigate?", "", [], [],
                       ["Name a user or a PC to investigate."], say="Name a user or a PC to investigate.")
    application = None
    if app:
        with today.lock:
            known = sorted({a for h in today.hours.values() for a in h.apps if _has(a, app) and a != NO_APP})
        application = known[0] if len(known) == 1 else None
        notes.append(f"Application: {application}." if application else
                     f"\"{app}\" matches {len(known)} applications seen today: the investigation covers all of them.")
    hours_n = _clamp(hours, 1, 24 * 8, 0) if hours else 0
    start, end = now - timedelta(hours=hours_n or DEFAULT_HOURS), now
    if not hours_n:
        rows = today.who(now, "slow")["rows"] if today.ready else []
        mine = [r for r in rows if (not picked.get("username") or r["username"] == picked["username"])
                and (not picked.get("device") or r["device"] == picked["device"])]
        if mine:
            start, end = (datetime.fromisoformat(mine[0][k].replace("Z", "+00:00")) for k in ("start", "end"))
            notes.append(f"Around their {TOP_STATUS} slow actions today ({mine[0]['first']}–{mine[0]['last']}).")
        else:
            notes.append(f"The last {DEFAULT_HOURS} hours (no {TOP_STATUS} slow action today in the top "
                         f"{len(rows)}).")
    who_txt = " on ".join(v for v in (picked.get("username"), picked.get("device")) if v)
    action = {"username": picked.get("username"), "device": picked.get("device"), "application": application,
              "start": start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
              "end": end.replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    span = f"{today.local(start):%b %d %H:%M} to {today.local(end):%H:%M} {today.zone_label}"
    return _result("investigate", used, f"Investigate {who_txt}", span, ["User", "PC", "From", "To"],
                   [[picked.get("username") or "—", picked.get("device") or "—", f"{today.local(start):%H:%M}",
                     f"{today.local(end):%H:%M}"]], notes, action=action,
                   say=f"Ready to investigate {who_txt}, {span}: press Investigate ▸.")


def run_block(name: str, args: dict, today: Today, names, now: datetime) -> dict | None:
    args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    try:
        if name == "slow_actions":
            return slow_actions(today, now, **{k: args[k] for k in ("group_by", "app", "days") if k in args})
        if name == "events":
            return events(today, now, **{k: args[k] for k in ("group_by", "name", "severity", "days") if k in args})
        if name == "who":
            return who(today, now, **{k: args[k] for k in ("what", "app", "name") if k in args})
        if name == "investigate":
            return investigate(today, names, now, **{k: args[k] for k in ("username", "device", "app", "hours")
                                                     if k in args})
    except (TypeError, ValueError, KeyError):
        return None
    return None


# -- the AI's sentence, held to our counts -----------------------------------------
def numbers(text: str) -> set[float]:
    out = set()
    for m in NUM.findall(text or ""):
        try:
            out.add(round(float(m.rstrip(",").replace(",", "")), 1))
        except ValueError:
            pass
    return out


def allowed(*texts: str, small: int = 0) -> set[float]:
    """Numbers the AI may say: those in what it was given, rounded too, and 0 to `small`."""
    got = set().union(*(numbers(t) for t in texts))
    return got | {float(round(x)) for x in got} | {float(n) for n in range(small + 1)}


def clean(text) -> str:
    text = re.sub(r"[*_`#]{1,3}", "", str(text or ""))
    return " ".join(text.split())[:800]


def system(today: Today, now: datetime) -> str:
    return f"{SYSTEM} Today is {today.local(now):%A %b %d, %Y}; times are {today.zone_label}."


def ask(question: str, history: list, client: AiClient, today: Today, names, now: datetime) -> dict:
    """One question: the AI picks a building block, we count, the AI words it (held to our numbers)."""
    started = time.monotonic()
    question = " ".join(str(question or "").split())[:500]
    messages = [{"role": "system", "content": system(today, now)}]
    for h in (history or [])[-HISTORY:]:
        if isinstance(h, dict) and h.get("q") and h.get("a"):
            messages += [{"role": "user", "content": str(h["q"])[:500]},
                         {"role": "assistant", "content": str(h["a"])[:800]}]
    messages.append({"role": "user", "content": question})
    out = {"question": question, "answer": "", "result": None, "note": None, "service": client.cfg.name}
    reply = client.chat(messages, TOOLS)
    call = tool_call(reply)
    if call is None:
        text = clean(reply.get("content"))
        if numbers(text) - allowed(question, system(today, now)):
            text, out["note"] = NO_COUNT, "The AI answered with numbers but no counts behind them, so it is not shown."
        out.update(answer=text or NO_COUNT, seconds=round(time.monotonic() - started, 1))
        return out
    name, args, call_id = call
    result = run_block(name, args, today, names, now)
    if result is None:
        out.update(answer=NO_COUNT, note=f"The AI asked for a building block we do not have ({name}).",
                   seconds=round(time.monotonic() - started, 1))
        return out
    given = {k: result[k] for k in ("title", "period", "columns", "rows", "total", "notes")}
    messages += [{"role": "assistant", "content": reply.get("content") or "", "tool_calls": reply["tool_calls"]},
                 {"role": "tool", "tool_call_id": call_id, "content": json.dumps(given, default=str)}]
    final = client.chat(messages, TOOLS)
    text = clean(final.get("content"))
    if tool_call(final) or not text:
        text, out["note"] = result["say"], "The AI did not word an answer, so this is our own sentence."
    else:
        extra = numbers(text) - allowed(json.dumps(given, default=str), json.dumps(args, default=str), question,
                                        system(today, now), small=len(result["rows"]))
        ratio = RATIO.search(text)
        if extra:
            shown = ", ".join(f"{x:g}" for x in sorted(extra)[:3])
            text, out["note"] = result["say"], (f"The AI's sentence had a number that is not in the counts "
                                                f"({shown}), so this is our own sentence.")
        elif ratio:
            text, out["note"] = result["say"], (f"The AI's sentence worked out a comparison the counts do not give "
                                                f"(\"{ratio.group(0)}\"), so this is our own sentence.")
    out.update(answer=text, result=result, seconds=round(time.monotonic() - started, 1))
    return out
