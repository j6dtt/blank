"""The "Inventory" tab, sixth (`/api/inventory`, `#inventory`; the user's "build it", Nov 21 2026;
proposal 2 in docs/roadmap.md, built on what the live Aternity fills: verify --inventory --columns).

One background reader (`keep_reading`) reads Aternity's device list every READ_EVERY_MIN (5,211
PCs in 1.4 s on live), one row per PC (its latest), and which PCs ran an application in the last
USE_DAYS (daily app usage, each PC once). All of it is held in memory: no PC, user or serial
number is written to disk. The lists opened by a click and "Find a PC" answer from that read, so
they cost Aternity nothing.

First cut: the tiles, "Needs a look" (`LINES`), last heard from with the agent's status, retired
PCs (the Software tab's count) and PCs nobody uses, Find a PC. Aternity's words are taken as it
sends them (config.INVENTORY_WORDS); every limit is a constant below.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from .aternity import odata_time
from .config import INVENTORY_COLUMNS, INVENTORY_FILLED, INVENTORY_ROLES, INVENTORY_WORDS, VERSION_SOURCES
from .http import short_error
from .models import parse_time, to_float
from .software import _partial, version_key
from .today import zone_of

log = logging.getLogger("uxcorrelate")

# ---- settings -----------------------------------------------------------------------
READ_EVERY_MIN = 60  # the device list is read this often (the user's choice, Nov 21 2026: hourly)
RETRY_MIN = 15  # ... and this soon again when Aternity did not answer
ROWS = 50_000  # rows of the device list asked for at most (5,211 PCs on live)
SCHEMA_HOURS = 24  # Aternity's list of columns is asked for again after this long
# the limits of "Needs a look" (the user: "you recommendations", Nov 21 2026)
SILENT_DAYS = 8  # not heard from: this many days or more (Aternity's own 'Disconnected' ends at 7)
OLD_DAYS = 31  # the last age group: this many days or more
USE_DAYS = 8  # not used: no application ran in these days (Aternity keeps activity about 8 days)
RESTART_DAYS = 14  # not restarted: the last start-up is more than this many days ago
STABILITY_LOW = 5  # Microsoft's stability index (1 to 10) under this
FREE_LOW_GB = 10  # little free space: Aternity's bands that end at this or under
PCS_SHOWN = 200  # PCs listed by a click
FIND_MIN, FIND_MAX = 2, 20  # Find a PC: characters needed, PCs listed
# -------------------------------------------------------------------------------------

R, W = INVENTORY_ROLES, INVENTORY_WORDS
LAST, STATUS = INVENTORY_COLUMNS["last"], INVENTORY_COLUMNS["status"]
# "Needs a look": key -> the line's name, most PCs first on the page
LINES = {"agent": "On an older agent than most", "bitlocker": "BitLocker off on a physical PC",
         "unused": f"Not used in {USE_DAYS} days", "restart": f"Not restarted in over {RESTART_DAYS} days",
         "silent": f"Not heard from in {SILENT_DAYS} days or more", "stopped": "Agent stopped",
         "stability": "Low stability index", "free": "Little free space on the system disk",
         "spinning": "Still on a spinning hard disk"}
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB)", re.I)
_OPEN = re.compile(r"\b(?:over|more than|above|greater than)\b|[>+]", re.I)
_GB = {"tb": 1024.0, "gb": 1.0, "mb": 1 / 1024, "kb": 1 / 1024 / 1024}


def band_top_gb(text) -> float | None:
    """The upper end, in GB, of one of Aternity's bands of free space ('10 GB to 20 GB' -> 20,
    'Less than 5 GB' -> 5). None for a band with no upper end ('Over 500 GB') or words that hold
    no size: such a band is never "little free space"."""
    s = str(text or "")
    sizes = [float(n) * _GB[u.lower()] for n, u in _SIZE.findall(s)]
    return None if not sizes or _OPEN.search(s) else max(sizes)


def is_virtual(kind) -> bool:
    """Aternity's kind of device says a virtual desktop ('Virtual Desktop')."""
    return W["virtual"] in str(kind or "").lower()


class Inventory:
    def __init__(self, zone: str = "", clock=lambda: datetime.now(timezone.utc)):
        self.zone, self.zone_label, _ = zone_of(zone)
        self.clock = clock
        self.lock = threading.Lock()
        self.pcs: dict[str, dict] = {}  # the PC's name in small letters -> its latest row (memory only)
        self.used: set[str] | None = None  # PCs that ran an application in USE_DAYS; None: not known
        self.have: set[str] = set()  # the columns of the device list this Aternity publishes
        self.as_of: datetime | None = None
        self.took: float | None = None
        self.notes: list[str] = []
        self.status = "Starting"
        self.off: str | None = None
        self.reading = False
        self.started = False
        self._schema: tuple[datetime, dict | None] | None = None

    # -- the read ---------------------------------------------------------------------
    def schema(self, client, now: datetime) -> dict | None:
        """Aternity's own list of columns (asked again after SCHEMA_HOURS); None: could not be read."""
        if self._schema and now - self._schema[0] < timedelta(hours=SCHEMA_HOURS) and self._schema[1] is not None:
            return self._schema[1]
        try:
            from .verify import parse_odata_metadata
            text = client.http.request("GET", "/$metadata", headers={"Accept": "application/xml"}).text
            self._schema = (now, parse_odata_metadata(text))
        except Exception as exc:
            log.warning("Inventory: Aternity's list of columns could not be read (%s)", short_error(exc))
            self._schema = (now, None)
        return self._schema[1]

    def read(self, client, cfg, now: datetime) -> None:
        """One read. An error of the device list is raised (the last read stays on the tab); app
        usage that cannot be read whole only leaves "used or not" unknown."""
        inv, dev = cfg.entities.get("inventory"), cfg.fields.get("inventory", {}).get("device")
        if not inv or not dev:
            raise ValueError("no device list is set (aternity.entities.inventory in config.yaml)")
        schema, notes = self.schema(client, now), []
        if schema is None:  # which columns to ask for is not known: asking for one it lacks is refused whole
            raise ValueError("Aternity's list of columns could not be read")
        if dev not in schema.get(inv, ()):
            raise ValueError(f"this Aternity does not publish its device list ({inv})")
        want = list(dict.fromkeys([LAST, STATUS] + [c for _, items in INVENTORY_FILLED for _, c, _ in items]))
        have = [c for c in want if c in schema[inv]]
        mark, t0 = len(client.warnings), time.monotonic()
        rows = client.query(inv, select=[dev] + have, top=ROWS)
        if _partial(client, mark, rows, ROWS):
            notes.append(f"Aternity's reply for the device list was cut short ({len(rows):,} rows): these counts are "
                         "partial.")
        pcs: dict[str, dict] = {}
        for r in rows:
            name = str(r.get(dev) or "").strip()
            if not name:
                continue
            t, seen = parse_time(r.get(LAST)) if LAST in have else None, pcs.get(name.lower())
            if seen is None or (t and (seen["heard"] is None or t > seen["heard"])):  # a PC listed twice: its latest
                pcs[name.lower()] = {"name": name, "heard": t, "row": r}
        used = self._used(client, schema, now, notes)
        with self.lock:
            self.pcs, self.used, self.have, self.as_of, self.notes = pcs, used, set(have), now, notes
            self.took = time.monotonic() - t0

    def _used(self, client, schema: dict, now: datetime, notes: list[str]) -> set[str] | None:
        """The PCs that ran an application in the last USE_DAYS (daily app usage, each PC once,
        added up by Aternity); None when it cannot be said: not published, failed or cut short."""
        u = VERSION_SOURCES["usage"]
        if not {u["device"], u["time"]} <= schema.get(u["entity"], set()):
            notes.append(f"This Aternity does not publish daily app usage ({u['entity']}): whether a PC is in use is "
                         "not known.")
            return None
        mark = len(client.warnings)
        try:
            got = client.query(u["entity"], f"{u['time']} ge {odata_time(now - timedelta(days=USE_DAYS))}",
                               select=[u["device"]], top=ROWS)
        except Exception as exc:
            notes.append(f"App usage could not be read ({short_error(exc)}): whether a PC is in use is not known.")
            return None
        if _partial(client, mark, got, ROWS):
            notes.append("Aternity's reply for app usage was cut short: whether a PC is in use is not known.")
            return None
        return {str(r.get(u["device"])).strip().lower() for r in got if r.get(u["device"])}

    # -- one PC -------------------------------------------------------------------------
    def age(self, t: datetime | None, now: datetime) -> int | None:
        """Whole local days since a time (0 = today); None: no time."""
        return None if t is None else max((now.astimezone(self.zone).date() - t.astimezone(self.zone).date()).days, 0)

    def _brief(self, pc: dict, what: str | None = None) -> dict:
        r = pc["row"]
        out = {"name": pc["name"], "kind": r.get(R["kind"]), "virtual": is_virtual(r.get(R["kind"])),
               "username": r.get(R["user"]), "department": r.get(R["department"]),
               "heard": pc["heard"].isoformat() if pc["heard"] else None}
        return dict(out, what=what) if what is not None else out

    # -- the rules ----------------------------------------------------------------------
    def _judged(self, now: datetime) -> dict:
        """Every "Needs a look" line from the read held: key -> {"pcs": [(PC, its own value in
        words)], "of": how many PCs it is counted among, "text": what it means; "unknown": why
        it cannot be said}. Called under the lock."""
        pcs = list(self.pcs.values())
        val = lambda pc, role: pc["row"].get(R[role]) if R[role] in self.have else None  # noqa: E731
        physical = [pc for pc in pcs if not is_virtual(val(pc, "kind"))]
        out: dict[str, dict] = {}

        def line(key, hits, of, text, unknown=None):
            out[key] = {"pcs": hits, "of": of, "text": text, "unknown": unknown}

        def missing(key, role_or_column, what):
            c = R.get(role_or_column, role_or_column)
            if c in self.have:
                return False
            line(key, [], 0, "", f"this Aternity does not publish {what}")
            return True

        # an older agent than the version most PCs have
        if not missing("agent", "agent", "the agent's version"):
            vs = Counter(str(val(pc, "agent")) for pc in pcs if val(pc, "agent"))
            most = max(vs, key=lambda v: (vs[v], version_key(v) or ())) if vs else None
            mk = version_key(most)
            older = [(pc, str(val(pc, "agent"))) for pc in pcs if val(pc, "agent") and mk is not None
                     and (version_key(val(pc, "agent")) or mk) < mk]
            line("agent", older, sum(vs.values()),
                 f"An Aternity agent older than {most}, the version most PCs have." if most else "")
        # BitLocker off, physical PCs only (a virtual desktop's disk is not the PC's to encrypt)
        if not missing("bitlocker", "bitlocker", "BitLocker's status") and not missing("bitlocker", "kind",
                                                                                     "the kind of device"):
            on = sum(1 for pc in pcs if val(pc, "bitlocker") and val(pc, "bitlocker") not in W["bitlocker_off"])
            line("bitlocker", [(pc, str(val(pc, "bitlocker"))) for pc in physical if val(pc, "bitlocker") in W["bitlocker_off"]],
                 len(physical), f"As the PC reports it to Aternity. {on:,} PCs in all report it on.")
        # in the list and heard from, but no application ran on it
        alive = [pc for pc in pcs if (self.age(pc["heard"], now) or 0) < SILENT_DAYS]
        if self.used is None:
            line("unused", [], 0, "", "whether a PC is in use could not be read (see the notes)")
        else:
            idle = [pc for pc in alive if pc["name"].lower() not in self.used]
            line("unused", [(pc, "virtual desktop" if is_virtual(val(pc, "kind")) else "physical PC") for pc in idle],
                 len(alive), f"In the list and not silent, but no application ran on it in {USE_DAYS} days. "
                 f"{sum(1 for pc in idle if is_virtual(val(pc, 'kind'))):,} of them are virtual desktops.")
        # the last start-up is long ago
        if not missing("restart", "boot", "the last start-up"):
            boots = [(pc, parse_time(val(pc, "boot"))) for pc in pcs]
            timed = [(pc, t) for pc, t in boots if t]
            late = [(pc, t) for pc, t in timed if (self.age(t, now) or 0) > RESTART_DAYS]
            line("restart", [(pc, f"last start-up {self.day(t)}, {self.age(t, now)} days ago") for pc, t in
                             sorted(late, key=lambda x: x[1])], len(timed),
                 "The last start-up Aternity has for the PC."
                 + (f" {len(pcs) - len(timed):,} {'PC has' if len(pcs) - len(timed) == 1 else 'PCs have'} none and "
                    f"{'is' if len(pcs) - len(timed) == 1 else 'are'} not judged." if len(pcs) > len(timed) else ""))
        # not heard from
        if not missing("silent", LAST, "when it last heard from a PC"):
            dated = [pc for pc in pcs if pc["heard"]]
            quiet = sorted((pc for pc in dated if self.age(pc["heard"], now) >= SILENT_DAYS), key=lambda pc: pc["heard"])
            line("silent", [(pc, f"last heard {self.day(pc['heard'])}, {self.age(pc['heard'], now)} days ago") for pc in quiet],
                 len(dated), "Still in Aternity's list. It seems to take a PC off the list about a month after it last "
                 "hears from it.")
        # the agent does not run
        if not missing("stopped", STATUS, "the agent's status"):
            line("stopped", [(pc, str(pc["row"].get(STATUS))) for pc in pcs if pc["row"].get(STATUS) in W["stopped"]],
                 len(pcs), "Aternity's own status of the PC's agent: " + ", ".join(f"'{w}'" for w in W["stopped"])
                 + ". It sends nothing until it runs again.")
        # Microsoft's stability index
        if not missing("stability", "stability", "Microsoft's stability index"):
            scored = [(pc, to_float(val(pc, "stability"))) for pc in pcs]
            scored = [(pc, x) for pc, x in scored if x is not None]
            line("stability", [(pc, f"{x:g}") for pc, x in sorted(scored, key=lambda y: y[1]) if x < STABILITY_LOW],
                 len(scored), f"Microsoft's own score of how stable the PC is, 1 to 10. Under {STABILITY_LOW}.")
        # little free space: Aternity's own bands
        if not missing("free", "free", "the free space"):
            banded = [(pc, str(val(pc, "free"))) for pc in pcs if val(pc, "free")]
            low = [(pc, b) for pc, b in banded if (band_top_gb(b) if band_top_gb(b) is not None else FREE_LOW_GB + 1) <= FREE_LOW_GB]
            bands = sorted({b for _, b in low}, key=lambda b: band_top_gb(b) or 0)
            line("free", sorted(low, key=lambda x: band_top_gb(x[1]) or 0), len(banded),
                 f"Aternity gives free space as bands; those that end at {FREE_LOW_GB} GB or under"
                 + (": " + ", ".join(f"'{b}'" for b in bands) + "." if bands else "."))
        # a spinning hard disk
        if not missing("spinning", "disk", "the kind of disk"):
            hdd = [(pc, str(val(pc, "disk"))) for pc in pcs if str(val(pc, "disk") or "").upper().startswith(W["spinning"])]
            line("spinning", hdd, sum(1 for pc in pcs if val(pc, "disk") and not is_virtual(val(pc, "kind"))),
                 "Aternity's kind of disk starts with '" + W["spinning"] + "'"
                 + (": " + ", ".join(f"'{k}'" for k in sorted({k for _, k in hdd})) + "." if hdd else "."))
        return out

    def day(self, t: datetime) -> str:
        return t.astimezone(self.zone).strftime("%b %d").replace(" 0", " ")

    # -- the tab ------------------------------------------------------------------------
    def view(self, now: datetime | None = None, retired: dict | None = None) -> dict:
        now = now or self.clock()
        with self.lock:
            out = {"ready": self.as_of is not None, "reading": self.reading, "status": self.status, "off": self.off,
                   "zone": self.zone_label, "every_min": READ_EVERY_MIN, "retired": retired}
            if self.as_of is None:
                return out
            pcs = list(self.pcs.values())
            judged = self._judged(now)
            kinds = Counter(str(pc["row"].get(R["kind"]) or "kind not given") for pc in pcs) if R["kind"] in self.have else {}
            ages = [self.age(pc["heard"], now) for pc in pcs]
            n = lambda lo, hi=None: sum(1 for a in ages if a is not None and a >= lo and (hi is None or a <= hi))  # noqa: E731
            dated = [pc for pc in pcs if pc["heard"]]
            oldest = min(dated, key=lambda pc: pc["heard"]) if dated else None
            lines = [{"key": k, "label": LINES[k], "text": j["text"], "n": len(j["pcs"]), "of": j["of"]}
                     for k, j in judged.items() if not j["unknown"]]
            alive = [pc for pc in pcs if (self.age(pc["heard"], now) or 0) < SILENT_DAYS]
            idle = len(judged["unused"]["pcs"])
            out.update({
                "as_of": self.as_of.isoformat(),
                "as_of_local": self.as_of.astimezone(self.zone).strftime("%b %d, %H:%M"),
                "pcs": len(pcs), "virtual": sum(1 for pc in pcs if is_virtual(pc["row"].get(R["kind"]))) if kinds else None,
                "kinds": [{"kind": k, "n": v} for k, v in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))],
                "heard": {"known": LAST in self.have, "today": n(0, 0), "week": n(1, SILENT_DAYS - 1),
                          "silent": n(SILENT_DAYS, OLD_DAYS - 1), "old": n(OLD_DAYS), "none": len(pcs) - len(dated),
                          "longest": ({"days": self.age(oldest["heard"], now), "day": self.day(oldest["heard"])}
                                      if oldest else None)},
                "statuses": ([{"status": k, "n": v} for k, v in Counter(
                    str(pc["row"].get(STATUS) or "no status") for pc in pcs).most_common()] if STATUS in self.have else []),
                "lines": sorted((x for x in lines if x["n"]), key=lambda x: (-x["n"], x["label"])),
                "quiet": [x["label"] for x in lines if not x["n"]],
                "unknown": [{"label": LINES[k], "why": j["unknown"]} for k, j in judged.items() if j["unknown"]],
                "used": ({"known": True, "of": len(alive), "used": len(alive) - idle, "unused": idle}
                         if self.used is not None else {"known": False}),
                "notes": list(self.notes),
                "rules": {"silent_days": SILENT_DAYS, "old_days": OLD_DAYS, "use_days": USE_DAYS,
                          "restart_days": RESTART_DAYS, "stability_low": STABILITY_LOW, "free_low_gb": FREE_LOW_GB,
                          "find_min": FIND_MIN},
            })
        return out

    def line_pcs(self, key: str, now: datetime | None = None) -> dict | None:
        """The PCs behind one "Needs a look" line, from the read held (no read of Aternity)."""
        now = now or self.clock()
        with self.lock:
            j = self._judged(now).get(key) if self.as_of else None
            if j is None or j["unknown"]:
                return None
            return {"what": LINES[key], "text": j["text"], "total": len(j["pcs"]), "of": j["of"],
                    "as_of_local": self.as_of.astimezone(self.zone).strftime("%b %d, %H:%M"),
                    "rows": [self._brief(pc, what) for pc, what in j["pcs"][:PCS_SHOWN]]}

    def find(self, text: str) -> dict:
        """PCs whose name, last user or serial number has the text (any case), names that start
        with it first."""
        q = str(text or "").strip().lower()
        if len(q) < FIND_MIN:
            return {"rows": [], "more": 0, "short": True}
        with self.lock:
            hits = [pc for pc in self.pcs.values() if q in pc["name"].lower()
                    or q in str(pc["row"].get(R["user"]) or "").lower() or q in str(pc["row"].get(R["serial"]) or "").lower()]
            hits.sort(key=lambda pc: (not pc["name"].lower().startswith(q), pc["name"].lower()))
            return {"rows": [self._brief(pc) for pc in hits[:FIND_MAX]], "more": max(len(hits) - FIND_MAX, 0),
                    "short": False}

    def card(self, name: str, now: datetime | None = None) -> dict | None:
        """Everything Aternity's device list holds about one PC, as it sends it, and the "Needs a
        look" lines the PC is on."""
        now = now or self.clock()
        with self.lock:
            pc = self.pcs.get(str(name or "").strip().lower())
            if pc is None:
                return None
            r = pc["row"]
            def said(value, how) -> str:  # a time in the page's time zone; everything else as Aternity sends it
                t = parse_time(value) if how == "date" else None
                return t.astimezone(self.zone).strftime("%b %d, %Y, %H:%M") if t else str(value)
            fields = [{"group": group, "label": label, "value": said(r.get(c), how)} for group, items in INVENTORY_FILLED
                      for label, c, how in items if c in self.have and r.get(c) not in (None, "")]
            boot = parse_time(r.get(R["boot"])) if R["boot"] in self.have else None
            judged = self._judged(now)
            return dict(self._brief(pc), status=r.get(STATUS) if STATUS in self.have else None,
                        heard_days=self.age(pc["heard"], now), boot_days=self.age(boot, now),
                        used=(pc["name"].lower() in self.used) if self.used is not None else None,
                        use_days=USE_DAYS, fields=fields,
                        lines=[LINES[k] for k, j in judged.items() if any(p is pc for p, _ in j["pcs"])],
                        as_of_local=self.as_of.astimezone(self.zone).strftime("%b %d, %H:%M"))


def keep_reading(inv: Inventory, make_client, cfg_of, sleep, clock=lambda: datetime.now(timezone.utc)) -> None:
    """Runs in a background thread: a read at start, then every READ_EVERY_MIN minutes; when
    Aternity does not answer, the last read stays on the tab and it is tried again after RETRY_MIN."""
    while True:
        wait = READ_EVERY_MIN * 60
        try:
            client = make_client()
            if client is None:
                inv.off = "Aternity is not set, so there is nothing to show (aternity.base_url in config.yaml)."
                log.info("Inventory: %s", inv.off)
                return
            inv.off, inv.reading, started = None, True, clock()
            inv.read(client, cfg_of(), started)
            inv.status = "Read"
            log.info("Inventory: read in %.1f s: %s PCs%s", inv.took or 0, f"{len(inv.pcs):,}",
                     f"; {len(inv.notes)} notes" if inv.notes else "")
            for n in inv.notes:
                log.info("Inventory: note: %s", n)
        except Exception as exc:  # said on the tab; the last read stays
            inv.status = (f"Aternity's device list could not be read ({short_error(exc)}); trying again in "
                          f"{RETRY_MIN} minutes.")
            log.warning("Inventory: %s", inv.status)
            wait = RETRY_MIN * 60
        finally:
            inv.reading = False
        sleep(wait)
