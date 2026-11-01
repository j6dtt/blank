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


def test_verify_shows_how_start_up_parts_and_the_change_log_are_filled():
    from pathlib import Path
    from uxcorrelate.verify import _change_log, _start_up_parts

    seen = []

    def handler(request):
        path, flt = request.url.path, request.url.params.get("$filter", "")
        seen.append(flt)
        if path.endswith("/WINDOWS_MACHINE_BOOTS"):
            return httpx.Response(200, json={"value": [
                {"TIMEFRAME": "2026-10-30T09:05:00Z", "MEASUREMENT_START_TIMESTAMP": "2026-10-30T09:00:00Z",
                 "DEVICE_NAME": "WS-B", "MEASUREMENT_TYPE": "Logon", "MEASUREMENT_DURATION_AVG": 90},
                {"TIMEFRAME": "2026-10-30T08:00:00Z", "DEVICE_NAME": "WS-B", "MEASUREMENT_TYPE": "Boot",
                 "MEASUREMENT_DURATION_AVG": 40}]})
        if path.endswith("/MACHINE_BOOT_COMPONENTS"):
            part = lambda t, typ, deg: {"TIMEFRAME": t, "DEVICE_NAME": "WS-B", "MEASUREMENT_TYPE": typ,  # noqa: E731
                                        "MEASUREMENT_DURATION_AVG": 5, "MEASUREMENT_DEGRADATION_TIME": deg}
            return httpx.Response(200, json={"value": [
                part("2026-10-30T09:05:00Z", "Phase X", 3), part("2026-10-30T08:00:00Z", "Driver Y", None),
                part("2026-10-30T08:00:00Z", "Driver Y", None), part("2026-10-30T09:05:00Z", "Phase Z", None)]})
        if path.endswith("/INSTALLED_SOFTWARE_CHANGE_LOG"):
            row = lambda t, kind, v: {"INSTALLED_SW_CHANGE_TIMESTAMP": t, "DEVICE_NAME": "WS-B",  # noqa: E731
                                      "INSTALLED_SW_NAME": "Viewer", "INSTALLED_SW_CHANGE_TYPE": kind,
                                      "INSTALLED_SW_VERSION": v, "INSTALLED_SW_VERSION_BEFORE_CHANGE": "N/A"}
            return httpx.Response(200, json={"value": [
                row("2026-10-29T10:00:00Z", "Removed", "1.0"), row("2026-10-29T10:00:00Z", "Added", "2.0"),
                {**row("2026-10-20T10:00:00Z", "Added", "5.0"), "INSTALLED_SW_NAME": "Editor"},
                # an update whose two rows are 7 minutes apart
                {**row("2026-10-25T10:00:00Z", "Removed", "3.0"), "INSTALLED_SW_NAME": "Browser"},
                {**row("2026-10-25T10:07:00Z", "Added", "3.1"), "INSTALLED_SW_NAME": "Browser"}]})
        return httpx.Response(404)

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/odata"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
    summary, starts, parts = _start_up_parts(settings, client, schema)
    assert "Types: Driver Y (2), Phase X (1), Phase Z (1)" in summary.message
    assert "WINDOWS_MACHINE_BOOTS: Logon (1), Boot (1)" in summary.message
    assert "type='Logon', duration=90" in starts.message
    assert starts.rows[0].startswith("timestamp='2026-10-30T08:00:00Z'")  # time order
    assert parts.rows == ["2026-10-30T08:00:00Z: 2 row(s), 0 longer than normal; types: Driver Y (2)",
                          "2026-10-30T09:05:00Z: 2 row(s), 1 longer than normal; types: Phase X (1), Phase Z (1)"]
    log, pc = _change_log(settings, client, schema)
    assert "Change types: Added (3), Removed (2)" in log.message
    assert "at the same second: Removed 1, Added 1; within an hour: Removed 1, Added 1; none: Added 1." in log.message
    assert log.rows[0] == ("timestamp='2026-10-29T10:00:00Z', name='Viewer', change='Removed', version='1.0', "
                           "version_before='N/A'")
    # the PC's whole 14 days, not a slice of all PCs
    assert "DEVICE_NAME eq 'WS-B'" in seen[-1]
    assert pc.item == "software change log of WS-B"
    assert pc.message.startswith("All its rows of the last 14 days: 5; 3 without a row of another change type")
    assert "Their nearest other change of that software: within 10 min 2, none in 14 days 1." in pc.message
    assert pc.rows == [
        "timestamp='2026-10-20T10:00:00Z', name='Editor', change='Added', version='5.0'  ->  none in 14 days",
        "timestamp='2026-10-25T10:00:00Z', name='Browser', change='Removed', version='3.0'  ->  7 min apart: "
        "timestamp='2026-10-25T10:07:00Z', change='Added', version='3.1'",
        "timestamp='2026-10-25T10:07:00Z', name='Browser', change='Added', version='3.1'  ->  7 min apart: "
        "timestamp='2026-10-25T10:00:00Z', change='Removed', version='3.0'"]
    assert "· timestamp='2026-10-29" in render_checks([log])


def test_verify_shows_how_health_events_and_app_crashes_are_filled():
    from pathlib import Path
    from uxcorrelate.verify import _app_usage, _health_events

    def handler(request):
        path = request.url.path
        if path.endswith("/DEVICE_HEALTH_RAW"):
            ev = lambda cat, name, **cols: {"TIMEFRAME": "2026-10-30T09:00:00Z", "DEVICE_NAME": "WS-1",  # noqa: E731
                                            "HEALTH_EVENT_CATEGORY": cat, "HEALTH_EVENT_NAME": name, **cols}
            return httpx.Response(200, json={"value": [
                ev("Fault", "Program Fault", APPLICATION_NAME="Editor", HEALTH_EVENT_SUB_COMPONENT="lib9.dll",
                   HEALTH_EVENT_DETAILS="x" * 400),
                ev("Fault", "Program Fault", APPLICATION_NAME="", HEALTH_EVENT_SUB_COMPONENT="N/A"),
                ev("Freeze", "Program Freeze", APPLICATION_NAME="Viewer")]})
        if path.endswith("/APPLICATIONS_RAW"):
            return httpx.Response(200, json={"value": [
                {"TIMEFRAME": "2026-10-30T09:00:00Z", "APPLICATION_NAME": "Editor", "APPLICATION_CRASHES_TOTAL": 2,
                 "APPLICATION_HANG_TIME_TOTAL": 0, "APPLICATION_UXI_AVG": 6.5},
                {"TIMEFRAME": "2026-10-30T10:00:00Z", "APPLICATION_NAME": "Viewer", "APPLICATION_CRASHES_TOTAL": 0,
                 "APPLICATION_HANG_TIME_TOTAL": None, "APPLICATION_UXI_AVG": 9.1}]})
        return httpx.Response(404)

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/odata"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
    kinds, rows = _health_events(settings, client, schema)
    assert "3 row(s), 2 kind(s)" in kinds.message
    assert kinds.rows[0].startswith("Fault / Program Fault: 2 row(s); application 1, component 0, ")
    assert "sub_component 1," in kinds.rows[0] and "details 1," in kinds.rows[0]  # N/A and "" are not values
    assert "category='Freeze'" in rows.rows[1] and "details=None" in rows.rows[1]
    assert len(rows.rows[0].split("details=")[1]) <= 153
    usage = _app_usage(settings, client, schema)[0]
    assert usage.rows[0] == ("crashes (APPLICATION_CRASHES_TOTAL): 2 row(s) with a value, 1 not zero; "
                             "not zero: min 2, median 2, max 2")
    assert usage.rows[1] == "hang_time (APPLICATION_HANG_TIME_TOTAL): 1 row(s) with a value, 0 not zero"
    assert usage.rows[-1].startswith("A row with a crash or hang: timestamp='2026-10-30T09:00:00Z', application='Editor'")


def test_verify_says_when_a_checked_column_has_no_values():
    from pathlib import Path
    from uxcorrelate.verify import _value_ranges

    def handler(request):
        if request.url.path.endswith("/BUSINESS_ACTIVITIES_RAW"):
            return httpx.Response(200, json={"value": [
                {"TIMEFRAME": "2026-10-30T09:00:00Z", "HRC_CPU_UTIL": 40, "NETWORK_RTT_AVG": None}] * 3})
        return httpx.Response(200, json={"value": []})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/odata"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
    msgs = {c.item: c.message for c in _value_ranges(settings, client, schema)}
    assert msgs["values NETWORK_RTT_AVG"] == ("No values in 3 rows of the last day: nothing is checked against "
                                              "client_network_latency_ms 150.0.")
    assert msgs["values HRC_CPU_UTIL"].startswith("min 40, median 40, max 40 over 3 rows")


def test_verify_shows_how_virtual_desktops_are_filled():
    from pathlib import Path
    from uxcorrelate.verify import _change_log, _process_sources, _process_hours, _remote_display, _vdi, verify_vdi_network

    def handler(request):
        path, flt = request.url.path, request.url.params.get("$filter", "")
        if path.endswith("/BUSINESS_ACTIVITIES_RAW"):
            row = lambda u, d, v, ip, c: {"TIMEFRAME": "2026-10-30T09:00:00Z", "USERNAME": u,  # noqa: E731
                                          "DEVICE_NAME": d, "VIRTUALIZATION": v, "DEVICE_IP_ADDRESS": ip,
                                          "CLIENT_DEVICE_NAME": c, "ACTIVITY_REMOTE_DISPLAY_LATENCY_AVG": 40}
            if "USERNAME eq 'ann'" in flt:  # a pooled user: another desktop each day
                return httpx.Response(200, json={"value": [{"DEVICE_NAME": "VD-1"}, {"DEVICE_NAME": "VD-2"}]})
            if "DEVICE_NAME eq 'VD-1'" in flt:
                return httpx.Response(200, json={"value": [{"USERNAME": "ann"}, {"USERNAME": "bob"}]})
            if "eq" in flt:
                return httpx.Response(200, json={"value": [{"DEVICE_NAME": "LT-9", "USERNAME": "cy"}]})
            return httpx.Response(200, json={"value": [row("ann", "VD-1", "true", "10.0.0.5", "TC-5"),
                                                       row("cy", "LT-9", "false", "172.16.9.9", "N/A")]})
        if path.endswith("/DEVICE_INVENTORY") and "TC-5" in flt:  # the device ann sits at
            return httpx.Response(200, json={"value": [{"DEVICE_NAME": "TC-5", "DEVICE_IP_ADDRESS": "10.0.0.77"}]})
        if path.endswith("/APPLICATION_RESOURCES_HOURLY"):
            hour = lambda p, cpu: {"TIMEFRAME": "2026-10-30T09:00:00-04:00", "DEVICE_NAME": "VD-1",  # noqa: E731
                                   "PROCESS_NAME": p, "PRC_CPU_UTIL_AVG": cpu / 2, "PRC_CPU_UTIL_MAX": cpu}
            return httpx.Response(200, json={"value": [hour("idle.exe", 1), hour("scan.exe", 60)]})
        if path.endswith("/DEVICE_INVENTORY"):
            return httpx.Response(200, json={"value": [{"DEVICE_NAME": "VD-1", "SCCM_CLIENT_VERSION": "5.0"}]})
        if path.endswith("/REMOTE_DISPLAY_LATENCY_RAW"):
            return httpx.Response(200, json={"value": [
                {"TIMEFRAME": "2026-10-30T09:00:00Z", "DEVICE_NAME": "VD-1", "REMOTE_DISPLAY_PROTOCOL": "P1",
                 "CLIENT_DEVICE_SUBNET": "10.0.0.0", "REMOTE_DISPLAY_LATENCY_AVG": 80, "SLA_STATUS": "Minor"},
                {"TIMEFRAME": "2026-10-30T09:05:00Z", "DEVICE_NAME": "VD-2", "REMOTE_DISPLAY_PROTOCOL": "P1",
                 "CLIENT_DEVICE_SUBNET": "172.16.5.0", "REMOTE_DISPLAY_LATENCY_AVG": 20, "SLA_STATUS": "Normal"}]})
        if path.endswith("/WINDOWS_MACHINE_BOOTS"):
            return httpx.Response(200, json={"value": [
                {"TIMEFRAME": "2026-10-18T22:05:00Z", "MEASUREMENT_START_TIMESTAMP": "2026-10-18T22:01:00Z",
                 "MEASUREMENT_TYPE": "Boot", "DEVICE_IMAGE_BUILD_NUMBER": "B7"}]})
        if path.endswith("/INSTALLED_SOFTWARE_CHANGE_LOG"):
            row = lambda t, kind, d="VD-1": {"INSTALLED_SW_CHANGE_TIMESTAMP": t, "DEVICE_NAME": d,  # noqa: E731
                                             "INSTALLED_SW_NAME": "Editor", "INSTALLED_SW_CHANGE_TYPE": kind,
                                             "INSTALLED_SW_VERSION": "8.9"}
            if "le" in flt:  # the same minute on every PC
                return httpx.Response(200, json={"value": [row("2026-10-17T15:40:00Z", "Uninstalled", d)
                                                           for d in ("VD-1", "VD-3", "VD-4")]})
            return httpx.Response(200, json={"value": [row("2026-10-17T15:40:00Z", "Uninstalled"),
                                                       row("2026-10-18T22:00:00Z", "Installed")]})
        if path.endswith("/APPLICATION_RESOURCES_HOURLY"):
            return httpx.Response(200, json={"value": [{"TIMEFRAME": "2026-10-30T09:00:00Z"}]})
        return httpx.Response(200, json={"value": []})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/odata"
    settings.netim.base_url = "https://netim.test:8543"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())

    cols, *_, values, pooled, mgmt = _vdi(settings, client, schema)
    assert cols.message.startswith("Published: VIRTUALIZATION, CLIENT_DEVICE_NAME")
    assert "virtualization (VIRTUALIZATION): 'true' 1, 'false' 1" in values.rows
    assert "display_latency (ACTIVITY_REMOTE_DISPLAY_LATENCY_AVG): 2 with a value; min 40, median 40, max 40" in values.rows
    assert pooled.rows[0] == ("VIRTUALIZATION 'true': user ann worked on 2 PC(s) in 14 days "
                              "('VD-1' 1, 'VD-2' 1); PC VD-1 was used by 2 user(s).")
    assert mgmt.rows == ["sccm_client (SCCM_CLIENT_VERSION): '5.0' 1", "image_build (DEVICE_IMAGE_BUILD_NUMBER): None 1"]

    (rd,) = _remote_display(settings, client, schema)
    assert "protocol (REMOTE_DISPLAY_PROTOCOL): 'P1' 2" in rd.rows
    assert "status (SLA_STATUS): 'Minor' 1, 'Normal' 1" in rd.rows

    *_, back = _change_log(settings, client, schema)
    assert back.item == "put-backs on VD-1"
    assert back.rows[0] == ("'Editor' '8.9': Uninstalled 2026-10-17T15:40:00Z, nearest start-up 1821 min after "
                            "('Boot'); Installed 2026-10-18T22:00:00Z, nearest start-up 1 min after ('Boot')")
    assert back.rows[1].startswith("Image builds (DEVICE_IMAGE_BUILD_NUMBER) in its 1 start-ups: 'B7' 1x")
    assert back.rows[2] == ("Within 1 min of 2026-10-17T15:40:00Z: 2 other PC(s) have change-log rows, "
                            "2 of them for 'Editor'.")

    (proc,) = _process_sources(client, schema)
    assert proc.message == "Published data sources with PROCESS_NAME: 4:"
    assert any(r.startswith("APPLICATION_RESOURCES_HOURLY: time column TIMEFRAME, device column yes, "
                            "rows in the last day: yes") for r in proc.rows)

    netim = NetimClient(settings.netim, transport=httpx.MockTransport(netim_handler))
    hours, one = _process_hours(settings, client, schema)
    assert "cpu_max (PRC_CPU_UTIL_MAX): 2 with a value; min 1, median 30.5, max 60" in hours.rows
    assert "Rows per PC and hour: min 2, median 2, max 2 (1 PC-hours)" in hours.rows
    assert one.rows[0].startswith("process='scan.exe'")  # busiest CPU first

    desktops, physical, clients = verify_vdi_network(settings, client, netim)
    assert desktops.rows == ["10.0.0.x: 1 PC(s), e.g. VD-1 10.0.0.5: inside den-rtr01 Gi0/1"]
    assert physical.rows == ["172.16.9.x: 1 PC(s), e.g. LT-9 172.16.9.9: no NetIM subnet"]
    assert clients.rows == ["TC-5: 10.0.0.77, inside den-rtr01 Gi0/1"]
