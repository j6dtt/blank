"""dev/sample_riverbed.py, the made-up Aternity and NetIM of the dev app: the app's own
clients read it as a real tenant, and its story comes out of an investigation."""

import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dev"))
import sample_riverbed as S  # noqa: E402

from uxcorrelate.aternity import AternityClient  # noqa: E402
from uxcorrelate.config import Settings  # noqa: E402
from uxcorrelate.correlate import InvestigationRequest, investigate  # noqa: E402
from uxcorrelate.netim import NetimClient  # noqa: E402
from uxcorrelate.today import Today, _read_health, _read_hour  # noqa: E402
from uxcorrelate.verify import verify_aternity, verify_netim  # noqa: E402

NOW = datetime(2026, 11, 13, 21, 30, tzinfo=timezone.utc)  # a Friday, 16:30 in New York


@pytest.fixture(scope="module")
def world():
    sample = S.Sample(lambda: NOW)

    def answer(req: httpx.Request) -> httpx.Response:
        status, ctype, data = sample.handle(req.method, req.url.path, req.url.query.decode(), req.content,
                                            "http://sample")
        return httpx.Response(status, headers={"content-type": ctype}, content=data)

    settings = Settings()
    settings.aternity.base_url = "http://sample/aternity.odata/v2.0"
    settings.netim.base_url = "http://sample/netim"
    settings.timezone = S.ZONE
    settings.thresholds.slow_status = ["Major"]
    settings.topology.applications = {"Sample ERP": ["erp-srv1"], "Sample Mail": ["mail-srv1"]}
    settings.topology.sites = {S.NORTH: ["north-br-rtr1"], S.SOUTH: ["south-br-rtr1"]}
    settings.topology.always_include = ["dc-core-sw1", "dc-fw1"]
    transport = httpx.MockTransport(answer)
    return sample, settings, transport


def clients(world):
    _, settings, transport = world
    return AternityClient(settings.aternity, transport=transport), NetimClient(settings.netim, transport=transport)


def test_every_column_it_sends_is_in_the_real_metadata(world):
    sample = world[0]
    for entity in S.TIME_COL | {"INSTALLED_SOFTWARE": 0, "DEVICE_INVENTORY": 0}:
        for row in sample.rows(entity, NOW - timedelta(days=2), NOW)[:2000]:
            sample._out(entity, row, [])
    assert sample.dropped == {}


def test_filters_pages_and_sums(world, monkeypatch):
    sample = world[0]
    a, _ = clients(world)
    flt = ("TIMEFRAME ge 2026-11-13T15:00:00Z and TIMEFRAME lt 2026-11-13T17:00:00Z and "
           "(USERNAME eq 'sample23' or USERNAME eq 'sample27')")
    rows = a.query("BUSINESS_ACTIVITIES_RAW", flt)
    assert rows and {r["USERNAME"] for r in rows} == {"sample23", "sample27"}
    monkeypatch.setattr(S, "PAGE_ROWS", 7)  # paging by @odata.nextLink gives the same rows
    assert a.query("BUSINESS_ACTIVITIES_RAW", flt) == rows
    # added up without DEVICE_NAME, as the live INSTALLED_SOFTWARE
    agg = a.query("INSTALLED_SOFTWARE", select=["INSTALLED_SW_NAME", "INSTALLED_SW_VERSION", "DEVICE_COUNT"])
    mail = {r["INSTALLED_SW_VERSION"]: r["DEVICE_COUNT"] for r in agg if r["INSTALLED_SW_NAME"] == "Sample Mail"}
    assert mail == {"16.2.1100": 14, "16.1.900": 38}
    # rows not arrived yet are not sent; a source prod does not publish "doesn't exist"
    assert all(r["TIMEFRAME"] <= "2026-11-13T21:30:00Z" for r in a.query("DEVICE_HEALTH_RAW", "TIMEFRAME ge 2026-11-13T20:00:00Z"))
    with pytest.raises(Exception, match="doesn't exist"):
        a.query("ANOMALY_DETECTION_INCIDENTS")


def test_same_rows_at_every_read(world):
    other = S.Sample(lambda: NOW)
    h = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)
    assert other.actions(h) == world[0].actions(h) and other.incidents(h.date()) == world[0].incidents(h.date())


def test_the_today_reader_reads_it(world):
    a, _ = clients(world)
    today = Today(S.ZONE)
    for k in range(10):
        _read_hour(today, None, a, NOW.replace(minute=0) - timedelta(hours=k), NOW)
    _read_health(today, a, NOW)
    kinds = Counter(x[2][1] for x in today.health)  # (category, name)
    assert kinds["HD Failure"] == 2 and kinds["System Crash"] == 1
    assert any(x[6] for x in today.health if x[2][1] == "HD Failure")  # Aternity's "(Drive)" label: a failing disk
    assert sum(h.status["Major"] for h in today.hours.values()) > 50


def test_verify_finds_nothing_wrong(world):
    a, n = clients(world)
    checks = verify_aternity(world[1], a) + verify_netim(world[1], n)
    assert [c.item + ": " + c.message for c in checks if c.ok is False] == []


def test_the_failing_disk_leads_an_investigation(world):
    a, n = clients(world)
    r = investigate(InvestigationRequest(start=NOW - timedelta(hours=9), end=NOW, username="sample07"), a, n,
                    world[1])
    assert "disk" in r["answer"]["title"].lower()


def test_the_branch_wan_link_is_seen(world):
    a, n = clients(world)
    start = datetime(2026, 11, 13, 15, 15, tzinfo=timezone.utc)  # 10:15 to 11:30 in New York
    r = investigate(InvestigationRequest(start=start, end=start + timedelta(hours=1, minutes=15), username="sample30"),
                    a, n, world[1])
    assert any("south-br-rtr1" in str(p.get("object")) for p in r["problems"])
