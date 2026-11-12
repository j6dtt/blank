"""The names offered as you type in a Username or Device box.

Every username and device name seen in Aternity's activity
(BUSINESS_ACTIVITIES_RAW). The rows come from the Today reader
(`today.keep_reading`): the last 24 hours at start, then every refresh_minutes.
A name not seen for KEEP_DAYS (about what Aternity keeps of activity) is
dropped. Users and PCs with no activity in that time are not
offered: the boxes still take any name typed.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from .aternity import odata_time

KEEP_DAYS = 8
MATCHES = 20  # names shown under the box
KINDS = ("username", "device")


class NameList:
    def __init__(self):
        self.seen: dict[str, dict[str, datetime]] = {k: {} for k in KINDS}  # name -> last seen
        self.read_from: datetime | None = None
        self.partial = False
        self.lock = threading.Lock()

    def read(self, aternity, start: datetime, end: datetime) -> int:
        """Add the usernames and device names of one span of activity; how many rows were read."""
        entity, f = aternity.cfg.entities["activities"], aternity.cfg.fields["activities"]
        ts = f["timestamp"]
        before = len(aternity.warnings)
        rows = aternity.query(entity, f"{ts} ge {odata_time(start)} and {ts} lt {odata_time(end)}",
                              select=[f[k] for k in KINDS])
        self.add(rows, f, start, end, len(aternity.warnings) > before)
        return len(rows)

    def add(self, rows: list[dict], f: dict, start: datetime, end: datetime, partial: bool = False) -> None:
        """Add the names on activity rows of one span (f: the activity column names)."""
        with self.lock:
            for row in rows:
                for kind in KINDS:
                    name = str(row.get(f[kind]) or "").strip()
                    if name:
                        seen = self.seen[kind]
                        seen[name] = max(seen.get(name, end), end)
            self.partial = self.partial or partial
            newest = max((t for s in self.seen.values() for t in s.values()), default=end)
            cut = newest - timedelta(days=KEEP_DAYS)
            for seen in self.seen.values():
                for name in [n for n, t in seen.items() if t < cut]:
                    del seen[name]
            self.read_from = max(min(self.read_from or start, start), cut)

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
