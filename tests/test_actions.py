"""The admin page's ACTIONS tab (actions.py): every action of one user today, read now from Aternity."""

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from uxcorrelate import actions, web
from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import DEFAULT_ATERNITY_FIELDS, AternityConfig
from uxcorrelate.names import NameList

ZONE = "America/New_York"  # UTC-5 in November
NOW = datetime(2026, 11, 17, 21, 45, tzinfo=timezone.utc)  # 16:45 in New York


def client_of(handler, max_pages=None):
    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0", username="u", password="p")
    if max_pages:
        cfg.max_pages = max_pages
    return AternityClient(cfg, transport=httpx.MockTransport(handler))


def act(t, app="Outlook", activity="Open Mail", rt=1.25, status="Normal", desk="VD-01", virt="true", sat="HV00045-SAMPLE45THIN"):
    return {"TIMEFRAME": t.isoformat(), "USERNAME": "CORP\\Ann", "APPLICATION_NAME": app, "ACTIVITY_NAME": activity,
            "ACTIVITY_RESPONSE_TIME": rt, "SLA_STATUS": status, "DEVICE_NAME": desk, "VIRTUALIZATION": virt,
            "CLIENT_DEVICE_NAME": sat}


def test_one_read_of_the_users_day_latest_first_to_the_second():
    asked = []
    rows = [act(datetime(2026, 11, 17, 13, 5, 7, tzinfo=timezone.utc)),
            act(datetime(2026, 11, 17, 21, 30, 59, tzinfo=timezone.utc), app="SAP", activity="Post", rt=12.5, status="Major"),
            act(datetime(2026, 11, 17, 15, 0, 0, tzinfo=timezone.utc), rt="N/A", status="Minor", desk="PC-07", virt="false", sat="N/A")]

    def handler(req):
        asked.append(dict(req.url.params))
        return httpx.Response(200, json={"value": rows})
    got = actions.read(client_of(handler), "CORP\\Ann", ZONE, NOW)
    p = asked[0]
    # from local midnight (05:00 UTC) to now, that username only, the columns shown only, at most ROWS_MAX
    assert p["$filter"] == ("TIMEFRAME ge 2026-11-17T05:00:00Z and TIMEFRAME lt 2026-11-17T21:45:00Z and "
                            "USERNAME eq 'CORP\\Ann'")
    assert p["$top"] == str(actions.ROWS_MAX + 1)
    assert set(p["$select"].split(",")) == {"TIMEFRAME", "USERNAME", "APPLICATION_NAME", "ACTIVITY_NAME", "ACTIVITY_RESPONSE_TIME",
                                            "SLA_STATUS", "DEVICE_NAME", "CLIENT_DEVICE_NAME", "VIRTUALIZATION"}
    assert [x["t"] for x in got["rows"]] == ["16:30:59", "10:00:00", "08:05:07"]
    assert got["rows"][0] | {"at": 0} == {"at": 0, "t": "16:30:59", "app": "SAP", "activity": "Post", "rt": 12.5, "status": "Major",
                                          "desktop": "VD-01", "sat_at": "HV00045-SAMPLE45THIN", "kind": "virtual"}
    assert got["rows"][1]["rt"] is None and got["rows"][1]["sat_at"] == "" and got["rows"][1]["kind"] == "physical"
    assert (got["count"], got["first"], got["last"], got["top"], got["top_count"]) == (3, "08:05:07", "16:30:59", "Major", 1)
    assert got["apps"] == [["Outlook", 2], ["SAP", 1]] and got["desktops"] == ["PC-07", "VD-01"]
    assert got["date"] == "2026-11-17" and got["as_of_local"] == "16:45" and not got["cut"] and not got["partial"]
    assert got["notes"] == []


def test_a_name_with_a_quote_is_escaped():
    asked = []

    def handler(req):
        asked.append(req.url.params["$filter"])
        return httpx.Response(200, json={"value": []})
    actions.read(client_of(handler), "o'neil", ZONE, NOW)
    assert asked[0].endswith("USERNAME eq 'o''neil'")


def test_more_than_rows_max_or_a_reply_cut_short_is_said(monkeypatch):
    monkeypatch.setattr(actions, "ROWS_MAX", 3)
    base = datetime(2026, 11, 17, 14, tzinfo=timezone.utc)
    got = actions.read(client_of(lambda req: httpx.Response(200, json={"value": [act(base + timedelta(minutes=i)) for i in range(4)]})),
                       "CORP\\Ann", ZONE, NOW)
    assert got["cut"] and got["count"] == 3 and "More than 3 actions today" in got["notes"][0]

    def paged(req):  # Aternity always has a next page: stopped after max_pages
        return httpx.Response(200, json={"value": [act(base)], "@odata.nextLink": "https://aternity.test/aternity.odata/v2.0/x?p=2"})
    got = actions.read(client_of(paged, max_pages=2), "CORP\\Ann", ZONE, NOW)
    assert got["partial"] and any("cut short" in n for n in got["notes"])


def test_the_username_is_asked_for_as_aternity_writes_it():
    names = NameList()
    names.add([{"USERNAME": "CORP\\Ann", "DEVICE_NAME": "VD-01"}], DEFAULT_ATERNITY_FIELDS["activities"],
              NOW - timedelta(hours=1), NOW)
    assert actions.spelled(names, " corp\\ann ") == ("CORP\\Ann", "")
    name, note = actions.spelled(names, "zed")
    assert name == "zed" and "not among the usernames seen" in note and "asked for as typed" in note


def test_the_columns_are_the_configured_activity_columns():
    assert all(DEFAULT_ATERNITY_FIELDS["activities"].get(k) for k in actions.COLUMNS)


def test_the_actions_route_needs_the_login_and_aternity(monkeypatch, tmp_path):
    monkeypatch.setenv(web.ADMIN_ENV, "s3cret-test")
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    https = TestClient(web.create_app(), base_url="https://testserver")
    assert https.get("/admin/api/actions?user=ann").status_code == 401
    assert https.post("/admin/api/login", json={"password": "s3cret-test"}).status_code == 200
    assert https.get("/admin/api/actions?user=").status_code == 400
    r = https.get("/admin/api/actions?user=ann")
    assert r.status_code == 503 and "aternity.base_url" in r.json()["detail"]  # not set in config.yaml
    page = (web.STATIC / "admin.html").read_text(encoding="utf-8")
    # the third tab, with its link; read only when Show is pressed (no minute)
    assert 'data-t="act">Actions<' in page and 'act: "actions"' in page
    assert 'fetch("admin/api/actions?user="' in page and 'fetch("api/names?kind=username&q="' in page
    assert "confirm(" not in page and "alert(" not in page
