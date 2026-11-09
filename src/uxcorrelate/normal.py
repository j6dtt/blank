"""Each slow action compared with its own normal.

For the applications and activities (e.g. "Teams Launch") that were slow:
- how long it usually takes for this user or PC (up to their last 30 days;
  a live Aternity keeps about 8 days of activities, so the days said are the
  days found), and
  for the other users at the same time;
- on how many days Aternity marked most of its runs slow, and since when;
- what changed on the PC between the last normal day and the first slow one
  (INSTALLED_SOFTWARE_CHANGE_LOG);
- the PC at those slow actions (CPU, memory, disk queue from the same
  activity rows), next to its usual, even when under the limits.
Plain values and counts only: the reader compares them.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median

from .cause import _day, changes_between
from .models import plural

log = logging.getLogger("uxcorrelate")
BASELINE_DAYS = 30
PAIRS = 3  # the app + activity pairs with the most slow actions
MIN_HISTORY = 5  # earlier runs needed to say what is normal
MIN_SLOW = 2  # slow runs on a PC needed to say it became slow (one is a blip)
PC = (("cpu", "CPU", " %"), ("memory", "memory", " %"), ("disk_queue", "disk queue", ""))


def _med(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(median(values), 1) if values else None


def _status(mine: list) -> str | None:
    """Aternity's own status for these slow actions, when they all have the same one."""
    seen = {s.status for s in mine}
    return seen.pop() if len(seen) == 1 else None


def compare_actions(aternity, slow: list, start, end, username: str | None, is_slow,
                    window: timedelta = timedelta(minutes=5), samples: list | None = None) -> list[dict]:
    """One entry per slow application + activity: sentences ("text") and steps ("todo")."""
    if not slow or not hasattr(aternity, "activity_history"):
        return []
    out = []
    for (app, act), n in Counter((s.application, s.activity) for s in slow).most_common(PAIRS):
        mine = [s for s in slow if (s.application, s.activity) == (app, act)]
        dev = Counter(s.device for s in mine).most_common(1)[0][0]
        who = username or dev
        name = f"{app} {act}" if act else app
        log.info("Aternity: how long %s usually takes for %s and for other users", name, who)
        hist = [s for s in aternity.activity_history(app, act, start - timedelta(days=BASELINE_DAYS), start,
                                                     username=username, device=None if username else dev)
                if s.response_time is not None]
        same = (lambda s: s.username == username) if username else (lambda s: s.device == dev)
        # other users within `window` of these slow actions (the same span as "who else")
        near = lambda s: any(abs(s.timestamp - m.timestamp) <= window for m in mine)  # noqa: E731
        others = [s for s in aternity.activity_history(app, act, min(m.timestamp for m in mine) - window,
                                                       max(m.timestamp for m in mine) + window)
                  if not same(s) and near(s)]
        enough = len(hist) >= MIN_HISTORY
        # calendar days, counted as a reader would (Nov 3 to Nov 6: the last 3 days)
        # calendar days, today included when it has runs: 7 dates from Nov 2 to Nov 8 are the last 7 days
        span = ((start.date() - min(s.timestamp for s in hist).date()).days
                + any(s.timestamp.date() == start.date() for s in hist)) if hist else 0
        back = f"last {plural(max(span, 1), 'day')}" if hist else ""
        took, typ = _med(s.response_time for s in mine), _med(s.response_time for s in others)
        usual = _med(s.response_time for s in hist) if enough else None
        worst = max((s.response_time for s in mine if s.response_time is not None), default=None)
        if took is None:
            text = [f"{name}: {n} slow action(s), Aternity gave no time for them."]
        elif n == 1:
            text = [f"{name} took {took} s at its slow action."]
        else:
            text = [f"{name} took {took} s at its {n} slow actions (slowest {round(worst, 1)} s)."]
        text.append(f"Usually {usual} s for {who} (typical of {len(hist)} in the {back})."
                    if enough else f"{'Only ' + str(len(hist)) if hist else 'No'} earlier {name} for {who} in the activity Aternity keeps: "
                    f"too few to say what is normal for {who}.")
        users = {s.username or s.device for s in others}
        text.append(f"Other users within {window.seconds // 60} min: typical {typ} s ({len(users)} users)."
                    if typ is not None else f"No other user ran {name} within {window.seconds // 60} min.")

        # days on which Aternity marked most runs slow, and since when
        by_day: dict = defaultdict(list)
        for s in hist:
            by_day[s.timestamp.date()].append(s)
        days = sorted(by_day)
        bad = [d for d in days if sum(map(is_slow, by_day[d])) * 2 > len(by_day[d])]
        run = []
        for d in reversed(days):
            if d not in bad:
                break
            run.insert(0, d)
        since, changes, todo, changed, normal_day, from_runs, pcs = None, [], [], 0, None, [], []
        persistent, before_slow = False, None
        if enough:
            line = (f"Aternity marked most runs slow on {len(bad)} of the {len(days)} days {who} used it "
                    f"in the {back}")
            before_slow = sum(1 for h in hist if is_slow(h))
            if not bad:
                # how many earlier runs were slow too: "most runs not slow" can hide a few
                line = (f"On each of the {len(days)} days {who} used it in the {back}, most runs were not slow "
                        f"({before_slow} of {len(hist)} slow).")
            elif len(run) == len(days):
                line += ": every day, so it is not new."
            elif run:
                line += f", every day since {_day(by_day[run[0]][0].timestamp)}."
            else:
                line += "; the last day before this was normal."
            text.append(line)
            if len(run) < len(days):  # a normal day before the slow ones: what changed after it
                last = max(by_day[days[len(days) - len(run) - 1]], key=lambda s: s.timestamp)
                normal = last.timestamp
                first = (min(s.timestamp for s in by_day[run[0]] if is_slow(s)) if run
                         else min(s.timestamp for s in mine))
                since, normal_day = first, normal
                # every run of it on this PC from the first slow one: "slow 2 of 9 times", not "slow since"
                from_runs = [s for s in hist + [x for x in samples or slow if (x.application, x.activity) == (app, act)]
                             if s.device == dev and s.timestamp >= first and s.response_time is not None]
                # each PC it was slow on, in order: its own count, time and changes (a user on
                # pooled desktops can be slow on two; one with no change weakens the others')
                runs_all = hist + [x for x in samples or slow if (x.application, x.activity) == (app, act)]
                for d in dict.fromkeys(s.device for s in sorted(mine, key=lambda s: s.timestamp)):
                    first_d = min(s.timestamp for s in runs_all if s.device == d and s.timestamp >= first
                                  and s.response_time is not None and is_slow(s))
                    runs_d = [s for s in runs_all if s.device == d and s.timestamp >= first_d
                              and s.response_time is not None]
                    # changes up to when it first became slow (any PC): later ones cannot explain that
                    ch = changes_between(aternity, d, normal, first)
                    pcs.append({"device": d, "runs": len(runs_d), "slow": sum(1 for s in runs_d if is_slow(s)),
                                "took": _med(s.response_time for s in mine if s.device == d),
                                "changes": ch["changes"], "changed": ch.get("count", len(ch["changes"])),
                                "text": ch["text"]})
                devs = [p["device"] for p in pcs]
                if any(s.virtual for s in mine) and last.device and last.device not in devs:
                    # pooled virtual desktops: the normal run was on another desktop
                    text.append(f"The last normal {name} was on {last.device}, another virtual desktop: "
                                f"what changed is read on {' and '.join(devs)} only.")
                if len(pcs) > 1:
                    text.append(f"Slow on {len(pcs)} PCs: " + ", ".join(
                        f"{p['device']} {p['slow']} of {p['runs']} times ({p['took']} s)" for p in pcs) + ".")
                # became slow = slow on 2+ runs and on most runs since the first slow one, on a PC;
                # otherwise occasional: no slowdown to explain, no change to check
                persistent = any(p["slow"] >= MIN_SLOW and p["slow"] * 2 > p["runs"] for p in pcs)
                total = (sum(p["slow"] for p in pcs), sum(p["runs"] for p in pcs))
                text.append(f"Since {_day(first)}: slow {total[0]} of {total[1]} times"
                            + (": being slow is new." if persistent else ": occasional, not a slowdown."))
                text += [p["text"] for p in pcs if p["text"]]
                own = next(p for p in pcs if p["device"] == dev)
                changes, changed = own["changes"], own["changed"]
                for p in pcs if persistent else []:
                    if p["changes"]:
                        names = ", ".join(dict.fromkeys(c["name"] for c in p["changes"] if c["name"]))
                        quiet = [q["device"] for q in pcs if not q["changes"]]
                        todo.append(f"On {p['device']}: check what changed before {name} became slow ({names})."
                                    + (f" It was slow on {' and '.join(quiet)} too, where no change was logged: "
                                       "the changes do not explain it alone." if quiet else ""))

        pc = []
        for key, label, unit in PC:
            top = max((getattr(s, key) for s in mine if getattr(s, key) is not None), default=None)
            if top is not None:
                u = _med(getattr(s, key) for s in hist) if enough else None
                pc.append(f"{label} up to {round(top, 1)}{unit}" + (f" (usually {u}{unit})" if u is not None else ""))
        at = " and ".join(dict.fromkeys(s.device for s in sorted(mine, key=lambda s: s.timestamp)))
        text.append(f"{at} at {'that slow action' if n == 1 else 'those slow actions'}: {', '.join(pc)}." if pc
                    else "Aternity gave no CPU, memory or disk queue for those actions.")
        out.append({"application": app, "activity": act, "device": dev, "slow_actions": n, "took": took,
                    "usual": usual, "others_typical": typ, "others": len(users), "slow_days": len(bad),
                    "days_used": len(days), "since": since.isoformat().replace("+00:00", "Z") if since else None,
                    "changes": changes, "changed": changed, "enough": enough, "runs": len(hist), "back": back,
                    "worst": round(worst, 1) if worst is not None else None,
                    "virtual": any(s.virtual for s in mine), "status": _status(mine),
                    "pcs": [{k: v for k, v in p.items() if k != "text"} for p in pcs],
                    "persistent": persistent, "before_slow": before_slow, "before_runs": len(hist),
                    "from_runs": len(from_runs), "from_slow": sum(1 for s in from_runs if is_slow(s)),
                    "normal_day": normal_day.isoformat().replace("+00:00", "Z") if normal_day else None,
                    "text": text, "todo": todo})
    return out
