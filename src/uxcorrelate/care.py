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
  used a PC (their own usual: the latest USUAL_DAYS such days, any day of
  the week, MIN_USUAL needed; read from their last HISTORY_DAYS of
  activity, by username, once a day). Not working days with working days,
  as the Today tab compares the whole fleet: people on the Care list often
  work weekends (the user, Nov 15 2026), and on a Saturday the 8 days read
  held 2 weekend days, so slow actions could never light a tile then.
  Each Major action is judged against their usual by its own time of day,
  the whole day at every check (`_replay`): judged only at the check, a
  Major action no check judged in time (a restart, the days before read
  late, rows sent late) met a usual grown since and was lost (stage, Nov 15
  2026). An OK tile with Major actions says the moment it came nearest;
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
  first seen at that read: Aternity sends rows late), and off at the
  ALERT_READS-th read of the Today reader after the last one that brought a
  new one ("left": reads counted, not minutes: a read takes time, so 15
  minutes held only 2 reads when reads were slow; stage, Nov 14 2026; a
  check after a change on the page is not a read), then the issue is
  "Earlier today". The tile says when Aternity sent it when that was more
  than a read after it happened, and which read clears it (the reader's
  schedule, `Today.next_read`). A failing disk (Aternity's "(Disk)" /
  "(Drive)" label on the failing part, DISK_PARTS) is the PC's state: on all
  day. What each issue has seen and its reads left are saved ("seen",
  "left"), so a restart does not light it again (one lit before a restart
  goes on counting, unless ALERT_READS reads' time passed); at a user's
  first check (just added, or after a restart) only what happened within
  the alert's time lights it;
- an investigation of the user runs (one at a time, at most one per user each
  RUN_EVERY) while one of today's issues is not covered by the latest one:
  new since, before its window, or first seen after it ran (Aternity sent it
  late). From INV_PAD before their first issue today to now. Its headline is
  the tile's "Why"; the page opens the whole of it.
Every user has a tile: alerts first (a failing disk, then Critical, then Major),
then names not seen, then the rest; inside each, the list's own order, set by
the page's "Arrange tiles" (`reorder`: the list file rewritten in that order).
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
from bisect import bisect_right
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .aternity import odata_quote, odata_time
from .config import SEVERITY_ORDER, TILE_SEVERITIES, TOP_STATUS, CareUser
from .http import short_error
from .models import parse_time
from .today import HOUR, MIN_USUAL, USUAL_DAYS, Today

log = logging.getLogger("uxcorrelate")
CARE_MAX = 50
HISTORY_DAYS = 8  # activity is kept about 8 days on the live tenant
KEEP_DAYS = 8
RUN_EVERY = HOUR  # at most one investigation per user in this time
INV_PAD = timedelta(minutes=30)
QUIET = HOUR  # a slow spell is over after this long with no Major action
QUIET_WORDS = "the hour"  # QUIET in words: "none in the hour after 09:45"
ALERT_READS = 3  # an alert goes off at this many reads after the last read that brought a new occurrence
SUMMARY_MAX = 120  # characters in /api/MSS/care/status's summary
STALE_READS = 3  # /api/MSS/care/status says the statuses are out of date after this many reads' time with no read
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
        self.counts = None  # usage.care_counts (web.py): tiles lit, for the admin page
        self.lock = threading.Lock()
        self.load()

    # -- files ----------------------------------------------------------------
    def load(self) -> None:
        """Each user's file, one at a time (one that cannot be read no longer stops the others
        being read). A user with two files (the name's spelling changed: the file name has it)
        keeps the newest."""
        if not self.store:
            return
        # the page's day, as _merge keeps them (the host's UTC day dropped a day early after 19:00 in New York)
        cut = self.today.day(datetime.now(timezone.utc)) - timedelta(days=KEEP_DAYS)
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            paths = sorted(self.store.glob("*.json"), key=lambda q: q.stat().st_mtime)  # the newest last: it wins
        except OSError as exc:
            self.note("files", f"The saved Care list in {self.store} could not be read ({short_error(exc)}). "
                               "The folder must be writable by the container (uid 1000).")
            return
        bad = []
        for path in paths:
            try:
                data = json.loads(path.read_text())
                user = str(data.get("username") or "").lower()
                issues = [i for i in data.get("issues", []) if date.fromisoformat(i["day"]) >= cut]
                report = data.get("report")
                if report and self.today.day(_when(report["at"])) < cut:
                    report = None
                if not user or (not issues and not report):
                    path.unlink()
                    continue
                self.issues[user], self.names[user] = issues, data["username"]
                self.reports.pop(user, None)
                if report:
                    self.reports[user] = report
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                bad.append(f"{path.name} ({short_error(exc)})")
        if bad:
            self.note("files", f"{len(bad)} saved Care list files in {self.store} could not be read: "
                               f"{'; '.join(bad[:3])}. Those users' saved issues start again.")

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
            for old in self.store.glob("*-" + path.name.rsplit("-", 1)[1]):  # the same user, spelled as before
                if old != path:
                    old.unlink(missing_ok=True)
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

    def reorder(self, usernames: list[str], by: str = "") -> bool:
        """The list's own order, from the page's "Arrange tiles": the tiles' order inside each group
        (a lit tile still goes first while it is lit) and /api/MSS/care/status's. ValueError: not
        exactly the users on the list now (it was changed while the page arranged it). False: the
        same order as before, nothing written."""
        want = [" ".join(str(n).split()).lower() for n in usernames]
        with self.checking:
            users = self._to_change()
            self._set_users(users)
            shown = [u.username.lower() for u in self.users]
            if sorted(want) != sorted(shown):
                raise ValueError("The Care list was changed while it was arranged (a user added or removed): "
                                 "cancel, then arrange it again.")
            if want == shown:
                return False
            first: dict[str, CareUser] = {}
            for u in users:
                first.setdefault(u.username.lower(), u)
            moved = [first[n] for n in want]
            taken = {id(u) for u in moved}
            users = moved + [u for u in users if id(u) not in taken]  # those past CARE_MAX stay after them
            self._write_list(users)
            self._set_users(users)
        log.info("Care list: the order of the tiles was changed on the page%s; now: %s", f" from {by}" if by else "",
                 ", ".join(f"{k}. {self._who(u)}" for k, u in enumerate(self.users, 1)))
        return True

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
    def check(self, client, now: datetime, read: bool = True) -> None:
        """After each read of Aternity (read: the Today reader's, which counts for the alert's
        reads; else a check after a change on the page): each user's issues today; a new one
        queues an investigation."""
        with self.checking:
            self._check(client, now, read)

    def _check(self, client, now: datetime, read: bool = True) -> None:
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
            if client is not None and (h is None or h["for"] != t or h.get("cut")):
                self._read_history(client, u, now)
            found = self.issues_now(u, now)
            first = user not in self.checked
            stale = self._restarted(user, now) if first else []
            usual = self.usual(user, now)  # slow actions judged only against a usual known in full
            new, again, cleared = self._merge(user, found, t, now, first=first,
                                              judged=bool(usual and usual["enough"]))
            self.checked.add(user)
            off = self._count_read(user, now, new + again) if read else []
            lit = sum(1 for i in new if self.alert(i, now)) + len(again)
            if lit and self.counts is not None:  # the admin page: tiles lit today
                self.counts(now, lit=lit)
            for issue in new:
                log.info("Care list: new issue for %s: %s%s", self._who(u), issue["text"],
                         " It was already over when first seen." if issue.get("cleared") else "")
                if self.alert(issue, now):
                    log.info("Care list: %s: alert on. %s", self._who(u), self._clears(issue))
            for issue in again:
                log.info("Care list: %s: more of \"%s\" since the last read: alert on again. %s", self._who(u),
                         issue["text"], self._clears(issue))
            for issue in off:
                log.info("Care list: %s: \"%s\" no longer lit: nothing new of it in %d reads since Aternity sent it "
                         "at %s.", self._who(u), issue["text"], ALERT_READS,
                         f"{self.today.local(_when(issue['fresh'])):%H:%M}")
            for issue in cleared:
                log.info("Care list: %s: \"%s\" is over (%s)", self._who(u), issue["text"], self._over(issue))
            if self._uncovered(user, t) and user not in self.pending and self.running != user:
                last = self.reports.get(user)
                if last is None or now - _when(last["at"]) >= RUN_EVERY:
                    self.pending.append(user)
            if new or found or cleared or off or stale:
                self.save(user)
        if read or self.as_of is None:  # "as of" is the last read of Aternity, not a change on the page
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
        """Is this issue's alert on: a failing disk today, or reads left before it goes off."""
        if i.get("disk") and i["day"] == self.today.day(now).isoformat():
            return True
        if "left" in i:
            return i["left"] > 0
        return bool(i.get("fresh")) and now - _when(i["fresh"]) < self._window()  # saved by the build before

    def _count_read(self, user: str, now: datetime, brought: list[dict]) -> list[dict]:
        """A read of the Today reader: one read less for each lit issue it brought nothing new of.
        Those that go off now (their time is kept: "OK since" in the status)."""
        mine = {id(i) for i in brought}
        off = []
        with self.lock:
            for i in self.issues.get(user, []):
                if id(i) in mine or not i.get("fresh"):
                    continue
                if "left" not in i:  # saved by the build before: the reads its time leaves
                    i["left"] = max(ALERT_READS - int((now - _when(i["fresh"])) / self._step()), 0)
                    if not i["left"]:
                        i["off"] = _iso(_when(i["fresh"]) + self._window())
                        continue
                if i["left"] > 0:
                    i["left"] -= 1
                    if not i["left"]:
                        i["off"] = _iso(now)
                        if not (i.get("disk") and i["day"] == self.today.day(now).isoformat()):
                            off.append(i)
        return off

    def _restarted(self, user: str, now: datetime) -> list[dict]:
        """A user's first check since the start: an issue lit before a restart goes on counting
        its reads, unless ALERT_READS reads' time has passed since (the reads missed while it was
        down are not known)."""
        gone = []
        with self.lock:
            for i in self.issues.get(user, []):
                if i.get("left") and now - _when(i["fresh"]) >= self._window():
                    i["left"], i["off"] = 0, _iso(_when(i["fresh"]) + self._window())
                    gone.append(i)
        return gone

    def _step(self) -> timedelta:
        return timedelta(minutes=self.today.refresh_min)

    def _alert_end(self, i: dict) -> datetime:
        """When its alert went off (an issue of the build before, or never lit: its last occurrence)."""
        if i.get("off"):
            return _when(i["off"])
        return _when(i["fresh"]) + self._window() if i.get("fresh") else _when(i.get("last") or i["since"])

    def _over(self, i: dict) -> str:
        """How a slow spell ended, in words."""
        if i.get("ended") == "usual":
            return f"back within their usual at {self.today.local(_when(i['cleared'])):%H:%M}"
        return f"none in {QUIET_WORDS} after {self.today.local(_when(i.get('last') or i['since'])):%H:%M}"

    def _clears(self, i: dict) -> str:
        """When Aternity sent it (when that was more than a read after it happened: a 17:21 crash
        first sent at 18:10 read as an hour-long light on stage, Nov 14 2026) and the read that
        clears it if no read brings a new one."""
        if i.get("disk"):
            return "Stays on today: a failing disk is the PC's state."
        if not i.get("fresh"):
            return ""
        sent, happened = _when(i["fresh"]), _when(i.get("last") or i["since"])
        said = ""
        if sent - happened > self._step():
            said = (f"Aternity sent {'the latest' if (i.get('seen') or 1) > 1 else 'it'} at "
                    f"{self.today.local(sent):%H:%M}. ")
        new = f"new {TOP_STATUS} action" if i["kind"] == "slow" else f"new {_name(i)}"
        nxt, left = self.today.next_read, i.get("left")
        if nxt is not None and left:
            at = self.today.local(nxt + self._step() * (left - 1))
            return f"{said}Clears at the {at:%H:%M} read if no {new}."
        at = self.today.local(sent + self._window())
        return f"{said}Clears at about {at:%H:%M} if no {new}."

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
        mark = len(client.warnings)
        try:
            rows = client.query(entity, f"{ts} ge {odata_time(start)} and {ts} le {odata_time(end)} and "
                                        f"{f['username']} eq {odata_quote(name)}",
                                select=[f[k] for k in ("timestamp", "username", "status") if f.get(k)])
        except Exception as exc:  # Aternity down: the next check tries again
            self.note("history", f"{u.username}'s activity of the days before could not be read "
                                 f"({short_error(exc)}); trying again at the next check.")
            return
        self.notes.pop("history", None)
        cut = len(client.warnings) > mark  # cut short: its days' counts are low (the usual too low)
        if cut:
            self.note("history", f"Aternity's reply for {u.username}'s days before was cut short: their usual "
                                 "is not judged until it is read whole (tried again at the next check).")
        days: dict[date, list] = {}
        for r in rows:
            when = parse_time(r.get(ts))
            if when is None or when >= end:
                continue
            d = self.today.day(when)
            major = days.setdefault(d, [])
            if r.get(f["status"]) == TOP_STATUS:
                major.append(self.today.clock_secs(when))
            if r.get(f["username"]):
                self.names[u.username.lower()] = r[f["username"]]
        self.history[u.username.lower()] = {"for": t, "days": days, "rows": len(rows), "cut": cut}
        log.info("Care list: read %d actions of %s over the %d days before (%d days with a PC in use)",
                 len(rows), self._who(u), HISTORY_DAYS, len(days))

    def usual(self, user: str, now: datetime) -> dict | None:
        """Their Major actions by this time of day on the latest USUAL_DAYS days before that they
        used a PC, any day of the week (people on the Care list often work weekends)."""
        h = self.history.get(user)
        if h is None:
            return None
        t = self.today.day(now)
        secs = self.today.clock_secs(now)
        same = sorted((d for d in h["days"] if d < t), reverse=True)[:USUAL_DAYS]
        past = [sum(1 for s in h["days"][d] if s <= secs) for d in same]
        if h.get("cut"):  # counts of a reply cut short: not a usual (it would light too often)
            return {"enough": False, "cut": True, "days": len(past), "kind": "days", "need": MIN_USUAL,
                    "read": HISTORY_DAYS}
        # "need" and "read" for the words when there are too few (a user new to Aternity or seldom
        # on a PC): the tile said "No issue: 3 rated Major" with no word that they were not compared
        if len(past) < MIN_USUAL:
            return {"enough": False, "days": len(past), "kind": "days", "need": MIN_USUAL, "read": HISTORY_DAYS}
        return {"enough": True, "days": len(past), "low": min(past), "high": max(past), "kind": "days"}

    def _replay(self, user: str, major: list, now: datetime) -> tuple[list[dict], dict | None]:
        """Today's slow spells, each Major action judged against their usual by its own time of day,
        the whole day at every check: (the spells, the moment today came nearest to their usual).
        It was judged only at each check, against their usual by then, which grows all day: a Major
        action no check judged in time (the app restarted, the days before read later, rows Aternity
        sent late, a user just added) was judged hours later and lost (stage, Nov 15 2026: "1 rated
        Major" with no "Earlier today"). A spell starts at a Major action above their usual by its
        time (more than on any of those days by then) and takes the Major actions less than QUIET
        apart; it is over after QUIET with none ("quiet") or once their usual by that time of day
        reaches today's count ("usual"); a later Major action still above starts a new spell."""
        u_ = self.usual(user, now)
        if not (u_ and u_["enough"]) or not major:
            return [], None
        today, t, h = self.today, self.today.day(now), self.history[user]
        same = sorted((d for d in h["days"] if d < t), reverse=True)[:USUAL_DAYS]
        times = {d: sorted(h["days"][d]) for d in same}
        steps = sorted({s for d in same for s in times[d]})  # times of day where their usual can rise

        def by(secs: float) -> tuple[int, int]:
            got = [bisect_right(times[d], secs) for d in same]
            return min(got), max(got)

        midnight = datetime(t.year, t.month, t.day, tzinfo=today.zone)
        at_clock = lambda s: (midnight + timedelta(seconds=s)).astimezone(timezone.utc)  # noqa: E731

        def end(sp: dict, upto: datetime) -> None:
            """Over before `upto`: QUIET after its last, or their usual reached its count, the earlier."""
            a, b = today.clock_secs(sp["last_t"]), today.clock_secs(upto)
            caught = next((s for s in steps if a < s <= b and by(s)[1] >= sp["count"]), None)
            quiet = sp["last_t"] + QUIET
            if caught is not None and at_clock(caught) < quiet:
                sp["ended"], sp["ended_at"] = "usual", at_clock(caught)
            elif quiet <= upto:
                sp["ended"], sp["ended_at"] = "quiet", quiet

        spells, cur, nearest, chain = [], None, None, None
        for k, r in enumerate(major):
            when, n = r[0], k + 1
            chain = chain if chain is not None and when - major[k - 1][0] < QUIET else when
            if cur is not None and cur["ended"] is None:
                end(cur, when)
            lo, hi = by(today.clock_secs(when))
            if nearest is None or n - hi > nearest["had"] - nearest["high"]:
                nearest = {"at": when, "had": n, "low": lo, "high": hi}
            if cur is not None and cur["ended"] is None:
                cur.update(last_t=when, count=n)
            elif n > hi:
                # where its run of Major actions starts: the chain's start, or after the spell before in it
                prev = spells[-1]["last_t"] if spells and spells[-1]["last_t"] >= chain else None
                start_t = chain if prev is None else next(x[0] for x in major if x[0] > prev)
                cur = {"since_t": when, "from_t": start_t, "last_t": when, "count": n, "ended": None,
                       "peak": (when, n, lo, hi)}
                spells.append(cur)
            if cur is not None and cur["ended"] is None and n - hi > cur["peak"][1] - cur["peak"][3]:
                cur["peak"] = (when, n, lo, hi)
        if cur is not None and cur["ended"] is None:
            end(cur, now)
        out = []
        for sp in spells:
            at, n, lo, hi = sp["peak"]
            apps = Counter(r[3] for r in major[:sp["count"]]).most_common(2)  # as its count: the day's so far
            out.append({"key": f"slow {t}", "kind": "slow", "level": TOP_STATUS, "since": _iso(sp["since_t"]),
                        "from": _iso(sp["from_t"]), "last": _iso(sp["last_t"]), "day": t.isoformat(),
                        "count": sp["count"], "ended": sp["ended"],
                        "ended_at": _iso(sp["ended_at"]) if sp["ended"] else None, "apps": [a for a, _ in apps],
                        "then": {"at": _iso(at), "had": n, "low": lo, "high": hi},
                        "text": f"{n} {TOP_STATUS} slow actions by {today.local(at):%H:%M}"
                                + (f", {sp['count']} by the last at {today.local(sp['last_t']):%H:%M}"
                                   if sp["count"] > n else "")
                                + f"; on the {u_['days']} {u_['kind']} before that they used a PC: {lo} to {hi} "
                                  "by the same time. Most in " + ", ".join(f"{a} ({c})" for a, c in apps) + "."})
        return out, nearest

    def _major_rows(self, user: str, t: date) -> list:
        today = self.today
        with today.lock:
            return sorted((r for h in today.hours.values() if h.day == t for r in h.top_rows
                           if (r[2] or "").lower() == user), key=lambda r: r[0])

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
        found = self._replay(user, major, now)[0]
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
               first: bool = False, judged: bool = True) -> tuple[list, list, list]:
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
            i["fresh"], i["left"] = _iso(now), ALERT_READS
            i.pop("off", None)
            return True

        def start(f: dict) -> dict:
            f.update(found=_iso(now), seen=0)
            seen(f, f)
            f.pop("count", None)
            return f

        with self.lock:
            mine = [i for i in self.issues.get(user, []) if i["day"] > cut]
            new, again, cleared = [], [], []
            # slow spells, judged over the whole day (_replay); each matched to the saved issues of its
            # run of Major actions (the build before judged at each check, so its since is later)
            slow = [i for i in mine if i["kind"] == "slow" and i["day"] == t.isoformat()]
            matched: set[int] = set()
            for f in (dict(f) for f in found if f["kind"] == "slow"):
                ended, ended_at, start_of = f.pop("ended"), f.pop("ended_at"), f.pop("from")
                mates = [i for i in slow if i["since"] <= f["last"] and i.get("last", i["since"]) >= start_of]
                if not mates:  # a new spell; one first seen when already over (late rows, a restart, the
                    # user just added, their usual known only now) is "Earlier today", as an event is
                    f["key"] = f"slow {t} {len(slow) + 1}"
                    if ended:
                        f.update(cleared=ended_at, ended=ended)
                    mine.append(start(f))
                    slow.append(f)
                    matched.add(id(f))
                    new.append(f)
                    continue
                matched.update(id(i) for i in mates)
                i = mates[0]
                i.update(text=f["text"], since=min(i["since"], f["since"]), apps=f["apps"], then=f["then"],
                         last=max(i.get("last", i["since"]), f["last"]))
                if seen(i, f):
                    again.append(i)
                if ended:
                    for m in mates:
                        if not m.get("cleared"):
                            m.update(cleared=ended_at, ended=ended)
                            cleared.append(m)
                elif i.get("cleared"):
                    for k in ("cleared", "ended"):  # the same spell: a late row joined it again
                        i.pop(k, None)
            for i in slow:  # saved, and above their usual at no time of its own now
                if id(i) in matched or i.get("cleared"):
                    continue
                end = _when(i.get("last") or i["since"]) + QUIET
                if judged:
                    i.update(cleared=_iso(now), ended="usual")
                elif end <= now:  # their usual not known now (days before cut short, not read): it was
                    # "back within their usual" all the same; over only when an hour passed with none
                    i.update(cleared=_iso(end), ended="quiet")
                else:
                    continue
                cleared.append(i)
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
        """The investigation worker: one at a time, in the order the issues came. An error the
        code did not expect is logged and the worker goes on (it once had no guard: one such
        error would stop every Care list investigation until a restart)."""
        while True:
            try:
                ran = self.run_next(investigate, clock())
            except Exception:
                log.exception("Care list: the investigation worker failed with an error the code did not expect; "
                              "it goes on with the next one")
                ran = None
            if ran is None:
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
        # lit first (a failing disk, then Critical, then Major: as a tile's own issues), then names not
        # seen, then the rest; inside each, the list's own order ("place", set by the page's Arrange
        # tiles: the user's choice, 2026-11-14)
        group = {"issue": 0, "unknown": 1}
        lit = lambda x: (not x["issues"][0].get("disk"), rank.get(x["level"], 9)) if x["status"] == "issue" else ()  # noqa: E731
        tiles = sorted((dict(self.tile(u, now), place=k) for k, u in enumerate(self.users)),
                       key=lambda x: (group.get(x["status"], 2), lit(x), x["place"]))
        issue = [x for x in tiles if x["status"] == "issue"]
        notes = list(self.notes.values())
        if self.skipped:
            notes.append(f"The Care list has {len(self.users) + self.skipped} users: the first {CARE_MAX} are "
                         f"checked, {self.skipped} skipped.")
        if not self.users:
            notes.append("No one is on the Care list: use \"Add a user\" above.")
        return {"ready": True, "as_of": _iso(self.as_of or now),
                "as_of_local": today.local(self.as_of or now).strftime("%H:%M"), "zone": today.zone_label,
                "date": t.isoformat(), "count": len(issue), "users": tiles, "notes": notes,
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
        mine = self._one_disk([dict(i, clears=self._clears(i)) for i in today_all if self.alert(i, now)])
        local = lambda k, i: today.local(_when(i[k])).strftime("%H:%M")  # noqa: E731
        earlier = sorted((dict(i, since_local=local("since", i), last_local=local("last", i) if i.get("last") else
                               local("since", i), over=self._over(i) if i.get("cleared") else None,
                               then_local=local("at", i["then"]) if i.get("then") else None)
                          for i in today_all if not self.alert(i, now)), key=lambda i: i["since"])
        rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        h = self.history.get(user)
        status = "issue" if mine else "ok"
        note = None
        if not today_all and h is not None and not h["rows"] and not actions:
            status, note = "unknown", f"Not seen in Aternity's activity in {HISTORY_DAYS} days: check the name " \
                                      "(click the tile to remove it, then add it again as Aternity writes it)."
        days = self._major_days(user, now, major, actions, issues)
        # the moment today came nearest to their usual (each Major action against their usual by its
        # time): an OK tile's "1 rated Major" says why it is no issue ("usual by now: 0 to 4" read as
        # a missing "Earlier today" for a Major action at 10:12, stage, Nov 15 2026)
        rows = self._major_rows(user, t)
        near = self._replay(user, rows, now)[1]
        # each Major action of today, latest first: the tile's "N rated Major" with nothing to show
        # which they were read as a bug (stage, Nov 15 2026: "1 rated Major" and no line about it)
        majors = [{"at": _iso(r[0]), "at_local": today.local(r[0]).strftime("%H:%M"), "pc": r[1], "app": r[3],
                   "activity": r[4], "secs": r[5]} for r in reversed(rows)]
        nearest = None if near is None else dict(near, at=_iso(near["at"]),
                                                 at_local=today.local(near["at"]).strftime("%H:%M"))
        # the latest issue before today: what the small chart's bars were (an OK tile with bars and
        # "0 rated Major" read as a contradiction: stage, Nov 14 2026)
        past = [i for i in issues if i["day"] < t.isoformat()]
        last = max(past, key=lambda i: i.get("last") or i["since"], default=None)
        before = None
        if last is not None:
            what = self._short(last) + (f" in {last['apps'][0]}" if last["kind"] == "event" and last.get("apps") else "")
            before = f"{today.local(_when(last.get('last') or last['since'])):%a %b %d}: {what}"
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
                "usual": self.usual(user, now), "nearest": nearest, "majors": majors,
                # the most serious first (the tile shows the first two): a failing disk, then Critical,
                # then Major, events before slow actions, then by time
                "issues": sorted(mine, key=lambda i: (not i.get("disk"), rank.get(i["level"], 9), i["kind"] == "slow",
                                                      i["since"])),
                "earlier": earlier, "before": before,
                "days": days, "by_local": today.local(now).strftime("%H:%M"),
                "why": why, "investigating": "now" if self.running == user else
                "waiting" if waiting else None}

    def _major_days(self, user: str, now: datetime, major: int, actions: int, issues: list[dict]) -> list[dict]:
        """The tile's bars: their Major actions by this time of day on each of the last KEEP_DAYS days,
        today last, from Aternity's own records (the days read for their usual, so the bars before
        today are the usual's numbers and today's is "N rated Major"). Each day says whether the
        Care list raised an issue then and whether it is one of the days of the usual. The bars were
        the issues this app saved: none for a day it did not check, and none for Major actions within
        the usual, so "3 rated Major" had nothing behind it (stage, Nov 15 2026).
        major None: not read (the read failed, or not yet today); pc False: no action measured."""
        t = self.today.day(now)
        secs = self.today.clock_secs(now)
        h = self.history.get(user)
        usual = sorted((d for d in (h or {}).get("days", {}) if d < t), reverse=True)[:USUAL_DAYS]
        raised = {i["day"] for i in issues}
        out = []
        for n in range(KEEP_DAYS - 1, -1, -1):
            d = t - timedelta(days=n)
            if d == t:
                got, pc = major, actions > 0
            elif h is None or d >= h["for"]:  # not read yet, or the read failed today
                got, pc = None, None
            else:
                times = h["days"].get(d)
                got, pc = (sum(1 for s in times if s <= secs), True) if times is not None else (0, False)
            out.append({"date": d.isoformat(), "major": got, "pc": pc, "issue": d.isoformat() in raised,
                        "usual": d in usual, "today": d == t, "cut": bool(h and h.get("cut")) and d != t})
        return out

    def _one_disk(self, lit: list[dict]) -> list[dict]:
        """A failing disk is the PC's state: its lit events are one line, not one per kind and spell
        (dev, Nov 14 2026: Faulty HD S.M.A.R.T. Status and two spells of HD Failure on one PC were
        three lines, the tile showing two and "+1 more")."""
        disks = sorted((i for i in lit if i.get("disk")), key=lambda i: i["since"])
        if len(disks) < 2:
            return lit
        rank = {s: k for k, s in enumerate(SEVERITY_ORDER)}
        names = list(dict.fromkeys(_name(i) for i in disks))
        pcs = sorted({p for i in disks for p in i.get("pcs") or []})
        n = sum(i.get("seen") or 1 for i in disks)
        first, last = disks[0]["since"], max(i.get("last") or i["since"] for i in disks)
        local = lambda x: f"{self.today.local(_when(x)):%H:%M}"  # noqa: E731
        one = dict(disks[0], level=min((i["level"] for i in disks), key=lambda v: rank.get(v, 9)), last=last,
                   seen=n, pcs=pcs, merged=len(disks),  # issues it stands for (the assessment counts them) apps=list(dict.fromkeys(a for i in disks for a in i.get("apps") or [])),
                   text=f"{' and '.join(names)} on {', '.join(pcs) or 'their PC'}: {n} events, first at "
                        f"{local(first)}" + (f", the last at {local(last)}" if last != first else "") + ".")
        return [one] + [i for i in lit if not i.get("disk")]

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
        every read of Aternity worked in full; the note says when one did not, or when there was
        no read for STALE_READS reads' time (the statuses are then out of date)."""
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
        elif now - today.read_ok > STALE_READS * timedelta(minutes=today.refresh_min):
            # no read at all for that long (the reader stopped, or a read hangs): said, not hidden
            out["note"] = (f"No read of Aternity since {today.read_ok:%H:%M} UTC (reads are every "
                           f"{today.refresh_min} min): the statuses are out of date.")
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
            then = i.get("then")  # by the moment it was most above their usual (_replay)
            usual = u.get("usual") or {}
            return ((f"{then['had']} {TOP_STATUS} slow actions by {_utc(then['at'])} UTC (usual by then "
                     f"{then['low']}-{then['high']})" if then else
                     f"{u['major']} {TOP_STATUS} slow actions today"
                     + (f" (usual by now {usual['low']}-{usual['high']})" if usual.get("enough") else ""))
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
        usual, near = u.get("usual") or {}, u.get("nearest")
        return (f"OK: {u['actions']:,} action{'' if u['actions'] == 1 else 's'} today, {u['major']:,} {TOP_STATUS}"
                + (f", never above their usual (nearest: {near['had']} by {_utc(near['at'])} UTC, usual "
                   f"{near['low']}-{near['high']})" if u["major"] and near and near["had"] <= near["high"] else
                   f" (usual by now {usual['low']}-{usual['high']})" if usual.get("enough") else
                   " (not compared: their days before were cut short)" if u["major"] and usual.get("cut") else
                   " (not compared: no usual yet)" if u["major"] and usual else
                   " (not compared: their days before not read yet)" if u["major"] else ""))
