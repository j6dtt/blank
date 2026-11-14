"""The "Care list" tab (/api/care): named users checked after every read of Aternity (every
refresh_minutes of config.yaml, 15 unless set).

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
  QUIET with no new one (user's choice, 2026-11-09). A spell is one issue, with
  one count and one investigation; it is not what lights the tile;
- the alert (the tile's colour, /api/MSS/care/status; the user's rule,
  2026-11-14): on when a read brings an occurrence not seen before (a row
  first seen at that read: Aternity sends rows late), on for ALERT_READS
  reads (refresh_minutes each) after the last one that brought a new one,
  then the issue is "Earlier today". A failing disk (Aternity's "(Disk)" /
  "(Drive)" label on the failing part, DISK_PARTS) is the PC's state: on all
  day. What each issue has seen is saved ("seen"), so a restart does not
  light it again; at a user's first check (just added, or after a restart)
  only what happened within the alert's time lights it;
- an investigation of the user runs (one at a time, at most one per user each
  RUN_EVERY) while one of today's issues is not covered by the latest one:
  new since, before its window, or first seen after it ran (Aternity sent it
  late). From INV_PAD before their first issue today to now. Its headline is
  the tile's "Why"; the page opens the whole of it.
Every user has a tile: alerts first, then names not seen, then the rest.
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
import unicodedata
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
ALERT_READS = 3  # an alert stays on this many reads after the last read that brought a new occurrence
SUMMARY_MAX = 120  # characters in /api/MSS/care/status's summary
WAIT_S = 10  # the investigation worker looks for work this often
LIST_FILE = "care_list.json"  # in the data folder: [{"username": ..., "label": ...}]
NAME_MAX, LABEL_MAX = 100, 60


def _iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _when(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _inv_start(issues: list) -> datetime | None:
    """Where an investigation of a user's day starts: INV_PAD before their first issue of the
    day, open or over (one rule for the one that runs by itself and "Investigate again now",
    which took the most serious open one: a 06:04 crash, over, was left out on stage, Nov 2026)."""
    first = min((_when(i["since"]) for i in issues), default=None)
    return first - INV_PAD if first else None


def _name(i: dict) -> str:
    """An event issue's name, as Aternity writes it ("Application Crash")."""
    what = i.get("what") or re.sub(r" \d{4}-\d\d-\d\dT\S+$", "", i["key"].split(" ", 2)[2]
                                   if i["key"].count(" ") >= 2 else "")  # "event <day> Cat/Name <time>"
    return what.split("/", 1)[1] if "/" in what else what or "event"


def _ascii(text: str) -> str:
    """Plain ASCII for another platform: dashes and quotes as "-" and "'", accents dropped."""
    text = text.translate({0x2013: "-", 0x2014: "-", 0x2212: "-", 0x2011: "-", 0x2010: "-", 0x2018: "'",
                           0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2026: "...", 0xA0: " ", 0x2192: "->"})
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _utc(t: str) -> str:
    return f"{_when(t):%H:%M}"


def _fit(head: str, more: list[str]) -> str:
    """The most serious first, then "+N more: ..." as far as SUMMARY_MAX allows."""
    if more:
        names = list(dict.fromkeys(more))
        full = f"{head}; +{len(more)} more: {', '.join(names)}"
        text = full if len(full) <= SUMMARY_MAX else f"{head}; +{len(more)} more"
    else:
        text = head
    return text if len(text) <= SUMMARY_MAX else text[:SUMMARY_MAX - 3].rstrip(" ,;") + "..."


def _times(x: tuple) -> int:
    """How many times one health event row says it happened (1 when Aternity does not say)."""
    return x[7] if len(x) > 7 and x[7] else 1


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
        self.checked: set[str] = set()  # users checked since the start (or since they were added)
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

    def pin(self, now: datetime) -> str:
        """The PIN a change on the page needs: today's date, YYYYMMDD, in the page's time zone
        (the user's choice, Nov 2026). The page has no login: this keeps a slip of the hand
        out, not someone who knows the rule."""
        return self.today.local(now).strftime("%Y%m%d")

    def relabel(self, username: str, label: str, by: str = "") -> CareUser | None:
        """A new label for a user on the list (their issues and investigation stay). None: not on it."""
        user, label = username.strip().lower(), " ".join(label.split())
        if len(label) > LABEL_MAX:
            raise ValueError(f"Too long: a label takes {LABEL_MAX} characters at most.")
        with self.checking:
            users = self._to_change()
            mine = next((u for u in users if u.username.lower() == user), None)
            if mine is None:
                return None
            before, mine.label = mine.label, label
            self._write_list(users)
            self._set_users(users)
        log.info("Care list: %s: label changed on the page from \"%s\" to \"%s\"%s", mine.username, before, label,
                 f" from {by}" if by else "")
        return mine

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
                self.checked.discard(user)
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
            new, again, cleared = self._merge(user, found, t, now, first=user not in self.checked)
            self.checked.add(user)
            for issue in new:
                log.info("Care list: new issue for %s: %s%s", self._who(u), issue["text"],
                         " It was already over when Aternity sent it." if issue.get("cleared") else "")
                if self.alert(issue, now):
                    log.info("Care list: %s: alert on. %s", self._who(u), self._clears(issue))
            for issue in again:
                log.info("Care list: %s: more of \"%s\" since the last read: alert on again. %s", self._who(u),
                         issue["text"], self._clears(issue))
            for issue in cleared:
                log.info("Care list: %s: \"%s\" is over (%s)", self._who(u), issue["text"], self._over(issue))
            if self._uncovered(user, t) and user not in self.pending and self.running != user:
                last = self.reports.get(user)
                if last is None or now - _when(last["at"]) >= RUN_EVERY:
                    self.pending.append(user)
            if new or found or cleared:
                self.save(user)
        self.as_of = now

    def _uncovered(self, user: str, t: date) -> list[dict]:
        """Today's issues the latest investigation of today does not cover: since after it ran or
        before its window, or first seen after it ran (Aternity sent it late: a 06:04 crash first
        seen at 08:00 was left out of an 08:04 one, stage, Nov 2026)."""
        day = t.isoformat()
        with self.lock:
            mine = [i for i in self.issues.get(user, []) if i["day"] == day]
            report = self.reports.get(user)
        if not report or self.today.day(_when(report["at"])) != t:
            return mine
        start, end, at = _when(report["start"]), _when(report["end"]), _when(report["at"])
        return [i for i in mine if not start <= _when(i["since"]) <= end
                or (i.get("found") and _when(i["found"]) > at)]

    # -- the alert ------------------------------------------------------------------
    def _window(self) -> timedelta:
        return timedelta(minutes=ALERT_READS * self.today.refresh_min)

    def alert(self, i: dict, now: datetime) -> bool:
        """Is this issue's alert on: a failing disk today, or a new occurrence in the last ALERT_READS reads."""
        if i.get("disk") and i["day"] == self.today.day(now).isoformat():
            return True
        return bool(i.get("fresh")) and now - _when(i["fresh"]) < self._window()

    def _alert_end(self, i: dict) -> datetime:
        """When its alert went off (an issue of the build before, or never lit: its last occurrence)."""
        return _when(i["fresh"]) + self._window() if i.get("fresh") else _when(i.get("last") or i["since"])

    def _over(self, i: dict) -> str:
        """How a slow spell ended, in words."""
        if i.get("ended") == "usual":
            return f"back within their usual at {self.today.local(_when(i['cleared'])):%H:%M}"
        return f"none in {QUIET_WORDS} after {self.today.local(_when(i.get('last') or i['since'])):%H:%M}"

    def _clears(self, i: dict) -> str:
        """When its alert goes off if no read brings a new one."""
        if i.get("disk"):
            return "Stays on today: a failing disk is the PC's state."
        at = self.today.local(_when(i["fresh"]) + self._window()) if i.get("fresh") else None
        new = f"new {TOP_STATUS} action" if i["kind"] == "slow" else f"new {_name(i)}"
        return f"Clears at {at:%H:%M} if no {new}." if at else ""

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
                          "count": len(major), "apps": [a for a, _ in apps],
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
                times = sum(_times(x) for x in xs)  # HEALTH_EVENT_VOLUME added up, as investigations do
                found.append({"key": f"event {t} {k[0]}/{k[1]} {_iso(xs[0][0])}", "kind": "event",
                              "what": f"{k[0]}/{k[1]}", "level": sev.get(k, xs[0][3]), "since": _iso(xs[0][0]),
                              "last": _iso(xs[-1][0]), "quiet": now - xs[-1][0] >= QUIET, "day": t.isoformat(),
                              "count": times, "disk": any(len(x) > 6 and x[6] for x in xs),
                              "pcs": sorted({x[1] for x in xs}),
                              "apps": [a for a, _ in Counter(x[5] for x in xs if x[5] in apps).most_common()],
                              "text": f"{k[1]} ({k[0]})" + (f" {times} times" if times > 1 else "")
                                      + (f" in {', '.join(apps[:3])}" if apps else "")
                                      + f", first at {today.local(xs[0][0]):%H:%M}"
                                      + (f", the last at {today.local(xs[-1][0]):%H:%M}" if len(xs) > 1 else "")
                                      + " on " + ", ".join(sorted({x[1] for x in xs})) + "."})
            if not self.names.get(user) and all_[0][4]:
                self.names[user] = all_[0][4]
        if major and major[0][2]:
            self.names.setdefault(user, major[0][2])
        return found

    def _merge(self, user: str, found: list[dict], t: date, now: datetime,
               first: bool = False) -> tuple[list, list, list]:
        """Today's issues into the user's list: (the new ones, those a read brought more of, the
        spells that ended). first: the user's first check since the start or since they were
        added; then only an occurrence within the alert's time lights the alert."""
        cut = (t - timedelta(days=KEEP_DAYS)).isoformat()
        window = self._window()

        def seen(i: dict, f: dict) -> bool:
            """More of it than seen before: the alert comes on (again) from this read."""
            count = f["count"]
            if "seen" not in i:  # saved by the build before: what it had is not new
                i["seen"] = count
                return False
            if count <= i["seen"]:
                return False
            i["seen"] = count
            if first and now - _when(f["last"]) >= window:
                return False  # it happened before the user was checked: not lit now
            i["fresh"] = _iso(now)
            return True

        def start(f: dict) -> dict:
            f.update(found=_iso(now), seen=0)
            seen(f, f)
            f.pop("count", None)
            return f

        with self.lock:
            mine = [i for i in self.issues.get(user, []) if i["day"] > cut]
            new, again, cleared = [], [], []
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
                last.update(text=now_slow["text"], last=now_slow["last"], apps=now_slow["apps"])
                if seen(last, now_slow):
                    again.append(last)
            elif now_slow:  # the first today, or a new spell
                now_slow["key"] = f"slow {t} {len(slow) + 1}"
                mine.append(start(now_slow))
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
                             since=min(i["since"], f["since"]), pcs=f["pcs"], apps=f["apps"],
                             disk=bool(i.get("disk") or f["disk"]))
                    if seen(i, f):
                        again.append(i)
                    if quiet and not i.get("cleared"):
                        i.update(cleared=_iso(now), ended="quiet")
                        cleared.append(i)
                    elif not quiet and i.get("cleared"):
                        for k in ("cleared", "ended"):  # a late row joined it again
                            i.pop(k, None)
                else:
                    if quiet:  # first seen when it was already over: Aternity sent it late
                        f.update(cleared=_iso(now), ended="quiet")
                    mine.append(start(f))
                    new.append(f)
            self.issues[user] = mine
        return new, again, cleared

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
            start = _inv_start(today) or now - HOUR - INV_PAD
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
        if not today.ready or self.as_of is None:
            return {"ready": False, "status": today.off or (today.status if not today.ready else
                                                            "Checking the Care list's users for the first time"),
                    "users": [], "count": 0}
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
                "checked": len(self.users), "max": CARE_MAX, "refresh_minutes": today.refresh_min,
                "alert_reads": ALERT_READS}

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
        mine = [dict(i, clears=self._clears(i)) for i in today_all if self.alert(i, now)]
        local = lambda k, i: today.local(_when(i[k])).strftime("%H:%M")  # noqa: E731
        earlier = sorted((dict(i, since_local=local("since", i), last_local=local("last", i) if i.get("last") else
                               local("since", i), over=self._over(i) if i.get("cleared") else None)
                          for i in today_all if not self.alert(i, now)), key=lambda i: i["since"])
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
                "start": _iso(_inv_start(today_all)) if today_all else None,  # "Investigate again now"
                "pcs": [{"device": d, "kind": kinds.get(d)} for d in pcs], "actions": actions, "major": major,
                "usual": self.usual(user, now),
                # the most serious first (the tile shows the first two): a failing disk, then Critical,
                # then Major, events before slow actions, then by time
                "issues": sorted(mine, key=lambda i: (not i.get("disk"), rank.get(i["level"], 9), i["kind"] == "slow",
                                                      i["since"])),
                "earlier": earlier,
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

    # -- /api/MSS/care/status: every tile for another platform ------------------------------
    def status(self, now: datetime) -> dict:
        """Each user's status as the tile shows it, for another platform that asks every few
        minutes (the user's request, 2026-11-14): all times UTC, a summary in our own words in
        plain ASCII under SUMMARY_MAX characters, no PC field. as_of is the last read in which
        every read of Aternity worked: older than 2 reads, the statuses are out of date."""
        today = self.today
        out = {"ready": False, "as_of": None, "refresh_minutes": today.refresh_min, "note": None, "users": []}
        v = self.view(now)
        if not v["ready"]:
            out["note"] = _ascii(v.get("status") or "Starting")
            return out
        if today.read_ok is None:
            out["note"] = "Aternity could not be read yet: no status until it is."
            return out
        out.update(ready=True, as_of=_iso(today.read_ok))
        if today.as_of and today.read_ok < today.as_of:
            out["note"] = (f"Aternity could not be read in full at {today.as_of:%H:%M} UTC: the statuses rest on "
                           f"the read of {today.read_ok:%H:%M} UTC.")
        midnight = _iso(today.midnight(today.day(now)))
        for u in v["users"]:
            row = {"username": u["username"], "label": u["label"] or "", "status": "ok", "level": None,
                   "since": None, "summary": ""}
            if u["status"] == "issue":
                row.update(status="alert", level=u["level"], since=u["since"],
                           summary=_fit(self._said(u["issues"][0], u), [self._short(i) for i in u["issues"][1:]]))
            elif u["status"] == "unknown":
                row.update(status="unknown",
                           summary=f"Not seen in Aternity in {HISTORY_DAYS} days: check the username")
            else:
                ends = [self._alert_end(i) for i in u["earlier"]]
                row["since"] = _iso(max(ends)) if ends else midnight
                row["summary"] = _fit(self._ok_words(u, ends), [])
            row["summary"] = _ascii(row["summary"])
            out["users"].append(row)
        return out

    def _short(self, i: dict) -> str:
        return f"{TOP_STATUS} slow actions" if i["kind"] == "slow" else _name(i)

    def _said(self, i: dict, u: dict) -> str:
        """The most serious alert in one line: what, where, when (UTC)."""
        if i["kind"] == "slow":
            usual = u.get("usual") or {}
            return (f"{u['major']} {TOP_STATUS} slow actions today"
                    + (f" (usual by now {usual['low']}-{usual['high']})" if usual.get("enough") else "")
                    + f", the last at {_utc(i.get('last') or i['since'])} UTC"
                    + (f", most in {i['apps'][0]}" if i.get("apps") else ""))
        apps, pcs, n = i.get("apps") or [], i.get("pcs") or [], i.get("seen") or 1
        what = _name(i) + (" - " + ", ".join(apps[:2]) + (f" +{len(apps) - 2}" if len(apps) > 2 else "")
                           if apps else "")
        on = ("on " + ", ".join(pcs) if len(pcs) <= 2 else f"on {len(pcs)} PCs") if pcs else ""
        if i.get("disk"):
            return f"{what} {on} since {_utc(i['since'])} UTC (stays on today)".replace("  ", " ")
        if n > 1:
            return f"{what}, {n} times {_utc(i['since'])}-{_utc(i.get('last') or i['since'])} UTC" \
                   + (f", {on}" if on else "")
        return f"{what} at {_utc(i['since'])} UTC" + (f" {on}" if on else "")

    def _ok_words(self, u: dict, ends: list[datetime]) -> str:
        if ends:
            since = f"OK since {max(ends):%H:%M} UTC."
            e = u["earlier"]
            if len(e) == 1:
                i = e[0]
                span = (f"{_utc(i['since'])}-{_utc(i['last'])} UTC" if i.get("last") and i["last"] != i["since"]
                        else f"at {_utc(i['since'])} UTC")
                what = self._short(i) + (" - " + i["apps"][0] if i["kind"] == "event" and i.get("apps") else "")
                return f"{since} Earlier today: {what} {span}"
            i = max(e, key=lambda i: i.get("last") or i["since"])
            what = self._short(i) + (" - " + i["apps"][0] if i["kind"] == "event" and i.get("apps") else "")
            return f"{since} Earlier today: {len(e)} issues, the last {what} at {_utc(i.get('last') or i['since'])} UTC"
        if not u["actions"]:
            return "OK: no activity today yet"
        usual = u.get("usual") or {}
        return (f"OK: {u['actions']:,} action{'' if u['actions'] == 1 else 's'} today, {u['major']:,} {TOP_STATUS}"
                + (f" (usual by now {usual['low']}-{usual['high']})" if usual.get("enough") else ""))
