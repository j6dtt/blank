"""The Software tab's reader: each red flag's rule, the saved days, the reads a click makes.

A fake Aternity adds rows up the way the live tenant does (stage, Nov 10): grouped by the
columns asked for, measures summed, DEVICE_COUNT = distinct PCs."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from uxcorrelate import software as sw
from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import Settings
from uxcorrelate.models import parse_time

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
MEASURES = {"APPLICATION_CRASHES_TOTAL", "APPLICATION_HANG_TIME_TOTAL", "ACTIVITY_VOLUME"}
FIXTURE = Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml"


from uxcorrelate.http import ApiError  # noqa: E402


def pc(i: int) -> str:
    return f"PC-{i:02d}"


def day(n: int) -> str:
    return (NOW - timedelta(days=n)).replace(hour=0, minute=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clause(row: dict, clause: str) -> bool:
    col, op, value = clause.split(" ", 2)
    have = row.get(col)
    if value.startswith("'"):
        return op == "eq" and have == value[1:-1].replace("''", "'")
    if op == "gt":
        return float(have or 0) > float(value)
    t, v = parse_time(have), parse_time(value)
    return t is not None and (t >= v if op == "ge" else t < v)


def fake(tables: dict[str, list[dict]], seen: list | None = None):
    def handler(request):
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=FIXTURE.read_text())
        entity = request.url.path.rsplit("/", 1)[1]
        p = request.url.params
        f, sel = p.get("$filter", ""), [c for c in p.get("$select", "").split(",") if c]
        if seen is not None:
            seen.append((entity, f, sel))
        rows = [r for r in tables.get(entity, [])
                if all(any(_clause(r, x) for x in c.split(" or ")) for c in f.split(" and ") if c)]
        keys = [c for c in sel if c not in MEASURES and c != "DEVICE_COUNT"]
        per: dict = {}
        for r in rows:  # Aternity adds up per the columns asked for
            g = per.setdefault(tuple(r.get(k) for k in keys), {"pcs": set(), **{m: 0.0 for m in MEASURES}})
            g["pcs"].add(r.get("DEVICE_NAME"))
            for m in MEASURES:
                g[m] += float(r.get(m) or 0)
        out = []
        for key, g in per.items():
            o = dict(zip(keys, key))
            for c in sel:
                if c in MEASURES:
                    o[c] = g[c]
                elif c == "DEVICE_COUNT":
                    o[c] = len(g["pcs"])
            out.append(o)
        top = int(p.get("$top") or 0)
        return httpx.Response(200, json={"value": out[:top] if top else out})
    return handler


def sw_row(i, name, version, kind="Application", scope="ALL"):
    return {"DEVICE_NAME": pc(i), "INSTALLED_SW_NAME": name, "INSTALLED_SW_VERSION": version,
            "INSTALLED_SW_TYPE": kind, "INSTALLED_SW_SCOPE": scope}


def tables() -> dict:
    """60 PCs. Edge: 40 on 121.0 (most, newest), 15 on 120.0, 5 on 119.0 (20 behind). Java: 8.1 on
    10 PCs and 11.0 on 6 of them. Zoom: 3 PCs, for one user. Rare Tool: 1 PC. Windows 23H2 on 58
    PCs: KB1 on 50, the 8 others have KB2 instead; 24H2 on 2. Outlook: 16.1 on PCs 1-40 (a crash
    on 12; PCs 31-40 are virtual desktops), 16.2 on PCs 41-60 (a crash on 2; 400 of its 2,400
    actions Major); PCs 41-50 ran 16.1 two weeks ago (before and after). Trellix ENS: every PC
    listed crashed."""
    inst = [sw_row(i, "Microsoft Edge", "121.0" if i <= 40 else "120.0" if i <= 55 else "119.0") for i in range(1, 61)]
    inst += [sw_row(i, "Java", "8.1") for i in range(1, 11)] + [sw_row(i, "Java", "11.0") for i in range(1, 7)]
    inst += [sw_row(i, "Zoom", "5.1", scope="Specific User") for i in range(1, 4)] + [sw_row(7, "Rare Tool", "1.0")]
    inst += [sw_row(i, "KB1" if i <= 50 else "KB2", "N/A", kind="OS Update") for i in range(1, 59)]
    inv = [{"DEVICE_NAME": pc(i), "OS_NAME": "MS Windows 11",
            "OS_VERSION": "Microsoft Windows 11 Enterprise " + ("23H2" if i <= 58 else "24H2"),
            "OS_VERSION_UPDATE_DATE": day(3 if i <= 30 else 200)} for i in range(1, 61)]
    usage = []
    for i in range(1, 61):
        v = "16.1" if i <= 40 else "16.2"
        crash = i <= 12 or i in (41, 42)
        for d in (1, 2):  # two days each: Aternity adds the days up per PC and version
            usage.append({"DEVICE_NAME": pc(i), "USERNAME": f"user{i}", "APPLICATION_NAME": "Outlook",
                          "APPLICATION_VERSION": v, "TIMEFRAME": day(d), "VIRTUALIZATION": "true" if 31 <= i <= 40 else "false",
                          "APPLICATION_CRASHES_TOTAL": 1 if crash and d == 1 else 0, "APPLICATION_HANG_TIME_TOTAL": 0})
        if 41 <= i <= 50:  # before their update, outside the 7 days compared
            usage += [{"DEVICE_NAME": pc(i), "APPLICATION_NAME": "Outlook", "APPLICATION_VERSION": "16.1",
                       "TIMEFRAME": day(d), "APPLICATION_CRASHES_TOTAL": 0, "APPLICATION_HANG_TIME_TOTAL": 0}
                      for d in (10, 9)]
    usage += [{"DEVICE_NAME": pc(i), "APPLICATION_NAME": "Trellix ENS", "APPLICATION_VERSION": "10.7" if i <= 30 else "25.11",
               "TIMEFRAME": day(1), "APPLICATION_CRASHES_TOTAL": 3, "APPLICATION_HANG_TIME_TOTAL": 0} for i in range(1, 36)]
    usage += [{"DEVICE_NAME": pc(i), "APPLICATION_NAME": "Word", "APPLICATION_VERSION": "16.1", "TIMEFRAME": day(1),
               "APPLICATION_CRASHES_TOTAL": 1, "APPLICATION_HANG_TIME_TOTAL": 0} for i in range(1, 30)]
    acts = []
    for i in range(1, 61):
        v = "16.1" if i <= 40 else "16.2"
        for act, n in (("Send Mail", 60), ("Open Calendar", 40)):
            acts.append({"DEVICE_NAME": pc(i), "APPLICATION_NAME": "Outlook", "APPLICATION_VERSION": v, "TIMEFRAME": day(1),
                         "ACTIVITY_NAME": act, "SLA_STATUS": "Normal", "ACTIVITY_VOLUME": n,
                         "VIRTUALIZATION": "true" if 31 <= i <= 40 else "false"})
        major = 1 if i <= 5 else 20 if i > 40 else 0
        if major:
            acts.append({"DEVICE_NAME": pc(i), "USERNAME": f"user{i}", "APPLICATION_NAME": "Outlook", "APPLICATION_VERSION": v,
                         "TIMEFRAME": day(1), "ACTIVITY_NAME": "Send Mail", "SLA_STATUS": "Major", "ACTIVITY_VOLUME": major,
                         "VIRTUALIZATION": "true" if 31 <= i <= 40 else "false"})
    health = [{"DEVICE_NAME": pc(i), "APPLICATION_NAME": "Outlook", "HEALTH_EVENT_NAME": "Application Crash",
               "TIMEFRAME": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")} for i in (1, 2, 3, 41)]
    return {"INSTALLED_SOFTWARE": inst, "DEVICE_INVENTORY": inv, "APPLICATIONS_DAILY": usage,
            "BUSINESS_ACTIVITIES_DAILY": acts, "DEVICE_HEALTH_RAW": health}


def client(seen=None, data=None):
    s = Settings()
    s.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    return AternityClient(s.aternity, transport=httpx.MockTransport(fake(data or tables(), seen))), s


def test_every_red_flag_with_its_counts_ranked_by_pcs(tmp_path):
    c, s = client()
    reader = sw.Software("", tmp_path, clock=lambda: NOW)
    snap = sw.read_snapshot(c, s.aternity, NOW, reader)
    assert snap["list"] == {"pcs": 60, "left_out": 0, "programs": 6, "installs": 138, "user_programs": 1,
                            "user_installs": 3}
    got = [(f["kind"], f["pcs"], f["text"]) for f in snap["flags"]]
    assert got == [
        ("slow", 20, "Outlook 16.2, physical PCs: 17% of its actions were rated Major (400 of 2,400, on 20 PCs). On the "
                     "version most have, 16.1: 0.2% (5 of 3,005)."),
        ("behind", 20, "1 programs have 5 PCs or more on a version older than the one most of their PCs have; most: "
                       "Microsoft Edge, 20 of 60 PCs older than 121.0."),
        ("crash", 12, "Outlook 16.1, physical PCs, the version most have: it crashed on 40% of its physical PCs "
                      "(12 of 30). On 16.2: 10% (2 of 20)."),
        ("windows", 8, "1 Windows updates are on most PCs of their Windows version but not on every one; most: KB1 "
                       "not on 8 of the 58 PCs on Microsoft Windows 11 Enterprise 23H2; 8 of those have KB2."),
        ("multi", 6, "1 programs have 2 or more versions on one PC, 6 PCs in all; most: Java on 6 of its 10 PCs "
                     "(8.1 + 11.0)."),
        ("user", 3, "1 programs are installed for one user only, 3 installs; most: Zoom on 3 PCs."),
        ("few", 3, "2 of the 4 programs are on 5 PCs or fewer."),  # its biggest: 3 PCs; kind order breaks the tie
    ]
    assert snap["notes"] == []
    trellix = snap["versions"]["Trellix ENS"]
    assert trellix["only"] == ["crash"] and trellix["raised"] == []  # every PC listed crashed: no share
    # nor of hang time: the PCs that ran it without a failure are not known
    assert all(not v["verdict"] for k in trellix["kinds"].values() for v in k["versions"])
    assert "Word" not in snap["versions"]  # one version: nothing to compare
    out = snap["versions"]["Outlook"]
    assert [v["version"] for v in out["versions"]] == ["16.1", "16.2"]
    assert out["versions"][1] == {
        "version": "16.2", "pcs": 20, "crash": 2, "hang": 0, "virtual": 0, "action_pcs": 20, "action_virtual": 0,
        "actions": 2400, "major": 400, "major_pcs": 20}
    # virtual desktops and physical PCs apart: PCs 31-40 are virtual, 10 of them, too few to judge
    phys, virt = out["kinds"]["physical"], out["kinds"]["virtual"]
    assert phys["ref"] == "16.1" and [(v["version"], v["pcs"], v["crash"]) for v in phys["versions"]] == [
        ("16.1", 30, 12), ("16.2", 20, 2)]
    assert phys["versions"][0]["verdict"]["crash"] == {"state": "raised", "few": False, "against": "16.2"}
    assert phys["versions"][1]["verdict"] == {"crash": {"state": "no", "few": False}, "hang": {"state": "no", "few": False},
                                              "slow": {"state": "raised", "few": False}}
    assert virt["ref"] is None and [(v["version"], v["pcs"], v["verdict"]) for v in virt["versions"]] == [("16.1", 10, {})]
    win = snap["windows"]
    assert win["pcs"] == 60 and win["older"] == 30 and win["dated"] == 60 and sum(n for _, n in win["weeks"]) == 30
    w23 = win["versions"][0]
    assert w23["windows"] == "Microsoft Windows 11 Enterprise 23H2" and w23["pcs"] == 58
    assert w23["missing"] == [{"update": "KB1", "on": 50, "without": 8, "none": 0,
                               "instead": [{"update": "KB2", "on": 8, "with": 0}]}]  # a later one in its place?
    assert "PC-" not in json.dumps(snap)  # counts and names only


def test_a_version_is_judged_against_the_version_most_pcs_have():
    """Each version against the version most PCs have, and that one against each other (the user,
    Nov 21 2026: "I don't ... know where you get the 2 of 515 numbers": its other versions added
    together were on the page only as a marker). Raised: 2 times the other's share, 5 PCs with
    it, and still 2 times with 2 more PCs with it on the other side."""
    def app(*vs):
        return {"application": "A", "read": ["usage"], "versions": [
            {"version": v, "pcs": pcs, "crash": crash, "hang": 0, "action_pcs": 0, "actions": 0, "major": 0,
             "major_pcs": 0} for v, pcs, crash in vs]}
    for vs, raised, verdict in (
        ((("1", 40, 2), ("2", 20, 6)), [("2", "version")], "raised"),  # 30% against 5%; 10% with 2 more there
        ((("1", 40, 4), ("2", 20, 5)), [], "unsure"),  # 25% against 10%, but 15% with 2 more PCs there
        ((("1", 40, 0), ("2", 20, 4)), [], "unsure"),  # 2 times, but only 4 PCs with it
        ((("1", 40, 7), ("2", 20, 2)), [], "no"),  # the version most PCs have: 17.5% against 10%, under 2 times
        ((("1", 40, 12), ("2", 20, 0)), [("1", "most")], "no"),  # the version most PCs have is the worse one
        ((("1", 1738, 19), ("2", 515, 2)), [], "no"),  # stage, Nov 21: one more crash among the 515 undid the flag
        ((("1", 40, 12), ("2", 19, 0)), [], None),  # too few PCs on the other version: not compared
    ):
        a = app(*vs)
        sw.judge(a)
        assert [(r["version"], r["how"]) for r in a["raised"]] == raised, vs
        most, other = a["kinds"]["all"]["versions"]
        assert a["kinds"]["all"]["ref"] == "1" and (other["verdict"].get("crash") or {}).get("state") == verdict, vs
    assert a["kinds"]["all"]["versions"][1]["verdict"] == {} and "few" not in most["verdict"]
    few = app(("1", 40, 0), ("2", 20, 4))
    sw.judge(few)
    assert few["kinds"]["all"]["versions"][1]["verdict"]["crash"] == {"state": "unsure", "few": True}
    assert [sw.share_text(*x) for x in ((45, 300), (60, 1900), (1, 26), (0, 9), (1, 3000), (5, 40), (1, 8))] == [
        "15%", "3.2%", "3.8%", "0%", "under 0.1%", "13%", "13%"]  # half rounded up, as the page does


def test_slow_actions_are_raised_only_when_five_more_major_actions_would_not_undo_it():
    """Stage, Nov 21 2026: Teams on virtual desktops was raised as slower on 1,316 of 7,112 actions
    rated Major (18.5%) against 19 of 207 (9.2%): one more Major action among the 207 would have
    undone it. Crashes and hangs had "still so with 2 more PCs"; slow actions had no such check.
    Now: still 2 times with 5 more of the other side's actions rated Major."""
    def app(*vs):
        return {"application": "A", "read": ["actions"], "versions": [
            {"version": v, "pcs": 0, "crash": 0, "hang": 0, "action_pcs": pcs, "actions": actions, "major": major,
             "major_pcs": major_pcs} for v, pcs, actions, major, major_pcs in vs]}
    for vs, raised, verdict in (
        ((("1", 1025, 7112, 1316, 1025), ("2", 20, 207, 19, 12)), [], "no"),  # stage: the version most have
        ((("1", 1025, 7112, 1316, 1025), ("2", 20, 207, 10, 8)), [("1", "most")], "no"),  # 15 of 207 is 7.2%: holds
        ((("1", 40, 400, 10, 8), ("2", 30, 300, 16, 9)), [], "unsure"),  # 5.3% against 2.5%; 5 more: 3.8%
        ((("1", 400, 4000, 200, 150), ("2", 30, 300, 36, 20)), [("2", "version")], "raised"),  # 12% against 5.1%
        ((("1", 400, 4000, 0, 0), ("2", 30, 300, 12, 4)), [], "unsure"),  # holds, but only 4 PCs with one
    ):
        a = app(*vs)
        sw.judge(a)
        assert [(r["version"], r["how"]) for r in a["raised"]] == raised, vs
        assert a["kinds"]["all"]["versions"][1]["verdict"]["slow"]["state"] == verdict, vs
    assert a["kinds"]["all"]["versions"][1]["verdict"]["slow"]["few"] is True
    assert sw.STILL_ACTIONS == 5 and sw.STILL == 2  # the page and the glossary say both numbers
    # why the version most PCs have is not raised though 2 times as high is kept for the page's words
    stage = app(("1", 1025, 7112, 1316, 1025), ("2", 20, 207, 19, 12))
    sw.judge(stage)
    most, other = stage["kinds"]["all"]["versions"]
    assert most["verdict"]["slow"] == {"state": "unsure", "few": False, "against": "2"}
    assert other["verdict"]["slow"] == {"state": "no", "few": False, "back": {"state": "unsure", "few": False}}
    word = {"application": "W", "read": ["usage"], "versions": [
        {"version": v, "pcs": pcs, "crash": crash, "hang": 0, "action_pcs": 0, "actions": 0, "major": 0, "major_pcs": 0}
        for v, pcs, crash in (("1", 1738, 19), ("2", 515, 2))]}
    sw.judge(word)  # the photo of Nov 21: 19 of 1,738 against 2 of 515, undone by 2 more PCs among the 515
    assert word["raised"] == [] and word["kinds"]["all"]["versions"][0]["verdict"]["crash"] == {
        "state": "unsure", "few": False, "against": "2"}


def teams() -> list[dict]:
    """Teams: PCs 1-30 went from 24.2 to 24.3 four days ago (5 days on 24.2 read, 3 on 24.3: the
    last 3 before are counted against the 3 after); PCs 31-60 stayed on 24.2. A crash after the
    update on PCs 1-12; before it on PCs 1-2, and on PC 3 on a day too far back to be counted."""
    def row(i, v, d, crash=0):
        return {"DEVICE_NAME": pc(i), "APPLICATION_NAME": "Teams", "APPLICATION_VERSION": v, "TIMEFRAME": day(d),
                "APPLICATION_CRASHES_TOTAL": crash, "APPLICATION_HANG_TIME_TOTAL": 0}
    rows = []
    for i in range(1, 31):
        rows += [row(i, "24.2", d, crash=int((d == 7 and i <= 2) or (d == 10 and i == 3))) for d in (10, 9, 8, 7, 6)]
        rows += [row(i, "24.3", d, crash=int(d == 2 and i <= 12)) for d in (3, 2, 1)]
    for i in range(31, 61):
        rows += [row(i, "24.2", d, crash=int(d == 1 and i == 31)) for d in (10, 8, 6, 4, 2, 1)]
    return rows


def test_before_and_after_an_update_on_the_same_pcs_with_as_many_days_on_each_side():
    """The same PCs before and after their version changed, for each PC as many days after as
    before, counted in PCs (the user, Nov 21 2026: "days of..." was confusing). A version most
    of whose PCs got it this week is "new": judged by this only, not side by side."""
    data = tables()
    data["APPLICATIONS_DAILY"] += teams()
    c, _ = client(data=data)
    a = sw.app_now(c, NOW, "Teams")
    assert set(a["kinds"]) == {"physical"} and a["day_rows"] == 420  # no PC is marked virtual; read day by day
    k = a["kinds"]["physical"]
    assert k["updates_more"] == 0
    assert k["updates"] == [{"from": "24.2", "to": "24.3", "pcs": 30, "both": 0, "first": "2026-11-07", "last": "2026-11-07",
                             "before": {"crash": 2, "hang": 0}, "after": {"crash": 12, "hang": 0},
                             "verdict": {"crash": {"state": "raised", "few": False}, "hang": {"state": "no", "few": False}}}]
    assert a["found"] == [{"kind": "crash", "version": "24.3", "on": "physical", "how": "update", "from": "24.2",
                           "n": 12, "of": 30, "before": 2, "pcs": 12}]
    assert sw.flag_text("crash", dict(a["found"][0], application="Teams")) == (
        "Teams 24.3, physical PCs: more crashes after the update from 24.2. Of the 30 PCs that got it, it crashed "
        "on 12 after the update and on 2 before it.")
    rows = {v["version"]: v for v in k["versions"]}
    assert k["ref"] == "24.2" and rows["24.2"]["pcs"] == 60 and rows["24.2"]["new"] is False
    assert rows["24.3"]["pcs"] == 30 and rows["24.3"]["new"] is True and rows["24.3"]["verdict"] == {}
    # the PCs behind it, read now by the same rule
    got = sw.pcs_after_update(c, NOW, "Teams", "24.3", "crash", "physical")
    assert got["total"] == 12 and got["rows"][0] == {"device": "PC-01", "username": None, "days": 1, "count": 1,
                                                    "last": "2026-11-08T00:00:00+00:00"}
    assert got["what"] == "Teams: physical PCs with a crash after their update to 24.3, last 14 days"
    assert sw.pcs_after_update(c, NOW, "Teams", "24.3", "crash", "virtual")["total"] == 0
    assert sw.pcs_after_update(c, NOW, "Teams", "24.2", "crash")["total"] == 0  # no PC changed to 24.2


def test_a_pc_updated_during_a_day_of_use_stays_in_before_and_after():
    """A PC that ran both versions on the day of its update was left out of before and after
    whole, though the code's own comment said that day is on neither side (found on the user's
    question of Nov 21 2026, "Did we break something": on stage 69 of 2,475 virtual desktops on a
    version were in its before and after). Now the day is left out and the PC stays."""
    d = lambda n: date(2026, 11, n)  # noqa: E731
    x = lambda *vs, crash=False: [set(vs), crash, False, True]  # noqa: E731
    by = {d(1): x("1"), d(2): x("1"), d(3): x("1", "2", crash=True), d(4): x("2"), d(5): x("2", crash=True),
          d(6): x("2")}
    f, t, when, before, after, both = sw._changed_once(by)
    assert (f, t, when, both) == ("1", "2", d(3), True)  # it changed on the day it ran both
    assert [day for day, _ in before] == [d(1), d(2)] and [day for day, _ in after] == [d(4), d(5)]  # 2 and 2
    assert sw._one_change(by)[4][-1][0] == d(6)  # every day after, for the Compare dialog's days
    assert sw._changed_once({d(1): x("1"), d(2): x("2")})[5] is False
    for no in ({d(1): x("1", "2"), d(2): x("2")},  # no day on the version before alone: nothing to compare
               {d(1): x("1"), d(2): x("1", "2"), d(3): x("1"), d(4): x("2")},  # back and forth
               {d(1): x("1"), d(2): x("1", "3"), d(3): x("2")},  # a third version
               {d(1): x("1"), d(2): x("2"), d(3): x("1")}, {d(1): x("1"), d(2): x("1")}):
        assert sw._changed_once(no) is None, no
    # counted: 25 PCs, 10 of them updated during a day of use; a crash after on 12, before on 2
    days = {pc(i): ({d(1): x("1", crash=i <= 2), d(2): x("1", "2", crash=True), d(3): x("2", crash=i <= 12)} if i <= 10
                    else {d(1): x("1", crash=i <= 2), d(3): x("2", crash=i <= 12)}) for i in range(1, 26)}
    assert sw._updates(days, lambda _: True) == [{
        "from": "1", "to": "2", "pcs": 25, "both": 10, "first": "2026-11-02", "last": "2026-11-03",
        "before": {"crash": 2, "hang": 0}, "after": {"crash": 12, "hang": 0}}]


def test_every_update_is_judged_not_only_the_two_with_the_most_pcs():
    """Only the 2 changes of version with the most PCs (per kind of PC) were judged, as only they
    are shown: a third could never be raised. Now all are judged; the page gets the 2 biggest and
    every raised one, and how many more were judged with nothing raised."""
    up = lambda f, t, pcs, before, after: {"from": f, "to": t, "pcs": pcs, "both": 0, "first": "2026-11-05",  # noqa: E731
                                           "last": "2026-11-06", "before": {"crash": before, "hang": 0},
                                           "after": {"crash": after, "hang": 0}}
    row = {"version": "4", "pcs": 200, "crash": 9, "hang": 0, "action_pcs": 0, "actions": 0, "major": 0, "major_pcs": 0,
           "new": False}
    a = {"application": "A", "read": ["usage"], "versions": [dict(row)], "kinds": {"physical": {
        "pcs": 200, "versions": [dict(row)],
        "updates": [up("1", "2", 60, 1, 1), up("2", "3", 40, 0, 2), up("3", "4", 25, 1, 9), up("1", "3", 20, 3, 3)]}}}
    sw.judge(a)
    k = a["kinds"]["physical"]
    assert [(u["from"], u["to"]) for u in k["updates"]] == [("1", "2"), ("2", "3"), ("3", "4")]  # 2 biggest + the raised
    assert k["updates_more"] == 1 and all("verdict" in u for u in k["updates"])
    assert a["raised"] == [{"kind": "crash", "version": "4", "on": "physical", "how": "update", "from": "3", "n": 9,
                            "of": 25, "before": 1, "pcs": 9}]
    sw.judge(a)  # judged again (a saved day when shown): the count of those not shown stays
    assert len(a["kinds"]["physical"]["updates"]) == 3 and a["kinds"]["physical"]["updates_more"] == 1


def test_a_day_saved_under_the_rule_before_is_judged_again_not_shown_with_its_old_words():
    """After the copy, the page shows the last saved day until the first read: its applications
    have no kinds of PC and its raised versions are the old rule's."""
    old = {"Outlook": {"application": "Outlook", "read": ["usage"], "only": [], "others": {}, "totals": None,
                       "raised": [{"kind": "crash", "version": "16.1", "n": 12, "of": 40, "others_n": 2, "others_of": 20,
                                   "pcs": 12}],
                       "versions": [{"version": "16.1", "pcs": 40, "crash": 12, "hang": 0, "virtual": 0, "action_pcs": 0,
                                     "actions": 0, "major": 0, "major_pcs": 0, "action_virtual": 0},
                                    {"version": "16.2", "pcs": 20, "crash": 0, "hang": 0, "virtual": 0, "action_pcs": 0,
                                     "actions": 0, "major": 0, "major_pcs": 0, "action_virtual": 0}]}}
    v = sw._versions_view(old)[0]
    assert list(v["kinds"]) == ["all"] and v["kinds"]["all"]["ref"] == "16.1" and v["raised"] == 1
    assert v["found"] == [{"kind": "crash", "on": "all", "ref": "16.2", "version": "16.1", "how": "most", "n": 12,
                           "of": 40, "ref_n": 0, "ref_of": 20, "pcs": 12}]
    assert sw.flag_text("crash", dict(v["found"][0], application="Outlook")) == (
        "Outlook 16.1, the version most have: it crashed on 30% of its PCs (12 of 40). On 16.2: 0% (0 of 20).")
    assert "others" in old["Outlook"] and "kinds" not in old["Outlook"]  # the saved day itself is not changed


def test_the_saved_days_mark_new_flags_and_keep_their_days(tmp_path):
    c, s = client()
    reader = sw.Software("", tmp_path, clock=lambda: NOW)
    first = sw.read_snapshot(c, s.aternity, NOW - timedelta(days=1), reader)
    first["flags"] = [f for f in first["flags"] if f["kind"] != "crash"]  # yesterday: no crash flag
    reader.save(first)
    reader.save(sw.read_snapshot(c, s.aternity, NOW, reader))
    again = sw.Software("", tmp_path, clock=lambda: NOW)  # after a restart: from the files
    v = again.view()
    assert v["ready"] and v["date"] == "2026-11-10" and len(v["flags"]) == 7
    new = {f["kind"]: f["new"] for f in v["flags"]}
    assert new["crash"] is True and new["behind"] is False
    crash = next(f for f in v["flags"] if f["kind"] == "crash")
    assert crash["days"] == [{"day": "2026-11-09", "pcs": 0}, {"day": "2026-11-10", "pcs": 12}]
    assert sorted(p.name for p in (tmp_path / "software").iterdir()) == ["2026-11-09.json", "2026-11-10.json"]
    assert v["multi"] == [{"name": "Java", "pcs": 6, "of": 10, "extra": 6, "combos": [["8.1 + 11.0", 6]]}]
    java = again.program("Java")  # 8.1 on 10 PCs (the most), 11.0 on 6 of them: none has only older ones
    assert (java["most"], java["newest"], java["behind"]) == ("8.1", "11.0", 0)
    assert v["tail"][0] == {"label": "1", "programs": 1, "names": ["Rare Tool"]}
    assert v["tail"][1]["label"] == "2-5" and v["tail"][1]["names"] == ["Zoom"]
    edge = again.program("Microsoft Edge")
    assert edge["versions"] == [["119.0", 5], ["120.0", 15], ["121.0", 40]]  # oldest to newest
    assert (edge["most"], edge["newest"], edge["oldest"], edge["behind"]) == ("121.0", "121.0", "119.0", 20)
    assert again.program("KB1")["dotted"] is False and again.program("nope") is None


def test_versions_in_order_are_numbers_not_text():
    assert sorted(["10.0", "9.1", "11.0.2", "N/A-ish"], key=sw.order) == ["9.1", "10.0", "11.0.2", "N/A-ish"]
    p = sw.program_view({"name": "X", "type": "Application", "pcs": 3, "user": 0, "multi": None,
                         "versions": {"beta": 1, "1.0": 2}})
    assert p["dotted"] is False and p["behind"] is None and p["most"] == "1.0"  # no order: no "behind"


def test_a_missing_source_is_a_note_not_a_failure():
    data = tables()
    del data["DEVICE_INVENTORY"]
    c, s = client(data=data)
    real = c.http.request

    def no_inventory(method, url, **kw):  # the tenant does not publish OS_VERSION
        r = real(method, url, **kw)
        if url.endswith("$metadata"):
            text = r.text.replace('Name="OS_VERSION"', 'Name="OS_VERSION_X"')
            return httpx.Response(200, text=text)
        return r
    c.http.request = no_inventory
    snap = sw.read_snapshot(c, s.aternity, NOW, sw.Software(""))
    assert snap["windows"] is None and "windows" not in {f["kind"] for f in snap["flags"]}
    assert snap["notes"] == ["Aternity's device list is empty, so PCs that are gone are still counted in the "
                             "installed software.",
                             "DEVICE_INVENTORY gives no OS_VERSION: Windows updates are not counted."]


def test_pcs_no_longer_in_aternitys_device_list_are_left_out_of_every_count(tmp_path):
    """Stage, Nov 20 2026 (verify --inventory): Aternity's installed software list had 6,464 PCs,
    1,266 of them no longer in its device list (3 in use in 8 days): it takes a PC off the device
    list about a month after it last heard from it and keeps its software, so 1 in 5 of the PCs
    behind the tab's counts was a PC that is gone. Their software is read and taken off the
    totals; when that cannot be done, nothing is left out and a note says so."""
    def data():
        t = tables()
        for k in (1, 2, 3):  # 3 PCs that are gone: Edge 119.0, one more Rare Tool, a tool only they have
            for name, v, scope in (("Microsoft Edge", "119.0", "ALL"), ("Old Tool", "2.0", "ALL"),
                                   ("Zoom", "5.1", "Specific User")):
                t["INSTALLED_SOFTWARE"].append({"DEVICE_NAME": f"OLD-{k}", "INSTALLED_SW_NAME": name,
                                                "INSTALLED_SW_VERSION": v, "INSTALLED_SW_TYPE": "Application",
                                                "INSTALLED_SW_SCOPE": scope})
        t["INSTALLED_SOFTWARE"].append({"DEVICE_NAME": "OLD-1", "INSTALLED_SW_NAME": "Java", "INSTALLED_SW_VERSION": "8.1",
                                        "INSTALLED_SW_TYPE": "Application", "INSTALLED_SW_SCOPE": "ALL"})
        t["INSTALLED_SOFTWARE"].append({"DEVICE_NAME": "OLD-1", "INSTALLED_SW_NAME": "Java", "INSTALLED_SW_VERSION": "11.0",
                                        "INSTALLED_SW_TYPE": "Application", "INSTALLED_SW_SCOPE": "ALL"})
        return t
    plain = sw.read_snapshot(client()[0], Settings().aternity, NOW, sw.Software("", clock=lambda: NOW))
    seen = []
    c, s = client(seen, data())
    reader = sw.Software("", tmp_path, clock=lambda: NOW)
    snap = sw.read_snapshot(c, s.aternity, NOW, reader)
    assert snap["list"] == dict(plain["list"], left_out=3)  # as if the 3 PCs were not there
    assert snap["programs"] == plain["programs"] and "Old Tool" not in snap["programs"]
    assert [(f["key"], f["pcs"]) for f in snap["flags"]] == [(f["key"], f["pcs"]) for f in plain["flags"]]
    asked = [f for e, f, _ in seen if e == "INSTALLED_SOFTWARE" and " or " in f]
    assert asked == ["DEVICE_NAME eq 'OLD-1' or DEVICE_NAME eq 'OLD-2' or DEVICE_NAME eq 'OLD-3'"]  # one read
    # no PC name is saved; the lists opened by a click leave the same PCs out
    reader.save(snap)
    assert reader.gone == {"OLD-1", "OLD-2", "OLD-3"} and "OLD-" not in (tmp_path / "software" / f"{snap['date']}.json").read_text()
    assert reader.view()["list"]["left_out"] == 3
    assert sw.pcs_with_program(c, "Microsoft Edge", "119.0")["total"] == 8
    assert sw.pcs_with_program(c, "Microsoft Edge", "119.0", gone=reader.gone)["total"] == 5

    # Aternity refuses several PCs in one read: one PC a read, the same counts
    c2, _ = client(data=data())
    real = c2.query

    def no_or(entity, filter=None, **kw):
        if filter and " or " in filter:
            raise ApiError("Aternity: HTTP 400", 400, "bad filter")
        return real(entity, filter, **kw)
    c2.query = no_or
    again = sw.read_snapshot(c2, s.aternity, NOW, reader)
    assert again["list"] == snap["list"] and again["programs"] == plain["programs"]

    # their software cannot be read at all: nothing is left out, and it is said
    def none(entity, filter=None, **kw):
        if filter and "OLD-" in filter:
            raise ApiError("Aternity: HTTP 500", 500, "down")
        return real(entity, filter, **kw)
    c2.query = none
    kept = sw.read_snapshot(c2, s.aternity, NOW, reader)
    assert kept["list"]["pcs"] == 63 and kept["list"]["left_out"] == 0 and kept["gone"] == []
    assert any("could not be read (down), so PCs that are gone are still counted" in n for n in kept["notes"])

    # most of the list missing from the device list: the lists do not go together, none left out
    t = data()
    t["DEVICE_INVENTORY"] = t["DEVICE_INVENTORY"][:10]
    odd = sw.read_snapshot(client(data=t)[0], s.aternity, NOW, reader)
    assert odd["list"]["left_out"] == 0 and any("too many to be PCs that are gone" in n for n in odd["notes"])


def test_a_read_aternity_refuses_as_too_long_is_asked_again_its_own_way(tmp_path):
    """Stage, Nov 20 2026: "Windows updates could not be read (Returned data too long. Use
    $fetch_all_rows=yes to retrieve all rows.)", so the tab had no Windows updates flag; a smaller
    limit and none were refused the same way, $fetch_all_rows=yes gave all 25,862 rows in 1 s. Any
    read refused that way is asked once more with it; another refusal is still an error."""
    inner, urls = fake(tables()), []

    def handler(request):
        urls.append(str(request.url))
        if "OS%20Update" in str(request.url) and "fetch_all_rows" not in str(request.url):
            return httpx.Response(400, json={"error": {"message": "Returned data too long.  Use $fetch_all_rows=yes "
                                                                  "to retrieve all rows."}})
        return inner(request)
    s = Settings()
    s.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    c = AternityClient(s.aternity, transport=httpx.MockTransport(handler))
    plain = sw.read_snapshot(client()[0], s.aternity, NOW, sw.Software("", clock=lambda: NOW))
    snap = sw.read_snapshot(c, s.aternity, NOW, sw.Software("", tmp_path, clock=lambda: NOW))
    assert snap["windows"] == plain["windows"] and snap["notes"] == plain["notes"]  # the flag is back, no note
    assert "windows" in {f["kind"] for f in snap["flags"]}
    again = [u for u in urls if "fetch_all_rows" in u]
    assert len(again) == 1 and again[0].endswith("&$fetch_all_rows=yes")
    with pytest.raises(ApiError):  # asked without it on purpose (verify --software): the refusal is said
        c.query("INSTALLED_SOFTWARE", filter="INSTALLED_SW_TYPE eq 'OS Update'", all_rows=False)


def test_a_click_reads_the_pcs_of_a_program_or_of_a_version_with_a_problem():
    seen = []
    c, _ = client(seen)
    got = sw.pcs_with_program(c, "Microsoft Edge", "120.0")
    assert got["total"] == 15 and got["rows"][0] == {"device": "PC-41", "version": "120.0"}
    assert got["what"] == "Microsoft Edge 120.0"
    crash = sw.pcs_with_problem(c, NOW, "Outlook", "16.1", "crash")
    assert crash["total"] == 12 and crash["what"] == "Outlook 16.1: PCs with a crash, last 7 days"
    assert crash["rows"][0] | {"last": None} == {"device": "PC-01", "username": "user1", "days": 1, "count": 1,
                                                 "last": None}
    assert seen[-1][1].endswith("APPLICATION_VERSION eq '16.1' and APPLICATION_CRASHES_TOTAL gt 0")
    slow = sw.pcs_with_problem(c, NOW, "Outlook", "16.2", "slow")
    assert slow["total"] == 20 and slow["rows"][0]["count"] == 20 and slow["rows"][0]["username"]


def test_a_click_lists_the_pcs_without_a_windows_update_as_the_daily_read_counts_them(tmp_path):
    c, s = client()
    reader = sw.Software("", tmp_path, clock=lambda: NOW)
    reader.save(sw.read_snapshot(c, s.aternity, NOW, reader))
    wins, instead = sw.missing_on(reader.snap, "KB1")
    assert wins == ["Microsoft Windows 11 Enterprise 23H2"] and instead == ["KB2"]
    assert sw.missing_on(reader.snap, "KB2") == ([], []) and sw.missing_on(None, "KB1") == ([], [])
    got = sw.pcs_without_update(c, s.aternity, "KB1", wins, instead)
    assert got["total"] == 8 and got["with_instead"] == 8  # the flag's "not on 8"; all 8 have KB2
    assert got["rows"][0] == {"device": "PC-51", "windows": wins[0], "instead": ["KB2"], "updates": 1}
    data = tables()
    data["INSTALLED_SOFTWARE"] = [r for r in data["INSTALLED_SOFTWARE"] if r["DEVICE_NAME"] != "PC-58"]
    c2, _ = client(data=data)
    first = sw.pcs_without_update(c2, s.aternity, "KB1", wins, instead)["rows"][0]
    assert first == {"device": "PC-58", "windows": wins[0], "instead": [], "updates": 0}  # none in its place: first


def test_compare_lines_up_two_versions_on_every_kind_of_issue():
    c, s = client()
    got = sw.compare(c, s.aternity, NOW, "Outlook")
    assert (got["a"], got["b"]) == ("16.1", "16.2")  # the two with the most PCs
    lines = {x["what"]: (x["a"], x["b"]) for x in got["lines"]}
    assert lines["PCs with a crash"] == ([12, 40], [2, 20])
    assert lines["PCs with hang time"] == ([0, 40], [0, 20])
    assert lines["PCs with Application Crash"] == ([3, 40], [1, 20])  # by the version the PC ran
    assert lines["Actions rated Major"] == ([5, 4005], [400, 2400])
    assert lines["Send Mail"] == ([5, 2405], [400, 1600]) and lines["Open Calendar"] == ([0, 1600], [0, 800])
    assert got["kind"] == {"a": [10, 40], "b": [0, 20]}  # virtual desktops on each
    one = sw.compare(c, s.aternity, NOW, "Word")
    assert one["lines"] == [] and "nothing to compare" in one["notes"][0]


def test_before_and_after_an_update_on_the_same_pcs():
    c, _ = client()
    got = sw.before_after(c, NOW, "Outlook")
    assert len(got["pairs"]) == 1
    p = got["pairs"][0]
    assert (p["from"], p["to"], p["pcs"]) == ("16.1", "16.2", 10)
    assert p["before"] == [20, 0, 0] and p["after"] == [20, 2, 0]  # PC-days, with a crash, with hang time
    assert p["crash_pcs"] == [0, 2] and got["notes"] == []
    assert "before and after cannot be compared" in sw.before_after(c, NOW, "Trellix ENS")["notes"][0]


class FakeAI:
    def __init__(self, text):
        self.text, self.asked = text, []

    def chat(self, messages, tools=None):
        self.asked.append(messages)
        return {"content": self.text}


def test_the_summary_for_management_is_held_to_the_flags_numbers(tmp_path):
    c, s = client()
    reader = sw.Software("", tmp_path, clock=lambda: NOW)
    reader.save(sw.read_snapshot(c, s.aternity, NOW, reader))
    v = reader.view()
    good = FakeAI("Outlook 16.2 is slower: 400 of 2,400 actions rated Major, on 20 PCs. Check it with the Outlook team.")
    got = sw.summary(v, good)
    assert got == {"text": good.text, "ours": False, "note": None}
    given = good.asked[0][1]["content"]
    assert given.startswith("As of ") and "- Slower: Outlook 16.2, physical PCs: 17% of its actions were rated Major (400 of 2,400" in given and "PC-" not in given
    made_up = sw.summary(v, FakeAI("Outlook 16.2 is slower on 37 PCs."))
    assert made_up["ours"] and made_up["note"] == "The AI's summary had numbers not in the flags (37), so these are our own sentences."
    assert made_up["text"].startswith("Slower: Outlook 16.2, physical PCs: 17% of its actions")
    worked = sw.summary(v, FakeAI("Outlook 16.2 has 2 times more Major actions."))
    assert worked["ours"] and "a worked-out comparison" in worked["note"]
    assert "No AI service is set" in sw.summary(v, None)["note"]


def test_the_tab_answers_before_its_first_read_and_says_what_is_missing():
    from fastapi.testclient import TestClient
    from uxcorrelate import web
    api = TestClient(web.create_app())  # no Aternity set, reader not started
    assert api.get("/api/software?brief=1").json() == {"ready": False, "flags": 0}
    assert api.get("/api/software").json()["ready"] is False
    assert api.get("/api/software/program", params={"name": "Edge"}).status_code == 404
    assert api.get("/api/software/pcs").status_code == 400
    assert api.get("/api/software/pcs", params={"application": "Outlook", "version": "1", "problem": "x"}).status_code == 400
    assert api.get("/api/software/pcs", params={"name": "Edge"}).status_code == 503
    assert api.post("/api/software/summary").status_code == 503
    assert api.get("/api/software/pcs", params={"update": "KB1"}).status_code == 404  # not read yet: not listed


def test_aternity_down_is_tried_again_soon_and_never_saved_as_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr("uxcorrelate.http.time.sleep", lambda s: None)  # the client's own waits between tries
    good, s = client()
    down = AternityClient(s.aternity, transport=httpx.MockTransport(lambda q: httpx.Response(503)))
    reader = sw.Software("", tmp_path, clock=lambda: NOW)
    clients, waits = [down, good, down], []

    class Stop(Exception):
        pass

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 1:  # after the first try: nothing to show, nothing saved, said on the tab
            assert reader.snap is None and not list(tmp_path.rglob("*.json")) and not reader.reading
            assert reader.status == ("Aternity's installed software list could not be read (HTTP 503); "
                                     "trying again in 15 minutes.")
        if len(waits) == 3:
            raise Stop

    try:
        sw.keep_reading(reader, lambda: clients.pop(0), lambda: s.aternity, sleep, clock=lambda: NOW)
    except Stop:
        pass
    assert waits == [15 * 60, 24 * 3600, 15 * 60]  # down, read, down again a day later
    assert len(reader.view()["flags"]) == 7 and len(list(tmp_path.rglob("*.json"))) == 1  # the good read is kept
    assert "trying again in 15 minutes" in reader.status


def test_a_broken_config_is_tried_again_not_the_end_of_the_reader():
    """A broken config.yaml stopped the software reader until a restart (Nov 14 2026 review)."""
    reader = sw.Software("", None)
    waits, tries = [], []

    class Stop(BaseException):
        pass

    def make():
        tries.append(1)
        if len(tries) == 1:
            raise ValueError("config.yaml: line 3")
        return None  # fixed, but Aternity is not set: the reader stops, saying so

    sw.keep_reading(reader, make, lambda: None, lambda s: waits.append(s), clock=lambda: NOW)
    assert waits == [15 * 60] and len(tries) == 2 and "not set" in reader.off


def test_a_read_cut_short_says_its_counts_are_partial(tmp_path, monkeypatch):
    """Aternity's reply stopped at the page limit or at $top: a flag or a click's counts from it
    say so (PCs past the cut seemed to have no update, an application's PCs seemed fewer)."""
    monkeypatch.setattr(sw, "LIST_ROWS", 30)  # Outlook's 60 PCs reach the 60 rows asked for
    c, s = client()
    snap = sw.read_snapshot(c, s.aternity, NOW, sw.Software("", tmp_path, clock=lambda: NOW))
    crash = next(f for f in snap["flags"] if f["kind"] == "crash")
    assert crash["text"].endswith(" " + sw.CUT) and snap["versions"]["Outlook"]["partial"] is True
    assert snap["windows"]["partial"] is True  # 30 of 60 PCs read
    assert any("cut short" in n for n in snap["notes"])
    win = {"windows": {"partial": True, "versions": [{"windows": "W", "pcs": 9, "missing": [
        {"update": "KB1", "on": 6, "without": 3, "none": 3, "instead": []}]}]}}
    assert sw._flags(win)[0]["text"].endswith("PCs on W. " + sw.CUT)
    monkeypatch.setattr(sw, "LIST_ROWS", 10)
    assert sw.before_after(c, NOW, "Outlook")["notes"][0].startswith(sw.CUT)
    assert sw.split_by_program(c, NOW, "Outlook", "Microsoft Edge")["notes"][0] == sw.CUT
    monkeypatch.setattr(sw, "LIST_ROWS", 20000)
    full = sw.read_snapshot(c, s.aternity, NOW, sw.Software("", tmp_path, clock=lambda: NOW))
    assert all(sw.CUT not in f["text"] for f in full["flags"]) and full["windows"]["partial"] is False
    assert sw.split_by_program(c, NOW, "Outlook", "Microsoft Edge")["notes"] == []


def test_one_unreadable_saved_day_does_not_stop_the_others_loading(tmp_path):
    c, s = client()
    reader = sw.Software("", tmp_path, clock=lambda: NOW)
    reader.save(sw.read_snapshot(c, s.aternity, NOW, reader))
    (tmp_path / "software" / "2026-11-09.json").write_text("{not json")
    again = sw.Software("", tmp_path, clock=lambda: NOW)
    assert again.view()["ready"] and again.view()["date"] == "2026-11-10"  # the latest read still shows
    assert "2026-11-09.json" in again.note and "2026-11-10" in again.saved
