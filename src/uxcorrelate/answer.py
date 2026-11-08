"""The plain-language answer at the top of an investigation.

The question is what went wrong for this user: slow actions are one kind of
issue, next to hangs, crashes and health events. Each issue says what is known
about it and who else has it, in the same shape; timing is a fact, not a rank.
Everything here is written for a help-desk reader: plain counts ("at 3 of 4
slow actions"), no scores. Times are returned as ISO strings next to the
text, so the UI can show them in the reader's time zone and the CLI in UTC.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from .after_start import sentence as after_start_sentence
from .cause import ISSUE_ORDER, UNKNOWN_VERSION, batch_removals, change_text
from .models import plural

MAX_CHANGES_SHOWN = 40  # per PC
WHERE = {"pc": "on the PC", "network": "on the network", "server": "on the application's servers"}


def _day(iso: str) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return t.strftime("%b %d").replace(" 0", " ")


def _pct(v: float | None) -> str:
    return "?" if v is None else f"{v:.0%}"


def problem_text(g: dict) -> str:
    """One problem row as a sentence (without the count)."""
    obj, what, worst, limit = g["object"], g["what"], g["worst"], g["limit"]
    if g["source"] == "Aternity" and g["app"]:
        m = re.match(r"(.+) (crashed|not responding)$", what)
        if m and m.group(2) == "crashed":
            return f"{m.group(1)} crashed on {obj}"
        if m:
            # APPLICATION_HANG_TIME_TOTAL's unit is not documented: a bare number means nothing
            # to a reader, so it is left out (the problem's notes keep it for IT)
            return f"{m.group(1)} was not responding on {obj}"
    if g["source"] == "Aternity event":
        return f"{what} on {obj}"
    if g["source"] == "Aternity start-up":
        return f"{obj} {what}"
    if g["source"] == "NetIM alert":
        return f"NetIM alert on {obj}: {what}"
    if what.startswith("Network round trip"):
        return f"{obj}'s network round trip reached {worst} ms (limit {limit})"
    if what.startswith("Free disk space"):
        return f"{obj}'s disk was nearly full ({worst}% free, limit {limit}%)"
    if what == "Disk queue":
        return f"{obj}'s disk could not keep up (queue {worst}, limit {limit})"
    if g["source"] == "NetIM":
        return f"{obj}: {what} reached {worst} (limit {limit})"
    return f"{obj}: {what.replace(' %', '')} reached {worst}{'%' if '%' in what else ''} (limit {limit})"


def _todo(g: dict) -> str:
    obj = g["object"]
    if g["source"] == "Aternity" and g["app"]:
        return f"On {obj}: check {g['app']} (add-ins, updates, file or mailbox size) and its history in Aternity."
    if g["source"] == "Aternity event":
        return f"On {obj}: open the device's health events in Aternity."
    if g["source"] == "Aternity start-up":
        return None  # its own steps come with its why; nothing more to open
    if g["source"] == "NetIM alert":
        return f"Check the NetIM alert on {obj}."
    if g["source"] == "NetIM":
        return f"Check what is loading {obj} at those times (top talkers, errors)."
    if g["what"].startswith("Network round trip"):
        return f"Check {obj}'s connection: VPN or home network, and its route to the servers."
    if g["what"].startswith("Free disk space"):
        return f"On {obj}: free up disk space on the system drive."
    if g["what"] == "Disk queue":
        return f"On {obj}: find which process was using the disk at those times (antivirus scan, updates, sync)."
    return f"On {obj}: find which process used the {g['what'].replace(' %', '').lower()} at those times."


def others_text(o: dict) -> str:
    """Who else has this issue, in one shape: how many, out of how many, on what, since when."""
    if not o["pcs"]:
        out = "Only this PC in this window" + (f", of {o['of']:,} {o['of_what']}" if o.get("of") else "") + "."
    else:
        out = f"{plural(o['pcs'], 'other PC')}" + (f" ({plural(o['users'], 'user')})" if o.get("users") else "")
        out += f" of {o['of']:,} {o['of_what']}" if o.get("of") else ""
        versions = {v: k for v, k in (o.get("versions") or {}).items() if v != UNKNOWN_VERSION}
        if len(versions) == 1:
            out += f", all on {next(iter(versions))}"
        elif versions:
            out += ", " + ", ".join(f"{k} on {v}" for v, k in list(versions.items())[:3])
        out += "."
        if versions and o.get("this_version") not in (None, UNKNOWN_VERSION):
            out += f" This PC has {o['this_version']}."
    h = o.get("history")
    if h:
        out += (f" Already on other PCs {h['days']} days before this window." if h["from_start"]
                else f" On other PCs since {_day(h['since'])}." if h["since"]
                else f" Not on other PCs in the {h['days']} days before this window.")
    return out


def verdict(o: dict | None) -> str | None:
    """How far an issue reaches, by fixed rules on the counts. Never says it is or is not
    the cause for this user."""
    if not o:
        return None
    h = o.get("history") or {}
    if not o["pcs"]:
        return "Only this PC"
    if h and h["since"] is None:
        return "New"
    if o["pcs"] == 1:
        return "On 1 other PC"
    return "Widespread" + (f", {h['trend']}" if h.get("trend") else "")


def _days(o: dict | None) -> list[dict]:
    """Other PCs per day: the days before the window, then the window."""
    if not o or not (o.get("history") or {}).get("per_day"):
        return []
    return ([{"day": d, "pcs": n, "window": False} for d, n in o["history"]["per_day"].items()]
            + [{"day": d, "pcs": n, "window": True} for d, n in (o.get("window_days") or {}).items()])


def _rollup(found: list[dict]) -> list[dict]:
    """One row per issue, across this user's PCs: the top of "What went wrong"."""
    rows: dict[str, dict] = {}
    for i, x in enumerate(found):
        if not x.get("what"):
            continue
        r = rows.setdefault(x["what"], {"issue": x["what"], "times": 0, "others": None, "others_pcs": -1,
                                        "verdict": None, "days": [], "first": i})
        r["times"] += x["times"]
        o = x.pop("_others")
        if o and o["pcs"] > r["others_pcs"]:
            r.update(others_pcs=o["pcs"], verdict=verdict(o), days=_days(o),
                     others=f"{o['pcs']:,}" + (f" of {o['of']:,}" if o.get("of") else ""))
    for r in rows.values():
        del r["others_pcs"]
    return list(rows.values())


def _pc_limit(g: dict) -> bool:
    """A PC value (CPU, memory, disk) over its limit, read with the user's actions."""
    return g["source"] == "Aternity" and g["kind"] == "pc" and g.get("limit") is not None


def _limit_step(g: dict, n: int) -> str:
    """What to do about a PC value over its limit at slow actions, when Aternity cannot
    name the program for that minute (its per-program figures are hourly)."""
    label = g["what"].replace(" %", "")
    near = "the slow action" if n == 1 else f"{g['slow_actions']} of {n} slow actions"
    return (f"On {g['object']}: {label} was over its limit during {near}, and Aternity cannot name the program for "
            "that minute: when it is slow again, see which program it is (Task Manager).")


# what a limit problem is called in the rollup ("CPU %" is a column name, not an issue)
LIMIT_ISSUE = {"CPU %": "CPU over its limit", "Memory %": "Memory over its limit",
               "Disk queue": "Disk queue over its limit", "Free disk space %": "Disk nearly full",
               "Network round trip ms": "Network round trip over its limit"}


def _item(g: dict, count: str) -> dict:
    """One issue: what happened, what is known, a possible cause, who else."""
    why = g.get("why") or {}
    text = list(why.get("text", []))
    if g.get("together"):
        text.append("Within minutes of it on this PC: " + ", ".join(g["together"]) + ".")
    o = why.get("others")
    what = LIMIT_ISSUE.get(g["what"], g["what"]) + (f" ({g['limit']:g}{'%' if '%' in g['what'] else ''})"
                                                     if g["what"] in LIMIT_ISSUE and g.get("limit") is not None
                                                     else "")
    return {"text": g["text"], "what": what, "times": g["times"], "_others": o,
            "count": count, "when": g["first"], "kind": g["kind"], "issue": g["issue"],
            "why": text, "possible": why.get("possible", []),
            "who": others_text(o) if o else None, "list": o["list"] if o else [],
            "list_of": o["what"] if o else None, "this_version": o["this_version"] if o else None}


def _slow_actions(slow: list) -> str:
    """ "This user had 4 slow actions: Open Mail in Outlook (3) and Chat in Teams (1)." """
    what = Counter(f"{s.activity} in {s.application}" if s.activity else s.application for s in slow).most_common()
    named = [f"{name} ({n})" for name, n in what[:3]]
    if len(what) > 3:
        named.append(f"{plural(len(what) - 3, 'other')}")
    return f"This user had {plural(len(slow), 'slow action')}: {_join(named)}."


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _issue_counts(problems: list[dict]) -> tuple[list[str], list[dict]]:
    """What the user felt, counted ("7 hangs"), and the background crashes."""
    rows = [g for g in problems if not (g.get("why") or {}).get("same_as_above")]
    total = lambda kind: sum(g["times"] for g in rows if g["issue"] == kind)  # noqa: E731
    felt = []
    if total("hang"):
        felt.append(plural(total("hang"), "hang"))
    if total("crash"):
        felt.append(plural(total("crash"), "app crash", "app crashes"))
    for what in dict.fromkeys(g["what"] for g in rows if g["issue"] == "system"):
        felt.append(plural(sum(g["times"] for g in rows if g["issue"] == "system" and g["what"] == what),
                           f"{what} event"))
    return felt, [g for g in rows if g["issue"] == "background"]


def _background_line(rows: list[dict]) -> str:
    """ "4 background crashes: the same crashes hit 863 and 638 other PCs." """
    total = sum(g["times"] for g in rows)
    wide = sorted({(g.get("why") or {}).get("others", {}).get("pcs", 0)
                   for g in rows if (g.get("why") or {}).get("others")}, reverse=True)
    wide = [f"{n:,}" for n in wide if n >= 2]
    return plural(total, "background crash", "background crashes") + (
        f": the same crashes hit {_join(wide)} other PCs" if wide else "")


# the headline's tie order for problems at the same number of slow actions: what the user
# felt, then health events, a start-up, the network, a PC reading, a nearly full disk
HEAD_ORDER = {"hang": 0, "crash": 0, "system": 1, "background": 1, "startup": 2, "infra": 3, "limit": 4}


def _head_rank(g: dict) -> tuple:
    disk = g["what"].startswith("Free disk")
    return (-g["slow_actions"], HEAD_ORDER.get(g["issue"], 9) + disk, -g["times"])


def _name(app: str, act: str | None) -> str:
    return f"{app} {act}" if act else app


def _start_name(c: dict) -> str:
    return _name(c["application"], c["activity"])


def _status_of(c: dict, slow: list) -> str:
    """ " (Minor)": Aternity's status of the slow action a start-up comparison is about."""
    s = next((s for s in slow if (s.application, s.activity, s.device, s.response_time)
              == (c["application"], c["activity"], c["device"], c["seconds"])), None)
    return f" ({s.status})" if s is not None and s.status else ""


def _after_when(c: dict) -> str:
    return "right after" if c["minutes"] <= 1 else f"{c['minutes']} min after"


def _felt_all(slow: list, worst) -> str:
    """ "Microsoft Outlook Launch took 40.6 s on VD-41 (Minor)", or "4 slow actions, slowest …" """
    if worst is None:
        return plural(len(slow), "slow action")
    name = _name(worst.application, worst.activity)
    at = f"{worst.response_time:.1f} s" + (f" on {worst.device}" if worst.device else "") + (
        f" ({worst.status})" if worst.status else "")
    return f"{name} took {at}" if len(slow) == 1 else f"{len(slow)} slow actions, slowest {name} {at}"


def _when(ch: list[dict]) -> str:
    first, last = min(x["time"] for x in ch), max(x["time"] for x in ch)
    return f"on {_day(first)}" if _day(first) == _day(last) else f"between {_day(first)} and {_day(last)}"


def _several(c: dict) -> list[dict]:
    """The PCs an action was slow on, when more than one (normal.py's "pcs")."""
    pcs = c.get("pcs") or []
    return pcs if len(pcs) > 1 else []


def _changed_anywhere(c: dict) -> bool:
    return bool(c["changes"]) or any(p["changes"] for p in _several(c))


def _changes_phrase(c: dict) -> str:
    """The changes before an action became slow, as the headline says them. Slow on several
    PCs: the changes on each; one with no change means they do not explain it alone."""
    pcs = _several(c)
    if pcs:
        told = [f"{plural(p['changed'] or len(p['changes']), 'change')} on {p['device']} {_when(p['changes'])}"
                for p in pcs if p["changes"]]
        quiet = [p["device"] for p in pcs if not p["changes"]]
        return _join(told) + (f", none on {_join(quiet)}: they do not explain it alone" if quiet
                              else ": possibly one of them")
    ch, k = c["changes"], c.get("changed") or len(c["changes"])
    if k == 1:
        x = ch[0]
        return f"Possibly: {x['name']} {x['change'] or 'changed'} on {_day(x['time'])}"
    return f"{plural(k, 'change')} logged there {_when(ch)}: possibly one of them"


def _counted(c: dict) -> str:
    """ "slow 2 of 9 times", or on several PCs "VD-41 1 of 11 times (5.2 s), VD-14 1 of 3 times (3.6 s)" """
    pcs = _several(c)
    if pcs:
        return ", ".join(f"{p['device']} {p['slow']} of {p['runs']} times ({p['took']} s)" for p in pcs)
    k, runs = c.get("from_slow"), c.get("from_runs")
    return f"slow {k} of {runs} times" if runs else "slow"


def _since_phrase(c: dict) -> str:
    """ "Microsoft Outlook Preview Mail on VD-41: slow 2 of 9 times from Nov 6 (4.4 s, usually 0.2 s);
    normal on each of the 4 days before." What was counted, never "slow since" (runs in between
    may have been fast). Slow on several PCs: each with its own count and time."""
    before = (f"normal on each of the {c['days_used']} days before" if not c["slow_days"] else
              f"normal on {_day(c['normal_day'])}" if c.get("normal_day") else "normal before")
    pcs = _several(c)
    if pcs:
        kind = "desktops" if c.get("virtual") else "PCs"
        return (f"{c['name']} slow on {len(pcs)} {kind} from {_day(c['since'])}: {_counted(c)}; usually "
                f"{c['usual']} s, {before}.")
    return (f"{c['name']} on {c['device']}: {_counted(c)} from {_day(c['since'])} ({c['took']} s, usually "
            f"{c['usual']} s); {before}.")


def _usual_from(c: dict) -> str:
    """How much the usual time rests on, so a reader can judge it."""
    return f"The usual time is from {plural(c['runs'], 'run')} in the {c['back']}."


def _group_line(name: str, took, dev: str, fact: str) -> str:
    return f"{name} {took} s on {dev}" + (f", {fact}" if fact else "")


def _pick_headline(at_slow: list[dict], slow: list, normal: list[dict], after_start: list[dict],
                   impact: dict | None) -> dict:
    """Which class of verdict the headline gives (docs/headline-rules.md), its subject, the
    start-up comparisons told at the top, and one line for each other slow app + activity."""
    named = [{**c, "name": _name(c["application"], c["activity"])} for c in normal if c.get("took") is not None]
    by = lambda xs: max(xs, key=lambda c: (c["slow_actions"], c.get("worst") or 0)) if xs else None  # noqa: E731
    cands = {
        "C": by([c for c in named if c["since"] and _changed_anywhere(c)]),
        "D": by([c for c in named if c["since"] and not _changed_anywhere(c)]),
        "F": by([c for c in named if c.get("enough") and c["days_used"] and c["slow_days"] == c["days_used"]]),
    }
    starts = [c for c in after_start if c["slower"]]
    start = max(starts, key=lambda c: (c["count"], c["seconds"] or 0)) if starts else None
    top = at_slow[0] if at_slow else None
    if not slow:
        cls, subject = None, None
    elif top is not None and not (_pc_limit(top) and start):
        # a PC reading (one value) gives way to a start-up comparison over many PCs
        cls, subject = "A", None
    elif impact and impact.get("unusual"):
        cls, subject = "B", None
    elif cands["C"] or cands["D"]:
        cls = "C" if cands["C"] else "D"
        subject = cands[cls]
    elif start:
        cls, subject = "E", start
    elif cands["F"]:
        cls, subject = "F", cands["F"]
    elif impact and impact["slow"] and impact["unusual"] is False:
        cls, subject = "G", None
    else:
        cls, subject = "H", None
    told_start = [subject] if cls == "E" else []
    also = []
    if cls in ("A", "C", "D", "E", "F"):
        # a problem row without its slow actions listed covers them all
        covered = set(top.get("slow_idx", range(len(slow)))) if cls == "A" else set()
        key = (subject["application"], subject["activity"]) if subject else None
        for (app, act) in dict.fromkeys((s.application, s.activity) for s in slow):
            mine = [i for i, s in enumerate(slow) if (s.application, s.activity) == (app, act)]
            if (app, act) == key or (cls == "A" and set(mine) <= covered):
                continue
            nm = next((c for c in named if (c["application"], c["activity"]) == (app, act)), None)
            st = next((c for c in starts if (c["application"], c["activity"]) == (app, act)), None)
            times = sorted(slow[i].response_time for i in mine if slow[i].response_time is not None)
            dev = slow[mine[0]].device
            if st is not None:
                told_start.append(st)
                also.append(_group_line(_name(app, act), f"{st['seconds']:.1f}", st["device"],
                                        f"{_after_when(st)} a start-up (on {st['kind']} {st['after']['share']:.0%} "
                                        f"are slow then, {st['later']['share']:.0%} later)"))
            elif nm is not None:
                counted = f"{_counted(nm)} from {_day(nm['since'])}" if nm["since"] else ""
                fact = (f"usually {nm['usual']} s, {counted}, " + (
                        _changes_phrase(nm) if _several(nm) else
                        f"after {plural(nm.get('changed') or len(nm['changes']), 'change')} there")
                        if nm["since"] and _changed_anywhere(nm)
                        else f"usually {nm['usual']} s, {counted}, no change to check logged"
                        if nm["since"] else
                        "slow on most runs every day it was used, not new"
                        if nm.get("enough") and nm["days_used"] and nm["slow_days"] == nm["days_used"] else
                        f"others typical {nm['others_typical']} s" if nm["others_typical"] is not None else "")
                also.append(_group_line(nm["name"], nm["took"], nm["device"], fact))
            elif times:
                also.append(_group_line(_name(app, act), f"{times[-1]:.1f}", dev, ""))
    return {"class": cls, "subject": subject, "start": subject if cls == "E" else None,
            "told_start": told_start, "also": also}


def build_answer(problems: list[dict], samples: list, slow: list, who: str, impact: dict | None,
                 netim_on: bool, mapped: list, user_ip: str | None, logons: dict | None,
                 minutes: int, split: dict | None, normal: list | None = None,
                 checked: dict | None = None, changed: dict | None = None,
                 calm: list[dict] | None = None, after_start: list[dict] | None = None,
                 ip_device: str | None = None, addresses: list[tuple] | None = None,
                 unread: list[str] | None = None) -> dict:
    n = len(slow)
    # the PCs the user worked on, by name: "Other things on VD-41", "on VD-41 and VD-14"
    devs = sorted({s.device for s in samples if s.device}, key=lambda d: (-sum(1 for s in slow if s.device == d), d))
    pcs_label = (devs[0] if len(devs) == 1 else _join(devs) if len(devs) == 2
                 else f"the {len(devs)} PCs" if devs else "this PC")
    worst = max((s for s in slow if s.response_time is not None), key=lambda s: s.response_time, default=None)
    # calm: start-ups near a slow action with nothing longer than normal (not problems,
    # correlate.py leaves them out); the reason says they were near
    # problems at slow actions: most slow actions first, then by kind (HEAD_ORDER)
    at_slow = sorted((g for g in problems if g["slow_actions"]), key=_head_rank)
    head = _pick_headline(at_slow, slow, normal or [], after_start or [], impact)
    found, todo = [], []
    for g in at_slow:
        why = g.get("why") or {}
        if why.get("same_as_above"):
            continue  # e.g. a hang's own health event: already explained above
        at = "at the slow action" if n == 1 else f"at {g['slow_actions']} of {n} slow actions"
        # a limit counts readings ("This user" in the rollup): say both; a reading counts only
        # for the action it was read with (correlate._matches)
        found.append(_item(g, f"{g['times']} of {g['readings']:,} readings, {at.replace('at ', 'with ', 1)}"
                           if g.get("readings") else at))
        # a PC value over its limit with its why told: only the steps the data supports
        # (Aternity cannot show which program it was at that minute)
        for step in why.get("todo") or ([_limit_step(g, n)] if _pc_limit(g) and why.get("text") else [_todo(g)]):
            if step and step not in todo:
                todo.append(step)
    later: list[str] = []
    # crashes, hangs and health events at other times: issues by themselves. What the user
    # felt first, then what spans many PCs; a limit passed with nothing felt comes last.
    # With slow actions they are not the answer: "Other things on these PCs", below it
    elsewhere_items: list[dict] = []
    put = elsewhere_items if n else found
    elsewhere = [g for g in problems if not g["slow_actions"] and g.get("why")
                 and not g["why"].get("same_as_above")]
    pcs = lambda g: (g["why"].get("others") or {}).get("pcs", 0)  # noqa: E731
    elsewhere.sort(key=lambda g: (ISSUE_ORDER.get(g["issue"], 9), -pcs(g), -g["times"]))
    for g in elsewhere:
        if g.get("readings"):
            count = f"{g['times']} of {g['readings']:,} readings" + (", none with a slow action" if n else "")
        else:
            # near a slow action of another app: not counted for it (correlate._other_app_near says so)
            count = f"{g['times']}x" + ((", not counted for a slow action" if g.get("near_other_app")
                                         else ", not near a slow action") if n else "")
        item = _item(g, count)
        put.append(item)
        steps = [s for s in g["why"].get("todo") or [] if s not in later]
        if n and g["issue"] in ("background", "limit"):
            # not something the user felt: its step stays with it, below the answer
            item["why"] += [f"Next step: {s}" for s in steps]
        else:
            later += steps  # after the slow actions' steps
    other = sum(1 for g in problems if not g["slow_actions"] and not g.get("why"))
    if other and n:
        put.append({"text": f"{plural(other, 'other problem')} happened only at other times, not near a slow action.",
                      "count": None, "when": None, "kind": None, "why": [], "possible": [], "who": None, "list": []})
    checks = _checked(checked, minutes) if n else []
    if n and not any(g["kind"] == "network" for g in at_slow):
        if not netim_on:
            checks.append("Network: not checked, NetIM is not configured.")
        elif not mapped:
            # every PC the user worked on, each with its own address
            pcs = addresses or [(ip_device or "the PC", user_ip)]
            named = _join([f"{d} ({ip or 'address unknown'})" for d, ip in pcs])
            checks.append(f"Network: not checked, the address{'es' if len(pcs) > 1 else ''} of {named} "
                          f"{'are' if len(pcs) > 1 else 'is'} not in any NetIM subnet.")
        else:
            checks.append(f"Network: nothing wrong on the path at those times ({len(mapped)} NetIM objects).")
        # a virtual desktop: the leg from the device the user sat at is a network of its own
        for d in dict.fromkeys(s.device for s in slow if s.virtual):
            used = sorted({s.client_device for s in samples if s.device == d and s.client_device})
            checks.append(f"Network between {d} (a virtual desktop) and the device the user sat at"
                          + (f" ({', '.join(used[:3])})" if used else "") + ": not checked, Aternity "
                          "gives no delay for it.")
    # every other slow activity right after a start-up, against all PCs (once: not those told at the top)
    for c in after_start or []:
        if not any(c is t for t in head["told_start"]):
            checks.append(after_start_sentence(c) + (
                " Slower right after a start-up." if c["slower"] else
                "" if not c["enough"] else " Not slower right after a start-up: not the reason."))
    for c in normal or []:
        for step in c["todo"]:
            if step not in todo:
                todo.append(step)

    # other users of the same apps at the slow times (an issue's own "who else" is on the issue)
    who_else = None
    if impact:
        names = impact["applications"]
        apps = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
        users, k = impact["users"], impact["slow"]
        lead = _slow_actions(slow)
        too = (f"{lead} At the same time, {k} of the {users} other people using {apps} also had a slow action "
               f"({_pct(impact['rate'])}).")
        if not users:
            who_else = f"{lead} Nobody else was using {apps} at the same time."
        elif not k:
            who_else = f"{lead} None of the {users} other people using {apps} had a slow action at the same time."
        elif impact["unusual"]:
            who_else = f"{too} That is more than normal: at this time on other days, at most {_pct(impact['usual_rate'])} had one."
        elif impact["unusual"] is False:
            who_else = f"{too} That is normal: at this time on other days, up to {_pct(impact['usual_rate'])} had one."
        else:
            who_else = f"{too} There are no other days to compare with."
        cp = impact["common_point"]
        if cp:
            who_else += f" {cp['slow']} of the {k} slow users are behind the same network point: {cp['name']}."
            todo.insert(0, f"Many users are slow behind {cp['name']}: check it first.")
        elif impact["unusual"]:
            todo.insert(0, f"Many {apps} users are slow at once: check the {apps} service and its servers.")

    # the headline: what the user felt, then one verdict (docs/headline-rules.md)
    top = at_slow[0] if at_slow else None
    # other users of the same apps were slow as often as on previous days at this hour
    as_usual = bool(impact and impact["slow"] and impact["unusual"] is False)
    usual_line = (f" Other {', '.join(impact['applications'])} users had slow actions about as often as normal at this time "
                  "(see Other users)." if as_usual else "")
    near = "the slow action" if n == 1 else f"any of the {n} slow actions"
    nothing = f"No problem was recorded within {minutes} min of {near}."
    if any(g.get("near_reading") for g in problems):
        nothing = (f"No problem was recorded within {minutes} min of {near}, other than a PC reading over "
                   f"its limit taken with another action (see Other things on {pcs_label}).")
    for g in calm or []:
        nothing += (f" {g['object']} was starting up near {'it' if n == 1 else 'a slow action'}: "
                    f"{g['what'].split(': ', 1)[-1]}. "
                    "No part of that start-up took longer than normal.")
    # the details stay in their own section; the reason says where to look
    felt_issues, background = _issue_counts([g for g in problems if not g["slow_actions"]])
    also = _background_line(background) if background else None
    cls, c = head["class"], head["subject"]
    if not n:
        marked = f"Aternity marked none of the {len(samples)} measured actions slow."
        if felt_issues:
            level, title = "issues", f"{_join(felt_issues)}, no slow actions"
            reason = f"Also {also}." if also else marked
        elif background:
            level, title = "none", "No slow actions, hangs or app crashes"
            reason = f"Only in the background: {also}."
        elif samples:
            level, title, reason = "none", "No problems found", f"{marked} No hang or app crash either."
        else:
            level, title, reason = "none", "No activity found", "Aternity has no activity for this window."
    elif cls == "A":
        level = "likely" if top["slow_actions"] * 2 > n else "possible"
        more = len(found) - 1
        title = (f"{_felt_all(slow, worst)}. {'Likely' if level == 'likely' else 'Possibly'}: {top['text']}"
                 + ("" if n == 1 else " at all of them" if top["slow_actions"] == n
                    else f" at {top['slow_actions']} of them")
                 + (f"; {plural(more, 'more issue')} (see What went wrong)" if more > 0 else ""))
        reason = (f"It happened within {minutes} min of the slow action." if n == 1 else
                  f"It happened within {minutes} min of {top['slow_actions']} of the {n} slow actions.")
        reason += usual_line
    elif cls == "B":
        level = "possible"
        cp = impact["common_point"]
        apps = _join(impact["applications"])
        title = f"{_felt_all(slow, worst)}. " + (
            f"Many users behind {cp['name']} were slow at the same time" if cp else
            f"{impact['slow']} of the {impact['users']} other {apps} users were slow at the same time, "
            "more than on other days")
        reason = f"{nothing} See Other users."
    elif cls in ("C", "D"):
        # slow on a PC with no change too: the changes do not explain it alone
        level = "possible" if cls == "C" and not any(not p["changes"] for p in _several(c)) else "unsure"
        title = _since_phrase(c) + " "
        title += _changes_phrase(c) if cls == "C" else (
            ("No change to check was logged on any of them before it" if _several(c)
             else "No change to check was logged there before it")
            + (" (on a virtual desktop the log can run late)" if c["virtual"] else ""))
        reason = f"{nothing} {_usual_from(c)}"
        if cls == "C":
            on = _join([p["device"] for p in _several(c)]) if _several(c) else c["device"]
            reason += f" What changed on {on} is listed under What changed."
        reason += usual_line
    elif cls == "E":
        level = "possible"
        title = (f"{_start_name(c)} took {c['seconds']:.1f} s on {c['device']}" + _status_of(c, slow)
                 + f", {_after_when(c)} a start-up. On {c['kind']} {c['after']['share']:.0%} are slow then, "
                 f"{c['later']['share']:.0%} later")
        with_it = nothing if top is None else f"Also with it: {top['text']} (see What went wrong)."
        reason = f"{after_start_sentence(c)} {with_it}{usual_line}"
    elif cls == "F":
        level = "unsure"
        title = (f"{c['name']} took {c['took']} s on {c['device']}" + (f" ({c['status']})" if c.get("status") else "")
                 + (f", usually {c['usual']} s" if c.get("usual") is not None else "")
                 + ". Aternity marked most runs slow on each of the "
                 f"{c['days_used']} days {who} used it: not new")
        reason = f"{nothing} {_usual_from(c)}{usual_line}"
    elif cls == "G":
        level = "unsure"
        title = (f"{_felt_all(slow, worst)}. Other {_join(impact['applications'])} users were as slow as usual "
                 "at this time")
        reason = nothing + usual_line
    else:
        level = "unsure"
        title = f"{_felt_all(slow, worst)}. No cause found" + (
            f" in what could be read ({plural(len(unread), 'source')} not read: see Warnings)" if unread else "")
        reason = nothing
        if split:
            side = max(split, key=split.get)
            reason += f" Aternity measured most of the slow time {WHERE[side]} ({_pct(split[side])})."
    # each other slow app and activity, with what is known of it
    also_slow = [x for x in head["also"] if x]
    if n and also_slow:
        reason += " Also slow: " + "; ".join(also_slow) + "."
    for t in head["told_start"]:
        name, kind = _start_name(t), t["kind"]
        step = (f"{name} on {kind} is slow {t['after']['share']:.0%} of the time right after a start-up, "
                f"{t['later']['share']:.0%} later, for many users, not only {who}. "
                f"Aternity does not say why: a question for whoever builds the {kind}.")
        if step not in todo:
            todo.insert(0, step)
    if n and felt_issues + ([also] if also else []):
        reason += (f" Also, not near {'the slow action' if n == 1 else 'the slow actions'}: "
                   f"{_join(felt_issues + ([also] if also else []))} (see Other things on {pcs_label}).")
    if not todo and n:
        todo.append(f"Nothing measured explains it: ask {who} what they were doing at those times "
                    "(see \"What was checked\" for what is ruled out).")

    todo += [s for s in later if s not in todo]
    per_app: dict[str, int] = {}
    for s in slow:
        per_app[s.application] = per_app.get(s.application, 0) + 1
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    felt = {
        "actions": len(samples), "slow_actions": n,
        "apps": sorted(per_app.items(), key=lambda kv: -kv[1])[:5],
        "first": iso(min(s.timestamp for s in slow)) if slow else None,
        "last": iso(max(s.timestamp for s in slow)) if slow else None,
        "slowest": None if worst is None else {
            "app": worst.application, "activity": worst.activity, "seconds": round(worst.response_time, 1),
            "when": iso(worst.timestamp), "device": worst.device, "status": worst.status},
        "by_status": dict(Counter(s.status for s in slow if s.status).most_common()),  # Aternity's own, e.g. Minor
        # each PC the user worked on: actions and slow actions (a user may have several)
        # a virtual desktop also names the device(s) the user sat at
        "pcs": [{"device": d, "actions": sum(1 for s in samples if s.device == d),
                 "slow": sum(1 for s in slow if s.device == d),
                 "virtual": any(s.virtual for s in samples if s.device == d),
                 "from": sorted({s.client_device for s in samples if s.device == d and s.virtual and s.client_device})}
                for d in sorted({s.device for s in samples}, key=lambda d: -sum(1 for s in slow if s.device == d))],
        # each PC's slowest start-up in the window (the user may have worked on several)
        "logons": _per_pc((logons or {}).get("starts") or []),
        "logon_unit": (logons or {}).get("unit"),
    }
    return {
        "level": level, "title": title, "reason": reason, "felt": felt, "time_went": split,
        "who_else": who_else, "rollup": _rollup(found), "found": found,
        "elsewhere": [{k: v for k, v in x.items() if k != "_others"} for x in elsewhere_items],
        "todo": todo, "changes": _changed(changed), "pcs_label": pcs_label,
        "normal": [c["text"] for c in normal or []], "checked": checks,
    }


def _per_pc(starts: list[dict]) -> list[dict]:
    """Each PC's first start-up in the list (the list is slowest first)."""
    seen: dict[str, dict] = {}
    for st in starts:
        seen.setdefault(st["device"], st)
    return list(seen.values())


def _changed(changed: dict | None) -> list[dict]:
    """What changed on each PC, listed once: the issues only name a change that matches them.
    Many removals with no install logged in one minute (a virtual desktop's image renewed,
    as the log shows it) are one line, not a page of "Uninstalled"."""
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    out = []
    for d, rows in (changed or {}).items():
        batch = batch_removals(rows)
        minute = lambda c: c["time"].replace(second=0, microsecond=0)  # noqa: E731
        items, done = [], set()
        for c in rows:
            m = minute(c)
            if id(c) in batch:
                if m not in done:
                    done.add(m)
                    names = [x["name"] for x in rows if id(x) in batch and minute(x) == m]
                    items.append({"time": iso(c["time"]), "text": (
                        f"{len(names)} programs logged as uninstalled in the same minute, with no install logged: "
                        f"{', '.join(names[:4])}"
                        + (f" and {len(names) - 4} more" if len(names) > 4 else ""))})
                continue
            items.append({"time": iso(c["time"]), "text": change_text(c).split(" ", 2)[2]})
        out.append({"device": d, "changes": items[:MAX_CHANGES_SHOWN], "more": max(0, len(items) - MAX_CHANGES_SHOWN)})
    return out


def _checked(c: dict | None, minutes: int) -> list[str]:
    """What was looked at besides the problems found, with plain counts."""
    if not c:
        return []
    devs, out = ", ".join(c["devices"]) or "the PC", []
    pc, lim = c["pc"], c["pc"]["limits"]
    if pc["measured"]:
        vals = [f"{label} {pc[k]}{unit} (limit {lim[k]}{unit})" for k, label, unit in
                (("cpu", "CPU", " %"), ("memory", "memory", " %"), ("disk_queue", "disk queue", ""))
                if pc[k] is not None]
        at = "the slow action" if pc["measured"] == 1 else f"the {pc['measured']} slow actions"
        out.append(f"PC during {at}, as Aternity read it with {'it' if pc['measured'] == 1 else 'them'}: highest "
                   + ", ".join(vals) + ".")
    else:
        out.append("PC: Aternity gave no CPU, memory or disk queue for the slow actions.")
    for key, what in (("events", "Health events"), ("crashes_hangs", "App crashes and hangs"),
                      ("start_ups", "Start-ups")):
        total, near = c[key]
        out.append(f"{what} on {devs}: {total} in the window, {near} within {minutes} min of a slow action.")
    return out
