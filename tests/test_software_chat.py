"""The Software tab's chat: each building block's counts, names matched to the latest read,
the AI's sentence held to our numbers, and the endpoint."""

from datetime import timedelta

import httpx
from fastapi.testclient import TestClient
from test_chat import ai_service
from test_software import NOW, client, sw_row, tables

from uxcorrelate import ai, web
from uxcorrelate import software as sw
from uxcorrelate import software_chat as sc
from uxcorrelate.aternity import AternityClient


def reader(tmp_path, data=None):
    c, s = client(data=data)
    r = sw.Software("", tmp_path)
    r.save(sw.read_snapshot(c, s.aternity, NOW, r))
    return r, r.view(), c, s


def run(name, args, r, v, c=None, s=None, known=()):
    return sc.run_block(name, args, r, v, lambda: c, s.aternity if s else None, known, NOW)


def test_names_are_matched_to_the_latest_read():
    edge = ["Microsoft Edge", "Microsoft Edge WebView2 Runtime", "Microsoft Edge Update"]
    assert sc.pick("edge", edge) == ("Microsoft Edge", [])  # the one that ends with it
    assert sc.pick("webview2", edge) == ("Microsoft Edge WebView2 Runtime", [])
    assert sc.pick("MICROSOFT EDGE", edge) == ("Microsoft Edge", [])
    assert sc.pick("Teams Meeting", ["Microsoft Teams Meeting Add-in for Microsoft Office", "Microsoft Teams classic"])[0] \
        == "Microsoft Teams Meeting Add-in for Microsoft Office"
    assert sc.pick("trellix", ["Trellix ENS", "Trellix DXL"]) == (None, ["Trellix DXL", "Trellix ENS"])
    assert sc.pick("nothing", edge) == (None, [])
    assert sw.version_of("16.0.20430", ["16.0.20430.20180", "16.0.20326.20142"]) == "16.0.20430.20180"
    assert sw.version_of("143", ["143.0.1", "143.0.2"]) is None and sw.starts_with("143.0.2", "143")
    assert not sw.starts_with("1430.1", "143")


def test_the_red_flags_and_each_kinds_whole_list(tmp_path):
    r, v, _, _ = reader(tmp_path)
    got = run("flags", {}, r, v)
    assert got["rows"][0] == ["Slower", "Outlook 16.2: 400 of 2,400 actions rated Major, on 20 PCs; its other "
                                        "versions: 5 of 4,005 actions.", 20, ""]
    assert got["total"] == "7 red flags" and got["notes"][0].startswith("Raised (a red flag): a version")
    assert run("flags", {"kind": "behind"}, r, v)["rows"] == [["Microsoft Edge", 20, 60, "121.0", "121.0"]]
    crash = run("flags", {"kind": "crash"}, r, v)
    assert crash["rows"] == [["Outlook", "16.1", 12, 40, 2, 20]]
    assert "Not compared: Trellix ENS: every PC listed had a crash" in crash["notes"][1]
    assert run("flags", {"kind": "hang"}, r, v)["rows"] == []
    assert run("flags", {"kind": "windows"}, r, v)["rows"] == [["Microsoft Windows 11 Enterprise 23H2", "KB1", 8, 58,
                                                                "KB2", 8]]
    assert run("flags", {"kind": "multi"}, r, v)["rows"] == [["Java", 6, 10, "8.1 + 11.0"]]
    assert run("flags", {"kind": "user"}, r, v)["rows"] == [["Zoom", 3, 3]]
    few = run("flags", {"kind": "few"}, r, v)
    assert few["notes"] == ["2 of the 4 programs are on 5 PCs or fewer."] and few["rows"][0] == ["1", 1]
    assert run("flags", {"kind": "nonsense"}, r, v)["title"] == "Red flags"  # an unknown kind: all of them
    assert run("flags", {"kind": "all"}, r, v)["rows"] == got["rows"]
    tool = next(t["function"] for t in sc.TOOLS if t["function"]["name"] == "flags")
    assert tool["parameters"]["properties"]["kind"]["enum"][0] == "all" and tool["parameters"]["required"] == ["kind"]


def test_a_programs_versions_and_an_applications_problems_per_version(tmp_path):
    r, v, _, _ = reader(tmp_path)
    got = run("versions", {"program": "edge"}, r, v)
    assert got["title"] == "Microsoft Edge: its versions"
    assert got["rows"] == [["121.0", 40, "most PCs, newest"], ["120.0", 15, ""], ["119.0", 5, "oldest"]]
    assert "Behind: 20 PCs on a version older than 121.0, the one most PCs have." in got["notes"]
    assert run("versions", {"program": "nothing"}, r, v)["rows"] == []
    p = run("version_problems", {"application": "outlook"}, r, v)
    assert p["rows"] == [["16.1", 40, 12, 0, 4005, 5, "Crashes more"], ["16.2", 20, 2, 0, 2400, 400, "Slower"]]
    assert p["say"].startswith("Outlook, last 7 days: 16.1: a crash on 12 of 40 PCs")
    t = run("version_problems", {"application": "Trellix ENS"}, r, v)
    assert "no share is compared" in t["notes"][1] and all(row[6] == "" for row in t["rows"])
    assert "Word is not among the 2 applications read every day" in run("version_problems", {"application": "Word"}, r, v)["say"]
    every = run("version_problems", {}, r, v)
    assert [row[0] for row in every["rows"]] == ["Outlook", "Trellix ENS"] and every["rows"][0][5] == 2


def test_the_blocks_read_now_compare_split_and_list_pcs(tmp_path):
    data = tables()
    data["INSTALLED_SOFTWARE"] += [sw_row(i, "Notepad++", x) for i, x in ((1, "8.6.1"), (2, "8.6.4"), (3, "8.7.0"))]
    r, v, c, s = reader(tmp_path, data)
    cmp = run("compare", {"application": "outlook", "a": "16.2"}, r, v, c, s)
    assert cmp["title"] == "Outlook: 16.2 against 16.1" and cmp["rows"][0] == ["PCs with a crash", 2, 20, 12, 40]
    assert ["Send Mail: actions rated Major", 400, 1600, 5, 2405] in cmp["rows"]
    assert "On virtual desktops: 10 of 20 PCs on 16.2, 0 of 40 on 16.1." in cmp["notes"]
    ba = run("before_after", {"application": "Outlook"}, r, v, c, s)
    assert ba["rows"] == [["16.1", "16.2", 10, 20, 0, 0, 20, 2, 0]]
    split = run("program_split", {"application": "Outlook", "program": "edge"}, r, v, c, s)
    assert split["rows"] == [["121.0", 40, 12], ["120.0", 15, 2], ["119.0", 5, 0]]
    assert split["columns"][-1] == "with a crash" and any("never a red flag" in n for n in split["notes"])
    slow = run("program_split", {"application": "Outlook", "program": "java", "problem": "slow"}, r, v, c, s)
    assert slow["rows"] == [["8.1", 10, 1005, 5], ["11.0", 6, 605, 5], ["without Java", 50, 5400, 400]]
    pcs = run("pcs", {"program": "notepad++", "version": "8.6"}, r, v, c, s)  # every 8.6.x
    assert pcs["title"] == "Notepad++ 8.6.x" and pcs["rows"] == [["PC-01", "8.6.1"], ["PC-02", "8.6.4"]]
    assert pcs["runs"][0]["device"] == "PC-01"
    assert run("pcs", {"program": "edge", "version": "120"}, r, v, c, s)["total"] == "15 PCs"
    assert run("pcs", {"program": "edge", "version": "99"}, r, v, c, s)["title"] == "Microsoft Edge: no version 99"
    crash = run("pcs", {"application": "outlook", "version": "16.1", "problem": "crash"}, r, v, c, s)
    assert crash["total"] == "12 PCs" and crash["rows"][0][:4] == ["PC-01", "user1", 1, 1]
    assert crash["runs"][0]["username"] == "user1" and crash["runs"][0]["end"] <= NOW.isoformat()
    assert run("pcs", {"application": "outlook"}, r, v, c, s)["rows"] == [["16.1"], ["16.2"]]  # which version?
    kb = run("pcs_without_update", {"update": "kb1"}, r, v, c, s)
    assert kb["title"] == "PCs on Microsoft Windows 11 Enterprise 23H2 without KB1" and kb["total"] == "8 PCs"
    assert kb["columns"] == ["PC", "Has in its place", "Windows updates listed"] and kb["rows"][0] == ["PC-51", "KB2", 1]
    assert kb["say"].startswith("KB1: not on 8 PCs of Microsoft Windows 11 Enterprise 23H2; 8 of them have KB2")
    assert kb["runs"][0] == {"device": "PC-51", "start": (NOW - timedelta(hours=4)).isoformat(), "end": NOW.isoformat()}
    assert run("pcs_without_update", {"update": "KB2"}, r, v, c, s)["title"] == "KB2: not listed as missing"


def test_aternity_not_answering_is_said_in_the_answer(tmp_path):
    r, v, _, s = reader(tmp_path)
    down = AternityClient(s.aternity, transport=httpx.MockTransport(lambda q: httpx.Response(403)))
    got = run("pcs", {"program": "edge"}, r, v, down, s)
    assert got["title"] == "Aternity could not be read" and got["rows"] == [] and "HTTP 403" in got["say"]
    cmp = run("compare", {"application": "Outlook"}, r, v, down, s)  # its reads say what failed
    assert cmp["title"] == "Outlook: nothing to compare" and any("could not be read" in n for n in cmp["notes"])
    assert run("compare", {"application": "Outlook"}, r, v, None, s)["title"] == "Aternity is not set"
    assert run("nothing", {}, r, v) is None


def test_the_ais_sentence_is_held_to_our_counts(tmp_path):
    r, _, c, s = reader(tmp_path)
    q = "Which programs are most out of date?"
    good = ai_service({q: ("flags", {"kind": "behind"})}, lambda g: "Microsoft Edge: 20 of 60 PCs are older than 121.0.",
                      [])
    got = sc.ask(q, [], good, r, lambda: c, s.aternity, [], NOW)
    assert got["answer"] == "Microsoft Edge: 20 of 60 PCs are older than 121.0." and got["note"] is None
    made_up = ai_service({q: ("flags", {"kind": "behind"})}, lambda g: "Microsoft Edge is behind on 45 PCs.", [])
    got = sc.ask(q, [], made_up, r, lambda: c, s.aternity, [], NOW)
    assert got["answer"].startswith("Programs with PCs behind: 1; most: Microsoft Edge, 20 of 60 PCs")
    assert "(45)" in got["note"]


def test_the_endpoint(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  services:\n    - name: gpt\n      base_url: https://ai.test/v1\n      model: m\n")
    app = web.create_app(str(cfg))
    q = "Which programs have 2 versions on one PC?"
    stand_in = ai_service({q: ("flags", {"kind": "multi"})}, lambda g: "Java has 2 versions on 6 of 10 PCs.", [])
    monkeypatch.setattr(ai, "AiClient", lambda *a, **k: stand_in)
    http = TestClient(app)
    assert "still being read" in http.post("/api/software/chat", json={"question": q}).json()["detail"]
    c, s = client()
    app.state.software.save(sw.read_snapshot(c, s.aternity, NOW, app.state.software))
    a = http.post("/api/software/chat", json={"question": q, "history": []}).json()
    assert a["answer"] == "Java has 2 versions on 6 of 10 PCs." and a["result"]["block"] == "flags"
    assert http.post("/api/software/chat", json={"question": " "}).status_code == 400
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}\n")
    assert TestClient(web.create_app(str(empty))).post("/api/software/chat", json={"question": q}).status_code == 503


def test_an_application_the_daily_read_left_out_is_read_now_by_its_exact_name(tmp_path):
    """Stage, Nov 2026: "Which version of Microsoft Edge crashed the most?" was answered "several
    applications have Microsoft Edge: ask with the full name" (Microsoft Edge Update, Microsoft Edge
    WebView2): the browser itself was not among the applications read every day."""
    data = tables()

    def use(app, i, version, crashes):
        return {"DEVICE_NAME": f"PC-{i:02d}", "APPLICATION_NAME": app, "APPLICATION_VERSION": version,
                "TIMEFRAME": data["APPLICATIONS_DAILY"][0]["TIMEFRAME"], "APPLICATION_CRASHES_TOTAL": crashes,
                "APPLICATION_HANG_TIME_TOTAL": 0}
    for app in ("Microsoft Edge Update", "Microsoft Edge WebView2"):
        data["APPLICATIONS_DAILY"] += [use(app, i, "1.0" if i <= 5 else "2.0", 1) for i in range(1, 11)]
    data["APPLICATIONS_DAILY"] += [use("Microsoft Edge", i, "156.0" if i <= 30 else "152.0", 1 if i in (2, 31, 32) else 0)
                                   for i in range(1, 41)]  # 3 crashes: under the 5 the daily read needs
    r, v, c, s = reader(tmp_path, data)
    assert "Microsoft Edge" not in v["applications"] and "Microsoft Edge Update" in v["applications"]
    for known in (["Microsoft Edge"], []):  # in today's activity or not: Aternity is asked for the exact name
        got = run("version_problems", {"application": "Microsoft Edge"}, r, v, c, s, known)
        assert got["title"] == "Microsoft Edge by version" and "read from Aternity now" in got["period"]
        assert got["rows"] == [["156.0", 30, 1, 0, 0, 0, ""], ["152.0", 10, 2, 0, 0, 0, ""]]  # no action of it is measured
        assert got["notes"][0].startswith("Read now: Microsoft Edge is not among the 4 applications read every day")
        assert "The most PCs with a crash: version 152.0, 2 of the 10 PCs that ran it" in got["notes"][-1]
    word = run("version_problems", {"application": "word"}, r, v, c, s, ["Word"])
    assert word["rows"] == [["16.1", 29, 29, 0, 0, 0, ""]]
    several = run("version_problems", {"application": "edge"}, r, v, c, s)  # no application is named "edge"
    assert several["rows"] == [["Microsoft Edge Update"], ["Microsoft Edge WebView2"]]
    assert several["notes"][0].startswith('No application is named exactly "edge"; several have it in their name')
    none = run("version_problems", {"application": "Java"}, r, v, c, s)
    assert none["title"] == "Java: nothing per version" and none["rows"] == []
    assert 'no use of an application named exactly "Java"' in none["notes"][0]
    assert "The installed list has a program of that name" in none["notes"][-1]
    # the other read-now blocks ask for the exact name too, and name the ones like it when there is nothing
    cmp = run("compare", {"application": "Microsoft Edge"}, r, v, c, s)
    assert cmp["title"] == "Microsoft Edge: 156.0 against 152.0" and cmp["rows"][0] == ["PCs with a crash", 1, 30, 2, 10]
    lost = run("compare", {"application": "edge"}, r, v, c, s)
    assert lost["title"] == "edge: nothing to compare"
    assert 'No application is named exactly "edge"' in lost["notes"][-1] and "Microsoft Edge Update" in lost["notes"][-1]
    assert "Microsoft Edge WebView2" in run("before_after", {"application": "edge"}, r, v, c, s)["notes"][-1]
    assert run("pcs", {"application": "Microsoft Edge", "version": "152.0", "problem": "crash"}, r, v, c, s)["total"] == "2 PCs"
    out = run("version_problems", {"application": "Outlook"}, r, v, c, s)  # one of the daily ones: not read again
    assert out["rows"][0][:3] == ["16.1", 40, 12] and "read from Aternity now" not in out["period"]
    assert "The most PCs with a crash: version 16.1, 12 of the 40" in " ".join(out["notes"])
