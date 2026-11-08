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

from .cause import ISSUE_ORDER, UNKNOWN_VERSION, change_text
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
            # APPLICATION_HANG_TIME_TOTAL's unit is not documented: shown as Aternity gives it
            return f"{m.group(1)} was not responding on {obj} (Aternity's hang time value up to {worst:.0f})"
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


def _item(g: dict, count: str) -> dict:
    """One issue: what happened, what is known, a possible cause, who else."""
    why = g.get("why") or {}
    text = list(why.get("text", []))
    if g.get("together"):
        text.append("Within minutes of it on this PC: " + ", ".join(g["together"]) + ".")
    o = why.get("others")
    return {"text": g["text"], "what": g["what"], "times": g["times"], "_others": o,
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


def build_answer(problems: list[dict], samples: list, slow: list, who: str, impact: dict | None,
                 netim_on: bool, mapped: list, user_ip: str | None, logons: dict | None,
                 minutes: int, split: dict | None, normal: list | None = None,
                 checked: dict | None = None, changed: dict | None = None,
                 calm: list[dict] | None = None) -> dict:
    n = len(slow)
    # calm: start-ups near a slow action with nothing longer than normal (not problems,
    # correlate.py leaves them out); the reason says they were near
    at_slow = [g for g in problems if g["slow_actions"]]
    found, todo = [], []
    for g in at_slow:
        why = g.get("why") or {}
        if why.get("same_as_above"):
            continue  # e.g. a hang's own health event: already explained above
        found.append(_item(g, "at the slow action" if n == 1 else f"at {g['slow_actions']} of {n} slow actions"))
        for step in why.get("todo") or [_todo(g)]:
            if step and step not in todo:
                todo.append(step)
    later: list[str] = []
    # crashes, hangs and health events at other times: issues by themselves. What the user
    # felt first, then what spans many PCs; a limit passed with nothing felt comes last
    elsewhere = [g for g in problems if not g["slow_actions"] and g.get("why")
                 and not g["why"].get("same_as_above")]
    pcs = lambda g: (g["why"].get("others") or {}).get("pcs", 0)  # noqa: E731
    elsewhere.sort(key=lambda g: (ISSUE_ORDER.get(g["issue"], 9), -pcs(g), -g["times"]))
    for g in elsewhere:
        count = f"{g['times']} of {g['readings']:,} readings" if g.get("readings") else f"{g['times']}x"
        found.append(_item(g, count + (", not near a slow action" if n else "")))
        later += [s for s in g["why"].get("todo") or [] if s not in later]  # after the slow actions' steps
    other = sum(1 for g in problems if not g["slow_actions"] and not g.get("why"))
    if other and n:
        found.append({"text": f"{plural(other, 'other problem')} happened only at other times, not near a slow action.",
                      "count": None, "when": None, "kind": None, "why": [], "possible": [], "who": None, "list": []})
    checks = _checked(checked, minutes) if n else []
    if n and not any(g["kind"] == "network" for g in at_slow):
        if not netim_on:
            checks.append("Network: not checked, NetIM is not configured.")
        elif not mapped:
            checks.append(f"Network: not checked, this PC's address ({user_ip or 'unknown'}) "
                          "is not in any NetIM subnet.")
        else:
            checks.append(f"Network: nothing wrong on the path at those times ({len(mapped)} NetIM objects).")
        # a virtual desktop: the leg from the device the user sat at is a network of its own
        for d in dict.fromkeys(s.device for s in slow if s.virtual):
            used = sorted({s.client_device for s in samples if s.device == d and s.client_device})
            checks.append(f"Network between {d} (a virtual desktop) and the device the user sat at"
                          + (f" ({', '.join(used[:3])})" if used else "") + ": not checked, Aternity "
                          "gives no delay for it.")
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

    # title: the problem seen at the most slow actions; "likely" when that is more than half
    top = at_slow[0] if at_slow else None
    lead = next(({**c, "name": f"{c['application']} {c['activity']}" if c["activity"] else c["application"]}
                 for c in normal or [] if c["since"] and c["changes"]), None)
    # other users of the same apps were slow as often as on previous days at this hour
    as_usual = bool(impact and impact["slow"] and impact["unusual"] is False)
    usual_line = (f" Other {', '.join(impact['applications'])} users had slow actions about as often as normal at this time "
                  "(see Other users)." if as_usual else "")
    near = "the slow action" if n == 1 else f"any of the {n} slow actions"
    nothing = f"No problem was recorded within {minutes} min of {near}."
    for g in calm or []:
        nothing += (f" {g['object']} was starting up near it: {g['what'].split(': ', 1)[-1]}. "
                    "No part of that start-up took longer than normal.")
    # the details stay in their own section; the reason says where to look
    felt_issues, background = _issue_counts([g for g in problems if not g["slow_actions"]])
    also = _background_line(background) if background else None
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
    elif top is None and lead:
        # nothing at the slow actions, but it became slow right after a change on the PC
        level = "possible"
        title = f"Possibly: what changed on {lead['device']} before {lead['name']} became slow"
        reason = (f"{nothing} Compared with normal shows it slow since {_day(lead['since'])} "
                  f"and what changed on {lead['device']} just before.{usual_line}")
    elif top is None and as_usual:
        level, title = "unsure", "Nothing unusual found"
        reason = nothing + usual_line
    elif top is None:
        level, title = "unsure", "Cause not found"
        reason = nothing
        if split:
            side = max(split, key=split.get)
            reason += f" Aternity measured most of the slow time {WHERE[side]} ({_pct(split[side])})."
    else:
        level = "likely" if top["slow_actions"] * 2 > n else "possible"
        title = f"{'Likely' if level == 'likely' else 'Possibly'}: {top['text']}"
        reason = (f"It happened within {minutes} min of the slow action." if n == 1 else
                  f"It happened within {minutes} min of {top['slow_actions']} of the {n} slow actions.")
        reason += usual_line
    if n and felt_issues + ([also] if also else []):
        reason += f" Also, not near the slow actions: {_join(felt_issues + ([also] if also else []))}."
    if not todo and n:
        todo.append(f"Nothing measured explains it: ask {who} what they were doing at those times "
                    "(see \"What was checked\" for what is ruled out).")

    todo += [s for s in later if s not in todo]
    worst = max((s for s in slow if s.response_time is not None), key=lambda s: s.response_time, default=None)
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
        "logon": (logons or {}).get("starts", [None])[0] if (logons or {}).get("starts") else None,
        "logon_unit": (logons or {}).get("unit"),
    }
    return {
        "level": level, "title": title, "reason": reason, "felt": felt, "time_went": split,
        "who_else": who_else, "rollup": _rollup(found), "found": found, "todo": todo, "changes": _changed(changed),
        "normal": [c["text"] for c in normal or []], "checked": checks,
    }


def _changed(changed: dict | None) -> list[dict]:
    """What changed on each PC, listed once: the issues only name a change that matches them."""
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    return [{"device": d, "changes": [{"time": iso(c["time"]), "text": change_text(c).split(" ", 2)[2]}
                                      for c in rows[:MAX_CHANGES_SHOWN]],
             "more": max(0, len(rows) - MAX_CHANGES_SHOWN)}
            for d, rows in (changed or {}).items()]


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
        out.append(f"PC at the {pc['measured']} slow actions Aternity measured it: highest " + ", ".join(vals) + ".")
    else:
        out.append("PC: Aternity gave no CPU, memory or disk queue for the slow actions.")
    for key, what in (("events", "Health events"), ("crashes_hangs", "App crashes and hangs"),
                      ("start_ups", "Start-ups")):
        total, near = c[key]
        out.append(f"{what} on {devs}: {total} in the window, {near} within {minutes} min of a slow action.")
    return out
