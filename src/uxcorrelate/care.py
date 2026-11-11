"""The "Care list" tab (/api/care): named users checked every 15 minutes.

The list is `<data>/care_list.json` (LIST_FILE), written by the page's "Add
a user" and a tile's "Remove" (`add`, `remove`) and read at every check;
CARE_MAX at most. It is made once from `care_list` in config.yaml (where the
list was before 2026-11-10), which is not read again after that. The page
has no login: anyone who can open it can change the list (each change is
logged with the address it came from). Each user is checked after each read
of the Today reader, from rows already read:
- an issue is an event Aternity rates Critical or Major (TILE_SEVERITIES)
  with their username today, or more actions Aternity rated Major
  (TOP_STATUS) by this time of day than on any of the days before that they
  used a PC (their own usual: the same kind of day, USUAL_DAYS at most,
  MIN_USUAL needed; read from their last HISTORY_DAYS of activity, by
  username, once a day);
- a slow-actions issue is a spell: it is over after QUIET with no Major
  action (the count is for the whole day, so it alone would hold all day:
  seen on stage), or when they are back within their usual by that time of
  day. A Major action after that, still above their usual, starts a new
  spell; a late row less than QUIET after the last one joins it again.
  Events of one kind (category + name) are spells the same way: over after
  QUIET with no new one (user's choice, 2026-11-09); one seen only after it
  was over is kept as over, with no investigation;
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
QUIET = HOUR  # a slow spell is over after this long with no Major action
QUIET_WORDS = "the hour"  # QUIET in words: "none in the hour after 09:45"
WAIT_S = 10  # the investigation worker looks for work this often
LIST_FILE = "care_list.json"  # in the data folder: [{"username": ..., "label": ...}]
NAME_MAX, LABEL_MAX = 100, 60


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
        self.load_users = load_users  # -> list[CareUser]: care_list in config.yaml, only to make the list file
        self.store = Path(store) / "care" if store else None
        self.list_file = Path(store) / LIST_FILE if store else None
        self.typed: list[CareUser] | None = None  # the list changed on the page with no data folder (tests)
        self.checking = threading.RLock()  # one check or list change at a time (the reader and the page)
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

    # -- the list ---------------------------------------------------------------
    def listed(self) -> list[CareUser]:
        """The list: care_list.json in the data folder; made from config.yaml's care_list when
        it is not there yet (once: the page changes the file from then on)."""
        if self.list_file is None:
            return list(self.typed if self.typed is not None else self.load_users() or [])
        if self.list_file.exists():
            data = json.loads(self.list_file.read_text())
            if not isinstance(data, list):
                raise ValueError(f"{LIST_FILE} must be a list of users")
            users = [CareUser(username=e) if isinstance(e, str) else CareUser(
                username=str(e.get("username") or ""), label=str(e.get("label") or ""))
                for e in data if isinstance(e, (str, dict))]
            return [CareUser(u.username.strip(), u.label.strip()) for u in users if u.username.strip()]
        users = list(self.load_users() or [])
        try:
            self._write_list(users)
            log.info("Care list: %s made with the %d users of care_list in config.yaml. The list is "
                     "that file from now on (the page's Add a user and Remove change it); care_list in "
                     "config.yaml is not read again.", self.list_file, len(users))
        except OSError as exc:  # config.yaml's list still works; the page cannot change it
            self.note("list", f"{self.list_file} could not be written ({short_error(exc)}), so the list is still "
                              "care_list in config.yaml and cannot be changed on the page. The folder must be "
                              "writable by the container (uid 1000).")
        else:
            self.notes.pop("list", None)
        return users

    def _write_list(self, users: list[CareUser]) -> None:
        if self.list_file is None:
            self.typed = list(users)
            return
        self.list_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.list_file.with_name("." + self.list_file.name + ".tmp")
        tmp.write_text(json.dumps([{"username": u.username, "label": u.label} for u in users], indent=2) + "\n")
        os.replace(tmp, self.list_file)

    def _to_change(self) -> list[CareUser]:
        try:
            return self.listed()
        except Exception as exc:  # a broken file is not written over
            raise OSError(f"the list could not be read first: {short_error(exc)}") from exc

    def _set_users(self, users: list[CareUser]) -> None:
        seen, unique = set(), []
        for u in users:
            if u.username.lower() not in seen:
                seen.add(u.username.lower())
                unique.append(u)
        self.users, self.skipped = unique[:CARE_MAX], max(len(unique) - CARE_MAX, 0)

    def add(self, username: str, label: str = "", by: str = "") -> CareUser:
        """A new tile, from the page. ValueError says why not, in words for the page."""
        username, label = " ".join(username.split()), " ".join(label.split())
        if not username:
            raise ValueError("Type a username, as Aternity writes it.")
        if len(username) > NAME_MAX or len(label) > LABEL_MAX:
            raise ValueError(f"Too long: a username takes {NAME_MAX} characters at most, a label {LABEL_MAX}.")
        user = CareUser(username, label)
        user.username = self.aternity_name(user)  # as Aternity writes it, when seen in its activity
        with self.checking:
            users = self._to_change()
            same = next((u for u in users if u.username.lower() == user.username.lower()), None)
            if same:
                raise ValueError(f"{self._who(same)} is already on the Care list.")
            if len(users) >= CARE_MAX:
                raise ValueError(f"The Care list is full ({CARE_MAX} users): remove one first.")
            self._write_list(users + [user])
            self._set_users(users + [user])
        log.info("Care list: %s added on the page%s; %d users now", self._who(user), f" from {by}" if by else "",
                 len(self.users))
        return user

    def remove(self, username: str, by: str = "") -> CareUser | None:
        """Take a user off the list, with their saved issues and investigation. None: not on it."""
        user = username.strip().lower()
        with self.checking:
            users = self._to_change()
            gone = next((u for u in users if u.username.lower() == user), None)
            if gone is None:
                return None
            left = [u for u in users if u.username.lower() != user]
            self._write_list(left)
            self._set_users(left)
            with self.lock:
                name = self.names.pop(user, gone.username)
                self.issues.pop(user, None)
                self.reports.pop(user, None)
                self.history.pop(user, None)
                if user in self.pending:
                    self.pending.remove(user)
            if self.store:
                try:  # by the name's own ending: the file may carry Aternity's spelling of it
                    for path in self.store.glob("*-" + _file_name(name).rsplit("-", 1)[1]):
                        path.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("Care list: %s's saved issues could not be deleted (%s)", name, short_error(exc))
        log.info("Care list: %s removed on the page%s, with their saved issues; %d users now", self._who(gone),
                 f" from {by}" if by else "", len(self.users))
        return gone

    # -- each check -------------------------------------------------------------
    def check(self, client, now: datetime) -> None:
        """After each read of Aternity: each user's issues today; a new one queues an investigation."""
        with self.checking:
            self._check(client, now)

    def _check(self, client, now: datetime) -> None:
        try:
            users = self.listed()
            self.notes.pop("settings", None)
        except Exception as exc:  # a broken file: keep the last list
            self.note("settings", f"The Care list could not be read ({short_error(exc)}): the last list is kept. "
                                  f"It is {self.list_file or 'care_list in config.yaml'}.")
            users = self.users
        self._set_users(users)
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
                log.info("Care list: %s: \"%s\" is over (%s)", self._who(u), issue["text"], self._over(issue))
            if new:
                last = self.reports.get(user)
                if user not in self.pending and self.running != user and (
                        last is None or now - _when(last["at"]) >= RUN_EVERY):
                    self.pending.append(user)
            if new or found or cleared:
                self.save(user)
        self.as_of = now

    def _over(self, i: dict) -> str:
        """How a slow spell ended, in words."""
        if i.get("ended") == "usual":
            return f"back within their usual at {self.today.local(_when(i['cleared'])):%H:%M}"
        return f"none in {QUIET_WORDS} after {self.today.local(_when(i.get('last') or i['since'])):%H:%M}"

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
            k = len(major) - 1  # the latest spell: Major actions less than QUIET apart
            while k > 0 and major[k][0] - major[k - 1][0] < QUIET:
                k -= 1
            last = major[-1][0]
            # from the action that made it more than on any day before, within this spell
            since = max(major[k][0], major[u_["high"]][0])
            apps = Counter(r[3] for r in major).most_common(2)
            found.append({"key": f"slow {t}", "kind": "slow", "level": TOP_STATUS, "since": _iso(since),
                          "last": _iso(last), "quiet": now - last >= QUIET, "day": t.isoformat(),
                          "text": f"{len(major)} {TOP_STATUS} slow actions by {today.local(now):%H:%M}, the last "
                                  f"at {today.local(last):%H:%M}; on the "
                                  f"{u_['days']} {u_['kind']} before that they used a PC: {u_['low']} to "
                                  f"{u_['high']} by this time. Most in "
                                  + ", ".join(f"{a} ({n})" for a, n in apps) + "."})
        kinds: dict = {}
        for x in events:
            if sev.get(x[2], x[3]) in TILE_SEVERITIES:
                kinds.setdefault(x[2], []).append(x)
        for k, all_ in kinds.items():
            spells = [[all_[0]]]  # events of this kind less than QUIET apart
            for x in all_[1:]:
                if x[0] - spells[-1][-1][0] < QUIET:
                    spells[-1].append(x)
                else:
                    spells.append([x])
            for xs in spells:
                apps = sorted({x[5] for x in xs if x[5] and x[5] != "(no application)"})
                found.append({"key": f"event {t} {k[0]}/{k[1]} {_iso(xs[0][0])}", "kind": "event",
                              "what": f"{k[0]}/{k[1]}", "level": sev.get(k, xs[0][3]), "since": _iso(xs[0][0]),
                              "last": _iso(xs[-1][0]), "quiet": now - xs[-1][0] >= QUIET, "day": t.isoformat(),
                              "text": f"{k[1]} ({k[0]})" + (f" {len(xs)} times" if len(xs) > 1 else "")
                                      + (f" in {', '.join(apps[:3])}" if apps else "")
                                      + f", first at {today.local(xs[0][0]):%H:%M}"
                                      + (f", the last at {today.local(xs[-1][0]):%H:%M}" if len(xs) > 1 else "")
                                      + " on " + ", ".join(sorted({x[1] for x in xs})) + "."})
            if not self.names.get(user) and all_[0][4]:
                self.names[user] = all_[0][4]
        if major and major[0][2]:
            self.names.setdefault(user, major[0][2])
        return found

    def _merge(self, user: str, found: list[dict], t: date, now: datetime) -> tuple[list, list]:
        """Today's issues into the user's list: (the new ones, the slow ones that cleared)."""
        cut = (t - timedelta(days=KEEP_DAYS)).isoformat()
        with self.lock:
            mine = [i for i in self.issues.get(user, []) if i["day"] > cut]
            new, cleared = [], []
            slow = [i for i in mine if i["kind"] == "slow" and i["day"] == t.isoformat()]
            last = slow[-1] if slow else None
            over = next((dict(f) for f in found if f["kind"] == "slow"), None)  # above their usual today
            now_slow = over if over and not over.pop("quiet") else None  # ... with a Major action within QUIET
            end = last.get("last", last["since"]) if last is not None else None
            if last is not None and not last.get("cleared") and (now_slow is None or now_slow["since"] > end):
                if over and now_slow is None:
                    last["last"] = over["last"]  # every Major action since it opened was in it
                last.update(cleared=_iso(now), ended="quiet" if over else "usual")
                cleared.append(last)
            if now_slow and last is not None and now_slow["since"] <= last.get("last", last["since"]):
                for k in ("cleared", "ended"):  # the same spell: a late row joined it again
                    last.pop(k, None)
                last.update(text=now_slow["text"], last=now_slow["last"])
            elif now_slow:  # the first today, or a new spell
                now_slow["key"] = f"slow {t} {len(slow) + 1}"
                mine.append(now_slow)
                new.append(now_slow)
            for f in found:
                if f["kind"] != "event":
                    continue
                f = dict(f)
                quiet = f.pop("quiet")
                # the issue this spell grew from: same kind, same day, overlapping (spells only grow)
                i = next((i for i in mine if i["kind"] == "event" and i["day"] == f["day"]
                          and (i.get("what") or i["key"].split(" ", 2)[2]) == f["what"]
                          and f["since"] <= i.get("last", i["since"]) and f["last"] >= i["since"]), None)
                if i is not None:
                    i.update(text=f["text"], level=f["level"], last=f["last"], what=f["what"],
                             since=min(i["since"], f["since"]))
                    if quiet and not i.get("cleared"):
                        i.update(cleared=_iso(now), ended="quiet")
                        cleared.append(i)
                    elif not quiet and i.get("cleared"):
                        for k in ("cleared", "ended"):  # a late row joined it again
                            i.pop(k, None)
                elif quiet:  # first seen when it was already over: kept, not investigated
                    f.update(cleared=_iso(now), ended="quiet")
                    mine.append(f)
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
            if not any(x.username.lower() == user for x in self.users):
                return user  # taken off the list while it ran: nothing of theirs is kept
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
            notes.append(f"The Care list has {len(self.users) + self.skipped} users: the first {CARE_MAX} are "
                         f"checked, {self.skipped} skipped.")
        if not self.users:
            notes.append("No one is on the Care list: use \"Add a user\" above.")
        return {"ready": True, "as_of": _iso(self.as_of or now),
                "as_of_local": today.local(self.as_of or now).strftime("%H:%M"), "zone": today.zone_label,
                "date": t.isoformat(), "count": len(issue), "users": issue + other, "notes": notes,
                "checked": len(self.users), "max": CARE_MAX}

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
                        since_local=today.local(_when(i["since"])).strftime("%H:%M"), over=self._over(i))
                   for i in today_all if i.get("cleared")]
        rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        h = self.history.get(user)
        status = "issue" if mine else "ok"
        note = None
        if not today_all and h is not None and not h["rows"] and not actions:
            status, note = "unknown", f"Not seen in Aternity's activity in {HISTORY_DAYS} days: check the name " \
                                      "(click the tile to remove it, then add it again as Aternity writes it)."
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
                "usual": self.usual(user, now),
                # the most serious first (the tile shows the first two): Critical, then Major, then by time
                "issues": sorted(mine, key=lambda i: (rank.get(i["level"], 9), i["kind"] == "slow", i["since"])),
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
                i["over"] = self._over(i)
        if report:
            local = lambda k: self.today.local(_when(report[k]))  # noqa: E731
            report = dict(report, at_local=local("at").strftime("%b %d %H:%M"),
                          span_local=f"{local('start'):%H:%M} to {local('end'):%H:%M} {self.today.zone_label}")
        return {**tile, "history": issues, "report": report}
