import json

import httpx

from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import Settings, Topology
from uxcorrelate.netim import NetimClient
from uxcorrelate.verify import parse_metric_classes, parse_odata_metadata, render_checks, verify_aternity, verify_netim

METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
 <edmx:DataServices>
  <Schema Namespace="Aternity" xmlns="http://docs.oasis-open.org/odata/ns/edm">
   <EntityType Name="BusinessActivityRaw">
    <Property Name="TIMEFRAME" Type="Edm.DateTimeOffset"/>
    <Property Name="USERNAME" Type="Edm.String"/>
    <Property Name="DEVICE_NAME" Type="Edm.String"/>
    <Property Name="APPLICATION_NAME" Type="Edm.String"/>
    <Property Name="ACTIVITY_NAME" Type="Edm.String"/>
    <Property Name="ACTIVITY_RESPONSE_TIME" Type="Edm.Double"/>
    <Property Name="CLIENT_TIME" Type="Edm.Double"/>
    <Property Name="NETWORK_TIME" Type="Edm.Double"/>
    <Property Name="BACKEND_TIME" Type="Edm.Double"/>
    <Property Name="BUSINESS_LOCATION" Type="Edm.String"/>
   </EntityType>
   <EntityType Name="DeviceInventory">
    <Property Name="DEVICE_NAME" Type="Edm.String"/>
    <Property Name="IP_ADDRESS" Type="Edm.String"/>
    <Property Name="LOCATION" Type="Edm.String"/>
    <Property Name="OS_NAME" Type="Edm.String"/>
   </EntityType>
   <EntityContainer Name="Container">
    <EntitySet Name="BUSINESS_ACTIVITIES_RAW" EntityType="Aternity.BusinessActivityRaw"/>
    <EntitySet Name="DEVICE_INVENTORY" EntityType="Aternity.DeviceInventory"/>
   </EntityContainer>
  </Schema>
 </edmx:DataServices>
</edmx:Edmx>"""


def aternity_handler(request: httpx.Request):
    if request.url.path.endswith("/$metadata"):
        return httpx.Response(200, text=METADATA)
    flt = request.url.params.get("$filter", "")
    found = "'CRM'" in flt or "'Denver'" in flt
    return httpx.Response(200, json={"value": [{"X": 1}] if found else []})


def netim_handler(request: httpx.Request):
    path = request.url.path
    if path == "/api/netim/v1/devices":
        return httpx.Response(200, json={"items": [{"id": 5, "name": "den-rtr01", "accessAddress": "10.0.0.1"}]})
    if path == "/api/netim/v1/devices/5/interfaces":
        return httpx.Response(200, json={"items": [
            {"id": 51, "name": "Gi0/1", "ipAddresses": [{"ipAddress": "10.0.0.1", "prefixLength": 24}]}]})
    classes = {"IFC_UTIL": ["utilizationIn", "utilizationOut"], "CPU_UTIL": ["cpuUtil"]}
    if path == "/api/netim/v1/metric-classes":
        return httpx.Response(200, json={"items": [{"id": c, "name": c} for c in classes]})
    if path.startswith("/api/netim/v1/metric-classes/"):
        cid = path.rsplit("/", 1)[1]
        if cid not in classes:
            return httpx.Response(404)
        return httpx.Response(200, json={"id": cid, "metrics": [{"name": m} for m in classes[cid]]})
    if path.endswith("network-metric-data"):
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "elementIdToObjectInfoMap": {"1": {"objectId": body["elementIds"][0]}},
            "metricData": {"metricNames": body["metrics"], "metricElementDataList": [
                {"elementId": 1, "timestampToValuesMap": {"1793095200000": [12.0]}}]}})
    if path == "/api/netim/v1/alerts":
        return httpx.Response(200, json={"items": []})
    return httpx.Response(404)


def make_settings(**topology) -> Settings:
    s = Settings()
    s.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    s.netim.base_url = "https://netim.test:8543"
    s.aternity.entities = {"activities": "BUSINESS_ACTIVITIES_RAW", "inventory": "DEVICE_INVENTORY"}
    s.aternity.fields = {
        "activities": {"timestamp": "TIMEFRAME", "username": "USERNAME", "application": "APPLICATION_NAME",
                       "response_time": "ACTIVITY_RESPONSE_TIME", "location": "BUSINESS_LOCATION"},
        "inventory": {"device": "DEVICE_NAME", "location": "LOCATION"},
    }
    s.netim.metrics = [m for m in s.netim.metrics if m.key in ("if_util_out", "dev_cpu")]
    s.topology = Topology(**topology)
    return s


def run(settings):
    aternity = AternityClient(settings.aternity, transport=httpx.MockTransport(aternity_handler))
    netim = NetimClient(settings.netim, transport=httpx.MockTransport(netim_handler))
    return verify_aternity(settings, aternity) + verify_netim(settings, netim)


def test_parse_odata_metadata_maps_entity_sets_to_columns():
    schema = parse_odata_metadata(METADATA)
    assert "BUSINESS_LOCATION" in schema["BUSINESS_ACTIVITIES_RAW"]
    assert schema["DEVICE_INVENTORY"] == {"DEVICE_NAME", "IP_ADDRESS", "LOCATION", "OS_NAME"}


def test_parse_metric_classes_reads_nested_metric_names():
    classes = parse_metric_classes([{"id": "IFC_UTIL", "metrics": [{"name": "utilizationOut"}]}])
    assert classes == {"IFC_UTIL": {"utilizationOut"}}


def test_verify_passes_when_every_name_exists():
    checks = run(make_settings(applications={"CRM": ["10.0.0.1"]}, sites={"Denver": ["den-rtr01"]}))
    assert [c for c in checks if c.ok is False] == []
    assert "All checks passed." in render_checks(checks)


def test_verify_names_each_wrong_setting_and_suggests_the_real_one():
    s = make_settings(applications={"CRM Portal": []}, sites={"Denver": ["den-rtr1"]})
    s.aternity.fields["activities"]["response_time"] = "RESPONSE_TIME"
    s.aternity.entities["inventory"] = "DEVICE_INVENTRY"
    s.netim.metrics[0].metric_class = "IF_UTIL"
    s.netim.metrics[1].metric = "cpuUtilization"
    failed = {c.item: c.message for c in run(s) if c.ok is False}

    assert "ACTIVITY_RESPONSE_TIME" in failed["fields.activities.response_time"]
    assert "DEVICE_INVENTORY" in failed["entities.inventory"]
    assert "Application 'CRM Portal' not found" in failed["topology: CRM Portal"]
    assert "den-rtr01" in failed["topology: den-rtr1"]
    assert "IFC_UTIL" in failed["metrics.if_util_out"]
    assert "cpuUtil" in failed["metrics.dev_cpu"]
    # one paste-ready block with both corrections
    paste = next(c.paste for c in run(s) if c.item == "corrected metrics")
    assert "metric_class: IFC_UTIL" in paste and "metric: cpuUtil" in paste


def test_verify_reports_unreachable_systems_with_a_fix():
    def down(request):
        raise httpx.ConnectError("Name does not resolve")

    s = make_settings()
    aternity = AternityClient(s.aternity, transport=httpx.MockTransport(down))
    aternity.http.retries = 0
    checks = verify_aternity(s, aternity)
    assert checks[0].ok is False and "ATERNITY_PASSWORD" in checks[0].fix


def test_metadata_is_requested_as_xml():
    seen = []

    def handler(request):
        if request.url.path.endswith("/$metadata"):
            seen.append(request.headers["Accept"])
        return aternity_handler(request)

    s = make_settings()
    verify_aternity(s, AternityClient(s.aternity, transport=httpx.MockTransport(handler)))
    assert seen == ["application/xml"]


def test_missing_data_source_lists_what_the_tenant_publishes():
    s = make_settings()
    s.aternity.entities["activities"] = "XYZ"
    failed = {c.item: c.message for c in run(s) if c.ok is False}
    assert "Published: BUSINESS_ACTIVITIES_RAW, DEVICE_INVENTORY." in failed["entities.activities"]


def test_metric_names_reads_netims_paged_metrics_list():
    # live NetIM wraps a class's metrics as {"items": [...], "meta": {...}}
    cls = {"id": "IFC_UTIL", "metrics": {"items": [{"id": "utilizationIn"}], "meta": {"total": 1}}}
    assert parse_metric_classes([cls]) == {"IFC_UTIL": {"utilizationIn"}}
