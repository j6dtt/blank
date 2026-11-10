"""The Software tab's reader: each red flag's rule, the saved days, the reads a click makes.

A fake Aternity adds rows up the way the live tenant does (stage, Nov 10): grouped by the
columns asked for, measures summed, DEVICE_COUNT = distinct PCs."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from uxcorrelate import software as sw
from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import Settings
from uxcorrelate.models import parse_time

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
MEASURES = {"APPLICATION_CRASHES_TOTAL", "APPLICATION_HANG_TIME_TOTAL", "ACTIVITY_VOLUME"}
FIXTURE = Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml"


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
        rows = [r for r in tables.get(entity, []) if all(_clause(r, c) for c in f.split(" and ") if c)]
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
    on 12), 16.2 on PCs 41-60 (a crash on 2; 400 of its 2,400 actions Major); PCs 41-50 ran 16.1
    two weeks ago (before and after). Trellix ENS: every PC listed crashed."""
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
                          "APPLICATION_VERSION": v, "TIMEFRAME": day(d), "VIRTUALIZATION": "true" if i > 50 else "false",
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
                         "VIRTUALIZATION": "true" if i > 50 else "false"})
        major = 1 if i <= 5 else 20 if i > 40 else 0
        if major:
            acts.append({"DEVICE_NAME": pc(i), "USERNAME": f"user{i}", "APPLICATION_NAME": "Outlook", "APPLICATION_VERSION": v,
                         "TIMEFRAME": day(1), "ACTIVITY_NAME": "Send Mail", "SLA_STATUS": "Major", "ACTIVITY_VOLUME": major,
                         "VIRTUALIZATION": "true" if i > 50 else "false"})
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
    reader = sw.Software("", tmp_path)
    snap = sw.read_snapshot(c, s.aternity, NOW, reader)
    assert snap["list"] == {"pcs": 60, "programs": 6, "installs": 138, "user_programs": 1, "user_installs": 3}
    got = [(f["kind"], f["pcs"], f["text"]) for f in snap["flags"]]
    assert got == [
        ("slow", 20, "Outlook 16.2: 400 of 2,400 actions rated Major, on 20 PCs; its other versions: 5 of 4,005 actions."),
        ("behind", 20, "1 programs have 5 PCs or more on a version older than the one most of their PCs have; most: "
                       "Microsoft Edge, 20 of 60 PCs older than 121.0."),
        ("crash", 12, "Outlook 16.1: a crash on 12 of 40 PCs; its other versions: 2 of 20 PCs."),
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
    assert trellix["others"] == {}  # nor of hang time: the PCs that ran it without a failure are not known
    assert "Word" not in snap["versions"]  # one version: nothing to compare
    out = snap["versions"]["Outlook"]
    assert [v["version"] for v in out["versions"]] == ["16.1", "16.2"]
    assert out["versions"][1] | {"others": None} == {
        "version": "16.2", "pcs": 20, "crash": 2, "hang": 0, "virtual": 10, "action_pcs": 20, "action_virtual": 10,
        "actions": 2400, "major": 400, "major_pcs": 20, "others": None}
    win = snap["windows"]
    assert win["pcs"] == 60 and win["older"] == 30 and win["dated"] == 60 and sum(n for _, n in win["weeks"]) == 30
    w23 = win["versions"][0]
    assert w23["windows"] == "Microsoft Windows 11 Enterprise 23H2" and w23["pcs"] == 58
    assert w23["missing"] == [{"update": "KB1", "on": 50, "without": 8, "none": 0,
                               "instead": [{"update": "KB2", "on": 8, "with": 0}]}]  # a later one in its place?
    assert "PC-" not in json.dumps(snap)  # counts and names only


def test_a_version_needs_enough_pcs_on_each_side_and_with_the_problem():
    def app(*vs):
        return {"application": "A", "read": ["usage"], "versions": [
            {"version": v, "pcs": pcs, "crash": crash, "hang": 0, "action_pcs": 0, "actions": 0, "major": 0,
             "major_pcs": 0} for v, pcs, crash in vs]}
    for vs, raised in (
        ((("1", 40, 12), ("2", 20, 2)), ["1"]),  # 30% against 10%
        ((("1", 40, 8), ("2", 20, 2)), ["1"]),  # 20% against 10%: 2 times, raised
        ((("1", 40, 7), ("2", 20, 2)), []),  # 17.5%: under 2 times
        ((("1", 19, 12), ("2", 40, 2)), []),  # too few PCs on it
        ((("1", 40, 12), ("2", 19, 0)), []),  # too few PCs on its other versions
        ((("1", 40, 4), ("2", 40, 0)), []),  # too few PCs with the problem
        ((("1", 40, 5), ("2", 40, 0)), ["1"]),  # none on its other versions
    ):
        a = app(*vs)
        sw.judge(a)
        assert [r["version"] for r in a["raised"]] == raised, vs


def test_the_saved_days_mark_new_flags_and_keep_their_days(tmp_path):
    c, s = client()
    reader = sw.Software("", tmp_path)
    first = sw.read_snapshot(c, s.aternity, NOW - timedelta(days=1), reader)
    first["flags"] = [f for f in first["flags"] if f["kind"] != "crash"]  # yesterday: no crash flag
    reader.save(first)
    reader.save(sw.read_snapshot(c, s.aternity, NOW, reader))
    again = sw.Software("", tmp_path)  # after a restart: from the files
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
    assert snap["notes"] == ["DEVICE_INVENTORY gives no OS_VERSION: Windows updates are not counted."]


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
    reader = sw.Software("", tmp_path)
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
    assert got["kind"] == {"a": [0, 40], "b": [10, 20]}  # virtual desktops on each
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
    reader = sw.Software("", tmp_path)
    reader.save(sw.read_snapshot(c, s.aternity, NOW, reader))
    v = reader.view()
    good = FakeAI("Outlook 16.2 is slower: 400 of 2,400 actions rated Major, on 20 PCs. Check it with the Outlook team.")
    got = sw.summary(v, good)
    assert got == {"text": good.text, "ours": False, "note": None}
    given = good.asked[0][1]["content"]
    assert given.startswith("As of ") and "- Slower: Outlook 16.2: 400 of 2,400 actions" in given and "PC-" not in given
    made_up = sw.summary(v, FakeAI("Outlook 16.2 is slower on 37 PCs."))
    assert made_up["ours"] and made_up["note"] == "The AI's summary had numbers not in the flags (37), so these are our own sentences."
    assert made_up["text"].startswith("Slower: Outlook 16.2: 400 of 2,400")
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
