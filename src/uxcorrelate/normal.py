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
PC = (("cpu", "CPU", " %"), ("memory", "memory", " %"), ("disk_queue", "disk queue", ""))


def _med(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(median(values), 1) if values else None


def _status(mine: list) -> str | None:
    """Aternity's own status for these slow actions, when they all have the same one."""
    seen = {s.status for s in mine}
    return seen.pop() if len(seen) == 1 else None


def compare_actions(aternity, slow: list, start, end, username: str | None, is_slow,
                    window: timedelta = timedelta(minutes=5)) -> list[dict]:
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
        since, changes, todo, changed = None, [], [], 0
        if enough:
            line = (f"Aternity marked most runs slow on {len(bad)} of the {len(days)} days {who} used it "
                    f"in the {back}")
            if not bad:
                line = (f"On each of the {len(days)} days {who} used it in the {back}, most runs were not slow: "
                        "being slow is new.")
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
                since = first
                if any(s.virtual for s in mine) and last.device and last.device != dev:
                    # pooled virtual desktops: the normal run was on another desktop
                    text.append(f"The last normal {name} was on {last.device}, another virtual desktop: "
                                f"what changed is read on {dev} only.")
                ch = changes_between(aternity, dev, normal, first)
                changes, changed = ch["changes"], ch.get("count", len(ch["changes"]))
                if ch["text"]:
                    text.append(ch["text"])
                if changes:
                    names = ", ".join(dict.fromkeys(c["name"] for c in changes if c["name"]))
                    todo.append(f"On {dev}: check what changed before {name} became slow ({names}).")

        pc = []
        for key, label, unit in PC:
            top = max((getattr(s, key) for s in mine if getattr(s, key) is not None), default=None)
            if top is not None:
                u = _med(getattr(s, key) for s in hist) if enough else None
                pc.append(f"{label} up to {round(top, 1)}{unit}" + (f" (usually {u}{unit})" if u is not None else ""))
        text.append(f"{dev} at {'that slow action' if n == 1 else 'those slow actions'}: {', '.join(pc)}." if pc
                    else "Aternity gave no CPU, memory or disk queue for those actions.")
        out.append({"application": app, "activity": act, "device": dev, "slow_actions": n, "took": took,
                    "usual": usual, "others_typical": typ, "others": len(users), "slow_days": len(bad),
                    "days_used": len(days), "since": since.isoformat().replace("+00:00", "Z") if since else None,
                    "changes": changes, "changed": changed, "enough": enough, "runs": len(hist), "back": back,
                    "worst": round(worst, 1) if worst is not None else None,
                    "virtual": any(s.virtual for s in mine), "status": _status(mine),
                    "text": text, "todo": todo})
    return out
