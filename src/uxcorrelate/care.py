"""The "Care list" tab (/api/care): named users checked every 15 minutes.

Each user in `care_list` (config.yaml, read at every check; CARE_MAX at most)
is checked after each read of the Today reader, from rows already read:
- an issue is an event Aternity rates Critical or Major (TILE_SEVERITIES)
  with their username today, or more actions Aternity rated Major
  (TOP_STATUS) by this time of day than on any of the days before that they
  used a PC (their own usual: the same kind of day, USUAL_DAYS at most,
  MIN_USUAL needed; read from their last HISTORY_DAYS of activity, by
  username, once a day);
- a slow-actions issue clears by itself at the first check where they are
  back within their usual by that time of day (a later rise is a new one);
  an event is a moment: it stays for the day;
- a new issue runs an investigation of the user (one at a time, at most one
  per user each RUN_EVERY), from INV_PAD before their first issue today to
  now. Its headline is the tile's "Why"; the page opens the whole of it.
Every user has a tile: issues first, then names not seen, then the rest.
Each user's issues (KEEP_DAYS) and latest investigation are saved in
<data>/care/<user>.json: usernames on disk (agreed 2026-11-09).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .aternity import odata_quote, odata_time
from .config import SEVERITY_ORDER, TILE_SEVERITIES, TOP_STATUS, CareUser
from .http import short_error
from .models import parse_time
from .today import HOUR, MIN_USUAL, USUAL_DAYS, Today, kind_of

log = logging.getLogger("uxcorrelate")
CARE_MAX = 50
HISTORY_DAYS = 8  # activity is kept about 8 days on the live tenant
KEEP_DAYS = 8
RUN_EVERY = HOUR  # at most one investigation per user in this time
INV_PAD = timedelta(minutes=30)
WAIT_S = 10  # the investigation worker looks for work this often


def _iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _when(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _file_name(username: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", username)[:40]
    return f"{safe}-{hashlib.sha1(username.lower().encode()).hexdigest()[:8]}.json"


class Care:
    def __init__(self, today: Today, load_users, store: str | os.PathLike | None = None, names=None):
        self.today = today
        self.name_list = names  # usernames seen in Aternity's activity: the exact spelling to ask for
        self.load_users = load_users  # -> list[CareUser] (config.yaml, read at every check)
        self.store = Path(store) / "care" if store else None
        self.users: list[CareUser] = []
        self.skipped = 0
        self.history: dict[str, dict] = {}  # user -> {"for": day read, "days": {day: [Major second of day]}, "rows"}
        self.issues: dict[str, list[dict]] = {}  # user -> issues of the last KEEP_DAYS
        self.reports: dict[str, dict] = {}  # user -> latest investigation
        self.names: dict[str, str] = {}  # user (lower case) -> the name as Aternity writes it
        self.pending: list[str] = []
        self.running: str | None = None
        self.notes: dict[str, str] = {}
        self.as_of: datetime | None = None
        self.started = False
        self.lock = threading.Lock()
        self.load()

    # -- files ----------------------------------------------------------------
    def load(self) -> None:
        if not self.store:
            return
        cut = date.today() - timedelta(days=KEEP_DAYS)
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.store.glob("*.json")):
                data = json.loads(path.read_text())
                user = str(data.get("username") or "").lower()
                issues = [i for i in data.get("issues", []) if date.fromisoformat(i["day"]) >= cut]
                report = data.get("report")
                if report and _when(report["at"]).date() < cut:
                    report = None
                if not user or (not issues and not report):
                    path.unlink()
                    continue
                self.issues[user], self.names[user] = issues, data["username"]
                if report:
                    self.reports[user] = report
        except (OSError, ValueError, KeyError) as exc:
            self.note("files", f"The saved Care list in {self.store} could not be read ({short_error(exc)}). "
                               "The folder must be writable by the container (uid 1000).")

    def save(self, user: str) -> None:
        if not self.store:
            return
        with self.lock:
            data = {"username": self.names.get(user, user), "issues": self.issues.get(user, []),
                    "report": self.reports.get(user)}
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            path = self.store / _file_name(data["username"])
            tmp = path.with_name("." + path.name + ".tmp")
            tmp.write_text(json.dumps(data, default=str))
            os.replace(tmp, path)
            self.notes.pop("files", None)
        except OSError as exc:
            self.note("files", f"The Care list could not be saved in {self.store} ({short_error(exc)}): its history "
                               "is lost on a restart. The folder must be writable by the container (uid 1000).")

    def note(self, key: str, text: str) -> None:
        if self.notes.get(key) != text:
            log.warning("Care list: %s", text)
        self.notes[key] = text

    # -- each check -------------------------------------------------------------
    def check(self, client, now: datetime) -> None:
        """After each read of Aternity: each user's issues today; a new one queues an investigation."""
        try:
            users = list(self.load_users() or [])
            self.notes.pop("settings", None)
        except Exception as exc:  # a broken config.yaml: keep the last list
            self.note("settings", f"care_list could not be read from config.yaml ({short_error(exc)}).")
            users = self.users
        seen, unique = set(), []
        for u in users:
            if u.username.lower() not in seen:
                seen.add(u.username.lower())
                unique.append(u)
        self.users, self.skipped = unique[:CARE_MAX], max(len(unique) - CARE_MAX, 0)
        t = self.today.day(now)
        for u in self.users:
            user = u.username.lower()
            h = self.history.get(user)
            if client is not None and (h is None or h["for"] != t):
                self._read_history(client, u, now)
            found = self.issues_now(u, now)
            new, cleared = self._merge(user, found, t, now)
            for issue in new:
                log.info("Care list: new issue for %s: %s", self._who(u), issue["text"])
            for issue in cleared:
                log.info("Care list: %s is back within their usual: \"%s\" cleared", self._who(u),
                         issue["text"])
            if new:
                last = self.reports.get(user)
                if user not in self.pending and self.running != user and (
                        last is None or now - _when(last["at"]) >= RUN_EVERY):
                    self.pending.append(user)
            if new or found or cleared:
                self.save(user)
        self.as_of = now

    def _who(self, u: CareUser) -> str:
        return f"{u.label} ({u.username})" if u.label else u.username

    def aternity_name(self, u: CareUser) -> str:
        """The username as Aternity writes it (a filter may match case exactly): seen today, else as typed."""
        user = u.username.lower()
        if self.name_list is not None:
            with self.name_list.lock:
                seen = [n for n in self.name_list.seen["username"] if n.lower() == user]
            if seen:
                return seen[0]
        return self.names.get(user, u.username)

    def _read_history(self, client, u: CareUser, now: datetime) -> None:
        t = self.today.day(now)
        name = self.aternity_name(u)
        start, end = self.today.midnight(t - timedelta(days=HISTORY_DAYS)), self.today.midnight(t)
        f, entity = client.cfg.fields["activities"], client.cfg.entities["activities"]
        ts = f["timestamp"]
        try:
            rows = client.query(entity, f"{ts} ge {odata_time(start)} and {ts} le {odata_time(end)} and "
                                        f"{f['username']} eq {odata_quote(name)}",
                                select=[f[k] for k in ("timestamp", "username", "status") if f.get(k)])
        except Exception as exc:  # Aternity down: the next check tries again
            self.note("history", f"{u.username}'s activity of the days before could not be read "
                                 f"({short_error(exc)}); trying again at the next check.")
            return
        self.notes.pop("history", None)
        days: dict[date, list] = {}
        for r in rows:
            when = parse_time(r.get(ts))
            if when is None or when >= end:
                continue
            d = self.today.day(when)
            major = days.setdefault(d, [])
            if r.get(f["status"]) == TOP_STATUS:
                major.append((when - self.today.midnight(d)).total_seconds())
            if r.get(f["username"]):
                self.names[u.username.lower()] = r[f["username"]]
        self.history[u.username.lower()] = {"for": t, "days": days, "rows": len(rows)}
        log.info("Care list: read %d actions of %s over the %d days before (%d days with a PC in use)",
                 len(rows), self._who(u), HISTORY_DAYS, len(days))

    def usual(self, user: str, now: datetime) -> dict | None:
        """Their Major actions by this time of day on the days before they used a PC (same kind of day)."""
        h = self.history.get(user)
        if h is None:
            return None
        t = self.today.day(now)
        secs = (now - self.today.midnight(t)).total_seconds()
        same = sorted((d for d in h["days"] if d < t and kind_of(d) == kind_of(t)), reverse=True)[:USUAL_DAYS]
        past = [sum(1 for s in h["days"][d] if s <= secs) for d in same]
        if len(past) < MIN_USUAL:
            return {"enough": False, "days": len(past), "kind": kind_of(t)}
        return {"enough": True, "days": len(past), "low": min(past), "high": max(past), "kind": kind_of(t)}

    def issues_now(self, u: CareUser, now: datetime) -> list[dict]:
        """Today's issues of one user, from what the Today reader holds."""
        user, today = u.username.lower(), self.today
        t = today.day(now)
        with today.lock:
            major = sorted((r for h in today.hours.values() if h.day == t for r in h.top_rows
                            if (r[2] or "").lower() == user), key=lambda r: r[0])
            events = sorted((x for x in today.health if today.day(x[0]) == t and (x[4] or "").lower() == user),
                            key=lambda x: x[0])
            sev = {k: c.most_common(1)[0][0] for k, c in today.health_sev.items() if c}
        found = []
        u_ = self.usual(user, now)
        if u_ and u_["enough"] and len(major) > u_["high"]:
            since = major[u_["high"]][0]  # the action that made it more than on any day before
            apps = Counter(r[3] for r in major).most_common(2)
            found.append({"key": f"slow {t}", "kind": "slow", "level": TOP_STATUS, "since": _iso(since),
                          "day": t.isoformat(),
                          "text": f"{len(major)} {TOP_STATUS} slow actions by {today.local(now):%H:%M}; on the "
                                  f"{u_['days']} {u_['kind']} before that they used a PC: {u_['low']} to "
                                  f"{u_['high']} by this time. Most in "
                                  + ", ".join(f"{a} ({n})" for a, n in apps) + "."})
        kinds: dict = {}
        for x in events:
            if sev.get(x[2], x[3]) in TILE_SEVERITIES:
                kinds.setdefault(x[2], []).append(x)
        for k, xs in kinds.items():
            apps = sorted({x[5] for x in xs if x[5] and x[5] != "(no application)"})
            found.append({"key": f"event {t} {k[0]}/{k[1]}", "kind": "event", "level": sev.get(k, xs[0][3]),
                          "since": _iso(xs[0][0]), "day": t.isoformat(),
                          "text": f"{k[1]} ({k[0]})" + (f" {len(xs)} times" if len(xs) > 1 else "")
                                  + (f" in {', '.join(apps[:3])}" if apps else "")
                                  + f", first at {today.local(xs[0][0]):%H:%M} on "
                                  + ", ".join(sorted({x[1] for x in xs})) + "."})
            if not self.names.get(user) and xs[0][4]:
                self.names[user] = xs[0][4]
        if major and major[0][2]:
            self.names.setdefault(user, major[0][2])
        return found

    def _merge(self, user: str, found: list[dict], t: date, now: datetime) -> tuple[list, list]:
        """Today's issues into the user's list: (the new ones, the slow ones that cleared)."""
        cut = (t - timedelta(days=KEEP_DAYS)).isoformat()
        with self.lock:
            mine = [i for i in self.issues.get(user, []) if i["day"] > cut]
            known = {i["key"]: i for i in mine}
            new, cleared = [], []
            slow = [i for i in mine if i["kind"] == "slow" and i["day"] == t.isoformat()]
            last = slow[-1] if slow else None
            now_slow = next((f for f in found if f["kind"] == "slow"), None)
            if now_slow and last is not None and not last.get("cleared"):
                last.update(text=now_slow["text"])
            elif now_slow:  # the first today, or a new rise after one cleared
                now_slow["key"] = f"slow {t} {len(slow) + 1}"
                mine.append(now_slow)
                new.append(now_slow)
            elif last is not None and not last.get("cleared"):
                last["cleared"] = _iso(now)
                cleared.append(last)
            for f in found:
                if f["kind"] == "slow":
                    continue
                if f["key"] in known:
                    known[f["key"]].update(text=f["text"], level=f["level"])
                else:
                    mine.append(f)
                    new.append(f)
            self.issues[user] = mine
        return new, cleared

    # -- investigations ---------------------------------------------------------
    def run_next(self, investigate, now: datetime) -> str | None:
        """Investigate the next user in line: investigate(username, start, end) -> the result."""
        with self.lock:
            if not self.pending:
                return None
            user = self.running = self.pending.pop(0)
        try:
            t = self.today.day(now)
            today = [i for i in self.issues.get(user, []) if i["day"] == t.isoformat()]
            start = min((_when(i["since"]) for i in today), default=now - HOUR) - INV_PAD
            name = self.names.get(user, user)
            u = next((x for x in self.users if x.username.lower() == user), CareUser(username=name))
            log.info("Care list: investigating %s, %s to %s", self._who(u), f"{self.today.local(start):%H:%M}",
                     f"{self.today.local(now):%H:%M}")
            report = {"at": _iso(now), "start": _iso(start), "end": _iso(now), "username": name}
            try:
                result = investigate(name, start, now)
                answer = result.get("answer") or {}
                report.update(title=answer.get("title"), reason=answer.get("reason"), level=answer.get("level"),
                              result=result)
                log.info("Care list: %s investigated: %s", self._who(u), answer.get("title"))
            except Exception as exc:
                report["error"] = short_error(exc)
                log.warning("Care list: %s could not be investigated (%s)", self._who(u), report["error"])
            with self.lock:
                self.reports[user] = report
            self.save(user)
            return user
        finally:
            self.running = None

    def work(self, investigate, sleep, clock=lambda: datetime.now(timezone.utc)) -> None:
        """The investigation worker: one at a time, in the order the issues came."""
        while True:
            if self.run_next(investigate, clock()) is None:
                sleep(WAIT_S)

    # -- the page -----------------------------------------------------------------
    def view(self, now: datetime) -> dict:
        today = self.today
        if not today.ready:
            return {"ready": False, "status": today.off or today.status, "users": [], "count": 0}
        t = today.day(now)
        rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        tiles = []
        for u in self.users:
            tiles.append(self.tile(u, now))
        issue = sorted((x for x in tiles if x["status"] == "issue"),
                       key=lambda x: (rank.get(x["level"], 9), -_when(x["since"]).timestamp()))
        other = sorted((x for x in tiles if x["status"] != "issue"),
                       key=lambda x: (x["status"] != "unknown", x["title"].lower()))  # names not seen first
        notes = list(self.notes.values())
        if self.skipped:
            notes.append(f"care_list has {len(self.users) + self.skipped} users: the first {CARE_MAX} are checked, "
                         f"{self.skipped} skipped.")
        if not self.users:
            notes.append("No one is on the Care list: add users under care_list in config.yaml (checked at the "
                         "next read, no restart).")
        return {"ready": True, "as_of": _iso(self.as_of or now),
                "as_of_local": today.local(self.as_of or now).strftime("%H:%M"), "zone": today.zone_label,
                "date": t.isoformat(), "count": len(issue), "users": issue + other, "notes": notes,
                "checked": len(self.users)}

    def tile(self, u: CareUser, now: datetime) -> dict:
        user, today = u.username.lower(), self.today
        t = today.day(now)
        with today.lock:
            hours = [h for h in today.hours.values() if h.day == t]
            pcs = sorted(set().union(*(h.user_devs.get(user, set()) for h in hours))) if hours else []
            kinds = {d: h.dev_kind[d] for h in hours for d in pcs if d in h.dev_kind}
            actions = sum(h.user_runs.get(user, 0) for h in hours)
            major = sum(1 for h in hours for r in h.top_rows if (r[2] or "").lower() == user)
        with self.lock:
            issues = [dict(i) for i in self.issues.get(user, [])]
            report = self.reports.get(user)
        today_all = [i for i in issues if i["day"] == t.isoformat()]
        mine = [i for i in today_all if not i.get("cleared")]
        cleared = [dict(i, cleared_local=today.local(_when(i["cleared"])).strftime("%H:%M"),
                        since_local=today.local(_when(i["since"])).strftime("%H:%M"))
                   for i in today_all if i.get("cleared")]
        rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        h = self.history.get(user)
        status = "issue" if mine else "ok"
        note = None
        if not today_all and h is not None and not h["rows"] and not actions:
            status, note = "unknown", f"Not seen in Aternity's activity in {HISTORY_DAYS} days: check the name in " \
                                      "care_list in config.yaml."
        days = []
        for n in range(KEEP_DAYS - 1, -1, -1):
            d = (t - timedelta(days=n)).isoformat()
            days.append({"date": d, "issues": sum(1 for i in issues if i["day"] == d)})
        why = None
        if report and today.day(_when(report["at"])) == t:
            why = {k: report.get(k) for k in ("title", "level", "error")}
            why["at_local"] = today.local(_when(report["at"])).strftime("%b %d %H:%M")
        waiting = user in self.pending or self.running == user
        return {"username": self.names.get(user, u.username), "label": u.label,
                "title": u.label or u.username, "status": status, "note": note,
                "level": min((i["level"] for i in mine), key=lambda s: rank.get(s, 9)) if mine else None,
                "since": min((i["since"] for i in mine), default=None),
                "since_local": today.local(_when(min(i["since"] for i in mine))).strftime("%H:%M") if mine else None,
                "pcs": [{"device": d, "kind": kinds.get(d)} for d in pcs], "actions": actions, "major": major,
                "usual": self.usual(user, now), "issues": sorted(mine, key=lambda i: i["since"]),
                "cleared": cleared,
                "days": days, "why": why, "investigating": "now" if self.running == user else
                "waiting" if waiting else None}

    def detail(self, username: str, now: datetime) -> dict | None:
        user = username.lower()
        u = next((x for x in self.users if x.username.lower() == user), None)
        if u is None:
            return None
        tile = self.tile(u, now)
        with self.lock:
            issues = sorted((dict(i) for i in self.issues.get(user, [])), key=lambda i: i["since"], reverse=True)
            report = self.reports.get(user)
        for i in issues:
            i["when_local"] = self.today.local(_when(i["since"])).strftime("%a %b %d %H:%M")
            if i.get("cleared"):
                i["cleared_local"] = self.today.local(_when(i["cleared"])).strftime("%H:%M")
        if report:
            local = lambda k: self.today.local(_when(report[k]))  # noqa: E731
            report = dict(report, at_local=local("at").strftime("%b %d %H:%M"),
                          span_local=f"{local('start'):%H:%M} to {local('end'):%H:%M} {self.today.zone_label}")
        return {**tile, "history": issues, "report": report}
