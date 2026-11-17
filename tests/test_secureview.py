"""SecureView (secureview.py): the devices people sat at whose name matches secureview.match,
first and last heard per local day, people with their own times, silent 30 / 60 / 90 days, and
Aternity's daily data for the days before the records. Made-up devices and users."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from uxcorrelate import web
from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import AternityConfig, Settings, config_notes, load_settings
from uxcorrelate.secureview import SecureView, keep_backfilling
from uxcorrelate.today import HOUR, Today, _read_hour

ZONE = "America/New_York"  # UTC-5 in November
MATCH = "^[A-Za-z0-9]{7}-.*THIN.*$"
F = {"timestamp": "TIMEFRAME", "username": "USERNAME", "device": "DEVICE_NAME", "client_device": "CLIENT_DEVICE_NAME"}
NOW = datetime(2026, 11, 17, 21, 45, tzinfo=timezone.utc)  # Tuesday 16:45 in New York


def at(d: date, hh: int, mm: int = 0) -> datetime:
    """Local time on day d, in UTC (November: UTC-5)."""
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc) + timedelta(hours=5)


def row(t, client, user="ann", desk="VD-01"):
    return {"TIMEFRAME": t.isoformat(), "USERNAME": user, "DEVICE_NAME": desk, "CLIENT_DEVICE_NAME": client}


def hour_rows(sv, rows, now=NOW, partial=False):
    """The rows as the Today reader reads them: by the hour they fall in."""
    per = {}
    for r in rows:
        t = datetime.fromisoformat(r["TIMEFRAME"])
        per.setdefault(t.replace(minute=0, second=0, microsecond=0), []).append(r)
    for start, rs in sorted(per.items()):
        sv.add_hour(start, rs, F, now, partial)


def test_only_matching_devices_any_case_and_off_when_empty_or_wrong(tmp_path):
    sv = SecureView(MATCH, ZONE, tmp_path)
    assert sv.on() and sv.matches("HV00045-SAMPLE45THIN") and sv.matches("hv00047-sample47thin")
    assert sv.matches("AB12CD3-HOMETHINVM") and sv.matches("AB12CD3-THINE")
    for no in ("WTC0037", "WTB41-THINLAB", "HV0045-SAMPLETHIN", "HV00045SAMPLETHIN", "HV00045-SAMPLE", None, ""):
        assert not sv.matches(no), no
    assert not SecureView("", ZONE, tmp_path).on()
    bad = SecureView("([", ZONE, tmp_path)
    assert not bad.on() and "not a regular expression" in bad.error
    assert bad.view(NOW)["error"] == bad.error


def test_a_wrong_pattern_is_a_config_note_and_an_empty_one_is_off(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("secureview:\n  match: '(['\n")
    assert any("secureview.match" in n for n in config_notes(cfg))
    cfg.write_text(f"secureview:\n  match: '{MATCH}'\n")
    assert load_settings(cfg).secureview.match == MATCH and not any("secureview" in n for n in config_notes(cfg))
    assert Settings().secureview.match == ""


def test_first_and_last_heard_widen_with_late_rows_and_each_person_keeps_their_own_times(tmp_path):
    sv = SecureView(MATCH, ZONE, tmp_path)
    d = date(2026, 11, 17)
    hour_rows(sv, [row(at(d, 9, 12), "HV00043-HOME43THINE", "bob", "VD-03"), row(at(d, 11, 40), "HV00043-HOME43THINE", "bob", "VD-03"),
                   row(at(d, 10, 0), "WTC0037", "cat")])
    # a second read of the 13:00 hour; Aternity sent an 08:05 row of the same device late
    hour_rows(sv, [row(at(d, 13, 30), "HV00043-HOME43THINE", "Dan", "VD-07"), row(at(d, 8, 5), "HV00043-HOME43THINE", "bob", "VD-03")])
    hour_rows(sv, [row(at(d, 13, 30), "HV00043-HOME43THINE", "dan", "VD-07")])  # the same row again
    v = sv.view(NOW)
    [x] = v["today"]["devices"]
    assert (x["name"], x["first_local"], x["last_local"]) == ("HV00043-HOME43THINE", "08:05", "13:30")
    assert x["people"] == [{"user": "bob", "first_local": "08:05", "last_local": "11:40"},
                           {"user": "Dan", "first_local": "13:30", "last_local": "13:30"}]
    assert x["desktops"] == ["VD-03", "VD-07"] and v["today"]["people"] == 2
    assert [h["n"] for h in v["today"]["hours"]][8:14] == [1, 1, 0, 1, 0, 1] and v["today"]["hours"][17]["n"] is None
    assert x["recent"] is False  # 13:30, three hours before 16:45


def test_a_row_belongs_to_its_own_local_day(tmp_path):
    sv = SecureView(MATCH, ZONE, tmp_path)
    # 23:30 on Nov 16 and 00:30 on Nov 17 local: both in UTC Nov 17
    late, early = at(date(2026, 11, 16), 23, 30), at(date(2026, 11, 17), 0, 30)
    hour_rows(sv, [row(late, "HV00045-SAMPLE45THIN"), row(early, "HV00045-SAMPLE45THIN")])
    assert sv.days[date(2026, 11, 16)]["devices"]["HV00045-SAMPLE45THIN"]["hours"] == {23}
    assert sv.days[date(2026, 11, 17)]["devices"]["HV00045-SAMPLE45THIN"]["hours"] == {0}
    assert sv.view(NOW)["today"]["first_local"] == "00:30"


def test_saved_days_are_read_back_after_a_restart(tmp_path):
    sv = SecureView(MATCH, ZONE, tmp_path)
    d = date(2026, 11, 17)
    hour_rows(sv, [row(at(d, 9), "HV00045-SAMPLE45THIN", "ann")])
    sv.save(NOW)
    saved = json.loads((tmp_path / "secureview" / "2026-11-17.json").read_text())
    assert list(saved["devices"]) == ["HV00045-SAMPLE45THIN"] and saved["devices"]["HV00045-SAMPLE45THIN"]["people"]["ann"][0] == "ann"
    assert not list((tmp_path / "secureview").glob(".*.tmp"))
    again = SecureView(MATCH, ZONE, tmp_path)
    assert again.view(NOW)["today"]["devices"][0]["first_local"] == "09:00"


def test_silent_30_60_90_days_and_the_last_person(tmp_path):
    sv = SecureView(MATCH, ZONE, tmp_path)
    today = date(2026, 11, 17)
    for ago, dev, user in [(120, "OLD0001-AWAYTHIN", "eve"), (95, "OLD0001-AWAYTHIN", "eve"), (65, "MID0002-HOMETHIN", "fay"),
                           (40, "NEW0003-HOMETHIN", "gus"), (31, "NEW0003-HOMETHIN", "hal"), (2, "NOW0004-HOMETHINVM", "ivy")]:
        hour_rows(sv, [row(at(today - timedelta(days=ago), 10), dev, user)])
    h = sv.view(NOW)["history"]
    by = {x["name"]: x for x in h["devices"]}
    assert {n: x["silent"] for n, x in by.items()} == {"OLD0001-AWAYTHIN": 95, "MID0002-HOMETHIN": 65,
                                                       "NEW0003-HOMETHIN": 31, "NOW0004-HOMETHINVM": 2}
    assert [x["name"] for x in h["devices"]][0] == "OLD0001-AWAYTHIN"  # the longest silent first
    assert by["NEW0003-HOMETHIN"]["person"] == "hal" and by["NEW0003-HOMETHIN"]["days"] == 2
    assert by["OLD0001-AWAYTHIN"]["first"] == "2026-07-20" and h["start"] == "2026-07-20"
    assert h["heard30"] == 1 and h["new30"] == 1  # only NOW0004 first heard in the last 30 days
    assert h["silent_by_day"][-1]["n"] == 3  # today: OLD, MID, NEW silent 30 days or more
    assert h["per_day"][-3]["n"] == 1 and h["per_day"][-3]["part"] is True  # 2 days ago: not read in full
    assert h["unread"] == 120 - 6  # every day since Jul 20 but the six with a row (today is not counted)


def test_the_usual_by_now_needs_days_read_in_full(tmp_path):
    sv = SecureView(MATCH, ZONE, tmp_path)
    today = date(2026, 11, 17)  # a Tuesday
    full = NOW + timedelta(days=1)  # every hour read after it ended
    working = [d for d in (today - timedelta(days=k) for k in range(1, 9)) if d.weekday() < 5][:5]
    for k, d in enumerate(working):
        for s in sv.hour_starts(d):
            sv.add_hour(s, [], F, full)
        hour_rows(sv, [row(at(d, 9), "AAA0001-ATHIN"), row(at(d, 15), "BBB0002-BTHIN")] + ([row(at(d, 18), "CCC0003-CTHIN")] if k else []), full)
    hour_rows(sv, [row(at(today, 9), "AAA0001-ATHIN")])
    t = sv.view(NOW)["today"]
    assert t["usual"]["days"] == 5 and (t["usual"]["low"], t["usual"]["high"]) == (2, 2)  # by 16:45: AAA, BBB
    assert (t["hours"][9]["low"], t["hours"][9]["high"]) == (1, 1)
    cut = SecureView(MATCH, ZONE, tmp_path / "other")
    for d in working:
        hour_rows(cut, [row(at(d, 9), "AAA0001-ATHIN")], full)  # only the hours with rows: not whole days
    assert cut.view(NOW)["today"]["usual"].get("low") is None


class Daily:
    """A made-up Aternity answering BUSINESS_ACTIVITIES_DAILY per day."""

    def __init__(self, days: dict, fail=False):
        self.days, self.fail, self.asked = days, fail, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(400, json={"error": {"message": "Unknown column: CLIENT_DEVICE_NAME"}})
        flt = req.url.params.get("$filter", "")
        lo = datetime.fromisoformat(flt.split("ge ")[1].split(" and")[0].replace("Z", "+00:00"))
        self.asked.append(req.url.params.get("$select"))
        rows = []
        for t, names in self.days.items():
            if lo <= t < lo + timedelta(hours=25):
                rows += [{"TIMEFRAME": t.isoformat(), "CLIENT_DEVICE_NAME": n} for n in names]
        return httpx.Response(200, json={"value": rows})


def client_of(handler):
    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0", username="u", password="p")
    return AternityClient(cfg, transport=httpx.MockTransport(handler))


def test_aternity_daily_data_fills_the_days_before_the_records(tmp_path):
    today = date(2026, 11, 17)
    # day rows start at a midnight: UTC for one day, local for another (whose midnight is not documented)
    d1, d2 = today - timedelta(days=40), today - timedelta(days=75)
    utc_midnight = datetime(d1.year, d1.month, d1.day, tzinfo=timezone.utc)
    local_midnight = at(d2, 0)
    fake = Daily({utc_midnight: ["HV00148-SAMPLE48THIN", "WTC0037", None], local_midnight: ["HV00149-SAMPLE49THINVM"]})
    sv = SecureView(MATCH, ZONE, tmp_path)
    sv.backfill(client_of(fake), NOW)
    assert fake.asked[0] == "TIMEFRAME,CLIENT_DEVICE_NAME" and len(fake.asked) == 90
    assert set(sv.days[d1]["devices"]) == {"HV00148-SAMPLE48THIN"} and sv.days[d1]["daily"]
    assert set(sv.days[d2]["devices"]) == {"HV00149-SAMPLE49THINVM"}
    h = sv.view(NOW)["history"]
    assert h["daily"]["state"] == "ok" and "back to" in h["daily"]["note"] and h["start"] == d2.isoformat()
    assert {x["name"]: x["silent"] for x in h["devices"]} == {"HV00148-SAMPLE48THIN": 40, "HV00149-SAMPLE49THINVM": 75}
    assert all(x["person"] is None for x in h["devices"])  # days only: no people, no times
    detail = sv.device("HV00148-SAMPLE48THIN", NOW)
    assert [q for q in detail["squares"] if q["heard"]] == [{"date": d1.isoformat(), "heard": True, "day_only": True, "part": False,
                                                              "first_local": None, "last_local": None, "people": [], "desktops": []}]
    sv.save(NOW)
    fake.asked.clear()
    again = SecureView(MATCH, ZONE, tmp_path)
    again.backfill(client_of(fake), NOW)
    assert len(fake.asked) == 88  # the two days read are not asked again


def test_daily_data_with_no_device_or_refused_is_said(tmp_path):
    today = date(2026, 11, 17)
    t = datetime.combine(today - timedelta(days=3), datetime.min.time(), timezone.utc)
    sv = SecureView(MATCH, ZONE, tmp_path)
    sv.backfill(client_of(Daily({t: [None]})), NOW)
    assert sv.daily["state"] == "none" and "CLIENT_DEVICE_NAME is empty" in sv.daily["note"]
    refused = SecureView(MATCH, ZONE, tmp_path / "b")
    refused.backfill(client_of(Daily({}, fail=True)), NOW)
    assert refused.daily["state"] == "failed" and "could not be read" in refused.daily["note"]
    assert refused.view(NOW)["history"]["daily"]["state"] == "failed"


def test_the_today_reader_reads_the_device_only_when_on_and_records_every_hour_it_reads(tmp_path):
    asked = []

    def handler(req):
        asked.append(req.url.params.get("$select"))
        lo = req.url.params["$filter"].split("ge ")[1].split(" and")[0]
        return httpx.Response(200, json={"value": [{"TIMEFRAME": lo.replace("Z", "+00:00"), "USERNAME": "ann",
                                                    "DEVICE_NAME": "VD-01", "CLIENT_DEVICE_NAME": "HV00045-SAMPLE45THIN"}]})
    today = Today(ZONE, tmp_path)
    _read_hour(today, None, client_of(handler), today.hour_of(NOW), NOW)
    assert "CLIENT_DEVICE_NAME" not in asked[-1]  # off: not read
    today.secureview = SecureView(MATCH, ZONE, tmp_path)
    yesterday = today.hour_of(NOW) - 20 * HOUR  # an hour of yesterday, read at start for the name list
    _read_hour(today, None, client_of(handler), yesterday, NOW)
    assert "CLIENT_DEVICE_NAME" in asked[-1]
    assert "HV00045-SAMPLE45THIN" in today.secureview.days[date(2026, 11, 16)]["devices"]


def test_the_admin_page_needs_its_login(monkeypatch, tmp_path):
    monkeypatch.setenv(web.ADMIN_ENV, "s3cret-test")
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    app = web.create_app()
    https = TestClient(app, base_url="https://testserver")
    assert https.get("/admin/api/secureview").status_code == 401
    assert https.get("/admin/api/secureview/device?name=x").status_code == 401
    assert https.post("/admin/api/login", json={"password": "s3cret-test"}).status_code == 200
    v = https.get("/admin/api/secureview").json()
    assert v["on"] is False and v["match"] == ""  # not set in config.yaml
    assert https.get("/admin/api/secureview/device?name=x").status_code == 404
    page = (web.STATIC / "admin.html").read_text(encoding="utf-8")
    assert 'fetch("admin/api/secureview"' in page and "confirm(" not in page and "alert(" not in page


def test_the_daily_read_waits_for_the_first_read_and_runs_each_night(tmp_path):
    class T:
        ready, off = False, None
    t, naps = T(), []
    sv = SecureView(MATCH, ZONE, tmp_path)

    def sleep(s):
        naps.append(s)
        t.ready = True
        if len(naps) > 2:
            raise StopIteration
    calls = []
    try:
        keep_backfilling(sv, t, lambda: calls.append(1) or None, sleep, clock=lambda: NOW)
    except StopIteration:
        pass
    assert naps[0] == 30 and calls == [1, 1]
    assert naps[1] == (datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc) - NOW).total_seconds()  # 03:00 local


def test_the_daily_source_names_are_riverbeds_own():
    from pathlib import Path
    from uxcorrelate.config import SECUREVIEW_DAILY
    from uxcorrelate.verify import parse_odata_metadata
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
    assert {v for k, v in SECUREVIEW_DAILY.items() if k != "entity"} <= schema[SECUREVIEW_DAILY["entity"]]
    assert "CLIENT_DEVICE_NAME" in schema["BUSINESS_ACTIVITIES_RAW"]  # what the Today reader adds
