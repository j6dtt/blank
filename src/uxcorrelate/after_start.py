"""A slow action right after its PC started up, against the same activity on all
PCs: runs right after a start-up of their PC and runs later, virtual desktops and
physical PCs apart. On the live system (verify, Nov 2026) app launches right after
a start-up were much slower, most of all on virtual desktops; other activities
were not. Plain counts and medians, no scores."""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta
from statistics import median

from .http import ApiError
from .models import parse_time

AFTER_START_MIN = 10  # "right after a start-up": up to this many minutes after its time
FLEET_HOURS = 24  # runs on all PCs read: this many hours up to the slow action
MIN_RUNS = 20  # fewer runs on either side: too few to compare
RATIO = 1.25  # "slower right after": this many times the share slow, or the median, of later runs


def _after(times: list[datetime], t: datetime) -> datetime | None:
    """The start-up that `t` is right after, if any."""
    i = bisect_right(times, t)
    if i and t - times[i - 1] <= timedelta(minutes=AFTER_START_MIN):
        return times[i - 1]
    return None


def kind(virtual: bool | None) -> str:
    return "virtual desktops" if virtual else "physical PCs" if virtual is False else "PCs"


def _side(runs: list, is_slow) -> dict:
    times = [s.response_time for s in runs if s.response_time is not None]
    slow = sum(1 for s in runs if is_slow(s))
    return {"runs": len(runs), "slow": slow, "share": slow / len(runs) if runs else 0.0,
            "median": round(median(times), 1) if times else None}


def compare(aternity, slow: list, starts: list[dict], is_slow, warnings: list[str]) -> list[dict]:
    """For each slow app activity of the user right after a start-up of its PC:
    the same activity on all PCs of that kind, right after a start-up and later."""
    mine: dict[str, list[datetime]] = {}
    for st in starts:
        t = parse_time(st.get("time"))
        if t is not None:
            mine.setdefault(st["device"], []).append(t)
    for times in mine.values():
        times.sort()
    picked = [(s, b) for s in slow if (b := _after(mine.get(s.device, []), s.timestamp))]
    if not picked or not hasattr(aternity, "activity_history") or not hasattr(aternity, "boots"):
        return []
    end = max(s.timestamp for s, _ in picked) + timedelta(minutes=1)
    start = end - timedelta(hours=FLEET_HOURS)
    try:
        boots = aternity.boots(start - timedelta(minutes=AFTER_START_MIN), end)
    except ApiError as exc:
        warnings.append(f"Start-ups of all PCs could not be read ({exc.short()}): "
                        "slow actions right after a start-up are not compared with other PCs.")
        return []
    fleet: dict[str, list[datetime]] = {}
    for b in boots:
        fleet.setdefault(b.device, []).append(b.timestamp)
    for times in fleet.values():
        times.sort()

    groups: dict[tuple, list] = {}
    for s, b in picked:
        groups.setdefault((s.application, s.activity, kind(s.virtual)), []).append((s, b))
    out: list[dict] = []
    runs_of: dict[tuple, list] = {}
    for (app, activity, k), items in groups.items():
        if (app, activity) not in runs_of:
            runs_of[app, activity] = aternity.activity_history(app, activity, start, end)
        same = [r for r in runs_of[app, activity] if kind(r.virtual) == k]
        after = [r for r in same if _after(fleet.get(r.device, []), r.timestamp)]
        later = [r for r in same if not _after(fleet.get(r.device, []), r.timestamp)]
        a, l = _side(after, is_slow), _side(later, is_slow)
        enough = a["runs"] >= MIN_RUNS and l["runs"] >= MIN_RUNS
        slower = enough and (a["share"] >= RATIO * l["share"] > 0 or (
            a["median"] is not None and l["median"] and a["median"] >= RATIO * l["median"]))
        worst, b = max(items, key=lambda x: x[0].response_time or 0)
        out.append({"application": app, "activity": activity, "kind": k, "count": len(items),
                    "device": worst.device, "seconds": worst.response_time,
                    "minutes": round((worst.timestamp - b).total_seconds() / 60),
                    "after": a, "later": l, "enough": enough, "slower": slower,
                    "hours": FLEET_HOURS})
    out.sort(key=lambda c: -(c["seconds"] or 0))
    return out


def sentence(c: dict) -> str:
    """ "On virtual desktops in the 24 h before, Microsoft Outlook Launch right after a
    start-up was slow 1,759 of 2,593 times (67%, median 40.5 s); later 786 of 1,814
    (43%, median 27.4 s)." """
    name = f"{c['application']} {c['activity']}" if c["activity"] else c["application"]
    side = lambda s: (f"{s['slow']:,} of {s['runs']:,} times ({s['share']:.0%}"  # noqa: E731
                      + (f", median {s['median']} s)" if s["median"] is not None else ")"))
    text = (f"On {c['kind']} in the {c['hours']} h before, {name} right after a start-up was slow "
            f"{side(c['after'])}; later {side(c['later'])}.")
    if not c["enough"]:
        return text + f" Too few runs to compare (under {MIN_RUNS} on one side)."
    return text
