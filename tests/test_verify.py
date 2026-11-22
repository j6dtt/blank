import json
from collections import Counter

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
    from uxcorrelate.verify import _change_log, _process_sources, _remote_display, _vdi, verify_vdi_network

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
    desktops, physical, clients = verify_vdi_network(settings, client, netim)
    assert desktops.rows == ["10.0.0.x: 1 PC(s), e.g. VD-1 10.0.0.5: inside den-rtr01 Gi0/1"]
    assert physical.rows == ["172.16.9.x: 1 PC(s), e.g. LT-9 172.16.9.9: no NetIM subnet"]
    assert clients.rows == ["TC-5: 10.0.0.77, inside den-rtr01 Gi0/1"]


def test_verify_reads_the_whole_fleet():
    from uxcorrelate.models import set_zone
    set_zone("")  # days and times in UTC here: an investigation test before leaves its zone set
    from pathlib import Path
    from uxcorrelate.verify import _fleet

    def event(day, dev, user, version="26.0 (Process Version)"):
        return {"TIMEFRAME": f"2026-11-{day:02}T09:00:00Z", "DEVICE_NAME": dev, "USERNAME": user,
                "HEALTH_EVENT_CATEGORY": "Background Process", "HEALTH_EVENT_NAME": "Background Process Crash",
                "HEALTH_EVENT_COMPONENT": "tagger.exe (Process)", "HEALTH_EVENT_COMPONENT_VERSION": version}

    def handler(request):
        path, flt = request.url.path, request.url.params.get("$filter", "")
        if path.endswith("/DEVICE_INVENTORY"):
            return httpx.Response(200, json={"value": [{"DEVICE_NAME": n} for n in ("VD-1", "VD-2", "VD-3")]})
        if path.endswith("/DEVICE_HEALTH_RAW"):
            return httpx.Response(200, json={"value": [event(3, "VD-1", "ann"), event(4, "VD-2", "ann")]})
        if path.endswith("/INSTALLED_SOFTWARE_CHANGE_LOG"):
            row = lambda d, t: {"INSTALLED_SW_CHANGE_TIMESTAMP": t, "DEVICE_NAME": d, "INSTALLED_SW_NAME": "Tagger",  # noqa: E731
                                "INSTALLED_SW_CHANGE_TYPE": "Installed", "INSTALLED_SW_VERSION": "26.0"}
            return httpx.Response(200, json={"value": [row("VD-1", "2026-11-03T10:00:00Z"),
                                                       row("VD-2", "2026-11-03T10:00:30Z")]})
        if path.endswith("/BUSINESS_ACTIVITIES_RAW"):
            return httpx.Response(200, json={"value": [
                {"DEVICE_NAME": "VD-1", "USERNAME": "ann", "VIRTUALIZATION": "true", "CLIENT_DEVICE_NAME": "TC-5"}]})
        if path.endswith("/APPLICATIONS_RAW"):
            if " gt 0" in flt:
                return httpx.Response(400, json={"error": {"message": "no"}})
            hang = lambda d, t: {"TIMEFRAME": t, "DEVICE_NAME": d, "APPLICATION_NAME": "Mail",  # noqa: E731
                                 "APPLICATION_CRASHES_TOTAL": 0, "APPLICATION_HANG_TIME_TOTAL": 5}
            return httpx.Response(200, json={"value": [hang("VD-1", "2026-11-02T09:00:00Z"),
                                                       hang("VD-1", "2026-11-04T09:00:00Z")]})
        return httpx.Response(200, json={"value": []})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/odata"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
    size, failures, per_day, software, hangs, after, sit = _fleet(settings, client, schema)
    assert size.rows[0] == "PCs in the inventory: 3"
    assert size.rows[1] == "last day's activity (1 rows read): 1 PCs, 1 users"
    assert "DEVICE_HEALTH_RAW has rows: 3d yes, 7d yes, 10d yes, 13d yes, 14d yes, 30d yes, 60d yes, 90d yes" in size.rows
    assert "Background Process Crash: tagger.exe (Process): 2 PCs" in failures.rows
    assert "  PCs per day: Nov 03 1, Nov 04 1" in failures.rows
    assert any(r.startswith("  2 PCs, 1 users in 14 days") for r in failures.rows)
    assert any(r.startswith("Tagger 26.0: 2 PCs, Nov 3, 10:00 AM to Nov 3, 10:00 AM") for r in software.rows)
    # one crash ran 26.0 before the log shows it on that PC, one after
    assert "  virtual: {'before: under a day': 1}" in software.rows
    assert "  not known: {'after it': 1}" in software.rows
    assert any("{'virtual': 1, 'not known': 1}" in r for r in software.rows)
    assert any(r.startswith("Background Process Crash: ") for r in per_day.rows)
    assert "no event of these days is a hang" in per_day.rows
    assert any(r.startswith("  further back, same weekday: ") for r in failures.rows)
    assert any(r.startswith("hang, Mail: ") for r in hangs.rows)
    assert any(r.startswith("Tagger 26.0, Mail: reached (") for r in hangs.rows)
    assert any(r == "CLIENT_DEVICE_NAME: 1 filled, 1 different" for r in sit.rows)


def test_verify_compares_actions_right_after_a_start_up_with_later_ones():
    """Pooled desktops: Mail Launch 2 min after a logon takes 60 s (Major), later 5 s.
    A physical PC's launch right after its start-up takes 6 s."""
    import re
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from uxcorrelate.verify import _after_start_up

    now = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
    at = lambda h, m=0: f"2026-11-08T{h:02}:{m:02}:00Z"  # noqa: E731
    boots = [{"TIMEFRAME": at(6, 18), "DEVICE_NAME": d} for d in ("VD-1", "VD-2", "PC-9")]

    def run(dev, t, rt, status, virtual, act="Launch"):
        return {"TIMEFRAME": t, "DEVICE_NAME": dev, "APPLICATION_NAME": "Mail", "ACTIVITY_NAME": act,
                "ACTIVITY_RESPONSE_TIME": rt, "SLA_STATUS": status, "VIRTUALIZATION": virtual}
    runs = [run("VD-1", at(6, 20), 60.0, "Major", "true"), run("VD-2", at(6, 25), 58.0, "Major", "true"),
            run("VD-1", at(11), 5.0, "Normal", "true"), run("VD-2", at(12), 40.0, "Minor", "true"),
            run("PC-9", at(6, 20), 6.0, "Normal", "false"), run("PC-9", at(9), 5.0, "Normal", "false"),
            run("VD-1", at(13), 9.0, "Major", "true", act="Send")]

    asked = []

    def handler(request):
        flt = request.url.params.get("$filter", "")
        start, end = re.findall(r"ge (\S+) and \S+ lt (\S+)", flt)[0]
        rows = boots if request.url.path.endswith("/WINDOWS_MACHINE_BOOTS") else runs
        rows = [r for r in rows if start <= r["TIMEFRAME"] < end]
        asked.append("slow" if "SLA_STATUS eq" in flt else "other")
        if "SLA_STATUS eq" in flt:
            return httpx.Response(400, json={"error": {"message": "no"}})  # read all, filtered here
        if "ACTIVITY_NAME eq" in flt:
            rows = [r for r in rows if r["ACTIVITY_NAME"] == "Launch"]
        return httpx.Response(200, json={"value": rows})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/odata"
    settings.thresholds.slow_status = ["Minor", "Major"]
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
    rows = _after_start_up(settings, client, schema, now)[0].rows
    refused = [r for r in rows if "filter refused" in r]
    assert refused == ["slow actions: filter refused (no), reading all rows"]
    assert asked.count("slow") == 1  # refused once, then every piece once, unfiltered
    assert [r for r in rows if r not in refused][1:] == [
        "start-up rows: 3 on 3 PCs",
        "slow actions (Aternity marked it Minor or Major): 4 on 2 PCs; right after a start-up:",
        "  virtual: 2 of 4 (50%)",
        "most often slow right after a start-up: Mail Launch 2",
        "Mail Launch, physical, right after: 1 runs, median 6.0 s, 0 of 1 (0%) slow",
        "Mail Launch, physical, later: 1 runs, median 5.0 s, 0 of 1 (0%) slow",
        "Mail Launch, virtual, right after: 2 runs, median 59.0 s, 2 of 2 (100%) slow",
        "Mail Launch, virtual, later: 2 runs, median 22.5 s, 1 of 2 (50%) slow",
    ]


def test_verify_overview_shows_aternitys_own_status_severity_and_incidents():
    """What a "today's issues" overview could rest on: Aternity's words, never ours."""
    from pathlib import Path
    from uxcorrelate.verify import verify_overview

    def handler(request):
        path = request.url.path
        if path.endswith("/$metadata"):
            text = (Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text()
            return httpx.Response(200, text=text.replace('<EntitySet Name="ANOMALY_DETECTION_INCIDENTS"',
                                                         '<EntitySet Name="GONE"'))
        if path.endswith("/BUSINESS_ACTIVITIES_RAW"):
            return httpx.Response(200, json={"value": [
                {"SLA_STATUS": "Normal", "DEVICE_NAME": "PC1"}, {"SLA_STATUS": "Normal", "DEVICE_NAME": "PC2"},
                {"SLA_STATUS": "Major", "DEVICE_NAME": "PC2"}]})
        if path.endswith("/DEVICE_HEALTH_RAW"):
            ev = lambda d, sev, name, part="OUTLOOK.EXE (Process)", n=1: {  # noqa: E731
                "DEVICE_NAME": d, "HEALTH_EVENT_SEVERITY": sev, "HEALTH_EVENT_CATEGORY": "Application",
                "HEALTH_EVENT_NAME": name, "HEALTH_EVENT_COMPONENT": part, "HEALTH_EVENT_VOLUME": n}
            return httpx.Response(200, json={"value": [ev("PC1", "High", "App Crash"), ev("PC2", "High", "App Crash", n=3),
                                                       ev("PC2", "Low", "App Hang"),
                                                       dict(ev("PC3", "Critical", "HD Failure", "\\Device\\Harddisk0\\DR0 (Drive)"),
                                                            HEALTH_EVENT_CATEGORY="Hardware"),
                                                       dict(ev("PC4", "Medium", "Low Disk Space", "C: (Drive)"),
                                                            HEALTH_EVENT_CATEGORY="System")]})
        if path.endswith("/INCIDENTS"):
            return httpx.Response(200, json={"value": [
                {"INCIDENT_CREATION_TIMESTAMP": "2026-11-08T09:00:00Z", "INCIDENT_STATE": "Open",
                 "INCIDENT_LIVE_IMPACT": "12", "APPLICATION_NAME": "ERP", "ACTIVITY_NAME": "Open"}]})
        return httpx.Response(200, json={"value": []})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    text = render_checks(verify_overview(settings, AternityClient(settings.aternity,
                                                                  transport=httpx.MockTransport(handler))))
    flat = " ".join(text.split())
    assert "3 activity rows in the last 60 min; a working day of 10 such hours is about 30 rows" in flat
    assert "'Normal': 2 rows on 2 PCs" in text and "'Major': 1 rows on 1 PCs" in text
    assert "'High': 2 rows on 2 PCs" in text and "Application / App Crash: 2 PCs, severity 'High' 2" in flat
    assert "INCIDENTS Published. 1 rows in the last 7 days; per day: Nov 08 1." in flat
    assert "INCIDENT_STATE: 'Open' 1" in text
    assert "SERVICE_DESK_ALERTS_RAW Published. 0 rows in the last 7 days: nothing to show from it." in flat
    assert "ANOMALY_DETECTION_INCIDENTS Not published by this Aternity" in flat
    # Aternity's label on each failing part: under a disk one, only a Hardware event is a failing disk
    assert ("by Aternity's label on the failing part (HEALTH_EVENT_COMPONENT). A failing disk: an event under Disk, "
            "Drive whose category is Hardware; each event under them with its category and severity.") in flat
    assert "Process: 2 PCs, events 'App Crash' 2, 'App Hang' 1" in flat
    # every event under a disk label with Aternity's category and severity: the evidence for which is a
    # failing disk (stage, Nov 16 2026: Low Disk Space and Corrupted FS under "(Drive)", category System)
    assert "Drive: 2 PCs, events 'HD Failure' 1, 'Low Disk Space' 1 (a disk or drive)" in flat
    assert ("· ↳ Hardware / HD Failure: 1 PCs, severity 'Critical' 1 (a failing disk) · ↳ System / Low Disk "
            "Space: 1 PCs, severity 'Medium' 1 (not a failing disk: category System)") in flat
    # can one row be several events? (the Care list and investigations add the column up)
    assert "1 of 5 health event rows in the last 7 days say more than 1 time (HEALTH_EVENT_VOLUME)" in flat
    assert "App Crash: 1 rows, up to 3 times" in flat


def test_overview_sources_are_riverbeds_own_names():
    from pathlib import Path
    from uxcorrelate.config import OVERVIEW_SOURCES
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
    for entity, spec in OVERVIEW_SOURCES.items():
        assert {spec["time"], *spec["values"]} <= schema[entity], entity


def test_software_names_are_riverbeds_own():
    from pathlib import Path
    from uxcorrelate.config import OS_COLUMNS, SOFTWARE_COLUMNS, SOFTWARE_SOURCE, VERSION_SOURCES
    schema = parse_odata_metadata((Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
    assert set(SOFTWARE_COLUMNS.values()) <= schema[SOFTWARE_SOURCE]
    assert set(OS_COLUMNS.values()) <= schema["DEVICE_INVENTORY"]
    for spec in VERSION_SOURCES.values():
        assert {v for k, v in spec.items() if k != "entity"} <= schema[spec["entity"]], spec["entity"]


def test_verify_software_shows_what_the_installed_list_holds_with_no_pc_names():
    """In short: the list's size and one user's installs, 2+ versions on one PC (checked PC by
    PC on the program with the most versions), Windows per PC and Windows updates per Windows
    version."""
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from uxcorrelate.verify import verify_software

    def sw(pc, name, version, n=1, kind="Application", scope="ALL"):
        return {"DEVICE_NAME": pc, "INSTALLED_SW_NAME": name, "INSTALLED_SW_VERSION": version,
                "INSTALLED_SW_TYPE": kind, "INSTALLED_SW_SCOPE": scope, "INSTALLED_SW_VERSION_COUNT": n,
                "INSTALLED_SW_COUNT": 1, "DEVICE_COUNT": 1}
    every = [sw("HOST-A", "Java", "8.1"), sw("HOST-A", "Java", "11.0"), sw("HOST-A", "Edge", "120.0"),
             sw("HOST-B", "Java", "8.1"), sw("HOST-B", "Edge", "121.0"), sw("HOST-C", "Edge", "120.0"),
             sw("HOST-C", "Zoom", "5.1", scope="Specific User"), sw("HOST-B", "KB5031", "N/A", kind="OS Update"),
             sw("HOST-C", "KB5031", "N/A", kind="OS Update")]
    now = datetime.now(timezone.utc)
    windows = [{"DEVICE_NAME": pc, "OS_NAME": "Windows 11", "OS_VERSION": v,
                "OS_VERSION_UPDATE_DATE": (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")}
               for pc, v, d in (("HOST-A", "24H2", 3), ("HOST-B", "24H2", 10), ("HOST-C", "23H2", 100))]
    seen = []

    def added(rows, sel):  # Aternity adds up per the columns asked for; DEVICE_COUNT: each PC once
        cols = [k for k in sel.split(",") if k != "DEVICE_COUNT"]
        per: dict = {}
        for r in rows:
            per.setdefault(tuple(r[k] for k in cols), set()).add(r["DEVICE_NAME"])
        return [dict(zip(cols, key), DEVICE_COUNT=len(pcs)) for key, pcs in per.items()]

    def handler(request):
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=(Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
        if request.url.path.endswith("/DEVICE_INVENTORY"):
            return httpx.Response(200, json={"value": windows})
        f, sel = request.url.params.get("$filter", ""), request.url.params.get("$select", "")
        seen.append(f)
        rows = every
        if "eq '" in f:
            column, value = f.split(" eq ")[0], f.split("'")[1]
            rows = [r for r in every if r[column] == value]
        if "DEVICE_COUNT" in sel and "DEVICE_NAME" not in sel or sel == "DEVICE_NAME":
            rows = added(rows, sel + ",DEVICE_COUNT")
        elif sel:
            rows = [{k: r.get(k) for k in sel.split(",")} for r in rows]
        return httpx.Response(200, json={"value": rows})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    text = render_checks(verify_software(settings, client))
    flat = " ".join(text.split())
    assert "HOST-" not in text  # no PC names
    assert "software: what is filled" not in text and "versions, 3 installs; most" not in text  # answered on stage
    assert "3 PCs, 4 programs, 9 installs (" in flat and "Not published" not in flat
    assert "1 programs installed for one user only, 1 installs." in flat
    assert ("1 programs have more installs than PCs, 1 more in all (" in flat
            and "a PC with 2 versions counts twice in installs, once in PCs. Check on Java: 1 more installs than "
                "PCs; read PC by PC, 1 PCs have 2+ versions (the same). Most: · Java: 1 more installs than its 2 PCs"
            in flat)
    assert "3 PCs in DEVICE_INVENTORY" in flat and "2 Windows versions; last updated known on 3" in flat
    assert "Windows 11 24H2: 2 PCs" in flat and "weeks (Monday): older 1," in flat
    assert "Windows 11 24H2 (2 PCs): KB5031 on 1, not on 1" in flat  # each update counted on its own version
    assert "Windows 11 23H2 (1 PCs): KB5031 on 1, not on 0" in flat
    # each way of asking for every Windows update is tried and said (the tab's own was refused on stage)
    assert "As the Software tab asked (at most 80,000 rows): 2 rows in 0 s, the whole reply." in flat
    assert "Aternity's own suggestion, $fetch_all_rows=yes: 2 rows in 0 s, the whole reply." in flat

    one = render_checks(verify_software(settings, client, pc="HOST-C"))  # named by the person asking
    assert "HOST-C: 3 rows" in one and "Settings > Apps" in one and "Zoom 5.1 (Application, Specific User)" in one
    assert "software: installed list" not in one and seen[-1] == "DEVICE_NAME eq 'HOST-C'"


def test_verify_versions_shows_the_application_version_on_what_users_feel():
    """App usage, actions, health events and the change log, each with Aternity's version."""
    from pathlib import Path
    from uxcorrelate.verify import verify_versions
    usage = [{"DEVICE_NAME": pc, "APPLICATION_NAME": app, "APPLICATION_VERSION": v, "TIMEFRAME": "2026-11-09T00:00:00Z",
              "APPLICATION_CRASHES_TOTAL": c, "APPLICATION_HANG_TIME_TOTAL": h}
             for pc, app, v, c, h in (("HOST-A", "Outlook", "16.1", 2, 0), ("HOST-B", "Outlook", "16.1", 0, 5),
                                      ("HOST-C", "Outlook", "16.2", 0, 0), ("HOST-C", "Word", "16.2", 1, "N/A"))]
    actions = [{"APPLICATION_NAME": "Outlook", "APPLICATION_VERSION": v, "SLA_STATUS": st, "ACTIVITY_VOLUME": n}
               for v, st, n in (("16.1", "Major", 30), ("16.1", "Normal", 970), ("16.2", "Major", 2),
                                ("16.2", "Normal", 98))]
    health = [{"HEALTH_EVENT_NAME": "Application Crash", "APPLICATION_NAME": "Outlook",
               "HEALTH_EVENT_COMPONENT": "OUTLOOK.EXE", "HEALTH_EVENT_COMPONENT_VERSION": "16.1"}] * 2
    log = [{"INSTALLED_SW_NAME": "Teams Add-in", "INSTALLED_SW_CHANGE_TYPE": t, "INSTALLED_SW_VERSION_BEFORE_CHANGE": b,
            "INSTALLED_SW_VERSION": v, "DEVICE_COUNT": n}
           for t, b, v, n in (("Installed", "1.26.08901", "1.26.18801", 40), ("Uninstalled", "N/A", "1.26.08901", 38))]

    def handler(request):
        path, p = request.url.path, request.url.params
        if path.endswith("/$metadata"):
            return httpx.Response(200, text=(Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
        if p.get("$orderby"):
            return httpx.Response(200, json={"value": [{"TIMEFRAME": "2026-08-01T00:00:00Z"}]})
        if path.endswith("/APPLICATIONS_DAILY"):
            rows = [r for r in usage if "'Outlook'" not in p["$filter"] or r["APPLICATION_NAME"] == "Outlook"]
            return httpx.Response(200, json={"value": [{k: r[k] for k in p["$select"].split(",")} for r in rows]})
        rows = {"/BUSINESS_ACTIVITIES_DAILY": actions, "/DEVICE_HEALTH_RAW": health,
                "/INSTALLED_SOFTWARE_CHANGE_LOG": log}
        return httpx.Response(200, json={"value": next((v for k, v in rows.items() if path.endswith(k)), [])})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    text = render_checks(verify_versions(settings, AternityClient(settings.aternity,
                                                                  transport=httpx.MockTransport(handler))))
    flat = " ".join(text.split())
    assert "HOST-" not in text
    assert ("APPLICATIONS_DAILY, last 7 days, without PCs: 4 rows (0.0 s); the version is filled on 4; the oldest "
            "day kept is Aug 01, 2026. Applications with the most crashes: · Outlook: 2 versions, 2 crashes") in flat
    assert "for 3 PC-and-version pairs" in flat and "one row per pair, Aternity adds up the days" in flat
    assert "16.1: 2 PCs, 1 with a crash, 1 with hang time" in flat and "16.2: 1 PCs, 0 with a crash" in flat
    assert "Outlook: 2 versions, 32 Major of 1,100 actions" in flat and "Outlook 16.1: 30 Major of 1,000" in flat
    assert ("2 health events in the last 24 hours (0.0 s); the failing part's version is filled on 2" in flat
            and "Application Crash · Outlook · OUTLOOK.EXE 16.1: 2" in flat)
    assert "Changes per type, with the version before filled: Installed 40 (40), Uninstalled 38 (0)" in flat
    assert "Teams Add-in: 1.26.08901 to 1.26.18801: 40 PCs" in flat


def test_verify_versions_reads_as_the_software_tab_and_says_what_it_cannot_compare():
    """verify --versions on stage (Nov 21 2026) read, as the Software tab does, only the application
    with the most crashes, Trellix ENS: every PC Aternity lists for it had a crash, so the tab compares
    nothing for it, but the check printed "a crash on 112 before and on 112 after" like a comparison
    and "Nothing is raised" with no reason; "1 of its 3 versions are". Now each of the applications
    with the most crashes is read so, and one like that is said."""
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from uxcorrelate.verify import verify_versions
    day = lambda back: (datetime.now(timezone.utc) - timedelta(days=back)).strftime("%Y-%m-%dT12:00:00Z")  # noqa: E731
    row = lambda pc, app, v, back, c: {  # noqa: E731
        "DEVICE_NAME": pc, "APPLICATION_NAME": app, "APPLICATION_VERSION": v, "TIMEFRAME": day(back),
        "APPLICATION_CRASHES_TOTAL": c, "APPLICATION_HANG_TIME_TOTAL": 0, "VIRTUALIZATION": "N/A"}
    # Shield: listed only where it crashed, 22 PCs, 20 of them on a new version since yesterday
    usage = [row(f"HOST-{i}", "Shield", "10.7", 5, 3) for i in range(22)] \
        + [row(f"HOST-{i}", "Shield", "26.8", 1, 3) for i in range(20)] \
        + [row(f"HOST-{i}", "Mail", "16.1", 2, 1 if i < 2 else 0) for i in range(25)] \
        + [row(f"HOST-{i}", "Mail", "16.2", 2, 0) for i in range(25, 28)]
    asked = []

    def handler(request):
        path, p = request.url.path, request.url.params
        if path.endswith("/$metadata"):
            return httpx.Response(200, text=(Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
        if p.get("$orderby"):
            return httpx.Response(200, json={"value": [{"TIMEFRAME": "2026-08-01T00:00:00Z"}]})
        if path.endswith("/APPLICATIONS_DAILY"):
            name = next((a for a in ("Shield", "Mail") if f"'{a}'" in p["$filter"]), None)
            if name and "TIMEFRAME" in p["$select"]:
                asked.append(name)
            rows = [r for r in usage if name in (None, r["APPLICATION_NAME"])]
            return httpx.Response(200, json={"value": [{k: r[k] for k in p["$select"].split(",")} for r in rows]})
        return httpx.Response(200, json={"value": []})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    text = render_checks(verify_versions(settings, AternityClient(settings.aternity,
                                                                  transport=httpx.MockTransport(handler))))
    flat = " ".join(text.split())
    assert "HOST-" not in text and asked == ["Shield", "Mail"]  # each read day by day, the most crashes first
    assert ("Shield, as the Software tab reads it: app usage day by day for 14 days, 42 rows; with its actions, "
            "0.0 s. The tab makes one such read per application, up to 40, once a day.") in flat
    assert ("On physical PCs: 22 in all, each counted once. 2 of its 2 versions are on 20 PCs or more; most have "
            "10.7; new this week: 26.8.") in flat
    # its update with no count that reads like a comparison, and why nothing is raised
    assert "· 20 of them went from 10.7 to 26.8. ·" in flat and "a crash on 20 before" not in flat
    assert ("Every PC Aternity lists for it had a crash, so there is no PC without one to compare with: the tab "
            "compares no share of crashes or hang time for it, on any version or update. · Nothing is raised "
            "for it.") in flat
    assert "Mail, as the Software tab reads it: app usage day by day for 14 days, 28 rows; with its actions" in flat
    assert ("On physical PCs: 28 in all, each counted once. 1 of its 2 versions is on 20 PCs or more; most have "
            "16.1.") in flat
    assert "Nothing is raised for it by the tab's rules. · These 2 reads together: 70 rows of app usage" in flat


def test_verify_says_which_red_flags_the_rule_before_raised_and_the_rule_now_does_not(tmp_path):
    """The user, Nov 21 2026: "How did we go from 28 red flags to 15 after this update? Did we
    break something or there was a really bad bug?" The tab's saved read, judged by both rules."""
    import json
    from uxcorrelate import software as sw
    from uxcorrelate.verify import flags_by_rule

    def row(v, pcs, crash, new=False):
        return {"version": v, "pcs": pcs, "crash": crash, "hang": 0, "virtual": 0, "action_pcs": 0, "actions": 0,
                "major": 0, "major_pcs": 0, "new": new}

    def app(name, rows, kinds):
        return {"application": name, "read": ["usage"], "only": [], "raised": [], "versions": [row(*r) for r in rows],
                "kinds": {on: {"pcs": sum(r[1] for r in rs), "versions": [row(*r) for r in rs], "updates": ups}
                          for on, (rs, ups) in kinds.items()}}
    up = {"from": "2", "to": "3", "pcs": 30, "both": 4, "first": "2026-11-15", "last": "2026-11-16",
          "before": {"crash": 1, "hang": 0}, "after": {"crash": 9, "hang": 0}}
    apps = [
        # the photo: 19 of 1,738 against 2 of 515 added together; now 2 more PCs on the other version undo it
        app("Word", [("1", 1738, 19), ("2", 515, 2)], {"physical": ([("1", 1738, 19), ("2", 515, 2)], [])}),
        # version 2: 2 times its other versions together (10 of 100 against 24 of 790), not the version most PCs
        # have (24 of 400); that one is raised by both rules
        app("Mail", [("1", 400, 24), ("2", 100, 10), ("3", 390, 0)],
            {"virtual": ([("1", 400, 24), ("2", 100, 10), ("3", 390, 0)], [])}),
        # 30 PCs in all, 15 of each kind: too few once they are apart
        app("Notes", [("1", 300, 3), ("2", 30, 6)],
            {"virtual": ([("1", 150, 2), ("2", 15, 3)], []), "physical": ([("1", 150, 1), ("2", 15, 3)], [])}),
        # raised by both, and a version new this week raised by its update only
        app("Chat", [("1", 200, 2), ("2", 60, 12), ("3", 30, 2)],
            {"physical": ([("1", 200, 2), ("2", 60, 12), ("3", 30, 2, True)], [up])}),
        # worse than its other version on different PCs, but not on the 53 PCs that ran both (7 with it, 9 with
        # the other): raised before, not now (the user, Nov 22 2026: "Am I seeing discrepancies here?")
        app("Sheets", [("1", 2475, 600), ("2", 165, 5)], {"virtual": ([("1", 2475, 600), ("2", 165, 5)], [
            {"from": "1", "to": "2", "pcs": 53, "both": 0, "first": "2026-11-12", "last": "2026-11-20",
             "before": {"crash": 7, "hang": 0}, "after": {"crash": 9, "hang": 0}}])}),
    ]
    assert flags_by_rule(str(tmp_path))[0].message.startswith("The tab's saved reads are not here")
    (tmp_path / "software").mkdir()
    (tmp_path / "software" / "2026-11-20.json").write_text(json.dumps({"versions": {"Old": {"versions": []}}}))
    assert "is from before virtual desktops and physical PCs were looked at apart" in flags_by_rule(str(tmp_path))[0].message
    (tmp_path / "software" / "2026-11-21.json").write_text(json.dumps({"versions": {a["application"]: a for a in apps}}))
    check = flags_by_rule(str(tmp_path))[0]
    assert check.system == "Software tab" and check.ok is None
    assert check.message == (
        "The tab's saved read of 2026-11-21, 5 applications, judged twice (no Aternity read). The rule before (each "
        "version against its other versions added together, all PCs mixed) raises 6 versions. The rule now raises "
        "3, counted per kind of PC as on the tab. 2 of the 6 are raised by both.")
    assert check.rows == [
        "4 raised before, not now:",
        "  1: more of it on the other side would undo it (2 more PCs, or 5 more actions rated Major)",
        "  1: 2 times its other versions added together, but not 2 times the one version it is compared with now",
        "  1: the PCs that ran both versions do not show it (the same PCs, the same number of days on each)",
        "  1: under 20 PCs, or under 5 with the problem, once virtual desktops and physical PCs are apart",
        "  Word 1, crashes: 1.1% (19 of 1,738) against 0.4% (2 of 515) on its other versions together. Now on "
        "physical PCs: more on the other side would undo it.",
        "  Mail 2, crashes: 10% (10 of 100) against 3% (24 of 790) on its other versions together. Now on virtual "
        "desktops: not 2 times the version it is compared with.",
        "  Sheets 1, crashes: 24% (600 of 2,475) against 3% (5 of 165) on its other versions together. Now on virtual "
        "desktops: the PCs that ran both versions do not show it.",
        "  Notes 2, crashes: 20% (6 of 30) against 1% (3 of 300) on its other versions together. Now on virtual "
        "desktops: too few PCs there.",
        "1 raised now, not before: 1 after an update (the rule before had no before and after).",
    ]
    assert sw.MIN_PCS == 20 and sw.RATIO == 2  # the numbers in those sentences are the code's


def test_every_check_group_is_printed_and_one_failed_part_stops_only_itself():
    """config.yaml's notes were counted in "problem(s) found" but never printed; one error in a
    part stopped the whole verify, which prints only at the end; a read cut short was not said."""
    from uxcorrelate.http import ApiError
    from uxcorrelate.verify import Check, _limit, part, render_checks
    text = render_checks([Check("config.yaml", "settings", False, "refresh_minutes 2 is out of range")])
    assert "refresh_minutes 2 is out of range" in text and "1 problem(s) found." in text

    def broken():
        raise ApiError("Aternity: HTTP 500", 500)
    assert part("fleet", broken)[0].message.startswith("Not checked: Aternity gave an error")
    assert part("fleet", lambda: 1 / 0)[0].ok is False  # a fault of ours: said, the others run

    class Client:
        warnings: list = []
    c = Client()

    def cut():
        c.warnings.append("Aternity X: stopped after 50 pages (100 rows); the result is partial.")
        return [Check("Aternity", "fleet", None, "100 rows")]
    got = part("fleet", cut, client=c)
    assert got[-1].item == "fleet: not read in full" and got[-1].rows[0].startswith("PARTIAL: Aternity X")
    assert _limit([{}] * 5, 5) and not _limit([{}] * 4, 5)


def test_verify_clients_names_the_devices_not_starting_with_wt_and_sets_them_against_vpn():
    """verify --clients (the user's request, Nov 16 2026): virtual desktops by the device people sat at
    (CLIENT_DEVICE_NAME), the ones not starting with WT by name, and each LOCATION_ON_VPN value as sent
    against them; physical PCs apart. Made-up names and values."""
    from pathlib import Path
    from uxcorrelate.verify import verify_clients

    def act(dev, user, client, vpn, virtual="true", kind="Remote Device"):
        return {"TIMEFRAME": "2026-11-16T14:00:00Z", "DEVICE_NAME": dev, "USERNAME": user, "VIRTUALIZATION": virtual,
                "CLIENT_DEVICE_NAME": client, "CLIENT_DEVICE_TYPE": kind, "LOCATION_ON_VPN": vpn}
    rows = ([act("VD-1", "u1", "WT-0101", "No")] * 3 + [act("VD-2", "u2", "wt-0102", "No")]
            + [act("VD-3", "u3", "HOME-PC7", "Yes")] * 2 + [act("VD-4", "u3", "HOME-PC7", "Yes")]
            + [act("VD-5", "u4", "LT-55", "Yes"), act("VD-5", "u4", "LT-55", "No")]
            + [act("VD-6", "u5", "WT-0300", "Yes")]
            + [act("VD-7", "u6", None, "No"), act("PC-9", "u7", None, "Yes", virtual="false", kind=None)])
    asked = []

    def handler(request):
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=(Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text())
        asked.append(request.url.params.get("$select"))
        return httpx.Response(200, json={"value": rows})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    checks = verify_clients(settings, AternityClient(settings.aternity, transport=httpx.MockTransport(handler)),
                            ["wt"], hours=99)
    flat = " ".join(render_checks(checks).split())
    assert asked == ["TIMEFRAME,DEVICE_NAME,USERNAME,VIRTUALIZATION,CLIENT_DEVICE_NAME,CLIENT_DEVICE_TYPE,LOCATION_ON_VPN"]
    assert ("Last 24 h: 12 actions. Virtual desktops: 11 actions on 7 desktops by 6 users; CLIENT_DEVICE_NAME "
            "filled on 10 of them, 5 devices (3 starting with WT, 2 not). Physical PCs: 1 actions on 1 PCs. "
            "How the device names begin: · WT…: 3 devices, 5 actions · HOME…: 1 devices, 3 actions · "
            "LT…: 1 devices, 2 actions · CLIENT_DEVICE_TYPE: starting with WT: 'Remote Device' 5; "
            "not: 'Remote Device' 5") in flat
    assert ("clients: not starting with WT 2 devices, 5 actions on 3 virtual desktops by 2 users. The 2 with the "
            "most actions, to check (device: actions · the desktops it was on · users · LOCATION_ON_VPN as sent): "
            "· HOME-PC7: 3 actions · on VD-3, VD-4 · 1 user(s) · LOCATION_ON_VPN: 'Yes' 3 "
            "· LT-55: 2 actions · on VD-5 · 1 user(s) · LOCATION_ON_VPN: 'Yes' 1, 'No' 1") in flat
    assert ("A device with actions under two values is under both (1 such): "
            "· 'No': virtual desktops 6 actions from 3 devices (2 starting with WT, 1 not) · physical PCs 0 actions "
            "on 0 PCs (0 starting with WT, 0 not) "
            "· 'Yes': virtual desktops 5 actions from 3 devices (1 starting with WT, 2 not) · physical PCs 1 actions "
            "on 1 PCs (0 starting with WT, 1 not)") in flat


def test_verify_inventory_says_which_device_columns_are_filled_and_with_what():
    """Before an Inventory tab (the user, Nov 21 2026: "For Inventory, first step is verify?"):
    Riverbed lists 97 columns for a PC, the live Aternity may fill fewer. verify --inventory's 5th
    answer (alone with --columns): per column a tab could show, on how many PCs it is filled and
    what its values look like. No PC, user or site names."""
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from uxcorrelate.config import INVENTORY_FILLED
    from uxcorrelate.models import day
    from uxcorrelate.verify import verify_inventory
    meta = (Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text()
    asked = [c for _, items in INVENTORY_FILLED for _, c, _ in items]
    assert set(asked) <= parse_odata_metadata(meta)["DEVICE_INVENTORY"] and len(asked) == len(set(asked))  # Riverbed's names
    now = datetime.now(timezone.utc)
    at = lambda days: (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    pcs = [{"DEVICE_NAME": f"HOST-{i}", "TIME_LAST_REPORTING": at(0), "DEVICE_MODEL": "Model A" if i < 6 else "Model B",
            "DEVICE_MANUFACTURER": "Maker", "DEVICE_SERIAL_NUMBER": f"SN-{i}", "OS_TOTAL_DISK_SPACE": str(100 + i * 10),
            "OS_FREE_DISK_SPACE": "N/A" if i == 0 else f"{i * 5}.5", "BOOT_LAST_TIMESTAMP": at(i),
            "OS_BITLOCKER_PROTECTION_STATUS": "On" if i % 2 else "Off", "BUSINESS_LOCATION": f"Site {i % 3}",
            "USERNAME": f"user{i}", "DEVICE_MEMORY": "16 GB", "DEVICE_TYPE": "N/A"} for i in range(10)]
    pcs.append(dict(pcs[0], TIME_LAST_REPORTING=at(9), DEVICE_MODEL="Old model"))  # a PC listed twice: its latest row
    seen = []

    def handler(request):
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=meta)
        assert request.url.path.endswith("/DEVICE_INVENTORY")
        seen.append(request.url.params["$select"].split(","))
        return httpx.Response(200, json={"value": [{k: r.get(k, "N/A") for k in seen[-1]} for r in pcs]})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    checks = verify_inventory(settings, AternityClient(settings.aternity, transport=httpx.MockTransport(handler)), True)
    assert [c.item for c in checks] == ["5. What the device list holds about each PC"] and len(seen) == 1  # one read
    assert seen[0] == ["DEVICE_NAME", "TIME_LAST_REPORTING"] + asked
    text = render_checks(checks)
    flat = " ".join(text.split())
    assert not any(w in text for w in ("HOST-", "SN-", "user1", "Site 1"))  # no PC, serial, user or site
    assert "Aternity's device list: 10 PCs (11 rows in 0.0 s). Of what an Inventory tab could show, 22 columns" in flat
    for line in (
        "What the PC is: · Manufacturer: filled on 10 of 10 PCs; always 'Maker'.",
        "Model: filled on 10 of 10 PCs; 2 different; most common: 'Model A' on 6, 'Model B' on 4.",
        "Serial number: filled on 10 of 10 PCs; 10 different (not listed here).",
        "Memory: filled on 10 of 10 PCs; always '16 GB'.",  # not a number: said as sent, so its form is seen
        "Empty on every PC: Kind of device, Processor, Physical memory.",
        "Size: filled on 10 of 10 PCs; numbers from 100 to 190, the middle one 145 (as sent, for example '100').",
        "Free space: filled on 9 of 10 PCs; numbers from 5.5 to 45.5, the middle one 25.5 (as sent, for example",
        f"Last start-up: filled on 10 of 10 PCs; from {day(now - timedelta(days=9))} to {day(now)}.",
        "Location: filled on 10 of 10 PCs; 3 different (not listed here).",
        "Last user: filled on 10 of 10 PCs; 10 different (not listed here).",
        "Empty on every PC: Health status, Health score, Microsoft stability index.",
    ):
        assert line in flat, line
    assert "BitLocker" not in text  # sent by this Aternity, no longer asked for (the user, Nov 22 2026)


def test_verify_actions_says_which_applications_have_measured_actions():
    """The user, Nov 21 2026: "what actions are measure in Aternity", "which applications in prod
    have measured actions": verify --actions. Application and activity names only."""
    from pathlib import Path
    from uxcorrelate.config import ACTION_COLUMNS, VERSION_SOURCES
    from uxcorrelate.verify import verify_actions
    meta = (Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text()
    schema = parse_odata_metadata(meta)
    assert ACTION_COLUMNS["type"] in schema["BUSINESS_ACTIVITIES_DAILY"]  # Riverbed's own names
    assert ACTION_COLUMNS["usage_time"] in schema["APPLICATIONS_DAILY"]
    assert VERSION_SOURCES["actions"]["activity"] in schema["BUSINESS_ACTIVITIES_DAILY"]
    acts = [{"APPLICATION_NAME": a, "ACTIVITY_NAME": act, "SLA_STATUS": st, "ACTIVITY_VOLUME": n, "APPLICATION_TYPE": ty}
            for a, act, st, n, ty in (
                ("Mail", "Open Mail", "Normal", 900, "Desktop"), ("Mail", "Open Mail", "Major", 30, "Desktop"),
                ("Mail", "Send", "Normal", 400, "Desktop"), ("Mail", "Send", "Minor", 20, "Desktop"),
                ("Portal", "Page Load", "Normal", 50, "Web"), ("Portal", "Page Load", "Major", 5, "Web"),
                ("N/A", "Launch", "Normal", 7, "N/A"))]
    usage = [{"APPLICATION_NAME": a, "APPLICATION_USAGE_TIME_TOTAL": t} for a, t in (
        ("Mail", 5000), ("mail", 10), ("Notepad", 300), ("Calculator", 900), ("Calculator", 100), ("Quiet", "N/A"))]
    seen = {}

    def handler(request):
        path, p = request.url.path, request.url.params
        if path.endswith("/$metadata"):
            return httpx.Response(200, text=meta)
        name = path.rsplit("/", 1)[-1]
        seen[name] = p["$select"]
        assert "TIMEFRAME ge " in p["$filter"] and "DEVICE_NAME" not in p["$select"] and "USERNAME" not in p["$select"]
        return httpx.Response(200, json={"value": acts if name == "BUSINESS_ACTIVITIES_DAILY" else usage})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    text = render_checks(verify_actions(settings, client))
    flat = " ".join(text.split())
    assert seen == {"BUSINESS_ACTIVITIES_DAILY": "APPLICATION_NAME,ACTIVITY_NAME,SLA_STATUS,ACTIVITY_VOLUME,APPLICATION_TYPE",
                    "APPLICATIONS_DAILY": "APPLICATION_NAME,APPLICATION_USAGE_TIME_TOTAL"}
    for line in (
        "In the last 7 days Aternity measured actions of 2 applications: 1,405 actions, 35 of them rated Major "
        "(BUSINESS_ACTIVITIES_DAILY, 7 rows in 0.0 s). Most actions first, with the kind of application as Aternity "
        "sends it: · Mail (Desktop): 1,350 actions, 30 rated Major, 2 activities · Portal (Web): 55 actions, 5 rated "
        "Major, 1 activity · 7 actions come with no application name: not counted above.",
        "For the 2 applications with the most actions, their activities by name, each with its actions and those "
        "rated Major: · Mail: Open Mail 930 (30 Major); Send 420 (0 Major). · Portal: Page Load 55 (5 Major).",
        # a name is counted as Aternity sends it ("Mail" and "mail" are two); measured or not is asked without capitals
        "App usage lists 5 applications in the last 7 days (APPLICATIONS_DAILY, 6 rows in 0.0 s): 2 of them have "
        "measured actions, 3 have none.",
        "The 3 used the most of them, by Aternity's usage time (not shown: it has no documented unit): Calculator; "
        "Notepad; Quiet.",
        "1 applications with measured actions are not in app usage: Portal.",
    ):
        assert line in flat, line
    assert "5000" not in flat and "5,000" not in flat and "All checks passed." in flat  # the usage time is never printed


def test_verify_inventory_says_in_plain_sentences_which_pcs_are_gone():
    """The user, Nov 20 2026: how does Aternity handle PCs that no longer exist, is there a "last
    heard"? Riverbed's pages give no rule, so verify --inventory counts it, in plain sentences (the
    first output "makes no sense"): the device list by when each PC was last heard, Aternity's
    status of each, and the PCs with software that are not in the device list, in use or not.
    No names."""
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from uxcorrelate.config import INVENTORY_COLUMNS
    from uxcorrelate.verify import verify_inventory
    meta = (Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text()
    assert set(INVENTORY_COLUMNS.values()) <= parse_odata_metadata(meta)["DEVICE_INVENTORY"]  # Riverbed's own names
    now = datetime.now(timezone.utc)
    at = lambda days: (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    pcs = [("HOST-A", at(0), "Up"), ("HOST-B", at(0), "Up"), ("HOST-C", at(3), "Away"), ("HOST-D", at(20), "Away"),
           ("HOST-E", at(25), "Away"), ("HOST-G", "N/A", "Up")]
    state = {"pcs": pcs, "listed": ["HOST-A", "HOST-B", "HOST-C", "HOST-E"] + [f"HOST-X{i}" for i in range(20)],
             "used": ["HOST-A", "HOST-B", "HOST-G", "HOST-X1"]}

    def handler(request):
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=meta)
        if request.url.path.endswith("/DEVICE_INVENTORY"):
            return httpx.Response(200, json={"value": [
                {"DEVICE_NAME": n, "TIME_LAST_REPORTING": t, "AGENT_CURRENT_STATUS": s} for n, t, s in state["pcs"]]})
        assert request.url.params.get("$select") == "DEVICE_NAME"  # each PC once, added up by Aternity
        if request.url.path.endswith("/APPLICATIONS_DAILY"):
            assert "TIMEFRAME ge " in request.url.params.get("$filter", "")
            return httpx.Response(200, json={"value": [{"DEVICE_NAME": n} for n in state["used"]]})
        return httpx.Response(200, json={"value": [{"DEVICE_NAME": n} for n in state["listed"]]})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    text = render_checks(verify_inventory(settings, client))
    flat = " ".join(text.split())
    assert "HOST-" not in text  # no PC names
    for line in ("Aternity's device list has 6 PCs.", "Heard from today: 2 PCs.", "Last heard 1 to 7 days ago: 1 PCs.",
                 "Last heard 8 to 30 days ago: 2 PCs.", "Last heard 31 days ago or more: 0 PCs.",
                 "No date from Aternity: 1 PCs", "was last heard 25 days ago",
                 "Aternity seems to take a PC off this list after about that long.",
                 "'Up': 3 PCs; all with a date were heard today (1 have no date).",
                 "'Away': 3 PCs; last heard between",
                 "Aternity's installed software list has 24 PCs.", "In the device list too: 4 PCs.",
                 "Not in the device list: 20 PCs. The Software tab leaves them out of its counts (see 4).",
                 "More than half of the PCs with software are not in the device list",
                 "Of the 2 PCs in the device list silent 8 days or more, 1 still have their software listed.",
                 "Of those 20 PCs, 1 ran an application in the last 8 days and 19 did not.",
                 "the 19 with no use are most likely retired PCs whose software Aternity still keeps.",
                 "of the 6 PCs in the device list, 3 ran an application in the last 8 days."):
        assert line in flat, line
    # most of them in use: live PCs under another name, never called retired
    state["used"] = state["listed"]
    flat = " ".join(render_checks(verify_inventory(settings, client)).split())
    assert "many of them are in use: they are live PCs the device list does not have" in flat and "retired PCs whose" not in flat


def test_verify_usage_says_the_pcs_in_use_per_day_and_month_with_their_share_of_the_list():
    """The user, Nov 22 2026, before usage charts on the Inventory tab ("Looking for daily/monthly
    usage", "Add percentage of usage", "Build it"): verify --usage. Counts only, no PC or user name."""
    import re
    from datetime import datetime, timezone
    from pathlib import Path
    from uxcorrelate.config import INVENTORY_COLUMNS, INVENTORY_ROLES, VERSION_SOURCES
    from uxcorrelate.models import parse_time, set_zone
    from uxcorrelate.verify import verify_usage
    set_zone(None)
    meta = (Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text()
    schema = parse_odata_metadata(meta)
    u = VERSION_SOURCES["usage"]
    assert {u["device"], u["time"], u["username"]} <= schema[u["entity"]]  # Riverbed's own names
    assert {INVENTORY_ROLES["kind"], INVENTORY_COLUMNS["last"]} <= schema["DEVICE_INVENTORY"]
    pcs = [("PC-1", "Laptop", "2026-11-01T08:00:00Z"),  # an older row of PC-1: its latest one says Desktop
           ("VD-1", "Virtual Desktop", "2026-11-20T08:00:00Z"), ("VD-2", "Virtual Desktop", "2026-11-20T08:00:00Z"),
           ("VD-3", "Virtual Desktop", "N/A"), ("PC-1", "Desktop", "2026-11-20T08:00:00Z"),
           ("LT-1", "Laptop", "2026-11-19T08:00:00Z")]
    listed = [{"DEVICE_NAME": n, "DEVICE_TYPE": k, "TIME_LAST_REPORTING": t} for n, k, t in pcs]
    used = [{"TIMEFRAME": f"2026-{d}T00:00:00Z", "DEVICE_NAME": pc, "USERNAME": who} for d, pc, who in (
        ("10-05", "VD-2", "bob"), ("10-20", "PC-1", "cy"), ("11-14", "LT-1", "eve"),
        ("11-18", "VD-1", "ann"), ("11-18", "vd-3", "N/A"),  # the name in other capitals; no username
        ("11-19", "VD-1", "ann"), ("11-19", "VD-2", "bob"), ("11-19", "PC-1", "cy"),
        ("11-19", "OLD-9", "dee"),  # not in the device list
        ("11-20", "VD-1", "ann"))]
    asked = []

    def handler(request):
        path, p = request.url.path, request.url.params
        if path.endswith("/$metadata"):
            return httpx.Response(200, text=meta)
        if path.endswith("/DEVICE_INVENTORY"):
            assert p["$select"] == "DEVICE_NAME,DEVICE_TYPE,TIME_LAST_REPORTING"
            return httpx.Response(200, json={"value": listed})
        assert path.endswith("/APPLICATIONS_DAILY")
        asked.append(p["$select"])
        lo = parse_time(re.search(r"TIMEFRAME ge (\S+)", p["$filter"]).group(1))
        hi = re.search(r"TIMEFRAME lt (\S+)", p["$filter"])
        rows = [r for r in used if lo <= parse_time(r["TIMEFRAME"]) and (hi is None or parse_time(r["TIMEFRAME"]) < parse_time(hi.group(1)))]
        if p["$select"] == "DEVICE_NAME":  # added up by Aternity: a row per PC
            rows = [{"DEVICE_NAME": n} for n in dict.fromkeys(r["DEVICE_NAME"] for r in rows)]
        if "$orderby" in p:
            rows = sorted(rows, key=lambda r: r["TIMEFRAME"])[:int(p["$top"])]
        return httpx.Response(200, json={"value": rows})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    text = render_checks(verify_usage(settings, client, now=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)))
    flat = " ".join(text.split())
    # the oldest day, 14 days one by one (with the username), then this month and the one before it kept in part
    assert asked == ["TIMEFRAME"] + ["DEVICE_NAME,TIMEFRAME,USERNAME"] * 14 + ["DEVICE_NAME"] * 2
    for line in (
        "Aternity keeps daily app usage (APPLICATIONS_DAILY) back to Mon Oct 05, 2026: 46 days before today. That is 0 "
        "whole months before this one.",
        "A day's rows carry one time, as sent: '2026-11-19T00:00:00Z'.",
        "Only the 5 PCs in Aternity's device list are counted, by its kind of device",
        "The last 14 days were read a day at a time: 8 rows in 0.0 s",
        "Kinds, in this order: Virtual Desktop (3 in the list) | Desktop (1) | Laptop (1)",
        "Fri Nov 13 no rows from Aternity",  # a day with no row is not "0 in use"
        "Sat Nov 14 0 0% | 0 0% | 1 100%",
        "Wed Nov 18 2 67% | 0 0% | 0 0%",  # vd-3 is VD-3 of the list
        "Thu Nov 19 2 67% | 1 100% | 0 0%",  # PC-1 by its latest row: a Desktop
        "Fri Nov 20 1 33% | 0 0% | 0 0% so far",
        "Virtual Desktop: a typical working day 2 of 3 (67%); the busiest day Wed Nov 18 with 2 (67%); a typical weekend "
        "day 0 (0%); on at least one of the 4 days 3 (100%).",  # the 4 days Aternity has rows for
        "1 PC with app usage in these days is not in the device list (retired or renamed, most likely): it is left out.",
        "Aternity fills the username on 7 of 8 rows of daily app usage (88%).",
        "Wed Nov 18 1; Thu Nov 19 4; Fri Nov 20 1.",
        "A typical working day: 4 people. 5 different people in the 4 days.",
        "8 rows in 0.0 s for 2 months, a row per PC.",
        "November so far: 3 100% | 1 100% | 1 100%",
        "October, from Oct 05 only: 1 33% | 1 100% | 0 0%",
        "September: not kept (daily app usage starts Oct 05)",
        "This month is kept whole: its count can come from Aternity itself, with no PC name kept by the tab.",
    ):
        assert line in flat, line
    for name in ("VD-1", "vd-3", "OLD-9", "ann", "dee"):  # counts only
        assert name not in text
    assert "All checks passed." in flat


def test_verify_usage_says_what_it_could_not_read_and_a_month_aternity_does_not_keep_whole():
    """A day Aternity refuses is said on its line, never 0; with no username column there is no
    people line; daily app usage that starts after the month began cannot give the month's count."""
    import re
    from datetime import datetime, timezone
    from pathlib import Path
    from uxcorrelate.models import parse_time, set_zone
    from uxcorrelate.verify import verify_usage
    set_zone(None)
    meta = (Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text()
    # this Aternity publishes daily app usage without the username
    start = meta.index('<EntityType Name="APPLICATIONS_DAILY_Type"')
    end = meta.index("</EntityType>", start)
    meta = meta[:start] + re.sub(r'<Property Name="USERNAME"[^>]*/>', "", meta[start:end]) + meta[end:]
    used = [{"TIMEFRAME": f"2026-11-{d}T00:00:00Z", "DEVICE_NAME": pc} for d, pc in (("12", "VD-1"), ("19", "VD-1"))]

    def handler(request):
        path, p = request.url.path, request.url.params
        if path.endswith("/$metadata"):
            return httpx.Response(200, text=meta)
        if path.endswith("/DEVICE_INVENTORY"):
            return httpx.Response(200, json={"value": [{"DEVICE_NAME": "VD-1", "DEVICE_TYPE": "Virtual Desktop",
                                                        "TIME_LAST_REPORTING": "2026-11-20T08:00:00Z"}]})
        assert "USERNAME" not in p["$select"]
        lo = parse_time(re.search(r"TIMEFRAME ge (\S+)", p["$filter"]).group(1))
        hi = re.search(r"TIMEFRAME lt (\S+)", p["$filter"])
        if hi and parse_time(hi.group(1)).day == 17 and "TIMEFRAME" in p["$select"]:  # the read of Nov 17 (to its noon)
            return httpx.Response(400, json={"error": {"message": "Bad request."}})
        rows = [r for r in used if lo <= parse_time(r["TIMEFRAME"]) and (hi is None or parse_time(r["TIMEFRAME"]) < parse_time(hi.group(1)))]
        if p["$select"] == "DEVICE_NAME":
            rows = [{"DEVICE_NAME": n} for n in dict.fromkeys(r["DEVICE_NAME"] for r in rows)]
        return httpx.Response(200, json={"value": rows[:int(p["$top"])] if "$orderby" in p else rows})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    flat = " ".join(render_checks(verify_usage(settings, client, now=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc))).split())
    for line in (
        "back to Thu Nov 12, 2026: 8 days before today. That is 0 whole months before this one.",
        "Kinds, in this order: Virtual Desktop (1 in the list)",
        "Tue Nov 17 could not be read (",
        "Thu Nov 19 1 100%",
        "on at least one of the 2 days 1 (100%)",  # the day not read, and the days with no row, are not among them
        # what the live Aternity does (stage, Nov 21 2026): no row for today, which printed as "0 0% so far"
        "Fri Nov 20 no rows from Aternity yet: today is not over",
        "Aternity has no row of daily app usage for today: it adds a day after the day is over, so a chart of it ends "
        "with yesterday.",
        "This Aternity does not publish the username on daily app usage (USERNAME of APPLICATIONS_DAILY): a line of "
        "people cannot come from it.",
        "November so far, from Nov 12 only: 1 100%",
        "October: not kept (daily app usage starts Nov 12)",
        "Daily app usage starts Nov 12, after this month began: a month's count from Aternity would miss its first days, "
        "so the tab would have to keep the month's PC names itself.",
    ):
        assert line in flat, line


def test_verify_usage_reads_the_days_without_the_username_when_aternity_refuses_it():
    """A username column Aternity lists and then refuses must not cost the days' counts."""
    from datetime import datetime, timezone
    from pathlib import Path
    from uxcorrelate.models import set_zone
    from uxcorrelate.verify import verify_usage
    set_zone(None)
    meta = (Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text()

    def handler(request):
        path, p = request.url.path, request.url.params
        if path.endswith("/$metadata"):
            return httpx.Response(200, text=meta)
        if path.endswith("/DEVICE_INVENTORY"):
            return httpx.Response(200, json={"value": [{"DEVICE_NAME": "VD-1", "DEVICE_TYPE": "Virtual Desktop"}]})
        if "USERNAME" in p["$select"]:
            return httpx.Response(400, json={"error": {"message": "Bad column."}})
        return httpx.Response(200, json={"value": [{"DEVICE_NAME": "VD-1", "TIMEFRAME": "2026-11-19T00:00:00Z"}]
                                         if "2026-11-19T12" in p["$filter"] or "$orderby" in p else []})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    flat = " ".join(render_checks(verify_usage(settings, client, now=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc))).split())
    assert "Thu Nov 19 1 100%" in flat and "could not be read" not in flat
    assert "Aternity refused daily app usage asked for with the username (" in flat
    assert "and gave it without: a line of people cannot come from it." in flat
