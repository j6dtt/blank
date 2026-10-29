"""The plain-language answer at the top of an investigation.

Everything here is written for a help-desk reader: no scores, no "overlap".
Times are returned as ISO strings next to the text, so the UI can show them
in the reader's time zone and the CLI in UTC.
"""

from __future__ import annotations

import re

LEVELS = {"likely": "Likely", "possible": "Possibly", "unsure": "Not sure", "none": "No slowness found"}


def _pct(v: float | None) -> str:
    return "?" if v is None else f"{v:.0%}"


def _finding(e: dict, slow_moments: int, thresholds) -> tuple[str, str | None]:
    """(sentence, todo) for one evidence row."""
    obj, metric, worst, limit = e["object"], e["metric"], e["worst"], e["threshold"]
    matched = f"matches {e['degraded_intervals_matched']} of {slow_moments} slow moments"
    m = re.match(r"App (hang time|crashes) \((.+)\)", metric)
    if m:
        app = m.group(2)
        if m.group(1) == "crashes":
            return f"{app} crashed on {obj} ({matched})", f"On {obj}: check {app} (add-ins, updates) and its crash history."
        return f"{app} was not responding on {obj} ({matched})", f"On {obj}: check {app} (add-ins, updates, mailbox or file size) and restart it."
    if e["source"] == "Aternity event":
        return f"Device health event on {obj}: {metric} ({matched})", f"On {obj}: open the device's health events in Aternity."
    if e["source"] == "NetIM alert":
        return f"NetIM alert on {obj}: {metric} ({matched})", f"Check the NetIM alert on {obj}."
    if e["source"] == "NetIM":
        return (f"{obj}: {metric} reached {worst} (limit {limit}) ({matched})",
                f"Check what is loading {obj} during the slow moments (top talkers, errors).")
    if metric.startswith("Endpoint network latency"):
        return (f"{obj}'s network round trip reached {worst} ms (limit {limit}) ({matched})",
                f"Check {obj}'s connection: VPN, Wi-Fi or home network, and its route to the servers.")
    what = metric.replace(" %", "")
    return (f"{obj} {what} reached {worst} (limit {limit}) ({matched})",
            f"On {obj}: find which process used the {what.lower()} at that time.")


def build_answer(verdict: str, scores: dict, evidence: list[dict], degraded: int, samples: list,
                 slow: list, who: str, impact: dict | None, mapped: list, logons: dict | None,
                 thresholds) -> dict:
    # how sure: measured evidence for most slow moments, some, or none
    overlap = scores.get(verdict, {}).get("overlap", 0) if verdict in scores else 0
    if verdict == "none":
        level = "none"
    elif verdict == "inconclusive" or overlap == 0:
        level = "unsure"
    else:
        level = "likely" if overlap > 0.5 else "possible"

    found, todo = [], []
    # the verdict's own evidence first; a named NetIM device says more than the PC's own view
    ranked = sorted(evidence, key=lambda e: (e["domain"] != verdict, not e["source"].startswith("NetIM"),
                                             -e["coverage"]))
    for e in ranked[:5]:
        text, step = _finding(e, degraded, thresholds)
        found.append({"text": text, "when": e["first"], "domain": e["domain"]})
        if step and step not in todo:
            todo.append(step)
    if degraded:
        if not mapped:
            found.append({"text": "The network on this PC's path could not be checked: "
                                  "its subnet is not in NetIM.", "when": None, "domain": "network"})
        elif not any(e["domain"] == "network" for e in evidence):
            found.append({"text": f"Nothing wrong found on the network path ({len(mapped)} NetIM objects checked).",
                          "when": None, "domain": "network"})

    # who else, in one sentence
    just_them = None
    if impact:
        apps = ", ".join(impact["applications"])
        n, k = impact["users"], impact["slow"]
        if not n:
            just_them = f"Nobody else used {apps} at that time."
        elif not k:
            just_them = f"Yes: none of the {n} other {apps} users were slow."
        elif impact["unusual"]:
            just_them = (f"No: {k} of {n} other {apps} users were slow ({_pct(impact['rate'])}), more than on "
                         f"any of the last {impact['baseline_days']} days (at most {_pct(impact['usual_rate'])}).")
        elif impact["unusual"] is False:
            just_them = (f"Mostly: {k} of {n} other {apps} users were slow ({_pct(impact['rate'])}), "
                         f"as usual at this hour (up to {_pct(impact['usual_rate'])} on previous days).")
        else:
            just_them = f"{k} of {n} other {apps} users were slow ({_pct(impact['rate'])}); no earlier days to compare."
        cp = impact["common_point"]
        if cp:
            just_them += f" {cp['slow']} of the {k} slow users share one network point: {cp['name']}."
            todo.insert(0, f"Many users are slow behind {cp['name']}: check it first.")
        elif impact["unusual"]:
            todo.insert(0, f"Many {apps} users are slow at once: check the {apps} service and its servers.")

    # title
    top = next((e for e in ranked if e["domain"] == verdict), None)
    if level == "none":
        title, reason = LEVELS["none"], "No action took longer than the limit in this window."
    elif level == "unsure":
        title = f"{LEVELS['unsure']} what caused it"
        reason = "Nothing measured lined up with the slow moments."
    else:
        where = {"client": f"{who}'s PC", "network": "the network", "backend": "the application's servers"}[verdict]
        title = f"{LEVELS[level]}: {where}"
        reason = found[0]["text"] if found else ""
    if not todo and degraded:
        todo.append(f"Nothing measured explains it: ask {who} what they were doing, and look at the device in Aternity.")

    shares = {d: s.get("breakdown_share") for d, s in scores.items()}
    worst = max((s for s in slow if s.response_time is not None), key=lambda s: s.response_time, default=None)
    felt = {
        "actions": len(samples), "slow_actions": len(slow),
        "first": min(s.timestamp for s in slow).isoformat().replace("+00:00", "Z") if slow else None,
        "last": max(s.timestamp for s in slow).isoformat().replace("+00:00", "Z") if slow else None,
        "slowest": None if worst is None else {
            "app": worst.application, "activity": worst.activity, "seconds": round(worst.response_time, 1),
            "when": worst.timestamp.isoformat().replace("+00:00", "Z")},
        "logon": (logons or {}).get("slowest", [None])[0] if (logons or {}).get("slowest") else None,
    }
    return {
        "level": level, "title": title, "reason": reason, "felt": felt,
        "time_went": None if all(v is None for v in shares.values()) else
        {"pc": shares.get("client"), "network": shares.get("network"), "server": shares.get("backend")},
        "just_them": just_them, "found": found, "todo": todo,
    }
