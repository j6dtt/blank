"""The assessment for executives, at the top of the Today tab (`/api/assess`).

The user's request (2026-11-11): "an AI overall assessment for executive hourly or 3-hour
(what new in the last x hour and overall today up to the hour)". Choices taken ("all
recommended"): every 3 hours (`HOURS`, from 06:00: the user, 2026-11-12), counts and application names only
(no user, no PC), one summary of the day written after midnight and shown the next morning
as "Yesterday" (written once, so the two never differ), kept `KEEP_DAYS` days.

Our code works the facts out, as plain lines under four labels (`PARTS`: Changed, Watch,
Earlier, Start-ups) with a sentence on what the day means for people working (`meaning`); the
AI words them, and every number in its text must be in those lines, as in the chats, and the
fixed words (`reach`: isolated, limited, widespread; above, within or below the usual range;
rose, fell, steady, new, cleared) only where a line gives them; otherwise our own text
(`ours`) is shown. Written once per period and saved in `<data>/assess/<date>.json`, so
everyone reads the same text and it costs one AI call a period. The verdict and the "look
first at" line are by fixed rules (`verdict`, `first`), never the AI's.

Layout (Proposal 1, the user's pick, Nov 12 2026): the box is the banner and the words, with
yesterday beside them; the three charts (`charts`) are a row of their own in Today's issues.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .chat import RATIO, allowed, clean, numbers
from .config import SEVERITY_ORDER, TOP_STATUS
from .http import ApiError, short_error
from .models import plural
from .today import MIN_USUAL, USUAL_DAYS, kind_of

HOURS = (6, 9, 12, 15, 18)  # local time: an assessment at each; the first read after it writes it
KEEP_DAYS = 8
TOP = 3  # events, applications named in one assessment
MIN_ACTIONS = 50  # an hour with fewer measured actions has no share (one slow action would move it a lot)
MIN_STARTS = 5  # an hour, or a type, with fewer start-ups has no typical time
BOOTS_EVERY = timedelta(minutes=55)  # today's start-ups are read again after this (the charts are "up to the hour")
VERDICT_PCS = 5  # fewer PCs than this do not make the day's verdict (as software.MIN_PROBLEM)
ISOLATED = VERDICT_PCS  # under this many PCs: isolated (too few to make the verdict)
WIDESPREAD = 50  # this many PCs or more: widespread; in between: limited (my suggestion, Nov 2026)
TEXT_MAX = 2000  # the AI's text (the chats' answers are shorter)
SOURCES = ("Aternity", "NetIM")  # a connection down that an executive must know of
ATTENTION, WORSE, NORMAL, UNKNOWN = ("Needs attention", "Worse than usual", "A normal day so far",
                                     "No usual to compare with yet")
log = logging.getLogger("uxcorrelate")

SYSTEM = ("You write a short assessment for executives of how the company's PC users are doing today, from "
          "Riverbed Aternity. You are given the verdict, what the day means for people working, and the facts as "
          "lines under the labels Changed, Watch, Earlier and Start-ups, then the day so far and what to look at "
          "first. Write it as plain text with no markdown and no table, in this shape: first one or two sentences: "
          "the verdict as it is given, then what the day means for people working, in plain words; then two to four "
          "lines that each start with \"- \" and give one thing, by name, with its number, most serious first, each "
          "starting with the label of the facts it comes from (\"Changed: \", \"Watch: \", \"Earlier: \" or "
          "\"Start-ups: \") when they have one; leave out what does not matter to an executive; then one last line "
          "that starts with \"Next: \" and gives the \"look first at\". The words isolated, limited, widespread, "
          "above, within or below the usual range, rose, fell, steady and cleared are set by rules on the counts: "
          "use them only where a line gives them, for that same count. Use only numbers that are in the lines, "
          "written exactly as given, and do not work out new ones (no sums, no new percentages, no \"twice as "
          "many\"). Never name a user or a PC. No greeting, no sign-off.")

# The fixed words (the user's choice, Nov 12 2026: "the richer wording"): each is set by a rule on the
# counts, so two days described the same way really were the same.
OLD = {"more than usual": "above", "fewer than usual": "below", "as usual": "within"}  # saved before Nov 12
RATED = re.compile(r"\b(isolated|limited|widespread|rose|fell|steady|cleared)\b", re.I)
PLACED = re.compile(r"\b(above|within|below)\b\W+(?:\w+\W+){0,2}?usual\b", re.I)


def reach(pcs: int) -> str:
    """How far it reaches: isolated under `ISOLATED` PCs, limited under `WIDESPREAD`, else widespread."""
    return "isolated" if pcs < ISOLATED else "limited" if pcs < WIDESPREAD else "widespread"


def _usual(u: dict | None) -> dict | None:
    """A count's place against the same kind of day before by this time (today.usual): above,
    within or below its usual range (the lowest to the highest of those days); None: not known yet."""
    if not u or not u.get("enough"):
        return None
    return {"pos": {"more": "above", "fewer": "below"}.get(u.get("verdict"), "within"),
            "low": u["low"], "high": u["high"], "days": u["days"]}


def _pos(u) -> str | None:
    """above / within / below; also from the words an assessment saved before Nov 12."""
    return u.get("pos") if isinstance(u, dict) else OLD.get(u)


def _pcs(n: int) -> str:
    return f"{n:,} PC" if n == 1 else f"{n:,} PCs"


def _against(u, whose: str = "its") -> str:
    """'above its usual range (0 to 1 PCs by this time)'; 'no usual to compare with yet'."""
    pos = _pos(u)
    if pos is None:
        return "no usual to compare with yet"
    if not isinstance(u, dict):
        return f"{pos} {whose} usual range"
    lo, hi = u["low"], u["high"]
    return f"{pos} {whose} usual range ({_pcs(lo) if lo == hi else f'{lo:,} to {hi:,} PCs'} by this time)"


def _what(e: dict) -> str:
    """'Application Crash (Major)'; the slow actions already say their rating."""
    name, sev = e.get("what") or e["name"], e.get("severity")
    return name if not sev or name.startswith(sev) else f"{name} ({sev})"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _rated(e: dict) -> bool:
    return e.get("severity") in SEVERITY_ORDER[:2]


def _rank(e: dict) -> tuple:
    """Most serious first: Aternity's severity, then a crash, a hang, then the PCs."""
    name = (e.get("name") or "").lower()
    sev = SEVERITY_ORDER.index(e["severity"]) if e.get("severity") in SEVERITY_ORDER else len(SEVERITY_ORDER)
    return (sev, 0 if "crash" in name else 1 if "hang" in name else 2, _pos(e.get("usual")) != "above", -e["pcs"])


def chart_facts(charts: list[dict]) -> dict:
    """What the charts give the words (no PC, no user): the latest point, and the hours of
    today above the usual range (the per-hour ones make "Earlier" and "Watch")."""
    return {c["key"]: {"what": c.get("what") or c["title"], "unit": c["unit"], "now": c.get("now"),
                       "above": [p["h"] for p in c["points"] if p["v"] is not None and p.get("hi") is not None
                                 and p["v"] > p["hi"]]}
            for c in charts}


def snapshot(today_view: dict, care_view: dict | None, status_view: dict | None, boots: dict | None = None,
             charts: dict | None = None) -> dict:
    """What an assessment rests on, as counts and names of events and applications: no user,
    no PC. Saved with it, so the next one can say what changed."""
    s = today_view["slow"]
    events = [{"name": e["name"], "category": e["category"], "severity": e.get("severity"), "pcs": e["pcs"],
               "usual": _usual(e.get("usual"))} for e in today_view.get("events", []) if e["pcs"]]
    users = (care_view or {}).get("users", [])
    return {
        "kind": today_view.get("kind"),
        "active": {k: today_view["active"]["today"][k] for k in ("users", "pcs", "virtual", "physical")},
        "events": sorted(events, key=_rank),
        "slow": {"pcs": s["pcs"], "actions": s["actions"], "users": s["users"], "usual": _usual(s.get("usual"))},
        "apps": {a["application"]: a["actions"] for a in today_view.get("applications", [])},
        "care": {"listed": len(users), "with_issue": sum(1 for u in users if u.get("status") == "issue"),
                 "issues": sum(len(u.get("issues", [])) + len(u.get("cleared", [])) for u in users)},
        "down": sorted(i["name"] for i in (status_view or {}).get("items", []) if i.get("state") == "down"),
        # start-ups today, per Aternity type: how many, the typical time, the days before (boots_view)
        "boots": boots,
        "charts": charts or {},
    }


def _counts(snap: dict) -> list[dict]:
    """The counts rated Critical or Major: the events, then the slow actions."""
    return [e for e in snap["events"] if _rated(e)] + [dict(snap["slow"], severity=TOP_STATUS, what=f"{TOP_STATUS} slow actions")]


def _judged(snap: dict) -> tuple[str, list[str]]:
    """The verdict and why, by fixed rules. A count rated Critical or Major that is above its
    usual range on `VERDICT_PCS` PCs or more makes the day worse than usual (Critical: needs
    attention), as does a connection to Aternity or NetIM being down. Fewer PCs than that are
    isolated: listed, but 2 PCs against a usual 0 to 1 are not the company's day (stage, Nov 2026)."""
    top = _counts(snap)
    above = [c for c in top if _pos(c["usual"]) == "above"]
    more = [c for c in above if c["pcs"] >= VERDICT_PCS]
    why = [f"{_what(c)} is on {_pcs(c['pcs'])}, {reach(c['pcs'])}, above its usual range" for c in more]
    down = [n for n in snap["down"] if n in SOURCES]
    if down:
        return ATTENTION, [f"the connection to {' and '.join(down)} is down"] + why
    if any(c["severity"] == "Critical" for c in more):
        return ATTENTION, why
    if more:
        return WORSE, why
    if not any(_pos(c["usual"]) for c in top):
        return UNKNOWN, ["the days before are not saved yet for any count rated Critical or Major"]
    small = [c for c in above if c["pcs"] < VERDICT_PCS]
    return NORMAL, [f"nothing rated Critical or Major is above its usual range on {VERDICT_PCS} PCs or more"
                    + (f" ({', '.join(_what(c) + ': ' + _pcs(c['pcs']) for c in small)}, isolated)" if small else "")]


def verdict(snap: dict) -> str:
    return _judged(snap)[0]


def first(snap: dict) -> str:
    """What to look at first: a connection down, else the most serious event (Critical or
    Major), else the slow actions when above their usual range, else nothing to act on."""
    down = [n for n in snap["down"] if n in SOURCES]  # an AI service down is said, never what to look at first
    if down:
        return (f"the connection to {' and '.join(down)}, which is down (the lights at the top right of "
                "the page say since when)")
    top = [e for e in snap["events"] if _rated(e)]
    if top:
        e = top[0]
        pos = _pos(e["usual"])
        return (f"{e['name']} on {_pcs(e['pcs'])} ({e['severity']}, {reach(e['pcs'])}"
                + (f", {pos} its usual range" if pos else "") + "): on the Today tab, click its tile for who has it")
    if _pos(snap["slow"]["usual"]) == "above":
        app = max(snap["apps"], key=snap["apps"].get, default=None)
        return (f"{TOP_STATUS} slow actions, above their usual range" + (f"; the application with the most is {app}" if app else "")
                + ": on the Today tab, click it for who has them")
    return "nothing needs action now; the next assessment says what changes"


def _event_line(e: dict) -> str:
    return f"{_what(e)}: {_pcs(e['pcs'])}, {reach(e['pcs'])}, {_against(e['usual'])}"


def _logon(boots: dict | None) -> tuple[str, dict] | None:
    """The start-up type an executive feels: the one with "logon" in its name, else the most."""
    types = (boots or {}).get("types") or {}
    return next(((n, x) for n, x in types.items() if "logon" in n.lower()), next(iter(types.items()), None))


def _time_pos(x: dict) -> str | None:
    u = x.get("usual")
    return None if not u else "above" if x["typical"] > u["high"] else "below" if x["typical"] < u["low"] else "within"


def _point_pos(p: dict | None) -> str | None:
    if not p or p.get("lo") is None:
        return None
    return "above" if p["v"] > p["hi"] else "below" if p["v"] < p["lo"] else "within"


def _fmt(v, unit: str) -> str:
    return f"{v:g}%" if unit == "%" else f"{v:,}" if unit == "PCs" else f"{v:g} {unit}"


def meaning(snap: dict) -> str:
    """What the day means for people working: the three things they feel (slow actions, PCs
    with a crash, the typical logon), each against its usual range."""
    parts = []
    s = _pos(snap["slow"]["usual"])
    parts.append(f"{TOP_STATUS} slow actions are {s} their usual range" if s
                 else f"{TOP_STATUS} slow actions have no usual to compare with yet")
    crash = _point_pos((snap.get("charts") or {}).get("crash", {}).get("now"))
    if crash:
        parts.append(f"PCs with a crash so far are {crash} their usual range")
    logon = _logon(snap.get("boots"))
    if logon and _time_pos(logon[1]):
        parts.append(f"a typical {logon[0]} is {_time_pos(logon[1])} its usual range")
    return f"Against the {snap.get('kind') or 'days'} before: {_join(parts)}."


def changed_lines(snap: dict, before: dict | None, since: str | None) -> tuple[list[str], set, str | None]:
    """What moved since the assessment before: new, rose (counts of the day only rise or stay),
    rose or fell (the share of actions rated Major, an hour against an hour), connections and
    the Care list; and one line of what stayed steady. None before: the first of the day."""
    if before is None:
        return [], set(), None
    out, keys, steady = [], set(), []
    for n in snap["down"]:
        if n not in before["down"]:
            out.append(f"The connection to {n} went down.")
    for n in before["down"]:
        if n not in snap["down"]:
            out.append(f"The connection to {n} is back.")
    was = {(e["category"], e["name"]): e for e in before["events"]}
    for e in snap["events"]:
        if not (_rated(e) or _pos(e["usual"]) == "above"):  # a Minor event is not executive news
            continue
        k, old = (e["category"], e["name"]), was.get((e["category"], e["name"]))
        if old is None:
            out.append(f"{_what(e)} is new since {since}: {_pcs(e['pcs'])}, {reach(e['pcs'])}, {_against(e['usual'])}.")
            keys.add(k)
        elif e["pcs"] > old["pcs"]:
            out.append(f"{_what(e)} rose from {old['pcs']:,} to {_pcs(e['pcs'])} since {since}: {reach(e['pcs'])}, "
                       f"{_against(e['usual'])}.")
            keys.add(k)
        elif _rated(e):
            steady.append(e)
    s, b = snap["slow"], before["slow"]
    if s["pcs"] > b["pcs"]:
        out.append(f"{TOP_STATUS} slow actions rose from {b['pcs']:,} to {_pcs(s['pcs'])} since {since}, "
                   f"{_against(s['usual'], 'their')}.")
        keys.add("slow")
    risen = sorted(((a, n - before["apps"].get(a, 0)) for a, n in snap["apps"].items()
                    if n > before["apps"].get(a, 0)), key=lambda kv: (-kv[1], kv[0]))[:TOP]
    if risen:
        out.append(f"{TOP_STATUS} slow actions added since {since}, by application: "
                   + ", ".join(f"{a} ({n:,})" for a, n in risen) + ".")
    c = (snap.get("charts") or {}).get("share") or {}
    o = ((before.get("charts") or {}).get("share") or {}).get("now")
    if c.get("now") and o and c["now"]["h"] != o["h"]:
        p, u = c["now"], c["unit"]
        moved = "rose" if p["v"] > o["v"] else "fell" if p["v"] < o["v"] else "steady"
        place = _point_pos(p)
        out.append(f"{c['what']} {moved}: {_fmt(o['v'], u)} at {o['h']:02d}:00, {_fmt(p['v'], u)} at {p['h']:02d}:00"
                   + (f", {place} its usual range ({_fmt(p['lo'], u)} to {_fmt(p['hi'], u)} at that hour)" if place else "")
                   + ".")
    more = snap["care"]["issues"] - before["care"]["issues"]
    if more > 0:
        out.append(f"Care list: {plural(more, 'new issue')} for the users watched.")
    line = (f"Steady since {since}: " + ", ".join(f"{_what(e)} ({_pcs(e['pcs'])})" for e in steady[:TOP]) + ".") if steady else None
    return out, keys, line


def _spells(hours: list[int]) -> list[tuple[int, int]]:
    """Hours in a row: [8, 9, 13] -> [(8, 9), (13, 13)]."""
    out: list[tuple[int, int]] = []
    for h in sorted(hours):
        if out and h == out[-1][1] + 1:
            out[-1] = (out[-1][0], h)
        else:
            out.append((h, h))
    return out


def _span(a: int, b: int) -> str:
    return f"from {a:02d}:00 to {(b + 1) % 24:02d}:00"


def watch_lines(snap: dict, changed: set) -> list[str]:
    """What is above its usual range now and did not move since the assessment before (what
    moved is said under Changed, once): events rated Critical or Major, the slow actions, and an
    hour-by-hour line above its range up to now."""
    out = []
    for e in snap["events"]:
        if _rated(e) and _pos(e["usual"]) == "above" and (e["category"], e["name"]) not in changed:
            out.append(f"{_what(e)} is on {_pcs(e['pcs'])}, {_against(e['usual'])}: {reach(e['pcs'])}"
                       + (", too few to change the day's verdict" if e["pcs"] < VERDICT_PCS else "") + ".")
    s = snap["slow"]
    if _pos(s["usual"]) == "above" and "slow" not in changed:
        out.append(f"{TOP_STATUS} slow actions are on {_pcs(s['pcs'])}, {_against(s['usual'], 'their')}.")
    for key in ("share", "logon"):
        c = (snap.get("charts") or {}).get(key) or {}
        spells, p = _spells(c.get("above", [])), c.get("now")
        if spells and p and spells[-1][1] == p["h"]:
            out.append(f"{c['what']} has been above its usual range since {spells[-1][0]:02d}:00: {_fmt(p['v'], c['unit'])} "
                       f"at {p['h']:02d}:00, against {_fmt(p['lo'], c['unit'])} to {_fmt(p['hi'], c['unit'])}.")
    return out


def earlier_lines(snap: dict, earlier: list[tuple[str, dict]]) -> list[str]:
    """What was above its usual range earlier today and is not now (cleared): hours of the
    per-hour lines, and counts an earlier assessment had above their range."""
    out = []
    for key in ("share", "logon"):
        c = (snap.get("charts") or {}).get(key) or {}
        spells, p = _spells(c.get("above", [])), c.get("now")
        on = bool(spells and p and spells[-1][1] == p["h"])
        done = spells[:-1] if on else spells
        if done:
            out.append(f"{c['what']} was above its usual range {_join([_span(a, b) for a, b in done])}"
                       + ("." if on else "; cleared since."))
    now = {(e["category"], e["name"]): e for e in snap["events"]}
    seen: dict = {}
    for label, s in earlier:  # the earliest it was said
        for e in s["events"]:
            if _rated(e) and _pos(e["usual"]) == "above":
                seen.setdefault((e["category"], e["name"]), (label, e))
        if _pos(s["slow"]["usual"]) == "above":
            seen.setdefault("slow", (label, dict(s["slow"], name=f"{TOP_STATUS} slow actions")))
    for k, (label, e) in seen.items():
        cur = snap["slow"] if k == "slow" else now.get(k)
        if cur is None or _pos(cur["usual"]) != "above":
            out.append(f"{_what(e)} was above its usual range at {label}; cleared since"
                       + (f": {_against(cur['usual'], 'their' if k == 'slow' else 'its')} now." if cur else "."))
    return out


def startup_lines(boots: dict | None, kind: str | None = None) -> list[str]:
    """Start-up and logon times today, each of Aternity's types against its usual range."""
    if not boots or not boots.get("types"):
        return []
    unit, out = boots.get("unit", "s"), []
    for name, x in list(boots["types"].items())[:TOP]:
        u, pos = x.get("usual"), _time_pos(x)
        out.append(f"A typical {name} takes {x['typical']:g} {unit} today ({x['n']:,} so far), "
                   + (f"{pos} its usual range ({u['low']:g} to {u['high']:g} {unit} on the {u['days']} "
                      f"{kind or 'days'} before)" if u else "no usual to compare with yet") + ".")
    return out


def today_lines(snap: dict) -> list[str]:
    """The day so far."""
    a, s = snap["active"], snap["slow"]
    out = [f"Active: {plural(a['users'], 'user')} on {_pcs(a['pcs'])} ({a['virtual']:,} virtual desktops, "
           f"{a['physical']:,} physical PCs).",
           f"{TOP_STATUS} slow actions: {s['actions']:,} on {_pcs(s['pcs'])}, {_against(s['usual'], 'their')}."]
    rated = [e for e in snap["events"] if _rated(e)]
    # the most serious, and every one above its usual range: what the verdict rests on is always shown
    top = rated[:TOP] + [e for e in rated[TOP:] if _pos(e["usual"]) == "above"]
    out += [f"{_event_line(e)}." for e in top] or ["No crash or event rated Critical or Major."]
    if len(rated) > len(top):
        out.append(f"{plural(len(rated) - len(top), 'other kind')} of event rated Critical or Major, none above "
                   "its usual range.")
    apps = sorted(snap["apps"].items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]
    if apps:
        out.append(f"Applications with the most {TOP_STATUS} slow actions: "
                   + ", ".join(f"{a} ({n:,})" for a, n in apps) + ".")
    c = snap["care"]
    if c["listed"]:
        out.append(f"Care list: {c['with_issue']:,} of the {plural(c['listed'], 'user')} watched "
                   f"{'has' if c['with_issue'] == 1 else 'have'} an open issue.")
    return out


def facts(snap: dict, earlier: list[tuple[str, dict]] | None = None) -> dict:
    """Everything an assessment says, as lines under their labels. `earlier`: today's
    assessments before this one, (time, snapshot), oldest first."""
    earlier = earlier or []
    before, since = (earlier[-1][1], earlier[-1][0]) if earlier else (None, None)
    changed, keys, steady = changed_lines(snap, before, since)
    said, why = _judged(snap)
    return {"verdict": said, "why": why, "since": since, "meaning": meaning(snap),
            "changed": changed + ([steady] if steady else []), "nothing_new": before is not None and not changed,
            "watch": watch_lines(snap, keys), "earlier": earlier_lines(snap, earlier),
            "startups": startup_lines(snap.get("boots"), snap.get("kind")),
            "today": today_lines(snap), "first": first(snap)}


PARTS = (("Changed", "changed"), ("Watch", "watch"), ("Earlier", "earlier"), ("Start-ups", "startups"))
LABELS = tuple(label for label, _ in PARTS)  # one on each point, so the eye finds what kind of news it is


def given(f: dict, label: str) -> str:
    """The facts as the lines the AI is given (and the numbers and words its text is held to)."""
    because = f" Because: {'; '.join(f['why'])}." if f.get("why") else ""
    out = [f"Assessment at {label}.", f"Verdict: {f['verdict']}.{because}"]
    if f.get("meaning"):
        out.append(f"For people working: {f['meaning']}")
    if f["since"]:
        out += [f"Changed since {f['since']}:"] + [f"- {x}" for x in f.get("changed") or ["Nothing changed."]]
    elif f.get("meaning"):
        out.append("Changed: nothing to compare with yet (the first assessment of the day).")
    for head, key in PARTS[1:]:
        if f.get(key):
            out += [f"{head}:"] + [f"- {x}" for x in f[key]]
    return "\n".join(out + ["The day so far:", *f["today"], f"Look first at: {f['first']}."])


def ours(f: dict) -> str:
    """Our own text in the same shape, when the AI's is not shown: the verdict and what it
    means, one point of each label (then more of what changed and what to watch), next."""
    head = f"{f['verdict']}" + (f": {'; '.join(f['why'])}" if f.get("why") else "") + "."
    if f.get("meaning"):
        head += " " + f["meaning"]
    pts = [f"- {label}: {f[key][0]}" for label, key in PARTS if f.get(key)]
    pts += [f"- {label}: {x}" for label, key in PARTS[:2] for x in f.get(key, [])[1:]]
    pts = pts[:4] + [f"- {x}" for x in f["today"][1:]][:max(0, 2 - len(pts))]
    return "\n".join([head, *pts, f"Next: look first at {f['first']}."])


def refused(text: str, lines: str) -> str | None:
    """Why the AI's text is not shown (None: fine): empty, a number that is not in the lines, a
    worked-out comparison, or one of the fixed words where no line gives it (a user or PC
    cannot be named right: it is given none)."""
    if not text:
        return "it gave no text"
    extra = numbers(text) - allowed(lines, SYSTEM)
    if extra:
        return "a number that is not in the facts (" + ", ".join(f"{x:g}" for x in sorted(extra)[:3]) + ")"
    ratio = RATIO.search(text)
    if ratio:
        return f"a comparison the facts do not give (\"{ratio.group(0)}\")"
    have = lines.lower()
    for m in RATED.finditer(text):
        if not re.search(rf"\b{m.group(1).lower()}\b", have):
            return f"the word \"{m.group(1)}\", which no line gives"
    said = {m.group(1).lower() for m in PLACED.finditer(have)}
    for m in PLACED.finditer(text):
        if m.group(1).lower() not in said:
            return f"\"{m.group(0)}\", which no line gives"
    return None


def word(client, f: dict, label: str, service: str = "") -> tuple[str | None, str | None]:
    """The AI's text for these facts, and a note when it is not shown."""
    if client is None:
        return None, "No AI service is set (ai.services in config.yaml): these are the facts as counted."
    lines = given(f, label)
    try:
        text = clean(client.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": lines}])
                     .get("content"), size=TEXT_MAX)
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
    out += [f"{k[1]} ({sev[k]}): {_pcs(n)}, {reach(n)}." for k, n in top] or ["No crash or event rated Critical or Major."]
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
        self.boots_note: str | None = "The start-ups are read with the next read of Aternity."  # why there is no logon chart
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
                try:  # the start-ups failing must not stop the assessment being written
                    self._boots(now)
                except Exception as exc:
                    self.boots_note = f"The start-ups could not be read ({short_error(exc)}); tried again with the next read."
                    log.warning("Assessment: %s", self.boots_note)
                self._yesterday(now, client_of)
                slot = self.due(now)
                if slot is not None:
                    self._write(now, slot, client_of)
        except Exception as exc:  # the reader goes on; the next read tries again
            log.warning("Assessment: could not be written (%s)", short_error(exc))

    def _write(self, now: datetime, slot: int, client_of) -> None:
        day = self.today.day(now).isoformat()
        rec = self.days.setdefault(day, {"date": day, "periods": [], "summary": None})
        snap = snapshot(self.today.view(now), self.care.view(now) if self.care else None,
                        self.status.view() if self.status else None, self.boots_view(now),
                        chart_facts(self.charts(now)))
        label = self.today.local(now).strftime("%H:%M")
        f = facts(snap, [(p["label"], p["snap"]) for p in rec["periods"] if p.get("snap")])
        client, service = client_of()
        text, note = word(client, f, label, service)
        rec["periods"].append({"slot": slot, "label": label, "at": now.isoformat().replace("+00:00", "Z"), **f,
                               "ours": ours(f), "text": text, "note": note, "service": service if text else "",
                               "snap": snap})
        self._save(day, now)
        log.info("Assessment written at %s: %s; %d changed since %s; %s", label, f["verdict"],
                 len(f["changed"]), f["since"] or "the start of the day", f"worded by {service}" if text else note)

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
        f = {"verdict": "The day in short", "why": [], "since": None, "meaning": "", "changed": [], "nothing_new": False,
             "watch": [], "earlier": [], "startups": [], "today": lines,
             "first": "today's first assessment, for what carries over"}
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
            self.boots_note = "Aternity is not set, so no start-ups are read."
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
                self.boots_note = (f"Aternity's start-ups of {d:%b %d} could not be read ({client.warnings[-1]}); "
                                   "tried again with the next read.")
                log.warning("Assessment: %s", self.boots_note)
                return
            rec["boots"] = boots_of(rows, self.today.local)
            self._save(d.isoformat(), now)
        self.boots_at = now
        got = self.days[t.isoformat()]["boots"]
        self.boots_note = None if any(x["n"] >= MIN_STARTS for x in got.values()) else (
            f"Aternity has {sum(x['n'] for x in got.values())} start-ups for today so far: too few for a typical time.")
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

        out = [{"key": "share", "title": f"Share of actions rated {TOP_STATUS}, per hour", "unit": "%", "points": share_pts,
                "what": f"The share of actions rated {TOP_STATUS}"},
               {"key": "crash", "title": "PCs with a crash, so far today", "unit": "PCs", "points": crash_pts,
                "what": "PCs with a crash so far"}]
        boots = self.days.get(t.isoformat(), {}).get("boots") or {}
        name = next((n for n in sorted(boots, key=lambda n: -boots[n]["n"]) if "logon" in n.lower()),
                    max(boots, key=lambda n: boots[n]["n"], default=None))
        if name:
            before = [self.days.get(d.isoformat(), {}).get("boots") or {} for d in self._same_days(t)]
            out.append({"key": "logon", "title": f"Typical {name} time, per hour", "unit": self.unit_of(),
                        "what": f"The typical {name} time", "points": [
                {"h": h, "v": boots[name]["hours"].get(str(h)) if h <= cur else None,
                 **dict(zip(("lo", "hi"), band([b.get(name, {}).get("hours", {}).get(str(h)) for b in before])))}
                for h in range(24)]})
        else:  # said in the chart's place, never a silent gap (stage, Nov 2026: the logon chart was just not there)
            out.append({"key": "logon", "title": "Typical logon time, per hour", "unit": self.unit_of(), "points": [],
                        "what": "The typical logon time",
                        "note": self.boots_note or "No start-up has a time today yet."})
        for c in out:  # the last hour with a value: the number beside the chart
            last = next((p for p in reversed(c["points"]) if p["v"] is not None), None)
            c["now"] = last
        return out

    # -- the page ---------------------------------------------------------------
    def view(self, now: datetime) -> dict:
        """What /api/assess returns: today's assessments (latest first), yesterday's summary, and
        the charts (drawn in Today's issues, as of the Today reader's last read)."""
        t = self.today.day(now)
        with self.lock:
            rec = self.days.get(t.isoformat(), {})
            y = self.days.get((t - timedelta(days=1)).isoformat(), {})
            periods = [{k: v for k, v in p.items() if k != "snap"} for p in rec.get("periods", [])][::-1]
        nxt = next((h for h in HOURS if self.today.local(now).hour < h), None)
        charts = self.charts(now) if self.today.ready else []
        return {"date": t.isoformat(), "zone": self.today.zone_label, "periods": periods,
                "yesterday": y.get("summary"), "hours": list(HOURS), "labels": list(LABELS),
                "next": f"{nxt:02d}:00" if nxt is not None else None, "note": self.note,
                # the charts are as of the Today reader's last read, not of the text's hour
                "now": self.today.local(self.today.as_of or now).strftime("%H:%M"), "charts": charts,
                "days": len(self._same_days(t))}


# -- what `verify --ai` measures with (made-up names and numbers) --------------------------------
SAMPLE = {"verdict": WORSE, "why": ["Sample Viewer crash (Major) is on 41 PCs, limited, above its usual range"],
          "since": "09:00", "nothing_new": False,
          "meaning": "Against the working days before: Major slow actions are within their usual range, PCs with a "
                     "crash so far are above their usual range and a typical Sample Logon is within its usual range.",
          "changed": ["Sample Viewer crash (Major) rose from 12 to 41 PCs since 09:00: limited, above its usual range "
                      "(3 to 9 PCs by this time).",
                      "Major slow actions added since 09:00, by application: Sample ERP (212), Sample Mail (96)."],
          "watch": ["Sample Agent hang (Major) is on 3 PCs, above its usual range (0 to 1 PCs by this time): isolated, "
                    "too few to change the day's verdict."],
          "earlier": ["The share of actions rated Major was above its usual range from 08:00 to 09:00; cleared since."],
          "startups": ["A typical Sample Logon takes 46 s today (812 so far), within its usual range (40 to 54 s on "
                       "the 5 working days before)."],
          "today": ["Active: 3,310 users on 3,905 PCs (2,650 virtual desktops, 1,255 physical PCs).",
                    "Major slow actions: 1,870 on 644 PCs, within their usual range (590 to 700 PCs by this time).",
                    "Sample Agent crash (Critical): 7 PCs, limited, within its usual range (5 to 9 PCs by this time).",
                    "Applications with the most Major slow actions: Sample ERP (540), Sample Mail (410)."],
          "first": "Sample Viewer crash on 41 PCs (Major, limited, above its usual range): on the Today tab, click its "
                   "tile for who has it"}
SAMPLE_WORDS = ("sample viewer", "41")  # a right text has what changed
