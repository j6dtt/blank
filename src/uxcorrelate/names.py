"""The names offered as you type in a Username or Device box.

Every username and device name seen in Aternity's activity
(BUSINESS_ACTIVITIES_RAW, those two columns only). At start the last
SEED_HOURS are read an hour at a time, newest first; then each hour the
time since. A name not seen for KEEP_DAYS (about what Aternity keeps of
activity) is dropped. Users and PCs with no activity in that time are not
offered: the boxes still take any name typed.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from .aternity import odata_time
from .http import short_error

log = logging.getLogger("uxcorrelate")
SEED_HOURS = 24
KEEP_DAYS = 8
MATCHES = 20  # names shown under the box
KINDS = ("username", "device")


class NameList:
    def __init__(self):
        self.seen: dict[str, dict[str, datetime]] = {k: {} for k in KINDS}  # name -> last seen
        self.read_from: datetime | None = None
        self.partial = False
        self.started = False  # the background read (one per process)
        self.lock = threading.Lock()

    def read(self, aternity, start: datetime, end: datetime) -> int:
        """Add the usernames and device names of one span of activity; how many rows were read."""
        entity, f = aternity.cfg.entities["activities"], aternity.cfg.fields["activities"]
        ts = f["timestamp"]
        before = len(aternity.warnings)
        rows = aternity.query(entity, f"{ts} ge {odata_time(start)} and {ts} lt {odata_time(end)}",
                              select=[f[k] for k in KINDS])
        with self.lock:
            for row in rows:
                for kind in KINDS:
                    name = str(row.get(f[kind]) or "").strip()
                    if name:
                        seen = self.seen[kind]
                        seen[name] = max(seen.get(name, end), end)
            self.partial = self.partial or len(aternity.warnings) > before
            newest = max((t for s in self.seen.values() for t in s.values()), default=end)
            cut = newest - timedelta(days=KEEP_DAYS)
            for seen in self.seen.values():
                for name in [n for n, t in seen.items() if t < cut]:
                    del seen[name]
            self.read_from = max(min(self.read_from or start, start), cut)
        return len(rows)

    def match(self, kind: str, text: str, limit: int = MATCHES) -> dict:
        """Names containing the text (any case), those starting with it first."""
        q = text.strip().lower()
        with self.lock:
            names = sorted(self.seen[kind], key=str.lower)
            info = {"count": len(names), "partial": self.partial,
                    "since": self.read_from.isoformat().replace("+00:00", "Z") if self.read_from else None}
        hits = [n for n in names if q in n.lower()] if q else []
        hits.sort(key=lambda n: not n.lower().startswith(q))  # stable: alphabetical within each
        return {"names": hits[:limit], "more": max(len(hits) - limit, 0), **info}


def keep_filled(names: NameList, make_client, sleep) -> None:
    """Runs in a background thread: fill the list, then add each hour as it passes."""
    now = datetime.now(timezone.utc)
    try:
        if make_client() is None:
            log.info("Name list: Aternity is not set, so no names are offered; the boxes take any name typed")
            return
    except Exception as exc:  # a broken config.yaml: investigations say so themselves
        log.warning("Name list: settings could not be read (%s); no names are offered", short_error(exc))
        return
    for back in range(SEED_HOURS):
        end = now - timedelta(hours=back)
        if not _read(names, make_client, end - timedelta(hours=1), end):
            break
    log.info("Name list ready: %d usernames and %d devices seen in Aternity activity",
             len(names.seen["username"]), len(names.seen["device"]))
    last = now
    while True:
        sleep(3600)
        now = datetime.now(timezone.utc)
        if _read(names, make_client, last - timedelta(hours=1), now):  # an hour of overlap: rows can arrive late
            last = now


def _read(names: NameList, make_client, start: datetime, end: datetime) -> bool:
    try:
        n = names.read(make_client(), start, end)
        log.info("Name list: read %d activity rows from %s to %s UTC; %d usernames, %d devices known",
                 n, start.strftime("%b %d %H:%M"), end.strftime("%H:%M"),
                 len(names.seen["username"]), len(names.seen["device"]))
        return True
    except Exception as exc:  # Aternity down: the boxes still take any name typed
        log.warning("Name list: Aternity activity could not be read (%s); trying again in an hour",
                    short_error(exc))
        return False
