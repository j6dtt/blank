"""Made-up Aternity and NetIM for the dev app: the dev host has no Riverbed.

Not part of the product: the product has no demo mode, and the bundle
(scripts/make-bundle.sh) leaves dev/ out. This server answers the dev app's
own clients as a real tenant would: Aternity's OData API with a real
$metadata (tests/fixtures/aternity_metadata_v2.xml) and NetIM's REST API in
steelscript-netim's layouts. Users, PCs, applications, programs and
addresses are made up (sample01.., PC-001.., "Sample Mail", TEST-NET
addresses). Every row is worked out from its time and SEED: a read gives the
same rows every time, after a restart too. The story is set against today in
ZONE, so it is the same every day.

The story (local times, today):
- sample07 on PC-007: a failing disk (Faulty HD S.M.A.R.T. Status every day
  since 3 days ago, HD Failure today), long disk queues, slow actions.
- Sample Mail 16.1.900 -> 16.2.1100 last night at 22:30 on PC-001..PC-014;
  Application Crashes in mailcore.dll on that version today.
- Sample Branch South (PC-027..PC-036): Sample ERP slow 10:30-11:15; the
  branch's WAN link in NetIM (south-br-rtr1 Gi0/1) over 90%.
- PC-019: CPU 88-98% from 11:00 to 13:00 (sampleindexer.exe).
- sample12 on PC-012: a System Crash at 14:05, then a start-up (a driver
  pack was updated on that PC 2 days ago).
- sample23 on PC-023: Sample Chat hangs every 20 minutes, every second one
  ends in a crash (Application Crash (After Hang)).
- Virtual desktops (VD-01..VD-16, pooled; users sample37..sample50 at thin
  clients TC-37..TC-50): logons slow this morning (User Profile), launches
  right after them slow.
- Around them, every day: other crashes, background crashes, low memory,
  low disk space, hangs.

Run it with dev/compose.yaml (README.md, "Sample data for the dev app").
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import re
import sys
import threading
from collections import OrderedDict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode
from zoneinfo import ZoneInfo

# ================================ SETTINGS ==================================
ZONE = "America/New_York"   # the story's "today": set the same timezone in config.yaml
SEED = "sample-1"           # another word gives other made-up rows, the same story
ACTIVITY_DAYS = 8           # actions, app usage, processes kept (prod: about 8)
HISTORY_DAYS = 30           # health events, change log, start-ups, daily sums
SEED_DAYS = 21              # past days of saved counts written for the Today usual
NOT_PUBLISHED = ("ANOMALY_DETECTION_INCIDENTS",)  # as on prod
PAGE_ROWS = 5000            # rows per OData page, then @odata.nextLink
# ============================================================================

UTC = timezone.utc
Z = ZoneInfo(ZONE)
HOUR = timedelta(hours=1)
MIN = timedelta(minutes=1)
HERE = Path(__file__).resolve().parent
METADATA = HERE.parent / "tests" / "fixtures" / "aternity_metadata_v2.xml"
ATERNITY = "/aternity.odata/v2.0"
NETIM = "/netim"
NETIM_API = NETIM + "/api/netim/v1"
NETIM_METRICS = NETIM + "/swarm/NETIM_NETWORK_METRIC_DATA_SERVICE/api/v1/network-metric-data"

log = logging.getLogger("sample-riverbed")


# -- the made-up world ---------------------------------------------------------
HQ, NORTH, SOUTH, DC = "Sample HQ", "Sample Branch North", "Sample Branch South", "Sample Data Center"
VENDOR = "Sample Software Co."


def _pcs() -> dict:
    pcs = {}
    for i in range(1, 37):
        if i <= 16:
            site, ip = HQ, f"198.51.100.{20 + i}"
        elif i <= 26:
            site, ip = NORTH, f"192.0.2.{i - 6}"      # .11-.20 in 192.0.2.0/25
        else:
            site, ip = SOUTH, f"192.0.2.{i + 114}"    # .141-.150 in 192.0.2.128/25
        pcs[f"PC-{i:03d}"] = {"n": i, "site": site, "ip": ip, "virtual": False,
                              "os": "24H2" if i % 6 == 0 else "23H2", "disk": 476.0, "image": None}
    for k in range(1, 17):
        pcs[f"VD-{k:02d}"] = {"n": 100 + k, "site": DC, "ip": f"203.0.113.{100 + k}", "virtual": True,
                              "os": "23H2", "disk": 127.0, "image": "2611.04"}
    return pcs


PCS = _pcs()
NIGHT = ("sample15", "sample16", "sample49", "sample50")  # 20:00 to 04:00
# today, these users work whatever the day: the story needs them
FORCED = {"sample07", "sample12", "sample23", "sample38", "sample40", "sample41"} | {
    f"sample{n:02d}" for n in range(27, 37)}


def _users() -> dict:
    us = {}
    for n in range(1, 51):
        mix = {"Sample Mail": 30, "Sample Browser": 25, "Sample Chat": 15, "Sample Docs": 15, "Sample ERP": 15}
        if 27 <= n <= 36:
            mix["Sample ERP"] = 35
        if n in (5, 9, 13, 21):
            mix["Sample ERP"] = 0
        us[f"sample{n:02d}"] = {"n": n, "pc": f"PC-{n:03d}" if n <= 36 else None,
                                "client": f"TC-{n}" if n > 36 else None,
                                "rate": 9 + (n * 5) % 8, "mix": mix}
    return us


USERS = _users()
# per application: process, client / network / backend share, launch, activities (typical seconds, weight).
# Aternity's status: Minor over 2x the typical time, Major over 3.5x; UNDEFINED have no limit set.
APPS = {
    "Sample Mail": {"process": "SAMPLEMAIL.EXE", "split": (0.35, 0.25, 0.40), "launch": ("Launch", 6.0),
                    "acts": {"Open Mail": (0.8, 50), "Send Mail": (1.2, 25), "Search Mail": (2.0, 10)}},
    "Sample ERP": {"process": "sampleerp.exe", "split": (0.20, 0.30, 0.50), "launch": ("Login", 3.0),
                   "acts": {"Open Order": (2.0, 50), "Save Order": (2.5, 35), "Run Report": (8.0, 15)}},
    "Sample Browser": {"process": "samplebrowser.exe", "split": (0.40, 0.35, 0.25), "launch": ("Launch", 2.5),
                       "acts": {"Page Load": (1.5, 60), "Browse": (1.0, 40)}},
    "Sample Chat": {"process": "samplechat.exe", "split": (0.45, 0.30, 0.25), "launch": ("Launch", 5.0),
                    "acts": {"Send Message": (0.5, 70), "Open Channel": (1.0, 30)}},
    "Sample Docs": {"process": "sampledocs.exe", "split": (0.60, 0.15, 0.25), "launch": ("Launch", 3.0),
                    "acts": {"Open Document": (2.0, 60), "Save Document": (1.5, 40)}},
}
UNDEFINED = {("Sample Browser", "Browse")}
LAUNCH_SPLIT = (0.8, 0.1, 0.1)
AFTER_START = 10  # minutes after a logon that a launch counts as "right after"
MAIL_NEW = {f"PC-{i:03d}" for i in range(1, 15)}
BEHIND_BROWSER = {"PC-005", "PC-018", "PC-022", "PC-029", "PC-034"}
BEHIND_ERP = {"PC-030", "PC-035"}
MISSING_KB = {"PC-003", "PC-010", "PC-019", "PC-025", "PC-029", "PC-033", "PC-035"}
PDF_USERS = {"PC-003", "PC-008", "PC-017", "PC-025", "PC-033"}
OLD_PROGRAMS = [("Sample Toolbar", "2.2"), ("Sample Fonts Legacy", "1.1"), ("Sample Codec Pack", "16.4"),
                ("Sample Old Runtime", "1.9.7"), ("Sample Help Viewer", "3.0"), ("Sample Updater Legacy", "4.1")]

# NetIM: devices (id, name, address, type, site) and interfaces
# (id, device id, name, address, prefix, speed bps, usual load %)
NETIM_DEVICES = [
    (1, "hq-core-sw1", "198.51.100.1", "Switch", HQ),
    (2, "north-br-rtr1", "192.0.2.1", "Router", NORTH),
    (3, "south-br-rtr1", "192.0.2.129", "Router", SOUTH),
    (4, "dc-core-sw1", "203.0.113.1", "Switch", DC),
    (5, "dc-fw1", "203.0.113.2", "Firewall", DC),
    (6, "erp-srv1", "203.0.113.5", "Server", DC),
    (7, "mail-srv1", "203.0.113.6", "Server", DC),
    (8, "wan-hub-rtr1", "198.18.0.1", "Router", DC),
]
INTERFACES = [
    (101, 1, "Vlan10", "198.51.100.1", 24, 10e9, 12), (102, 1, "Gi1/0/48", "198.18.0.10", 30, 10e9, 18),
    (201, 2, "Gi0/0", "192.0.2.1", 25, 1e9, 15), (202, 2, "Gi0/1", "198.18.0.2", 30, 100e6, 35),
    (301, 3, "Gi0/0", "192.0.2.129", 25, 1e9, 15), (302, 3, "Gi0/1", "198.18.0.6", 30, 100e6, 38),
    (401, 4, "Vlan100", "203.0.113.1", 24, 10e9, 20), (501, 5, "inside", "203.0.113.2", 24, 10e9, 22),
    (502, 5, "outside", "198.18.0.14", 30, 10e9, 25), (601, 6, "eth0", "203.0.113.5", 24, 10e9, 18),
    (701, 7, "eth0", "203.0.113.6", 24, 10e9, 16), (801, 8, "Gi0/1", "198.18.0.1", 30, 100e6, 34),
    (802, 8, "Gi0/2", "198.18.0.5", 30, 100e6, 36), (803, 8, "Gi0/3", "198.18.0.9", 30, 10e9, 18),
]
SOUTH_WAN = 302
METRIC_CLASSES = [  # only names Riverbed's SDK confirms (config.py DEFAULT_NETIM_METRICS)
    {"id": "IFC_UTIL", "name": "Interface Utilization", "metrics": [
        {"id": "utilizationIn", "name": "utilizationIn", "units": "%"},
        {"id": "utilizationOut", "name": "utilizationOut", "units": "%"}]},
    {"id": "CPU_UTIL", "name": "CPU Utilization", "metrics": [{"id": "cpuUtil", "name": "cpuUtil", "units": "%"}]},
]
DEVICE_CPU = {1: 14, 2: 18, 3: 20, 4: 16, 5: 24, 6: 32, 7: 22, 8: 19}

# OData: each data source's time column, how long it is kept, what is added up
TIME_COL = {
    "BUSINESS_ACTIVITIES_RAW": "TIMEFRAME", "APPLICATIONS_RAW": "TIMEFRAME", "DEVICE_HEALTH_RAW": "TIMEFRAME",
    "WINDOWS_MACHINE_BOOTS": "TIMEFRAME", "MACHINE_BOOT_COMPONENTS": "TIMEFRAME",
    "INSTALLED_SOFTWARE_CHANGE_LOG": "INSTALLED_SW_CHANGE_TIMESTAMP", "APPLICATION_RESOURCES_HOURLY": "TIMEFRAME",
    "APPLICATIONS_DAILY": "TIMEFRAME", "BUSINESS_ACTIVITIES_DAILY": "TIMEFRAME",
}
SHORT_KEPT = ("BUSINESS_ACTIVITIES_RAW", "APPLICATIONS_RAW", "APPLICATION_RESOURCES_HOURLY")
# Aternity adds these up over the columns asked for (INSTALLED_SOFTWARE without DEVICE_NAME
# comes per name and version with DEVICE_COUNT, as on the live tenant)
ADDED_UP = ("INSTALLED_SOFTWARE", "APPLICATIONS_DAILY", "BUSINESS_ACTIVITIES_DAILY")
SUMS = ("APPLICATION_CRASHES_TOTAL", "APPLICATION_HANG_TIME_TOTAL", "ACTIVITY_VOLUME", "INSTALLED_SW_COUNT",
        "INSTALLED_SW_INSTALLATION_VOLUME")
AVERAGES = ("APPLICATION_UXI_AVG", "ACTIVITY_RESPONSE_TIME_AVG")
ONES = ("INSTALLED_SW_VERSION_COUNT",)  # 1 on every live row


# -- time and randomness ------------------------------------------------------
def _rng(*key) -> random.Random:
    """The same draws for the same key, in every run (str seeds are hashed with SHA-512)."""
    return random.Random(":".join(str(k) for k in (SEED,) + key))


def _at(d: date, hours: float) -> datetime:
    """Local clock time on day d (hours may pass 24), in UTC."""
    return (datetime.combine(d, time(0), Z) + timedelta(hours=hours)).astimezone(UTC)


def _day(t: datetime) -> date:
    return t.astimezone(Z).date()


def _clock(t: datetime) -> float:
    lt = t.astimezone(Z)
    return lt.hour + lt.minute / 60 + lt.second / 3600


def _sec(t: datetime) -> datetime:
    return t.replace(microsecond=0)


def _poisson(r: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= r.random()
        if p <= limit:
            return k
        k += 1


def _pick(r: random.Random, weights: dict):
    names = [k for k, w in weights.items() if w > 0]
    return r.choices(names, [weights[k] for k in names])[0]


class Session(NamedTuple):
    user: str
    dev: str
    day: date
    start: datetime         # the logon starts
    end: datetime
    logon: float            # seconds
    boot: float | None      # seconds, when the PC started just before
    after_crash: bool = False

    @property
    def ready(self) -> datetime:
        return self.start + timedelta(seconds=self.logon)

    @property
    def boot_start(self) -> datetime:
        return self.start - timedelta(seconds=(self.boot or 0) + 40)


# -- the schema ---------------------------------------------------------------
def _schema() -> tuple[str, dict]:
    text = METADATA.read_text()
    for name in NOT_PUBLISHED:
        text = re.sub(rf'\s*<EntitySet Name="{name}"[^>]*/>', "", text)
    types = {t: dict(re.findall(r'<Property Name="([^"]+)" Type="Edm\.(\w+)"', body))
             for t, body in re.findall(r'<EntityType Name="([^"]+)">(.*?)</EntityType>', text, re.S)}
    sets = {n: types.get(t.split(".")[-1], {})
            for n, t in re.findall(r'<EntitySet Name="([^"]+)" EntityType="([^"]+)"', text)}
    return text, sets


# -- OData $filter: comparisons joined by and / or / not, in brackets ----------
_TOKEN = re.compile(r"\s*(?:(\()|(\))|('(?:[^']|'')*')|(\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?)"
                    r"|(-?\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_]*))")
OPS = ("eq", "ne", "gt", "ge", "lt", "le")


def parse_filter(text: str):
    """("and", a, b) / ("or", a, b) / ("not", a) / ("cmp", column, op, value)."""
    toks, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(f"The $filter could not be read at: {text[pos:pos + 30]!r}")
        pos = m.end()
        lp, rp, s, dt, num, word = m.groups()
        if lp or rp:
            toks.append(("(",) if lp else (")",))
        elif s is not None:
            toks.append(("v", s[1:-1].replace("''", "'")))
        elif dt:
            toks.append(("v", datetime.fromisoformat(dt.replace("Z", "+00:00"))))
        elif num:
            toks.append(("v", float(num)))
        elif word.lower() in ("and", "or", "not") or word in OPS:
            toks.append((word.lower(),))
        elif word in ("null", "true", "false"):
            toks.append(("v", {"null": None, "true": True, "false": False}[word]))
        else:
            toks.append(("id", word))
    toks.append(("end",))
    i = 0

    def peek():
        return toks[i][0]

    def take(kind):
        nonlocal i
        if toks[i][0] != kind:
            raise ValueError(f"The $filter is not valid: expected {kind}, found {toks[i][-1]!r}")
        i += 1
        return toks[i - 1]

    def expr():
        node = conj()
        while peek() == "or":
            take("or")
            node = ("or", node, conj())
        return node

    def conj():
        node = unary()
        while peek() == "and":
            take("and")
            node = ("and", node, unary())
        return node

    def unary():
        if peek() == "not":
            take("not")
            return ("not", unary())
        if peek() == "(":
            take("(")
            node = expr()
            take(")")
            return node
        col = take("id")[1]
        op = toks[i][0]
        if op not in OPS:
            raise ValueError(f"The $filter is not valid: {col} has no comparison")
        take(op)
        return ("cmp", col, op, take("v")[1])

    node = expr()
    take("end")
    return node


def _columns(node) -> set:
    if node[0] == "cmp":
        return {node[1]}
    return set().union(*(_columns(n) for n in node[1:]))


def _match(node, row) -> bool:
    kind = node[0]
    if kind == "and":
        return _match(node[1], row) and _match(node[2], row)
    if kind == "or":
        return _match(node[1], row) or _match(node[2], row)
    if kind == "not":
        return not _match(node[1], row)
    _, col, op, want = node
    have = row.get(col)
    if want is None or have is None:
        return (have is want) if op == "eq" else (have is not want) if op == "ne" else False
    if isinstance(want, bool) or isinstance(have, str) != isinstance(want, str):
        have, want = str(have).lower(), str(want).lower()
    try:
        return {"eq": have == want, "ne": have != want, "gt": have > want, "ge": have >= want,
                "lt": have < want, "le": have <= want}[op]
    except TypeError:
        return False


def _conjuncts(node) -> list:
    return _conjuncts(node[1]) + _conjuncts(node[2]) if node[0] == "and" else [node]


def _span_of(node, col: str) -> tuple[datetime | None, datetime | None]:
    """The time range a filter asks for (its top-level and-ed comparisons on the time column)."""
    lo = hi = None
    for c in _conjuncts(node) if node else []:
        if c[0] == "cmp" and c[1] == col and isinstance(c[3], datetime):
            if c[2] in ("ge", "gt", "eq"):
                lo = max(lo, c[3]) if lo else c[3]
            if c[2] in ("le", "lt", "eq"):
                hi = min(hi, c[3]) if hi else c[3]
    return lo, hi


class _Cache:
    def __init__(self, size: int):
        self.size, self.items, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key, make):
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        value = make()
        with self.lock:
            self.items[key] = value
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return value


# -- the server -----------------------------------------------------------------
class Sample:
    def __init__(self, clock=lambda: datetime.now(UTC)):
        self.clock = clock
        self.metadata, self.types = _schema()
        self.cache = _Cache(400)
        self.dropped: dict[str, set] = {}  # columns made here that the $metadata lacks (a test keeps it empty)

    @property
    def today(self) -> date:
        return _day(self.clock())

    # -- who works when -------------------------------------------------------
    def vd_of(self, user: str, d: date) -> str:
        """Pooled desktops: each day a user gets another one, never two users at once."""
        return f"VD-{(USERS[user]['n'] - 37 + d.toordinal() * 5) % 16 + 1:02d}"

    def sessions(self, user: str, d: date) -> list[Session]:
        T = self.today
        return self.cache.get(("sessions", user, d, T), lambda: self._sessions(user, d, T))

    def _sessions(self, user: str, d: date, T: date) -> list[Session]:
        u, r = USERS[user], _rng("shift", user, d)
        working, forced = d.weekday() < 5, d == T and user in FORCED
        if user in NIGHT:
            works, begin, length = r.random() < (0.9 if working else 0.75), 20 + r.random() / 2, 8 + r.random() / 2
        elif working or forced:
            works, begin, length = r.random() < 0.94 or forced, 6.5 + 2 * r.random(), 8.5 + 1.5 * r.random()
        else:
            works, begin, length = r.random() < 0.35, 8 + 2 * r.random(), 5 + 2 * r.random()
        if not works:
            return []
        dev = u["pc"] or self.vd_of(user, d)
        pc = PCS[dev]
        start, end = _at(d, begin), _at(d, begin + length)
        r = _rng("logon", user, d)
        if pc["virtual"]:
            boot = r.gauss(40, 4)
            logon = r.uniform(70, 95) if d == T and begin < 9.75 else r.gauss(29, 3)
        else:
            boot = r.gauss(48, 5) if r.random() < 0.55 else None
            if dev == "PC-007" and T - timedelta(days=3) <= d <= T:
                boot = r.uniform(105, 130)  # the failing disk
            logon = r.gauss(20 + pc["n"] % 7, 2.5)
        out = [Session(user, dev, d, start, end, max(logon, 8), max(boot, 20) if boot else None)]
        crash = _at(T, 14 + 5 / 60)
        if d == T and dev == "PC-012" and start < crash < end:  # the System Crash, then a start-up
            back = crash + timedelta(minutes=3, seconds=40)
            out = [out[0]._replace(end=crash),
                   Session(user, dev, d, back, end, r.gauss(27, 2), r.uniform(58, 66), True)]
        return out

    def _near(self, user: str, t: datetime) -> list[Session]:
        d = _day(t)
        return [s for dd in (d - timedelta(days=1), d) for s in self.sessions(user, dd)]

    # -- versions --------------------------------------------------------------
    def mail_update(self, dev: str) -> datetime:
        return _at(self.today - timedelta(days=1), 22.5) + timedelta(seconds=7 * PCS[dev]["n"])

    def browser_update(self, dev: str) -> datetime:
        pc = PCS[dev]
        return self.vd_batch() if pc["virtual"] else _at(self.today - timedelta(days=6), 10) + pc["n"] * MIN

    def chat_update(self, dev: str) -> datetime:
        pc = PCS[dev]
        return self.vd_batch() if pc["virtual"] else _at(self.today - timedelta(days=10), 11) + pc["n"] * MIN

    def vd_batch(self) -> datetime:
        return _at(self.today - timedelta(days=9), 3)

    def driver_update(self, dev: str) -> datetime:
        if dev == "PC-012":
            return _at(self.today - timedelta(days=2), 14.5)
        return _at(self.today - timedelta(days=40), 9) + PCS[dev]["n"] * MIN

    def version(self, dev: str, app: str, t: datetime) -> str:
        if app == "Sample Mail":
            return "16.2.1100" if dev in MAIL_NEW and t >= self.mail_update(dev) else "16.1.900"
        if app == "Sample Browser":
            return "130.0.4" if dev in BEHIND_BROWSER or t < self.browser_update(dev) else "131.0.2"
        if app == "Sample Chat":
            return "4.12.0" if t >= self.chat_update(dev) else "4.11.5"
        if app == "Sample ERP":
            return "8.3.9" if dev in BEHIND_ERP else "8.4.2"
        return "2025.2.1"

    # -- actions (BUSINESS_ACTIVITIES_RAW) ---------------------------------------
    def actions(self, h: datetime) -> list[dict]:
        """Every action of one hour (h: its start, UTC), as sent once all have arrived."""
        T = self.today
        return self.cache.get(("act", h, T), lambda: self._actions(h, T))

    def _actions(self, h: datetime, T: date) -> list[dict]:
        rows = []
        for user, u in USERS.items():
            for s in self._near(user, h):
                a, b = max(s.ready, h), min(s.end, h + HOUR)
                if a >= b:
                    continue
                r = _rng("act", user, s.start.timestamp(), h.timestamp())
                if h <= s.ready < h + HOUR:  # the first launches after a logon
                    top = sorted((w, app) for app, w in u["mix"].items() if w)[-3:]
                    for _, app in top:
                        t = s.ready + timedelta(minutes=r.uniform(0.5, 6))
                        if t < b:
                            rows.append(self._action(r, s, t, app, APPS[app]["launch"][0], True, T))
                rate = u["rate"] * (0.6 if user in NIGHT else 1 if s.day.weekday() < 5 else 0.7)
                for _ in range(_poisson(r, rate * (b - a) / HOUR)):
                    app = _pick(r, u["mix"])
                    act = _pick(r, {k: v[1] for k, v in APPS[app]["acts"].items()})
                    rows.append(self._action(r, s, a + (b - a) * r.random(), app, act, False, T))
        rows.sort(key=lambda x: (x["TIMEFRAME"], x["DEVICE_NAME"]))
        return rows

    def _action(self, r, s: Session, t: datetime, app: str, act: str, launch: bool, T: date) -> dict:
        pc, d, c = PCS[s.dev], _day(t), _clock(t)
        spec = APPS[app]
        typical = spec["launch"][1] if launch else spec["acts"][act][0]
        split = LAUNCH_SPLIT if launch else spec["split"]
        m = [1.0, 1.0, 1.0]  # client, network, backend
        if launch and (t - s.ready) <= AFTER_START * MIN:  # right after a logon: virtual desktops most
            m[0] *= 1.8 if pc["virtual"] else 1.3
            if pc["virtual"] and d == T and c < 10:
                m[0] *= 2.8
        if d == T and pc["site"] == SOUTH and 10.5 <= c < 11.25:  # the branch's WAN link is full
            m[1] *= 12 if app == "Sample ERP" else 3
        if d == T and s.dev == "PC-019" and 11 <= c < 13:  # sampleindexer.exe takes the CPU
            m[0] *= 6
        if s.dev == "PC-007" and T - timedelta(days=3) <= d <= T:  # the failing disk
            m[0] *= 4.5 if d == T else 2
        if launch and app == "Sample Mail" and self.version(s.dev, app, t) == "16.2.1100":
            m[0] *= 1.4
        noise = r.lognormvariate(0, 0.25) * (r.uniform(1.5, 4.5) if r.random() < 0.04 else 1)
        parts = [typical * split[k] * m[k] * noise for k in range(3)]
        rt = sum(parts)
        status = ("Undefined" if (app, act) in UNDEFINED else "Major" if rt > 3.5 * typical
                  else "Minor" if rt > 2 * typical else "Normal")
        cpu = 12 + pc["n"] * 7 % 20 + r.uniform(-5, 10) + (20 if launch else 0)
        if d == T and s.dev == "PC-019" and 11 <= c < 13:
            cpu = r.uniform(88, 98)
        queue = r.uniform(0.05, 0.8)
        if s.dev == "PC-007" and T - timedelta(days=3) <= d <= T:
            queue = r.uniform(3, 6) if d == T else r.uniform(1.5, 3)
        rtt = {HQ: (2, 6), NORTH: (28, 40), SOUTH: (30, 45), DC: (1, 3)}[pc["site"]]
        if d == T and pc["site"] == SOUTH and 10.5 <= c < 11.25:
            rtt = (120, 180)
        u = USERS[s.user]
        return {
            "TIMEFRAME": _sec(t), "USERNAME": s.user, "DEVICE_NAME": s.dev, "APPLICATION_NAME": app,
            "ACTIVITY_NAME": act, "ACTIVITY_RESPONSE_TIME": round(rt, 3), "ACTIVITY_CLIENT_TIME": round(parts[0], 3),
            "ACTIVITY_NETWORK_TIME": round(parts[1], 3), "ACTIVITY_BACKEND_TIME": round(parts[2], 3),
            "SLA_STATUS": status, "HRC_CPU_UTIL": round(min(cpu, 100), 1),
            "HRC_PHYSICAL_MEMORY_UTIL": round((62 if pc["virtual"] else 45) + pc["n"] * 5 % 18 + r.uniform(-4, 6), 1),
            "HRC_DISK_QUEUE_LENGTH": round(queue, 2), "NETWORK_RTT_AVG": round(r.uniform(*rtt), 1),
            "DEVICE_IP_ADDRESS": pc["ip"], "BUSINESS_LOCATION": pc["site"], "OS_NAME": "Windows 11",
            "VIRTUALIZATION": "true" if pc["virtual"] else "false",
            "CLIENT_DEVICE_NAME": u["client"], "CLIENT_DEVICE_TYPE": "Thin Client" if u["client"] else None,
            "APPLICATION_VERSION": self.version(s.dev, app, t), "DEVICE_IMAGE_BUILD_NUMBER": pc["image"],
            "_at": t + timedelta(minutes=r.uniform(2, 6)),
        }

    # -- crashes, hangs and other health events, per local day ---------------------
    def incidents(self, d: date) -> list[dict]:
        T = self.today
        return self.cache.get(("inc", d, T), lambda: self._incidents(d, T))

    def _incidents(self, d: date, T: date) -> list[dict]:
        r = _rng("incidents", d)
        out: list[dict] = []
        working = [s for user in USERS for s in self.sessions(user, d)]

        def moment(rr, pool=None):
            pool = [s for s in (pool if pool is not None else working) if s.end > s.ready]
            if not pool:
                return None, None
            s = rr.choice(pool)
            return s, s.ready + (s.end - s.ready) * rr.random()

        def add(s, t, name, sev, cat, comp=None, cver=None, app=None, sub=None, subver=None, crash=0, hang=0.0,
                event=True, volume=1, exc=None, error=None, details=None, dev=None, late=True, rr=r):
            if t is None:
                return
            lag = rr.uniform(60, 180) if late and rr.random() < 0.05 else rr.uniform(2, 12)
            out.append({"t": _sec(t), "dev": dev or s.dev, "user": s.user if s else None, "app": app, "name": name,
                        "sev": sev, "cat": cat, "comp": comp, "cver": cver, "sub": sub, "subver": subver,
                        "exc": exc, "error": error, "details": details, "volume": volume, "crash": crash,
                        "hang": hang, "event": event, "at": t + timedelta(minutes=lag)})

        def proc(app, dev, t):
            return f"{APPS[app]['process']} (Process)", f"{self.version(dev, app, t)} (Process Version)"

        # every day
        for _ in range(r.choice([1, 2, 2, 3, 4])):
            app = _pick(r, {"Sample Docs": 4, "Sample Browser": 3, "Sample ERP": 2, "Sample Chat": 1})
            s, t = moment(r)
            if s:
                comp, cver = proc(app, s.dev, t)
                sub = r.choice(["unknown (Dll)", "samplerender.dll (Dll)"])
                add(s, t, "Application Crash", "Major", "Application", comp, cver, app, sub,
                    "0.0.0.0 (DLL Version)" if sub.startswith("unknown") else "3.4.1 (DLL Version)",
                    crash=1, exc="0xc0000005")
        s, t = moment(r, [x for x in working if x.dev not in MAIL_NEW or x.start < self.mail_update(x.dev)])
        if s and r.random() < 0.5:  # Sample Mail 16.1.900 crashes now and then, elsewhere
            comp, cver = proc("Sample Mail", s.dev, t)
            add(s, t, "Application Crash", "Major", "Application", comp, cver, "Sample Mail", "unknown (Dll)",
                "0.0.0.0 (DLL Version)", crash=1, exc="0xc0000409")
        for _ in range(r.randint(2, 5)):
            s, t = moment(r)
            add(s, t, "Background Process Crash", "Minor", "Background Process", "sampleagent.exe (Process)",
                "7.2.0 (Process Version)", exc="0xc0000005")
        for _ in range(r.randint(2, 4)):
            s, t = moment(r, [x for x in working if PCS[x.dev]["virtual"]] or None)
            add(s, t, "Low Memory Pagefaults", "Minor", "System", volume=r.randint(2, 6))
        for dev in ("PC-031", "PC-033"):
            s, t = moment(r, [x for x in working if x.dev == dev])
            if r.random() < 0.7:
                add(s, t, "Low Disk Space", "Minor", "System", "C: (Drive)", details="Free space under 10%")
        if r.random() < 0.6:
            s, t = moment(r)
            if s:
                comp, cver = proc("Sample Browser", s.dev, t)
                add(s, t, "Application Crash (After Hang)", "Major", "Application", comp, cver, "Sample Browser",
                    "unknown (Dll)", "0.0.0.0 (DLL Version)", crash=1)
        if r.random() < 0.4:
            s, t = moment(r)
            add(s, t, "Application Crash", "Major", "Application", "bad_module_info (Process)",
                "0.0.0.0 (Process Version)", exc="0xc0000005")
        if r.random() < 0.3:
            s, t = moment(r)
            add(s, t, "Unexpected Shutdown", "Minor", "System")
        for _ in range(r.randint(2, 5)):  # hangs that ended by themselves: only in app usage
            app = _pick(r, {"Sample Mail": 3, "Sample Docs": 2, "Sample Browser": 2})
            s, t = moment(r)
            add(s, t, "hang", None, None, app=app, hang=round(r.uniform(5, 30)), event=False)

        # the story
        if T - timedelta(days=3) <= d <= T:  # PC-007's disk is failing
            rr = _rng("disk", d)
            mine = [x for x in working if x.dev == "PC-007"]
            s = mine[0] if mine else None
            t = s.ready + 12 * MIN if s else _at(d, 9 + rr.random())
            add(s, t, "Faulty HD S.M.A.R.T. Status", "Major", "Hardware", "SAMPLE SSD 512GB (Disk)",
                details="Index: 0", dev="PC-007", late=False, rr=rr)
            if d == T and s:
                for after in (55, 215):
                    add(s, s.ready + after * MIN, "HD Failure", "Critical", "Hardware",
                        "\\Device\\Harddisk0\\DR0 (Drive)", details="The device has a bad block.", dev="PC-007",
                        late=False, rr=rr)
        if d == T:
            rr = _rng("story", d)
            for s in [x for x in working if x.dev in MAIL_NEW]:  # Sample Mail 16.2.1100 crashes
                for _ in range(rr.choice([0, 1, 1, 2, 3])):
                    t = s.ready + (s.end - s.ready) * rr.random()
                    comp, cver = proc("Sample Mail", s.dev, t)
                    add(s, t, "Application Crash", "Major", "Application", comp, cver, "Sample Mail",
                        "mailcore.dll (Dll)", "16.2.1100 (DLL Version)", crash=1, exc="0xc0000005", rr=rr)
            crash = [x for x in working if x.dev == "PC-012" and not x.after_crash]
            if crash:  # PC-012's System Crash
                add(crash[0], crash[0].end, "System Crash", "Critical", "System", "samplefilter.sys (Driver)",
                    "5.4 (Driver Version)", error="0x0000003B", details="SYSTEM_SERVICE_EXCEPTION", late=False, rr=rr)
            for s in [x for x in working if x.user == "sample23"]:  # Sample Chat hangs, every second one crashes
                t, k = s.ready + 60 * MIN, 0
                while t < s.end:
                    add(s, t, "hang", None, None, app="Sample Chat", hang=round(rr.uniform(15, 45)), event=False,
                        late=False, rr=rr)
                    if k % 2:
                        comp, cver = proc("Sample Chat", s.dev, t)
                        add(s, t + MIN, "Application Crash (After Hang)", "Major", "Application", comp, cver,
                            "Sample Chat", "unknown (Dll)", "0.0.0.0 (DLL Version)", crash=1, late=False, rr=rr)
                    t, k = t + 20 * MIN, k + 1
        for x in out:  # the story's events arrive within minutes
            if x["at"] - x["t"] > 15 * MIN and x["name"] in ("HD Failure", "System Crash"):
                x["at"] = x["t"] + 5 * MIN
        return out

    def health(self, x: dict) -> dict:
        pc = PCS[x["dev"]]
        return {"TIMEFRAME": x["t"], "DEVICE_NAME": x["dev"], "USERNAME": x["user"], "APPLICATION_NAME": x["app"],
                "HEALTH_EVENT_CATEGORY": x["cat"], "HEALTH_EVENT_NAME": x["name"], "HEALTH_EVENT_COMPONENT": x["comp"],
                "HEALTH_EVENT_SEVERITY": x["sev"], "HEALTH_EVENT_VOLUME": x["volume"],
                "HEALTH_EVENT_COMPONENT_VERSION": x["cver"], "HEALTH_EVENT_SUB_COMPONENT": x["sub"],
                "HEALTH_EVENT_SUB_COMPONENT_VERSION": x["subver"], "HEALTH_EVENT_EXCEPTION_TYPE": x["exc"],
                "HEALTH_EVENT_ERROR": x["error"], "HEALTH_EVENT_DETAILS": x["details"],
                "BUSINESS_LOCATION": pc["site"], "DEVICE_IP_ADDRESS": pc["ip"], "_at": x["at"]}

    # -- app usage (APPLICATIONS_RAW), processes, start-ups -------------------------
    def usage(self, h: datetime) -> list[dict]:
        T = self.today
        return self.cache.get(("use", h, T), lambda: self._usage(h, T))

    def _usage(self, h: datetime, T: date) -> list[dict]:
        per: dict = {}
        for a in self.actions(h):
            x = per.setdefault((a["DEVICE_NAME"], a["APPLICATION_NAME"]), [a, 0, 0])
            x[1] += 1
            x[2] += a["SLA_STATUS"] == "Major"
        rows = []
        for (dev, app), (a, n, major) in sorted(per.items()):
            r = _rng("uxi", dev, app, h.timestamp())
            rows.append(self._use(dev, app, a["USERNAME"], h, round(r.uniform(7.8, 9.6) - 3 * major / n, 1), 0, 0,
                                  h + HOUR + 10 * MIN))
        for x in self.incidents(_day(h)) + self.incidents(_day(h) - timedelta(days=1)):
            if x["app"] and (x["crash"] or x["hang"]) and h <= x["t"] < h + HOUR:
                t = x["t"].replace(minute=x["t"].minute // 5 * 5, second=0)
                rows.append(self._use(x["dev"], x["app"], x["user"], t, None, x["crash"], x["hang"], x["at"]))
        return rows

    def _use(self, dev, app, user, t, uxi, crashes, hang, arrive) -> dict:
        pc = PCS[dev]
        return {"TIMEFRAME": t, "DEVICE_NAME": dev, "USERNAME": user, "APPLICATION_NAME": app,
                "APPLICATION_VERSION": self.version(dev, app, t), "APPLICATION_UXI_AVG": uxi,
                "APPLICATION_CRASHES_TOTAL": crashes, "APPLICATION_HANG_TIME_TOTAL": hang,
                "VIRTUALIZATION": "true" if pc["virtual"] else "false", "BUSINESS_LOCATION": pc["site"],
                "_at": arrive}

    def processes(self, h: datetime) -> list[dict]:
        T = self.today
        return self.cache.get(("proc", h, T), lambda: self._processes(h, T))

    def _processes(self, h: datetime, T: date) -> list[dict]:
        used: dict[str, set] = {}
        for a in self.actions(h):
            used.setdefault(a["DEVICE_NAME"], set()).add(a["APPLICATION_NAME"])
        rows = []
        for dev, apps in sorted(used.items()):
            r = _rng("proc", dev, h.timestamp())
            t = h + timedelta(minutes=4 + PCS[dev]["n"] * 7 % 50)
            c = _clock(h)
            names = [(APPS[a]["process"], a, r.uniform(2, 22), r.uniform(150, 900)) for a in sorted(apps)]
            hot = _day(h) == T and dev == "PC-019" and 11 <= c < 13
            names += [("sampleagent.exe", None, r.uniform(0.5, 4), r.uniform(60, 140)),
                      ("sampleindexer.exe", None, r.uniform(62, 78) if hot else r.uniform(0.2, 3),
                       1100 if hot else r.uniform(40, 120))]
            for p, app, cpu, mb in names:
                rows.append({"TIMEFRAME": t, "DEVICE_NAME": dev, "PROCESS_NAME": p, "APPLICATION_NAME": app,
                             "PRC_CPU_UTIL_MAX": round(cpu, 1), "PRC_PHYSICAL_MEMORY_UTIL_MAX": round(mb * 2 ** 20),
                             "_at": h + HOUR + 10 * MIN})
        return rows

    def startups(self, d: date) -> tuple[list[dict], list[dict]]:
        """Start-ups (WINDOWS_MACHINE_BOOTS) and what each spent its time on (MACHINE_BOOT_COMPONENTS)."""
        T = self.today
        return self.cache.get(("boot", d, T), lambda: self._startups(d, T))

    def _startups(self, d: date, T: date) -> tuple[list[dict], list[dict]]:
        boots, parts = [], []
        for user in USERS:
            for s in self.sessions(user, d):
                pc, r = PCS[s.dev], _rng("parts", user, s.start.timestamp())
                runs = []
                if s.boot:
                    runs.append(("Boot", None, s.boot_start, s.boot))
                runs.append(("User Logon", user, s.start, s.logon))
                for kind, who, start, dur in runs:
                    end = _sec(start + timedelta(seconds=dur))
                    boots.append({"TIMEFRAME": end, "MEASUREMENT_START_TIMESTAMP": _sec(start), "USERNAME": who,
                                  "DEVICE_NAME": s.dev, "BUSINESS_LOCATION": pc["site"], "DEVICE_IP_ADDRESS": pc["ip"],
                                  "MEASUREMENT_TYPE": kind, "MEASUREMENT_DURATION_AVG": round(dur, 1),
                                  "DEVICE_IMAGE_BUILD_NUMBER": pc["image"], "_at": end + 5 * MIN})
                    if kind == "Boot":
                        usual = 48 if not pc["virtual"] else 40
                        shares = {"Drivers": 0.25, "Windows Services": 0.45, "System Session": 0.30}
                        slow = "Windows Services"
                        ptype = "Boot Process"
                    else:
                        usual = 29 if pc["virtual"] else 20 + pc["n"] % 7
                        shares = {"Group Policy": 0.25, "User Profile": 0.30, "Shell Start": 0.25,
                                  "Startup Apps": 0.20}
                        slow = "User Profile"
                        ptype = "Logon Process"
                    extra = max(0.0, dur - usual * 1.3)  # what took longer than usual, all in one part
                    for name, share in shares.items():
                        own = (dur - extra) * share + (extra if name == slow else 0)
                        # degradation: none within its normal time, now and then a little
                        late = extra if name == slow else r.uniform(0.3, 1.5) if r.random() < 0.08 else 0.0
                        parts.append({"TIMEFRAME": end, "DEVICE_NAME": s.dev, "USERNAME": who,
                                      "BUSINESS_LOCATION": pc["site"], "MEASUREMENT_TYPE": ptype,
                                      "MEASUREMENT_COMPONENT": name, "MEASUREMENT_DURATION_AVG": round(own, 1),
                                      "MEASUREMENT_DEGRADATION_TIME": round(late, 1), "_at": end + 5 * MIN})
        return boots, parts

    # -- software ----------------------------------------------------------------
    def installed(self) -> list[dict]:
        T = self.today
        return self.cache.get(("sw", T), self._installed)

    def _installed(self) -> list[dict]:
        now, rows = self.clock(), []
        long_ago = _at(self.today - timedelta(days=60), 10)
        for dev, pc in PCS.items():
            progs = [("Sample Mail", self.version(dev, "Sample Mail", now),
                      self.mail_update(dev) if dev in MAIL_NEW else long_ago),
                     ("Sample Browser", self.version(dev, "Sample Browser", now),
                      long_ago if dev in BEHIND_BROWSER else self.browser_update(dev)),
                     ("Sample Chat", "4.12.0", self.chat_update(dev)),
                     ("Sample ERP", self.version(dev, "Sample ERP", now), long_ago),
                     ("Sample Docs", "2025.2.1", long_ago), ("Sample Browser Update", "1.3.0", long_ago),
                     ("Sample Agent", "7.2.0", long_ago), ("Sample Indexer", "3.1.4", long_ago),
                     ("Sample Runtime", "2.1.0", self.vd_batch() if pc["virtual"] else long_ago),
                     ("Sample C++ Library", "14.3", long_ago), ("Sample Fonts", "1.8", long_ago),
                     ("Sample Remote Help", "9.0.2", long_ago), ("Sample Zip", "24.08", long_ago)]
            if pc["virtual"]:
                progs.append(("Sample VDI Agent", "2311.1", long_ago))
            else:
                progs += [("Sample VPN", "5.0.3", long_ago), ("Sample Driver Pack", "5.4", self.driver_update(dev))]
            if dev == "PC-031":  # the old version left behind
                progs.append(("Sample Chat", "4.11.5", long_ago))
            for name, version, changed in progs:
                rows.append(self._sw(dev, name, version, "Application", "ALL", None, changed))
            if dev in PDF_USERS:
                rows.append(self._sw(dev, "Sample PDF Reader", "24.1", "Application", "Specific User", None, long_ago))
            if dev == "PC-024":
                rows.append(self._sw(dev, "Sample Stats Pro", "3.2", "Application", "Specific User", None, long_ago))
            kbs = [f"KB90001{k:02d}" for k in range(1, 6)] if pc["os"] == "23H2" else \
                [f"KB90002{k:02d}" for k in range(1, 4)]
            for kb in kbs:
                if kb == "KB9000105" and dev in MISSING_KB:
                    continue
                at = _at(self.today - timedelta(days=3), 2) if kb == "KB9000105" else long_ago
                rows.append(self._sw(dev, kb, None, "OS Update", "ALL", "Microsoft Windows", at, "Microsoft Corporation"))
        return rows

    def _sw(self, dev, name, version, kind, scope, related, changed, vendor=VENDOR) -> dict:
        return {"DEVICE_NAME": dev, "INSTALLED_SW_NAME": name, "INSTALLED_SW_VERSION": version,
                "INSTALLED_SW_VENDOR": vendor, "INSTALLED_SW_TYPE": kind, "INSTALLED_SW_SCOPE": scope,
                "INSTALLED_SW_RELATED_TO": related, "INSTALLED_SW_INSTALLATION_VOLUME": 1,
                "INSTALLED_SW_LAST_CHANGE_TIMESTAMP": _sec(changed), "INSTALLED_SW_COUNT": 1,
                "INSTALLED_SW_VERSION_COUNT": 1, "DEVICE_COUNT": 1}

    def changes(self) -> list[dict]:
        T = self.today
        return self.cache.get(("log", T), self._changes)

    def _changes(self) -> list[dict]:
        rows = []
        T = self.today

        def row(dev, t, name, change, version, before=None, vendor=VENDOR):
            rows.append({"INSTALLED_SW_CHANGE_TIMESTAMP": _sec(t), "DEVICE_NAME": dev, "INSTALLED_SW_NAME": name,
                         "INSTALLED_SW_CHANGE_TYPE": change, "INSTALLED_SW_VERSION": version,
                         "INSTALLED_SW_VERSION_BEFORE_CHANGE": before, "INSTALLED_SW_VENDOR": vendor,
                         "_at": t + 10 * MIN})

        for dev, pc in PCS.items():
            if dev in MAIL_NEW:  # an update: two rows at the same second
                t = self.mail_update(dev)
                row(dev, t, "Sample Mail", "Uninstalled", "16.1.900")
                row(dev, t, "Sample Mail", "Installed", "16.2.1100")
            if pc["virtual"]:  # the desktop image: many products in one minute
                t = self.vd_batch()
                for k, (name, old, new) in enumerate([("Sample Browser", "130.0.4", "131.0.2"),
                                                      ("Sample Chat", "4.11.5", "4.12.0"),
                                                      ("Sample Runtime", "2.0.0", "2.1.0")]):
                    row(dev, t + timedelta(seconds=k * 9), name, "Uninstalled", old)
                    row(dev, t + timedelta(seconds=k * 9), name, "Installed", new)
                continue
            if dev not in BEHIND_BROWSER:  # Sample Browser: the two rows 2 minutes apart
                t = self.browser_update(dev)
                row(dev, t, "Sample Browser", "Uninstalled", "130.0.4")
                row(dev, t + 2 * MIN, "Sample Browser", "Installed", "131.0.2")
            row(dev, self.chat_update(dev), "Sample Chat", "Updated", "4.12.0", "4.11.5")
            if dev == "PC-031":
                row(dev, self.chat_update(dev) - 3 * MIN, "Sample Chat", "Installed", "4.12.0")
            if dev == "PC-012":
                row(dev, self.driver_update(dev), "Sample Driver Pack", "Updated", "5.4", "5.3")
            if pc["os"] == "23H2" and dev not in MISSING_KB:
                row(dev, _at(T - timedelta(days=3), 2) + pc["n"] * MIN, "KB9000105", "Installed", None,
                    vendor="Microsoft Corporation")
            if dev == "PC-020":  # 6 removals in one minute, nothing put in their place
                t = _at(T - timedelta(days=4), 15.2)
                for k, (name, version) in enumerate(OLD_PROGRAMS):
                    row(dev, t + timedelta(seconds=4 * k), name, "Uninstalled", version)
        return rows

    def inventory(self) -> list[dict]:
        now, rows = self.clock(), []
        for dev, pc in PCS.items():
            kb = pc["os"] == "23H2" and dev not in MISSING_KB and not pc["virtual"]
            rows.append({"DEVICE_NAME": dev, "DEVICE_IP_ADDRESS": pc["ip"], "BUSINESS_LOCATION": pc["site"],
                         "OS_NAME": "Windows 11", "OS_VERSION": pc["os"],
                         "OS_VERSION_UPDATE_DATE": _at(self.today - timedelta(days=3 if kb else 33), 2),
                         "OS_TOTAL_DISK_SPACE": pc["disk"], "SCCM_CLIENT_VERSION": "5.00.9128.1000",
                         "DEVICE_IMAGE_BUILD_NUMBER": pc["image"],
                         "TIME_LAST_REPORTING": _sec(now - timedelta(minutes=pc["n"] % 9))})
        return rows

    # -- daily sums (APPLICATIONS_DAILY, BUSINESS_ACTIVITIES_DAILY) -------------
    def daily(self, d: date, which: str) -> list[dict]:
        T = self.today
        if d >= T:
            return self._daily(d, which)
        return self.cache.get(("daily", d, which, T), lambda: self._daily(d, which))

    def _daily(self, d: date, which: str) -> list[dict]:
        start, now = _at(d, 0), self.clock()
        hours = [start + HOUR * k for k in range(25) if start + HOUR * k < min(_at(d + timedelta(days=1), 0), now)]
        per: dict = {}
        for h in hours:
            for a in self.actions(h):
                if a["TIMEFRAME"] > now:
                    continue
                if which == "usage":
                    key = (a["DEVICE_NAME"], a["APPLICATION_NAME"])
                else:
                    key = (a["DEVICE_NAME"], a["APPLICATION_NAME"], a["ACTIVITY_NAME"], a["SLA_STATUS"])
                x = per.setdefault(key, {"user": a["USERNAME"], "n": 0, "rt": 0.0, "crash": 0, "hang": 0.0,
                                         "version": a["APPLICATION_VERSION"]})
                x["n"] += 1
                x["rt"] += a["ACTIVITY_RESPONSE_TIME"]
        rows = []
        if which == "usage":
            for x in self.incidents(d):
                if x["app"] and (x["crash"] or x["hang"]) and x["t"] <= now:
                    y = per.setdefault((x["dev"], x["app"]), {"user": x["user"], "n": 0, "rt": 0.0, "crash": 0,
                                                              "hang": 0.0, "version": None})
                    y["crash"] += x["crash"]
                    y["hang"] += x["hang"]
                    y["version"] = self.version(x["dev"], x["app"], x["t"])
        for key, x in sorted(per.items()):
            dev, app = key[0], key[1]
            pc = PCS[dev]
            row = {"TIMEFRAME": start, "DEVICE_NAME": dev, "USERNAME": x["user"], "APPLICATION_NAME": app,
                   "APPLICATION_VERSION": x["version"] or self.version(dev, app, start + 12 * HOUR),
                   "VIRTUALIZATION": "true" if pc["virtual"] else "false", "BUSINESS_LOCATION": pc["site"]}
            if which == "usage":
                row.update({"APPLICATION_CRASHES_TOTAL": x["crash"], "APPLICATION_HANG_TIME_TOTAL": x["hang"],
                            "APPLICATION_UXI_AVG": round(9.2 - 0.8 * x["crash"], 1)})
            else:
                row.update({"ACTIVITY_NAME": key[2], "SLA_STATUS": key[3], "ACTIVITY_VOLUME": x["n"],
                            "ACTIVITY_RESPONSE_TIME_AVG": round(x["rt"] / x["n"], 3)})
            rows.append(row)
        return rows

    # -- rows of a data source between two times -----------------------------------
    def rows(self, entity: str, lo: datetime | None, hi: datetime | None) -> list[dict]:
        now = self.clock()
        kept = now - timedelta(days=ACTIVITY_DAYS if entity in SHORT_KEPT else HISTORY_DAYS)
        lo = max(lo or kept, kept)
        hi = min(hi or now, now)
        if lo > hi:
            return []
        hours = []
        h = lo.replace(minute=0, second=0, microsecond=0)
        while h <= hi:
            hours.append(h)
            h += HOUR
        days = [_day(lo) + timedelta(days=k) for k in range((_day(hi) - _day(lo)).days + 1)]
        if entity == "BUSINESS_ACTIVITIES_RAW":
            return [r for h in hours for r in self.actions(h)]
        if entity == "APPLICATIONS_RAW":
            return [r for h in hours for r in self.usage(h)]
        if entity == "APPLICATION_RESOURCES_HOURLY":
            return [r for h in [hours[0] - HOUR] + hours for r in self.processes(h)]
        if entity == "DEVICE_HEALTH_RAW":
            return [self.health(x) for d in days for x in self.incidents(d) if x["event"]]
        if entity == "WINDOWS_MACHINE_BOOTS":
            return [r for d in days for r in self.startups(d)[0]]
        if entity == "MACHINE_BOOT_COMPONENTS":
            return [r for d in days for r in self.startups(d)[1]]
        if entity == "INSTALLED_SOFTWARE_CHANGE_LOG":
            return self.changes()
        if entity == "APPLICATIONS_DAILY":
            return [r for d in days for r in self.daily(d, "usage")]
        if entity == "BUSINESS_ACTIVITIES_DAILY":
            return [r for d in days for r in self.daily(d, "actions")]
        if entity == "INSTALLED_SOFTWARE":
            return self.installed()
        if entity == "DEVICE_INVENTORY":
            return self.inventory()
        return []  # published, no rows (INCIDENTS, SERVICE_DESK_ALERTS_RAW, ...: as on prod)

    # -- OData -----------------------------------------------------------------------
    def odata(self, entity: str, query: str, base: str) -> tuple[int, dict]:
        if entity not in self.types:
            return 404, {"error": {"code": "404", "message": f"Entity set '{entity}' doesn't exist."}}
        cols = self.types[entity]
        params = dict(parse_qsl(query, keep_blank_values=True))
        select = [c.strip() for c in params.get("$select", "").split(",") if c.strip()]
        try:
            node = parse_filter(params["$filter"]) if params.get("$filter") else None
            order = [(p.split()[0], p.split()[-1].lower() == "desc") for p in params.get("$orderby", "").split(",")
                     if p.strip()]
            top = int(params["$top"]) if params.get("$top") else None
            skip = int(params.get("$skiptoken") or params.get("$skip") or 0)
        except (ValueError, IndexError) as exc:
            return 400, {"error": {"code": "400", "message": str(exc)}}
        unknown = [c for c in select + sorted(_columns(node) if node else ()) + [c for c, _ in order] if c not in cols]
        if unknown:
            return 400, {"error": {"code": "400", "message": f"Unknown column: {unknown[0]} (not in {entity})."}}
        lo, hi = _span_of(node, TIME_COL[entity]) if entity in TIME_COL else (None, None)
        now = self.clock()
        rows = [r for r in self.rows(entity, lo, hi)
                if r.get("_at", now) <= now and (node is None or _match(node, r))]
        if entity in ADDED_UP and select:  # over the columns asked for (with DEVICE_NAME: PC by PC)
            rows = self._add_up(rows, select)
        for col, desc in reversed(order):
            rows.sort(key=lambda r: (r.get(col) is None, r.get(col) if r.get(col) is not None else 0), reverse=desc)
        if top is not None:
            rows = rows[:top]
        page = rows[skip:skip + PAGE_ROWS]
        out = {"@odata.context": f"{base}{ATERNITY}/$metadata#{entity}",
               "value": [self._out(entity, r, select) for r in page]}
        if skip + PAGE_ROWS < len(rows):
            more = dict(params, **{"$skiptoken": str(skip + PAGE_ROWS)})
            out["@odata.nextLink"] = f"{base}{ATERNITY}/{entity}?" + urlencode(more, safe="$'(),:")
        return 200, out

    def _add_up(self, rows: list[dict], select: list[str]) -> list[dict]:
        dims = [c for c in select if c not in SUMS + AVERAGES + ONES + ("DEVICE_COUNT",)]
        groups: dict = {}
        for r in rows:
            g = groups.setdefault(tuple(r.get(c) for c in dims), {"rows": [], "devs": set()})
            g["rows"].append(r)
            g["devs"].add(r.get("DEVICE_NAME"))
        out = []
        for key, g in groups.items():
            row = dict(zip(dims, key))
            for c in select:
                vals = [x.get(c) for x in g["rows"] if x.get(c) is not None]
                if c in SUMS:
                    row[c] = round(sum(vals), 3)
                elif c in AVERAGES:
                    row[c] = round(sum(vals) / len(vals), 3) if vals else None
                elif c in ONES:
                    row[c] = 1
                elif c == "DEVICE_COUNT":
                    row[c] = len(g["devs"] - {None})
            out.append(row)
        return out

    def _out(self, entity: str, row: dict, select: list[str]) -> dict:
        types = self.types[entity]
        extra = {c for c in row if not c.startswith("_") and c not in types}
        if extra:
            self.dropped.setdefault(entity, set()).update(extra)
        out = {}
        for c in select or [c for c in row if not c.startswith("_") and c in types]:
            v = row.get(c)
            if isinstance(v, datetime):
                v = v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif v is None and types.get(c) == "String":
                v = "N/A"  # as Aternity writes no value
            out[c] = v
        return out

    # -- NetIM ---------------------------------------------------------------------------
    def netim(self, method: str, path: str, query: str, body: bytes) -> tuple[int, dict]:
        params = dict(parse_qsl(query))
        if method == "POST" and path == NETIM_METRICS:
            return 200, self._metric_data(json.loads(body or b"{}"))
        if method != "GET" or not path.startswith(NETIM_API):
            return 404, {"error": {"message": f"No such NetIM path: {path}"}}
        rest = path[len(NETIM_API):].strip("/").split("/")
        if rest == ["devices"]:
            items = [{"id": i, "name": n, "deviceName": n, "accessAddress": ip, "deviceType": kind, "siteName": site}
                     for i, n, ip, kind, site in NETIM_DEVICES]
        elif len(rest) == 3 and rest[0] == "devices" and rest[2] == "interfaces":
            items = [{"id": i, "name": n, "displayName": n, "ipAddresses": [{"ipAddress": ip, "prefixLength": p}],
                      "speed": int(speed)} for i, dev, n, ip, p, speed, _ in INTERFACES if str(dev) == rest[1]]
        elif rest == ["metric-classes"]:
            items = METRIC_CLASSES
        elif len(rest) == 2 and rest[0] == "metric-classes":
            one = next((m for m in METRIC_CLASSES if m["id"] == rest[1]), None)
            return (200, one) if one else (404, {"error": {"message": f"Metric class '{rest[1]}' does not exist"}})
        else:
            return 404, {"error": {"message": f"No such NetIM path: {path}"}}
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 500))
        return 200, {"items": items[offset:offset + limit],
                     "meta": {"total": len(items), "offset": offset, "limit": limit, "next_offset": offset + limit}}

    def _metric_data(self, req: dict) -> dict:
        names = list(req.get("metrics") or [])
        kind = req.get("elementType", "VNES_INTERFACE")
        start = datetime.fromtimestamp(int(req.get("startTime", 0)) / 1000, UTC)
        end = min(datetime.fromtimestamp(int(req.get("endTime", 0)) / 1000, UTC), self.clock())
        known = {i[0] for i in INTERFACES} if kind == "VNES_INTERFACE" else {d[0] for d in NETIM_DEVICES}
        ids = [int(i) for i in req.get("elementIds") or [] if str(i).isdigit() and int(i) in known]
        t0 = start.replace(second=0, microsecond=0, minute=start.minute // 5 * 5)
        data = []
        for el in ids:
            points, t = {}, t0
            while t <= end:
                if t >= start:
                    points[str(int(t.timestamp() * 1000))] = [f"{self._value(el, kind, m, t):.1f}" for m in names]
                t += 5 * MIN
            data.append({"elementId": el, "timestampToValuesMap": points})
        return {"elementIdToObjectInfoMap": {str(el): {"objectId": el, "objectType": kind} for el in ids},
                "metricData": {"metricNames": names,
                               "metricToMetaInfoMap": {m: {"units": "%", "valueEnumMap": "null"} for m in names},
                               "metricElementDataList": data}}

    def _value(self, el: int, kind: str, metric: str, t: datetime) -> float:
        r = _rng("netim", el, metric, int(t.timestamp()) // 300)
        d, c, T = _day(t), _clock(t), self.today
        busy = 1.0 if 7.5 <= c < 18 and d.weekday() < 5 else 0.45
        burst = d == T and 10.5 <= c < 11.25
        if kind == "VNES_OE":
            if burst and el == 3:
                return r.uniform(55, 65)
            return max(1.0, DEVICE_CPU.get(el, 15) * (0.6 + 0.4 * busy) + r.uniform(-4, 6))
        load = next((i[6] for i in INTERFACES if i[0] == el), 20)
        if burst and el == SOUTH_WAN:
            return r.uniform(91, 97) if metric == "utilizationIn" else r.uniform(38, 52)
        return max(0.5, load * (0.35 + 0.65 * busy) * (1 if metric == "utilizationIn" else 0.6) + r.uniform(-5, 5))

    # -- one request -----------------------------------------------------------------------
    def handle(self, method: str, path: str, query: str, body: bytes, base: str) -> tuple[int, str, bytes]:
        path = path.rstrip("/") or "/"
        if path == ATERNITY + "/$metadata":
            return 200, "application/xml", self.metadata.encode()
        if path == ATERNITY:
            return self._json(200, {"value": [{"name": n, "url": n} for n in sorted(self.types)]})
        if path.startswith(ATERNITY + "/"):
            entity = path[len(ATERNITY) + 1:]
            status, data = self.odata(entity, query, base)
            log.info("Aternity %s: %s", entity, f"{len(data['value'])} rows sent" if status == 200
                     else f"refused ({data['error']['message']})")
            return self._json(status, data)
        if path.startswith(NETIM):
            status, data = self.netim(method, path, query, body)
            log.info("NetIM %s %s: %s", method, path[len(NETIM):], "answered" if status == 200 else "not found")
            return self._json(status, data)
        return self._json(404, {"error": {"message": f"Nothing at {path}: Aternity is under {ATERNITY}, "
                                                    f"NetIM under {NETIM}."}})

    @staticmethod
    def _json(status: int, data) -> tuple[int, str, bytes]:
        return status, "application/json", json.dumps(data).encode()


# -- the saved daily counts, so the Today tab has a usual from the start ----------------
def seed(sample: Sample, data: Path, days: int = SEED_DAYS, force: bool = False) -> list[date]:
    """Each past day's counts, saved as the app's Today reader saves them (data/days),
    from the same rows this server sends. Days already saved are kept unless force."""
    sys.path.insert(0, str(HERE.parent / "src"))
    from uxcorrelate.config import DEFAULT_ATERNITY_FIELDS, NO_VALUE
    from uxcorrelate.today import Today

    today = Today(ZONE, store=data)
    f = DEFAULT_ATERNITY_FIELDS["activities"]
    done = []
    for n in range(days, 0, -1):
        d = sample.today - timedelta(days=n)
        if not force and (data / "days" / f"{d.isoformat()}.json").exists():
            continue
        read_at = today.midnight(d + timedelta(days=1)) + 2 * HOUR
        for start in today.hour_starts(d):
            rows = [{k: (None if v in NO_VALUE else v) for k, v in sample._out("BUSINESS_ACTIVITIES_RAW", r, []).items()}
                    for r in sample.actions(start)]
            today.add_hour(start, rows, f, read_at)
        today.save(d, today.summary(d))
        today.hours.clear()
        done.append(d)
    return done


def serve(sample: Sample, port: int) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import Response
    from starlette.routing import Route

    async def any_path(request):
        body = await request.body()
        base = f"{request.url.scheme}://{request.headers.get('host', 'localhost')}"
        status, ctype, data = await run_in_threadpool(sample.handle, request.method, request.url.path,
                                                      request.url.query, body, base)
        return Response(data, status, media_type=ctype)

    api = Starlette(routes=[Route("/{path:path}", any_path, methods=["GET", "POST"])])
    uvicorn.run(api, host="0.0.0.0", port=port, log_level="warning")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Made-up Aternity and NetIM for the dev app.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="save the past days' counts (when missing), then answer on PORT")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--data", type=Path, help="the app's data folder (its days/ gets the saved counts)")
    k = sub.add_parser("seed", help="only save the past days' counts")
    k.add_argument("--data", type=Path, required=True)
    k.add_argument("--force", action="store_true", help="write over days already saved")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    sample = Sample()
    if a.data:
        done = seed(sample, a.data, force=getattr(a, "force", False))
        log.info("Saved the counts of %d past days in %s%s", len(done), a.data / "days",
                 f" ({done[0]} to {done[-1]}); restart the app to read them" if done else " (all there already)")
    if a.cmd == "serve":
        log.info("Answering on port %d: Aternity at %s, NetIM at %s (made-up data, today is %s in %s)",
                 a.port, ATERNITY, NETIM, sample.today, ZONE)
        serve(sample, a.port)


if __name__ == "__main__":
    main()
