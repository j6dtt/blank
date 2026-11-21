"""The "Software" tab (/api/software): red flags on installed software and on
application versions, for management. The design is docs/software-tab.md.

Once a day, and at start, one background reader (`keep_reading`) reads Aternity:
- the installed list (`SOFTWARE_SOURCE`), added up by Aternity per program and
  version, per program, per PC, and for one user only (`SOFTWARE_USER_SCOPE`);
- each program with more installs than PCs, PC by PC: PCs with 2+ versions;
- Windows on each PC (the inventory) and each Windows update (`SOFTWARE_OS_UPDATE`);
- each application's version on app usage (crashes, hang time) and on actions
  (Aternity's Major) over the last DAYS days, per PC, for the applications with
  a problem and 2 versions or more (`VERSION_SOURCES`).
From these it raises red flags: plain counts of PCs, never a score, ranked by
PCs. Each read is saved in <data>/software/<date>.json (counts, program and
application names; no PC or user names), kept KEEP_DAYS, for the "new" marker
and each flag's days. PC names are only read when a list is opened (`pcs`).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .aternity import _bare, is_virtual, odata_quote, odata_time
from .config import (OS_COLUMNS, SOFTWARE_COLUMNS, SOFTWARE_OS_UPDATE, SOFTWARE_SOURCE, SOFTWARE_USER_SCOPE,
                     TOP_STATUS, VERSION_SOURCES)
from .http import short_error
from .models import parse_time, to_float
from .today import zone_of

log = logging.getLogger("uxcorrelate")

READ_EVERY_H = 24  # the whole read, once a day (and at start)
RETRY_MIN = 15     # ... and this soon again when Aternity did not answer for the list itself
KEEP_DAYS = 90  # saved days, for "new" and each flag's days
DAYS = 7  # application versions: the days counted
RATIO = 2.0  # a version is raised at this times the share it is compared with (decision 1)
MIN_PCS = 20  # ... with at least this many PCs on each side
MIN_PROBLEM = 5  # ... and at least this many PCs with the problem on it
STILL = 2  # ... and still at RATIO times with this many more PCs with the problem on the other side
ARRIVED = 0.5  # "new this week": more than this share of a version's PCs got it within DAYS
UPDATES_SHOWN = 2  # before and after an update: the changes with the most PCs, per kind of PC
PLACES = {"virtual": "virtual desktops", "physical": "physical PCs", "all": "PCs"}  # the kinds of PC, in words
FEW_PCS = 5  # flag 6: programs on this many PCs or fewer
WIN_MOST = 0.5  # flag 5: an update on more than half the PCs of its Windows version
MULTI_MAX = 200  # flag 4: programs read PC by PC, most extra installs first (143 on the live tenant)
APPS_MAX = 40  # flag 1: applications read PC by PC, for each of usage and actions
WEEKS = 12  # Windows last updated: weeks shown, then "older"
AGG_ROWS = 5000  # rows of a read Aternity adds up (name + version: 2,245 on the live tenant)
LIST_ROWS = 20000  # rows of a read that lists PCs
PCS_SHOWN = 200  # PCs in a list opened by a click
GONE_BATCH = 25  # retired PCs asked for in one read of their software (their names joined by "or")
GONE_MOST = 0.5  # more of the list's PCs than this missing from the device list: not taken as retired PCs
UPDATE_DAYS = 14  # before and after an update: the days read
TAIL = ((1, 1), (2, 5), (6, 20), (21, 100), (101, 1000), (1001, None))  # flag 6: programs by how many PCs
NO_VERSION = ""  # how a row with no version is kept (Aternity's "N/A")

KIND = {  # flag kinds: the words on the page, most important first
    "crash": "Crashes more", "hang": "Hangs more", "slow": "Slower",
    "windows": "Windows update not on every PC", "multi": "2+ versions on one PC",
    "behind": "Behind most PCs", "user": "Installed for one user", "few": f"On {FEW_PCS} PCs or fewer",
}


def version_key(v) -> tuple | None:
    """A dotted version as numbers ("26.002.21901" -> (26, 2, 21901)), else None."""
    parts = str(v or "").strip().split(".")
    return tuple(int(p) for p in parts) if parts and all(p.isdigit() for p in parts) else None


def order(v) -> tuple:
    """Sort key: dotted versions by their numbers, oldest first, then the others by name."""
    k = version_key(v)
    return (0, k, "") if k is not None else (1, (), str(v))


def shown(v) -> str:
    return "(no version)" if v in (None, NO_VERSION) else str(v)


def version_of(asked, versions) -> str | None:
    """The version meant: exactly, else the one that starts with it ("16.0.20430" for
    16.0.20430.20180); None when none or several do."""
    asked = str(asked or "").strip()
    if not asked:
        return None
    if asked in versions:
        return asked
    starts = [v for v in versions if str(v).startswith(asked + ".")]
    return starts[0] if len(starts) == 1 else None


def starts_with(v, asked: str) -> bool:
    """A version is `asked` or starts with it ("143" has 143.0.3650.139)."""
    return str(v) == asked or str(v).startswith(asked + ".")


def _int(v) -> int:
    return int(to_float(v) or 0)


def _most(versions: dict) -> str | None:
    """The version most PCs have (a tie: the newer)."""
    return max(versions, key=lambda v: (versions[v], order(v))) if versions else None


CUT = "Aternity's reply was cut short, so these counts are partial."


def _partial(client, mark: int, rows: list, top: int | None) -> bool:
    """A read cut short: the client stopped at its page limit (a warning after `mark`), or the
    rows reached `top`. Its counts are partial and must say so."""
    return len(client.warnings) > mark or bool(top and len(rows) >= top)


class _Reads:
    """Aternity reads that never stop the whole read: what failed or was cut short is a note."""

    def __init__(self, client, notes: list[str]):
        self.client, self.notes = client, notes
        self.schema: dict | None = None
        self.error = ""  # the first read Aternity did not answer, in a few words
        self.cut = False  # the last read was cut short (its counts are partial)
        self.usage: dict[str, int] = {}  # every application in app usage -> its crashes (the name search)
        self.crash_names: dict[str, int] = {}  # application (else program file) of crash events -> PCs
        try:
            from .verify import parse_odata_metadata
            text = client.http.request("GET", "/$metadata", headers={"Accept": "application/xml"}).text
            self.schema = parse_odata_metadata(text)
        except Exception as exc:  # then every column is asked for as configured
            notes.append(f"Aternity's list of data sources could not be read ({short_error(exc)}); "
                         "every column was asked for as configured.")

    def has(self, entity: str | None, column: str | None) -> bool:
        if not entity or not column:
            return False
        return self.schema is None or (entity in self.schema and column in self.schema[entity])

    def rows(self, entity: str, what: str, **kw) -> list[dict] | None:
        mark = len(self.client.warnings)
        self.cut = False
        try:
            got = self.client.query(entity, **kw)
        except Exception as exc:
            self.error = self.error or short_error(exc)
            self.notes.append(f"{what} could not be read ({short_error(exc)}).")
            return None
        self.cut = _partial(self.client, mark, got, kw.get("top"))
        if self.cut:
            self.notes.append(f"{what}: Aternity's reply was cut short ({len(got):,} rows), so its counts "
                              "are partial.")
        return got


# -- the daily read -------------------------------------------------------------
def _installed(q: _Reads, say) -> tuple[dict, dict | None, set]:
    """Programs: PCs per version, PCs, one-user PCs; the list's size; and its PCs."""
    S, c = SOFTWARE_SOURCE, SOFTWARE_COLUMNS
    D, N, V, P, T = (c[k] for k in ("device", "name", "version", "pcs", "type"))
    if q.schema is not None and S not in q.schema:
        q.notes.append(f"{S} is not published by this Aternity: nothing on installed software can be read.")
        return {}, None, set()
    say("Reading the installed software list")
    agg = q.rows(S, "The installed software list", select=[N, V, T, P] if q.has(S, T) else [N, V, P],
                 top=AGG_ROWS)
    if not agg:
        return {}, None, set()
    progs: dict[str, dict] = {}
    for r in agg:
        name = r.get(N)
        if name is None:
            continue
        p = progs.setdefault(str(name), {"name": str(name), "type": r.get(T), "versions": {}, "pcs": 0, "user": 0,
                                         "multi": None})
        v = NO_VERSION if r.get(V) is None else str(r.get(V))
        p["versions"][v] = p["versions"].get(v, 0) + _int(r.get(P))
    byname = q.rows(S, "The installed list per program", select=[N, P], top=AGG_ROWS)
    for r in byname or []:
        if str(r.get(N)) in progs:
            progs[str(r.get(N))]["pcs"] = _int(r.get(P))
    for p in progs.values():  # not read per program: a PC with 2 versions counts twice
        if not p["pcs"]:
            p["pcs"] = sum(p["versions"].values())
    listed = q.rows(S, "The PCs in the installed list", select=[D], top=LIST_ROWS)
    users = None
    if q.has(S, c.get("scope")):
        users = q.rows(S, "Programs installed for one user", select=[N, P], top=AGG_ROWS,
                       filter=f"{c['scope']} eq {odata_quote(SOFTWARE_USER_SCOPE)}")
        for r in users or []:
            if str(r.get(N)) in progs:
                progs[str(r.get(N))]["user"] = _int(r.get(P))
    pcs = {str(r.get(D)) for r in listed or [] if r.get(D)}
    size = {"pcs": len(pcs) if listed else None, "left_out": 0, **_sizes(progs, users is not None)}
    return progs, size, (pcs if listed and not q.cut else set())


def _sizes(progs: dict, users: bool) -> dict:
    return {"programs": len(progs), "installs": sum(sum(p["versions"].values()) for p in progs.values()),
            "user_programs": sum(1 for p in progs.values() if p["user"]) if users else None,
            "user_installs": sum(p["user"] for p in progs.values()) if users else None}


NOT_LEFT_OUT = "so PCs that are gone are still counted in the installed software."


def _retired(q: _Reads, cfg, listed: set, say) -> set:
    """The PCs of the installed list that are no longer in Aternity's device list: Aternity takes
    a PC off its device list about a month after it last heard from it but keeps its software
    (stage, Nov 20 2026: 1,266 of 6,464 PCs, 3 of them in use in 8 days). Empty when not known:
    the device list not read whole, or more than `GONE_MOST` of the PCs missing from it (then
    they are not retired PCs: the two lists do not go together)."""
    inv, dev = cfg.entities.get("inventory"), cfg.fields.get("inventory", {}).get("device")
    if not listed:
        return set()
    if not inv or not dev or not q.has(inv, dev):
        q.notes.append(f"Aternity's device list is not published, {NOT_LEFT_OUT}")
        return set()
    say("Reading Aternity's device list")
    rows = q.rows(inv, "Aternity's device list", select=[dev], top=LIST_ROWS)
    known = {str(r.get(dev)).lower() for r in rows or [] if r.get(dev)}
    if not known or q.cut:
        if rows is not None:  # a failed read has its own note
            q.notes.append(f"Aternity's device list {'was cut short' if known else 'is empty'}, {NOT_LEFT_OUT}")
        return set()
    gone = {pc for pc in listed if pc.lower() not in known}
    if len(gone) > GONE_MOST * len(listed):
        q.notes.append(f"{len(gone):,} of the {len(listed):,} PCs with installed software are not in Aternity's "
                       f"device list: too many to be PCs that are gone, so none is left out.")
        return set()
    return gone


def rows_of_pcs(client, pcs, select: list[str], say=lambda s: None) -> tuple[list[dict] | None, dict]:
    """The installed software rows of these PCs, `GONE_BATCH` PCs a read (their names joined by
    "or"; when Aternity refuses that or cuts a reply short, one PC a read from there on). None
    when a read failed or was cut short even so: the rows are then not the whole of it. Also how
    it went ({"reads", "one_by_one", "error"}), for verify --inventory's timing."""
    D = SOFTWARE_COLUMNS["device"]
    pcs, out, i, batch = sorted(pcs), [], 0, GONE_BATCH
    info = {"reads": 0, "one_by_one": False, "error": ""}
    while i < len(pcs):
        chunk = pcs[i:i + batch]
        say(f"Reading the software of PCs no longer in Aternity's device list: {i:,} of {len(pcs):,}")
        mark = len(client.warnings)
        try:
            got = client.query(SOFTWARE_SOURCE, filter=" or ".join(f"{D} eq {odata_quote(pc)}" for pc in chunk),
                               select=select, top=LIST_ROWS)
            error = "Aternity's reply was cut short" if _partial(client, mark, got, LIST_ROWS) else ""
        except Exception as exc:
            error = short_error(exc)
        info["reads"] += 1
        if error and batch > 1:
            batch, info["one_by_one"] = 1, True
            continue
        if error:
            info["error"] = error
            return None, info
        out += got
        i += len(chunk)
    return out, info


def _leave_out(q: _Reads, progs: dict, gone: set, say) -> bool:
    """Takes the retired PCs' software off Aternity's totals (it adds them up for us, and has no
    "last heard" on the installed list to filter by): each PC once per program and version, per
    program, and per program installed for one user. False (nothing changed) when their software
    could not be read whole."""
    c = SOFTWARE_COLUMNS
    D, N, V, scope = c["device"], c["name"], c["version"], c.get("scope")
    scoped = q.has(SOFTWARE_SOURCE, scope)
    rows, info = rows_of_pcs(q.client, gone, [D, N, V] + ([scope] if scoped else []), say)
    if rows is None:
        q.notes.append(f"The software of the {len(gone):,} PCs no longer in Aternity's device list could not be "
                       f"read ({info['error']}), {NOT_LEFT_OUT}")
        return False
    per_v, per_n, per_u = set(), set(), set()
    for r in rows:
        pc, p = r.get(D), progs.get(str(r.get(N)))
        if p is None or not pc:
            continue
        v = NO_VERSION if r.get(V) is None else str(r.get(V))
        per_v.add((pc, p["name"], v))
        per_n.add((pc, p["name"]))
        if scoped and r.get(scope) == SOFTWARE_USER_SCOPE:
            per_u.add((pc, p["name"]))
    for _, name, v in per_v:
        if v in progs[name]["versions"]:
            progs[name]["versions"][v] = max(progs[name]["versions"][v] - 1, 0)
    for _, name in per_n:
        progs[name]["pcs"] = max(progs[name]["pcs"] - 1, 0)
    for _, name in per_u:
        progs[name]["user"] = max(progs[name]["user"] - 1, 0)
    for name in {x[1] for x in per_n}:
        p = progs[name]
        p["versions"] = {v: k for v, k in p["versions"].items() if k}
        if not p["versions"] or not p["pcs"]:
            del progs[name]  # only on PCs that are gone
    return True


def _multi(q: _Reads, progs: dict, say, gone=frozenset()) -> None:
    """2+ versions on one PC: each program with more installs than PCs, read PC by PC (the PCs
    in `gone`, no longer in Aternity's device list, left out)."""
    S, c = SOFTWARE_SOURCE, SOFTWARE_COLUMNS
    D, N, V = c["device"], c["name"], c["version"]
    cands = sorted((p for p in progs.values() if p["type"] != SOFTWARE_OS_UPDATE
                    and sum(p["versions"].values()) > p["pcs"] > 0),
                   key=lambda p: (p["pcs"] - sum(p["versions"].values()), p["name"]))
    if len(cands) > MULTI_MAX:
        q.notes.append(f"2+ versions on one PC: {len(cands):,} programs have more installs than PCs; the "
                       f"{MULTI_MAX} with the most were read PC by PC.")
    for i, p in enumerate(cands[:MULTI_MAX]):
        say(f"Reading 2+ versions on one PC: {i} of {min(len(cands), MULTI_MAX)} programs")
        rows = q.rows(S, f"{p['name']}, PC by PC", filter=f"{N} eq {odata_quote(p['name'])}", select=[D, V],
                      top=LIST_ROWS)
        if rows is None:
            continue
        per: dict = {}
        for r in rows:
            if r.get(D) not in gone:
                per.setdefault(r.get(D), set()).add(NO_VERSION if r.get(V) is None else str(r.get(V)))
        two = [vs for vs in per.values() if len(vs) > 1]
        combos = Counter(" + ".join(shown(v) for v in sorted(vs, key=order)) for vs in two)
        most = version_key(_most(p["versions"]))
        dotted = most is not None and all(version_key(v) is not None for vs in per.values() for v in vs)
        p["multi"] = {"pcs": len(two), "combos": [[k, n] for k, n in combos.most_common(3)],
                      # behind, PC by PC: only when every version on the PC is older than the most common one
                      "behind": sum(1 for vs in per.values() if max(version_key(v) for v in vs) < most)
                      if dotted else None}


def windows_of(r: dict) -> str:
    """A PC's Windows, as the page shows it: the inventory's OS version, else its OS name."""
    o = OS_COLUMNS
    return str(r.get(o["version"]) or "").strip() or str(r.get(o["name"]) or "").strip() or "not known"


def _windows(q: _Reads, cfg, local, now: datetime, say) -> dict | None:
    """Windows on each PC, when it was last updated, and each Windows update on the PCs of
    its own Windows version: the updates most of them have, not on every one, and what
    the PCs without one have instead (a later update may be in its place)."""
    inv, dev = cfg.entities.get("inventory"), cfg.fields.get("inventory", {}).get("device")
    o = OS_COLUMNS
    cols = [x for x in (o["name"], o["version"], o["updated"]) if q.has(inv, x)]
    if not inv or not dev or o["version"] not in cols:
        q.notes.append(f"{inv or 'The inventory'} gives no {o['version']}: Windows updates are not counted.")
        return None
    say("Reading Windows on each PC")
    rows = q.rows(inv, "Windows on each PC", select=[dev] + cols, top=LIST_ROWS)
    if not rows:
        return None
    partial = q.cut
    win: dict = {}
    weeks: Counter = Counter()
    older = 0
    this_monday = local.monday(now)
    for r in rows:
        d = r.get(dev)
        if not d:
            continue
        win[d] = windows_of(r)
        t = parse_time(r.get(o["updated"])) if o["updated"] in cols else None
        if t:
            m = local.monday(t)
            if (this_monday - m).days >= WEEKS * 7:
                older += 1
            else:
                weeks[m.isoformat()] += 1
    out = {"pcs": len(win), "older": older, "dated": older + sum(weeks.values()),
           "weeks": [[(this_monday - timedelta(days=7 * k)).isoformat(),
                      weeks[(this_monday - timedelta(days=7 * k)).isoformat()]] for k in range(WEEKS - 1, -1, -1)],
           "versions": [], "read_updates": False, "partial": partial}
    S, c = SOFTWARE_SOURCE, SOFTWARE_COLUMNS
    by_win: dict = {}
    for d, w in win.items():
        by_win.setdefault(w, []).append(d)
    if not q.has(S, c.get("type")):
        out["versions"] = [{"windows": w, "pcs": len(ds), "missing": []} for w, ds in
                           sorted(by_win.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
        return out
    say("Reading Windows updates")
    ups = q.rows(S, "Windows updates", filter=f"{c['type']} eq {odata_quote(SOFTWARE_OS_UPDATE)}",
                 select=[c["device"], c["name"]], top=LIST_ROWS * 4)
    has: dict = {}
    for r in ups or []:
        has.setdefault(r.get(c["device"]), set()).add(str(r.get(c["name"])))
    out["read_updates"] = ups is not None
    # cut short: PCs past the cut seem to have no update, which a flag would count as missing
    out["partial"] = partial or q.cut
    for w, devs in sorted(by_win.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        total = len(devs)
        count = Counter(u for d in devs for u in has.get(d, ()))
        missing = []
        for u, k in count.items():
            if k <= WIN_MOST * total or total - k < MIN_PROBLEM:
                continue
            without = [d for d in devs if u not in has.get(d, ())]
            with_ = [d for d in devs if u in has.get(d, ())]
            inst = Counter(x for d in without for x in has.get(d, ()))
            inw = Counter(x for d in with_ for x in has.get(d, ()))
            instead = [{"update": x, "on": n, "with": inw[x]} for x, n in
                       sorted(inst.items(), key=lambda kv: (-kv[1], kv[0]))
                       if n * len(with_) > inw[x] * len(without)][:3]
            missing.append({"update": u, "on": k, "without": total - k,
                            "none": sum(1 for d in without if not has.get(d)), "instead": instead})
        missing.sort(key=lambda m: (-m["without"], m["update"]))
        out["versions"].append({"windows": w, "pcs": total, "updates": len(count),
                                "no_updates": sum(1 for d in devs if not has.get(d)), "missing": missing})
    return out


def _blank() -> dict:
    return {"pcs": set(), "crash": set(), "hang": set(), "virtual": set(), "apcs": set(), "avirtual": set(),
            "major_pcs": set(), "actions": 0.0, "major": 0.0, "dev": {}}  # dev: PC -> [actions, Major actions]


def _span(spec: dict, start: datetime, end: datetime) -> str:
    return f"{spec['time']} ge {odata_time(start)} and {spec['time']} lt {odata_time(end)}"


def _versions(q: _Reads, now: datetime, say) -> dict:
    """Per application and version: PCs that ran it, with a crash, with hang time, on virtual
    desktops (app usage); actions, Major actions, PCs with a Major action (actions)."""
    apps: dict[str, dict] = {}
    extras: dict[str, dict] = {}  # per application: its days, its virtual desktops (`_app_rows`)
    read: dict[str, set] = {}
    cut: set[str] = set()  # applications whose rows were cut short
    start = now - timedelta(days=DAYS)
    for key, problem, pick in (("usage", "crashes and hangs", "crashes"), ("actions", "slow actions", "status")):
        s = VERSION_SOURCES[key]
        E, A, V = s["entity"], s["application"], s["version"]
        if q.schema is not None and E not in q.schema:
            q.notes.append(f"{E} is not published: versions are not compared on {problem}.")
            continue
        if not q.has(E, V):
            q.notes.append(f"{E} gives no {V}: versions are not compared on {problem}.")
            continue
        say(f"Reading application versions on {problem}")
        cols = [A, V, s["crashes"], s["hang_time"]] if key == "usage" else [A, V, s["status"], s["volume"]]
        agg = q.rows(E, f"Application versions on {problem}", filter=_span(s, start, now), select=cols,
                     top=AGG_ROWS)
        per: dict = {}
        for r in agg or []:
            a, v = r.get(A), r.get(V)
            if a is None or v is None:
                continue
            x = per.setdefault(str(a), [set(), 0.0, 0.0])
            x[0].add(str(v))
            if key == "usage":
                x[1] += to_float(r.get(s["crashes"])) or 0
                x[2] += to_float(r.get(s["hang_time"])) or 0
            elif r.get(s["status"]) == TOP_STATUS:
                x[1] += to_float(r.get(s["volume"])) or 1
        if key == "usage":  # every name, also those not read per version below
            q.usage = {a: int(x[1]) for a, x in per.items()}
        cands = sorted((a for a, x in per.items() if len(x[0]) > 1 and (x[1] >= MIN_PROBLEM or x[2] > 0)),
                       key=lambda a: (-per[a][1], -per[a][2], a))
        if len(cands) > APPS_MAX:
            q.notes.append(f"{len(cands)} applications have {problem} on 2 versions or more; the {APPS_MAX} with "
                           "the most were compared.")
        for i, a in enumerate(cands[:APPS_MAX]):
            say(f"Reading application versions on {problem}: {i} of {min(len(cands), APPS_MAX)} applications")
            if _app_rows(q, key, a, start, now, apps.setdefault(a, {}), extras.setdefault(a, {})):
                read.setdefault(a, set()).add(key)
                if q.cut:
                    cut.add(a)
    return {a: _judged(a, vs, read.get(a, ()), a in cut, extras.get(a), now) for a, vs in apps.items() if a in read}


def _app_rows(q: _Reads, key: str, a: str, start: datetime, now: datetime, vs: dict,
              extra: dict | None = None) -> bool:
    """One application's rows of one source (VERSION_SOURCES), per PC and version, added to
    `vs`; False when they could not be read. App usage is read day by day back to UPDATE_DAYS
    (one read, as before): only its days from `start` are counted per version, and every day
    goes to `extra["days"]` (PC -> day -> [its versions, a crash, hang time, counted]) for before
    and after an update and "new this week". `extra["virtual"]`: the PCs Aternity marks virtual;
    `extra["marked"]`: the sources that gave the mark; `extra["rows"]`: rows per source."""
    s = VERSION_SOURCES[key]
    E, V = s["entity"], s["version"]
    if (q.schema is not None and E not in q.schema) or not q.has(E, V):
        return False
    extra = extra if extra is not None else {}
    usage = key == "usage"
    virt = s.get("virtual") if q.has(E, s.get("virtual")) else None
    sel = [s["device"], V] + ([s["time"], s["crashes"], s["hang_time"]] if usage else [s["status"], s["volume"]]) \
        + ([virt] if virt else [])
    since = min(start, now - timedelta(days=UPDATE_DAYS)) if usage else start
    rows = q.rows(E, f"{a}, per PC and version" + (", day by day" if usage else ""), select=sel,
                  top=LIST_ROWS * (5 if usage else 2),
                  filter=f"{_span(s, since, now)} and {s['application']} eq {odata_quote(a)}")
    if rows is None:
        return False
    if virt:
        extra.setdefault("marked", set()).add(key)
    extra.setdefault("rows", {})[key] = len(rows)
    days, virtual = extra.setdefault("days", {}), extra.setdefault("virtual", set())
    for r in rows:
        v, d = r.get(V), r.get(s["device"])
        if v is None or d is None:
            continue
        if virt and is_virtual(r.get(virt)):
            virtual.add(d)
        if usage:
            crash, hang = (to_float(r.get(s["crashes"])) or 0) > 0, (to_float(r.get(s["hang_time"])) or 0) > 0
            t = parse_time(r.get(s["time"]))
            if t is not None:
                day = days.setdefault(d, {}).setdefault(t.date(), [set(), False, False, t >= start])
                day[0].add(str(v))
                day[1], day[2] = day[1] or crash, day[2] or hang
                if t < start:  # before the days counted: only for before and after an update
                    continue
            x = vs.setdefault(str(v), _blank())  # a row with no day is counted (it was asked for by day)
            x["pcs"].add(d)
            if crash:
                x["crash"].add(d)
            if hang:
                x["hang"].add(d)
            if virt and is_virtual(r.get(virt)):
                x["virtual"].add(d)
        else:
            x = vs.setdefault(str(v), _blank())
            n = to_float(r.get(s["volume"]))
            n = 1.0 if n is None else n
            x["apcs"].add(d)
            x["actions"] += n
            dev = x["dev"].setdefault(d, [0.0, 0.0])
            dev[0] += n
            if r.get(s["status"]) == TOP_STATUS:
                x["major"] += n
                x["major_pcs"].add(d)
                dev[1] += n
            if virt and is_virtual(r.get(virt)):
                x["avirtual"].add(d)
    return True


def _changed_once(by: dict) -> tuple | None:
    """A PC whose version of the application changed once in the days read: (from, to, the day
    it changed, its days before, its days after), the same number of days on each side (the
    last ones before, the first ones after), so neither side had more time to go wrong. None
    when it did not change, changed more than once, or ran 2 versions on one day (that day is
    on neither side)."""
    seq = sorted(by.items())
    if any(len(x[0]) != 1 for _, x in seq):
        return None
    vs = [next(iter(x[0])) for _, x in seq]
    changes = [i for i in range(1, len(vs)) if vs[i] != vs[i - 1]]
    if len(changes) != 1:
        return None
    i = changes[0]
    k = min(i, len(seq) - i)
    return vs[0], vs[i], seq[i][0], seq[i - k:i], seq[i:i + k]


def _updates(days: dict, inn) -> list[dict]:
    """Before and after an update, on the same PCs (those of one kind, `inn`): per change of
    version that MIN_PCS PCs or more made, the PCs with a crash and with hang time on their
    days before and on as many days after. The changes with the most PCs."""
    pairs: dict = {}
    for d, by in days.items():
        got = _changed_once(by) if inn(d) else None
        if not got:
            continue
        f, t, when, before, after = got
        p = pairs.setdefault((f, t), {"from": f, "to": t, "pcs": 0, "first": when, "last": when,
                                      "before": {"crash": 0, "hang": 0}, "after": {"crash": 0, "hang": 0}})
        p["pcs"] += 1
        p["first"], p["last"] = min(p["first"], when), max(p["last"], when)
        for side, part in (("before", before), ("after", after)):
            p[side]["crash"] += any(x[1] for _, x in part)
            p[side]["hang"] += any(x[2] for _, x in part)
    out = sorted((p for p in pairs.values() if p["pcs"] >= MIN_PCS), key=lambda p: (-p["pcs"], order(p["to"])))
    return [dict(p, first=p["first"].isoformat(), last=p["last"].isoformat()) for p in out[:UPDATES_SHOWN]]


def _new(v: str, pcs: set, days: dict) -> bool:
    """"New this week": more than ARRIVED of the version's PCs got it within the days counted
    (their first day on it is one of those days, after a day on another version). It has had
    less time to go wrong than the others, so it is judged before and after its update only."""
    got = 0
    for d in pcs:
        by = days.get(d) or {}
        on = [day for day, x in by.items() if v in x[0]]
        if on and by[min(on)][3] and any(day < min(on) for day in by):
            got += 1
    return bool(pcs) and got > ARRIVED * len(pcs)


def _kind_rows(versions: list[dict]) -> dict:
    """All PCs as one kind, from the rows per version (a day saved before the kinds of PC, or
    Aternity gives no virtual / physical mark)."""
    rows = [{k: v.get(k, 0) for k in ("version", "pcs", "crash", "hang", "action_pcs", "actions", "major",
                                       "major_pcs")} | {"new": False} for v in versions]
    return {"pcs": sum(max(r["pcs"], r["action_pcs"]) for r in rows), "versions": rows, "updates": []}


def _kinds(vs: dict, extra: dict) -> dict:
    """The application's versions per kind of PC (Aternity's virtual / physical mark; all PCs as
    one kind, "all", when it gives none): a virtual desktop and a physical PC are compared apart,
    as one version can behave differently on each. Per kind: its versions (most PCs first), each
    with its PCs, those with a crash and with hang time, its actions and those rated Major, and
    whether it is new this week; the PCs that changed version (`_updates`)."""
    virtual, days = set(extra.get("virtual", ())), extra.get("days") or {}
    out = {}
    for on in (("virtual", "physical") if extra.get("marked") else ("all",)):
        inn = {"all": lambda d: True, "virtual": lambda d: d in virtual, "physical": lambda d: d not in virtual}[on]
        rows, every = [], set()
        for v, x in vs.items():
            pcs, apcs = {d for d in x["pcs"] if inn(d)}, {d for d in x["apcs"] if inn(d)}
            if not pcs and not apcs:
                continue
            every |= pcs | apcs
            acts = [n for d, n in x["dev"].items() if inn(d)]
            rows.append({"version": v, "pcs": len(pcs), "crash": len(x["crash"] & pcs), "hang": len(x["hang"] & pcs),
                         "action_pcs": len(apcs), "actions": int(round(sum(n[0] for n in acts))),
                         "major": int(round(sum(n[1] for n in acts))), "major_pcs": len(x["major_pcs"] & apcs),
                         "new": _new(v, pcs, days)})
        rows.sort(key=lambda r: (-max(r["pcs"], r["action_pcs"]), order(r["version"])))
        ups = _updates(days, inn)
        if rows or ups:
            out[on] = {"pcs": len(every), "versions": rows, "updates": ups}
    return out


def _judged(a: str, vs: dict, read, partial: bool = False, extra: dict | None = None,
            now: datetime | None = None) -> dict:
    rows = [{"version": v, "pcs": len(x["pcs"]), "crash": len(x["crash"]), "hang": len(x["hang"]),
             "virtual": len(x["virtual"]), "action_pcs": len(x["apcs"]), "action_virtual": len(x["avirtual"]),
             "actions": int(round(x["actions"])), "major": int(round(x["major"])),
             "major_pcs": len(x["major_pcs"])} for v, x in vs.items()]
    rows.sort(key=lambda r: (-max(r["pcs"], r["action_pcs"]), order(r["version"])))
    union = lambda k: len(set().union(*(x[k] for x in vs.values()))) if vs else 0  # noqa: E731
    # the application's PCs across its versions, each PC once (for "which crashed the most")
    app = {"application": a, "read": sorted(read), "versions": rows, "partial": partial,
           "totals": {"pcs": union("pcs"), "crash": union("crash"), "hang": union("hang"),
                      "action_pcs": union("apcs"), "slow": union("major_pcs")},
           "kinds": _kinds(vs, extra or {})}
    judge(app)
    return app


def app_now(client, now: datetime, application: str) -> dict:
    """One application per version, read now, in the daily read's shape (judged by the same
    rules): for one the daily read left out (1 version, too few problems, or beyond APPS_MAX)."""
    notes: list[str] = []
    q = _Reads(client, notes)
    vs: dict = {}
    read, cut = [], False
    extra: dict = {}
    for key in ("usage", "actions"):
        if _app_rows(q, key, application, now - timedelta(days=DAYS), now, vs, extra):
            read.append(key)
            cut = cut or q.cut
    app = _judged(application, vs, read, cut, extra, now)
    return dict(_versions_view({application: app})[0], notes=notes, error=q.error if not read else "",
                day_rows=extra.get("rows", {}).get("usage"))


# words that name no program by themselves: "Microsoft Edge" is looked for as "edge"
GENERIC = {"microsoft", "ms", "the", "a", "an", "of", "for", "app", "application", "applications", "program",
           "programs", "version", "versions", "browser"}


def words(asked) -> list[str]:
    """The words of a name asked for that say which program it is ("Microsoft Edge" -> edge)."""
    tokens = re.findall(r"[a-z0-9+#]+", str(asked or "").lower())
    return [t for t in tokens if t not in GENERIC] or tokens


def named_like(name, asked) -> bool:
    """Every telling word of what was asked is in the name: "edge" is in msedge.exe and in
    Microsoft Edge Update. One rule for every name search (the user's finding on stage, Nov
    2026: three fixes each looked for the exact name "Microsoft Edge", which Aternity never uses)."""
    low, ws = str(name or "").lower(), words(asked)
    return bool(ws) and all(w in low for w in ws)


def _crashes(q: _Reads, now: datetime, say) -> list[dict] | None:
    """Every crash event of the last DAYS days, added up by application, program file (the
    failing part) and its version: PCs and events. Names and counts only. None: not read."""
    cfg = q.client.cfg
    entity, f = cfg.entities.get("health_events"), cfg.fields.get("health_events", {})
    need = [f.get(k) for k in ("timestamp", "device", "name")]
    if not entity or not all(need) or (q.schema is not None and entity not in q.schema):
        q.notes.append("Health events are not published: crashes by program are not counted.")
        return None
    cols = [f[k] for k in ("device", "name", "application", "component", "component_version")
            if f.get(k) and q.has(entity, f[k])]
    ts = f["timestamp"]
    per: dict[tuple, dict] = {}
    by_name: dict[str, set] = {}
    for n in range(DAYS, 0, -1):  # a day at a time, as the Today reader
        say(f"Reading crash events: day {DAYS - n + 1} of {DAYS}")
        start, end = now - timedelta(days=n), now - timedelta(days=n - 1)
        rows = q.rows(entity, "Crash events of a day", select=cols,
                      filter=f"{ts} ge {odata_time(start)} and {ts} lt {odata_time(end)}")
        if rows is None:
            return None
        for r in rows:
            name, dev = r.get(f["name"]), r.get(f["device"])
            if not dev or "crash" not in str(name or "").lower():
                continue
            # without Aternity's labels ("x.exe (Process)", "1.2 (Process Version)"); 0.0.0.0 is no version
            version = _bare(r.get(f.get("component_version", "")))
            key = (r.get(f.get("application", "")) or None, _bare(r.get(f.get("component", ""))) or None,
                   None if version in ("", "0.0.0.0") else version)
            x = per.setdefault(key, {"pcs": set(), "events": 0, "kinds": set()})
            x["pcs"].add(dev)
            x["events"] += 1
            x["kinds"].add(str(name))
            if key[0] or key[1]:
                by_name.setdefault(key[0] or key[1], set()).add(dev)
    q.crash_names = {k: len(v) for k, v in by_name.items()}  # each name's PCs, each PC once
    return [{"application": a, "part": p, "version": v, "pcs": len(x["pcs"]), "events": x["events"],
             "kinds": sorted(x["kinds"])} for (a, p, v), x in sorted(
                 per.items(), key=lambda kv: (-len(kv[1]["pcs"]), str(kv[0])))]


def crashes_by_version(client, now: datetime, application: str) -> dict:
    """An application's crash events of the last DAYS days by the version of the part that
    failed (health events): for one Aternity's application usage does not hold, as the Edge
    browser on stage (Nov 2026). Counts only: the PCs that ran each version are not known."""
    per: dict[str, dict] = {}
    kinds: Counter = Counter()
    parts: Counter = Counter()
    for e in client.app_health_events(application, now - timedelta(days=DAYS), now):
        if "crash" not in (e.name or "").lower():
            continue
        x = per.setdefault(e.component_version or "(no version)", {"pcs": set(), "events": 0})
        x["pcs"].add(e.device)
        x["events"] += int(e.count or 1)
        kinds[e.name] += 1
        if e.component:
            parts[e.component] += 1
    rows = sorted(({"version": v, "pcs": len(x["pcs"]), "events": x["events"]} for v, x in per.items()),
                  key=lambda r: (-r["pcs"], -r["events"], order(r["version"])))
    return {"rows": rows, "kinds": [k for k, _ in kinds.most_common()],
            "part": parts.most_common(1)[0][0] if parts else None,
            "pcs": len(set().union(*(x["pcs"] for x in per.values()))) if per else 0}


def _check(a_n, a_of, b_n, b_of, with_it, still: int) -> tuple[str, bool]:
    """One side against the other, in plain counts. "raised": a's share is at least RATIO times
    b's, MIN_PROBLEM PCs or more have the problem, and it would still be RATIO times with
    `still` more PCs with it on b's side (so one or two PCs never decide it). "unsure": RATIO
    times, but one of the other two fails (True: too few PCs with it; False: the "more PCs"
    check). "more": a higher share, under RATIO times. "no": not higher. "none": nothing to
    count on one side."""
    if not a_of or not b_of:
        return "none", False
    if a_n > 0 and a_n * b_of >= RATIO * b_n * a_of:
        few = with_it < MIN_PROBLEM
        holds = a_n * b_of >= RATIO * (b_n + still) * a_of
        return ("raised", False) if holds and not few else ("unsure", few)
    return ("more" if a_n * b_of > b_n * a_of else "no"), False


def judge(app: dict) -> None:
    """Flag 1 (decision 1, as changed on Nov 21 2026: the user could not tell where "its other
    versions: 2 of 515" came from, and one more crash among those 515 would have undone the
    flag). Per kind of PC (`_kinds`), two comparisons a person can see on the page:
    - before and after an update, on the same PCs with the same number of days on each side:
      raised when the PCs with a crash (or hang time) after are RATIO times those before;
    - each version against the version most PCs have (not one that is new this week): raised
      on whichever of the two has RATIO times the other's share of PCs with a crash, of PCs
      with hang time, or of actions rated Major. MIN_PCS PCs on each side.
    Every one also needs MIN_PROBLEM PCs with the problem and, counted in PCs, to hold with
    STILL more of them on the other side (`_check`). Each row keeps its `verdict` per count, for
    the page's words. When every PC listed had a crash (or hang time), app usage holds the
    application only where it failed: nothing of app usage is compared (`only`)."""
    vs, read = app["versions"], app["read"]
    kinds = app.setdefault("kinds", {"all": _kind_rows(vs)})
    app["raised"], app["only"] = [], []
    usage = "usage" in read
    if usage:
        tof = sum(v["pcs"] for v in vs)
        app["only"] = [k for k in ("crash", "hang") if sum(v[k] for v in vs) >= MIN_PROBLEM
                       and sum(v[k] for v in vs) == tof]
    sides = []
    if usage and not app["only"]:  # held only where it failed: no share of either can be compared
        sides += [("crash", "crash", "pcs", "pcs", "crash", STILL), ("hang", "hang", "pcs", "pcs", "hang", STILL)]
    if "actions" in read:
        sides += [("slow", "major", "actions", "action_pcs", "major_pcs", 0)]
    for on, k in kinds.items():
        for u in k["updates"]:
            u["verdict"] = {}
            for kind in ("crash", "hang") if usage and not app["only"] else ():
                n, before = u["after"][kind], u["before"][kind]
                state, few = _check(n, u["pcs"], before, u["pcs"], n, STILL)
                u["verdict"][kind] = {"state": state, "few": few}
                if state == "raised":
                    app["raised"].append({"kind": kind, "version": u["to"], "on": on, "how": "update",
                                          "from": u["from"], "n": n, "of": u["pcs"], "before": before, "pcs": n})
        for v in k["versions"]:
            v["verdict"] = {}
        judged = [v for v in k["versions"] if max(v["pcs"], v["action_pcs"]) >= MIN_PCS]
        ref = next((v for v in judged if not v["new"]), judged[0] if judged else None)
        k["ref"] = ref["version"] if ref else None
        for v in judged:
            if v is ref or v["new"]:
                continue
            for kind, n_key, of_key, pcs_key, prob_key, still in sides:
                if v[pcs_key] < MIN_PCS or ref[pcs_key] < MIN_PCS:
                    continue
                mine, most = (v[n_key], v[of_key]), (ref[n_key], ref[of_key])
                state, few = _check(*mine, *most, v[prob_key], still)
                v["verdict"][kind] = {"state": state, "few": few}
                found = {"kind": kind, "on": on, "ref": ref["version"]}
                if state == "raised":
                    app["raised"].append(found | {"version": v["version"], "how": "version", "n": mine[0],
                                                  "of": mine[1], "ref_n": most[0], "ref_of": most[1],
                                                  "pcs": v[prob_key]})
                if kind not in ref["verdict"] and _check(*most, *mine, ref[prob_key], still)[0] == "raised":
                    ref["verdict"][kind] = {"state": "raised", "few": False, "against": v["version"]}
                    app["raised"].append(found | {"version": ref["version"], "how": "most", "ref": v["version"],
                                                  "n": most[0], "of": most[1], "ref_n": mine[0], "ref_of": mine[1],
                                                  "pcs": ref[prob_key]})


def share_text(n, of) -> str:
    """A share as the page writes it: a whole percentage from 10%, one decimal under it, half
    rounded up (the page does the same sum, so the two never differ). Always beside its counts."""
    if not n or not of:
        return "0%"
    v = n / of * 100
    if v < 0.05:
        return "under 0.1%"
    return f"{math.floor(v + 0.5)}%" if v >= 10 else f"{math.floor(v * 10 + 0.5) / 10:g}%"


DID = {"crash": "crashed", "hang": "stopped responding"}


def flag_text(kind: str, f: dict) -> str:
    """The red flag's sentence: what was compared with what, each share with its counts."""
    a, v = f.get("application"), f.get("version")
    if kind not in ("crash", "hang", "slow"):
        return f.get("text", "")
    on = PLACES.get(f.get("on"), PLACES["all"])
    where = f", {on}" if f.get("on") in ("virtual", "physical") else ""
    if f.get("how") == "update":
        more = "more crashes" if kind == "crash" else "it stops responding more"
        return (f"{a} {v}{where}: {more} after the update from {f['from']}. Of the {f['of']:,} PCs that got it, it "
                f"{DID[kind]} on {f['n']:,} after the update and on {f['before']:,} before it.")
    mine, other = share_text(f["n"], f["of"]), share_text(f["ref_n"], f["ref_of"])
    what = (f"{mine} of its actions were rated {TOP_STATUS} ({f['n']:,} of {f['of']:,}, on {f['pcs']:,} PCs)"
            if kind == "slow" else f"it {DID[kind]} on {mine} of its {on} ({f['n']:,} of {f['of']:,})")
    theirs = f"{other} ({f['ref_n']:,} of {f['ref_of']:,})"
    if f.get("how") == "most":
        return f"{a} {v}{where}, the version most have: {what}. On {f['ref']}: {theirs}."
    return f"{a} {v}{where}: {what}. On the version most have, {f['ref']}: {theirs}."


def program_view(p: dict) -> dict:
    """A program's versions, oldest to newest when they are all numbers, with most / newest /
    oldest and the PCs behind (on a version older than the one most PCs have; decision 2)."""
    vs = [[v, n] for v, n in p["versions"].items()]
    dotted = bool(vs) and all(version_key(v) is not None for v, _ in vs)
    vs.sort(key=lambda x: order(x[0]) if dotted else (-x[1], str(x[0])))
    most = _most(p["versions"])
    out = {"name": p["name"], "type": p["type"], "pcs": p["pcs"], "installs": sum(n for _, n in vs),
           "user": p["user"], "multi": p["multi"], "versions": vs, "dotted": dotted,
           "most": most, "newest": None, "oldest": None, "behind": None}
    if dotted and most is not None:
        out["newest"], out["oldest"] = vs[-1][0], vs[0][0]
        if p["multi"] and p["multi"].get("behind") is not None:  # read PC by PC: a PC with 2 versions once
            out["behind"] = p["multi"]["behind"]
        else:  # each PC has one version (installs = PCs)
            out["behind"] = min(p["pcs"], sum(n for v, n in vs if version_key(v) < version_key(most)))
    return out


def _flags(snap: dict) -> list[dict]:
    """The red flags, one line each, ranked by PCs: each raised version (flag 1), then one
    line per other kind naming the program with the most PCs."""
    flags = []
    seen = set()
    for a in snap.get("versions", {}).values():
        # one flag per version, count and kind of PC: before and after its update first (the same
        # PCs on both sides is the stronger comparison)
        for r in sorted(a["raised"], key=lambda r: r.get("how") != "update"):
            on = r.get("on", "all")
            key = f"{r['kind']}|{a['application']}|{r['version']}" + (f"|{on}" if on != "all" else "")
            if key in seen:
                continue
            seen.add(key)
            f = dict(r, application=a["application"])
            flags.append({"key": key, "kind": r["kind"], "label": KIND[r["kind"]], "pcs": r["pcs"],
                          "text": flag_text(r["kind"], f) + (f" {CUT}" if a.get("partial") else ""),
                          "application": a["application"], "version": r["version"], "on": on})
    progs = [program_view(p) for p in snap.get("programs", {}).values()]
    apps = [p for p in progs if p["type"] != SOFTWARE_OS_UPDATE]
    behind = sorted((p for p in apps if (p["behind"] or 0) >= MIN_PROBLEM), key=lambda p: (-p["behind"], p["name"]))
    if behind:
        b = behind[0]
        flags.append({"key": "behind", "kind": "behind", "label": KIND["behind"], "pcs": b["behind"],
                      "program": b["name"],
                      "text": f"{len(behind):,} programs have {MIN_PROBLEM} PCs or more on a version older than the "
                              f"one most of their PCs have; most: {b['name']}, {b['behind']:,} of {b['pcs']:,} PCs "
                              f"older than {b['most']}."})
    multi = sorted((p for p in apps if p["multi"] and p["multi"]["pcs"]), key=lambda p: (-p["multi"]["pcs"], p["name"]))
    if multi:
        m = multi[0]
        combo = f" ({m['multi']['combos'][0][0]})" if m["multi"]["combos"] else ""
        flags.append({"key": "multi", "kind": "multi", "label": KIND["multi"], "pcs": m["multi"]["pcs"],
                      "program": m["name"],
                      "text": f"{len(multi):,} programs have 2 or more versions on one PC, "
                              f"{sum(p['multi']['pcs'] for p in multi):,} PCs in all; most: {m['name']} on "
                              f"{m['multi']['pcs']:,} of its {m['pcs']:,} PCs{combo}."})
    user = sorted((p for p in progs if p["user"]), key=lambda p: (-p["user"], p["name"]))
    if user:
        u = user[0]
        flags.append({"key": "user", "kind": "user", "label": KIND["user"], "pcs": u["user"], "program": u["name"],
                      "text": f"{len(user):,} programs are installed for one user only, "
                              f"{sum(p['user'] for p in user):,} installs; most: {u['name']} on {u['user']:,} PCs."})
    win = snap.get("windows") or {}
    gaps = [(m, w) for w in win.get("versions", []) for m in w["missing"]]
    if gaps:
        m, w = max(gaps, key=lambda x: (x[0]["without"], x[1]["pcs"]))
        flags.append({"key": "windows", "kind": "windows", "label": KIND["windows"], "pcs": m["without"],
                      "text": f"{len(gaps):,} Windows updates are on most PCs of their Windows version but not on "
                              f"every one; most: {m['update']} not on {m['without']:,} of the {w['pcs']:,} PCs on "
                              f"{w['windows']}" + (f"; {m['instead'][0]['on']:,} of those have "
                                                   f"{m['instead'][0]['update']}." if m["instead"] else ".")
                              + (f" {CUT}" if win.get("partial") else "")})
    few = [p for p in apps if p["pcs"] <= FEW_PCS]
    if few:
        flags.append({"key": "few", "kind": "few", "label": KIND["few"], "pcs": max(p["pcs"] for p in few),
                      "text": f"{len(few):,} of the {len(apps):,} programs are on {FEW_PCS} PCs or fewer."})
    flags.sort(key=lambda f: (-f["pcs"], list(KIND).index(f["kind"]), f["key"]))
    return flags


def read_snapshot(client, cfg, now: datetime, local, say=lambda s: None) -> dict:
    """The whole daily read: counts and names only (no PC or user names)."""
    notes: list[str] = []
    q = _Reads(client, notes)
    progs, size, listed = _installed(q, say)
    gone = _retired(q, cfg, listed, say)
    if gone and _leave_out(q, progs, gone, say):  # only PCs still in Aternity's device list are counted
        size.update(_sizes(progs, size["user_programs"] is not None), pcs=len(listed) - len(gone), left_out=len(gone))
    else:
        gone = set()
    _multi(q, progs, say, gone)
    snap = {"as_of": now.isoformat(), "date": local.day(now).isoformat(), "list": size, "programs": progs,
            "windows": _windows(q, cfg, local, now, say), "versions": _versions(q, now, say), "notes": notes,
            "days": DAYS}
    snap["crashes"] = _crashes(q, now, say)
    snap["usage"] = q.usage  # application -> crashes in app usage, every application
    snap["crash_names"] = q.crash_names
    # Aternity down: the list itself did not come. Not a day's counts (keep_reading tries again soon)
    snap["unread"] = q.error if size is None else ""
    snap["flags"] = _flags(snap)
    snap["gone"] = sorted(gone)  # for the lists opened by a click; `Software.save` keeps it out of the file
    return snap


# -- the reader and its saved days ------------------------------------------------
class Software:
    def __init__(self, zone: str = "", store: str | os.PathLike | None = None,
                 clock=lambda: datetime.now(timezone.utc)):
        self.zone, self.zone_label, _ = zone_of(zone)
        self.clock = clock  # what "today" is when old files are deleted: the tests give their own day
        self.store = Path(store) / "software" if store else None
        self.snap: dict | None = None  # the latest read
        self.gone: set[str] = set()  # PCs left out of the latest read (no longer in Aternity's device list):
        # in memory only (no PC name is saved), so after a restart the lists opened by a click leave
        # them out again from the first read on
        self.saved: dict[str, dict] = {}  # date -> {flag key: PCs}, from the files
        self.status = "Starting"
        self.off: str | None = None
        self.note: str | None = None  # the files could not be read or written
        self.reading = False
        self.started = False
        self.lock = threading.Lock()
        self.load()

    def local(self, t: datetime) -> datetime:
        return t.astimezone(self.zone)

    def day(self, t: datetime) -> date:
        return self.local(t).date()

    def monday(self, t: datetime) -> date:
        d = self.day(t)
        return d - timedelta(days=d.weekday())

    def load(self) -> None:
        if not self.store:
            return
        bad = []
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            cut = (self.clock().astimezone(self.zone).date() - timedelta(days=KEEP_DAYS)).isoformat()
            latest = None
            for path in sorted(self.store.glob("*.json")):
                try:  # one unreadable day no longer stops the others (and the latest read) loading
                    if path.stem < cut:
                        path.unlink()
                        continue
                    snap = json.loads(path.read_text())
                    self.saved[path.stem] = {f["key"]: f["pcs"] for f in snap.get("flags", [])}
                    latest = snap
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    bad.append(f"{path.name} ({short_error(exc)})")
            self.snap = latest
        except OSError as exc:
            bad.append(f"the folder ({short_error(exc)})")
        if bad:
            self.note = (f"Saved days in {self.store} could not be read: {', '.join(bad[:3])}; those days are not "
                         "in each flag's days. The folder must be writable by the container (uid 1000).")
            log.warning("Software: %s", self.note)

    def save(self, snap: dict) -> None:
        gone = snap.pop("gone", None)  # PC names: never in the saved day
        with self.lock:
            if gone is not None:
                self.gone = set(gone)
            self.snap = snap
            self.saved[snap["date"]] = {f["key"]: f["pcs"] for f in snap["flags"]}
        if not self.store:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            tmp = self.store / f".{snap['date']}.tmp"
            tmp.write_text(json.dumps(snap, sort_keys=True))
            os.replace(tmp, self.store / f"{snap['date']}.json")
            self.note = None
        except OSError as exc:
            self.note = (f"Today's software counts could not be saved in {self.store} ({short_error(exc)}). The "
                         "folder must be writable by the container (uid 1000).")
            log.warning("Software: %s", self.note)

    def view(self) -> dict:
        """The tab: the list's size, the red flags (new? their days), each kind's section."""
        snap = self.snap
        out = {"ready": snap is not None, "reading": self.reading, "status": self.status, "off": self.off,
               "zone": self.zone_label}
        if snap is None:
            return out
        before = [d for d in sorted(self.saved) if d < snap["date"]]
        # "new" is against the latest day before that has any flag: a day saved empty while Aternity
        # was down (stage, Nov 10) made all 21 flags of the next day "new"
        full = [d for d in before if self.saved[d]]
        prev = self.saved.get(full[-1]) if full else None
        days = [d for d in sorted(self.saved) if d <= snap["date"]][-KEEP_DAYS:]
        flags = []
        for f in snap["flags"]:
            g = dict(f, new=prev is not None and f["key"] not in prev,
                     days=[{"day": d, "pcs": self.saved[d].get(f["key"], 0)} for d in days])
            flags.append(g)
        progs = [program_view(p) for p in snap["programs"].values()]
        apps = [p for p in progs if p["type"] != SOFTWARE_OS_UPDATE]
        tail = []
        for lo, hi in TAIL:
            names = sorted((p["name"] for p in apps if p["pcs"] >= lo and (hi is None or p["pcs"] <= hi)),
                           key=str.lower)
            tail.append({"label": str(lo) if lo == hi else f"{lo:,}-{hi:,}" if hi else f"over {lo - 1:,}",
                         "programs": len(names), "names": names})
        notes = list(snap.get("notes", [])) + ([self.note] if self.note else [])
        out.update({
            "as_of": snap["as_of"], "as_of_local": self.local(datetime.fromisoformat(snap["as_of"])).strftime(
                "%b %d, %H:%M"), "date": snap["date"], "list": snap["list"], "flags": flags, "notes": notes,
            "days": snap.get("days", DAYS),
            "rules": {"ratio": RATIO, "min_pcs": MIN_PCS, "min_problem": MIN_PROBLEM, "few": FEW_PCS, "still": STILL,
                      "update_days": UPDATE_DAYS},
            "behind": sorted((p for p in apps if p["behind"]), key=lambda p: (-p["behind"], p["name"]))[:100],
            "user": [{"name": p["name"], "pcs": p["user"], "of": p["pcs"]} for p in
                     sorted((p for p in progs if p["user"]), key=lambda p: (-p["user"], p["name"]))],
            "multi": [{"name": p["name"], "pcs": p["multi"]["pcs"], "of": p["pcs"],
                       "extra": p["installs"] - p["pcs"], "combos": p["multi"]["combos"]} for p in
                      sorted((p for p in apps if p["multi"] and p["multi"]["pcs"]),
                             key=lambda p: (-p["multi"]["pcs"], p["name"]))],
            "windows": snap.get("windows"), "versions": _versions_view(snap.get("versions", {})), "tail": tail,
            "programs": sorted((p["name"] for p in apps), key=str.lower),
            "applications": sorted(snap.get("versions", {}), key=str.lower)})
        return out

    def program(self, name: str) -> dict | None:
        p = (self.snap or {}).get("programs", {}).get(name)
        return program_view(p) if p else None


def _versions_view(apps: dict) -> list[dict]:
    """Applications with a raised version first (most PCs with the problem), then the others."""
    out = []
    for a in apps.values():
        if "kinds" not in a or any("how" not in r for r in a.get("raised", [])):
            a = dict(a, kinds={"all": _kind_rows(a["versions"])})  # a day saved under the rule before: judged again
            judge(a)
        raised = {(r["version"], r["kind"]) for r in a["raised"]}
        vs = [dict(v, raised=sorted(k for (ver, k) in raised if ver == v["version"])) for v in a["versions"]]
        out.append({"application": a["application"], "read": a["read"], "only": a["only"], "versions": vs,
                    "totals": a.get("totals"), "kinds": a["kinds"], "found": a["raised"],
                    "raised": len(a["raised"]), "problem": sum(v["crash"] + v["hang"] + v["major_pcs"] for v in vs)})
    out.sort(key=lambda a: (-a["raised"], -a["problem"], a["application"].lower()))
    return out


def keep_reading(sw: Software, make_client, cfg_of, sleep, clock=lambda: datetime.now(timezone.utc)) -> None:
    """Runs in a background thread: the whole read at start, then every READ_EVERY_H hours."""
    while True:
        try:
            client = make_client()
        except Exception as exc:  # a broken config.yaml: tried again (it once stopped the reader until a restart)
            sw.off = f"The settings could not be read ({short_error(exc)}); trying again in {RETRY_MIN} minutes."
            log.warning("Software: %s", sw.off)
            sleep(RETRY_MIN * 60)
            continue
        sw.off = None
        if client is None:
            sw.off = "Aternity is not set, so there is nothing to show (aternity.base_url in config.yaml)."
            log.info("Software: %s", sw.off)
            return
        sw.reading, started, wait = True, clock(), READ_EVERY_H * 3600

        def say(text: str) -> None:
            sw.status = text

        try:
            snap = read_snapshot(client, cfg_of(), started, sw, say)
            if snap["unread"]:  # keep the last good read on the tab; saving this one would empty today's flags
                sw.status = (f"Aternity's installed software list could not be read ({snap['unread']}); "
                             f"trying again in {RETRY_MIN} minutes.")
                log.warning("Software: %s", sw.status)
                wait = RETRY_MIN * 60
                continue
            sw.save(snap)
            sw.status = "Read"
            s = snap["list"] or {}
            log.info("Software: read in %.0f s: %s PCs, %s programs, %d red flags%s", (clock() - started).total_seconds(),
                     f"{s['pcs']:,}" if s.get("pcs") is not None else "no", f"{s.get('programs', 0):,}",
                     len(snap["flags"]), f"; {len(snap['notes'])} notes" if snap["notes"] else "")
            if s.get("left_out"):
                log.info("Software: %s more PCs with installed software are no longer in Aternity's device list: "
                         "left out of every count", f"{s['left_out']:,}")
            for n in snap["notes"]:
                log.info("Software: note: %s", n)
        except Exception as exc:  # shown on the tab; the next read tries again
            sw.status = f"The software read failed ({short_error(exc)}); trying again in {READ_EVERY_H} hours."
            log.exception("Software: read failed")
        finally:  # also after `continue`: the wait is here, outside the try
            sw.reading = False
            sleep(wait)


# -- reads made by a click ------------------------------------------------------------
PC_LIST = ("name", "version", "vendor", "type", "scope", "changed")  # a PC's own list (Ask anything)


def pc_list_columns(q: _Reads) -> list[str]:
    """The columns of a PC's own list that this Aternity publishes (PC_LIST)."""
    return [SOFTWARE_COLUMNS[k] for k in PC_LIST if q.has(SOFTWARE_SOURCE, SOFTWARE_COLUMNS[k])]


def pc_counts(rows: list[dict], typed: bool = True) -> dict:
    """One PC's own list in numbers: different programs and Windows updates (by name), and rows
    (a program can have several: versions, users)."""
    c = SOFTWARE_COLUMNS
    win = [r for r in rows if typed and r.get(c["type"]) == SOFTWARE_OS_UPDATE]
    other = [r for r in rows if not (typed and r.get(c["type"]) == SOFTWARE_OS_UPDATE)]
    return {"programs": len({r.get(c["name"]) for r in other if r.get(c["name"])}),
            "windows_updates": len({r.get(c["name"]) for r in win if r.get(c["name"])}),
            "rows": len(other), "update_rows": len(win)}


def programs_per_pc(client, notes: list[str], gone=frozenset()) -> dict | None:
    """Programs on every PC ({pc: {"programs", "windows_updates"}}), and how it is known.
    Every PC's list in one read is refused by the live Aternity ("Returned data too long",
    stage, Nov 2026), so Aternity adds INSTALLED_SW_COUNT up per PC (one read of ~2 rows a
    PC). What it counts is not documented: on stage it was 511 for a PC whose list has 619
    rows, so not rows. It is used only when it equals, on the PCs with the most and the
    fewest, the different program names in their own lists (or their rows), said each time;
    else the numbers are said and no count is given."""
    S, c = SOFTWARE_SOURCE, SOFTWARE_COLUMNS
    D, T, K = c["device"], c["type"], c["count"]
    q = _Reads(client, notes)
    if q.schema is not None and S not in q.schema:
        notes.append(f"{S} is not published by this Aternity: nothing on installed software can be read.")
        return None
    if not q.has(S, K):
        notes.append(f"{S} has no {K}, so programs cannot be counted per PC.")
        return None
    typed = q.has(S, T)
    mark = len(client.warnings)
    summed = q.rows(S, "Programs per PC, added up by Aternity", select=[D, T, K] if typed else [D, K],
                    top=LIST_ROWS * 3)
    if not summed or len(client.warnings) > mark or q.cut:  # PCs left out: no ranking of them
        return None
    per: dict = {}
    for r in summed:
        if r.get(D) and str(r.get(D)) not in gone:  # PCs no longer in Aternity's device list: left out
            p = per.setdefault(str(r.get(D)), {"programs": 0, "windows_updates": 0})
            p["windows_updates" if typed and r.get(T) == SOFTWARE_OS_UPDATE else "programs"] += _int(r.get(K))
    ranked = sorted(per, key=lambda d: (-sum(per[d].values()), d))
    checked = []
    for d in dict.fromkeys((ranked[0], ranked[-1])):
        own = q.rows(S, f"{d}'s own list", filter=f"{D} eq {odata_quote(d)}", select=pc_list_columns(q),
                     top=LIST_ROWS)
        if own is None:
            return None
        checked.append((d, pc_counts(own, typed)))
    names = all(per[d] == {k: n[k] for k in ("programs", "windows_updates")} for d, n in checked)
    rows = all(per[d] == {"programs": n["rows"], "windows_updates": n["update_rows"]} for d, n in checked)
    pcs = " and ".join(d for d, _ in checked)
    if names:
        return {"pcs": per, "counts": "different program names",
                "how": f"added up by Aternity (INSTALLED_SW_COUNT): the different program names, as in the own "
                       f"lists of {pcs}"}
    if rows:
        return {"pcs": per, "counts": "rows of the list",
                "how": f"added up by Aternity (INSTALLED_SW_COUNT): the rows, as in the own lists of {pcs}"}
    notes.append("Aternity's count per PC is neither the program names nor the rows of the PC's own list, so no "
                 "count is given: " + "; ".join(
                     f"{d}: Aternity {sum(per[d].values()):,}, its list {n['programs'] + n['windows_updates']:,} "
                     f"names in {n['rows'] + n['update_rows']:,} rows" for d, n in checked) + ".")
    return None


def pcs_with_program(client, name: str, version: str | None, prefix: bool = False, gone=frozenset()) -> dict:
    """The PCs with a program (at one version, or `prefix`: every version starting with it),
    read now; each PC with its versions of the program. The PCs in `gone` (`Software.gone`: no
    longer in Aternity's device list) are left out, as in the daily read's counts."""
    c = SOFTWARE_COLUMNS
    flt = f"{c['name']} eq {odata_quote(name)}"
    if version not in (None, NO_VERSION) and not prefix:
        flt += f" and {c['version']} eq {odata_quote(version)}"
    rows = client.query(SOFTWARE_SOURCE, filter=flt, select=[c["device"], c["version"]], top=LIST_ROWS)
    if version == NO_VERSION:
        rows = [r for r in rows if r.get(c["version"]) is None]
    elif prefix and version is not None:
        rows = [r for r in rows if r.get(c["version"]) is not None and starts_with(r.get(c["version"]), version)]
    per: dict[str, set] = {}
    for r in rows:
        if r.get(c["device"]) and str(r.get(c["device"])) not in gone:
            per.setdefault(str(r.get(c["device"])), set()).add(shown(r.get(c["version"])))
    devs = sorted(per, key=str.lower)
    what = name if version is None else f"{name} {version}.x" if prefix else f"{name} {shown(version)}"
    return {"what": what, "total": len(devs), "problem": None,
            "rows": [{"device": d, "version": ", ".join(sorted(per[d], key=order))} for d in devs[:PCS_SHOWN]]}


def pcs_with_problem(client, now: datetime, application: str, version: str, problem: str, kind: str = "") -> dict:
    """The PCs with a crash, hang time or a Major action on one application version over the
    last DAYS days, read now: user, days with it, the last of them (for Investigate). `kind`
    (virtual / physical): only the PCs Aternity marks so, as the count that was clicked."""
    start = now - timedelta(days=DAYS)
    key = "actions" if problem == "slow" else "usage"
    s = VERSION_SOURCES[key]
    flt = f"{_span(s, start, now)} and {s['application']} eq {odata_quote(application)} and " \
          f"{s['version']} eq {odata_quote(version)}"
    if problem == "slow":
        flt += f" and {s['status']} eq {odata_quote(TOP_STATUS)}"
        count = s["volume"]
    else:
        count = s["crashes"] if problem == "crash" else s["hang_time"]
        flt += f" and {count} gt 0"
    sel = [s["device"], s["time"], count]
    kind = kind if kind in ("virtual", "physical") else ""
    tries = ([[s["username"], s["virtual"]], [s["virtual"]]] if kind else []) + [[s["username"]], []]
    for more in tries:  # the user and the virtual mark, when Aternity publishes them on this source
        try:
            rows = client.query(s["entity"], filter=flt, select=sel + more, top=LIST_ROWS)
            break
        except Exception:
            if not more:
                raise
    marked = s["virtual"] in more
    per: dict = {}
    for r in rows:
        d, t = r.get(s["device"]), parse_time(r.get(s["time"]))
        if not d or t is None:
            continue
        x = per.setdefault(str(d), {"device": str(d), "username": None, "days": set(), "count": 0.0, "last": t,
                                    "virtual": False})
        x["virtual"] = x["virtual"] or (marked and is_virtual(r.get(s["virtual"])))
        x["days"].add(t.date())
        x["last"] = max(x["last"], t)
        if problem != "hang":  # hang time has no documented unit: never shown
            x["count"] += to_float(r.get(count)) or (1 if problem == "slow" else 0)
        if r.get(s.get("username")):
            x["username"] = str(r.get(s["username"]))
    if kind and marked:
        per = {d: x for d, x in per.items() if x["virtual"] == (kind == "virtual")}
    out = sorted(per.values(), key=lambda x: (-len(x["days"]), -x["last"].timestamp(), x["device"].lower()))
    words = {"crash": "a crash", "hang": "hang time", "slow": f"a {TOP_STATUS} action"}[problem]
    who = PLACES[kind] if kind and marked else "PCs"
    unmarked = " (Aternity gave no virtual / physical mark here: every PC is listed)" if kind and not marked else ""
    return {"what": f"{application} {version}: {who} with {words}, last {DAYS} days{unmarked}", "total": len(out),
            "problem": problem,
            "rows": [{"device": x["device"], "username": x["username"], "days": len(x["days"]),
                      "count": int(round(x["count"])) if problem != "hang" else None,
                      "last": x["last"].isoformat()} for x in out[:PCS_SHOWN]]}


def pcs_after_update(client, now: datetime, application: str, version: str, problem: str, kind: str = "") -> dict:
    """The PCs behind "more after the update": those whose version of the application changed
    once to `version` in the last UPDATE_DAYS days and had a crash (or hang time) on their days
    after it, read now by the daily read's own rule (`_changed_once`: as many days after as
    before). `kind` as in `pcs_with_problem`."""
    u = VERSION_SOURCES["usage"]
    kind = kind if kind in ("virtual", "physical") else ""
    sel = [u["device"], u["time"], u["version"], u["crashes"], u["hang_time"]]
    flt = f"{_span(u, now - timedelta(days=UPDATE_DAYS), now)} and {u['application']} eq {odata_quote(application)}"
    for more in ([[u["virtual"]]] if kind else []) + [[]]:
        try:
            rows = client.query(u["entity"], filter=flt, select=sel + more, top=LIST_ROWS * 5)
            break
        except Exception:
            if not more:
                raise
    marked = bool(more)
    days: dict = {}
    virtual = set()
    for r in rows:
        d, t, v = r.get(u["device"]), parse_time(r.get(u["time"])), r.get(u["version"])
        if d is None or t is None or v is None:
            continue
        crashes = to_float(r.get(u["crashes"])) or 0
        day = days.setdefault(str(d), {}).setdefault(t.date(), [set(), False, False, True, 0.0, t])
        day[0].add(str(v))
        day[1], day[2] = day[1] or crashes > 0, day[2] or (to_float(r.get(u["hang_time"])) or 0) > 0
        day[4] += crashes
        if marked and is_virtual(r.get(u["virtual"])):
            virtual.add(str(d))
    hit_at = 1 if problem == "crash" else 2
    out = []
    for d, by in days.items():
        if kind and marked and (d in virtual) != (kind == "virtual"):
            continue
        got = _changed_once(by)
        hit = [x for _, x in got[4] if x[hit_at]] if got and got[1] == version else []
        if hit:
            out.append({"device": d, "username": None, "days": len(hit),
                        "count": int(round(sum(x[4] for x in hit))) if problem == "crash" else None,
                        "last": max(x[5] for x in hit)})
    out.sort(key=lambda x: (-x["days"], -x["last"].timestamp(), x["device"].lower()))
    words = "a crash" if problem == "crash" else "hang time"
    who = PLACES[kind] if kind and marked else "PCs"
    return {"what": f"{application}: {who} with {words} after their update to {version}, last {UPDATE_DAYS} days",
            "total": len(out), "problem": problem,
            "rows": [dict(x, last=x["last"].isoformat()) for x in out[:PCS_SHOWN]]}


def missing_on(snap: dict | None, update: str, windows: str | None = None) -> tuple[list[str], list[str]]:
    """Where the latest read lists a Windows update as not on every PC: its Windows versions
    (or only `windows`), and the updates the PCs without it have instead."""
    wins, instead = [], []
    for w in ((snap or {}).get("windows") or {}).get("versions", []):
        if windows and w["windows"] != windows:
            continue
        for m in w["missing"]:
            if m["update"] == update:
                wins.append(w["windows"])
                instead += [y["update"] for y in m["instead"] if y["update"] not in instead]
    return wins, instead


def pcs_without_update(client, cfg, update: str, windows: list[str], instead=()) -> dict:
    """The PCs on these Windows versions without a Windows update, read now as the daily read
    counts them (the same names, so the total is its "not on N"): for each, which of `instead`
    it has (a later update may be in its place) and how many Windows updates it lists. The
    PCs with none of `instead` first: the ones to look at."""
    inv, dev = cfg.entities.get("inventory"), cfg.fields.get("inventory", {}).get("device")
    o, c = OS_COLUMNS, SOFTWARE_COLUMNS
    notes: list[str] = []
    q = _Reads(client, notes)
    cols = [x for x in (o["name"], o["version"]) if q.has(inv, x)]
    pcs = client.query(inv, select=[dev] + cols, top=LIST_ROWS)
    on = {r[dev]: windows_of(r) for r in pcs if r.get(dev) and windows_of(r) in windows}
    ups = client.query(SOFTWARE_SOURCE, filter=f"{c['type']} eq {odata_quote(SOFTWARE_OS_UPDATE)}",
                       select=[c["device"], c["name"]], top=LIST_ROWS * 4)
    if len(pcs) >= LIST_ROWS or len(ups) >= LIST_ROWS * 4:
        notes.append("Aternity's reply was cut short, so this list may be partial.")
    has: dict = {}
    for r in ups:
        has.setdefault(r.get(c["device"]), set()).add(str(r.get(c["name"])))
    rows = [{"device": str(d), "windows": w, "instead": [x for x in instead if x in has.get(d, ())],
             "updates": len(has.get(d, ()))} for d, w in on.items() if update not in has.get(d, ())]
    rows.sort(key=lambda x: (bool(x["instead"]), x["device"].lower()))
    return {"what": f"PCs on {', '.join(windows)} without {update}", "total": len(rows), "problem": "update",
            "update": update, "instead": list(instead), "with_instead": sum(1 for x in rows if x["instead"]),
            "rows": rows[:PCS_SHOWN], "notes": notes}


def compare(client, cfg, now: datetime, application: str, a: str | None = None, b: str | None = None) -> dict:
    """Two versions of an application side by side on every kind of issue Aternity records,
    over the last DAYS days, as shares of the PCs (or actions) on each: PCs with a crash, with
    hang time, with each kind of health event of the application (matched by the version the
    PC ran), actions rated Major (overall and on the busiest activities), and how many of
    each version's PCs are virtual desktops."""
    start = now - timedelta(days=DAYS)
    notes: list[str] = []
    q = _Reads(client, notes)
    u, ac = VERSION_SOURCES["usage"], VERSION_SOURCES["actions"]
    virt = u.get("virtual") if q.has(u["entity"], u.get("virtual")) else None
    rows = q.rows(u["entity"], f"{application}, per PC and version",
                  select=[u["device"], u["version"], u["crashes"], u["hang_time"]] + ([virt] if virt else []),
                  filter=f"{_span(u, start, now)} and {u['application']} eq {odata_quote(application)}",
                  top=LIST_ROWS * 2) or []
    per: dict = {}
    pc_versions: dict = {}
    for r in rows:
        v, d = r.get(u["version"]), r.get(u["device"])
        if v is None or d is None:
            continue
        x = per.setdefault(str(v), _blank())
        x["pcs"].add(d)
        pc_versions.setdefault(d, set()).add(str(v))
        if (to_float(r.get(u["crashes"])) or 0) > 0:
            x["crash"].add(d)
        if (to_float(r.get(u["hang_time"])) or 0) > 0:
            x["hang"].add(d)
        if virt and is_virtual(r.get(virt)):
            x["virtual"].add(d)
    by_act = q.has(ac["entity"], ac.get("activity"))
    acts = q.rows(ac["entity"], f"{application} actions per version",
                  select=([ac["activity"]] if by_act else []) + [ac["version"], ac["status"], ac["volume"]],
                  filter=f"{_span(ac, start, now)} and {ac['application']} eq {odata_quote(application)}",
                  top=AGG_ROWS) if q.has(ac["entity"], ac["version"]) else None
    act: dict = {}
    for r in acts or []:
        v = r.get(ac["version"])
        if v is None:
            continue
        n = to_float(r.get(ac["volume"]))
        n = 1.0 if n is None else n
        x = per.setdefault(str(v), _blank())
        x["actions"] += n
        y = act.setdefault(str(r.get(ac["activity"])), {}).setdefault(str(v), [0.0, 0.0]) if by_act else [0.0, 0.0]
        y[0] += n
        if r.get(ac["status"]) == TOP_STATUS:
            x["major"] += n
            y[1] += n
    versions = sorted(per, key=lambda v: (-max(len(per[v]["pcs"]), per[v]["actions"]), order(v)))
    if len(versions) < 2:
        return {"application": application, "versions": versions, "a": None, "b": None, "lines": [],
                "notes": notes + [f"{application} ran on {len(versions)} version(s) in the last {DAYS} days: "
                                  "nothing to compare."], "days": DAYS}
    a = version_of(a, per) or versions[0]
    b = version_of(b, per)
    b = b if b and b != a else next(v for v in versions if v != a)
    lines = []

    def line(what: str, group: str, na, ofa, nb, ofb, unit: str = "PCs") -> None:
        lines.append({"what": what, "group": group, "unit": unit, "a": [int(round(na)), int(round(ofa))],
                      "b": [int(round(nb)), int(round(ofb))]})

    A, B = per[a], per[b]
    if A["pcs"] or B["pcs"]:
        line("PCs with a crash", "usage", len(A["crash"]), len(A["pcs"]), len(B["crash"]), len(B["pcs"]))
        line("PCs with hang time", "usage", len(A["hang"]), len(A["pcs"]), len(B["hang"]), len(B["pcs"]))
        events = client.app_health_events(application, start, now)
        kinds: dict = {}
        for e in events:
            for v in pc_versions.get(e.device, ()):
                if v in (a, b):
                    kinds.setdefault(e.name or "(no name)", {a: set(), b: set()})[v].add(e.device)
        for k, by in sorted(kinds.items(), key=lambda kv: (-len(kv[1][a]) - len(kv[1][b]), kv[0]))[:5]:
            line(f"PCs with {k}", "events", len(by[a]), len(A["pcs"]), len(by[b]), len(B["pcs"]))
        if client.warnings:
            notes += [w for w in dict.fromkeys(client.warnings) if "health" in w.lower() and w not in notes]
    if A["actions"] or B["actions"]:
        line(f"Actions rated {TOP_STATUS}", "actions", A["major"], A["actions"], B["major"], B["actions"], "actions")
        busy = sorted(act, key=lambda k: (-sum(x[0] for x in act[k].values()), k))[:5]
        for k in busy:
            ya, yb = act[k].get(a, [0, 0]), act[k].get(b, [0, 0])
            line(k, "activity", ya[1], ya[0], yb[1], yb[0], "actions")
    kind = None
    if virt and A["pcs"] and B["pcs"]:
        kind = {"a": [len(A["virtual"]), len(A["pcs"])], "b": [len(B["virtual"]), len(B["pcs"])]}
    table = [{"version": v, "pcs": len(per[v]["pcs"]), "crash": len(per[v]["crash"]), "hang": len(per[v]["hang"]),
              "virtual": len(per[v]["virtual"]) if virt else None, "actions": int(round(per[v]["actions"])),
              "major": int(round(per[v]["major"]))} for v in versions]
    return {"application": application, "a": a, "b": b, "versions": versions, "table": table, "lines": lines,
            "kind": kind, "notes": notes, "days": DAYS}


def before_after(client, now: datetime, application: str) -> dict:
    """Before and after an update, on the same PCs: each PC whose version of the application
    changed once in the last UPDATE_DAYS days (Aternity's own version, day by day), with its
    days on each side, and the days with a crash or hang time on each side. Plain counts of
    PC-days, as each side has its own number of days."""
    u = VERSION_SOURCES["usage"]
    start = now - timedelta(days=UPDATE_DAYS)
    mark, top = len(client.warnings), LIST_ROWS * 5
    rows = client.query(u["entity"], select=[u["device"], u["time"], u["version"], u["crashes"], u["hang_time"]],
                        filter=f"{_span(u, start, now)} and {u['application']} eq {odata_quote(application)}",
                        top=top)
    cut = _partial(client, mark, rows, top)
    days: dict = {}
    for r in rows:
        d, t, v = r.get(u["device"]), parse_time(r.get(u["time"])), r.get(u["version"])
        if d is None or t is None or v is None:
            continue
        x = days.setdefault(d, {}).setdefault(t.date(), [set(), 0, 0])
        x[0].add(str(v))
        x[1] += (to_float(r.get(u["crashes"])) or 0) > 0
        x[2] += (to_float(r.get(u["hang_time"])) or 0) > 0
    pairs: dict = {}
    for d, by in days.items():
        seq = sorted(by.items())
        if any(len(x[0]) > 1 for _, x in seq):
            continue  # 2 versions on one day: the day of the change is not on either side
        vs = [next(iter(x[0])) for _, x in seq]
        changes = [i for i in range(1, len(vs)) if vs[i] != vs[i - 1]]
        if len(changes) != 1:
            continue
        i = changes[0]
        p = pairs.setdefault((vs[0], vs[i]), {"pcs": 0, "before": [0, 0, 0], "after": [0, 0, 0],
                                              "crash_pcs": [0, 0], "hang_pcs": [0, 0], "first": seq[i][0],
                                              "last": seq[i][0]})
        p["pcs"] += 1
        p["first"], p["last"] = min(p["first"], seq[i][0]), max(p["last"], seq[i][0])
        for side, part, k in (("before", seq[:i], 0), ("after", seq[i:], 1)):
            p[side][0] += len(part)
            p[side][1] += sum(1 for _, x in part if x[1])
            p[side][2] += sum(1 for _, x in part if x[2])
            p["crash_pcs"][k] += any(x[1] for _, x in part)
            p["hang_pcs"][k] += any(x[2] for _, x in part)
    out = sorted(({"from": f, "to": t, **p, "first": p["first"].isoformat(), "last": p["last"].isoformat()}
                  for (f, t), p in pairs.items()), key=lambda p: (-p["pcs"], order(p["to"])))
    every = rows and all((to_float(r.get(u["crashes"])) or 0) > 0 for r in rows)
    notes = [f"Every day listed for {application} had a crash: app usage holds it only where it failed, so before "
             "and after cannot be compared."] if every else []
    if cut:
        notes.insert(0, f"{CUT} Read: {len(rows):,} rows.")
    return {"application": application, "days": UPDATE_DAYS, "pairs": out[:5], "notes": notes}


def split_by_program(client, now: datetime, application: str, program: str, problem: str = "crash") -> dict:
    """An application's crashes, hang time or Major actions over the last DAYS days, on the PCs
    of each version of an installed program (PCs matched by name; a PC with 2 versions of the
    program counts under each), and on the PCs without it in the installed list. Never a red
    flag: split by any of the installed programs, some differ by chance (docs/software-tab.md)."""
    c = SOFTWARE_COLUMNS
    problem = problem if problem in ("crash", "hang", "slow") else "crash"
    mark = len(client.warnings)
    inst = client.query(SOFTWARE_SOURCE, filter=f"{c['name']} eq {odata_quote(program)}",
                        select=[c["device"], c["version"]], top=LIST_ROWS)
    cut = _partial(client, mark, inst, LIST_ROWS)
    has: dict[str, set] = {}
    for r in inst:
        if r.get(c["device"]):
            has.setdefault(str(r.get(c["device"])).lower(), set()).add(shown(r.get(c["version"])))
    start, notes = now - timedelta(days=DAYS), []
    s = VERSION_SOURCES["actions" if problem == "slow" else "usage"]
    flt = f"{_span(s, start, now)} and {s['application']} eq {odata_quote(application)}"
    per: dict[str, list] = {}  # PC -> [ran, with the problem, actions, Major actions]
    mark = len(client.warnings)
    if problem == "slow":
        rows = client.query(s["entity"], filter=flt, select=[s["device"], s["status"], s["volume"]], top=LIST_ROWS)
        for r in rows:
            if not r.get(s["device"]):
                continue
            x = per.setdefault(str(r.get(s["device"])).lower(), [1, 0, 0.0, 0.0])
            k = to_float(r.get(s["volume"]))
            k = 1.0 if k is None else k
            x[2] += k
            if r.get(s["status"]) == TOP_STATUS:
                x[1], x[3] = 1, x[3] + k
    else:
        count = s["crashes"] if problem == "crash" else s["hang_time"]
        rows = client.query(s["entity"], filter=flt, select=[s["device"], count], top=LIST_ROWS)
        for r in rows:
            if r.get(s["device"]):
                x = per.setdefault(str(r.get(s["device"])).lower(), [1, 0, 0.0, 0.0])
                x[1] = max(x[1], int((to_float(r.get(count)) or 0) > 0))
        if per and all(x[1] for x in per.values()):
            notes.append(f"Every PC listed for {application} had {'a crash' if problem == 'crash' else 'hang time'}: "
                         "app usage holds it only where it failed, so the PCs that ran it without one are not known.")
    if cut or _partial(client, mark, rows, LIST_ROWS):
        notes.insert(0, CUT)
    groups: dict[str, list] = {}
    for d, x in per.items():
        for v in has.get(d) or ["without"]:
            g = groups.setdefault(v, [0, 0, 0.0, 0.0])
            for i in range(4):
                g[i] += x[i]
    out = [{"version": v, "pcs": g[0], "with": g[1], "actions": int(round(g[2])), "major": int(round(g[3]))}
           for v, g in groups.items() if v != "without"]
    out.sort(key=lambda r: (-r["pcs"], order(r["version"])))
    if "without" in groups:
        g = groups["without"]
        out.append({"version": None, "pcs": g[0], "with": g[1], "actions": int(round(g[2])), "major": int(round(g[3]))})
    if not inst:
        notes.append(f"No PC has a program named {program} in the installed list.")
    if not per:
        notes.append(f"No {application} rows in the last {DAYS} days: names are as Aternity writes them.")
    return {"application": application, "program": program, "problem": problem, "days": DAYS, "rows": out,
            "notes": notes, "pcs": len(per)}


# -- the summary for management ----------------------------------------------------------
SUMMARY_FLAGS = 10  # red flags given to the AI, most PCs first
SUMMARY = ("You write for the company's IT management, from the red flags of its PCs' installed software and "
           "application versions, as Aternity lists them. Write up to 5 short sentences of plain text (no lists, "
           "no markdown): what needs attention first and why, naming the program, version or Windows update. Use "
           "only numbers given, written exactly as given; never work out a new number, a percentage or a ratio, "
           "and never call a number high or low yourself. Say when a flag is new since the day before. End with "
           "one next step.")


def facts(view: dict) -> str:
    """What the summary may say: the list's size and the red flags, as the page shows them."""
    s = view.get("list") or {}
    head = (f"As of {view.get('as_of_local')}: {s.get('pcs') or 0:,} PCs, {s.get('programs') or 0:,} programs, "
            f"{s.get('installs') or 0:,} installs.")
    return "\n".join([head] + [f"- {f['label']}{' (new since the day before)' if f.get('new') else ''}: {f['text']}"
                               for f in view.get("flags", [])[:SUMMARY_FLAGS]])


def our_summary(view: dict) -> str:
    flags = view.get("flags", [])
    if not flags:
        return "No red flag in the latest read of the installed software and application versions."
    return " ".join(f"{f['label']}{' (new)' if f.get('new') else ''}: {f['text']}" for f in flags[:5])


def summary(view: dict, client) -> dict:
    """GPT's summary of the red flags for management, held to our numbers: a number that is not
    in the flags, or a worked-out comparison, and our own sentences are shown instead."""
    from .chat import RATIO as WORKED_OUT, allowed, numbers
    given = facts(view)
    out = {"text": our_summary(view), "ours": True, "note": None}
    if not view.get("flags") or client is None:
        if client is None:
            out["note"] = "No AI service is set (ai.services in config.yaml), so these are our own sentences."
        return out
    reply = client.chat([{"role": "system", "content": SUMMARY}, {"role": "user", "content": given}])
    text = " ".join(re.sub(r"[*_`#]{1,3}", "", str(reply.get("content") or "")).split())[:1500]
    bad = sorted(numbers(text) - allowed(given, SUMMARY, small=2))
    if not text:
        out["note"] = "The AI gave no summary, so these are our own sentences."
    elif bad or WORKED_OUT.search(text):
        what = f"numbers not in the flags ({', '.join(f'{x:g}' for x in bad[:3])})" if bad else "a worked-out comparison"
        out["note"] = f"The AI's summary had {what}, so these are our own sentences."
    else:
        out.update(text=text, ours=False)
    return out
