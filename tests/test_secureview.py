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
    """A made-up Aternity answering BUSINESS_ACTIVITIES_DAILY per day ({day's TIMEFRAME: rows}) and
    BUSINESS_ACTIVITIES_HOURLY per hour ({hour's TIMEFRAME: rows}); a row is a device's name, or
    (device, username, desktop), added up as Aternity does."""

    def __init__(self, days: dict, fail=False, hours: dict | None = None, hour_fail=False, page_hours=None):
        self.days, self.fail, self.asked, self.hours, self.hour_fail = days, fail, [], hours or {}, hour_fail
        self.hourly_asked: list[tuple] = []
        self.page_hours = page_hours  # a read of more hours than this gets a next page (cut short at max_pages 1)

    @staticmethod
    def _row(t, n):
        dev, user, desk = n if isinstance(n, tuple) else (n, None, None)
        return {"TIMEFRAME": t.isoformat(), "CLIENT_DEVICE_NAME": dev, "USERNAME": user, "DEVICE_NAME": desk}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        flt = req.url.params.get("$filter", "")
        lo = datetime.fromisoformat(flt.split("ge ")[1].split(" and")[0].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(flt.split("lt ")[1].split(" and")[0].replace("Z", "+00:00"))
        if req.url.path.endswith("BUSINESS_ACTIVITIES_HOURLY"):
            self.hourly_asked.append((lo, hi))
            if self.hour_fail:
                return httpx.Response(404, json={"error": {"message": "Entity set 'BUSINESS_ACTIVITIES_HOURLY' doesn't exist."}})
            rows = [self._row(t, n) for t, names in self.hours.items() if lo <= t < hi for n in names]
            out = {"value": rows}
            if self.page_hours and hi - lo > timedelta(hours=self.page_hours):
                out["@odata.nextLink"] = str(req.url)  # more pages: cut short at max_pages
            return httpx.Response(200, json=out)
        if self.fail:
            return httpx.Response(400, json={"error": {"message": "Unknown column: CLIENT_DEVICE_NAME"}})
        self.asked.append(req.url.params.get("$select"))
        rows = []
        for t, names in self.days.items():
            if lo <= t < lo + timedelta(hours=25):
                rows += [self._row(t, n) for n in names]
        return httpx.Response(200, json={"value": rows})


def client_of(handler, max_pages=None):
    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0", username="u", password="p")
    if max_pages:
        cfg.max_pages = max_pages
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
    assert fake.asked[0] == "TIMEFRAME,CLIENT_DEVICE_NAME,USERNAME,DEVICE_NAME" and len(fake.asked) == 90
    # its hourly data first: no row on the two days before today (one may not be written yet), so none after
    assert len(fake.hourly_asked) == 8 and {hi - lo for lo, hi in fake.hourly_asked} == {timedelta(hours=6)}
    assert set(sv.days[d1]["devices"]) == {"HV00148-SAMPLE48THIN"} and sv.days[d1]["daily"]
    assert set(sv.days[d2]["devices"]) == {"HV00149-SAMPLE49THINVM"}
    h = sv.view(NOW)["history"]
    assert h["daily"]["state"] == "ok" and "back to" in h["daily"]["note"] and h["start"] == d2.isoformat()
    assert {x["name"]: x["silent"] for x in h["devices"]} == {"HV00148-SAMPLE48THIN": 40, "HV00149-SAMPLE49THINVM": 75}
    assert all(x["person"] is None for x in h["devices"])  # days only: no people, no times
    detail = sv.device("HV00148-SAMPLE48THIN", NOW)
    assert [q for q in detail["squares"] if q["heard"]] == [{"date": d1.isoformat(), "heard": True, "day_only": True, "by": "day",
                                                              "part": False, "first_local": None, "last_local": None, "people": [],
                                                              "desktops": []}]
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
    # two tabs, each with its link (the user, Nov 17 2026): Statistics opens first, SecureView by its link
    assert 'data-t="stats">Statistics<' in page and 'data-t="sv">SecureView<' in page
    assert 'const TABS = { stats: "statistics", sv: "secureview", act: "actions", code: "code" };' in page
    assert 'Object.keys(TABS).find(k => location.hash === "#" + TABS[k]) || "stats"' in page
    # by department across the page, a row sets the Department box (the user's pick B, Nov 17 2026)
    assert 'svDeptPick("svTDept", "tdept", tDept, "svTq");' in page and 'svDeptPick("svHDept", "hdept", hDept, "svHq");' in page
    assert "svHl(" not in page and 'SV.hdept === SV_NO_PERSON ? !x.person' in page


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


def test_search_finds_people_devices_and_desktops_over_every_day_kept(tmp_path):
    from uxcorrelate import secureview
    today = date(2026, 11, 17)
    d1, d2 = today - timedelta(days=1), today - timedelta(days=3)
    old = today - timedelta(days=60)
    sv = SecureView(MATCH, ZONE, tmp_path)
    t = datetime(old.year, old.month, old.day, tzinfo=timezone.utc)
    sv.backfill(client_of(Daily({t: ["HV00148-SAMPLE48THIN"]})), NOW)  # a day only: no times, no one named
    hour_rows(sv, [row(at(d1, 8, 5), "HV00148-SAMPLE48THIN", "CORP\\Ann", "VD-01"),
                   row(at(d1, 11, 40), "HV00148-SAMPLE48THIN", "CORP\\Ann", "VD-02"),
                   row(at(d1, 13, 0), "HV00148-SAMPLE48THIN", "bob", "VD-01"),
                   row(at(d2, 9, 0), "HV00149-SAMPLE49THIN", "CORP\\Ann", "VD-01"),
                   row(at(today, 7, 30), "HV00150-SAMPLE50THIN", "carol", "VD-77")])
    got = sv.search("ann", NOW)
    assert [m["name"] for m in got["people"]] == ["CORP\\Ann"] and not got["devices"] and not got["desktops"]
    ann = got["people"][0]
    assert (ann["days"], ann["first"], ann["last"], ann["devices"]) == (2, d2.isoformat(), d1.isoformat(), 2)
    assert ann["rows"] == [{"date": d1.isoformat(), "device": "HV00148-SAMPLE48THIN", "first_local": "08:05", "last_local": "11:40",
                            "by": "minute", "day_only": False, "desktops": ["VD-01", "VD-02"], "others": 1},
                           {"date": d2.isoformat(), "device": "HV00149-SAMPLE49THIN", "first_local": "09:00", "last_local": "09:00",
                            "by": "minute", "day_only": False, "desktops": ["VD-01"], "others": 0}]
    dev = sv.search("SAMPLE48", NOW)["devices"]
    assert [m["name"] for m in dev] == ["HV00148-SAMPLE48THIN"] and dev[0]["people"] == 2 and dev[0]["days"] == 2
    assert [r["date"] for r in dev[0]["rows"]] == [d1.isoformat(), old.isoformat()]  # latest first
    assert [p["user"] for p in dev[0]["rows"][0]["people"]] == ["CORP\\Ann", "bob"]  # in the order they came
    assert dev[0]["rows"][1] == {"date": old.isoformat(), "day_only": True, "by": "day", "first_local": None, "last_local": None,
                                 "people": [], "desktops": []}
    desk = sv.search("vd-0", NOW)["desktops"]
    assert [m["name"] for m in desk] == ["VD-01", "VD-02"]  # VD-01 last seen yesterday too: then by name
    assert desk[0]["devices"] == 2 and desk[0]["people"] == 2 and [r["device"] for r in desk[0]["rows"]] == \
        ["HV00148-SAMPLE48THIN", "HV00149-SAMPLE49THIN"]
    # an exact name first; then the latest heard
    assert [m["name"] for m in sv.search("sample", NOW)["devices"]][0] == "HV00150-SAMPLE50THIN"
    # AD found "Ann Archer" as the account ann: her days though no username has the words typed
    named = sv.search("archer", NOW, ["ann"])
    assert [m["name"] for m in named["people"]] == ["CORP\\Ann"]
    assert got["records_since"] == d2.isoformat() and got["keep_days"] == secureview.KEEP_DAYS


def test_search_lists_at_most_search_max_of_a_kind(tmp_path, monkeypatch):
    from uxcorrelate import secureview
    monkeypatch.setattr(secureview, "SEARCH_MAX", 2)
    sv = SecureView(MATCH, ZONE, tmp_path)
    hour_rows(sv, [row(at(date(2026, 11, 17), 8, i), f"HV0014{i}-SAMPLE4{i}THIN", f"user{i}") for i in range(5)])
    got = sv.search("user", NOW)
    assert len(got["people"]) == 2 and got["more"]["people"] == 3 and got["max"] == 2


def test_the_search_route_reads_ad_only_when_no_username_has_it(monkeypatch, tmp_path):
    monkeypatch.setenv(web.ADMIN_ENV, "s3cret-test")
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    app = web.create_app()
    https = TestClient(app, base_url="https://testserver")
    assert https.get("/admin/api/secureview/search?q=ann").status_code == 401
    assert https.post("/admin/api/login", json={"password": "s3cret-test"}).status_code == 200
    assert https.get("/admin/api/secureview/search?q=a").status_code == 400
    asked = []
    app.state.ad.named = lambda text, now: asked.append(text) or {"state": "ok", "why": "", "people": [
        {"sam": "ann", "found": True, "name": "Ann Archer", "department": "Finance", "title": None, "office": None, "disabled": False}]}
    import re
    sv = app.state.secureview
    sv.match, sv.rx = MATCH, re.compile(MATCH, re.IGNORECASE)  # as secureview.match in config.yaml
    hour_rows(sv, [row(datetime.now(timezone.utc) - timedelta(hours=2), "HV00148-SAMPLE48THIN", "CORP\\Ann")])
    app.state.names.add([{"USERNAME": "CORP\\Ann", "DEVICE_NAME": "VD-01"}], {"username": "USERNAME", "device": "DEVICE_NAME"},
                        datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc))
    got = https.get("/admin/api/secureview/search?q=ann").json()
    assert [m["name"] for m in got["people"]] == ["CORP\\Ann"] and got["ad"] is None and not asked  # a username has it
    got = https.get("/admin/api/secureview/search?q=Ann%20Archer").json()
    assert asked == ["Ann Archer"] and [m["name"] for m in got["people"]] == ["CORP\\Ann"]
    assert got["ad"]["people"][0]["aternity"] == "CORP\\Ann"  # whom Actions can read, as Aternity writes them
    assert https.get("/admin/api/secureview/search?q=corp%5Cann").json()["known"] == "CORP\\Ann"
    page = (web.STATIC / "admin.html").read_text(encoding="utf-8")
    assert '["search", "Search"]' in page and 'fetch("admin/api/secureview/search?q="' in page
    assert 'data-sq="${esc(user)}"' in page  # a username on Today or History opens Search with it


T48 = "HV00148-SAMPLE48THIN"


def test_aternitys_hourly_data_gives_the_hour_and_never_moves_a_time_seen(tmp_path):
    today = date(2026, 11, 17)
    d1, d2, d3 = today - timedelta(days=1), today - timedelta(days=2), today - timedelta(days=7)
    sv = SecureView(MATCH, ZONE, tmp_path)
    # SecureView's own records of d1 (part of the day): ann 08:05 to 14:10
    hour_rows(sv, [row(at(d1, 8, 5), T48, "CORP\\Ann", "VD-01"), row(at(d1, 14, 10), T48, "CORP\\Ann", "VD-01")])
    hours = {at(d1, 8): [(T48, "CORP\\Ann", "VD-01")], at(d1, 15): [(T48, "CORP\\Ann", "VD-02")],
             at(d1, 11): [(T48, "bob", "VD-01"), ("WTC0037", "zed", "VD-09")],
             at(d2, 9): [(T48, "CORP\\Ann", "VD-01")], at(d2, 17): [(T48, "CORP\\Ann", "VD-01")]}
    days = {datetime(d3.year, d3.month, d3.day, tzinfo=timezone.utc): [(T48, "carol", "VD-03")]}
    sv.backfill(client_of(Daily(days, hours=hours)), NOW)
    x = sv.days[d1]["devices"][T48]
    assert x["first"] == at(d1, 8, 5).timestamp() and x["back"]["by"] == "hour"  # its own minute is kept
    assert x["desktops"] == {"VD-01", "VD-02"} and "WTC0037" not in sv.days[d1]["devices"]
    ann = sv.search("ann", NOW)["people"][0]["rows"]
    assert [(r["date"], r["first_local"], r["last_local"], r["by"]) for r in ann] == [
        (d1.isoformat(), "08:05", "15:xx", "hour"),  # 08:05: the records' minute, in the hour Aternity has; 15:xx: later
        (d2.isoformat(), "09:xx", "17:xx", "hour")]
    dev = sv.search("sample48", NOW)["devices"][0]
    assert [(r["date"], r["first_local"], r["last_local"], r["day_only"]) for r in dev["rows"]] == [
        (d1.isoformat(), "08:05", "15:xx", False), (d2.isoformat(), "09:xx", "17:xx", False), (d3.isoformat(), None, None, True)]
    assert dev["rows"][0]["people"] == [{"user": "CORP\\Ann", "first_local": "08:05", "last_local": "15:xx"},
                                        {"user": "bob", "first_local": "11:xx", "last_local": "11:xx"}]
    assert dev["rows"][2]["people"] == [{"user": "carol", "first_local": None, "last_local": None}]  # its daily data
    assert sv.search("vd-03", NOW)["desktops"][0]["rows"][0]["people"][0]["user"] == "carol"
    assert sv.daily["hour"] == {d1, d2} and sv.daily["read"] == {d1, d2, d3}
    note = sv.view(NOW)["history"]["daily"]["note"]
    assert f"Days before the records, from Aternity: 3, back to {d3:%a %b %d}." in note
    assert f"2 with each person's first and last hour (its hourly data, back to {d2:%a %b %d})" in note
    assert "1 day only, with the people and desktops (its daily data)." in note
    last = {r["name"]: r for r in sv.view(NOW)["history"]["devices"]}[T48]
    assert (last["person"], last["person_day"], last["person_first"], last["person_last"]) == ("CORP\\Ann", d1.isoformat(), "08:05", "15:xx")
    detail = [q for q in sv.device(T48, NOW)["squares"] if q["heard"]]
    assert [(q["by"], q["first_local"]) for q in detail] == [("day", None), ("hour", "09:xx"), ("hour", "08:05")]
    # saved and read back
    sv.save(NOW)
    again = SecureView(MATCH, ZONE, tmp_path)
    assert again.search("ann", NOW)["people"][0]["rows"] == sv.search("ann", NOW)["people"][0]["rows"]
    assert again.daily["hour"] == {d1, d2}


def test_an_hourly_read_cut_short_is_read_hour_by_hour(tmp_path):
    d1 = date(2026, 11, 16)
    fake = Daily({}, hours={at(d1, 8): [(T48, "ann", "VD-01")], at(d1, 9): [(T48, "ann", "VD-01")]}, page_hours=1)
    sv = SecureView(MATCH, ZONE, tmp_path)
    sv.backfill(client_of(fake, max_pages=1), NOW)
    assert sv.days[d1]["devices"][T48]["back"]["h"] == [at(d1, 8).timestamp(), at(d1, 9).timestamp()]
    # every 6-hour read came back cut short: each hour on its own, on the two days tried (the second has no row)
    assert sum(1 for lo, hi in fake.hourly_asked if hi - lo == timedelta(hours=1)) == 48


def test_no_hourly_data_or_none_naming_a_device_reads_the_daily(tmp_path):
    d1 = date(2026, 11, 16)
    days = {datetime(2026, 11, 16, tzinfo=timezone.utc): [(T48, "ann", "VD-01")]}
    gone = Daily(days, hour_fail=True)
    sv = SecureView(MATCH, ZONE, tmp_path)
    sv.backfill(client_of(gone), NOW)
    assert len(gone.hourly_asked) == 1 and sv.daily["hour"] == set() and d1 in sv.daily["read"]
    assert "None with the hour (its hourly data: Mon Nov 16 could not be read" in sv.daily["note"]
    unnamed = Daily(days, hours={at(d1, 9): [(None, "ann", "PC-07")]})
    sv = SecureView(MATCH, ZONE, tmp_path / "b")
    sv.backfill(client_of(unnamed), NOW)
    assert sv.daily["hour"] == set() and sv.days[d1]["devices"][T48]["back"]["people"] == {"ann": ["ann", None, None]}
    assert "names no device people sat at on Mon Nov 16" in sv.daily["note"]


def test_days_read_before_the_people_were_asked_for_are_read_again(tmp_path):
    (tmp_path / "secureview").mkdir()
    (tmp_path / "secureview" / "daily.json").write_text(json.dumps({"read": ["2026-11-10"], "state": "ok", "note": "x", "at": 1}))
    assert SecureView(MATCH, ZONE, tmp_path).daily["read"] == set()


def test_the_hourly_source_names_are_riverbeds_own():
    from pathlib import Path
    from uxcorrelate.config import SECUREVIEW_HOURLY
    from uxcorrelate.verify import parse_odata_metadata
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
    assert {v for k, v in SECUREVIEW_HOURLY.items() if k != "entity"} <= schema[SECUREVIEW_HOURLY["entity"]]
