"""The chat on the Today tab: building blocks count, the AI only picks and words.
Made-up PCs, users and apps; a stand-in AI service."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from uxcorrelate import ai, web
from uxcorrelate.ai import AiClient
from uxcorrelate.chat import AFTER_HANG, EVENTS_ONLY, NO_COUNT, TOOLS, ask, chat_service, events, investigate, run_block, slow_actions, who
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
    mail = slow_actions(filled(), NOW, "app", app="mai")  # part of the name, any case: both Mail apps, said
    assert [r[0] for r in mail["rows"]] == ["Mail", "Mail Viewer"] and "matches 2 applications" in mail["notes"][0]
    exact = slow_actions(filled(), NOW, "app", app="mail")  # the exact name wins: not Mail Viewer too
    assert [r[0] for r in exact["rows"]] == ["Mail"] and not any("matches" in n for n in exact["notes"])


def test_slow_actions_per_hour_activity_and_day():
    t = filled()
    hours = slow_actions(t, NOW, "hour")
    assert hours["rows"] == [["09:00", 4, None], ["14:00", 1, None]]
    acts = slow_actions(t, NOW, "activity", app="Mai", days=3)
    assert acts["rows"] == [["Mail Launch", 1, 1, 40.0], ["Mail Open", 1, 1, 12.0],  # most first, then by name
                            ["Mail Viewer Open", 1, 1, 5.0]]  # "Mai" fits Mail Viewer too
    assert "for today only" in acts["notes"][-1] and acts["period"].startswith("today")
    days = slow_actions(t, NOW, "day", days=4)
    assert days["rows"] == [["Mon Nov 09 (so far)", 5, 4, 4], ["Sun Nov 08", None, None, None],
                            ["Sat Nov 07", None, None, None], ["Fri Nov 06", 7, 5, 40]]


def test_every_block_for_all_apps_says_how_today_compares_and_which_apps_lead():
    t = filled()  # one working day saved: not enough for a usual yet
    for group_by in ("app", "hour", "activity", "day", "pc_kind"):
        notes = slow_actions(t, NOW, group_by)["notes"]
        assert any(n.startswith("No usual yet: 1 of the 3 working days needed") for n in notes), group_by
    day = slow_actions(t, NOW, "day")["notes"]
    assert "Most Major slow actions today: ERP 2, Mail 2, Mail Viewer 1." in day  # gives the AI names to tell
    assert not any(n.startswith("Most") for n in slow_actions(t, NOW, "app")["notes"])  # the rows name them
    assert not any("usual" in n.lower() for n in slow_actions(t, NOW, "day", app="ERP")["notes"])  # all apps only
    for d in (date(2026, 11, 4), date(2026, 11, 5)):  # three working days saved: the tile's verdict
        t.saved[d] = dict(t.saved[date(2026, 11, 6)])
    day = slow_actions(t, NOW, "day")["notes"]
    assert day[0] == "More than usual: 3 PCs with a Major action by 14:00; the 3 working days before had 0 to 0."


def test_the_highest_share_is_named_only_with_enough_runs():
    t = filled()
    assert not any("Highest" in n for n in slow_actions(t, NOW, "app")["notes"])  # 3 runs at most: too few
    ten = t.midnight(date(2026, 11, 9)) + 10 * HOUR
    t.add_hour(ten, [act(ten, f"PC{i}", "Major" if i < 10 else "Normal", "Big") for i in range(200)]
               + [act(ten, f"PC{i}", "Major" if i < 5 else "Normal", "Small") for i in range(20)], F, NOW)
    r = slow_actions(t, NOW, "app")
    assert r["rows"][0][:4] == ["Big", 10, 200, 5]  # the most by count; Mail's 67% is of 3 runs only
    assert "Highest Major share, of the applications with 20 runs or more: Small, 5 of 20 runs (25%)." in r["notes"]


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


def test_active_users_and_pcs_today_and_since_the_last_full_hour():
    """The user's request, Nov 2026: how many users and PCs are active, virtual and physical."""
    from uxcorrelate.chat import active
    t = filled()
    a = t.view(NOW)["active"]  # VD-1, VD-2 (virtual), PC3, PC4 (physical) at 09:00; PC4 again at 14:00
    assert a["today"] == {"users": 4, "pcs": 4, "virtual": 2, "physical": 2, "not_known": 0, "users_virtual": 2,
                          "users_physical": 2, "users_both": 0}
    assert a["recent"]["since"] == "13:00" and (a["recent"]["users"], a["recent"]["pcs"]) == (1, 1)
    r = active(t, NOW)
    assert r["rows"] == [["Today so far", 4, 4, 2, 2], ["Since 13:00", 1, 1, 0, 1]]
    assert r["say"] == ("Active today so far: 4 users on 4 PCs (2 virtual, 2 physical). Since 13:00: 1 user on 1 PC.")
    assert run_block("active", {}, t, None, NOW)["title"] == "Active users and PCs"
    # a user on a virtual desktop and a physical PC is one user on both kinds
    t.add_hour(t.midnight(date(2026, 11, 9)) + 10 * HOUR, [dict(act(NOW - 4 * HOUR, "PC3", "Normal", "ERP", False), USERNAME="u-VD-1")], F, NOW)
    assert t.view(NOW)["active"]["today"]["users_both"] == 1 and t.view(NOW)["active"]["today"]["users"] == 4


def test_a_list_of_pcs_with_an_event_over_the_week_and_a_name_matched_by_a_shared_word():
    """Stage, Nov 2026: "List PCs name with System crash this week" gave today's one PC, "List all
    System Crash this week" gave counts per day, and "hard drive failure" found no event (HD Failure)."""
    t = filled()  # today: Application Crash on VD-1 (twice) and PC3, System Crash on PC4; Friday: PC8, PC9
    w = who(t, NOW, "event", name="application crash", days=4)
    assert w["title"] == "PCs with Application Crash" and w["total"] == "4 PCs"
    assert w["columns"] == ["User", "PC", "Days with it", "Last"]
    assert [x[:3] for x in w["rows"]] == [["u-VD-1", "VD-1", 1], ["u-PC3", "PC3", 1], ["—", "PC8", 1], ["—", "PC9", 1]]
    assert w["rows"][2][3].startswith("Fri Nov 06") and w["runs"][2]["device"] == "PC8" and w["runs"][2]["username"] is None
    assert any("2 of the 3 days before were not read" in n for n in w["notes"])
    assert any("user is known for today's events only" in n for n in w["notes"])
    assert [x[1] for x in who(t, NOW, "event", name="crash", app="erp", days=4)["rows"]] == ["PC3"]
    assert run_block("who", {"what": "event", "name": "System Crash", "days": 7}, t, None, NOW)["rows"][0][1] == "PC4"
    assert who(t, NOW, "event", name="nothing like it", days=7)["rows"] == []
    # a name no event has: those that share a word with it, said
    t.set_health([ev(NOW - HOUR, "PC5", "HD Failure", "Critical", cat="Hardware")], H, today=True)
    hd = events(t, NOW, name="hard drive failure", days=7)
    assert hd["rows"] == [["HD Failure", "Hardware", "Critical", 1]]
    assert any(n.startswith('No event is named like "hard drive failure": shown are those that share a word')
               for n in hd["notes"])
    assert EVENTS_ONLY not in hd["notes"]  # hang time says nothing of a disk
    assert hd["notes"][-1] == "Counts only: to see which PCs, ask to list the PCs with it."
    assert who(t, NOW, "event", name="hard drive failure")["rows"][0][1] == "PC5"
    assert "days" in next(x["function"] for x in TOOLS if x["function"]["name"] == "who")["parameters"]["properties"]


def test_markdown_marks_go_but_an_underscore_inside_a_name_stays():
    from uxcorrelate.chat import clean
    assert clean("**bad_module_info** stands out, _really_ at `2,148` PCs") == "bad_module_info stands out, really at 2,148 PCs"
    assert clean("# Title\n__bold__ and my_app_name.exe") == "Title\nbold and my_app_name.exe"
    # the answer's shape is kept: one line, points (any bullet becomes "- "), a Next line
    shaped = clean("Mail has the most.\n\n* **Mail**: 2 of 3\n1. ERP: 2 of 3\n• Viewer: 1\nNext: ask who had them.")
    assert shaped == "Mail has the most.\n- Mail: 2 of 3\n- ERP: 2 of 3\n- Viewer: 1\nNext: ask who had them."
    from uxcorrelate.chat import SHAPE, SYSTEM
    assert SHAPE in SYSTEM and "start with \"- \"" in SHAPE and "Next: " in SHAPE


def test_events_and_who_of_one_application_only():
    """Stage, Nov 2026: "how many Outlook crashed today" counted every application's crashes, and the
    who list led to a user whose Outlook had not crashed: neither block could take an application."""
    t = filled()
    r = events(t, NOW, name="crash", app="erp")
    assert r["title"] == 'PCs with "crash" events of ERP, by event' and r["columns"] == ["Event", "Category", "Severity", "PCs"]
    assert r["rows"] == [["Application Crash", "Application", "Major", 1]]  # PC3 only, not Mail's VD-1; no usual
    assert EVENTS_ONLY in r["notes"] and AFTER_HANG not in r["notes"]
    days = events(t, NOW, "day", app="mail", days=4)
    assert days["rows"][0] == ["Mon Nov 09 (so far)", 1] and days["rows"][-1] == ["Fri Nov 06", 2]
    none = events(t, NOW, app="outlook")
    assert none["rows"] == [] and 'No application named like "outlook"' in none["notes"][0]
    w = who(t, NOW, "event", app="ERP", name="application crash")
    assert [x[1] for x in w["rows"]] == ["PC3"]
    assert EVENTS_ONLY in w["notes"] and w["title"] == "Users and PCs: Application Crash, of ERP"
    assert [x[1] for x in who(t, NOW, "event", name="application crash")["rows"]] == ["VD-1", "PC3"]
    assert who(t, NOW, "event", app="outlook", name="crash")["rows"] == []
    # two kinds with "crash" in their name: both listed, each row saying which; per day they are told
    t.set_health([ev(NOW - HOUR, "VD-1", "Application Crash", "Major", "Mail"),
                  ev(NOW - HOUR, "PC7", "Application Crash (After Hang)", "Major", "Mail Viewer"),
                  ev(NOW - HOUR, "PC3", "Application Crash", "Major", "ERP")], H, today=True)
    both = who(t, NOW, "event", name="application crash")
    assert both["columns"] == ["User", "PC", "Times", "Event", "When"] and AFTER_HANG in both["notes"]
    assert sorted(x[3] for x in both["rows"]) == ["Application Crash", "Application Crash", "Application Crash (After Hang)"]
    assert "matches 2 kinds of event, all listed" in both["notes"][0]
    assert [x[1] for x in who(t, NOW, "event", name="crash", app="mail")["rows"]] == ["VD-1"]  # not Mail Viewer's PC7
    assert sorted(x[1] for x in who(t, NOW, "event", name="crash", app="mai")["rows"]) == ["PC7", "VD-1"]
    per_day = events(t, NOW, "day", name="application crash")
    assert per_day["rows"][0][1] == 3 and "matches 2 kinds of event, counted together" in per_day["notes"][0]
    assert events(t, NOW, name="crash", app="mail")["rows"] == [["Application Crash", "Application", "Major", 1]]
    tool = next(x["function"] for x in TOOLS if x["function"]["name"] == "events")
    assert "app" in tool["parameters"]["properties"]


def test_the_events_usual_in_one_note_more_than_usual_first():
    t = filled()
    assert not any(n.startswith("Against") for n in events(t, NOW, "event")["notes"])  # not enough days yet
    t.set_health([], H, today=False, days_read=(date(2026, 11, 4), date(2026, 11, 5)))  # 3 working days read
    r = events(t, NOW, "event")
    assert [row[4] for row in r["rows"]] == ["as usual (0 to 2)", "more than usual (0 to 0)"]
    assert r["notes"][-1] == ("Against the days before, by this time of day: more than usual: System Crash; "
                              "as usual: 1 of the 2.")


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


def test_words_about_usual_must_be_what_the_counts_say():
    q = "How are slow actions today?"
    pick = {q: ("slow_actions", {"group_by": "app"})}

    def said(t, words, picks=pick):
        return ask(q, [], ai_service(picks, lambda g: words, []), t, None, NOW)
    t = filled()  # no usual yet (seen on stage: the AI said "As usual" all the same)
    a = said(t, "As usual so far, ERP had 2.")
    assert a["answer"].startswith("Major slow actions by application") and '"As usual"' in a["note"]
    assert said(t, "There is no usual yet; ERP had 2, the most.")["note"] is None
    assert said(t, "It is too early to say whether this is unusual; ERP had 2.")["note"] is None
    assert '"not unusual"' in said(t, "ERP had 2, which is not unusual.")["note"]  # = as usual
    for d in (date(2026, 11, 4), date(2026, 11, 5)):  # three working days saved: the tile says More than usual
        t.saved[d] = dict(t.saved[date(2026, 11, 6)])
    assert said(t, "More Major slow actions than usual; ERP had 2.")["note"] is None
    for words in ("Fewer than usual so far; ERP had 2.", "As usual, ERP had 2."):
        assert "do not say so" in said(t, words)["note"]
    crashes = {q: ("events", {"group_by": "event"})}  # its Usual column holds words: "not enough days" here
    assert '"More crashes than usual"' in said(t, "More crashes than usual on 2 PCs.", crashes)["note"]
    hours = {q: ("slow_actions", {"group_by": "hour"})}  # a Usual column with values: each hour read as it is
    assert said(t, "09:00 was below usual with 4 against 7.", hours)["note"] is None  # though the day is More


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
