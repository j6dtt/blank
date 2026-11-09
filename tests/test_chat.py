"""The chat on the Today tab: building blocks count, the AI only picks and words.
Made-up PCs, users and apps; a stand-in AI service."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from uxcorrelate import ai, web
from uxcorrelate.ai import AiClient
from uxcorrelate.chat import NO_COUNT, ask, chat_service, events, investigate, run_block, slow_actions, who
from uxcorrelate.config import AiService, Settings
from uxcorrelate.names import NameList
from uxcorrelate.today import HOUR, Today

ZONE = "America/Chicago"
NOW = datetime(2026, 11, 9, 20, 30, tzinfo=timezone.utc)  # Monday 14:30 in Chicago
F = {"timestamp": "TIMEFRAME", "username": "USERNAME", "device": "DEVICE_NAME", "application": "APPLICATION_NAME",
     "status": "SLA_STATUS", "activity": "ACTIVITY_NAME", "response_time": "ACTIVITY_RESPONSE_TIME",
     "virtualization": "VIRTUALIZATION"}
H = {"timestamp": "TIMEFRAME", "device": "DEVICE_NAME", "category": "HEALTH_EVENT_CATEGORY",
     "name": "HEALTH_EVENT_NAME", "severity": "HEALTH_EVENT_SEVERITY", "username": "USERNAME",
     "application": "APPLICATION_NAME"}


def act(t, pc, status, app="ERP", virtual=False, secs=5.0, activity="Open"):
    return {"TIMEFRAME": t.isoformat(), "USERNAME": f"u-{pc}", "DEVICE_NAME": pc, "APPLICATION_NAME": app,
            "SLA_STATUS": status, "ACTIVITY_NAME": activity, "ACTIVITY_RESPONSE_TIME": secs,
            "VIRTUALIZATION": "true" if virtual else "false"}


def ev(t, pc, name, sev, app=None, cat="Application"):
    return {"TIMEFRAME": t.isoformat(), "DEVICE_NAME": pc, "HEALTH_EVENT_CATEGORY": cat, "HEALTH_EVENT_NAME": name,
            "HEALTH_EVENT_SEVERITY": sev, "USERNAME": f"u-{pc}", "APPLICATION_NAME": app}


def filled() -> Today:
    t = Today(ZONE)
    nine = t.midnight(date(2026, 11, 9)) + 9 * HOUR  # 09:00 Chicago
    t.add_hour(nine, [act(nine, "VD-1", "Major", "Mail", True, 40.0, "Launch"),
                      act(nine + timedelta(minutes=20), "VD-1", "Major", "Mail", True, 12.0),
                      act(nine, "VD-2", "Major", "ERP", True), act(nine, "PC3", "Major", "ERP"),
                      act(nine, "PC3", "Normal", "ERP"), act(nine, "PC4", "Normal", "Mail")], F, NOW)
    t.add_hour(nine + 5 * HOUR, [act(nine + 5 * HOUR, "PC4", "Major", "Mail Viewer")], F, NOW)  # 14:00
    t.saved[date(2026, 11, 6)] = {"complete": True, "hours": {"9": {"Major": 7}}, "top_pcs": 5, "pcs": 40,
                                  "top_pcs_by_hour": [0] * 24,
                                  "apps": {"Mail": {"runs": 50, "Major": 6, "pcs": 4},
                                           "ERP": {"runs": 20, "Major": 1, "pcs": 1}},
                                  "kinds": {"virtual": {"runs": 30, "Major": 6, "pcs": 10, "top_pcs": 4},
                                            "physical": {"runs": 40, "Major": 1, "pcs": 30, "top_pcs": 1}}}
    t.set_health([ev(nine, "VD-1", "Application Crash", "Major", "Mail"),
                  ev(nine + HOUR, "VD-1", "Application Crash", "Major", "Mail"),
                  ev(nine, "PC3", "Application Crash", "Major", "ERP"),
                  ev(nine, "PC4", "System Crash", "Critical", cat="System")], H, today=True)
    fri = t.midnight(date(2026, 11, 6)) + 9 * HOUR
    t.set_health([ev(fri, "PC8", "Application Crash", "Major", "Mail"),
                  ev(fri, "PC9", "Application Crash", "Major", "Mail")], H, today=False,
                 days_read=(date(2026, 11, 6),))
    t.ready = True
    return t


def test_slow_actions_by_application_today_and_with_the_nights_saved_counts():
    r = slow_actions(filled(), NOW, "app")
    assert r["rows"] == [["ERP", 2, 3, 67, 2], ["Mail", 2, 3, 67, 1], ["Mail Viewer", 1, 1, 100, 1]]
    assert r["total"] == "5 Major slow actions in 3 applications" and "today (Mon Nov 09, 2026)" in r["period"]
    assert any("3 working days needed" in n for n in r["notes"])  # the tile's usual, in words
    three = slow_actions(filled(), NOW, "app", days=4)  # Fri saved; Sat, Sun not
    assert three["rows"][0] == ["Mail", 8, 53, 15] and three["rows"][1] == ["ERP", 3, 23, 13]
    assert "2 of the 3 days before have no saved counts" in three["notes"][0]
    mail = slow_actions(filled(), NOW, "app", app="mail")  # any case, part of the name: both Mail apps
    assert [r[0] for r in mail["rows"]] == ["Mail", "Mail Viewer"] and "matches 2 applications" in mail["notes"][0]


def test_slow_actions_per_hour_activity_and_day():
    t = filled()
    hours = slow_actions(t, NOW, "hour")
    assert hours["rows"] == [["09:00", 4, None], ["14:00", 1, None]]
    acts = slow_actions(t, NOW, "activity", app="Mail", days=3)
    assert acts["rows"] == [["Mail Launch", 1, 1, 40.0], ["Mail Open", 1, 1, 12.0],  # most first, then by name
                            ["Mail Viewer Open", 1, 1, 5.0]]  # "Mail" fits Mail Viewer too
    assert "for today only" in acts["notes"][-1] and acts["period"].startswith("today")
    days = slow_actions(t, NOW, "day", days=4)
    assert days["rows"] == [["Mon Nov 09 (so far)", 5, 4, 4], ["Sun Nov 08", None, None, None],
                            ["Sat Nov 07", None, None, None], ["Fri Nov 06", 7, 5, 40]]


def test_virtual_desktops_against_physical_pcs():
    r = slow_actions(filled(), NOW, "pc_kind")
    assert r["rows"] == [["Virtual desktops", 3, 3, 100, 2, 2], ["Physical PCs", 4, 2, 50, 2, 2]]
    week = slow_actions(filled(), NOW, "pc_kind", days=7)  # with Friday's saved counts, no PCs added up
    assert week["rows"] == [["Virtual desktops", 33, 9, 27], ["Physical PCs", 44, 3, 7]]


def test_events_by_event_day_and_application():
    t = filled()
    r = events(t, NOW)
    assert [x[:4] for x in r["rows"]] == [["Application Crash", "Application", "Major", 2],
                                          ["System Crash", "System", "Critical", 1]]
    assert r["rows"][0][4] == "not enough days"
    days = events(t, NOW, "day", name="application crash", days=4)
    assert days["rows"] == [["Mon Nov 09 (so far)", 2], ["Sun Nov 08", None], ["Sat Nov 07", None],
                            ["Fri Nov 06", 2]]
    assert "2 of the 3 days before were not read" in days["notes"][0]
    apps = events(t, NOW, "app", days=7)
    assert apps["rows"][0] == ["Mail", 3, "Application Crash"] and ["ERP", 1, "Application Crash"] in apps["rows"]
    assert ["(no application)", 1, "System Crash"] in apps["rows"] and any("no application" in n for n in apps["notes"])
    assert events(t, NOW, severity="critical")["rows"][0][0] == "System Crash"


def test_who_lists_users_and_pcs_with_a_run_for_each():
    t = filled()
    r = who(t, NOW, "slow", app="mail")  # "mail" fits two apps: the one with the most is listed
    assert r["rows"] == [["u-VD-1", "VD-1", 2, "Mail Launch 40.0 s", "09:00–09:20"]]
    assert r["runs"][0] == {"username": "u-VD-1", "device": "VD-1", "application": "Mail",
                            "start": "2026-11-09T14:30:00Z", "end": "2026-11-09T15:50:00Z"}
    crash = who(t, NOW, "event", name="application crash")
    assert [x[:3] for x in crash["rows"]] == [["u-VD-1", "VD-1", 2], ["u-PC3", "PC3", 1]]
    assert "No event named like" in who(t, NOW, "event", name="blue screen")["notes"][0]


def test_investigate_takes_known_names_and_asks_when_unsure():
    t, names = filled(), NameList()
    names.add([{"USERNAME": "u-VD-1", "DEVICE_NAME": "VD-1"}, {"USERNAME": "u-VD-2", "DEVICE_NAME": "VD-2"}],
              F, NOW - HOUR, NOW)
    r = investigate(t, names, NOW, username="U-VD-1")  # any case
    assert r["action"]["username"] == "u-VD-1" and r["action"]["start"] == "2026-11-09T14:30:00Z"
    assert "Around their Major slow actions today" in r["notes"][0]
    unsure = investigate(t, names, NOW, username="vd")
    assert unsure["action"] is None and unsure["rows"] == [["u-VD-1"], ["u-VD-2"]]
    one = investigate(t, names, NOW, device="vd-2", hours=2)
    assert one["action"]["device"] == "VD-2" and one["action"]["start"] == "2026-11-09T18:30:00Z"
    assert investigate(t, names, NOW)["notes"] == ["Name a user or a PC to investigate."]
    assert investigate(t, NameList(), NOW, username="newname")["action"]["username"] == "newname"  # list empty


def ai_service(picks: dict, words, seen: list):
    """A stand-in AI: picks per question, then words what it was given with `words(given)`."""
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append(body)
        last = body["messages"][-1]
        if last["role"] == "tool":
            return reply(words(json.loads(last["content"])))
        pick = picks.get(last["content"])
        if isinstance(pick, tuple):
            return reply(None, [{"id": "c1", "type": "function",
                                 "function": {"name": pick[0], "arguments": json.dumps(pick[1])}}])
        return reply(pick)

    def reply(content, calls=None):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content,
                                                                  "tool_calls": calls}}]})
    return AiClient(AiService(name="gpt", base_url="https://ai.test/v1", model="m"),
                    transport=httpx.MockTransport(handler))


Q = "Which apps had the most Major slow actions today?"


def test_the_ais_sentence_is_kept_when_its_numbers_are_ours():
    seen = []
    client = ai_service({Q: ("slow_actions", {"group_by": "app"})},
                        lambda g: f"**ERP** and Mail had {g['rows'][0][1]} each, of {g['total'].split()[0]}.", seen)
    a = ask(Q, [{"q": "earlier?", "a": "Earlier answer."}], client, filled(), None, NOW)
    assert a["answer"] == "ERP and Mail had 2 each, of 5." and a["note"] is None  # markdown marks dropped
    assert a["result"]["block"] == "slow_actions" and a["result"]["rows"][0][0] == "ERP"
    roles = [m["role"] for m in seen[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user", "assistant", "tool"]  # history, the pick, our counts
    assert "Monday Nov 09, 2026" in seen[0]["messages"][0]["content"] and seen[0]["tools"]


def test_a_sentence_with_a_number_not_in_the_counts_is_replaced_by_ours():
    client = ai_service({Q: ("slow_actions", {"group_by": "app"})}, lambda g: "ERP had 40% of 12 slow actions.", [])
    a = ask(Q, [], client, filled(), None, NOW)
    assert a["answer"].startswith("Major slow actions by application, today")
    assert "ERP: 2 Major slow actions; Mail: 2 Major slow actions" in a["answer"]
    assert "(12, 40)" in a["note"]


def test_a_comparison_worked_out_by_the_ai_is_replaced_by_ours():
    q = "Are virtual desktops slower?"
    for words in ("Virtual desktops were 3 times worse.", "Virtual desktops had twice as many."):
        client = ai_service({q: ("slow_actions", {"group_by": "pc_kind"})}, lambda g, w=words: w, [])
        a = ask(q, [], client, filled(), None, NOW)
        assert a["answer"].startswith("Major slow actions, virtual desktops") and "comparison" in a["note"]
        assert "Virtual desktops: 3 Major slow actions; Physical PCs: 2 Major slow actions" in a["answer"]
    fine = ai_service({q: ("slow_actions", {"group_by": "pc_kind"})},
                      lambda g: "All 3 runs on virtual desktops were Major, against 2 of 4 on physical PCs.", [])
    assert ask(q, [], fine, filled(), None, NOW)["note"] is None


def test_no_building_block_no_numbers():
    client = ai_service({"Weather?": "I can only answer about PCs.", "Guess?": "About 300 crashes."},
                        lambda g: "", [])
    assert ask("Weather?", [], client, filled(), None, NOW)["answer"] == "I can only answer about PCs."
    a = ask("Guess?", [], client, filled(), None, NOW)
    assert a["answer"] == NO_COUNT and "no counts behind them" in a["note"]
    odd = ai_service({Q: ("weather", {})}, lambda g: "", [])
    assert "we do not have (weather)" in ask(Q, [], odd, filled(), None, NOW)["note"]
    assert run_block("slow_actions", {"group_by": "nonsense", "days": "x"}, filled(), None, NOW)["args"] == \
        {"group_by": "app", "app": None, "days": 1}  # odd settings: the defaults


def test_the_chat_service_is_the_named_one_or_the_first():
    s = Settings()
    assert chat_service(s) is None
    s.ai.services = [AiService(name="gpt", base_url="https://a/v1", model="m"),
                     AiService(name="nemo", base_url="https://b/v1", model="n")]
    assert chat_service(s).name == "gpt"
    s.ai.chat = "nemo"
    assert chat_service(s).name == "nemo"


def test_the_endpoint(monkeypatch, tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}\n")
    http = TestClient(web.create_app(str(empty)))
    assert http.post("/api/chat", json={"question": "hi"}).status_code == 503  # no AI service set
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  services:\n    - name: gpt\n      base_url: https://ai.test/v1\n      model: m\n")
    app = web.create_app(str(cfg))
    stand_in = ai_service({Q: ("events", {})}, lambda g: "Application Crash is on the most PCs.", [])
    monkeypatch.setattr(ai, "AiClient", lambda *a, **k: stand_in)
    http = TestClient(app)
    assert "Still reading" in http.post("/api/chat", json={"question": Q}).json()["detail"]
    app.state.today.__dict__.update(filled().__dict__)  # what the reader would hold
    a = http.post("/api/chat", json={"question": Q, "history": []}).json()
    assert a["answer"] == "Application Crash is on the most PCs." and a["result"]["block"] == "events"
    assert http.post("/api/chat", json={"question": "  "}).status_code == 400


@pytest.mark.parametrize("value,kind", [("true", "virtual"), ("False", "physical"), (None, "not known")])
def test_pc_kind(value, kind):
    from uxcorrelate.today import pc_kind
    assert pc_kind(value) == kind
