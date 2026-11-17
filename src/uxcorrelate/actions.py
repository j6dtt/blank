"""The admin page's ACTIONS tab: every action Aternity measured today for one user, any user (not
only SecureView's), read now (the user, 2026-11-17: "search by user for today all actions with
timestamp", "in admin page"; docs/admin-roadmap.md).

One read of the activity (BUSINESS_ACTIVITIES_RAW) for that username from local midnight to now,
the columns shown only, at most ROWS_MAX rows; nothing kept. The username is asked for as Aternity
writes it (the names seen in its activity, as the Care list and the chats do); a name not among
them is asked for as typed, and said. A read cut short is said, never "no actions"."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .aternity import is_virtual, odata_quote, odata_time
from .config import TOP_STATUS
from .models import parse_time, to_float
from .names import KEEP_DAYS as NAME_DAYS
from .today import zone_of

ROWS_MAX = 5000  # rows of one read (a day of one user is usually far fewer)
# the activity columns read (config.py's names for BUSINESS_ACTIVITIES_RAW, all in Riverbed's $metadata)
COLUMNS = ("timestamp", "username", "application", "activity", "response_time", "status", "device",
           "client_device", "virtualization")


def spelled(names, user: str) -> tuple[str, str]:
    """(the username as Aternity writes it, a note when it is not among the names seen)."""
    user = " ".join(str(user).split())
    with names.lock:
        seen = [n for n in names.seen["username"] if n.lower() == user.lower()]
    if seen:
        return seen[0], ""
    return user, (f"\"{user}\" is not among the usernames seen in Aternity's activity in the last "
                  f"{NAME_DAYS} days: asked for as typed.")


def read(client, user: str, zone: str, now: datetime, note: str = "") -> dict:
    """The user's actions today, latest first, with local times to the second."""
    tz, label, _ = zone_of(zone)
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    entity, f = client.cfg.entities["activities"], client.cfg.fields["activities"]
    ts = f["timestamp"]
    flt = (f"{ts} ge {odata_time(start)} and {ts} lt {odata_time(now)} and "
           f"{f['username']} eq {odata_quote(user)}")
    before = len(client.warnings)
    rows = client.query(entity, flt, top=ROWS_MAX + 1, select=[f[k] for k in COLUMNS if f.get(k)])
    partial = len(client.warnings) > before
    cut = len(rows) > ROWS_MAX
    got = []
    for r in rows[:ROWS_MAX]:
        t = parse_time(r.get(ts))
        if t is None:
            continue
        virtual = is_virtual(r.get(f.get("virtualization", "")))
        got.append({"at": t.timestamp(), "t": t.astimezone(tz).strftime("%H:%M:%S"),
                    "app": r.get(f["application"]) or "", "activity": r.get(f.get("activity", "")) or "",
                    "rt": to_float(r.get(f.get("response_time", ""))), "status": r.get(f.get("status", "")) or "",
                    "desktop": r.get(f["device"]) or "", "sat_at": r.get(f.get("client_device", "")) or "",
                    "kind": "virtual" if virtual else "physical" if virtual is False else ""})
    got.sort(key=lambda x: -x["at"])
    apps = Counter(x["app"] for x in got)
    notes = [note] if note else []
    if cut:
        notes.append(f"More than {ROWS_MAX:,} actions today: the first {ROWS_MAX:,} Aternity sent are listed.")
    if partial:
        notes.append("Aternity's reply was cut short: these are the actions it sent, not all of today's.")
    return {"user": user, "date": local.date().isoformat(), "zone": label, "as_of_local": local.strftime("%H:%M"),
            "count": len(got), "first": got[-1]["t"] if got else None, "last": got[0]["t"] if got else None,
            "top": TOP_STATUS, "top_count": sum(1 for x in got if x["status"] == TOP_STATUS),
            "desktops": sorted({x["desktop"] for x in got if x["desktop"]}),
            "sat_at": sorted({x["sat_at"] for x in got if x["sat_at"]}),
            "apps": [[a, n] for a, n in sorted(apps.items(), key=lambda kv: (-kv[1], kv[0].lower()))],
            "rows": got, "cut": cut, "partial": partial, "notes": notes, "max": ROWS_MAX}
