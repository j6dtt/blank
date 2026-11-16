"""The Care list tab: named users checked every 15 minutes against their own usual.
Made-up users, PCs and apps."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from uxcorrelate import web
from uxcorrelate.aternity import AternityClient
from uxcorrelate.care import CARE_MAX, Care
from uxcorrelate.config import AternityConfig, CareUser, load_settings
from uxcorrelate.models import failing_disk
from uxcorrelate.today import HOUR, Today

ZONE = "America/New_York"
NOW = datetime(2026, 11, 9, 19, 30, tzinfo=timezone.utc)  # Monday 14:30 in New York
F = {"timestamp": "TIMEFRAME", "username": "USERNAME", "device": "DEVICE_NAME", "application": "APPLICATION_NAME",
     "status": "SLA_STATUS", "activity": "ACTIVITY_NAME", "response_time": "ACTIVITY_RESPONSE_TIME",
     "virtualization": "VIRTUALIZATION"}
H = {"timestamp": "TIMEFRAME", "device": "DEVICE_NAME", "category": "HEALTH_EVENT_CATEGORY",
     "name": "HEALTH_EVENT_NAME", "severity": "HEALTH_EVENT_SEVERITY", "username": "USERNAME",
     "application": "APPLICATION_NAME"}
USERS = [CareUser("mq2208", "Payroll lead"), CareUser("tz9013"), CareUser("ghost1")]


def act(t, user, pc, status, app="Mail", virtual=True):
    return {"TIMEFRAME": t.isoformat(), "USERNAME": user, "DEVICE_NAME": pc, "APPLICATION_NAME": app,
            "SLA_STATUS": status, "ACTIVITY_NAME": "Open", "ACTIVITY_RESPONSE_TIME": 9.0,
            "VIRTUALIZATION": "true" if virtual else "false"}


def today_with(majors: int, event=None) -> Today:
    """MQ2208 (as Aternity writes it) with `majors` Major actions on VD-7 from 14:00, 10 min apart; tz9013
    with one."""
    t = Today(ZONE)
    two = t.midnight(date(2026, 11, 9)) + 14 * HOUR
    rows = [act(two + timedelta(minutes=10 * k), "MQ2208", "VD-7", "Major") for k in range(majors)]
    rows += [act(two, "MQ2208", "VD-7", "Normal"), act(two, "tz9013", "PC-3", "Major", "ERP", False)]
    t.add_hour(two, rows, F, NOW)
    t.set_health(event or [], H, today=True)
    t.ready = True
    return t


def history(request: httpx.Request):
    """8 days of activity, by username: MQ2208 0-2 Major by 14:30 on the latest 5 days, a weekend
    among them; tz9013 1-2."""
    flt = request.url.params["$filter"].lower()
    tz = Today(ZONE)
    rows = []
    for back, majors in ((1, 1), (2, 2), (3, 1), (4, 2), (5, 0), (6, 9), (7, 9)):  # Sun 1, Sat 2; Tue 9 and Mon
        # 9 are past the latest 5 days
        d = date(2026, 11, 9) - timedelta(days=back)
        ten = tz.midnight(d) + 10 * HOUR
        if "'mq2208'" in flt:
            rows += [act(ten, "MQ2208", "VD-7", "Major") for _ in range(majors)] + [act(ten, "MQ2208", "VD-7", "Normal")]
            rows.append(act(tz.midnight(d) + 22 * HOUR, "MQ2208", "VD-7", "Major"))  # after 14:30: not by this time
        if "'tz9013'" in flt:
            rows += [act(ten, "tz9013", "PC-3", "Major") for _ in range(min(majors, 3) or 1)]
    return httpx.Response(200, json={"value": rows})


def client(seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request.url.params["$filter"])
        return history(request)
    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0", username="u", password="p")
    return AternityClient(cfg, transport=httpx.MockTransport(handler))


def ev(t, user, pc, name, sev, cat="System", app=None):
    return {"TIMEFRAME": t.isoformat(), "DEVICE_NAME": pc, "HEALTH_EVENT_CATEGORY": cat, "HEALTH_EVENT_NAME": name,
            "HEALTH_EVENT_SEVERITY": sev, "USERNAME": user, "APPLICATION_NAME": app}


def test_more_major_actions_than_on_their_own_days_before_is_an_issue_and_queues_one_investigation():
    seen = []
    care = Care(today_with(3), lambda: USERS)
    care.check(client(seen), NOW)
    assert len(seen) == 3 and "USERNAME eq 'mq2208'" in seen[0]  # their own days, by username
    assert "TIMEFRAME ge 2026-11-01T04:00:00Z" in seen[0]  # 8 days before today's midnight (New York)
    v = care.view(NOW)
    assert v["count"] == 1 and [u["title"] for u in v["users"]] == ["Payroll lead", "ghost1", "tz9013"]
    mq = v["users"][0]
    assert (mq["status"], mq["level"], mq["since_local"], mq["username"]) == ("issue", "Major", "14:20", "MQ2208")
    # the latest 5 days with a PC in use, the weekend too: people on the Care list often work weekends
    assert mq["usual"] == {"enough": True, "days": 5, "low": 0, "high": 2, "kind": "days"}
    assert mq["issues"][0]["text"] == ("3 Major slow actions by 14:20; on the 5 days before that they used a PC: "
                                       "0 to 2 by the same time. Most in Mail (3).")
    assert care._said(mq["issues"][0], mq) == ("3 Major slow actions by 19:20 UTC (usual by then 0-2), the last at "
                                               "19:20 UTC, most in Mail")  # the status summary
    assert mq["pcs"] == [{"device": "VD-7", "kind": "virtual"}] and (mq["actions"], mq["major"]) == (4, 3)
    # each Major action listed, latest first: the count with nothing behind it read as a bug (stage)
    assert [(m["at_local"], m["app"], m["activity"], m["secs"], m["pc"]) for m in mq["majors"]] == [
        ("14:20", "Mail", "Open", 9.0, "VD-7"), ("14:10", "Mail", "Open", 9.0, "VD-7"),
        ("14:00", "Mail", "Open", 9.0, "VD-7")]
    assert len(v["users"][2]["majors"]) == v["users"][2]["major"] == 1  # an OK tile lists its one too
    assert mq["investigating"] == "waiting" and care.pending == ["mq2208"]
    assert v["users"][1]["status"] == "unknown" and "check the name" in v["users"][1]["note"]
    assert v["users"][2]["status"] == "ok"  # 1 Major, usual 1 to 3


def test_the_name_is_asked_for_as_aternity_writes_it():
    from uxcorrelate.names import NameList
    names, seen = NameList(), []
    names.add([{"USERNAME": "MQ2208", "DEVICE_NAME": "VD-7"}], F, NOW - HOUR, NOW)
    Care(today_with(0), lambda: USERS[:1], names=names).check(client(seen), NOW)
    assert "USERNAME eq 'MQ2208'" in seen[0]  # care_list says mq2208


def test_the_reader_checks_the_care_list_after_each_read():
    from uxcorrelate.today import keep_reading

    class Stop(Exception):
        pass

    class Stub:
        def __init__(self):
            self.checks = []

        def check(self, client, now, read=True):
            self.checks.append(now)
    stub, sleeps = Stub(), []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) > 1:
            raise Stop()
    try:
        keep_reading(Today(ZONE), None, lambda: client(), sleep, clock=lambda: NOW, care=stub)
    except Stop:
        pass
    assert len(stub.checks) == 2  # after the first read, and after the next one


def test_as_many_as_on_a_day_before_is_not_an_issue():
    care = Care(today_with(2), lambda: USERS[:1])
    care.check(client(), NOW)
    assert care.view(NOW)["count"] == 0 and care.pending == []


def test_a_critical_or_major_event_is_an_issue_a_minor_one_is_not():
    t0 = datetime(2026, 11, 9, 19, 5, tzinfo=timezone.utc)  # 14:05 New York
    events = [ev(t0, "MQ2208", "VD-7", "System Crash", "Critical"),
              ev(t0, "MQ2208", "VD-7", "Application Crash", "Major", "Application", "Mail"),
              ev(t0 + timedelta(minutes=20), "MQ2208", "VD-7", "Application Crash", "Major", "Application", "Mail"),
              ev(t0, "MQ2208", "VD-7", "Low Memory Pagefaults", "Minor")]
    care = Care(today_with(0, events), lambda: USERS[:1])
    care.check(client(), NOW)
    mq = care.view(NOW)["users"][0]
    assert mq["level"] == "Critical" and mq["since_local"] == "14:05"
    assert [i["text"] for i in mq["issues"]] == [
        "System Crash (System), first at 14:05 on VD-7.",
        "Application Crash (Application) 2 times in Mail, first at 14:05, the last at 14:25 on VD-7."]


def test_an_event_is_over_after_an_hour_with_no_new_one_and_a_later_one_is_a_new_issue():
    at = lambda h, m: datetime(2026, 11, 9, h + 5, m, tzinfo=timezone.utc)  # noqa: E731  (New York time)
    crash = lambda h, m: ev(at(h, m), "MQ2208", "VD-7", "Application Crash", "Major", "Application", "Mail")  # noqa
    care = Care(today_with(0, [crash(9, 0), crash(14, 0), crash(14, 40)]), lambda: USERS[:1])
    read(care, at(14, 50))
    issues = care.issues["mq2208"]  # 09:00 was over before the user's first check: kept, not lit
    assert [i["clears"] for i in care.view(at(14, 50))["users"][0]["issues"]] == [
        "Clears at the 15:35 read if no new Application Crash."]  # the 3rd read of 15 min after the one at 14:50
    assert [(i["since"][11:16], "cleared" in i) for i in issues] == [("14:00", True), ("19:00", False)]  # UTC
    assert care.pending == ["mq2208"]
    care.run_next(lambda u, s, e: {"answer": {"title": "Mail crashed on VD-7"}}, at(14, 50))
    for m in (5, 20, 35):  # the reads after 14:50: off at the 3rd
        care.check(client(), at(15, m))
    care.check(client(), at(15, 45))  # 65 min after 14:40
    mq = care.view(at(15, 45))["users"][0]
    assert (mq["status"], mq["issues"]) == ("ok", [])
    assert [i["over"] for i in mq["earlier"]] == ["none in the hour after 09:00", "none in the hour after 14:40"]
    assert mq["earlier"][1]["text"] == "Application Crash (Application) 2 times in Mail, first at 14:00, " \
                                       "the last at 14:40 on VD-7."
    # 17:00: a new crash, a new issue and a new investigation
    care.today.set_health([crash(9, 0), crash(14, 0), crash(14, 40), crash(17, 0)], H, today=True)
    care.check(client(), at(17, 5))
    assert len(care.issues["mq2208"]) == 3 and care.view(at(17, 5))["users"][0]["status"] == "issue"
    assert care.pending == ["mq2208"]
    # "Investigate again now" and the one that runs by itself: from 30 min before the day's first
    # issue, over or not (stage, Nov 2026: the button took the open 17:00 one, leaving 09:00 out)
    assert care.view(at(17, 5))["users"][0]["start"] == "2026-11-09T13:30:00Z"  # 08:30 New York
    assert care.detail("mq2208", at(17, 5))["start"] == "2026-11-09T13:30:00Z"
    calls = []
    care.run_next(lambda u, s, e: calls.append(s) or {"answer": {"title": "T"}}, at(17, 5))
    assert calls == [datetime(2026, 11, 9, 13, 30, tzinfo=timezone.utc)]


def test_an_open_event_saved_by_the_build_before_is_the_same_issue():
    t0 = datetime(2026, 11, 9, 19, 5, tzinfo=timezone.utc)  # 14:05 New York
    care = Care(today_with(0, [ev(t0, "MQ2208", "VD-7", "System Crash", "Critical")]), lambda: USERS[:1])
    care.issues["mq2208"] = [{"key": "event 2026-11-09 System/System Crash", "kind": "event", "level": "Critical",
                              "since": "2026-11-09T19:05:00Z", "day": "2026-11-09", "text": "System Crash"}]
    care.reports["mq2208"] = {"at": "2026-11-09T19:10:00Z", "start": "2026-11-09T18:35:00Z",
                              "end": "2026-11-09T19:10:00Z", "username": "MQ2208", "title": "T"}
    care.check(client(), NOW)
    assert len(care.issues["mq2208"]) == 1 and care.pending == []
    # what it had is not new: a restart onto this build does not light it again
    assert care.issues["mq2208"][0]["seen"] == 1 and care.view(NOW)["users"][0]["status"] == "ok"
    care.check(client(), NOW + HOUR)
    assert care.issues["mq2208"][0]["ended"] == "quiet" and care.issues["mq2208"][0]["last"] == "2026-11-09T19:05:00Z"


def test_the_investigation_runs_once_an_hour_and_its_headline_is_the_why():
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(client(), NOW)
    calls = []

    def investigate(username, start, end):
        calls.append((username, start, end))
        return {"answer": {"title": "Mail was slow on VD-7", "reason": "Its CPU was high.", "level": "found"}}
    assert care.run_next(investigate, NOW) == "mq2208" and care.run_next(investigate, NOW) is None
    assert calls == [("MQ2208", datetime(2026, 11, 9, 18, 50, tzinfo=timezone.utc), NOW)]  # 30 min before 14:20
    mq = care.view(NOW)["users"][0]
    assert mq["why"]["title"] == "Mail was slow on VD-7" and mq["investigating"] is None
    # a new issue 20 minutes later: said, but no second investigation within the hour
    later = NOW + timedelta(minutes=20)
    care.today.set_health([ev(later - timedelta(minutes=5), "MQ2208", "VD-7", "System Crash", "Critical")], H,
                          today=True)
    care.check(client(), later)
    assert len(care.view(later)["users"][0]["issues"]) == 2 and care.pending == []
    detail = care.detail("mq2208", later)
    assert detail["report"]["result"]["answer"]["title"] == "Mail was slow on VD-7"
    assert (detail["report"]["at_local"], detail["report"]["span_local"]) == ("Nov 09 14:30",
                                                                            "13:50 to 14:30 America/New_York")
    assert [i["kind"] for i in detail["history"]] == ["event", "slow"]  # newest first


def test_a_slow_spell_is_over_after_an_hour_with_no_major_action_and_a_later_one_is_a_new_issue():
    care = Care(today_with(3), lambda: USERS[:1])  # Major at 14:00, 14:10, 14:20
    care.check(client(), NOW)
    care.run_next(lambda u, s, e: {"answer": {"title": "Mail was slow on VD-7"}}, NOW)
    at = lambda h, m: datetime(2026, 11, 9, h + 5, m, tzinfo=timezone.utc)  # noqa: E731  (New York time)
    read(care, at(14, 45))
    read(care, at(15, 0))
    mq = care.view(at(15, 10))["users"][0]
    assert mq["status"] == "issue"  # 2 reads after 14:30
    assert mq["issues"][0]["clears"] == "Clears at the 15:15 read if no new Major action."  # the 3rd after 14:30
    assert care.detail("mq2208", at(15, 10))["issues"][0]["clears"] == mq["issues"][0]["clears"]
    care.check(client(), at(15, 15))
    assert care.view(at(15, 15))["users"][0]["status"] == "ok"  # the 3rd read: off
    care.check(client(), at(15, 25))  # 65 min: over, though 3 is still more than their usual 0 to 2
    mq = care.view(at(15, 25))["users"][0]
    assert (mq["status"], mq["issues"], mq["level"]) == ("ok", [], None)
    assert [(i["since_local"], i["over"]) for i in mq["earlier"]] == [("14:20", "none in the hour after 14:20")]
    assert mq["why"]["title"] == "Mail was slow on VD-7"  # the investigation of the day stays
    assert care.detail("mq2208", at(15, 25))["history"][0]["over"] == "none in the hour after 14:20"
    care.check(client(), at(15, 25))
    assert len(care.issues["mq2208"]) == 1  # over once
    # a late row: a Major at 15:15, read at 15:40, is less than an hour after 14:20: the same spell again
    three = care.today.midnight(date(2026, 11, 9)) + 15 * HOUR
    care.today.add_hour(three, [act(at(15, 15), "MQ2208", "VD-7", "Major")], F, at(15, 40))
    care.check(client(), at(15, 40))
    slow = care.issues["mq2208"]
    assert len(slow) == 1 and "cleared" not in slow[0] and slow[0]["last"] == "2026-11-09T20:15:00Z"
    assert care.pending == []  # not a new issue: the investigation covers its start
    assert care.view(at(15, 40))["users"][0]["status"] == "issue"  # but a row this read saw first: lit
    for m in (55, 70, 80):  # the reads after it: off at the 3rd (16:20)
        care.check(client(), at(15, 0) + timedelta(minutes=m))
    assert care.issues["mq2208"][0]["cleared"] == "2026-11-09T21:15:00Z"  # an hour after 15:15, not the read
    # 17:00, still more than their usual: a new spell, a new issue, a new investigation (over an hour on)
    five = care.today.midnight(date(2026, 11, 9)) + 17 * HOUR
    care.today.add_hour(five, [act(at(17, 0), "MQ2208", "VD-7", "Major")], F, at(17, 5))
    care.check(client(), at(17, 5))
    slow = care.issues["mq2208"]
    assert [("cleared" in i, i["since"]) for i in slow] == [(True, "2026-11-09T19:20:00Z"),
                                                             (False, "2026-11-09T22:00:00Z")]
    assert slow[1]["key"].endswith(" 2") and care.pending == ["mq2208"]
    assert care.view(at(17, 5))["users"][0]["since_local"] == "17:00"


def test_an_open_slow_issue_saved_by_the_build_before_ends_at_its_last_major_action():
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(client(), NOW)
    care.issues["mq2208"] = [{"key": "slow 2026-11-09", "kind": "slow", "level": "Major", "day": "2026-11-09",
                              "since": "2026-11-09T19:00:00Z", "text": "3 Major slow actions by 14:30"}]  # no "last"
    care.check(client(), datetime(2026, 11, 9, 20, 25, tzinfo=timezone.utc))
    assert care.issues["mq2208"][0]["last"] == "2026-11-09T19:20:00Z"
    assert care.view(NOW + HOUR)["users"][0]["earlier"][0]["over"] == "none in the hour after 14:20"


def test_a_slow_spell_is_over_when_they_are_back_within_their_usual():
    def more(request):  # one more Major at 14:40 on each day before: their usual by 14:40 is 1 to 3
        r = history(request)
        if "'mq2208'" not in request.url.params["$filter"].lower():
            return r
        tz, rows = Today(ZONE), r.json()["value"]
        rows += [act(tz.midnight(date(2026, 11, 9) - timedelta(days=b)) + 14 * HOUR + timedelta(minutes=40),
                     "MQ2208", "VD-7", "Major") for b in range(1, 8)]
        return httpx.Response(200, json={"value": rows})
    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0", username="u", password="p")
    aternity = AternityClient(cfg, transport=httpx.MockTransport(more))
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(aternity, NOW)  # 14:30: 3 against 0 to 2
    assert care.view(NOW)["users"][0]["status"] == "issue"
    for k in (1, 2, 3):  # the 3 reads after it: the light goes off
        care.check(aternity, NOW + k * timedelta(minutes=15))
    mq = care.view(NOW + 3 * timedelta(minutes=15))["users"][0]
    assert mq["status"] == "ok" and [i["over"] for i in mq["earlier"]] == ["back within their usual at 14:40"]


def test_a_major_action_is_judged_by_its_own_time_whenever_it_is_checked():
    """Stage, Nov 15 2026: "1 rated Major" and no "Earlier today". The rule was judged only at each
    check against their usual by then, which grows all day: 3 Major by 14:20 against 0 to 2 was an
    issue when checked at 14:30, and nothing at all when first checked at 22:30 (their days before
    had one more Major at 22:00: 1 to 3 by then), after a restart or a late read of their days."""
    late = NOW + 8 * HOUR  # 22:30
    on_time = Care(today_with(3), lambda: USERS[:1])
    on_time.check(client(), NOW)
    on_time.check(client(), late)
    first_late = Care(today_with(3), lambda: USERS[:1])
    first_late.check(client(), late)
    for care in (on_time, first_late):
        (i,) = care.issues["mq2208"]
        assert (i["since"], i["last"], i["ended"]) == ("2026-11-09T19:20:00Z", "2026-11-09T19:20:00Z", "quiet")
        assert i["text"].startswith("3 Major slow actions by 14:20; on the 5 days before that they used a PC: "
                                    "0 to 2 by the same time.")
    mq = first_late.view(late)["users"][0]
    assert mq["status"] == "ok" and [e["since_local"] for e in mq["earlier"]] == ["14:20"]  # seen late: not lit
    assert first_late.pending == ["mq2208"]  # and investigated


def test_an_ok_tile_says_the_moment_its_major_actions_came_nearest_to_their_usual():
    care = Care(today_with(2), lambda: USERS[:1])  # Major at 14:00 and 14:10; their usual by then 0 to 2
    care.check(client(), NOW)
    mq = care.view(NOW)["users"][0]
    assert mq["status"] == "ok" and care.issues.get("mq2208", []) == []
    assert {k: mq["nearest"][k] for k in ("had", "low", "high", "at_local")} == {"had": 2, "low": 0, "high": 2,
                                                                                  "at_local": "14:10"}
    assert care._ok_words(mq, []) == ("OK: 3 actions today, 2 Major, never above their usual (nearest: 2 by "
                                      "19:10 UTC, usual 0-2)")  # the status summary, in UTC


def test_every_user_has_a_tile_ok_with_what_was_checked():
    care = Care(today_with(2), lambda: USERS[:2])
    care.check(client(), NOW)
    v = care.view(NOW)
    assert [(u["title"], u["status"]) for u in v["users"]] == [("Payroll lead", "ok"), ("tz9013", "ok")]
    mq = v["users"][0]
    assert (mq["actions"], mq["major"], mq["usual"]["low"], mq["usual"]["high"]) == (3, 2, 0, 2)
    assert mq["earlier"] == [] and mq["pcs"] == [{"device": "VD-7", "kind": "virtual"}]


def test_a_failed_investigation_is_said():
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(client(), NOW)

    def broken(username, start, end):
        raise RuntimeError("NetIM did not answer")
    care.run_next(broken, NOW)
    assert "NetIM did not answer" in care.view(NOW)["users"][0]["why"]["error"]


def test_issues_and_the_investigation_are_kept_8_days_on_disk(tmp_path):
    care = Care(today_with(3), lambda: USERS[:1], tmp_path)
    care.check(client(), NOW)
    care.run_next(lambda u, s, e: {"answer": {"title": "T"}}, NOW)
    files = list((tmp_path / "care").glob("*.json"))
    assert len(files) == 1 and files[0].name.startswith("MQ2208-")
    saved = json.loads(files[0].read_text())
    assert saved["username"] == "MQ2208" and saved["issues"][0]["kind"] == "slow" and saved["report"]["title"] == "T"
    again = Care(today_with(0), lambda: USERS[:1], tmp_path)
    assert again.issues["mq2208"][0]["kind"] == "slow" and again.reports["mq2208"]["title"] == "T"
    old = dict(saved, issues=[dict(saved["issues"][0], day="2026-10-01")], report=None)
    files[0].write_text(json.dumps(old))
    Care(today_with(0), lambda: USERS[:1], tmp_path)
    assert not files[0].exists()  # nothing left within 8 days


def test_the_list_is_read_at_every_check_up_to_50_without_repeats():
    users = [CareUser(f"user{k}") for k in range(CARE_MAX + 3)] + [CareUser("USER1")]
    care = Care(today_with(0), lambda: users)
    care.check(None, NOW)  # no Aternity client: no history read
    v = care.view(NOW)
    assert v["checked"] == CARE_MAX and any("3 skipped" in x for x in v["notes"])
    care.load_users = lambda: []
    care.check(None, NOW)
    assert "No one is on the Care list" in care.view(NOW)["notes"][-1]


def test_care_list_in_config_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("care_list:\n  - username: xk4471\n    label: Front desk\n  - tz9013\n  - username: ''\n")
    got = load_settings(path).care_list
    assert [(u.username, u.label) for u in got] == [("xk4471", "Front desk"), ("tz9013", "")]


def test_how_often_aternity_is_read_is_a_setting_in_config_yaml(tmp_path):
    """The user, 2026-11-12: try 5 minutes on stage. One setting for the Today reader, the
    names and the Care list (they share its reads), kept between 5 and 60 minutes."""
    from uxcorrelate import explain, today as today_mod
    path = tmp_path / "config.yaml"
    for value, got in (("5", 5), ("2", 5), ("90", 60), ("''", 15)):
        path.write_text(f"refresh_minutes: {value}\n")
        assert load_settings(path).refresh_minutes == got
    path.write_text("timezone: ''\n")
    assert load_settings(path).refresh_minutes == today_mod.REFRESH_MIN == 15
    path.write_text("refresh_minutes: 5\ncare_list:\n  - mq2208\n")
    try:
        app = web.create_app(str(path))
        assert app.state.today.refresh_min == 5
        assert app.state.care.today.refresh_min == 5  # the Care list checks after each of its reads
        assert "every 5 minutes" in explain.GLOSSARY["today"]["Today"]
        assert "(5 minutes)" in explain.GLOSSARY["care"]["Care list"]
    finally:
        explain.set_refresh(today_mod.REFRESH_MIN)  # the meanings are the module's: as before for other tests


def test_the_endpoints(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("care_list:\n  - username: mq2208\n    label: Payroll lead\n")
    app = web.create_app(str(path))
    http = TestClient(app)  # not started: no background read in tests
    assert http.get("/api/care").json()["ready"] is False
    care = app.state.care
    care.today.__dict__.update(today_with(3).__dict__)
    care.check(client(), NOW)
    assert http.get("/api/care").json()["users"][0]["title"] == "Payroll lead"
    assert http.get("/api/care/user", params={"username": "MQ2208"}).json()["label"] == "Payroll lead"
    assert http.get("/api/care/user", params={"username": "nobody"}).status_code == 404


def test_the_list_file_is_made_once_from_config_yaml_then_it_is_the_list(tmp_path, caplog):
    cfg = [USERS[0]]
    care = Care(today_with(0), lambda: cfg, tmp_path)
    with caplog.at_level("INFO", logger="uxcorrelate"):
        care.check(None, NOW)
    file = tmp_path / "care_list.json"
    assert json.loads(file.read_text()) == [{"username": "mq2208", "label": "Payroll lead"}]
    assert "made with the 1 users of care_list in config.yaml" in caplog.text
    cfg.append(USERS[1])  # config.yaml is not read again
    care.check(None, NOW)
    assert [u.username for u in care.users] == ["mq2208"]
    file.write_text('[{"username": " tz9013 ", "label": "Desk"}, "ghost1", {"label": "no name"}, 7]')  # edited by hand
    care.check(None, NOW)
    assert [(u.username, u.label) for u in care.users] == [("tz9013", "Desk"), ("ghost1", "")]
    file.write_text("{not json")  # broken: said, the last list is kept, and the page does not write over it
    care.check(None, NOW)
    assert len(care.users) == 2 and "could not be read" in care.view(NOW)["notes"][0]
    try:
        care.add("new1")
        raise AssertionError("added over a broken file")
    except OSError as exc:
        assert "could not be read first" in str(exc) and file.read_text() == "{not json"


def test_a_user_is_added_and_removed_on_the_page(tmp_path, caplog):
    from uxcorrelate.names import NameList
    names = NameList()
    names.seen["username"]["MQ2208"] = NOW
    care = Care(today_with(3), lambda: [], tmp_path, names)
    care.check(client(), NOW)
    assert care.view(NOW)["notes"] == ['No one is on the Care list: use "Add a user" above.']
    with caplog.at_level("INFO", logger="uxcorrelate"):
        got = care.add("  mq2208 ", " Payroll   lead ", "198.51.100.7")
    assert (got.username, got.label) == ("MQ2208", "Payroll lead")  # as Aternity writes it
    assert "Care list: Payroll lead (MQ2208) added on the page from 198.51.100.7; 1 users now" in caplog.text
    assert json.loads((tmp_path / "care_list.json").read_text()) == [{"username": "MQ2208", "label": "Payroll lead"}]
    assert care.view(NOW)["users"][0]["title"] == "Payroll lead"  # the tile is there before the next check
    care.check(client(), NOW)  # the page asks for one at once: history read, the issue found
    care.run_next(lambda u, s, e: {"answer": {"title": "T"}}, NOW)
    assert care.view(NOW)["count"] == 1 and len(list((tmp_path / "care").glob("*.json"))) == 1

    def refused(*a):
        try:
            care.add(*a)
        except ValueError as exc:
            return str(exc)
    assert refused("MQ2208") == "Payroll lead (MQ2208) is already on the Care list."
    assert refused("  ") == "Type a username, as Aternity writes it."
    assert refused("x" * 101).startswith("Too long")
    assert Care(today_with(0), lambda: [], tmp_path).listed()[0].username == "MQ2208"  # kept over a restart

    assert care.remove("nobody") is None
    care.pending.append("mq2208")
    assert care.remove("mq2208", "198.51.100.7").label == "Payroll lead"
    assert care.users == [] and json.loads((tmp_path / "care_list.json").read_text()) == []
    assert not care.issues and not care.reports and not care.pending
    assert not list((tmp_path / "care").glob("*.json"))  # their saved issues and investigation are deleted
    assert "removed on the page from 198.51.100.7, with their saved issues; 0 users now" in caplog.text

    for k in range(CARE_MAX):
        care.add(f"user{k}")
    assert refused("one-more") == f"The Care list is full ({CARE_MAX} users): remove one first."


def test_an_investigation_of_a_user_removed_while_it_ran_is_not_kept(tmp_path):
    care = Care(today_with(3), lambda: USERS[:1], tmp_path)
    care.check(client(), NOW)

    def investigate(u, s, e):
        care.remove("mq2208")
        return {"answer": {"title": "T"}}
    care.run_next(investigate, NOW)
    assert not care.reports and not list((tmp_path / "care").glob("*.json"))


def test_the_add_and_remove_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("care_list:\n  - username: mq2208\n    label: Payroll lead\n")
    app = web.create_app(str(path))
    http = TestClient(app)
    app.state.care.today.__dict__.update(today_with(3).__dict__)
    # every change needs the PIN: today's date, YYYYMMDD, in the page's time zone (the user's choice)
    pin = app.state.care.pin(datetime.now(timezone.utc))
    # in the page's time zone (the test's Today is New York's: after midnight UTC it is still the day before)
    assert len(pin) == 8 and pin == f"{app.state.care.today.local(datetime.now(timezone.utc)):%Y%m%d}"
    for wrong in ({}, {"pin": "20200101"}, {"pin": ""}):
        no = http.post("/api/care/users", json={"username": "tz9013", "label": "Desk", **wrong})
        assert no.status_code == 403 and no.json()["detail"] == "Wrong PIN: the Care list was not changed."
    assert http.delete("/api/care/users", params={"username": "mq2208"}).status_code == 403
    assert http.put("/api/care/users", json={"username": "mq2208", "label": "X"}).status_code == 403
    listed = tmp_path / "care_list.json"
    assert not listed.exists() or "tz9013" not in listed.read_text()  # nothing changed
    assert http.post("/api/care/users", json={"username": "tz9013", "label": "Desk", "pin": pin}).json() == {
        "username": "tz9013", "label": "Desk"}
    # a new label: the list file has it, the user's saved issues stay
    assert http.put("/api/care/users", json={"username": "TZ9013", "label": "Front desk", "pin": pin}).json() == {
        "username": "tz9013", "label": "Front desk"}
    assert http.put("/api/care/users", json={"username": "nobody", "label": "X", "pin": pin}).status_code == 404
    assert http.put("/api/care/users", json={"username": "tz9013", "label": "Desk", "pin": pin}).status_code == 200
    titles = [u["title"] for u in http.get("/api/care").json()["users"]]
    assert sorted(titles) == ["Desk", "Payroll lead"] and http.get("/api/care").json()["max"] == CARE_MAX
    again = http.post("/api/care/users", json={"username": "TZ9013", "pin": pin})
    assert again.status_code == 400 and again.json()["detail"] == "Desk (tz9013) is already on the Care list."
    assert http.delete("/api/care/users", params={"username": "mq2208", "pin": pin}).json()["label"] == "Payroll lead"
    assert http.delete("/api/care/users", params={"username": "mq2208", "pin": pin}).status_code == 404
    assert [u["username"] for u in json.loads((tmp_path / "care_list.json").read_text())] == ["tz9013"]
    (tmp_path / "care_list.json").write_text("{broken")
    bad = http.post("/api/care/users", json={"username": "new1", "pin": pin})
    assert bad.status_code == 500 and "could not be changed" in bad.json()["detail"]


def test_the_tiles_keep_the_lists_own_order_inside_each_group(tmp_path, caplog):
    """Arrange tiles (the user's choice, 2026-11-14): lit first, then names not found, then the rest;
    inside each, the list's own order, which the page sets (reorder)."""
    care = Care(today_with(3), lambda: USERS + [CareUser("ghost2", "Night desk")], tmp_path)
    care.check(client(), NOW)
    order = lambda: [(u["title"], u["status"], u["place"]) for u in care.view(NOW)["users"]]  # noqa: E731
    assert order() == [("Payroll lead", "issue", 0), ("ghost1", "unknown", 2), ("Night desk", "unknown", 3),
                       ("tz9013", "ok", 1)]
    with caplog.at_level("INFO", logger="uxcorrelate"):
        assert care.reorder(["ghost2", " tz9013 ", "GHOST1", "MQ2208"], "198.51.100.7") is True
    assert [u["username"] for u in json.loads((tmp_path / "care_list.json").read_text())] == [
        "ghost2", "tz9013", "ghost1", "mq2208"]
    assert order() == [("Payroll lead", "issue", 3), ("Night desk", "unknown", 0), ("ghost1", "unknown", 2),
                       ("tz9013", "ok", 1)]  # the lit tile still first
    assert ("Care list: the order of the tiles was changed on the page from 198.51.100.7; now: 1. Night desk "
            "(ghost2), 2. tz9013, 3. ghost1, 4. Payroll lead (mq2208)") in caplog.text
    assert care.reorder(["ghost2", "tz9013", "ghost1", "mq2208"]) is False  # the same order: nothing written
    for wrong in (["ghost2", "tz9013", "ghost1"], ["ghost2", "tz9013", "ghost1", "mq2208", "new1"],
                  ["ghost2", "ghost2", "ghost1", "mq2208"]):  # the list was changed meanwhile
        with pytest.raises(ValueError, match="changed while it was arranged"):
            care.reorder(wrong)
    assert [u.username for u in Care(today_with(0), lambda: [], tmp_path).listed()] == [
        "ghost2", "tz9013", "ghost1", "mq2208"]  # kept over a restart
    # past CARE_MAX: not on the page, kept after the users it shows
    many = [CareUser(f"user{k}") for k in range(CARE_MAX + 2)]
    care = Care(today_with(0), lambda: many)
    care.check(None, NOW)
    shown = [f"user{k}" for k in range(CARE_MAX)]
    assert care.reorder(shown[::-1]) is True
    assert [u.username for u in care.listed()] == shown[::-1] + [f"user{CARE_MAX}", f"user{CARE_MAX + 1}"]


def test_the_order_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("care_list:\n  - username: mq2208\n    label: Payroll lead\n  - tz9013\n  - ghost1\n")
    app = web.create_app(str(path))
    http = TestClient(app)
    care = app.state.care
    care.today.__dict__.update(today_with(2).__dict__)  # 2 Major, as on a day before: OK
    care.check(client(), NOW)
    care.today.read_ok = NOW
    listed = lambda: [u["username"] for u in json.loads((tmp_path / "care_list.json").read_text())]  # noqa: E731
    new = ["tz9013", "ghost1", "MQ2208"]
    no = http.put("/api/care/order", json={"usernames": new, "pin": "20200101"})
    assert no.status_code == 403 and no.json()["detail"] == "Wrong PIN: the Care list was not changed."
    assert listed() == ["mq2208", "tz9013", "ghost1"]
    pin = care.pin(datetime.now(timezone.utc))
    assert http.put("/api/care/order", json={"usernames": new, "pin": pin}).json() == {"changed": True}
    assert listed() == ["tz9013", "ghost1", "mq2208"]
    assert [u["username"] for u in care.view(NOW)["users"]] == ["ghost1", "tz9013", "MQ2208"]  # not found first
    assert [u["username"] for u in care.status(NOW)["users"]] == ["ghost1", "tz9013", "MQ2208"]  # the status too
    # lit: a failing disk first, then Critical, then Major, whatever the list's order
    care.today.set_health([ev(AT(14, 0), "MQ2208", "VD-7", "Application Crash", "Major", "Application", "Mail"),
                           ev(AT(14, 1), "ghost1", "VD-9", "System Crash", "Critical"),
                           dict(ev(AT(14, 2), "tz9013", "PC-3", "Faulty HD S.M.A.R.T. Status", "Major", "Hardware"),
                                HEALTH_EVENT_COMPONENT="WDC WD10EZEX (Disk)")], H2, today=True)
    care.check(client(), NOW)
    assert [(u["username"], u["level"]) for u in care.view(NOW)["users"]] == [
        ("tz9013", "Major"), ("ghost1", "Critical"), ("MQ2208", "Major")]
    assert http.put("/api/care/order", json={"usernames": new, "pin": pin}).json() == {"changed": False}
    gone = http.put("/api/care/order", json={"usernames": new[:2], "pin": pin})
    assert gone.status_code == 409 and "changed while it was arranged" in gone.json()["detail"]
    (tmp_path / "care_list.json").write_text("{broken")
    bad = http.put("/api/care/order", json={"usernames": new, "pin": pin})
    assert bad.status_code == 500 and "could not be changed" in bad.json()["detail"]


# -- the alert (the user's rule, 2026-11-14) and /api/MSS/care/status ----------------------------------
AT = lambda h, m: datetime(2026, 11, 9, h + 5, m, tzinfo=timezone.utc)  # noqa: E731  (New York time, Monday)
H2 = dict(H, component="HEALTH_EVENT_COMPONENT", count="HEALTH_EVENT_VOLUME")


def event(h, m, name="Application Hang", sev="Major", cat="Application", app="Mail", pc="VD-7", part=None, n=None,
          user="MQ2208"):
    row = ev(AT(h, m), user, pc, name, sev, cat, app)
    row.update(HEALTH_EVENT_COMPONENT=part, HEALTH_EVENT_VOLUME=n)
    return row


def read(care: Care, at: datetime) -> None:
    """One read of the Today reader at `at`: the next one is refresh_min on (Today.next_read)."""
    care.today.next_read = at + timedelta(minutes=care.today.refresh_min)
    care.check(client(), at)


def care_at(rows, refresh=5, users=USERS[:1], store=None) -> Care:
    t = today_with(0)
    t.refresh_min = refresh
    t.set_health(rows, H2, today=True)
    return Care(t, lambda: users, store)


def test_an_alert_is_on_for_3_reads_after_a_read_that_brought_something_new_then_it_is_earlier_today():
    care = care_at([])
    care.check(client(), AT(14, 0))
    care.today.set_health([event(14, 2)], H2, today=True)
    read(care, AT(14, 5))  # the read that saw the 14:02 hang first
    mq = care.view(AT(14, 5))["users"][0]
    assert (mq["status"], mq["level"], mq["since_local"]) == ("issue", "Major", "14:02")
    assert mq["issues"][0]["clears"] == "Clears at the 14:20 read if no new Application Hang."  # 3 reads of 5 min
    for m in (10, 15):  # nothing new: still on
        care.check(client(), AT(14, m))
        assert care.view(AT(14, m))["users"][0]["status"] == "issue"
    care.check(client(), AT(14, 20))
    mq = care.view(AT(14, 20))["users"][0]
    assert (mq["status"], mq["issues"], [i["text"] for i in mq["earlier"]]) == (
        "ok", [], ["Application Hang (Application) in Mail, first at 14:02 on VD-7."])
    assert mq["earlier"][0]["over"] is None  # the hour after it is not over: one issue, one count
    # the same hang again within the hour: the same issue, its alert on again
    care.today.set_health([event(14, 2), event(14, 23)], H2, today=True)
    care.check(client(), AT(14, 25))
    mq = care.view(AT(14, 25))["users"][0]
    assert len(care.issues["mq2208"]) == 1 and mq["status"] == "issue"
    assert mq["issues"][0]["text"].startswith("Application Hang (Application) 2 times in Mail")
    # another event while it is on: the tile stays on, the most serious first
    care.today.set_health([event(14, 2), event(14, 23), event(14, 28, "System Crash", "Critical", "System", None)],
                          H2, today=True)
    care.check(client(), AT(14, 30))
    mq = care.view(AT(14, 30))["users"][0]
    assert mq["level"] == "Critical" and [i["what"] for i in mq["issues"]] == [
        "System/System Crash", "Application/Application Hang"]


def test_a_row_aternity_sends_late_lights_the_tile_and_its_day_is_investigated_again():
    care = care_at([event(9, 0, "Application Crash")], refresh=15)
    care.check(client(), AT(9, 5))
    calls = []
    run = lambda u, s, e: calls.append((s, e)) or {"answer": {"title": "Mail crashed"}}  # noqa: E731
    care.run_next(run, AT(9, 5))
    assert calls == [(AT(8, 30), AT(9, 5))]
    # 09:30, another kind: lit, but no second investigation within the hour ...
    care.today.set_health([event(9, 0, "Application Crash"), event(9, 30, "System Crash", "Critical", "System", None)],
                          H2, today=True)
    care.check(client(), AT(9, 35))
    assert care.view(AT(9, 35))["users"][0]["level"] == "Critical" and care.pending == []
    care.check(client(), AT(10, 5))  # ... until the hour is up (before, it was never investigated)
    assert care.pending == ["mq2208"]
    care.run_next(run, AT(10, 5))
    # 11:00: a 06:04 crash Aternity sent late, over when first seen: lit, and the day is investigated again
    late = [event(9, 0, "Application Crash"), event(9, 30, "System Crash", "Critical", "System", None),
            event(6, 4, "Application Crash", app="ERP", pc="VD-7")]
    care.today.set_health(late, H2, today=True)
    care.check(client(), AT(11, 10))
    mq = care.view(AT(11, 10))["users"][0]
    first = next(i for i in care.issues["mq2208"] if i["since"] == "2026-11-09T11:04:00Z")
    assert first["ended"] == "quiet" and mq["status"] == "issue" and care.pending == ["mq2208"]
    care.run_next(run, AT(11, 10))
    assert calls[-1] == (AT(5, 34), AT(11, 10))  # 30 min before 06:04
    care.check(client(), AT(11, 25))
    assert care.pending == []  # covered now


def test_a_failing_disk_stays_on_all_day():
    disk = event(8, 0, "Faulty HD S.M.A.R.T. Status", "Major", "Hardware", None, part="WDC WD10EZEX (Disk)")
    care = care_at([disk, event(15, 0, "System Crash", "Critical", "System", None)], refresh=5)
    care.check(client(), AT(15, 2))
    for at in (AT(15, 7), AT(15, 12), AT(15, 17), AT(17, 55)):  # the System Crash goes off at 15:17
        care.check(client(), at)
        mq = care.view(at)["users"][0]
        assert mq["status"] == "issue" and mq["issues"][0]["what"] == "Hardware/Faulty HD S.M.A.R.T. Status"
    assert mq["issues"][0]["clears"] == "Stays on today: a failing disk is the PC's state."
    assert [i["what"] for i in mq["earlier"]] == ["System/System Crash"]  # its 3 reads are over
    tomorrow = AT(8, 0) + timedelta(days=1)
    assert care.view(tomorrow)["users"][0]["status"] != "issue"


def test_a_full_drive_is_an_event_not_a_failing_disk():
    """Aternity files Low Disk Space and Corrupted FS under "(Drive)" too, category System (verify
    --overview, stage, Nov 16 2026; the label alone kept them lit all day): lit for its reads like
    any event. Only a Hardware event under a disk or drive is a failing disk (models.failing_disk)."""
    assert failing_disk("\\Device\\Harddisk0\\DR0 (Drive)", "Hardware")
    assert failing_disk("WDC WD10EZEX (Disk)", " hardware ")
    assert not failing_disk("C: (Drive)", "System") and not failing_disk("C: (Drive)", None)
    assert not failing_disk("OUTLOOK.EXE (Process)", "Hardware")
    care = care_at([event(15, 0, "Low Disk Space", "Major", "System", None, part="C: (Drive)"),
                    event(15, 1, "Corrupted FS", "Major", "System", None, part="C: (Drive)")], refresh=5)
    care.check(client(), AT(15, 2))
    assert care.view(AT(15, 2))["users"][0]["status"] == "issue"
    for at in (AT(15, 7), AT(15, 12), AT(15, 17)):  # off at the 3rd read after it, as any event
        care.check(client(), at)
    mq = care.view(AT(15, 17))["users"][0]
    assert mq["status"] == "ok" and {i["what"] for i in mq["earlier"]} == {"System/Low Disk Space",
                                                                          "System/Corrupted FS"}


def test_a_failing_disk_mark_saved_by_the_label_alone_goes_at_the_next_check(tmp_path):
    """The rule before Nov 16 2026 saved Low Disk Space under "(Drive)" as a failing disk, on all day:
    after the update the mark is worked out again from today's rows, so it goes off as any event."""
    full = event(15, 0, "Low Disk Space", "Major", "System", None, part="C: (Drive)")
    care = care_at([full], store=tmp_path)
    care.check(client(), AT(15, 2))
    for i in care.issues["mq2208"]:
        i["disk"] = True  # as the older rule saved it
    care.save("mq2208")
    again = care_at([full], store=tmp_path)
    for at in (AT(15, 7), AT(15, 12), AT(15, 17), AT(15, 22)):
        again.check(client(), at)
    mq = again.view(AT(15, 22))["users"][0]
    assert mq["status"] == "ok" and not any(i.get("disk") for i in again.issues["mq2208"])


def test_a_restart_does_not_light_again_what_was_seen(tmp_path):
    care = care_at([event(14, 2)], store=tmp_path)
    care.check(client(), AT(14, 0))
    care.check(client(), AT(14, 5))
    assert care.view(AT(14, 5))["users"][0]["status"] == "issue"
    # restarted at 14:40 (Today reads it all again): seen before, so not lit; one that came while it was
    # down, at 14:10, is past its alert's time: not lit either; one within it is
    again = care_at([event(14, 2), event(14, 10, "System Crash", "Critical", "System", None)], store=tmp_path)
    again.check(client(), AT(14, 40))
    mq = again.view(AT(14, 40))["users"][0]
    assert mq["status"] == "ok" and len(mq["earlier"]) == 2
    third = care_at([event(14, 2), event(14, 10, "System Crash", "Critical", "System", None),
                     event(14, 50, "System Crash", "Critical", "System", None)], store=tmp_path)
    third.check(client(), AT(14, 55))
    assert third.view(AT(14, 55))["users"][0]["status"] == "issue"


def test_a_user_just_added_is_lit_only_by_what_is_recent_and_their_day_is_investigated(tmp_path):
    care = care_at([event(9, 0, "Application Crash"), event(14, 28)], refresh=15, store=tmp_path)
    care.check(client(), AT(14, 30))
    mq = care.view(AT(14, 30))["users"][0]
    assert [i["what"] for i in mq["issues"]] == ["Application/Application Hang"]  # 2 min ago
    assert [i["what"] for i in mq["earlier"]] == ["Application/Application Crash"]  # 09:00
    assert care.pending == ["mq2208"]


def test_times_in_one_event_row_are_added_up_and_a_row_that_grows_lights_again():
    care = care_at([event(14, 2, n=3)])
    care.check(client(), AT(14, 0))
    care.check(client(), AT(14, 5))
    assert care.issues["mq2208"][0]["text"].startswith("Application Hang (Application) 3 times in Mail")
    for m in (10, 15, 20):
        care.check(client(), AT(14, m))
    assert care.view(AT(14, 20))["users"][0]["status"] == "ok"
    care.today.set_health([event(14, 2, n=4)], H2, today=True)  # the same row, one more time
    care.check(client(), AT(14, 25))
    assert care.view(AT(14, 25))["users"][0]["status"] == "issue" and care.issues["mq2208"][0]["seen"] == 4


def test_the_status_for_another_platform(tmp_path):
    """GET /api/MSS/care/status (the user's request, 2026-11-14): every tile, all times UTC, a short
    summary in our own words in plain ASCII, no PC field."""
    path = tmp_path / "config.yaml"
    path.write_text("care_list:\n  - username: mq2208\n    label: Payroll lead\n  - tz9013\n  - ghost1\n")
    app = web.create_app(str(path))
    http = TestClient(app)
    got = http.get("/api/MSS/care/status").json()
    assert (got["ready"], got["users"], got["refresh_minutes"]) == (False, [], 15) and got["note"]
    care = app.state.care
    care.today.__dict__.update(today_with(3).__dict__)  # Major at 14:00, 14:10, 14:20 (19:00 UTC on)
    care.today.set_health([
        ev(AT(14, 5), "MQ2208", "VD-7", "Application Crash", "Major", "Application", "Mail"),
        ev(AT(14, 25), "MQ2208", "VD-7", "Application Crash", "Major", "Application", "Mail"),
        dict(ev(AT(8, 0), "tz9013", "PC-3", "Faulty HD S.M.A.R.T. Status", "Major", "Hardware"),
             HEALTH_EVENT_COMPONENT="WDC WD10EZEX (Disk)")], H2, today=True)
    care.check(client(), NOW)
    assert care.status(NOW)["ready"] is False  # no read of Aternity has worked yet
    care.today.read_ok = care.today.as_of = NOW
    got = care.status(NOW)
    assert (got["ready"], got["as_of"], got["note"]) == (True, "2026-11-09T19:30:00Z", None)
    assert http.get("/api/MSS/care/status").json()["ready"] is True  # the route (it asks at the real time)
    rows = {u["username"]: u for u in got["users"]}
    assert [u["username"] for u in got["users"]] == ["tz9013", "MQ2208", "ghost1"]  # as the tab: a failing disk first
    assert rows["MQ2208"] == {
        "username": "MQ2208", "label": "Payroll lead", "status": "alert", "level": "Major",
        "since": "2026-11-09T19:05:00Z",
        "summary": "Application Crash - Mail, 2 times 19:05-19:25 UTC, on VD-7; +1 more: Major slow actions"}
    assert rows["tz9013"] == {
        "username": "tz9013", "label": "", "status": "alert", "level": "Major", "since": "2026-11-09T13:00:00Z",
        "summary": "Faulty HD S.M.A.R.T. Status on PC-3 since 13:00 UTC (stays on today)"}
    assert rows["ghost1"] == {"username": "ghost1", "label": "", "status": "unknown", "level": None, "since": None,
                              "summary": "Not seen in Aternity in 8 days: check the username"}
    # two hours on: the crashes and slow actions are earlier today (off at the 3rd read after 19:30 UTC),
    # the disk is still on
    for k in (1, 2, 3):
        care.check(client(), NOW + k * timedelta(minutes=15))
    care.today.read_ok = care.today.as_of = NOW + 2 * HOUR
    later = care.status(NOW + 2 * HOUR)
    mq = next(u for u in later["users"] if u["username"] == "MQ2208")
    assert (mq["status"], mq["level"], mq["since"]) == ("ok", None, "2026-11-09T20:15:00Z")  # the 3rd read after 20:30
    assert mq["summary"] == "OK since 20:15 UTC. Earlier today: 2 issues, the last Application Crash - Mail at 19:25 UTC"
    # a read that failed in part: as_of stays at the last good one, and the note says so
    care.today.read_ok, care.today.as_of = NOW, NOW + HOUR
    assert care.status(NOW + HOUR)["as_of"] == "2026-11-09T19:30:00Z" and "20:30 UTC" in care.status(NOW + HOUR)["note"]
    assert all(len(u["summary"]) <= 120 and u["summary"].isascii() and "pcs" not in u for u in got["users"])


def test_status_summaries_ok_one_earlier_and_plain_ascii():
    care = Care(today_with(0), lambda: USERS[:1])
    one = [{"kind": "event", "what": "Application/Application Crash", "since": "2026-11-09T18:50:00Z",
            "last": "2026-11-09T19:20:00Z", "apps": ["Outlook"], "key": "k"}]
    assert care._ok_words({"earlier": one}, [AT(14, 35)]) == \
        "OK since 19:35 UTC. Earlier today: Application Crash - Outlook 18:50-19:20 UTC"
    assert care._ok_words({"earlier": [], "actions": 214, "major": 1, "usual": {"enough": True, "low": 0, "high": 3}},
                          []) == "OK: 214 actions today, 1 Major (usual by now 0-3)"
    assert care._ok_words({"earlier": [], "actions": 0, "major": 0, "usual": None}, []) == "OK: no activity today yet"
    from uxcorrelate.care import _ascii, _fit
    assert _ascii("Microsoft® Outlook – Mail…") == "Microsoft Outlook - Mail..."
    long = _fit("System Crash at 14:02 UTC on VD-41", ["Application Crash"] * 3 + ["x" * 120])
    assert long == "System Crash at 14:02 UTC on VD-41; +4 more"


# -- gaps found on stage, Nov 14 2026: the light counted 15 minutes, not 3 reads -------------------------
def test_the_light_counts_reads_not_minutes_and_says_when_aternity_sent_it(caplog):
    """A crash Aternity first sent 49 minutes later, while reads took 2.5 minutes, so a cycle was 7.5
    (stage, Nov 14 2026: 17:21, sent at 18:10; here 6 hours earlier). The light goes off at the 3rd read
    after the one that brought it, whatever their times; a check after a change on the page is not a
    read; the tile says when Aternity sent it and which read clears it."""
    caplog.set_level("INFO", "uxcorrelate")
    crash = event(11, 21, "Application Crash", app="Microsoft OneDrive")
    care = care_at([], refresh=5)
    read(care, AT(12, 3))
    care.today.set_health([crash], H2, today=True)
    sent = AT(12, 10) + timedelta(seconds=31)
    read(care, sent)
    mq = care.view(sent)["users"][0]
    assert mq["status"] == "issue" and mq["issues"][0]["clears"] == (
        "Aternity sent it at 12:10. Clears at the 12:25 read if no new Application Crash.")
    assert "alert on. Aternity sent it at 12:10. Clears at the 12:25 read" in caplog.text
    care.check(client(), sent + timedelta(minutes=2), read=False)  # a check after a change on the page
    assert care.issues["mq2208"][0]["left"] == 3 and care.as_of == sent  # not a read: "as of" stays too
    for k, lit in ((1, True), (2, True), (3, False)):  # every 7.5 minutes: at 14:54 after it, still lit
        at = sent + k * timedelta(minutes=7, seconds=27)
        read(care, at)
        assert (care.view(at)["users"][0]["status"] == "issue") is lit, k
    assert care.issues["mq2208"][0]["off"] == "2026-11-09T17:32:52Z"
    assert "no longer lit: nothing new of it in 3 reads since Aternity sent it at 12:10." in caplog.text
    care.today.read_ok = care.today.as_of = at
    s = care.status(at)["users"][0]
    assert (s["status"], s["since"]) == ("ok", "2026-11-09T17:32:52Z")  # OK since the read that put it out
    # a row sent within a read of when it happened: nothing said of when it was sent
    care.today.set_health([crash, event(12, 30, "Application Crash", app="Microsoft OneDrive")], H2, today=True)
    read(care, AT(12, 35))
    assert care.view(AT(12, 35))["users"][0]["issues"][0]["clears"] == (
        "Clears at the 12:50 read if no new Application Crash.")


def test_a_light_goes_on_counting_over_a_restart_unless_its_time_passed(tmp_path):
    care = care_at([], store=tmp_path)
    read(care, AT(14, 0))
    care.today.set_health([event(14, 2)], H2, today=True)
    read(care, AT(14, 5))  # lit: 3 reads left
    read(care, AT(14, 10))  # 2 left
    again = care_at([event(14, 2)], store=tmp_path)  # restarted at once
    read(again, AT(14, 12))
    assert again.view(AT(14, 12))["users"][0]["status"] == "issue" and again.issues["mq2208"][0]["left"] == 1
    read(again, AT(14, 17))
    assert again.view(AT(14, 17))["users"][0]["status"] == "ok"
    # restarted long after: the reads missed are not known, its time is up: off, not lit for 3 more reads
    care = care_at([], store=tmp_path / "b")
    read(care, AT(15, 0))
    care.today.set_health([event(15, 2)], H2, today=True)
    read(care, AT(15, 5))
    late = care_at([event(15, 2)], store=tmp_path / "b")
    read(late, AT(16, 0))
    lit = late.issues["mq2208"][0]
    assert late.view(AT(16, 0))["users"][0]["status"] == "ok" and lit["off"] == "2026-11-09T20:20:00Z"


def test_an_issue_saved_by_the_build_before_counts_the_reads_its_time_leaves():
    care = care_at([event(14, 2)])
    read(care, AT(14, 5))
    i = care.issues["mq2208"][0]
    del i["left"]  # as saved before the light counted reads
    assert care.alert(i, AT(14, 12))  # 7 minutes: on by its time
    read(care, AT(14, 12))
    assert i["left"] == 1  # 1 read of 5 minutes gone by its time, and this one
    read(care, AT(14, 17))
    assert not care.alert(i, AT(14, 17)) and i["off"] == "2026-11-09T19:17:00Z"


def test_saved_files_one_unreadable_or_two_for_one_user(tmp_path):
    care = care_at([event(14, 2)], store=tmp_path)
    read(care, AT(14, 5))
    mine = next((tmp_path / "care").glob("*.json"))
    # the same user saved before under another spelling (the file name has it): the newest wins and
    # the other is deleted at the next save
    older = mine.with_name("mq2208-" + mine.name.rsplit("-", 1)[1])
    older.write_text(json.dumps({"username": "mq2208", "issues": [dict(care.issues["mq2208"][0], text="OLD")],
                                 "report": None}))
    import os
    os.utime(older, (1, 1))
    (tmp_path / "care" / "broken-00000000.json").write_text("{not json")
    again = Care(today_with(0), lambda: USERS[:1], tmp_path)
    assert again.issues["mq2208"][0]["text"] != "OLD"  # the newest file
    assert "1 saved Care list files" in " ".join(again.notes.values()) and "broken-00000000.json" in " ".join(
        again.notes.values())
    again.save("mq2208")
    assert not older.exists() and mine.exists()


def test_the_status_says_when_no_read_came_for_3_reads_time():
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(client(), NOW)
    care.today.read_ok = care.today.as_of = NOW
    assert care.status(NOW + timedelta(minutes=44))["note"] is None  # 15-minute reads: under 3 reads' time
    note = care.status(NOW + timedelta(minutes=46))["note"]
    assert note == "No read of Aternity since 19:30 UTC (reads are every 15 min): the statuses are out of date."


def test_the_investigation_worker_goes_on_after_an_error_it_did_not_expect(caplog):
    care = Care(today_with(0), lambda: USERS[:1])
    calls = []

    class Stop(BaseException):  # not an Exception: the worker's guard lets it through
        pass

    def run_next(investigate, now):
        calls.append(now)
        if len(calls) == 1:
            raise KeyError("since")
        raise Stop

    care.run_next = run_next
    with pytest.raises(Stop):
        care.work(None, lambda s: None, clock=lambda: NOW)
    assert len(calls) == 2 and "error the code did not expect" in caplog.text


def test_an_ok_tile_says_when_major_actions_were_not_compared_and_what_the_bars_were():
    """Stage, Nov 14 2026: "No issue: 164 actions today, 3 rated Major" with no word that they were not
    compared; and a tile with bars said "0 rated Major" with no word of what the bars were."""
    sat = datetime(2026, 11, 14, 19, 30, tzinfo=timezone.utc)
    care = Care(today_with(3), lambda: USERS[:1])  # Major at 14:00, 14:10, 14:20 on Monday Nov 9
    care.check(client(), NOW)
    care.issues["mq2208"][0]["apps"] = ["Mail"]
    assert care.usual("mq2208", sat)["enough"]  # a Saturday: the latest 5 days, any day of the week
    h = care.history["mq2208"]["days"]
    care.history["mq2208"]["days"] = {d: h[d] for d in sorted(h)[-2:]}  # only 2 days with a PC in use
    u = care.usual("mq2208", sat)
    assert u == {"enough": False, "days": 2, "kind": "days", "need": 3, "read": 8}
    tile = care.tile(USERS[0], sat)
    assert tile["before"] == "Mon Nov 09: Major slow actions"
    words = care._ok_words(dict(tile, actions=164, major=3), [])
    assert words == "OK: 164 actions today, 3 Major (not compared: no usual yet)"


def test_a_failing_disk_with_several_events_is_one_line_on_the_tile():
    rows = [event(8, 23, "Faulty HD S.M.A.R.T. Status", "Major", "Hardware", None, part="WDC (Disk)"),
            event(9, 6, "HD Failure", "Critical", "Hardware", None, part="DR0 (Drive)"),
            event(11, 46, "HD Failure", "Critical", "Hardware", None, part="DR0 (Drive)"),
            event(12, 0, "System Crash", "Critical", "System", None)]
    care = care_at(rows)
    read(care, AT(12, 2))
    mq = care.view(AT(12, 2))["users"][0]
    assert [i["text"] for i in mq["issues"]] == [
        "Faulty HD S.M.A.R.T. Status and HD Failure on VD-7: 3 events, first at 08:23, the last at 11:46.",
        "System Crash (System), first at 12:00 on VD-7."]
    assert mq["level"] == "Critical" and mq["issues"][0]["level"] == "Critical"
    assert mq["issues"][0]["clears"] == "Stays on today: a failing disk is the PC's state."


def test_a_slow_spell_first_seen_when_already_over_is_kept_as_earlier_today():
    """Dev, Nov 15 2026: 5 Major against a usual of 0 to 3, the last over an hour before the user's
    first check: the spell was dropped and the tile said "No issue: 5 rated Major (usual by now: 0 to 3)"."""
    care = Care(today_with(3), lambda: USERS[:1])  # Major at 14:00, 14:10, 14:20
    later = NOW + 2 * HOUR  # 16:30: the last Major was 2 hours before the first check
    care.check(client(), later)
    mq = care.view(later)["users"][0]
    assert mq["status"] == "ok" and mq["usual"]["enough"]  # first seen when over: not lit
    assert [(i["since_local"], i["over"]) for i in mq["earlier"]] == [("14:20", "none in the hour after 14:20")]
    care.check(client(), later + timedelta(minutes=15))
    assert len(care.issues["mq2208"]) == 1  # kept once


def test_the_tile_bars_are_their_major_actions_each_day_from_aternity_today_last():
    """The bars were the issues this app saved: none for a day it did not check and none for Major
    actions within the usual, so "3 rated Major" had nothing behind it (stage, Nov 15 2026)."""
    care = Care(today_with(2), lambda: USERS[:1])
    care.check(None, NOW)  # no Aternity: the days before are not read (dashed), today from the reader
    assert [d["major"] for d in care.view(NOW)["users"][0]["days"]] == [None] * 7 + [2]
    care.check(client(), NOW)
    mq = care.view(NOW)["users"][0]
    days = mq["days"]
    assert [d["date"] for d in days][0] == "2026-11-02" and days[-1]["date"] == "2026-11-09" and days[-1]["today"]
    assert [d["major"] for d in days] == [9, 9, 0, 2, 1, 2, 1, 2]  # by 14:30: their 22:00 one is left out
    assert days[-1]["major"] == mq["major"]  # today's bar is the tile's "2 rated Major"
    mine = [d["major"] for d in days if d["usual"]]  # the dark bars are the usual's days: its range
    assert (min(mine), max(mine)) == (mq["usual"]["low"], mq["usual"]["high"]) == (0, 2)
    assert [d["usual"] for d in days] == [False, False, True, True, True, True, True, False]
    assert mq["by_local"] == "14:30" and not any(d["issue"] for d in days)
    care.issues["mq2208"].append({"day": "2026-11-06", "kind": "slow", "level": "Major", "key": "slow 2026-11-06 1",
                                  "since": "2026-11-06T15:00:00Z", "last": "2026-11-06T15:20:00Z", "text": "x"})
    mq = care.view(NOW)["users"][0]
    assert [d["issue"] for d in mq["days"]] == [False, False, False, False, True, False, False, False]
    assert mq["before"] == "Fri Nov 06: Major slow actions"


def test_their_days_before_cut_short_are_not_a_usual_and_are_read_again():
    """A reply cut short has low counts: as a usual it would light the tile too often."""
    seen = []

    def handler(request):  # every page says there is more: max_pages stops it
        seen.append(1)
        body = history(request).json()
        return httpx.Response(200, json={**body, "@odata.nextLink": str(request.url)})
    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0", username="u", password="p",
                         max_pages=1)
    cut = AternityClient(cfg, transport=httpx.MockTransport(handler))
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(cut, NOW)
    mq = care.view(NOW)["users"][0]
    assert mq["usual"]["enough"] is False and mq["usual"]["cut"] and mq["status"] == "ok"  # not judged
    assert all(d["cut"] for d in mq["days"][:-1]) and "cut short" in care.notes["history"]
    care.check(cut, NOW + timedelta(minutes=5))
    assert len(seen) == 2  # read again at the next check


def test_an_open_slow_issue_is_not_back_within_a_usual_that_could_not_be_read():
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(client(), NOW)  # 14:30: open since 14:20
    care.history["mq2208"]["cut"] = True  # the next read of their days before was cut short
    care.check(None, NOW + timedelta(minutes=15))
    assert "cleared" not in care.issues["mq2208"][0]  # it said "back within their usual at 14:45"
    care.check(None, NOW + HOUR)  # 15:30: an hour after 14:20 with no Major action
    i = care.issues["mq2208"][0]
    assert (i["ended"], i["cleared"]) == ("quiet", "2026-11-09T20:20:00Z")

