"""The plain-language answer at the top of an investigation.

Everything here is written for a help-desk reader: plain counts ("at 3 of 4
slow actions"), no scores. Times are returned as ISO strings next to the
text, so the UI can show them in the reader's time zone and the CLI in UTC.
"""

from __future__ import annotations

import re

WHERE = {"pc": "on the PC", "network": "on the network", "server": "on the application's servers"}


def _pct(v: float | None) -> str:
    return "?" if v is None else f"{v:.0%}"


def problem_text(g: dict) -> str:
    """One problem row as a sentence (without the count)."""
    obj, what, worst, limit = g["object"], g["what"], g["worst"], g["limit"]
    if g["source"] == "Aternity" and g["app"]:
        m = re.match(r"(.+) (crashed|not responding \(s\))$", what)
        if m and m.group(2) == "crashed":
            return f"{m.group(1)} crashed on {obj}"
        if m:
            return f"{m.group(1)} was not responding on {obj} (up to {worst} s)"
    if g["source"] == "Aternity event":
        return f"Health event on {obj}: {what}"
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
                 minutes: int, split: dict | None) -> dict:
    n = len(slow)
    at_slow = [g for g in problems if g["slow_actions"]]
    found, todo = [], []
    for g in at_slow[:6]:
        why = g.get("why") or {}
        if why.get("same_as_above"):
            continue  # e.g. a hang's own health event: already explained above
        found.append({"text": g["text"], "count": f"at {g['slow_actions']} of {n} slow actions",
                      "when": g["first"], "kind": g["kind"], "why": why.get("text", [])})
        for step in why.get("todo") or [_todo(g)]:
            if step not in todo:
                todo.append(step)
    other = len(problems) - len(at_slow)
    if other and n:
        found.append({"text": f"{other} other problem(s) happened only at other times, not near a slow action.",
                      "count": None, "when": None, "kind": None, "why": []})
    if n and not any(g["kind"] == "network" for g in at_slow):
        if not netim_on:
            text = "The network was not checked: NetIM is not configured."
        elif not mapped:
            text = (f"The network was not checked: this PC's address ({user_ip or 'unknown'}) "
                    "is not in any NetIM subnet.")
        else:
            text = f"Nothing wrong on the network path at those times ({len(mapped)} NetIM objects checked)."
        found.append({"text": text, "count": None, "when": None, "kind": "network", "why": []})

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
    if not n:
        level, title = "none", "No slow actions found"
        reason = "Nothing was slow in this window."
    elif top is None:
        level, title = "unsure", "Cause not found"
        reason = f"No problem was recorded within {minutes} min of any of the {n} slow actions."
        if split:
            side = max(split, key=split.get)
            reason += f" Aternity measured most of the slow time {WHERE[side]} ({_pct(split[side])})."
    else:
        level = "likely" if top["slow_actions"] * 2 > n else "possible"
        title = f"{'Likely' if level == 'likely' else 'Possibly'}: {top['text']}"
        reason = f"It happened within {minutes} min of {top['slow_actions']} of the {n} slow actions."
        if (top.get("why") or {}).get("text"):
            reason += " " + top["why"]["text"][0]
    if not todo and n:
        todo.append(f"Nothing measured explains it: ask {who} what they were doing, and look at the device in Aternity.")

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
            "when": iso(worst.timestamp)},
        "logon": (logons or {}).get("starts", [None])[0] if (logons or {}).get("starts") else None,
        "logon_unit": (logons or {}).get("unit"),
    }
    return {
        "level": level, "title": title, "reason": reason, "felt": felt, "time_went": split,
        "who_else": who_else, "same_failure": same, "found": found, "todo": todo,
    }
