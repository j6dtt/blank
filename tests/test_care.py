"""The Care list tab: named users checked every 15 minutes against their own usual.
Made-up users, PCs and apps."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from uxcorrelate import web
from uxcorrelate.aternity import AternityClient
from uxcorrelate.care import CARE_MAX, Care
from uxcorrelate.config import AternityConfig, CareUser, load_settings
from uxcorrelate.today import HOUR, Today

ZONE = "America/Chicago"
NOW = datetime(2026, 11, 9, 20, 30, tzinfo=timezone.utc)  # Monday 14:30 in Chicago
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
    """8 days of activity, by username: MQ2208 0-2 Major by 14:30 on working days; tz9013 1-3."""
    flt = request.url.params["$filter"].lower()
    tz = Today(ZONE)
    rows = []
    for back, majors in ((1, 5), (2, 9), (3, 1), (4, 2), (5, 0), (6, 2), (7, 1)):  # Sun 5, Sat 9: weekend
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
    assert "TIMEFRAME ge 2026-11-01T05:00:00Z" in seen[0]  # 8 days before today's midnight (Chicago)
    v = care.view(NOW)
    assert v["count"] == 1 and [u["title"] for u in v["users"]] == ["Payroll lead", "ghost1", "tz9013"]
    mq = v["users"][0]
    assert (mq["status"], mq["level"], mq["since_local"], mq["username"]) == ("issue", "Major", "14:20", "MQ2208")
    assert mq["usual"] == {"enough": True, "days": 5, "low": 0, "high": 2, "kind": "working days"}  # weekend left out
    assert mq["issues"][0]["text"].startswith("3 Major slow actions by 14:30, the last at 14:20; on the 5 working "
                                              "days before that "
                                              "they used a PC: 0 to 2 by this time. Most in Mail (3).")
    assert mq["pcs"] == [{"device": "VD-7", "kind": "virtual"}] and (mq["actions"], mq["major"]) == (4, 3)
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

        def check(self, client, now):
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
    t0 = datetime(2026, 11, 9, 17, 42, tzinfo=timezone.utc)  # 11:42 Chicago
    events = [ev(t0, "MQ2208", "VD-7", "System Crash", "Critical"),
              ev(t0, "MQ2208", "VD-7", "Application Crash", "Major", "Application", "Mail"),
              ev(t0 + HOUR, "MQ2208", "VD-7", "Application Crash", "Major", "Application", "Mail"),
              ev(t0, "MQ2208", "VD-7", "Low Memory Pagefaults", "Minor")]
    care = Care(today_with(0, events), lambda: USERS[:1])
    care.check(client(), NOW)
    mq = care.view(NOW)["users"][0]
    assert mq["level"] == "Critical" and mq["since_local"] == "11:42"
    assert [i["text"] for i in mq["issues"]] == [
        "System Crash (System), first at 11:42 on VD-7.",
        "Application Crash (Application) 2 times in Mail, first at 11:42 on VD-7."]


def test_the_investigation_runs_once_an_hour_and_its_headline_is_the_why():
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(client(), NOW)
    calls = []

    def investigate(username, start, end):
        calls.append((username, start, end))
        return {"answer": {"title": "Mail was slow on VD-7", "reason": "Its CPU was high.", "level": "found"}}
    assert care.run_next(investigate, NOW) == "mq2208" and care.run_next(investigate, NOW) is None
    assert calls == [("MQ2208", datetime(2026, 11, 9, 19, 50, tzinfo=timezone.utc), NOW)]  # 30 min before 14:20
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
                                                                            "13:50 to 14:30 America/Chicago")
    assert [i["kind"] for i in detail["history"]] == ["event", "slow"]  # newest first


def test_a_slow_spell_is_over_after_an_hour_with_no_major_action_and_a_later_one_is_a_new_issue():
    care = Care(today_with(3), lambda: USERS[:1])  # Major at 14:00, 14:10, 14:20
    care.check(client(), NOW)
    care.run_next(lambda u, s, e: {"answer": {"title": "Mail was slow on VD-7"}}, NOW)
    at = lambda h, m: datetime(2026, 11, 9, h + 6, m, tzinfo=timezone.utc)  # noqa: E731  (Chicago time)
    care.check(client(), at(15, 10))
    assert care.view(at(15, 10))["users"][0]["status"] == "issue"  # 50 min after the last
    care.check(client(), at(15, 25))  # 65 min: over, though 3 is still more than their usual 0 to 2
    mq = care.view(at(15, 25))["users"][0]
    assert (mq["status"], mq["issues"], mq["level"]) == ("ok", [], None)
    assert [(i["since_local"], i["over"]) for i in mq["cleared"]] == [("14:20", "none in the hour after 14:20")]
    assert mq["why"]["title"] == "Mail was slow on VD-7"  # the investigation of the day stays
    assert care.detail("mq2208", at(15, 25))["history"][0]["over"] == "none in the hour after 14:20"
    care.check(client(), at(15, 25))
    assert len(care.issues["mq2208"]) == 1  # over once
    # a late row: a Major at 15:15, read at 15:40, is less than an hour after 14:20: the same spell again
    three = care.today.midnight(date(2026, 11, 9)) + 15 * HOUR
    care.today.add_hour(three, [act(at(15, 15), "MQ2208", "VD-7", "Major")], F, at(15, 40))
    care.check(client(), at(15, 40))
    slow = care.issues["mq2208"]
    assert len(slow) == 1 and "cleared" not in slow[0] and slow[0]["last"] == "2026-11-09T21:15:00Z"
    assert care.pending == []  # not a new issue
    care.check(client(), at(16, 20))
    assert care.issues["mq2208"][0]["cleared"] == "2026-11-09T22:20:00Z"
    # 17:00, still more than their usual: a new spell, a new issue, a new investigation (over an hour on)
    five = care.today.midnight(date(2026, 11, 9)) + 17 * HOUR
    care.today.add_hour(five, [act(at(17, 0), "MQ2208", "VD-7", "Major")], F, at(17, 5))
    care.check(client(), at(17, 5))
    slow = care.issues["mq2208"]
    assert [("cleared" in i, i["since"]) for i in slow] == [(True, "2026-11-09T20:20:00Z"),
                                                             (False, "2026-11-09T23:00:00Z")]
    assert slow[1]["key"].endswith(" 2") and care.pending == ["mq2208"]
    assert care.view(at(17, 5))["users"][0]["since_local"] == "17:00"


def test_an_open_slow_issue_saved_by_the_build_before_ends_at_its_last_major_action():
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(client(), NOW)
    care.issues["mq2208"] = [{"key": "slow 2026-11-09", "kind": "slow", "level": "Major", "day": "2026-11-09",
                              "since": "2026-11-09T20:00:00Z", "text": "3 Major slow actions by 14:30"}]  # no "last"
    care.check(client(), datetime(2026, 11, 9, 21, 25, tzinfo=timezone.utc))
    assert care.issues["mq2208"][0]["last"] == "2026-11-09T20:20:00Z"
    assert care.view(NOW + HOUR)["users"][0]["cleared"][0]["over"] == "none in the hour after 14:20"


def test_a_slow_spell_is_over_when_they_are_back_within_their_usual():
    care = Care(today_with(3), lambda: USERS[:1])
    care.check(client(), NOW)
    late = NOW + 8 * HOUR  # 22:30: their days before had one more Major at 22:00, so 3 is within 1 to 3
    care.check(client(), late)
    mq = care.view(late)["users"][0]
    assert mq["status"] == "ok" and [i["over"] for i in mq["cleared"]] == ["back within their usual at 22:30"]


def test_every_user_has_a_tile_ok_with_what_was_checked():
    care = Care(today_with(2), lambda: USERS[:2])
    care.check(client(), NOW)
    v = care.view(NOW)
    assert [(u["title"], u["status"]) for u in v["users"]] == [("Payroll lead", "ok"), ("tz9013", "ok")]
    mq = v["users"][0]
    assert (mq["actions"], mq["major"], mq["usual"]["low"], mq["usual"]["high"]) == (3, 2, 0, 2)
    assert mq["cleared"] == [] and mq["pcs"] == [{"device": "VD-7", "kind": "virtual"}]


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
