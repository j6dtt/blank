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
RATIO = 2.0  # a version is raised at this times its other versions' share (decision 1)
MIN_PCS = 20  # ... with at least this many PCs on each side
MIN_PROBLEM = 5  # ... and at least this many PCs with the problem on it
FEW_PCS = 5  # flag 6: programs on this many PCs or fewer
WIN_MOST = 0.5  # flag 5: an update on more than half the PCs of its Windows version
MULTI_MAX = 200  # flag 4: programs read PC by PC, most extra installs first (143 on the live tenant)
APPS_MAX = 40  # flag 1: applications read PC by PC, for each of usage and actions
WEEKS = 12  # Windows last updated: weeks shown, then "older"
AGG_ROWS = 5000  # rows of a read Aternity adds up (name + version: 2,245 on the live tenant)
LIST_ROWS = 20000  # rows of a read that lists PCs
PCS_SHOWN = 200  # PCs in a list opened by a click
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


class _Reads:
    """Aternity reads that never stop the whole read: what failed or was cut short is a note."""

    def __init__(self, client, notes: list[str]):
        self.client, self.notes = client, notes
        self.schema: dict | None = None
        self.error = ""  # the first read Aternity did not answer, in a few words
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
        try:
            got = self.client.query(entity, **kw)
        except Exception as exc:
            self.error = self.error or short_error(exc)
            self.notes.append(f"{what} could not be read ({short_error(exc)}).")
            return None
        if len(self.client.warnings) > mark or (kw.get("top") and len(got) >= kw["top"]):
            self.notes.append(f"{what}: Aternity's reply was cut short ({len(got):,} rows), so its counts "
                              "are partial.")
        return got


# -- the daily read -------------------------------------------------------------
def _installed(q: _Reads, say) -> tuple[dict, dict | None]:
    """Programs: PCs per version, PCs, one-user PCs; and the list's size."""
    S, c = SOFTWARE_SOURCE, SOFTWARE_COLUMNS
    D, N, V, P, T = (c[k] for k in ("device", "name", "version", "pcs", "type"))
    if q.schema is not None and S not in q.schema:
        q.notes.append(f"{S} is not published by this Aternity: nothing on installed software can be read.")
        return {}, None
    say("Reading the installed software list")
    agg = q.rows(S, "The installed software list", select=[N, V, T, P] if q.has(S, T) else [N, V, P],
                 top=AGG_ROWS)
    if not agg:
        return {}, None
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
    size = {"pcs": len({r.get(D) for r in listed}) if listed else None, "programs": len(progs),
            "installs": sum(sum(p["versions"].values()) for p in progs.values()),
            "user_programs": sum(1 for p in progs.values() if p["user"]) if users is not None else None,
            "user_installs": sum(p["user"] for p in progs.values()) if users is not None else None}
    return progs, size


def _multi(q: _Reads, progs: dict, say) -> None:
    """2+ versions on one PC: each program with more installs than PCs, read PC by PC."""
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
           "versions": [], "read_updates": False}
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
            "major_pcs": set(), "actions": 0.0, "major": 0.0}


def _span(spec: dict, start: datetime, end: datetime) -> str:
    return f"{spec['time']} ge {odata_time(start)} and {spec['time']} lt {odata_time(end)}"


def _versions(q: _Reads, now: datetime, say) -> dict:
    """Per application and version: PCs that ran it, with a crash, with hang time, on virtual
    desktops (app usage); actions, Major actions, PCs with a Major action (actions)."""
    apps: dict[str, dict] = {}
    read: dict[str, set] = {}
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
            if _app_rows(q, key, a, start, now, apps.setdefault(a, {})):
                read.setdefault(a, set()).add(key)
    return {a: _judged(a, vs, read.get(a, ())) for a, vs in apps.items() if a in read}


def _app_rows(q: _Reads, key: str, a: str, start: datetime, now: datetime, vs: dict) -> bool:
    """One application's rows of one source (VERSION_SOURCES), per PC and version, added to
    `vs`; False when they could not be read."""
    s = VERSION_SOURCES[key]
    E, V = s["entity"], s["version"]
    if (q.schema is not None and E not in q.schema) or not q.has(E, V):
        return False
    virt = s.get("virtual") if q.has(E, s.get("virtual")) else None
    sel = [s["device"], V] + ([s["crashes"], s["hang_time"]] if key == "usage" else [s["status"], s["volume"]]) \
        + ([virt] if virt else [])
    rows = q.rows(E, f"{a}, per PC and version", select=sel, top=LIST_ROWS * 2,
                  filter=f"{_span(s, start, now)} and {s['application']} eq {odata_quote(a)}")
    if rows is None:
        return False
    for r in rows:
        v, d = r.get(V), r.get(s["device"])
        if v is None or d is None:
            continue
        x = vs.setdefault(str(v), _blank())
        if key == "usage":
            x["pcs"].add(d)
            if (to_float(r.get(s["crashes"])) or 0) > 0:
                x["crash"].add(d)
            if (to_float(r.get(s["hang_time"])) or 0) > 0:
                x["hang"].add(d)
            if virt and is_virtual(r.get(virt)):
                x["virtual"].add(d)
        else:
            n = to_float(r.get(s["volume"]))
            n = 1.0 if n is None else n
            x["apcs"].add(d)
            x["actions"] += n
            if r.get(s["status"]) == TOP_STATUS:
                x["major"] += n
                x["major_pcs"].add(d)
            if virt and is_virtual(r.get(virt)):
                x["avirtual"].add(d)
    return True


def _judged(a: str, vs: dict, read) -> dict:
    rows = [{"version": v, "pcs": len(x["pcs"]), "crash": len(x["crash"]), "hang": len(x["hang"]),
             "virtual": len(x["virtual"]), "action_pcs": len(x["apcs"]), "action_virtual": len(x["avirtual"]),
             "actions": int(round(x["actions"])), "major": int(round(x["major"])),
             "major_pcs": len(x["major_pcs"])} for v, x in vs.items()]
    rows.sort(key=lambda r: (-max(r["pcs"], r["action_pcs"]), order(r["version"])))
    union = lambda k: len(set().union(*(x[k] for x in vs.values()))) if vs else 0  # noqa: E731
    # the application's PCs across its versions, each PC once (for "which crashed the most")
    app = {"application": a, "read": sorted(read), "versions": rows,
           "totals": {"pcs": union("pcs"), "crash": union("crash"), "hang": union("hang"),
                      "action_pcs": union("apcs"), "slow": union("major_pcs")}}
    judge(app)
    return app


def app_now(client, now: datetime, application: str) -> dict:
    """One application per version, read now, in the daily read's shape (judged by the same
    rules): for one the daily read left out (1 version, too few problems, or beyond APPS_MAX)."""
    notes: list[str] = []
    q = _Reads(client, notes)
    vs: dict = {}
    read = [key for key in ("usage", "actions") if _app_rows(q, key, application, now - timedelta(days=DAYS), now, vs)]
    app = _judged(application, vs, read)
    return dict(_versions_view({application: app})[0], notes=notes, error=q.error if not read else "")


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


def judge(app: dict) -> None:
    """Flag 1 (decision 1): a version is raised when its share of PCs with a crash (or hang
    time), or of actions rated Major, is at least RATIO times its other versions' share, with
    MIN_PCS PCs on each side and MIN_PROBLEM PCs with the problem on it. When every PC listed
    had a crash (or hang time), app usage holds the application only where it failed: no
    share of either can be compared, so none is raised and the reason is kept (`only`)."""
    vs, read = app["versions"], app["read"]
    app["raised"], app["only"], app["others"] = [], [], {}
    sides = []
    if "usage" in read:
        tof = sum(v["pcs"] for v in vs)
        app["only"] = [k for k in ("crash", "hang") if sum(v[k] for v in vs) >= MIN_PROBLEM
                       and sum(v[k] for v in vs) == tof]
        if not app["only"]:  # held only where it failed: no share of either can be compared
            sides += [("crash", "crash", "pcs", "pcs", "crash"), ("hang", "hang", "pcs", "pcs", "hang")]
    if "actions" in read:
        sides += [("slow", "major", "actions", "action_pcs", "major_pcs")]
    for kind, n_key, of_key, pcs_key, prob_key in sides:
        tn, tof, tpcs = (sum(v[k] for v in vs) for k in (n_key, of_key, pcs_key))
        for v in vs:
            n, of, on, oof = v[n_key], v[of_key], tn - v[n_key], tof - v[of_key]
            app["others"].setdefault(v["version"], {})[kind] = [on, oof]
            if (v[pcs_key] >= MIN_PCS and tpcs - v[pcs_key] >= MIN_PCS and v[prob_key] >= MIN_PROBLEM
                    and of > 0 and oof > 0 and n * oof >= RATIO * on * of and n * oof > on * of):
                app["raised"].append({"kind": kind, "version": v["version"], "n": n, "of": of, "others_n": on,
                                      "others_of": oof, "pcs": v[prob_key]})


def flag_text(kind: str, f: dict) -> str:
    """The red flag's sentence: what was counted, in plain counts."""
    a, v = f.get("application"), f.get("version")
    if kind == "crash":
        return (f"{a} {v}: a crash on {f['n']:,} of {f['of']:,} PCs; its other versions: {f['others_n']:,} of "
                f"{f['others_of']:,} PCs.")
    if kind == "hang":
        return (f"{a} {v}: hang time on {f['n']:,} of {f['of']:,} PCs; its other versions: {f['others_n']:,} of "
                f"{f['others_of']:,} PCs.")
    if kind == "slow":
        return (f"{a} {v}: {f['n']:,} of {f['of']:,} actions rated {TOP_STATUS}, on {f['pcs']:,} PCs; its other "
                f"versions: {f['others_n']:,} of {f['others_of']:,} actions.")
    return f.get("text", "")


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
    for a in snap.get("versions", {}).values():
        for r in a["raised"]:
            f = dict(r, application=a["application"])
            flags.append({"key": f"{r['kind']}|{a['application']}|{r['version']}", "kind": r["kind"],
                          "label": KIND[r["kind"]], "pcs": r["pcs"], "text": flag_text(r["kind"], f),
                          "application": a["application"], "version": r["version"]})
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
                                                   f"{m['instead'][0]['update']}." if m["instead"] else ".")})
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
    progs, size = _installed(q, say)
    _multi(q, progs, say)
    snap = {"as_of": now.isoformat(), "date": local.day(now).isoformat(), "list": size, "programs": progs,
            "windows": _windows(q, cfg, local, now, say), "versions": _versions(q, now, say), "notes": notes,
            "days": DAYS}
    snap["crashes"] = _crashes(q, now, say)
    snap["usage"] = q.usage  # application -> crashes in app usage, every application
    snap["crash_names"] = q.crash_names
    # Aternity down: the list itself did not come. Not a day's counts (keep_reading tries again soon)
    snap["unread"] = q.error if size is None else ""
    snap["flags"] = _flags(snap)
    return snap


# -- the reader and its saved days ------------------------------------------------
class Software:
    def __init__(self, zone: str = "", store: str | os.PathLike | None = None):
        self.zone, self.zone_label, _ = zone_of(zone)
        self.store = Path(store) / "software" if store else None
        self.snap: dict | None = None  # the latest read
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
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            cut = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
            latest = None
            for path in sorted(self.store.glob("*.json")):
                if path.stem < cut:
                    path.unlink()
                    continue
                snap = json.loads(path.read_text())
                self.saved[path.stem] = {f["key"]: f["pcs"] for f in snap.get("flags", [])}
                latest = snap
            self.snap = latest
        except (OSError, ValueError) as exc:
            self.note = (f"The saved days in {self.store} could not be read ({short_error(exc)}): flags are not "
                         "marked new and have no days before. The folder must be writable by the container "
                         "(uid 1000).")
            log.warning("Software: %s", self.note)

    def save(self, snap: dict) -> None:
        with self.lock:
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
            "rules": {"ratio": RATIO, "min_pcs": MIN_PCS, "min_problem": MIN_PROBLEM, "few": FEW_PCS},
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
        raised = {(r["version"], r["kind"]) for r in a["raised"]}
        vs = [dict(v, raised=sorted(k for (ver, k) in raised if ver == v["version"]),
                   others=a["others"].get(v["version"], {})) for v in a["versions"]]
        out.append({"application": a["application"], "read": a["read"], "only": a["only"], "versions": vs,
                    "totals": a.get("totals"),
                    "raised": len(a["raised"]), "problem": sum(v["crash"] + v["hang"] + v["major_pcs"] for v in vs)})
    out.sort(key=lambda a: (-a["raised"], -a["problem"], a["application"].lower()))
    return out


def keep_reading(sw: Software, make_client, cfg_of, sleep, clock=lambda: datetime.now(timezone.utc)) -> None:
    """Runs in a background thread: the whole read at start, then every READ_EVERY_H hours."""
    while True:
        try:
            client = make_client()
        except Exception as exc:  # a broken config.yaml
            sw.off = f"The settings could not be read ({short_error(exc)})."
            log.warning("Software: %s", sw.off)
            return
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


def programs_per_pc(client, notes: list[str]) -> dict | None:
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
    if not summed or len(client.warnings) > mark:
        return None
    per: dict = {}
    for r in summed:
        if r.get(D):
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


def pcs_with_program(client, name: str, version: str | None, prefix: bool = False) -> dict:
    """The PCs with a program (at one version, or `prefix`: every version starting with it),
    read now; each PC with its versions of the program."""
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
        if r.get(c["device"]):
            per.setdefault(str(r.get(c["device"])), set()).add(shown(r.get(c["version"])))
    devs = sorted(per, key=str.lower)
    what = name if version is None else f"{name} {version}.x" if prefix else f"{name} {shown(version)}"
    return {"what": what, "total": len(devs), "problem": None,
            "rows": [{"device": d, "version": ", ".join(sorted(per[d], key=order))} for d in devs[:PCS_SHOWN]]}


def pcs_with_problem(client, now: datetime, application: str, version: str, problem: str) -> dict:
    """The PCs with a crash, hang time or a Major action on one application version over the
    last DAYS days, read now: user, days with it, the last of them (for Investigate)."""
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
    try:  # the user, when Aternity publishes it on this source
        rows = client.query(s["entity"], filter=flt, select=sel + [s["username"]], top=LIST_ROWS)
    except Exception:
        rows = client.query(s["entity"], filter=flt, select=sel, top=LIST_ROWS)
    per: dict = {}
    for r in rows:
        d, t = r.get(s["device"]), parse_time(r.get(s["time"]))
        if not d or t is None:
            continue
        x = per.setdefault(str(d), {"device": str(d), "username": None, "days": set(), "count": 0.0, "last": t})
        x["days"].add(t.date())
        x["last"] = max(x["last"], t)
        if problem != "hang":  # hang time has no documented unit: never shown
            x["count"] += to_float(r.get(count)) or (1 if problem == "slow" else 0)
        if r.get(s.get("username")):
            x["username"] = str(r.get(s["username"]))
    out = sorted(per.values(), key=lambda x: (-len(x["days"]), -x["last"].timestamp(), x["device"].lower()))
    words = {"crash": "a crash", "hang": "hang time", "slow": f"a {TOP_STATUS} action"}[problem]
    return {"what": f"{application} {version}: PCs with {words}, last {DAYS} days", "total": len(out),
            "problem": problem,
            "rows": [{"device": x["device"], "username": x["username"], "days": len(x["days"]),
                      "count": int(round(x["count"])) if problem != "hang" else None,
                      "last": x["last"].isoformat()} for x in out[:PCS_SHOWN]]}


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
            notes += [w for w in client.warnings if "health" in w.lower()]
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
    rows = client.query(u["entity"], select=[u["device"], u["time"], u["version"], u["crashes"], u["hang_time"]],
                        filter=f"{_span(u, start, now)} and {u['application']} eq {odata_quote(application)}",
                        top=LIST_ROWS * 5)
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
    return {"application": application, "days": UPDATE_DAYS, "pairs": out[:5], "notes": notes}


def split_by_program(client, now: datetime, application: str, program: str, problem: str = "crash") -> dict:
    """An application's crashes, hang time or Major actions over the last DAYS days, on the PCs
    of each version of an installed program (PCs matched by name; a PC with 2 versions of the
    program counts under each), and on the PCs without it in the installed list. Never a red
    flag: split by any of the installed programs, some differ by chance (docs/software-tab.md)."""
    c = SOFTWARE_COLUMNS
    problem = problem if problem in ("crash", "hang", "slow") else "crash"
    inst = client.query(SOFTWARE_SOURCE, filter=f"{c['name']} eq {odata_quote(program)}",
                        select=[c["device"], c["version"]], top=LIST_ROWS)
    has: dict[str, set] = {}
    for r in inst:
        if r.get(c["device"]):
            has.setdefault(str(r.get(c["device"])).lower(), set()).add(shown(r.get(c["version"])))
    start, notes = now - timedelta(days=DAYS), []
    s = VERSION_SOURCES["actions" if problem == "slow" else "usage"]
    flt = f"{_span(s, start, now)} and {s['application']} eq {odata_quote(application)}"
    per: dict[str, list] = {}  # PC -> [ran, with the problem, actions, Major actions]
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
