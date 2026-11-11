"""The assessment for executives: its facts (counts and names of events and applications only),
its fixed verdict, what is new since the one before, the AI's text held to the facts, and when
one is written."""

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
    return A.snapshot(t.view(now), kw.get("care"), kw.get("status"), kw.get("software"))


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
    assert "System Crash (Critical): 1 PC." in lines and "Application Crash (Major): 2 PCs." in lines
    assert lines[-1] == "Applications with the most Major slow actions: ERP (2), Mail (2), Mail Viewer (1)."
    text = " ".join(lines + [A.first(s)])
    assert not any(name in text for name in ("VD-1", "PC3", "PC4", "u-VD-1", "u-PC3"))  # no user, no PC


def test_the_verdict_is_by_fixed_rules():
    s = snap_of(filled())
    assert A.verdict(s) == A.UNKNOWN  # no usual known yet for anything
    s["slow"]["usual"] = "as usual"
    assert A.verdict(s) == A.NORMAL
    s["events"][1]["usual"] = "more than usual"  # a Major event
    assert A.verdict(s) == A.WORSE
    s["events"][0]["usual"] = "more than usual"  # a Critical one
    assert A.verdict(s) == A.ATTENTION
    calm = snap_of(filled())
    calm["down"] = ["gpt"]  # an AI service down is not the users' experience
    assert A.verdict(calm) == A.UNKNOWN and A.first(calm).startswith("System Crash on 1 PC (Critical)")
    calm["down"] = ["Aternity"]
    assert A.verdict(calm) == A.ATTENTION and A.first(calm).startswith("the connection to Aternity, which is down")
    assert A.first(s).startswith("System Crash on 1 PC (Critical, more than usual): on the Today tab")
    none = snap_of(filled())
    none["events"] = []
    assert A.first(none).startswith("nothing needs action now")


def test_what_is_new_since_the_assessment_before():
    t = filled()
    before = snap_of(t)
    assert A.new_lines(before, None) == [] and A.facts(before, None, None)["nothing_new"] is False
    same = A.facts(before, before, "09:00")
    assert same["new"] == [] and same["nothing_new"] is True
    assert same["open"] == ["System Crash (Critical): 1 PC.", "Application Crash (Major): 2 PCs."]
    assert "Still open from earlier:\nSystem Crash (Critical): 1 PC." in A.given(same, "12:00")
    assert "Nothing new." in A.given(same, "12:00") and "What is new since 09:00:" in A.given(same, "12:00")
    # two more PCs with an Application Crash, a new kind of event, more Major actions in ERP
    t.set_health([ev(NOW - HOUR, d, "Application Crash", "Major", "Mail") for d in ("VD-1", "PC3", "PC6", "PC7")]
                 + [ev(NOW - HOUR, "PC4", "System Crash", "Critical", cat="System"),
                    ev(NOW - HOUR, "PC9", "Low Memory", "Major", cat="System")], H, today=True)
    t.add_hour(t.midnight(date(2026, 11, 9)) + 13 * HOUR, [act(NOW - HOUR, "PC3", "Major", "ERP") for _ in range(3)], F, NOW)
    after = snap_of(t)
    new = A.new_lines(after, before)
    assert "Application Crash (Major): 4 PCs; it was 2 at the last assessment." in new
    assert "New: Low Memory (Major): 1 PC." in new
    assert "Major slow actions added since the last assessment, by application: ERP (3)." in new
    after["down"], after["care"]["issues"] = ["NetIM"], before["care"]["issues"] + 2
    more = A.new_lines(after, before)
    assert "The connection to NetIM went down." in more and "Care list: 2 new issues for the users watched." in more
    assert "The connection to NetIM is back." in A.new_lines(before, after)


def test_the_ais_text_is_held_to_the_facts():
    f = A.facts(snap_of(filled()), None, None)
    good = "No usual to compare with yet.\n- System Crash is on 1 PC, rated Critical.\nNext: look at System Crash."
    ai = Ai(good)
    assert A.word(ai, f, "09:00", "gpt") == (good, None)
    assert ai.seen[0][0]["content"] == A.SYSTEM and "Verdict: No usual to compare with yet." in ai.seen[0][1]["content"]
    text, note = A.word(Ai("System Crash hit 37 PCs."), f, "09:00", "gpt")
    assert text is None and "(37)" in note and "these are the facts as counted" in note
    assert "twice" in A.word(Ai("Crashes are twice as many as hangs."), f, "09:00", "gpt")[1]
    assert "did not answer" in A.word(Ai(None), f, "09:00", "gpt")[1]
    assert "No AI service is set" in A.word(None, f, "09:00")[1]


def test_one_is_written_when_due_and_kept(tmp_path):
    t = filled()  # NOW is 14:30 in Chicago: the 12:00 one is owed
    a = A.Assessments(t, store=tmp_path)
    ai = Ai("No usual to compare with yet.\n- System Crash is on 1 PC.\nNext: look at System Crash.")
    assert a.due(NOW) == 12 and a.due(NOW - 6 * HOUR) is None  # 08:30: before the first
    a.tick(NOW, lambda: (ai, "gpt"))
    v = a.view(NOW)
    assert len(v["periods"]) == 1 and v["next"] == "15:00" and v["yesterday"] is None
    p = v["periods"][0]
    assert (p["label"], p["slot"], p["since"], p["verdict"], p["service"]) == ("14:30", 12, None, A.UNKNOWN, "gpt")
    assert p["text"].startswith("No usual to compare with yet.") and "snap" not in p and p["today"][0].startswith("Active: 4 users")
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
    assert "Application Crash (Major): 2 PCs." in y["lines"]
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
    assert got["periods"] == [] and got["hours"] == [9, 12, 15, 18] and got["yesterday"] is None
