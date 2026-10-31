from datetime import datetime, timezone

import httpx

from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import AternityConfig, NetimConfig, NetimMetric
from uxcorrelate.netim import NetimClient, parse_metric_response

START = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def test_aternity_builds_odata_filter_and_follows_next_link():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url)
        if "skip" in str(request.url):
            return httpx.Response(200, json={"value": [
                {"TIMEFRAME": "2026-10-27T10:05:00Z", "USERNAME": "o'brien", "DEVICE_NAME": "LT1",
                 "APPLICATION_NAME": "CRM", "ACTIVITY_RESPONSE_TIME": "4.2"}]})
        return httpx.Response(200, json={
            "value": [{"TIMEFRAME": "2026-10-27T10:00:00Z", "USERNAME": "o'brien", "DEVICE_NAME": "LT1",
                       "APPLICATION_NAME": "CRM", "ACTIVITY_RESPONSE_TIME": 1.5, "ACTIVITY_NETWORK_TIME": 0.4,
                       "HRC_CPU_UTIL": 97, "DEVICE_IP_ADDRESS": "10.1.1.5", "BUSINESS_LOCATION": "Denver"}],
            "@odata.nextLink": "https://aternity.test/aternity.odata/v2.0/BUSINESS_ACTIVITIES_RAW?skip=1",
        })

    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0", username="u", password="p")
    client = AternityClient(cfg, transport=httpx.MockTransport(handler))
    samples = client.user_activity(START, END, username="o'brien", application="CRM")

    assert [s.response_time for s in samples] == [1.5, 4.2]
    assert samples[0].network_time == 0.4
    # device context comes from the same rows, no extra queries
    assert client.endpoint_info("LT1").ip_address == "10.1.1.5"
    assert client.endpoint_info("LT1").location == "Denver"
    flt = seen[0].params["$filter"]
    assert "USERNAME eq 'o''brien'" in flt
    assert "TIMEFRAME ge 2026-10-27T10:00:00Z" in flt
    assert "APPLICATION_NAME eq 'CRM'" in flt
    assert seen[0].path == "/aternity.odata/v2.0/BUSINESS_ACTIVITIES_RAW"


def test_netim_devices_interfaces_and_metrics():
    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/api/netim/v1/devices":
            return httpx.Response(200, json={"items": [
                {"id": 5, "name": "rtr1", "accessAddress": "10.0.0.1", "deviceType": "router"}]})
        if path == "/api/netim/v1/devices/5/interfaces":
            return httpx.Response(200, json={"items": [
                {"id": 51, "name": "Gi0/0", "ipAddresses": [{"ipAddress": "10.0.0.1", "prefixLength": 24}],
                 "speed": 1000000000}]})
        if path.endswith("network-metric-data"):
            import json
            body = json.loads(request.content)
            assert body["elementIds"] == [51] and body["metricClass"] == "IFC_UTIL"
            return httpx.Response(200, json={
                "elementIdToObjectInfoMap": {"1": {"objectId": 51}},
                "metricData": {"metricNames": ["utilizationIn"], "metricElementDataList": [
                    {"elementId": 1, "timestampToValuesMap": {"1793095200000": [91.0], "1793095500000": [40]}}]}})
        return httpx.Response(404)

    client = NetimClient(NetimConfig(base_url="https://netim.test:8543"),
                         transport=httpx.MockTransport(handler))
    devices = client.devices()
    assert devices[0].name == "rtr1" and devices[0].ip_address == "10.0.0.1"
    itfs = client.interfaces("5")
    assert itfs[0].ip_addresses == ["10.0.0.1/24"]
    metric = NetimMetric("if_util_in", "util", "INTERFACE", "IFC_UTIL", "utilizationIn", 80)
    samples = client.metrics(metric, ["51"], START, END)
    assert [s.value for s in samples] == [91.0, 40.0]
    # alerts are off by default; a failing alerts path -> warning, not failure
    assert client.alerts(START, END) == []
    client.cfg.alerts_path = "/api/netim/v1/alerts"
    assert client.alerts(START, END) == []
    assert client.warnings


def test_parse_metric_response_flat_shape_filters_other_metrics():
    metric = NetimMetric("dev_cpu", "cpu", "DEVICE", "CPU_UTIL", "cpuUtilization", 85)
    data = [
        {"deviceId": "7", "metricName": "cpuUtilization", "time": "2026-10-27T10:00:00Z", "value": 90},
        {"deviceId": "7", "metricName": "somethingElse", "time": "2026-10-27T10:00:00Z", "value": 1},
    ]
    samples = parse_metric_response(data, metric, "DEVICE")
    assert len(samples) == 1 and samples[0].object_id == "7" and samples[0].value == 90


def test_aternity_query_keeps_dollar_signs_literal():
    # live Aternity: "Query option must start with $: %24format=json"
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"value": []})

    client = AternityClient(AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0"),
                            transport=httpx.MockTransport(handler))
    client.query("BUSINESS_ACTIVITIES_RAW", "USERNAME eq 'a b' and TIMEFRAME ge 2026-10-28T18:21:45Z", top=5)
    assert seen == ["/aternity.odata/v2.0/BUSINESS_ACTIVITIES_RAW?$format=json"
                    "&$filter=USERNAME%20eq%20'a%20b'%20and%20TIMEFRAME%20ge%202026-10-28T18:21:45Z&$top=5"]


def test_aternity_health_events_and_uxi_and_their_failures():
    def handler(request: httpx.Request):
        flt = request.url.params["$filter"]
        if request.url.path.endswith("/DEVICE_HEALTH_RAW"):
            assert "DEVICE_NAME eq 'LT1'" in flt
            return httpx.Response(200, json={"value": [
                {"TIMEFRAME": "2026-10-27T10:05:00Z", "HEALTH_EVENT_CATEGORY": "Crash",
                 "HEALTH_EVENT_NAME": "Application crash", "HEALTH_EVENT_COMPONENT": "OUTLOOK.EXE",
                 "HEALTH_EVENT_VOLUME": 2}]})
        if request.url.path.endswith("/APPLICATIONS_RAW"):
            assert "APPLICATION_NAME eq 'CRM'" in flt
            return httpx.Response(200, json={"value": [
                {"TIMEFRAME": "2026-10-27T10:00:00Z", "APPLICATION_NAME": "CRM",
                 "APPLICATION_UXI_AVG": "6.4", "APPLICATION_CRASHES_TOTAL": 1}]})
        return httpx.Response(404)

    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0")
    client = AternityClient(cfg, transport=httpx.MockTransport(handler))
    events = client.health_events("LT1", START, END)
    assert events[0].category == "Crash" and events[0].component == "OUTLOOK.EXE" and events[0].count == 2
    usage = client.app_usage("LT1", START, END, application="CRM")
    assert usage[0].uxi == 6.4 and usage[0].crashes == 1

    # a missing data source is a warning, never an aborted investigation
    client.cfg.entities["health_events"] = "NOT_THERE"
    client.http.retries = 0
    assert client.health_events("LT1", START, END) == []
    assert client.warnings[0].startswith("Device health events not checked")


def test_free_disk_space_as_a_percent_from_either_kind_of_column():
    def handler(request: httpx.Request):
        if request.url.path.endswith("/RAW_DATA_STORAGE"):
            return httpx.Response(200, json={"value": [
                {"MEASUREMENT_START_TIMESTAMP": "2026-10-27T10:00:00Z", "FREE_SPACE_PERCENT": 4.5}]})
        if request.url.path.endswith("/APPLICATIONS_RAW"):  # free space in the inventory's unit
            return httpx.Response(200, json={"value": [{"TIMEFRAME": "2026-10-27T10:00:00Z", "OS_FREE_DISK_SPACE": 12}]})
        if request.url.path.endswith("/DEVICE_INVENTORY"):
            return httpx.Response(200, json={"value": [{"DEVICE_NAME": "LT1", "OS_TOTAL_DISK_SPACE": 240}]})
        return httpx.Response(404)

    cfg = AternityConfig(base_url="https://aternity.test/aternity.odata/v2.0")
    client = AternityClient(cfg, transport=httpx.MockTransport(handler))
    assert [h.free_disk for h in client.disk_space("LT1", START, END)] == [4.5]
    cfg.entities["disk_space"] = "APPLICATIONS_RAW"
    cfg.fields["disk_space"].update(timestamp="TIMEFRAME", free_percent="", free_space="OS_FREE_DISK_SPACE")
    assert [h.free_disk for h in client.disk_space("LT1", START, END)] == [5.0]
