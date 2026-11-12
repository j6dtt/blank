"""The assessment for executives: its facts (counts and names of events and applications only),
its fixed verdict and words, what changed since the one before, what to watch, what cleared, the
AI's text held to the facts, and when one is written."""

from datetime import date, timedelta

from fastapi.testclient import TestClient
from test_chat import F, H, NOW, act, ev, filled

from uxcorrelate import assess as A
from uxcorrelate import web
from uxcorrelate.http import ApiError
from uxcorrelate.today import HOUR


class Ai:
    def __init__(self, text):
        self.text, self.seen = text, []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        if self.text is None:
            raise ApiError("no", status=503)
        return {"content": self.text}


def snap_of(t, now=NOW, **kw):
    return A.snapshot(t.view(now), kw.get("care"), kw.get("status"), kw.get("boots"), kw.get("charts"))


def U(pos, low=0, high=1):
    """A count's place against its usual range, as the snapshot keeps it."""
    return {"pos": pos, "low": low, "high": high, "days": 3}


def test_the_facts_are_counts_and_names_of_events_and_applications_only():
    s = snap_of(filled())
    assert s["active"] == {"users": 4, "pcs": 4, "virtual": 2, "physical": 2}
    assert [(e["name"], e["severity"], e["pcs"]) for e in s["events"]] == [
        ("System Crash", "Critical", 1), ("Application Crash", "Major", 2)]  # the most serious first
    assert s["slow"] == {"pcs": 4, "actions": 5, "users": 4, "usual": None}
    assert s["apps"] == {"ERP": 2, "Mail": 2, "Mail Viewer": 1}
    lines = A.today_lines(s)
    assert lines[0] == "Active: 4 users on 4 PCs (2 virtual desktops, 2 physical PCs)."
    assert lines[1] == "Major slow actions: 5 on 4 PCs, no usual to compare with yet."
    assert "System Crash (Critical): 1 PC, isolated, no usual to compare with yet." in lines
    assert "Application Crash (Major): 2 PCs, isolated, no usual to compare with yet." in lines
    assert lines[-1] == "Applications with the most Major slow actions: ERP (2), Mail (2), Mail Viewer (1)."
    text = " ".join(lines + [A.first(s)])
    assert not any(name in text for name in ("VD-1", "PC3", "PC4", "u-VD-1", "u-PC3"))  # no user, no PC


def test_the_verdict_is_by_fixed_rules():
    s = snap_of(filled())
    assert A.verdict(s) == A.UNKNOWN  # no usual known yet for anything
    s["slow"]["usual"] = U("within", 3, 6)
    assert A.verdict(s) == A.NORMAL
    # stage, Nov 2026: Low Disk Space on 2 PCs against a usual 0 to 1 made the whole day "Worse than
    # usual", and the counts shown did not have it: under 5 PCs it is isolated, listed, not the verdict
    s["events"][1]["usual"] = U("above")  # a Major event, on 2 PCs
    assert A.verdict(s) == A.NORMAL
    assert A.facts(s)["why"] == ["nothing rated Critical or Major is above its usual range on 5 PCs or more "
                                 "(Application Crash (Major): 2 PCs, isolated)"]
    s["events"][1]["pcs"] = 12
    assert A.verdict(s) == A.WORSE
    assert A.facts(s)["why"] == ["Application Crash (Major) is on 12 PCs, limited, above its usual range"]
    assert ("Verdict: Worse than usual. Because: Application Crash (Major) is on 12 PCs, limited, above its usual "
            "range.") in A.given(A.facts(s), "12:00")
    s["events"][0].update(usual=U("above"), pcs=60)  # a Critical one
    assert A.verdict(s) == A.ATTENTION and A.facts(s)["why"][0].startswith("System Crash (Critical) is on 60 PCs, widespread")
    # what the verdict rests on is always among the lines, however many kinds there are
    many = snap_of(filled())
    many["events"] += [dict(many["events"][1], name=f"Event {i}", usual=U("within")) for i in range(4)]
    many["events"].append(dict(many["events"][1], name="Low Disk Space", pcs=9, usual=U("above")))
    lines = A.today_lines(many)
    assert "Low Disk Space (Major): 9 PCs, limited, above its usual range (0 to 1 PCs by this time)." in lines
    assert "3 other kinds of event rated Critical or Major, none above its usual range." in lines
    calm = snap_of(filled())
    calm["down"] = ["gpt"]  # an AI service down is not the users' experience
    assert A.verdict(calm) == A.UNKNOWN and A.first(calm).startswith("System Crash on 1 PC (Critical, isolated)")
    calm["down"] = ["Aternity"]
    assert A.verdict(calm) == A.ATTENTION and A.first(calm).startswith("the connection to Aternity, which is down")
    assert A.first(s).startswith("System Crash on 60 PCs (Critical, widespread, above its usual range): on the Today tab")
    none = snap_of(filled())
    none["events"] = []
    assert A.first(none).startswith("nothing needs action now")
    # an assessment saved before Nov 12 said "more than usual": still read as above its usual range
    old = snap_of(filled())
    old["events"][1].update(usual="more than usual", pcs=12)
    old["slow"]["usual"] = "as usual"
    assert A.verdict(old) == A.WORSE and A.facts(old)["why"] == ["Application Crash (Major) is on 12 PCs, limited, "
                                                                 "above its usual range"]


def test_the_fixed_words():
    """The user's choice, Nov 12 2026: the words always mean the same."""
    assert [A.reach(n) for n in (1, 4, 5, 49, 50, 400)] == ["isolated", "isolated", "limited", "limited",
                                                            "widespread", "widespread"]
    assert A._against(U("above", 0, 1)) == "above its usual range (0 to 1 PCs by this time)"
    assert A._against(U("within", 1, 1)) == "within its usual range (1 PC by this time)"
    assert A._against(None) == "no usual to compare with yet" and A._against("fewer than usual") == "below its usual range"
    s = snap_of(filled())
    s["slow"]["usual"] = U("within", 3, 6)
    assert A.meaning(s) == "Against the working days before: Major slow actions are within their usual range."
    s["charts"] = {"crash": {"what": "PCs with a crash so far", "unit": "PCs", "above": [],
                             "now": {"h": 14, "v": 2, "lo": 3, "hi": 5}}}
    s["boots"] = {"unit": "s", "types": {"User Logon": {"n": 8, "typical": 43.5,
                                                        "usual": {"low": 39.5, "high": 44.0, "days": 3}}}}
    assert A.meaning(s) == ("Against the working days before: Major slow actions are within their usual range, PCs "
                            "with a crash so far are below their usual range and a typical User Logon is within its "
                            "usual range.")


def test_what_changed_since_the_assessment_before():
    t = filled()
    before = snap_of(t)
    f = A.facts(before)
    assert f["changed"] == [] and f["nothing_new"] is False and f["since"] is None
    assert "Changed: nothing to compare with yet (the first assessment of the day)." in A.given(f, "09:00")
    same = A.facts(before, [("09:00", before)])
    assert same["nothing_new"] is True and same["since"] == "09:00"
    assert same["changed"] == ["Steady since 09:00: System Crash (Critical) (1 PC), Application Crash (Major) (2 PCs)."]
    assert "Changed since 09:00:\n- Steady since 09:00: System Crash" in A.given(same, "12:00")
    # two more PCs with an Application Crash, a new kind of event, more Major actions in ERP
    t.set_health([ev(NOW - HOUR, d, "Application Crash", "Major", "Mail") for d in ("VD-1", "PC3", "PC6", "PC7")]
                 + [ev(NOW - HOUR, "PC4", "System Crash", "Critical", cat="System"),
                    ev(NOW - HOUR, "PC9", "Low Memory", "Major", cat="System")], H, today=True)
    t.add_hour(t.midnight(date(2026, 11, 9)) + 13 * HOUR, [act(NOW - HOUR, "PC3", "Major", "ERP") for _ in range(3)], F, NOW)
    after = snap_of(t)
    new = A.facts(after, [("09:00", before)])["changed"]
    assert "Application Crash (Major) rose from 2 to 4 PCs since 09:00: isolated, no usual to compare with yet." in new
    assert "Low Memory (Major) is new since 09:00: 1 PC, isolated, no usual to compare with yet." in new
    assert "Major slow actions added since 09:00, by application: ERP (3)." in new
    assert new[-1] == "Steady since 09:00: System Crash (Critical) (1 PC)."
    after["down"], after["care"]["issues"] = ["NetIM"], before["care"]["issues"] + 2
    more = A.facts(after, [("09:00", before)])["changed"]
    assert more[0] == "The connection to NetIM went down." and "Care list: 2 new issues for the users watched." in more
    assert "The connection to NetIM is back." in A.facts(before, [("12:00", after)])["changed"]
    # the share of actions rated Major in an hour can fall, against the hour of the assessment before
    share = {"what": "The share of actions rated Major", "unit": "%", "above": []}
    before["charts"] = {"share": dict(share, now={"h": 11, "v": 2.6, "lo": None, "hi": None})}
    after["charts"] = {"share": dict(share, now={"h": 14, "v": 1.9, "lo": 1.2, "hi": 2.4})}
    assert ("The share of actions rated Major fell: 2.6% at 11:00, 1.9% at 14:00, within its usual range (1.2% to "
            "2.4% at that hour).") in A.facts(after, [("12:00", before)])["changed"]


def test_what_to_watch_and_what_cleared_earlier():
    s = snap_of(filled())
    share = {"what": "The share of actions rated Major", "unit": "%"}
    s["charts"] = {"share": dict(share, above=[8, 9, 13, 14], now={"h": 14, "v": 3.0, "lo": 1.0, "hi": 2.0})}
    s["events"][1].update(usual=U("above"))  # Application Crash, 2 PCs: isolated
    f = A.facts(s)
    assert f["watch"] == ["Application Crash (Major) is on 2 PCs, above its usual range (0 to 1 PCs by this time): "
                          "isolated, too few to change the day's verdict.",
                          "The share of actions rated Major has been above its usual range since 13:00: 3% at 14:00, "
                          "against 1% to 2%."]
    assert f["earlier"] == ["The share of actions rated Major was above its usual range from 08:00 to 10:00."]
    s["charts"]["share"]["above"] = [8, 9]  # not above now
    assert A.facts(s)["earlier"] == ["The share of actions rated Major was above its usual range from 08:00 to 10:00; "
                                     "cleared since."]
    # above at an earlier assessment, within now: cleared; what moved is said once, under Changed
    was = snap_of(filled())
    was["events"][1].update(usual=U("above"))
    now = snap_of(filled())
    now["events"][1].update(usual=U("within", 1, 3), pcs=3)
    f = A.facts(now, [("09:00", was), ("12:00", snap_of(filled()))])
    assert ("Application Crash (Major) was above its usual range at 09:00; cleared since: within its usual range "
            "(1 to 3 PCs by this time) now.") in f["earlier"]
    now["events"][1].update(usual=U("above"))
    f = A.facts(now, [("12:00", was)])
    assert f["changed"][0].startswith("Application Crash (Major) rose from 2 to 3 PCs since 12:00")
    assert not any("Application Crash" in x for x in f["watch"])


def test_our_own_text_has_the_labels_and_the_shape():
    s = snap_of(filled())
    s["events"][1].update(usual=U("above"))
    text = A.ours(A.facts(s))
    lines = text.splitlines()
    assert lines[0].startswith("A normal day so far: nothing rated Critical or Major is above its usual range")
    assert lines[1].startswith("- Watch: Application Crash (Major) is on 2 PCs") and lines[-1].startswith("Next: look first at")
    assert len([x for x in lines if x.startswith("- ")]) >= 2  # the day so far fills up to two points
    assert A.refused(text, A.given(A.facts(s), "12:00")) is None


def test_the_ais_text_is_held_to_the_facts():
    f = A.facts(snap_of(filled()))
    good = "No usual to compare with yet.\n- System Crash is on 1 PC, rated Critical, isolated.\nNext: look at System Crash."
    ai = Ai(good)
    assert A.word(ai, f, "09:00", "gpt") == (good, None)
    assert ai.seen[0][0]["content"] == A.SYSTEM and "Verdict: No usual to compare with yet." in ai.seen[0][1]["content"]
    text, note = A.word(Ai("System Crash hit 37 PCs."), f, "09:00", "gpt")
    assert text is None and "(37)" in note and "these are the facts as counted" in note
    assert "twice" in A.word(Ai("Crashes are twice as many as hangs."), f, "09:00", "gpt")[1]
    assert "did not answer" in A.word(Ai(None), f, "09:00", "gpt")[1]
    assert "No AI service is set" in A.word(None, f, "09:00")[1]
    # the fixed words only where a line gives them (no line here says widespread, or above the usual range)
    assert "the word \"widespread\"" in A.word(Ai("System Crash is widespread."), f, "09:00", "gpt")[1]
    assert "\"above its usual" in A.word(Ai("System Crash is above its usual range."), f, "09:00", "gpt")[1]
    assert A.word(Ai("System Crash is isolated."), f, "09:00", "gpt")[0] == "System Crash is isolated."


def test_one_is_written_when_due_and_kept(tmp_path):
    t = filled()  # NOW is 14:30 in Chicago: the 12:00 one is owed
    a = A.Assessments(t, store=tmp_path)
    ai = Ai("No usual to compare with yet.\n- System Crash is on 1 PC.\nNext: look at System Crash.")
    assert a.due(NOW) == 12 and a.due(NOW - 9 * HOUR) is None  # 05:30: before the first
    assert a.due(NOW - 6 * HOUR) == 6  # 08:30: the 06:00 one
    a.tick(NOW, lambda: (ai, "gpt"))
    v = a.view(NOW)
    assert len(v["periods"]) == 1 and v["next"] == "15:00" and v["yesterday"] is None
    p = v["periods"][0]
    assert (p["label"], p["slot"], p["since"], p["verdict"], p["service"]) == ("14:30", 12, None, A.UNKNOWN, "gpt")
    assert p["text"].startswith("No usual to compare with yet.") and "snap" not in p and p["today"][0].startswith("Active: 4 users")
    assert p["ours"].startswith(A.UNKNOWN) and v["labels"] == ["Changed", "Watch", "Earlier", "Start-ups"]
    a.tick(NOW + timedelta(minutes=15), lambda: (ai, "gpt"))  # the same period: not written again
    assert len(a.view(NOW)["periods"]) == 1 and len(ai.seen) == 1
    a.tick(NOW + HOUR, lambda: (Ai("It is 99 PCs."), "gpt"))  # 15:30: the next, the AI's text refused
    v = a.view(NOW + HOUR)
    assert [p["label"] for p in v["periods"]] == ["15:30", "14:30"]  # latest first
    assert v["periods"][0]["text"] is None and "(99)" in v["periods"][0]["note"] and v["periods"][0]["since"] == "14:30"
    assert v["periods"][0]["nothing_new"] is True
    again = A.Assessments(t, store=tmp_path)  # after a restart: read back, nothing owed
    assert len(again.view(NOW + HOUR)["periods"]) == 2 and again.due(NOW + HOUR) is None
    assert "u-VD-1" not in (tmp_path / "assess" / "2026-11-09.json").read_text()
    not_ready = A.Assessments(filled(), store=tmp_path / "x")
    not_ready.today.ready = False
    not_ready.tick(NOW)
    assert not_ready.view(NOW)["periods"] == []


def test_yesterdays_summary_is_written_once_from_its_saved_counts(tmp_path):
    t = filled()
    t.health_days[date(2026, 11, 8)] = {("Application", "Application Crash"): {"PC1": 10.0, "PC2": 20.0}}
    t.saved[date(2026, 11, 8)] = {"complete": True, "pcs": 40, "top_pcs": 5, "apps": {"Mail": {"runs": 50, "Major": 6}}}
    a = A.Assessments(t, store=tmp_path)
    a.tick(NOW, lambda: (None, ""))
    y = a.view(NOW)["yesterday"]
    assert y["lines"][0] == "Sunday Nov 08: 40 PCs active; Major slow actions on 5 PCs."
    assert "Application Crash (Major): 2 PCs, isolated." in y["lines"]
    assert "Applications with the most Major slow actions: Mail (6)." in y["lines"] and y["text"] is None
    a.days["2026-11-08"]["summary"]["lines"] = ["kept"]
    a.tick(NOW + timedelta(minutes=15), lambda: (None, ""))
    assert a.view(NOW)["yesterday"]["lines"] == ["kept"]  # once
    late = A.Assessments(filled(), store=tmp_path / "none")  # no saved counts for the day before: none
    late.tick(NOW, lambda: (None, ""))
    assert late.view(NOW)["yesterday"] is None


def test_the_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("timezone: America/Chicago\n")
    app = web.create_app(str(cfg))
    got = TestClient(app).get("/api/assess").json()
    assert got["periods"] == [] and got["hours"] == [6, 9, 12, 15, 18] and got["yesterday"] is None


def test_start_ups_are_in_the_facts_and_software_is_not():
    """The user's change, Nov 2026: Software out of the assessment, start-up and logon times in."""
    from uxcorrelate.models import BootSample
    t = filled()
    day0 = t.midnight(date(2026, 11, 9))
    rows = [BootSample(day0 + HOUR * 8 + timedelta(minutes=k), f"PC{k}", type="User Logon", duration=40.0 + k) for k in range(7)]
    rows += [BootSample(day0 + HOUR * 9, "PC1", type="User Logon", duration=90.0),  # one alone in its hour: no point
             BootSample(day0 + HOUR * 8, "PC2", type="Machine Boot", duration=70.0)]
    b = A.boots_of(rows, t.local)
    assert b["User Logon"] == {"n": 8, "typical": 43.5, "hours": {"8": 43.0}} and b["Machine Boot"]["n"] == 1
    a = A.Assessments(t)
    a.days["2026-11-09"] = {"date": "2026-11-09", "periods": [], "summary": None, "boots": b}
    for d, typical in (("2026-11-06", 41.0), ("2026-11-05", 44.0), ("2026-11-04", 39.5)):
        a.days[d] = {"date": d, "periods": [], "summary": None,
                     "boots": {"User Logon": {"n": 30, "typical": typical, "hours": {"8": typical + 1}}}}
    v = a.boots_view(NOW)
    assert list(v["types"]) == ["User Logon"]  # Machine Boot: under 5 today, left out
    assert v["types"]["User Logon"] == {"n": 8, "typical": 43.5, "usual": {"low": 39.5, "high": 44.0, "days": 3}}
    f = A.facts(snap_of(t, boots=v))
    assert f["startups"] == ["A typical User Logon takes 43.5 s today (8 so far), within its usual range (39.5 to 44 s "
                             "on the 3 working days before)."]
    assert not any("Software" in x for x in f["today"] + f["startups"])
    del a.days["2026-11-04"]
    assert A.startup_lines(a.boots_view(NOW)) == ["A typical User Logon takes 43.5 s today (8 so far), no usual to "
                                                  "compare with yet."]
    # the chart: today's hours with 5 or more, the days before as a range only with 3 of them
    logon = next(c for c in a.charts(NOW) if c["key"] == "logon")
    assert logon["title"] == "Typical User Logon time, per hour" and logon["now"] == {"h": 8, "v": 43.0, "lo": None, "hi": None}
    assert logon["what"] == "The typical User Logon time"


def test_the_charts_are_today_up_to_the_hour_against_the_days_before():
    t = filled()
    day0 = t.midnight(date(2026, 11, 9))
    t.add_hour(day0 + 10 * HOUR, [act(day0 + 10 * HOUR, f"PC{k}", "Major" if k < 3 else "Normal") for k in range(60)], F, NOW)
    for back, share in ((3, 2), (4, 4), (5, 6)):  # Fri, Thu, Wed
        d = date(2026, 11, 9) - timedelta(days=back)
        t.saved[d] = {"complete": True, "top_pcs_by_hour": [0] * 24,
                      "hours": {"10": {"Major": share, "Normal": 100 - share}, "9": {"Major": 1, "Normal": 9}}}
        t.health_days[d] = {("Application", "Application Crash"): {f"PC{k}": 3600.0 * 9 for k in range(back)},
                            ("System", "Low Memory"): {"PC99": 100.0}}
    a = A.Assessments(t)
    share, crash = a.charts(NOW)[:2]
    ten = share["points"][10]
    assert ten == {"h": 10, "v": 5.0, "lo": 2.0, "hi": 6.0}  # 3 of 60; the days before 2% to 6%
    assert share["points"][9]["v"] is None and share["points"][9]["lo"] is None  # under 50 measured actions: no share
    assert share["points"][15]["v"] is None and share["now"]["h"] == 10  # nothing after the current hour (14:30)
    # PCs with a crash so far: VD-1 and PC3 (Application Crash) and PC4 (System Crash) at 09:00; each PC once
    assert [p["v"] for p in crash["points"][8:11]] == [0, 3, 3] and crash["points"][9]["lo"] == 3 and crash["points"][9]["hi"] == 5
    assert crash["now"] == {"h": 14, "v": 3, "lo": 3, "hi": 5}
    # no start-ups read: said in the chart's place, never a silent gap
    logon = a.charts(NOW)[2]
    assert logon["key"] == "logon" and logon["points"] == [] and "read with the next read" in logon["note"]

    class Down:
        warnings: list = []

        def boots(self, start, end):
            self.warnings.append("boot/logon data: HTTP 500")
            return []
    a.aternity_of = lambda: Down()
    a.tick(NOW, lambda: (None, ""))
    assert "could not be read (boot/logon data: HTTP 500)" in a.charts(NOW)[2]["note"]
    p = a.days["2026-11-09"]["periods"][0]  # the assessment is written all the same, with what the charts give
    assert p["snap"]["charts"]["share"] == {"what": "The share of actions rated Major", "unit": "%", "above": [],
                                            "now": {"h": 10, "v": 5.0, "lo": 2.0, "hi": 6.0}}
    assert "PCs with a crash so far are within their usual range" in p["meaning"]
    assert "kpis" not in a.view(NOW)  # Proposal 1: no row of numbers; the charts carry them, in Today's issues
