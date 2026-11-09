"""The "Today" tab: counts of what Aternity rated Critical / Major today, against the
same kind of day before. Made-up PCs, users and apps."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from uxcorrelate import web
from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import AternityConfig
from uxcorrelate.names import NameList
from uxcorrelate.today import HOUR, Today, keep_reading, usual

F = {"timestamp": "TIMEFRAME", "username": "USERNAME", "device": "DEVICE_NAME",
     "application": "APPLICATION_NAME", "status": "SLA_STATUS"}
H = {"timestamp": "TIMEFRAME", "device": "DEVICE_NAME", "category": "HEALTH_EVENT_CATEGORY",
     "name": "HEALTH_EVENT_NAME", "severity": "HEALTH_EVENT_SEVERITY"}
ZONE = "America/Chicago"  # UTC-6 in November
NOW = datetime(2026, 11, 9, 20, 30, tzinfo=timezone.utc)  # Monday 14:30 in Chicago


def act(pc, status, app="ERP", user=None):
    return {"USERNAME": user or f"u-{pc}", "DEVICE_NAME": pc, "APPLICATION_NAME": app, "SLA_STATUS": status}


def ev(t, pc, name, sev, cat="Application"):
    return {"TIMEFRAME": t.isoformat(), "DEVICE_NAME": pc, "HEALTH_EVENT_CATEGORY": cat,
            "HEALTH_EVENT_NAME": name, "HEALTH_EVENT_SEVERITY": sev}


def day_file(d, top_by_hour, major_at_9=0, complete=True):
    return {"date": d.isoformat(), "complete": complete, "top_pcs_by_hour": top_by_hour,
            "hours": {"9": {"Major": major_at_9}}}


def filled(**kw):
    t = Today(ZONE, **kw)
    nine = t.midnight(date(2026, 11, 9)) + 9 * HOUR  # 09:00 Chicago
    t.add_hour(nine, [act("PC1", "Major"), act("PC1", "Major"), act("PC2", "Minor"), act("PC3", "Normal"),
                      act("PC4", "Major", app="Mail"), act("PC5", "Undefined")], F, NOW)
    t.add_hour(nine + 5 * HOUR, [act("PC2", "Major")], F, NOW)  # 14:00, this hour so far
    nine_utc = t.midnight(date(2026, 11, 9)) + 9 * HOUR
    t.set_health([ev(nine_utc, "PC1", "Application Crash", "Major"), ev(nine_utc, "PC2", "Application Crash", "Major"),
                  ev(nine_utc, "PC3", "Background Process Crash", "Minor", "Background Process")], H, today=True)
    t.ready = True
    return t


def test_counts_pcs_users_and_apps_with_aternitys_own_words():
    v = filled().view(NOW)
    assert v["zone"] == ZONE and v["date"] == "2026-11-09" and v["kind"] == "working days"
    s = v["slow"]
    assert (s["status"], s["pcs"], s["users"], s["actions"], s["active_pcs"]) == ("Major", 3, 3, 4, 5)
    assert (s["by"], s["by_pcs"]) == ("14:00", 2)  # PC2's Major came in the 14:00 hour, not before
    assert v["applications"] == [{"application": "ERP", "actions": 3, "runs": 6, "pcs": 2},
                                 {"application": "Mail", "actions": 1, "runs": 1, "pcs": 1}]
    hours = {h["hour"]: h for h in v["hours"]}
    assert hours["09:00"]["count"] == 3 and hours["14:00"]["now"] and hours["10:00"]["count"] is None
    assert [(e["name"], e["severity"], e["pcs"]) for e in v["tiles"]] == [("Application Crash", "Major", 2)]
    assert v["none"] == ["Critical"]  # said, not left out
    assert [e["name"] for e in v["events"]] == ["Application Crash", "Background Process Crash"]
    assert any("1 of the 3 working days" in x or "0 of the 3 working days" in x for x in v["notes"])


def test_usual_is_the_range_of_the_same_kind_of_day_before():
    assert usual([2, 5, 4], 6)["verdict"] == "more" and usual([2, 5, 4], 1)["verdict"] == "fewer"
    assert usual([2, 5, 4], 5) == {"enough": True, "days": 3, "low": 2, "high": 5, "verdict": "usual"}
    assert usual([2, 5], 9) == {"enough": False, "days": 2}


def test_actions_are_compared_with_the_saved_working_days_by_the_same_hour(tmp_path):
    days = tmp_path / "days"
    days.mkdir()
    by_hour = lambda n: [0] * 13 + [n] * 11  # noqa: E731  PCs with a Major action by the end of each hour
    for d, n in ((date(2026, 11, 6), 1), (date(2026, 11, 5), 1), (date(2026, 11, 4), 1),
                 (date(2026, 11, 8), 9),  # a Sunday: another kind of day
                 (date(2026, 11, 3), 0)):
        (days / f"{d}.json").write_text(json.dumps(day_file(d, by_hour(n), major_at_9=2)))
    (days / "2026-11-02.json").write_text(json.dumps(day_file(date(2026, 11, 2), by_hour(5), complete=False)))
    v = filled(store=tmp_path).view(NOW)
    # by 14:00 (the end of 13:00): 1, 1, 1, 0 on the 4 complete working days; today 2
    assert v["slow"]["usual"] == {"enough": True, "days": 4, "low": 0, "high": 1, "verdict": "more"}
    assert {h["hour"]: h["usual"] for h in v["hours"]}["09:00"] == 2
    assert not any("needed are saved" in x for x in v["notes"])


def test_a_finished_day_is_saved_without_names_and_its_hours_dropped(tmp_path):
    t = Today(ZONE, tmp_path)
    d = date(2026, 11, 8)
    starts = t.hour_starts(d)
    for s in starts:
        t.add_hour(s, [act("PC9", "Major", user="ann")] if s == starts[10] else [], F, s + 2 * HOUR)
    t.add_hour(t.hour_of(NOW), [act("PC1", "Normal")], F, NOW)
    t.finish(NOW)
    saved = json.loads((tmp_path / "days" / "2026-11-08.json").read_text())
    assert saved["complete"] and saved["top_pcs"] == 1 and saved["top_pcs_by_hour"][10] == 1
    assert saved["top_pcs_by_hour"][9] == 0 and saved["hours"]["10"] == {"Major": 1}
    assert "PC9" not in json.dumps(saved) and "ann" not in json.dumps(saved)
    assert all(h.day == date(2026, 11, 9) for h in t.hours.values())
    assert Today(ZONE, tmp_path).saved[d]["top_pcs"] == 1  # read back at the next start


def test_a_day_with_an_hour_missing_is_saved_but_never_used_as_usual(tmp_path):
    t = Today(ZONE, tmp_path)
    d = date(2026, 11, 6)
    for s in t.hour_starts(d)[1:]:  # midnight hour never read
        t.add_hour(s, [], F, s + 2 * HOUR)
    t.finish(NOW)
    assert t.saved[d]["complete"] is False


def test_hours_due_now_this_hour_one_not_read_again_and_one_never_read():
    t = Today(ZONE)
    cur = t.hour_of(NOW)
    t.add_hour(cur - HOUR, [], F, cur)  # read as it ended: read again 15 min later
    t.add_hour(cur - 2 * HOUR, [], F, cur)
    due = t.hours_due(NOW)
    assert due[0] == cur and cur - HOUR in due and cur - 2 * HOUR not in due
    assert t.midnight(date(2026, 11, 9)) in due  # today's first hour was never read


def test_events_by_this_time_on_the_days_before_and_critical_first():
    t = filled()
    mid = lambda d: t.midnight(d)  # noqa: E731
    rows = []
    for d, crashes in ((date(2026, 11, 6), 1), (date(2026, 11, 5), 1), (date(2026, 11, 4), 1)):
        rows += [ev(mid(d) + 8 * HOUR, f"PC{i}", "Application Crash", "Major") for i in range(crashes)]
        rows.append(ev(mid(d) + 20 * HOUR, "PC8", "Application Crash", "Major"))  # after 14:30: not "by this time"
    t.set_health(rows, H, today=False, days_read=(date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5),
                                                  date(2026, 11, 6)))
    t.set_health(t_health_today(t) + [ev(mid(date(2026, 11, 9)) + HOUR, "PC7", "System Crash", "Critical", "System")],
                 H, today=True)
    v = t.view(NOW)
    assert [(e["severity"], e["name"]) for e in v["tiles"]] == [("Critical", "System Crash"),
                                                                ("Major", "Application Crash")]
    crash = v["tiles"][1]
    # Nov 3 was read and had none: 0 by this time, so 4 days from 0 to 1
    assert crash["usual"] == {"enough": True, "days": 4, "low": 0, "high": 1, "verdict": "more"}
    # Nov 2 to 8: Nov 2 not read; Nov 3 read, none; Nov 4-6 whole days; the weekend not read
    assert [d["pcs"] for d in crash["days"]] == [None, 0, 2, 2, 2, None, None]
    assert v["none"] == []


def t_health_today(t):
    nine = t.midnight(date(2026, 11, 9)) + 9 * HOUR
    return [ev(nine, "PC1", "Application Crash", "Major"), ev(nine, "PC2", "Application Crash", "Major")]


def test_the_day_starts_at_midnight_in_the_set_time_zone_and_a_wrong_one_is_said():
    t = Today(ZONE)
    assert t.midnight(date(2026, 11, 9)) == datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)
    assert len(t.hour_starts(date(2026, 11, 1))) == 25  # clocks went back that night
    utc = Today("")
    assert utc.zone_label == "UTC" and "set timezone" in utc.zone_note
    assert "not a known time zone" in Today("Mars/Olympus").zone_note


def test_a_folder_that_cannot_be_written_is_said(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    t = Today(ZONE, blocker)  # its "days" folder cannot be made
    t.save(date(2026, 11, 8), {"complete": True})
    assert "cannot build up" in t.notes["files"] and "uid 1000" in t.notes["files"]


class Stop(Exception):
    pass


def test_the_reader_fills_today_and_the_name_list_then_waits(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        flt = request.url.params.get("$filter", "")
        seen.append((request.url.path.rsplit("/", 1)[-1], request.url.params.get("$select", ""), flt))
        if request.url.path.endswith("/BUSINESS_ACTIVITIES_RAW"):
            if "2026-11-09T15:00:00Z" in flt:  # 09:00-10:00 Chicago
                return httpx.Response(200, json={"value": [act("PC1", "Major")]})
            return httpx.Response(200, json={"value": []})
        if "2026-11-09T06:00:00Z" in flt:  # today's health events
            return httpx.Response(200, json={"value": [ev(NOW - HOUR, "PC1", "System Crash", "Critical", "System")]})
        return httpx.Response(200, json={"value": []})

    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0", username="u", password="p")
    t, names = Today(ZONE, tmp_path), NameList()
    with pytest.raises(Stop):
        keep_reading(t, names, lambda: AternityClient(cfg, transport=httpx.MockTransport(handler)),
                     lambda s: (_ for _ in ()).throw(Stop()), clock=lambda: NOW)
    acts = [x for x in seen if x[0] == "BUSINESS_ACTIVITIES_RAW"]
    assert len(acts) == 25 and acts[0][1] == ("TIMEFRAME,USERNAME,DEVICE_NAME,APPLICATION_NAME,SLA_STATUS,"
                                              "ACTIVITY_NAME,ACTIVITY_RESPONSE_TIME")
    assert acts[0][2] == "TIMEFRAME ge 2026-11-09T20:00:00Z and TIMEFRAME lt 2026-11-09T21:00:00Z"  # newest first
    health = [x for x in seen if x[0] == "DEVICE_HEALTH_RAW"]
    assert "USERNAME" in health[0][1].split(",")
    assert len(health) == 15 and health[-1][2].endswith("lt 2026-11-09T20:30:00Z")  # 14 days before, then today
    v = t.view(NOW)
    assert v["ready"] and v["slow"]["pcs"] == 1 and v["tiles"][0]["name"] == "System Crash"
    assert list(names.seen["device"]) == ["PC1"]


def test_not_set_and_still_reading_are_said():
    t = Today(ZONE)
    keep_reading(t, None, lambda: None, lambda s: None)
    assert t.view(NOW) == {"ready": False, "off": t.off, "status": "Starting", "zone": ZONE}
    assert "Aternity is not set" in t.off


def test_who_lists_the_users_and_pcs_behind_a_tile_or_a_row_with_a_span_to_investigate():
    t = Today(ZONE)
    nine = t.midnight(date(2026, 11, 9)) + 9 * HOUR  # 15:00 UTC
    G = dict(F, activity="ACTIVITY_NAME", response_time="ACTIVITY_RESPONSE_TIME")

    def run(minute, pc, app, secs, user=None, status="Major"):
        return dict(act(pc, status, app, user), TIMEFRAME=(nine + timedelta(minutes=minute)).isoformat(),
                    ACTIVITY_NAME="Open", ACTIVITY_RESPONSE_TIME=secs)
    t.add_hour(nine, [run(5, "PC1", "ERP", 12.0), run(40, "PC1", "ERP", 30.5), run(20, "PC1", "Mail", 9.0),
                      run(10, "PC2", "ERP", 50.0), run(15, "PC3", "ERP", 3.0, status="Minor")], G, NOW)
    t.add_hour(nine + 5 * HOUR, [run(5 * 60 + 25, "PC4", "Mail", 4.0)], G, NOW)  # 14:25, 5 min before NOW
    t.set_health([ev(nine, "PC1", "Application Crash", "Major") | {"USERNAME": "u-PC1"},
                  ev(nine + HOUR, "PC1", "Application Crash", "Major") | {"USERNAME": "u-PC1"},
                  ev(nine, "PC9", "Application Crash", "Major"),  # no user: still listed by its PC
                  ev(nine, "PC2", "Low Memory", "Minor", "System")], dict(H, username="USERNAME"), today=True)

    slow = t.who(NOW, "slow")
    assert (slow["what"], slow["total"], slow["pcs"], slow["users"]) == ("Major slow actions", 3, 3, 3)
    pc1 = slow["rows"][0]  # most slow actions first; Minor is not counted
    assert (pc1["device"], pc1["username"], pc1["count"]) == ("PC1", "u-PC1", 3)
    assert (pc1["application"], pc1["activity"], pc1["seconds"]) == ("ERP", "Open", 30.5)  # its slowest
    assert (pc1["first"], pc1["last"]) == ("09:05", "09:40")  # Chicago time
    assert (pc1["start"], pc1["end"]) == ("2026-11-09T14:35:00Z", "2026-11-09T16:10:00Z")  # 30 min each side
    assert [r["device"] for r in slow["rows"]] == ["PC1", "PC2", "PC4"]  # then the slowest
    assert slow["rows"][2]["end"] == "2026-11-09T20:30:00Z"  # never after now

    erp = t.who(NOW, "app", "ERP")
    assert erp["application"] == "ERP" and [(r["device"], r["count"]) for r in erp["rows"]] == [("PC1", 2), ("PC2", 1)]
    assert t.who(NOW, "app", "Nothing")["rows"] == []
    t.add_hour(nine + HOUR, [run(65, "PC5", None, 8.0)], G, NOW)
    none = t.who(NOW, "app", "(no application)")
    assert [r["device"] for r in none["rows"]] == ["PC5"] and none["application"] is None  # not put in the form

    crash = t.who(NOW, "event", "Application Crash", "Application")
    assert crash["what"] == "Application Crash (Application)" and crash["application"] is None
    assert [(r["device"], r["username"], r["count"]) for r in crash["rows"]] == [("PC1", "u-PC1", 2), ("PC9", None, 1)]
    assert (crash["rows"][0]["start"], crash["rows"][0]["end"]) == ("2026-11-09T14:00:00Z", "2026-11-09T17:00:00Z")
    assert "seconds" not in crash["rows"][0]  # events have no time taken
    assert t.who(NOW, "event", "Application Crash", "System")["rows"] == []  # the category counts too


def test_who_shows_at_most_the_limit_but_counts_them_all():
    t = Today(ZONE)
    nine = t.midnight(date(2026, 11, 9)) + 9 * HOUR
    t.add_hour(nine, [act(f"PC{i}", "Major") for i in range(70)], F, NOW)
    w = t.who(NOW, "slow")
    assert (w["total"], w["pcs"], len(w["rows"])) == (70, 70, 50)
    assert w["rows"][0]["first"] == "09:00"  # no time in the row: the hour's start


def test_the_endpoint():
    app = web.create_app()
    http = TestClient(app)  # not started: no background read in tests
    assert http.get("/api/today").json()["ready"] is False
    app.state.today.ready = True
    assert http.get("/api/today").json()["ready"] is True
    assert http.get("/api/today/who", params={"kind": "slow"}).json()["rows"] == []
    assert http.get("/api/today/who", params={"kind": "nope"}).status_code == 400
