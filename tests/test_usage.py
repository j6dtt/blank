"""The admin page (usage.py, web.py /admin): who used the app, found by the caller's address in
Aternity's activity; what they did; the calls made to Riverbed; the login. Made-up users, PCs
and addresses (TEST-NET)."""

import json
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from uxcorrelate import http as _http
from uxcorrelate import usage as usage_mod
from uxcorrelate import web
from uxcorrelate.config import Settings
from uxcorrelate.http import RestClient
from uxcorrelate.today import HOUR, Today
from uxcorrelate.usage import GIVE_UP, Usage

ZONE = "America/New_York"
F = {"timestamp": "TIMEFRAME", "username": "USERNAME", "device": "DEVICE_NAME", "application": "APPLICATION_NAME",
     "status": "SLA_STATUS", "virtualization": "VIRTUALIZATION", "ip_address": "DEVICE_IP_ADDRESS"}
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)  # Monday 10:00 in New York
VD_IP, PC_IP, SHARED_IP = "192.0.2.11", "192.0.2.12", "192.0.2.50"


def row(t, user, dev, ip, virtual=True):
    return {"TIMEFRAME": t.isoformat(), "USERNAME": user, "DEVICE_NAME": dev, "APPLICATION_NAME": "Mail",
            "SLA_STATUS": "Normal", "VIRTUALIZATION": "true" if virtual else "false", "DEVICE_IP_ADDRESS": ip}


def read(today, rows, now=NOW):
    """The Today reader's read of the current hour (it keeps the rows of watched addresses)."""
    start = today.hour_of(now)
    today.add_hour(start, rows, F, now)


def test_a_visit_is_the_user_aternity_saw_on_its_address(tmp_path):
    today = Today(ZONE)
    use = Usage(today, tmp_path)
    use.seen(VD_IP, "today", NOW - timedelta(minutes=5))
    use.seen(VD_IP, "care", NOW - timedelta(minutes=2))
    assert today.watch == {VD_IP}  # the reader now keeps that address's rows
    read(today, [row(NOW - timedelta(minutes=20), "sample07", "VD-03", VD_IP),
                 row(NOW - timedelta(minutes=3), "sample07", "VD-03", VD_IP),
                 row(NOW - timedelta(minutes=3), "sample09", "PC-004", PC_IP, virtual=False)])  # not watched
    assert set(today.at_address(VD_IP, NOW - HOUR, NOW)) == {"sample07"}
    assert today.at_address(PC_IP, NOW - HOUR, NOW) == {}
    use.read_done(NOW)
    v = use.view(NOW)
    [p] = v["people"]
    assert (p["who"], p["pcs"], p["kind"], p["tabs"], p["tab_now"]) == ("sample07", ["VD-03"], "virtual",
                                                                       ["Today", "Care list"], "Care list")
    assert p["first_local"] == "09:55" and p["last_local"] == "09:58"  # New York
    assert [a["who"] for a in v["active"]] == ["sample07"] and v["note"] is None
    assert v["pages"]["total"] == 2 and v["pages"]["by_hour"][9] == 2


def test_several_users_on_one_address_at_once_are_one_of_them_and_no_row_is_not_known(tmp_path):
    today = Today(ZONE)
    use = Usage(today, tmp_path)
    t = NOW - timedelta(minutes=10)
    use.seen(SHARED_IP, "investigate", t)
    use.seen("192.0.2.99", "today", t)  # no Aternity agent on it
    read(today, [row(t - timedelta(minutes=5), "sample21", "SRV-RDS1", SHARED_IP),
                 row(t + timedelta(minutes=5), "sample21", "SRV-RDS1", SHARED_IP),
                 row(t - timedelta(minutes=8), "sample22", "SRV-RDS1", SHARED_IP),
                 row(t + timedelta(minutes=2), "sample22", "SRV-RDS1", SHARED_IP)])
    use.read_done(NOW)
    people = {p["who"]: p for p in use.view(NOW)["people"]}
    assert people["sample21"]["others"] == ["sample21", "sample22"]  # "one of 2 on it then"
    unknown = people[None]
    assert unknown["addr"] == "192.0.2.99" and unknown["pcs"] == []
    # not known for good only GIVE_UP after the visit (Aternity sends rows late)
    day = use.days[today.day(NOW).isoformat()]
    later = [v for v in day["visits"] if v["a"] == "192.0.2.99"][0]
    assert "p" not in later
    use.read_done(t + GIVE_UP)
    assert later["p"] == {"x": True}


def test_a_late_row_still_places_a_visit_and_a_placed_one_stays(tmp_path):
    today = Today(ZONE)
    use = Usage(today, tmp_path)
    t = NOW - timedelta(minutes=30)
    use.seen(VD_IP, "software", t)
    use.read_done(NOW)  # nothing from that address yet
    assert use.view(NOW)["people"][0]["who"] is None
    read(today, [row(t + timedelta(minutes=20), "sample07", "VD-03", VD_IP)])  # sent late
    use.read_done(NOW)  # within MATCH, not at the visit: for now
    assert use.view(NOW)["people"][0]["who"] == "sample07"
    use.read_done(t + GIVE_UP)  # for good
    read(today, [row(t + timedelta(minutes=50), "sample08", "VD-03", VD_IP)])  # a pooled desktop's next user
    assert use.view(t + GIVE_UP)["people"][0]["who"] == "sample07"


def test_every_visit_from_one_address_is_said_not_guessed(tmp_path):
    today = Today(ZONE)
    use = Usage(today, tmp_path)
    for k in range(usage_mod.ONE_ADDRESS):
        use.seen("198.51.100.1", "today", NOW - timedelta(minutes=k))
    assert "people cannot be told apart" in use.view(NOW)["note"]


def test_a_day_is_saved_read_back_after_a_restart_and_deleted_after_30_days(tmp_path):
    today = Today(ZONE)
    use = Usage(today, tmp_path)
    use.seen(VD_IP, "today", NOW)
    use.investigation(VD_IP, "today", 12.3, {"answer": {"found": [{"issue": "disk"}]}}, now=NOW)
    use.asked(VD_IP, "Which PCs   had a crash?", 4.0, {"steps": [{"title": "Events"}, {"title": "Failed"}],
                                                       "flagged": ["7"]}, now=NOW)
    use.care_change(VD_IP, "add sample30", False, NOW)  # a wrong PIN
    old = tmp_path / "usage" / f"{(today.day(NOW) - timedelta(days=31)).isoformat()}.json"
    old.write_text("{}")
    use.save(NOW, force=True)
    f = tmp_path / "usage" / f"{today.day(NOW).isoformat()}.json"
    assert f.exists()

    again = Usage(Today(ZONE), tmp_path)  # a restart
    v = again.view(NOW + timedelta(days=1, hours=1))  # the next day: the day before is pruned of what is 30 days old
    assert not old.exists()
    v = again.view(NOW + timedelta(days=1), today.day(NOW).isoformat())
    assert v["investigations"]["by"] == [("today", 1)] and v["investigations"]["found"] == [("a failing disk", 1)]
    q = v["ask"]
    assert (q["n"], q["steps"], q["failed_lookups"], q["flagged"]) == (1, 2, 1, 1)
    assert q["questions"][0]["q"] == "Which PCs had a crash?"
    assert v["care"]["wrong_pins"] == 1
    assert today.day(NOW).isoformat() in v["dates"]
    fresh = Usage(Today(ZONE), tmp_path)
    fresh.view(NOW)
    assert fresh.today.watch == {VD_IP}  # today's addresses are watched again after a restart


def test_every_call_to_riverbed_is_counted_under_what_the_app_was_doing(monkeypatch):
    today = Today(ZONE)
    use = Usage(today)
    monkeypatch.setattr(_http, "METER", use.call)

    def handler(request):
        if request.url.path.endswith("/BAD"):
            return httpx.Response(500)
        return httpx.Response(200, json={"value": [{"a": 1}, {"a": 2}, {"a": 3}]})
    client = RestClient("Aternity", "https://aternity.test/aternity.odata/v2.0", retries=1,
                        transport=httpx.MockTransport(handler))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with _http.doing("Today reader"):
        client.get_json("/BUSINESS_ACTIVITIES_RAW")
        with pytest.raises(Exception):
            client.get_json("/BAD")
    _http.as_doing("investigations", lambda: client.get_json("/DEVICE_HEALTH_RAW"))()
    [aternity] = use.view(datetime.now(timezone.utc))["load"]
    assert (aternity["system"], aternity["calls"], aternity["rows"], aternity["failed"], aternity["retries"]) == (
        "Aternity", 3, 6, 1, 1)
    purposes = {p["purpose"]: p for p in aternity["purposes"]}
    assert purposes["Today reader"]["calls"] == 2 and purposes["investigations"]["rows"] == 3
    assert {s["source"] for s in aternity["sources"]} == {"BUSINESS_ACTIVITIES_RAW", "BAD", "DEVICE_HEALTH_RAW"}


def app_with(monkeypatch, password="s3cret-test"):
    if password is None:
        monkeypatch.delenv(web.ADMIN_ENV, raising=False)
    else:
        monkeypatch.setenv(web.ADMIN_ENV, password)
    monkeypatch.delenv("UXCORRELATE_DATA", raising=False)
    return web.create_app()


def test_admin_login_only_over_https_locked_after_5_wrong_and_8_hours(monkeypatch):
    app = app_with(monkeypatch)
    plain = TestClient(app)
    assert plain.get("/admin").status_code == 200 and "Admin" in plain.get("/admin").text
    assert plain.post("/admin/api/login", json={"password": "s3cret-test"}).status_code == 400  # plain HTTP
    https = TestClient(app, base_url="https://testserver")
    assert https.get("/admin/api/usage").status_code == 401
    assert https.get("/admin/api/me").json()["in"] is False
    for k in range(4):
        r = https.post("/admin/api/login", json={"password": "wrong"})
        assert r.status_code == 403 and f"{4 - k} more tries" in r.json()["detail"]
    assert https.post("/admin/api/login", json={"password": "wrong"}).status_code == 423
    assert https.post("/admin/api/login", json={"password": "s3cret-test"}).status_code == 423  # locked: even the right one
    other = TestClient(web.create_app(), base_url="https://testserver")  # a new app: the lock is per app run
    r = other.post("/admin/api/login", json={"password": "s3cret-test"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/admin" in cookie and "SameSite=strict" in cookie
    assert "Max-Age=28800" in cookie
    assert other.get("/admin/api/me").json()["in"] is True
    v = other.get("/admin/api/usage").json()
    assert v["today"] is True and "people" in v and "load" in v
    assert other.get("/admin/api/usage?day=../etc").status_code == 400
    assert other.post("/admin/api/logout").status_code == 200
    assert other.get("/admin/api/usage").status_code == 401


def test_admin_without_a_password_says_how_to_set_one(monkeypatch):
    https = TestClient(app_with(monkeypatch, None), base_url="https://testserver")
    assert https.get("/admin/api/me").json() == {"in": False, "https": True, "set": False, "host": "testserver"}
    r = https.post("/admin/api/login", json={"password": "anything"})
    assert r.status_code == 503 and "ADMIN_PASSWORD" in r.json()["detail"]


def test_the_page_reports_tabs_and_where_an_investigation_started(monkeypatch):
    def fake_settings(path=None):
        s = Settings()
        s.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
        return s
    monkeypatch.setattr(web, "load_settings", fake_settings)
    monkeypatch.setattr(web, "investigate", lambda req, a, n, s: {"answer": {"found": [{"issue": "crash"}]}})
    app = app_with(monkeypatch)
    client = TestClient(app)
    assert client.post("/api/seen", json={"tab": "care"}).json() == {"ok": True}
    client.post("/api/seen", json={"tab": "nope"})  # not a tab: not counted
    body = {"username": "sample07", "start": "2026-11-16T13:00:00Z", "end": "2026-11-16T15:00:00Z", "source": "today"}
    job = client.post("/api/jobs", json=body).json()["id"]
    for _ in range(100):
        if client.get(f"/api/jobs/{job}").json()["done"]:
            break
        time.sleep(0.02)
    v = app.state.usage.view(datetime.now(timezone.utc))
    assert v["pages"]["by_tab"] == [{"tab": "Care list", "n": 1}]
    assert v["investigations"]["by"] == [("today", 1)] and v["investigations"]["found"] == [("an app crash", 1)]
    assert v["people"][0]["addr"] == "testclient" and v["people"][0]["investigations"] == 1


def test_the_main_page_sends_its_tab_and_where_each_investigation_started():
    page = (web.STATIC / "index.html").read_text(encoding="utf-8")
    assert 'fetch("api/seen"' in page and "seenTab(tab);" in page
    assert "body.source = invFrom" in page and 'runs[Number(b.dataset.k)], "ask")' in page
    admin = (web.STATIC / "admin.html").read_text(encoding="utf-8")
    for by in ("investigate", "today", "care", "care-auto", "ask", "software", "API"):
        assert (f'"{by}":' in admin) or (f" {by}:" in admin)
    assert "confirm(" not in admin and "alert(" not in admin


def test_netim_reads_in_parallel_are_counted_under_what_the_app_was_doing(monkeypatch):
    """NetIM reads interfaces in a pool of threads, which do not inherit the purpose: they were "other"."""
    from uxcorrelate.config import NetimConfig
    from uxcorrelate.models import NetimDevice
    from uxcorrelate.netim import NetimClient
    use = Usage(Today(ZONE))
    monkeypatch.setattr(_http, "METER", use.call)
    client = NetimClient(NetimConfig(base_url="https://netim.test:8543"),
                         transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"items": []})))
    with _http.doing("NetIM index"):
        client.read_interfaces([NetimDevice(id=str(k), name=f"rtr{k}") for k in range(6)])
    [netim] = use.view(datetime.now(timezone.utc))["load"]
    assert [(p["purpose"], p["calls"]) for p in netim["purposes"]] == [("NetIM index", 6)]


def test_the_read_at_start_is_told_apart_from_the_regular_reads(tmp_path):
    today = Today(ZONE)
    use = Usage(today, tmp_path)
    today.start_took = 1500.0  # the last 24 hours, at start
    use.read_done(NOW)
    today.read_took = 40.0
    use.read_done(NOW + timedelta(minutes=15))
    today.read_took = 1000.0  # longer than the 15 minutes between reads
    use.read_done(NOW + timedelta(minutes=30))
    r = use.view(NOW + timedelta(minutes=30))["reads"]
    assert (r["n"], r["longest"], r["over"]) == (2, 1000.0, 1)
    assert r["starts"] == [{"at_local": "10:00", "seconds": 1500.0}]
