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
