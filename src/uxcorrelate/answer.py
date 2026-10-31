"""The plain-language answer at the top of an investigation.

Everything here is written for a help-desk reader: plain counts ("at 3 of 4
slow actions"), no scores. Times are returned as ISO strings next to the
text, so the UI can show them in the reader's time zone and the CLI in UTC.
"""

from __future__ import annotations

import re
from datetime import datetime

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
            return f"{m.group(1)} was not responding on {obj} (Aternity hang time up to {worst})"
    if g["source"] == "Aternity event":
        return f"Health event on {obj}: {what}"
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


def build_answer(problems: list[dict], samples: list, slow: list, who: str, impact: dict | None,
                 netim_on: bool, mapped: list, user_ip: str | None, logons: dict | None,
                 minutes: int, split: dict | None, normal: list | None = None,
                 checked: dict | None = None) -> dict:
    n = len(slow)
    at_slow = [g for g in problems if g["slow_actions"]]
    found, todo = [], []
    for g in at_slow:
        why = g.get("why") or {}
        if why.get("same_as_above"):
            continue  # e.g. a hang's own health event: already explained above
        found.append({"text": g["text"], "count": f"at {g['slow_actions']} of {n} slow actions",
                      "when": g["first"], "kind": g["kind"], "why": why.get("text", [])})
        for step in why.get("todo") or [_todo(g)]:
            if step and step not in todo:
                todo.append(step)
    later: list[str] = []
    # crashes, hangs and health events at other times: an issue by themselves, with their why
    elsewhere = [g for g in problems if not g["slow_actions"] and g.get("why")
                 and not g["why"].get("same_as_above")]
    for g in elsewhere:
        why = g["why"]
        text = list(why.get("text", []))
        o = why.get("others")
        if o:
            text.append(f"{o['pcs']} other PC(s) had the same failure in this window." if o["pcs"]
                        else "Only this PC had this failure in this window.")
        found.append({"text": g["text"], "count": f"{g['times']}x, not near a slow action", "when": g["first"],
                      "kind": g["kind"], "why": text})
        later += [s for s in why.get("todo") or [] if s not in later]  # after the slow actions' steps
    other = sum(1 for g in problems if not g["slow_actions"] and not g.get("why"))
    if other and n:
        found.append({"text": f"{other} other problem(s) happened only at other times, not near a slow action.",
                      "count": None, "when": None, "kind": None, "why": []})
    checks = _checked(checked, minutes) if n else []
    if n and not any(g["kind"] == "network" for g in at_slow):
        if not netim_on:
            checks.append("Network: not checked, NetIM is not configured.")
        elif not mapped:
            checks.append(f"Network: not checked, this PC's address ({user_ip or 'unknown'}) "
                          "is not in any NetIM subnet.")
        else:
            checks.append(f"Network: nothing wrong on the path at those times ({len(mapped)} NetIM objects).")
    for c in normal or []:
        for step in c["todo"]:
            if step not in todo:
                todo.append(step)

    # who else is affected? For an application failure: the other PCs with the same failure.
    # Otherwise: other users of the same apps who were slow at the same times.
    same = next(((g.get("why") or {}).get("others") for g in at_slow
                 if (g.get("why") or {}).get("others") and not g["why"].get("same_as_above")), None)
    who_else = None
    if same:
        n_pcs = same["pcs"]
        if not n_pcs:
            who_else = f"Only this PC: no other PC had {same['what']} in this window."
        else:
            versions = same["versions"]
            spread = (f"all on {next(iter(versions))}" if len(versions) == 1
                      else ", ".join(f"{k} on {v}" for v, k in list(versions.items())[:3]))
            who_else = (f"{n_pcs} other PC(s) had the same failure ({same['what']}), {spread}. "
                         f"This PC has {same['this_version']}.")
    elif impact:
        apps = ", ".join(impact["applications"])
        users, k = impact["users"], impact["slow"]
        if not users:
            who_else = f"Nobody else used {apps} at those times."
        elif not k:
            who_else = f"Only this user: none of the {users} other {apps} users were slow."
        elif impact["unusual"]:
            who_else = (f"{k} of {users} other {apps} users were slow ({_pct(impact['rate'])}), more than on "
                         f"any of the last {impact['baseline_days']} days (at most {_pct(impact['usual_rate'])}).")
        elif impact["unusual"] is False:
            who_else = (f"{k} of {users} other {apps} users were slow ({_pct(impact['rate'])}), "
                         f"as usual at this hour (up to {_pct(impact['usual_rate'])} on previous days).")
        else:
            who_else = f"{k} of {users} other {apps} users were slow ({_pct(impact['rate'])}); no earlier days to compare."
        cp = impact["common_point"]
        if cp:
            who_else += f" {cp['slow']} of the {k} slow users share one network point: {cp['name']}."
            todo.insert(0, f"Many users are slow behind {cp['name']}: check it first.")
        elif impact["unusual"]:
            todo.insert(0, f"Many {apps} users are slow at once: check the {apps} service and its servers.")

    # title: the problem seen at the most slow actions; "likely" when that is more than half
    top = at_slow[0] if at_slow else None
    lead = next(({**c, "name": f"{c['application']} {c['activity']}" if c["activity"] else c["application"]}
                 for c in normal or [] if c["since"] and c["changes"]), None)
    # other users of the same apps were slow as often as on previous days at this hour
    as_usual = bool(impact and impact["slow"] and impact["unusual"] is False)
    usual_line = (f" Other {', '.join(impact['applications'])} users were slow as often as usual at this hour "
                  "(see Who else)." if as_usual else "")
    nothing = f"No problem was recorded within {minutes} min of any of the {n} slow actions."
    # the details stay in their own section; the reason says where to look
    if not n:
        level, title = "none", "No slow actions found"
        reason = "Nothing was slow in this window."
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
        weak = (top.get("why") or {}).get("weak")
        level = "likely" if top["slow_actions"] * 2 > n and not weak else "possible"
        title = f"{'Likely' if level == 'likely' else 'Possibly'}: {top['text']}"
        reason = f"It happened within {minutes} min of {top['slow_actions']} of the {n} slow actions."
        if weak:
            reason += (" Only the timing links them: Aternity measured nothing in it as longer than normal, "
                       "so it may not be the cause.")
        reason += usual_line
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
            "when": iso(worst.timestamp), "device": worst.device},
        # each PC the user worked on: actions and slow actions (a user may have several)
        "pcs": [{"device": d, "actions": sum(1 for s in samples if s.device == d),
                 "slow": sum(1 for s in slow if s.device == d)}
                for d in sorted({s.device for s in samples}, key=lambda d: -sum(1 for s in slow if s.device == d))],
        "logon": (logons or {}).get("starts", [None])[0] if (logons or {}).get("starts") else None,
        "logon_unit": (logons or {}).get("unit"),
    }
    return {
        "level": level, "title": title, "reason": reason, "felt": felt, "time_went": split,
        "who_else": who_else, "same_failure": same, "found": found, "todo": todo,
        "normal": [c["text"] for c in normal or []], "checked": checks,
    }


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
