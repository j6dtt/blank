"""SecureView (secureview.py): the devices people sat at whose name matches secureview.match,
first and last heard per local day, people with their own times, silent 30 / 60 / 90 days, and
Aternity's daily data for the days before the records. Made-up devices and users."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from uxcorrelate import secureview as sv_module
from uxcorrelate import web
from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import AternityConfig, Settings, config_notes, load_settings
from uxcorrelate.secureview import People, SecureView, keep_backfilling
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
    sv.save(now)  # as the Today reader, at the end of each read (the feed's numbers are given there)


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


# -- the feed for another platform (the user, Nov 24-25 2026: "I want to pull regularly (small interval)
#    throughout the day. ONLY NEW DEVICE(S) RETURN(S) each call"; "This reset daily correct?"; "add first
#    heard timestamp"; AD's name and department; "Flatten people array, why would desktop be an array?";
#    "on demand, build it") -------------------------------------------------------------------------------
D17 = date(2026, 11, 17)
A, B, C = "HV00041-HOME41THIN", "HV00042-HOME42THIN", "HV00043-HOME43THINE"
A1, B1, C1 = "HV00041", "HV00042", "HV00043"  # as the feed gives them: up to the dash (feed_name)
AD = {"ann": {"found": True, "name": "Ann Archer", "title": "Clerk", "department": "Finance", "office": "B2",
              "disabled": False},
      "bob": {"found": True, "name": "Bob Baker", "title": None, "department": "Operations", "office": None,
              "disabled": False}}


class Directory:
    """ad.Lookup.look for the feed, made-up: answers from AD above, a name it has not is "not in AD";
    `state` down / off / refused answers nobody."""
    def __init__(self, state="ok"):
        self.state, self.asked = state, []

    def __call__(self, users, now):
        self.asked.append(list(users))
        got = {u: AD.get(u.lower(), {"found": False}) for u in users} if self.state == "ok" else {}
        return {"state": self.state, "why": "", "users": got}


def reader(read_ok=NOW, ready=True, as_of=None, refresh=5):
    """What the feed reads of the Today reader."""
    from types import SimpleNamespace
    return SimpleNamespace(ready=ready, off=None, status="Reading Aternity's activity: 3 of 25 hours read",
                           read_ok=read_ok, as_of=as_of or read_ok, refresh_min=refresh)


def feed_of(tmp_path, state="ok"):
    sv = SecureView(MATCH, ZONE, tmp_path)
    ad = Directory(state)
    return sv, People(sv, ad), ad


def names(reply):
    return [x["device"] for x in reply["devices"]]


def test_the_feed_gives_a_device_once_on_each_day_it_is_heard_in_the_order_first_heard(tmp_path):
    sv, people, _ = feed_of(tmp_path)
    hour_rows(sv, [row(at(D17, 8, 10), B, "bob", "VD-02"), row(at(D17, 8, 2), A, "ann", "VD-01"),
                   row(at(D17, 8, 20), "WTC0037", "cat")])  # WTC0037 is no SecureView device
    people.ask(NOW)
    first = people.reply(reader(), None, NOW)  # no number sent: today's devices so far
    assert first["devices"] == [
        {"device": A1, "day": "2026-11-17", "first_heard": "2026-11-17T13:02:00Z", "username": "ann",
         "name": "Ann Archer", "department": "Finance", "in_ad": True, "desktop": "VD-01"},
        {"device": B1, "day": "2026-11-17", "first_heard": "2026-11-17T13:10:00Z", "username": "bob",
         "name": "Bob Baker", "department": "Operations", "in_ad": True, "desktop": "VD-02"}]
    assert (first["ready"], first["as_of"], first["refresh_minutes"], first["note"], first["ad"], first["more"]) == \
        (True, "2026-11-17T21:45:00Z", 5, None, "ok", False)
    assert people.reply(reader(), first["next"], NOW)["devices"] == []  # nothing new: nothing, the same number
    assert people.reply(reader(), first["next"], NOW)["next"] == first["next"]
    # the next read: A again, and a second person on it (not new), C new
    hour_rows(sv, [row(at(D17, 9, 30), A, "ann", "VD-01"), row(at(D17, 9, 31), A, "dan", "VD-07"),
                   row(at(D17, 9, 5), C, "cy", "VD-03")])
    people.ask(NOW)
    second = people.reply(reader(), first["next"], NOW)
    assert names(second) == [C1] and second["next"] == first["next"] + 1
    assert second["devices"][0] | {"device": 0} == {"device": 0, "day": "2026-11-17", "first_heard": "2026-11-17T14:05:00Z",
                                                   "username": "cy", "name": None, "department": None, "in_ad": False,
                                                   "desktop": "VD-03"}  # cy is not in AD: said, not left out
    # the next day everything starts again: A is new for Wednesday (the number goes on, it never starts again)
    d18, now18 = date(2026, 11, 18), NOW + timedelta(days=1)
    hour_rows(sv, [row(at(d18, 8, 15), A, "ann", "VD-01")], now=now18)
    people.ask(now18)
    third = people.reply(reader(now18), second["next"], now18)
    assert [(x["device"], x["day"], x["first_heard"]) for x in third["devices"]] == [(A1, "2026-11-18", "2026-11-18T13:15:00Z")]
    assert third["next"] == second["next"] + 1
    assert names(people.reply(reader(now18), None, now18)) == [A1]  # with no number: that day's only


def test_the_feeds_person_and_desktop_are_those_of_the_first_row_that_names_a_person(tmp_path):
    sv, people, _ = feed_of(tmp_path)
    hour_rows(sv, [row(at(D17, 8, 20), A, "ann", "VD-01"), row(at(D17, 8, 0), A, None, "VD-09"),
                   row(at(D17, 8, 5), A, "bob", "VD-02")])
    people.ask(NOW)
    [x] = people.reply(reader(), None, NOW)["devices"]
    assert (x["first_heard"], x["username"], x["desktop"]) == ("2026-11-17T13:00:00Z", "bob", "VD-02")
    # a row Aternity sends late, earlier than bob's: the person moves, the device is not given again
    n = people.reply(reader(), None, NOW)["next"]
    hour_rows(sv, [row(at(D17, 7, 58), A, "cy", "VD-03")])
    assert people.reply(reader(), n, NOW)["devices"] == []
    assert people.reply(reader(), None, NOW)["devices"] == []  # asked from the start: cy is asked of AD now
    people.ask(NOW)
    [x] = people.reply(reader(), None, NOW)["devices"]
    assert (x["first_heard"], x["username"], x["desktop"]) == ("2026-11-17T12:58:00Z", "cy", "VD-03")
    # a device nobody is named on: no person, nothing asked of AD, given at once
    hour_rows(sv, [row(at(D17, 9, 0), B, None, None)])
    [y] = people.reply(reader(), n, NOW)["devices"]
    assert (y["device"], y["username"], y["name"], y["in_ad"], y["desktop"]) == (B1, None, None, None, None)


def test_a_late_row_of_yesterday_is_given_with_its_own_day_and_aternitys_own_days_are_not_in_the_feed(tmp_path):
    sv, people, _ = feed_of(tmp_path)
    d16 = date(2026, 11, 16)
    hour_rows(sv, [row(at(D17, 8, 2), A, "ann")])
    people.ask(NOW)
    n = people.reply(reader(), None, NOW)["next"]
    hour_rows(sv, [row(at(d16, 23, 58), B, "bob", "VD-02")])  # read after midnight: it is Monday's
    people.ask(NOW)
    got = people.reply(reader(), n, NOW)
    assert [(x["device"], x["day"]) for x in got["devices"]] == [(B1, "2026-11-16")]
    assert names(people.reply(reader(), None, NOW)) == [A1]  # with no number: today's, not Monday's
    # a day the app filled in from Aternity's own data (the backfill) has no number: not in the feed
    old = NOW - timedelta(days=40)
    fake = Daily({datetime(old.year, old.month, old.day, tzinfo=timezone.utc): ["HV00148-SAMPLE48THIN"]})
    sv.backfill(client_of(fake), NOW)
    assert any("HV00148-SAMPLE48THIN" in d["devices"] for d in sv.days.values())
    assert people.reply(reader(), got["next"], NOW)["devices"] == [] and len(sv.feed) == 2


def test_the_feeds_numbers_are_saved_so_a_restart_gives_nothing_twice_and_loses_nothing(tmp_path):
    sv, people, _ = feed_of(tmp_path)
    hour_rows(sv, [row(at(D17, 8, 2), A, "ann"), row(at(D17, 8, 10), B, "bob", "VD-02")])
    people.ask(NOW)
    n = people.reply(reader(), None, NOW)["next"]
    hour_rows(sv, [row(at(D17, 9, 5), C, "cy", "VD-03")])  # heard, not asked for before the restart
    sv.save(NOW)
    again, people2, _ = feed_of(tmp_path)  # a restart
    assert (again.feed_n, again.feed_base, len(again.feed)) == (sv.feed_n, sv.feed_base, 3)
    # the restart reads the last 24 hours again: the same rows give no new number
    hour_rows(again, [row(at(D17, 8, 2), A, "ann"), row(at(D17, 8, 10), B, "bob", "VD-02"), row(at(D17, 9, 5), C, "cy", "VD-03")])
    assert again.feed_n == sv.feed_n
    people2.ask(NOW)
    got = people2.reply(reader(), n, NOW)
    assert names(got) == [C1] and got["next"] == n + 1
    # the state file written after the days: a stop between the two loses no number
    (tmp_path / "secureview" / "daily.json").unlink()
    third = SecureView(MATCH, ZONE, tmp_path)
    assert third.feed_n == sv.feed_n and third.feed_base <= sv.feed_n - 3
    hour_rows(third, [row(at(D17, 10, 0), "HV00044-HOME44THIN", "ann")])
    assert third.feed[-1][0] == sv.feed_n + 1


def test_a_number_that_is_not_this_feeds_starts_again_with_todays_devices_and_says_so(tmp_path):
    sv, people, _ = feed_of(tmp_path)
    hour_rows(sv, [row(at(D17, 8, 2), A, "ann")])
    people.ask(NOW)
    n = people.reply(reader(), None, NOW)["next"]
    assert n == sv.feed_base + 1  # numbers start at the second the store was first used, then go up by one
    for wrong in (n + 50, sv.feed_base - 1, 0, -3):  # ahead of the feed, or from before its numbers began
        got = people.reply(reader(), wrong, NOW)
        assert names(got) == [A1] and got["next"] == n and "is not one this feed gave" in got["note"]
    assert people.reply(reader(), sv.feed_base, NOW)["note"] is None  # where its numbers start is its own
    # a store made again later (the data folder replaced) starts above every number the old one gave
    newer = SecureView(MATCH, ZONE, tmp_path / "replaced")
    assert newer.feed_base >= sv.feed_base


def test_a_reply_holds_feed_max_devices_and_says_more(tmp_path, monkeypatch):
    monkeypatch.setattr("uxcorrelate.secureview.FEED_MAX", 2)
    sv, people, _ = feed_of(tmp_path)
    hour_rows(sv, [row(at(D17, 8, k), f"HV0005{k}-HOME5{k}THIN", "ann") for k in range(5)])
    people.ask(NOW)
    one = people.reply(reader(), None, NOW)
    assert (len(one["devices"]), one["more"]) == (2, True)
    two = people.reply(reader(), one["next"], NOW)
    three = people.reply(reader(), two["next"], NOW)
    assert (len(two["devices"]), two["more"], len(three["devices"]), three["more"]) == (2, True, 1, False)
    assert names(one) + names(two) + names(three) == [f"HV0005{k}" for k in range(5)]


def test_while_ad_does_not_answer_the_last_answer_is_used_and_a_new_person_waits_15_minutes_at_most(tmp_path):
    sv, people, ad = feed_of(tmp_path)
    hour_rows(sv, [row(at(D17, 8, 2), A, "ann")])
    people.ask(NOW)
    n = people.reply(reader(), None, NOW)["next"]
    ad.state = "down"
    # ann (answered before) on another device, then bob (never answered), then ann again
    hour_rows(sv, [row(at(D17, 9, 0), B, "ann"), row(at(D17, 9, 5), C, "bob", "VD-02"),
                   row(at(D17, 9, 9), "HV00044-HOME44THIN", "Ann")])
    people.ask(NOW)
    got = people.reply(reader(), n, NOW)
    assert names(got) == [B1] and got["devices"][0]["name"] == "Ann Archer" and got["ad"] == "down"
    assert got["next"] == n + 1 and "wait for Active Directory's answer (15 min at most)" in got["note"]
    soon = NOW + timedelta(minutes=14)
    people.ask(soon)
    assert people.reply(reader(soon), got["next"], soon)["devices"] == []  # still waiting, nothing skipped
    late = NOW + timedelta(minutes=16)
    people.ask(late)
    gone = people.reply(reader(late), got["next"], late)
    assert [(x["device"], x["username"], x["name"], x["in_ad"]) for x in gone["devices"]] == [
        (C1, "bob", None, None), ("HV00044", "Ann", "Ann Archer", True)]
    assert "did not answer in 15 min: 1 of these devices" in gone["note"]
    # asked for again with the old number: bob's device does not start a second wait
    assert names(people.reply(reader(late), got["next"], late)) == [C1, "HV00044"]
    # AD answers again: a device bob is heard on after that is asked for and given with his name
    ad.state = "ok"
    hour_rows(sv, [row(at(D17, 10, 0), "HV00045-HOME45THIN", "bob", "VD-02")], now=late)
    people.ask(late)
    back = people.reply(reader(late), gone["next"], late)
    assert [(x["device"], x["name"], x["department"]) for x in back["devices"]] == [("HV00045", "Bob Baker", "Operations")]
    assert back["ad"] == "ok" and back["note"] is None


def test_after_a_restart_todays_people_are_asked_for_at_once_and_a_caller_behind_gets_all_its_devices_together(tmp_path):
    """A restart loses the answers held. Today's devices are what a first call brings, so their
    people are asked for at the start. A caller that is behind asks for older devices: their people
    are asked for at its call, all of them, and the devices come together at its next one (asked for
    one at a time, it got one device a call: found on dev after a restart, Nov 25 2026)."""
    d16 = date(2026, 11, 16)
    sv, people, ad = feed_of(tmp_path)
    n0 = sv.feed_n
    two_hours_ago = NOW - timedelta(hours=2)
    hour_rows(sv, [row(at(d16, 20, 1), A, "ann"), row(at(d16, 20, 2), B, "bob", "VD-02"), row(at(d16, 20, 3), C, "cy", "VD-03"),
                   row(at(D17, 8, 2), "HV00044-HOME44THIN", "ann")], now=two_hours_ago)
    sv2, people2, ad2 = feed_of(tmp_path)  # the restart: nothing held
    people2.ask(NOW)
    assert ad2.asked == [["ann"]]  # today's device's person, though numbered two hours ago
    assert names(people2.reply(reader(), None, NOW)) == ["HV00044"]  # so the first call brings it at once
    people2.known.clear()  # as if only yesterday's people were wanted: none of the three is held
    got = people2.reply(reader(), n0, NOW)
    assert got["devices"] == [] and got["next"] == n0 and "wait for Active Directory's answer" in got["note"]
    assert sv2.fresh.is_set() and sorted(people2.pending) == ["ann", "bob", "cy"]  # all three asked for, not the first
    people2.ask(NOW)
    assert names(people2.reply(reader(), n0, NOW)) == [A1, B1, C1, "HV00044"]


def test_with_no_ad_account_or_a_refused_login_devices_go_at_once_without_name_and_department(tmp_path):
    for state, said in (("off", None), ("refused", "refuses the app's login")):
        sv, people, _ = feed_of(tmp_path / state, state)
        hour_rows(sv, [row(at(D17, 8, 2), A, "ann")])
        people.ask(NOW)
        got = people.reply(reader(), None, NOW)
        assert [(x["username"], x["name"], x["department"], x["in_ad"]) for x in got["devices"]] == [("ann", None, None, None)]
        assert got["ad"] == state and (said in got["note"] if said else got["note"] is None)


def test_the_feed_says_when_it_is_not_ready_or_a_read_of_aternity_failed(tmp_path):
    sv, people, _ = feed_of(tmp_path)
    hour_rows(sv, [row(at(D17, 8, 2), A, "ann")])
    people.ask(NOW)
    got = people.reply(reader(ready=False), 7, NOW)  # the first read after a restart is not done
    assert (got["ready"], got["devices"], got["next"]) == (False, [], 7) and got["note"].startswith("Reading Aternity")
    assert people.reply(reader(read_ok=None), None, NOW)["ready"] is False
    failed = people.reply(reader(NOW - timedelta(minutes=5), as_of=NOW), None, NOW)
    assert failed["ready"] and names(failed) == [A1] and "could not be read in full at 21:45 UTC" in failed["note"]
    stale = people.reply(reader(NOW - timedelta(minutes=16)), None, NOW)
    assert "No read of Aternity since 21:29 UTC (reads are every 5 min)" in stale["note"]
    off = People(SecureView("", ZONE, tmp_path / "off"), Directory())
    assert off.reply(reader(), None, NOW)["note"] == "SecureView is off: secureview.match in config.yaml is empty."


def test_the_feed_needs_its_own_key_over_https(monkeypatch, tmp_path):
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    monkeypatch.delenv(web.FEED_ENV, raising=False)
    app = web.create_app()
    http, https = TestClient(app), TestClient(app, base_url="https://testserver")
    url, key = "/api/MSS/secureview/new", {web.FEED_HEADER: "feed-key-of-the-test"}
    assert https.get(url, headers=key).status_code == 503  # no key set: the feed is off
    monkeypatch.setenv(web.FEED_ENV, "feed-key-of-the-test")
    assert http.get(url, headers=key).status_code == 400  # plain HTTP: the key could be read on the network
    for _ in range(web.FEED_TRIES + 2):  # no key sent is not a wrong try: it never locks
        assert https.get(url).status_code == 401
    got = https.get(url, headers=key)
    assert got.status_code == 200 and set(got.json()) == {"ready", "as_of", "refresh_minutes", "note", "ad", "devices",
                                                        "more", "next"}
    assert got.json()["ready"] is False and got.json()["devices"] == []  # no secureview.match in this config
    assert https.get(url + "?after=abc", headers=key).status_code == 400
    assert https.get(url + "?after=12", headers=key).json()["next"] == 12
    wrong = {web.FEED_HEADER: "not-the-key"}
    assert [https.get(url, headers=wrong).status_code for _ in range(web.FEED_TRIES)] == [401] * (web.FEED_TRIES - 1) + [423]
    assert https.get(url, headers=key).status_code == 423  # locked: the right key too, FEED_LOCK_MIN
    feed = app.state.usage.view(datetime.now(timezone.utc))["feed"]
    assert (feed["n"], feed["devices"], feed["from"]) == (2, 0, "testclient")  # the two calls that were answered
    page = (web.STATIC / "admin.html").read_text(encoding="utf-8")
    assert "The other platform's calls for SecureView's new devices" in page and "j.feed" in page


def test_a_number_is_given_only_once_it_is_saved(tmp_path, monkeypatch):
    """A number given and then lost in a stop would go to another device after the restart: one
    device given twice, one never. So a device is held back until its day is on disk."""
    sv, people, _ = feed_of(tmp_path)
    hour_rows(sv, [row(at(D17, 8, 2), A, "ann")])  # saved at once, before any caller can ask
    assert json.loads((tmp_path / "secureview" / "2026-11-17.json").read_text())["devices"][A]["n"] == sv.feed_n
    people.ask(NOW)
    n = people.reply(reader(), None, NOW)["next"]
    real = sv_module.os.replace

    def full(*a):
        raise OSError("No space left on device")
    monkeypatch.setattr(sv_module.os, "replace", full)
    hour_rows(sv, [row(at(D17, 9, 5), B, "bob", "VD-02")])
    people.ask(NOW)
    got = people.reply(reader(), n, NOW)
    assert got["devices"] == [] and got["next"] == n and "1 new device held back" in got["note"]
    monkeypatch.setattr(sv_module.os, "replace", real)
    sv.save(NOW)  # the reader's own save after its next read
    got = people.reply(reader(), n, NOW)
    assert names(got) == [B1] and got["note"] is None


def test_verify_feed_asks_the_running_app_as_the_other_platform_would(monkeypatch, tmp_path):
    """`verify --feed`: no key, plain HTTP, with the key, then again with the number given. Counts
    only: no device or user name is printed."""
    from urllib.parse import urlsplit

    from uxcorrelate.verify import verify_feed
    path = tmp_path / "config.yaml"
    path.write_text(f"secureview:\n  match: '{MATCH}'\n")
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    monkeypatch.delenv("AD_USERNAME", raising=False)
    monkeypatch.setenv(web.FEED_ENV, "feed-key-of-the-test-0123")
    settings = load_settings(str(path))
    app = web.create_app(str(path))
    clients = {"http": TestClient(app), "https": TestClient(app, base_url="https://testserver")}

    def get(url, headers):
        u = urlsplit(url)
        if u.port == 8443:  # this app answers on Kubernetes' port only: the first one tried does not
            raise OSError("Connection refused")
        return clients[u.scheme].get(u.path + ("?" + u.query if u.query else ""), headers=headers)

    env = {web.FEED_ENV: "feed-key-of-the-test-0123"}
    assert "is not set in .env" in verify_feed(settings, env={}, get=get)[0].message
    not_ready = verify_feed(settings, env=env, get=get)
    assert [c.ok for c in not_ready] == [True, True, None] and "not ready yet" in not_ready[-1].message
    now = datetime.now(timezone.utc)
    sv, today = app.state.secureview, app.state.today
    sv.add_hour(now.replace(minute=0, second=0, microsecond=0),
                [row(now, A, "ann"), row(now, B, None, None), row(now, "WTC0037", "cat")], F, now)
    sv.save(now)
    today.ready, today.read_ok, today.as_of = True, now, now
    app.state.sv_people.ask(now)  # no AD account here: devices go at once, without name and department
    checks = verify_feed(settings, env=env, get=get)
    by = {c.item: c for c in checks}
    assert [c.ok for c in checks] == [True, True, True, True]
    assert by["with the key"].message.startswith("2 devices first heard today so far, each given once; next=")
    assert "With a person: 1; in AD: 0; not in AD: 0; no answer from AD: 1; with a desktop: 1. Active Directory: off." \
        in by["with the key"].message
    assert by["with the key"].message.endswith("A device's name is given up to its dash: no two of them have the same name.")
    assert by["asked again with its number"].message.startswith("0 devices: nothing new, none of them given before")
    text = " ".join(c.message + " ".join(c.rows) for c in checks)
    assert A not in text and A1 not in text and "ann" not in text and "feed-key" not in text  # no name, never the key
    short = verify_feed(settings, env={web.FEED_ENV: "abc"}, get=get)
    assert "has 3 characters: use 20 or more" in short[0].message and short[-1].ok is False  # the app has another key
    assert verify_feed(Settings(), env=env, get=get)[0].item == "SecureView"  # no secureview.match: off


def test_a_day_heard_before_the_feed_existed_gets_no_number_but_todays(tmp_path):
    """The copy that brings the feed: the restart reads the last 24 hours again. Today's devices,
    heard before it, are to come at the first call; yesterday evening's are not new."""
    d16 = date(2026, 11, 16)
    old = SecureView(MATCH, ZONE, tmp_path)
    hour_rows(old, [row(at(d16, 20, 0), A, "ann"), row(at(D17, 8, 2), A, "ann"), row(at(D17, 8, 10), B, "bob", "VD-02")])
    old.save(NOW)
    for f in (tmp_path / "secureview").glob("2026-*.json"):  # as the build before wrote them: no number, no first row
        raw = json.loads(f.read_text())
        for x in raw["devices"].values():
            for k in ("n", "at", "who"):
                x.pop(k, None)
        f.write_text(json.dumps(raw))
    (tmp_path / "secureview" / "daily.json").unlink()
    sv, people, _ = feed_of(tmp_path)
    assert sv.feed == [] and people.reply(reader(), None, NOW)["devices"] == []
    hour_rows(sv, [row(at(d16, 20, 0), A, "ann"), row(at(D17, 8, 2), A, "ann"), row(at(D17, 8, 10), B, "bob", "VD-02"),
                   row(at(d16, 23, 50), C, "cy", "VD-03")])  # the same rows again, and one of Monday not heard before
    assert [(d.isoformat(), name) for _, d, name in sv.feed] == [("2026-11-16", C), ("2026-11-17", A), ("2026-11-17", B)]
    people.ask(NOW)
    got = people.reply(reader(), None, NOW)
    assert [(x["device"], x["username"], x["desktop"]) for x in got["devices"]] == [(A1, "ann", "VD-01"), (B1, "bob", "VD-02")]
    assert people.reply(reader(), got["next"], NOW)["devices"] == []


def test_one_reads_devices_are_numbered_in_the_order_first_heard_whatever_the_order_of_its_hours(tmp_path):
    """After a restart the reader reads the last 24 hours newest first: the numbers are given at the
    end of the read, not hour by hour."""
    sv, people, _ = feed_of(tmp_path)
    nine, eight = at(D17, 9), at(D17, 8)
    sv.add_hour(nine, [row(at(D17, 9, 40), B, "bob", "VD-02"), row(at(D17, 9, 20), A, "ann")], F, NOW)
    assert sv.feed == [] and people.reply(reader(), None, NOW)["devices"] == []  # the read is not over
    sv.add_hour(eight, [row(at(D17, 8, 30), C, "cy", "VD-03"), row(at(D17, 8, 5), B, "bob", "VD-02")], F, NOW)
    sv.save(NOW)
    people.ask(NOW)
    got = people.reply(reader(), None, NOW)
    assert [(x["device"], x["first_heard"][11:16]) for x in got["devices"]] == [(B1, "13:05"), (C1, "13:30"), (A1, "14:20")]


def test_the_feed_gives_a_devices_name_up_to_its_dash_and_a_name_with_no_dash_whole(tmp_path):
    """The user, Nov 25 2026: "Remove everythin after the dash (-) in 'device' value when return
    data", "add error trap in case there's no -"."""
    from uxcorrelate.secureview import feed_name
    assert feed_name("HV00045-SAMPLE45THINVM") == "HV00045" and feed_name("hv00047-sample47thin") == "hv00047"
    assert feed_name("HV00045-A-THIN-2") == "HV00045" and feed_name("HV00045 -THIN") == "HV00045"  # the first dash
    # no dash, or nothing before it: the whole name, never an error, never an empty one
    assert [feed_name(x) for x in ("THINCLIENT9", "-THIN", "", None, 7)] == ["THINCLIENT9", "-THIN", "", "None", "7"]
    # only the reply is cut: with a pattern that takes names with no dash, and two whole names with one first part
    sv = SecureView("THIN", ZONE, tmp_path)
    people = People(sv, Directory())
    hour_rows(sv, [row(at(D17, 8, 1), "THINCLIENT9", "ann"), row(at(D17, 8, 2), "HV00041-HOME41THIN", "ann"),
                   row(at(D17, 8, 3), "HV00041-HOME41THINVM", "bob", "VD-02")])
    people.ask(NOW)
    got = people.reply(reader(), None, NOW)
    assert [(x["device"], x["username"]) for x in got["devices"]] == [("THINCLIENT9", "ann"), ("HV00041", "ann"), ("HV00041", "bob")]
    assert set(sv.days[D17]["devices"]) == {"THINCLIENT9", "HV00041-HOME41THIN", "HV00041-HOME41THINVM"}  # kept whole
    assert people.reply(reader(), got["next"], NOW)["devices"] == []
