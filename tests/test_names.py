"""Names offered as you type in the Username and Device boxes."""

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from uxcorrelate import web
from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import AternityConfig
from uxcorrelate.names import KEEP_DAYS, MATCHES, NameList

T = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


def client(rows, seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request.url.params)
        return httpx.Response(200, json={"value": rows})
    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0", username="u", password="p")
    return AternityClient(cfg, transport=httpx.MockTransport(handler))


def test_reads_only_the_two_columns_of_one_span_and_skips_no_value():
    seen = []
    rows = [{"USERNAME": "ab1234", "DEVICE_NAME": "VD-41"}, {"USERNAME": "N/A", "DEVICE_NAME": "VD-14"},
            {"USERNAME": " cd5678 ", "DEVICE_NAME": None}]
    names = NameList()
    assert names.read(client(rows, seen), T - timedelta(hours=1), T) == 3
    assert sorted(names.seen["username"]) == ["ab1234", "cd5678"]
    assert sorted(names.seen["device"]) == ["VD-14", "VD-41"]
    assert seen[0]["$select"] == "USERNAME,DEVICE_NAME"
    assert seen[0]["$filter"] == "TIMEFRAME ge 2026-11-09T08:00:00Z and TIMEFRAME lt 2026-11-09T09:00:00Z"


def test_matches_any_part_any_case_those_starting_with_it_first():
    names = NameList()
    names.read(client([{"USERNAME": u, "DEVICE_NAME": "PC1"} for u in ("xmoss1", "MOSSLEY", "bmoss", "amy")]),
               T - timedelta(hours=1), T)
    m = names.match("username", "moss")
    assert m["names"] == ["MOSSLEY", "bmoss", "xmoss1"] and m["more"] == 0 and m["count"] == 4
    assert m["since"] == "2026-11-09T08:00:00Z"
    assert names.match("username", "  ")["names"] == []


def test_long_lists_are_cut_and_say_how_many_more():
    names = NameList()
    names.read(client([{"USERNAME": f"user{i:03}", "DEVICE_NAME": None} for i in range(30)]), T - timedelta(hours=1), T)
    m = names.match("username", "user")
    assert len(m["names"]) == MATCHES and m["more"] == 30 - MATCHES


def test_a_name_not_seen_for_the_days_aternity_keeps_is_dropped():
    names = NameList()
    old = T - timedelta(days=KEEP_DAYS + 1)
    names.read(client([{"USERNAME": "gone", "DEVICE_NAME": "PC-OLD"}]), old - timedelta(hours=1), old)
    names.read(client([{"USERNAME": "here", "DEVICE_NAME": "PC1"}]), T - timedelta(hours=1), T)
    assert list(names.seen["username"]) == ["here"] and list(names.seen["device"]) == ["PC1"]


def test_a_cut_short_reply_is_said():
    names = NameList()
    c = client([{"USERNAME": "a", "DEVICE_NAME": "b"}])
    c.cfg.max_pages = 1
    c.http.get_json = lambda url: {"value": [{"USERNAME": "a", "DEVICE_NAME": "b"}], "@odata.nextLink": url}
    names.read(c, T - timedelta(hours=1), T)
    assert names.match("device", "b")["partial"] is True


def test_the_endpoint_answers_per_kind():
    app = web.create_app()
    app.state.names.read(client([{"USERNAME": "ab1234", "DEVICE_NAME": "VD-41"}]), T - timedelta(hours=1), T)
    http = TestClient(app)  # not started: no background read in tests
    assert http.get("/api/names", params={"kind": "device", "q": "vd"}).json()["names"] == ["VD-41"]
    assert http.get("/api/names", params={"kind": "username", "q": "1234"}).json()["names"] == ["ab1234"]
    assert http.get("/api/names", params={"kind": "location", "q": "x"}).status_code == 400
