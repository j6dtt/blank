"""The code against Riverbed's own reference data (see tests/fixtures/README.md)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from uxcorrelate.config import NetimConfig, NetimMetric, Settings
from uxcorrelate.netim import NetimClient, metric_request_body, parse_metric_response
from uxcorrelate.verify import check_schema, parse_odata_metadata

FIXTURES = Path(__file__).parent / "fixtures"
START = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def test_every_default_aternity_name_is_in_real_metadata():
    schema = parse_odata_metadata((FIXTURES / "aternity_metadata_v2.xml").read_text())
    failed = [f"{c.item}: {c.message}" for c in check_schema(Settings(), schema) if c.ok is False]
    assert failed == []


def test_metric_request_matches_steelscript_netim():
    metric = NetimMetric("if_util_in", "util", "INTERFACE", "IFC_UTIL", "utilizationIn", 80)
    body = metric_request_body(metric, ["51"], START, END)
    assert body["elementIds"] == [51]
    assert body["elementType"] == "VNES_INTERFACE"
    assert body["metricClass"] == "IFC_UTIL" and body["metrics"] == ["utilizationIn"]
    assert body["startTime"] == 1793095200000 and body["endTime"] == 1793102400000
    assert body["includeSamples"] is True and body["metricEpochEnum"] == "RAW"
    device = NetimMetric("dev_cpu", "cpu", "DEVICE", "CPU_UTIL", "cpuUtil", 85)
    assert metric_request_body(device, ["7"], START, END)["elementType"] == "VNES_OE"


def test_metric_reply_in_steelscript_layout_is_parsed():
    data = json.loads((FIXTURES / "netim_metric_data.json").read_text())
    out = NetimMetric("if_util_out", "util", "INTERFACE", "IFC_UTIL", "utilizationOut", 80)
    samples = parse_metric_response(data, out, "INTERFACE")
    assert [(s.object_id, s.value) for s in samples] == [("51", 12.0), ("51", 11.0)]
    assert samples[0].timestamp == datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


def test_netim_lists_follow_meta_next_offset():
    seen = []

    def handler(request: httpx.Request):
        offset = int(request.url.params["offset"])
        seen.append(offset)
        items = [{"id": offset + 1, "deviceName": f"dev{offset + 1}", "accessAddress": "10.0.0.1"}]
        nxt = offset + 1 if offset < 2 else None
        return httpx.Response(200, json={"items": items, "meta": {"total": 3, "limit": 1, "next_offset": nxt}})

    client = NetimClient(NetimConfig(base_url="https://netim.test:8543"), transport=httpx.MockTransport(handler))
    assert [d.name for d in client.devices()] == ["dev1", "dev2", "dev3"]
    assert seen == [0, 1, 2]


def test_a_tenant_without_some_sources_is_told_what_is_skipped_and_where_free_disk_is():
    # like a live tenant: no RAW_DATA_TOP_PROCESSES or RAW_DATA_STORAGE, no free disk on activities
    schema = parse_odata_metadata((FIXTURES / "aternity_metadata_v2.xml").read_text())
    for gone in ("RAW_DATA_TOP_PROCESSES", "RAW_DATA_STORAGE"):
        del schema[gone]
    schema["BUSINESS_ACTIVITIES_RAW"] = schema["BUSINESS_ACTIVITIES_RAW"] - {"OS_FREE_DISK_SPACE"}
    assert not [c for c in check_schema(Settings(), schema) if "disk_space" in c.item or c.item == "free disk space"]
    settings = Settings()
    settings.aternity.entities["disk_space"] = "RAW_DATA_STORAGE"
    checks = {c.item: c for c in check_schema(settings, schema)}
    assert checks["entities.top_processes"].ok is None  # skipped, not a failure
    assert "busiest processes" in checks["entities.top_processes"].message
    disk = checks["free disk space"]
    assert disk.ok is False and "disk_space: APPLICATIONS_RAW" in disk.paste
    assert "free_space: OS_FREE_DISK_SPACE" in disk.paste and 'free_percent: ""' in disk.paste
    assert [c.item for c in checks.values() if c.ok is False] == ["free disk space"]
