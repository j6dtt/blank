"""The chat on the Today tab: a question in plain words, answered with our own counts.

The AI never reads Aternity and never counts. It picks one building block (a
"tool") and its settings; our code counts from what the Today reader already
holds (no extra load on Aternity); the AI words the answer in two to four
short sentences: usual or not (only as the counts say), what stands out by
name, one next step. Every number in its sentence must be one of the counts
it was given (or in the question): otherwise our own sentence is shown, and says so. The
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
from collections import Counter
from datetime import date, datetime, timedelta

from .ai import AiClient, tool_call
from .config import TOP_STATUS, AiService, Settings
from .today import EVENT_PAD, HEALTH_DAYS, KEEP_FILES_DAYS, MIN_USUAL, NO_APP, USUAL_DAYS, Today, kind_of

ROWS = 10  # rows given to the AI and shown, most first (hours: all of today's)
HISTORY = 3  # earlier questions and answers sent with a new one
DEFAULT_HOURS = 4  # investigate: how far back when the user had no slow action today
SAY_ROWS = 3  # rows in our own sentence
SHARE_RUNS = 20  # fewer runs: an application's Major share means little (as after_start.MIN_RUNS)


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
                    "Major, Minor). A question naming an application (e.g. Outlook crashes) needs app.", {
        "group_by": {"type": "string", "enum": ["event", "day", "app"],
                     "description": "One row per kind of event, per day, or per application."},
        "name": {"type": "string", "description": "Only events whose name has this, e.g. Application Crash."},
        "severity": {"type": "string", "enum": ["Critical", "Major", "Minor"]},
        "app": APP, "days": _days(HEALTH_DAYS + 1)}),
    _tool("who", "List the users and PCs with the most Major slow actions today, or the PCs with an event, today "
                 "or over several days. Use it whenever the question asks to list, name or show which PCs or users.", {
        "what": {"type": "string", "enum": ["slow", "event"]}, "app": APP,
        "name": {"type": "string", "description": "For what=event: the event's name, e.g. Application Crash."},
        "days": {"type": "integer", "description": f"For what=event: how many days, today included: 1 = today "
                                                   f"only, 7 = this week (at most {HEALTH_DAYS + 1})."}},
        ("what",)),
    _tool("active", "How many users and PCs are active (had at least one action Aternity measured): today so far and "
                    "in the last hour, with the PCs split into virtual desktops and physical PCs. Use it for how many "
                    "users, people, PCs or desktops are active, online, working or in use.", {}),
    _tool("investigate", "Investigate one user or PC: why they were slow, or what went wrong.", {
        "username": {"type": "string"}, "device": {"type": "string", "description": "The PC's name."},
        "app": APP, "hours": {"type": "integer", "description": "How many hours back from now."}}),
]

# How an answer is laid out (the user, Nov 2026: "responses in better format than just running
# sentences"): one line that answers, short points, one next step. The page draws it (answerHtml).
SHAPE = ("as plain text with no markdown and no table, in this shape: first one short sentence that answers the "
         "question; then two to four lines that each start with \"- \" and give one thing that stands out, by "
         "name, with its number; then one last line that starts with \"Next: \" and gives one next step")
LINES_MAX, TEXT_MAX = 7, 1200

SYSTEM = ("You answer questions from IT staff about the experience of the company's PC users, from Aternity. "
          "Numbers come only from the tools: call exactly one tool to answer. If no tool fits the question, say "
          "so in one sentence and call no tool. Never state a number a tool did not give, and do not work out new "
          "ones (no sums, no percentages). When the counts come back, answer as a helpful colleague on the service "
          f"desk would, {SHAPE}. First, "
          "whether it is more, fewer or as usual, only when a note or row says exactly that (More than usual, "
          "Fewer than usual, As usual); if none does, say nothing about usual. Never call a count high, low, good "
          "or bad yourself. Then what stands out, by name: the application, activity, hour, event, user or PC at "
          "the top, and the highest share when a note names it. End with one next step that fits: a follow-up "
          "question to ask here (for example who had them, per hour, which activities), Investigate by a user's "
          "name, or the Care list for the users watched there. Use only the few numbers that matter, written "
          "exactly as given.")
NO_COUNT = ("I can only give numbers from the counts: slow actions, crashes and events, who had them today, or an "
            "investigation of one user or PC.")
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# a comparison worked out from the counts ("3 times worse", "twice as many"): a new number in words
RATIO = re.compile(r"\b(?:\d[\d.,]*|two|three|four|five|six|ten)\s*(?:x|times)\s+(?:more|less|fewer|worse|better|"
                   r"higher|lower|slower|faster|as|the)\b|\b(?:twice|double|triple|half as|half the)\b", re.I)

# how the sentence compares with usual: "more Major slow actions than usual", "as usual", "unusual" (normal is
# left out: Aternity's own status word)
MORE = ("more", "higher", "busier", "worse", "slower", "above")
FEWER = ("fewer", "less", "lower", "quieter", "better", "faster", "below")
USUAL = re.compile(r"\b(?P<w>" + "|".join(MORE + FEWER) + r")\b(?:[\s,]+[\w.-]+){0,5}?[\s,]+(?:than|the)\s+"
                   r"usual\b|\b(?P<ab>above|below)\s+usual\b|\b(?P<as>as\s+usual|not\s+unusual)\b"
                   r"|\bunusual(?:ly)?\b", re.I)
# "too early to say whether it is unusual", "no usual yet": not a verdict (the AI's sentence only)
HEDGE = re.compile(r"\b(?:whether|if|not|no|cannot|can't|too early)\b[^.!?;:]*$", re.I)


def _usual(text: str, hedged: bool = False) -> list[tuple[str, str]]:
    """Each comparison with usual in a text: (its words, more / fewer / as / either);
    `hedged`: leave out one said with "whether", "not", "no" ... before it."""
    out = []
    for m in USUAL.finditer(text or ""):
        if hedged and HEDGE.search(text[max(0, m.start() - 60):m.start()]):
            continue
        w = (m.group("w") or m.group("ab") or "").lower()
        out.append((m.group(0), "more" if w in MORE else "fewer" if w in FEWER else "as" if m.group("as")
                    else "either"))
    return out


def usual_wrong(text: str, given: dict) -> str | None:
    """The AI's words about usual that the counts do not give (None: fine).

    Fine: any reading of a Usual column of numbers (per hour), else what a note
    or row says (more / fewer / as usual); "unusual" needs a more or fewer."""
    said = _usual(text, hedged=True)
    cols = [i for i, c in enumerate(given.get("columns") or []) if str(c).lower().startswith("usual")]
    if not said or any(len(r) > i and isinstance(r[i], (int, float)) for r in given.get("rows") or []
                       for i in cols):
        return None
    have = {k for _, k in _usual(json.dumps(given, default=str))}
    for words, k in said:
        if not (k in have or (k == "either" and have & {"more", "fewer"})):
            return words
    return None


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


def _match(names, asked) -> list[str]:
    """The names meant by what was asked: the one that is exactly it (any case), else every
    one that has it. One rule for every block: "Microsoft Edge" is not also Microsoft Edge
    Update (stage, Nov 2026)."""
    q = " ".join(str(asked or "").lower().split())
    names = sorted({n for n in names if n})
    if not q:
        return []
    from .software import named_like  # the one word rule of every name search ("Microsoft Edge" -> edge)
    return ([n for n in names if " ".join(n.lower().split()) == q] or [n for n in names if q in n.lower()]
            or [n for n in names if named_like(n, asked)])


AFTER_HANG = ("Application Crash (After Hang) is Aternity's own name; an investigation lists it under hangs "
              "(not responding), not under crashes.")
EVENTS_ONLY = ("Counted: events Aternity recorded. An investigation also shows an application's hang time from "
               "Aternity's application usage (\"not responding\"), which is not an event and is not in these "
               "counts.")


def _event_notes(kinds, asked: bool) -> list[str]:
    """What a crash or event count does not say (the user's finding on stage, Nov 2026). The
    hang-time line only with an application's events: it says nothing of a System Crash."""
    out = [AFTER_HANG] if any("after hang" in (k[1] or "").lower() for k in kinds) else []
    of_app = any("applic" in f"{k[0]} {k[1]}".lower() or "hang" in (k[1] or "").lower() for k in kinds)
    return out + ([EVENTS_ONLY] if asked and of_app else [])


def _kinds_named(kinds, name) -> tuple[set, list[str]]:
    """The kinds of event a name means: those with it in their name; when none has, those that
    share a telling word with it ("hard drive failure" -> HD Failure), with a note saying so."""
    kinds = set(kinds)
    if not (name or "").strip():
        return kinds, []
    got = {k for k in kinds if _has(k[1], name)}
    if got:
        return got, []
    from .software import words
    ws = [w for w in words(name) if len(w) >= 3]
    near = {k for k in kinds if any(w in (k[1] or "").lower() for w in ws)}
    if not near:
        return set(), []
    return near, [f"No event is named like \"{name}\": shown are those that share a word with it "
                  f"({', '.join(sorted(k[1] for k in near)[:6])}{' …' if len(near) > 6 else ''})."]


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
        return [f"No usual yet: {u.get('days', 0)} of the {MIN_USUAL} {view['kind']} needed are saved so far."]
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
    every = apps_today | {a for d in have for a in saved[d].get("apps", {})}
    matched = _match(every, app) if app else sorted(every)
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
    if not app:  # how today compares, as on the Today tab, whatever the grouping
        notes += _usual_note(today.view(now))
        if group_by not in ("app", "activity"):  # those rows name the applications already
            lead = Counter()
            for h in hours:
                lead.update({a: c[TOP_STATUS] for a, c in h.apps.items() if c[TOP_STATUS]})
            top3 = sorted(lead.items(), key=lambda kv: (-kv[1], kv[0]))[:SAY_ROWS]
            if top3:
                notes.append(f"Most {TOP_STATUS} slow actions today: "
                             + ", ".join(f"{a} {_say(n)}" for a, n in top3) + ".")

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
        most = max((kv for kv in top if kv[1][1] >= SHARE_RUNS), key=lambda kv: kv[1][0] / kv[1][1], default=None)
        if most and top and most[0] != top[0][0]:
            notes.append(f"Highest {TOP_STATUS} share, of the applications with {SHARE_RUNS} runs or more: {most[0]}, "
                         f"{most[1][0]:,} of {most[1][1]:,} runs ({_share(most[1][0], most[1][1])}%).")
        cols = ["Application", f"{TOP_STATUS} slow actions", "Runs", f"{TOP_STATUS} share %"]
        rows = [[a, x[0], x[1], _share(x[0], x[1])] + ([len(x[2])] if days == 1 else []) for a, x in top[:ROWS]]
        if days == 1:
            cols.append(f"PCs with {TOP_STATUS}")
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
           severity: str | None = None, days=1, app: str | None = None) -> dict:
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
    used = {"group_by": group_by, "name": name, "severity": severity, "app": app, "days": days}
    app = (app or "").strip() or None

    wanted, loose = _kinds_named({x[2] for x in health} | {k for kinds in hd.values() for k in kinds}, name)

    def keep(k) -> bool:
        return k in wanted and (not severity or (sev.get(k) or "").lower() == severity.strip().lower())

    per_day: dict[date, dict] = {t: {}}
    per_app: dict[date, dict] = {t: {}}
    matched = _match({x[5] for x in health if keep(x[2])} | {k[1] for kinds in ha.values() for k in kinds
                                                             if keep(k[0])}, app) if app else []
    for x in health:
        if keep(x[2]) and (not app or x[5] in matched):
            per_day[t].setdefault(x[2], set()).add(x[1])
            per_app[t].setdefault((x[2], x[5]), set()).add(x[1])
    for d, kinds in hd.items():
        per_day[d] = {k: set(v) for k, v in kinds.items() if keep(k)}
    for d, kinds in ha.items():
        per_app[d] = {k: v for k, v in kinds.items() if keep(k[0]) and (not app or k[1] in matched)}
    if app:  # only that application's events: each day's PCs from the per-application counts
        for d in list(per_day):
            per_day[d] = {}
            for (k, _), devs in per_app.get(d, {}).items():
                per_day[d].setdefault(k, set()).update(devs)
    before = [t - timedelta(days=n) for n in range(days - 1, 0, -1)]
    notes = []
    missing = [d for d in before if d not in read]
    if missing:
        notes.append(f"{len(missing)} of the {len(before)} days before were not read yet (read once a day).")
    what = " ".join(w for w in (severity, f"\"{name}\"" if name else None) if w)
    in_app = ""
    if app:
        in_app = f" of {matched[0]}" if len(matched) == 1 else f" of applications named like \"{app}\""
        if not matched:
            notes.append(f"No application named like \"{app}\" had such an event in this period. Aternity records "
                         f"some crashes with no application name ({NO_APP}); \"Ask about software\" counts "
                         "crashes by the program file that crashed.")
        elif len(matched) > 1:
            notes.append(f"No application is named exactly \"{app}\"; {len(matched)} have it in their name, "
                         f"counted together: {', '.join(matched[:5])}{' …' if len(matched) > 5 else ''}.")
    counted = sorted({k for kinds in per_day.values() for k in kinds})
    notes += loose + _event_notes(counted, bool(app or name))
    if group_by != "app" and counted:
        notes.append("Counts only: to see which PCs, ask to list the PCs with it.")
    period = _period(today, t, days)

    if group_by == "event":
        pcs: dict = {}
        for kinds in per_day.values():
            for k, devs in kinds.items():
                pcs.setdefault(k, set()).update(devs)
        verdict = {}
        if days == 1 and not app:  # the usual is known for all applications together only
            view = today.view(now)
            word = {"more": "more than usual", "fewer": "fewer than usual", "usual": "as usual"}
            for e in view.get("tiles", []) + view.get("events", []):
                u = e["usual"]
                verdict[(e["category"], e["name"])] = (
                    f"{word[u['verdict']]} ({u['low']:,} to {u['high']:,})" if u.get("enough") else "not enough days")
        top = sorted(pcs.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        usual = days == 1 and not app
        rows = [[k[1], k[0], sev.get(k), len(v)] + ([verdict.get(k)] if usual else []) for k, v in top[:ROWS]]
        if usual:  # the rows' verdicts in one note, more than usual first (the AI reads notes best)
            said = {w: [k[1] for k, _ in top[:ROWS] if (verdict.get(k) or "").startswith(w)]
                    for w in ("more than usual", "fewer than usual", "as usual")}
            if any(said.values()):
                n = len(top[:ROWS])
                parts = [f"{w}: {', '.join(said[w])}" for w in ("more than usual", "fewer than usual") if said[w]]
                same = len(said["as usual"])
                if same:
                    parts.append(f"all {n} as usual" if same == n else f"as usual: {same} of the {n}")
                notes.append("Against the days before, by this time of day: " + "; ".join(parts) + ".")
        cols = ["Event", "Category", "Severity", "PCs"] + (["Usual by this time"] if usual else [])
        return _result("events", used, f"PCs with {what + ' ' if what else ''}events{in_app}, by event", period, cols, rows,
                       notes, f"{len(top)} kinds of event")

    if group_by == "day":
        rows = []
        for d in before + [t]:
            if d != t and d not in read:
                rows.append([f"{d:%a %b %d}", None])
                continue
            devs = set().union(*per_day.get(d, {}).values()) if per_day.get(d) else set()
            rows.append([f"{d:%a %b %d}" + (" (so far)" if d == t else ""), len(devs)])
        if len(counted) > 1 and name:  # "Application Crash" is also in "Application Crash (After Hang)"
            notes.insert(0, f"\"{name}\" matches {len(counted)} kinds of event, counted together (a PC with two "
                            f"counts once): {', '.join(k[1] for k in counted[:5])}{' …' if len(counted) > 5 else ''}.")
        return _result("events", used, f"PCs with {what + ' ' if what else ''}events{in_app}, per day", period,
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
    return _result("events", used, f"PCs with {what + ' ' if what else ''}events{in_app}, by application", period,
                   ["Application", "PCs", "Events"], rows, notes, f"{len(top)} applications")


def active(today: Today, now: datetime) -> dict:
    """Active users and PCs, as the strip at the top of the Today tab."""
    a = today.view(now).get("active")
    if not a:
        return _result("active", {}, "Active users and PCs", "", [], [], ["Today is still being read."])
    t, r = a["today"], a["recent"]
    rows = [[label, x["users"], x["pcs"], x["virtual"], x["physical"]]
            for label, x in (("Today so far", t), (f"Since {r['since']}", r))]
    notes = ["Active: at least one action Aternity measured in an application, whatever its rating. A PC that is "
             "on with nobody working in a measured application is not counted.",
             f"Users today: {t['users_virtual']:,} on a virtual desktop, {t['users_physical']:,} on a physical PC"
             + (f", {t['users_both']:,} of them on both" if t["users_both"] else "")
             + ". Virtual or physical is Aternity's own mark; a user who moved between desktops is one user and "
             "several PCs."]
    if t["not_known"]:
        notes.append(f"{t['not_known']:,} PCs today have no virtual or physical mark from Aternity.")
    say = (f"Active today so far: {_n(t['users'], 'user')} on {_n(t['pcs'], 'PC')} ({t['virtual']:,} virtual, "
           f"{t['physical']:,} physical). Since {r['since']}: {_n(r['users'], 'user')} on {_n(r['pcs'], 'PC')}.")
    return _result("active", {}, "Active users and PCs", _period(today, today.day(now), 1),
                   ["When", "Users", "PCs", "Virtual desktops", "Physical PCs"], rows, notes, say=say)


WHO_ROWS = 30  # PCs in a list over several days


def _who_days(today: Today, now: datetime, used: dict, name, app, days: int) -> dict:
    """The PCs with an event over several days (the user, Nov 2026: "List PCs with System crash
    this week" got today's one PC, then per-day counts). The days before keep the PC, not the user."""
    t = today.day(now)
    first = t - timedelta(days=days - 1)
    with today.lock:
        rows = [x for x in today.health if today.day(x[0]) == t]
        hd = {d: v for d, v in today.health_days.items() if first <= d < t}
        ha = {d: v for d, v in today.health_apps.items() if first <= d < t}
        read = {d for d in today.health_read if first <= d < t}
    wanted, notes = _kinds_named({x[2] for x in rows} | {k for kinds in hd.values() for k in kinds}, name)
    apps = _match({x[5] for x in rows if x[2] in wanted} | {k[1] for kinds in ha.values() for k in kinds
                                                           if k[0] in wanted}, app) if app else None
    pcs: dict[str, dict] = {}

    def add(dev, d, when, kind, user=None):
        p = pcs.setdefault(dev, {"days": set(), "last": when, "kinds": set(), "user": None})
        p["days"].add(d)
        p["kinds"].add(kind[1])
        p["last"] = max(p["last"], when)
        p["user"] = user or p["user"]
    for x in rows:
        if x[2] in wanted and (apps is None or x[5] in apps):
            add(x[1], t, x[0], x[2], x[4])
    for d, kinds in hd.items():
        for k, devs in kinds.items():
            if k not in wanted:
                continue
            ok = None if apps is None else set().union(*(ha.get(d, {}).get((k, a), set()) for a in apps))
            for dev, secs in devs.items():
                if ok is None or dev in ok:
                    add(dev, d, today.midnight(d) + timedelta(seconds=secs), k)
    period = _period(today, t, days)
    what = _join_names(sorted({k[1] for k in wanted})) if wanted else f"\"{name or 'any'}\""
    of = f" of {', '.join(apps[:3])}" if apps else ""
    missing = days - 1 - len(read)
    if missing > 0:
        notes.append(f"{missing} of the {days - 1} days before were not read yet (read once a day).")
    if not pcs:
        return _result("who", used, f"PCs with {what}{of}", period, [], [],
                       notes + [f"No PC had an event named like \"{name or 'any'}\"{of} in this period."])
    several = len({k for p in pcs.values() for k in p["kinds"]}) > 1
    order = sorted(pcs.items(), key=lambda kv: (-len(kv[1]["days"]), -kv[1]["last"].timestamp(), kv[0]))
    out, runs = [], []
    for dev, p in order[:WHO_ROWS]:
        out.append([p["user"] or "—", dev, len(p["days"])] + ([", ".join(sorted(p["kinds"]))] if several else [])
                   + [f"{today.local(p['last']):%a %b %d %H:%M}"])
        runs.append({"username": p["user"], "device": dev, "application": None,
                     "start": (p["last"] - EVENT_PAD).isoformat().replace("+00:00", "Z"),
                     "end": min(p["last"] + EVENT_PAD, now).replace(microsecond=0).isoformat().replace("+00:00", "Z")})
    notes.append("The user is known for today's events only: the days before keep the PC. Investigate ▸ runs for "
                 "the hour around the PC's last one.")
    if len(order) > WHO_ROWS:
        notes.append(f"The {WHO_ROWS} PCs with the most days are shown, of {len(order):,}.")
    notes += _event_notes(sorted(wanted), bool(app or name))
    return _result("who", used, f"PCs with {what}{of}", period,
                   ["User", "PC", "Days with it"] + (["Event"] if several else []) + ["Last"], out, notes,
                   _n(len(order), "PC"), runs=runs, label=(0, 1), value=2)


def _join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{len(names)} kinds of event ({', '.join(names[:3])}{' …' if len(names) > 3 else ''})"


def who(today: Today, now: datetime, what: str = "slow", app: str | None = None, name: str | None = None,
        days=1) -> dict:
    t = today.day(now)
    days = _clamp(days, 1, HEALTH_DAYS + 1, 1)
    used = {"what": what, "app": app, "name": name, "days": days}
    period = _period(today, t, 1)
    notes = []
    if what == "event" and days > 1:
        return _who_days(today, now, used, name, app, days)
    if what == "event":
        with today.lock:
            every = {x[2] for x in today.health if today.day(x[0]) == t}
            wanted, loose = _kinds_named(every, name)
            notes += loose
            mine = [x for x in today.health if today.day(x[0]) == t and x[2] in wanted]
        apps = _match({x[5] for x in mine}, app) if app else None
        kinds = sorted({x[2] for x in mine if apps is None or x[5] in apps})
        of = f" of applications named like \"{app}\"" if app else ""
        if not kinds:
            return _result("who", used, f"Users and PCs with \"{name or 'any'}\" events{of}", period, [], [],
                           [f"No event named like \"{name or 'any'}\"{of} today."])
        if len(kinds) > 1:  # all of them, each row saying which (it was only the one on the most PCs)
            notes.append(f"\"{name or ''}\" matches {len(kinds)} kinds of event{of}, all listed: "
                         f"{', '.join(k[1] for k in kinds[:5])}{' …' if len(kinds) > 5 else ''}.")
        if app and len(apps) > 1:
            notes.append(f"No application is named exactly \"{app}\"; {len(apps)} have it in their name, listed "
                         f"together: {', '.join(apps[:5])}{' …' if len(apps) > 5 else ''}.")
        notes += _event_notes(kinds, bool(app or name))
        w = today.who(now, "event", kinds=set(kinds), apps=set(apps) if apps is not None else None)
    else:
        name_of = None
        if app:
            with today.lock:
                totals: dict = {}
                for h in today.hours.values():
                    if h.day == t:
                        for a, c in h.apps.items():
                            if c[TOP_STATUS]:
                                totals[a] = totals.get(a, 0) + c[TOP_STATUS]
            totals = {a: totals[a] for a in _match(totals, app)}
            if not totals:
                return _result("who", used, f"Users and PCs with {TOP_STATUS} slow actions in \"{app}\"", period,
                               [], [], [f"No application named like \"{app}\" had a {TOP_STATUS} action today."])
            name_of = max(totals, key=lambda a: (totals[a], a))
            if len(totals) > 1:
                notes.append(f"No application is named exactly \"{app}\"; {len(totals)} have it in their name: "
                             f"{', '.join(sorted(totals)[:5])}. This is the one with the most: {name_of}.")
        w = today.who(now, "app", name_of) if name_of else today.who(now, "slow")
    event = w["kind"] == "event"
    several = event and len(w.get("kinds") or []) > 1
    rows, runs = [], []
    for r in w["rows"][:ROWS]:
        slowest = None if event else " ".join(str(v) for v in (r.get("application"), r.get("activity")) if v)
        if not event and r.get("seconds") is not None:
            slowest = f"{slowest} {r['seconds']:.1f} s"
        which = [", ".join(r.get("events") or [])] if event and several else []
        rows.append([r["username"] or "—", r["device"], r["count"]] + ([] if event else [slowest]) + which
                    + [r["first"] if r["first"] == r["last"] else f"{r['first']}–{r['last']}"])
        runs.append({"username": r["username"], "device": r["device"], "application": w.get("application"),
                     "start": r["start"], "end": r["end"]})
    cols = (["User", "PC", "Times" if event else f"{TOP_STATUS} slow"] + ([] if event else ["Slowest"])
            + (["Event"] if several else []) + ["When"])
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
            known = _match({a for h in today.hours.values() for a in h.apps if a != NO_APP}, app)
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
            return events(today, now, **{k: args[k] for k in ("group_by", "name", "severity", "days", "app")
                                         if k in args})
        if name == "who":
            return who(today, now, **{k: args[k] for k in ("what", "app", "name", "days") if k in args})
        if name == "active":
            return active(today, now)
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
    # markdown marks; an underscore inside a name stays (GPT's "bad_module_info" became
    # "badmoduleinfo" on stage): only one at the edge of a word is emphasis
    # Lines are kept (SHAPE): a bullet of any kind ("*", "•", "1.") becomes "- ".
    out = []
    for line in str(text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•–]|\d{1,2}[.)])\s+", "- ", line)
        line = re.sub(r"[*`#]{1,3}|(?<![A-Za-z0-9])_{1,3}|_{1,3}(?![A-Za-z0-9])", "", line)
        line = " ".join(line.split())
        if line and line != "-":
            out.append(line)
    return "\n".join(out[:LINES_MAX])[:TEXT_MAX]


def system(today: Today, now: datetime) -> str:
    return f"{SYSTEM} Today is {today.local(now):%A %b %d, %Y}; times are {today.zone_label}."


def ask(question: str, history: list, client: AiClient, today: Today, names, now: datetime) -> dict:
    """One question on the Today tab (see converse)."""
    return converse(question, history, client, system(today, now), TOOLS,
                    lambda name, args: run_block(name, args, today, names, now), NO_COUNT)


def converse(question: str, history: list, client: AiClient, system_text: str, tools: list, run, no_count: str) -> dict:
    """One question: the AI picks a building block, `run(name, args)` counts (None: no such
    block), the AI words it, held to our numbers (the Today and Software tabs' chats)."""
    started = time.monotonic()
    question = " ".join(str(question or "").split())[:500]
    messages = [{"role": "system", "content": system_text}]
    for h in (history or [])[-HISTORY:]:
        if isinstance(h, dict) and h.get("q") and h.get("a"):
            messages += [{"role": "user", "content": str(h["q"])[:500]},
                         {"role": "assistant", "content": str(h["a"])[:800]}]
    messages.append({"role": "user", "content": question})
    out = {"question": question, "answer": "", "result": None, "note": None, "service": client.cfg.name}
    reply = client.chat(messages, tools)
    call = tool_call(reply)
    if call is None:
        text = clean(reply.get("content"))
        if numbers(text) - allowed(question, system_text):
            text, out["note"] = no_count, "The AI answered with numbers but no counts behind them, so it is not shown."
        out.update(answer=text or no_count, seconds=round(time.monotonic() - started, 1))
        return out
    name, args, call_id = call
    result = run(name, args)
    if result is None:
        out.update(answer=no_count, note=f"The AI asked for a building block we do not have ({name}).",
                   seconds=round(time.monotonic() - started, 1))
        return out
    given = {k: result[k] for k in ("title", "period", "columns", "rows", "total", "notes")}
    messages += [{"role": "assistant", "content": reply.get("content") or "", "tool_calls": reply["tool_calls"]},
                 {"role": "tool", "tool_call_id": call_id, "content": json.dumps(given, default=str)}]
    final = client.chat(messages, tools)
    text = clean(final.get("content"))
    if tool_call(final) or not text:
        text, out["note"] = result["say"], "The AI did not word an answer, so this is our own sentence."
    else:
        extra = numbers(text) - allowed(json.dumps(given, default=str), json.dumps(args, default=str), question,
                                        system_text, small=len(result["rows"]))
        ratio = RATIO.search(text)
        usual = usual_wrong(text, given)
        if extra:
            shown = ", ".join(f"{x:g}" for x in sorted(extra)[:3])
            text, out["note"] = result["say"], (f"The AI's sentence had a number that is not in the counts "
                                                f"({shown}), so this is our own sentence.")
        elif ratio:
            text, out["note"] = result["say"], (f"The AI's sentence worked out a comparison the counts do not give "
                                                f"(\"{ratio.group(0)}\"), so this is our own sentence.")
        elif usual:
            text, out["note"] = result["say"], (f"The AI's sentence said \"{usual}\", but the counts do not say "
                                                "so, so this is our own sentence.")
    out.update(answer=text, result=result, seconds=round(time.monotonic() - started, 1))
    return out
