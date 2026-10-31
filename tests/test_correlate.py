from datetime import datetime, timedelta, timezone

import pytest

from uxcorrelate.config import Settings
from uxcorrelate.correlate import InvestigationRequest, investigate
from synthetic import CPU_SPIKE, INCIDENT, DemoAternity, DemoNetim, demo_sources
from uxcorrelate.models import (
    AppUsageSample, BootComponent, BootSample, DeviceHealthSample, EndpointInfo, HealthEvent, ProcessSample, SoftwareChange, UserSample,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)


def run_demo(**overrides):
    settings = Settings()
    aternity, netim, start, end = demo_sources(settings, now=NOW)
    req = InvestigationRequest(start=start, end=end, username="jdoe", application="CRM Portal")
    for k, v in overrides.items():
        setattr(req, k, v)
    return investigate(req, aternity, netim, settings), start


class StubAternity:
    def __init__(self, samples, health=(), usage=()):
        self.samples, self.health, self.usage = samples, list(health), list(usage)

    def user_activity(self, *a, **k):
        return self.samples

    def device_health(self, *a, **k):
        return [h for h in self.health if h.free_disk is None]

    def app_usage(self, *a, **k):
        return self.usage

    def disk_space(self, *a, **k):
        return [h for h in self.health if h.free_disk is not None]

    def endpoint_info(self, device):
        return EndpointInfo(device=device)


def amy(samples, health=(), usage=(), settings=None, source=StubAternity):
    return investigate(InvestigationRequest(start=T0, end=T0 + timedelta(hours=2), username="amy"),
                       source(samples, health, usage), None, settings or Settings())


def erp(bad):
    """24 ERP actions 5 min apart; the bad ones take 6 s, mostly on the PC."""
    return [UserSample(T0 + timedelta(minutes=5 * i), "amy", "PC1", "ERP", response_time=6.0 if bad(i) else 1.0,
                       client_time=5.0 if bad(i) else 0.3, network_time=0.3, backend_time=0.4)
            for i in range(24)]


def test_demo_points_at_the_branch_wan():
    result, start = run_demo()
    a, top = result["answer"], result["problems"][0]
    assert a["level"] == "likely"
    assert a["title"].startswith("Likely: den-br-rtr01:Gi0/1 (WAN-MPLS): Interface utilization")
    assert top["kind"] == "network" and top["slow_actions"] == top["of_slow"] == result["summary"]["slow"] == 23
    first_slow = min(t for t, rt, slow, app in result["timeline"]["actions"] if slow)
    assert datetime.fromisoformat(first_slow.replace("Z", "+00:00")) == start + timedelta(minutes=INCIDENT[0])


def test_mapping_roles():
    result, _ = run_demo()
    roles = {(m["object_type"], m["name"]): m["role"] for m in result["mapped_objects"]}
    assert roles[("DEVICE", "den-br-rtr01")] == "client-gateway"
    assert roles[("INTERFACE", "den-br-rtr01:Gi0/1 (WAN-MPLS)")] == "site"
    assert roles[("DEVICE", "crm-app01")] == "app-server"
    assert roles[("INTERFACE", "crm-app01:eth0")] == "app-server"
    assert roles[("DEVICE", "dc-core-sw01")] == "app-server-network"


def test_window_without_incident_reports_none():
    _, start = run_demo()
    result, _ = run_demo(start=start + timedelta(minutes=CPU_SPIKE[0] - 20),
                         end=start + timedelta(minutes=CPU_SPIKE[1] + 10))
    assert result["answer"]["level"] == "none"
    # the CPU spike is still listed, just not blamed
    cpu = [g for g in result["problems"] if g["what"] == "CPU %"]
    assert cpu and cpu[0]["slow_actions"] == 0


def test_requires_user_or_device():
    with pytest.raises(ValueError):
        investigate(InvestigationRequest(start=NOW - timedelta(hours=1), end=NOW),
                    DemoAternity(NOW), DemoNetim(NOW), Settings())


def test_pc_cpu_without_netim():
    bad = lambda i: 8 <= i < 12  # noqa: E731
    health = [DeviceHealthSample(T0 + timedelta(minutes=5 * i), "PC1", cpu=99.0 if bad(i) else 20.0, memory=50.0)
              for i in range(24)]
    result = amy(erp(bad), health)
    a = result["answer"]
    assert a["title"] == "Likely: PC1: CPU reached 99.0% (limit 85.0)" and result["problems"][0]["slow_actions"] == 4
    assert any("NetIM is not configured" in w for w in result["warnings"])
    assert any(f["text"].startswith("The network was not checked: NetIM is not configured") for f in a["found"])


def test_a_crash_of_another_app_is_not_blamed():
    # seen on prod: a Notepad crash was blamed for slow Outlook
    at = T0 + timedelta(minutes=40)
    usage = [AppUsageSample(at, "PC1", "Notepad", crashes=2), AppUsageSample(at, "PC1", "ERP", hang_time=12.0)]
    result = amy(erp(lambda i: i == 8), usage=usage)
    by_app = {g["app"]: g for g in result["problems"]}
    assert by_app["Notepad"]["slow_actions"] == 0
    assert by_app["ERP"]["slow_actions"] == 1
    assert result["answer"]["title"] == "Likely: ERP was not responding on PC1 (Aternity hang time up to 12.0)"


def test_aternity_status_decides_what_is_slow():
    settings = Settings()
    settings.thresholds.slow_status = ["Major"]
    samples = [UserSample(T0 + timedelta(minutes=5 * i), "amy", "PC1", "ERP", response_time=9.0,
                          status="Major" if i == 2 else "Normal") for i in range(6)]
    result = amy(samples, settings=settings)
    assert result["summary"]["slow"] == 1 and result["summary"]["slow_rule"] == "Aternity marked it Major"


def test_a_nearly_full_disk_is_a_pc_problem():
    bad = lambda i: 8 <= i < 12  # noqa: E731
    health = [DeviceHealthSample(T0 + timedelta(minutes=5 * i), "PC1", free_disk=3.0 if bad(i) else 40.0)
              for i in range(24)]
    result = amy(erp(bad), health)
    top = result["problems"][0]
    assert top["kind"] == "pc" and top["worst"] == 3.0 and top["slow_actions"] == 4
    assert result["answer"]["title"] == "Likely: PC1's disk was nearly full (3.0% free, limit 10.0%)"


def test_nothing_near_the_slow_actions_says_so():
    # one slow action; the CPU problem came 35 minutes later
    result = amy(erp(lambda i: i == 3), [DeviceHealthSample(T0 + timedelta(minutes=50), "PC1", cpu=99.0)])
    a = result["answer"]
    assert a["level"] == "unsure" and a["title"] == "Cause not found"
    assert "Aternity measured most of the slow time on the PC" in a["reason"]
    assert result["problems"][0]["slow_actions"] == 0


def test_who_else_finds_the_common_network_point_and_compares_with_earlier_days():
    result, _ = run_demo()
    im = result["impact"]
    assert im["users"] == 50 and im["slow"] == 20 and im["baseline_days"] == 3
    assert im["usual_rate"] == 0.0 and im["unusual"] is True
    cp = im["common_point"]
    assert cp["kind"] == "gateway" and cp["name"] == "den-br-rtr01 Gi0/0 (LAN) (10.20.30.0/24)"
    assert cp["slow"] == 20 and cp["includes_user"]
    hq = next(g for g in im["groups"] if g["kind"] == "subnet")
    assert hq["name"] == "10.50.0.0/24 (not in NetIM)" and hq["slow"] == 0
    assert im["affected_users"][0]["network"] == cp["name"]


def test_answer_is_plain_language():
    a = run_demo()[0]["answer"]
    assert a["who_else"].startswith("20 of 50 other CRM Portal users were slow (40%)")
    assert "share one network point: den-br-rtr01 Gi0/0 (LAN)" in a["who_else"]
    assert a["todo"][0].startswith("Many users are slow behind den-br-rtr01")
    assert a["found"][0]["count"] == "at 23 of 23 slow actions"
    assert a["time_went"]["network"] > 0.8 and a["felt"]["slow_actions"] == 23
    assert any("other problem(s) happened only at other times" in f["text"] for f in a["found"])


def test_who_else_says_when_only_this_user_is_slow():
    class WithPeers(StubAternity):
        def app_activity(self, applications, start, end):
            return [UserSample(start, f"u{n}", f"P{n}", "ERP", location="HQ", response_time=1.0)
                    for n in range(10)]

    result = amy(erp(lambda i: i == 3), source=WithPeers)
    assert result["impact"]["slow"] == 0
    assert result["answer"]["who_else"] == "Only this user: none of the 10 other ERP users were slow."


def test_the_users_slowest_start_up_is_in_what_they_felt():
    f = run_demo()[0]["answer"]["felt"]
    assert f["logon"]["username"] == "jdoe" and f["logon"]["values"]["Logon"]["duration"] == 94.0
    assert f["logon_unit"] == "s"


class Traced(StubAternity):
    """amy's ERP hangs in scanner.dll 2.1, updated the day before; three other PCs on 2.1 hang too."""
    at = T0 + timedelta(minutes=40)

    def health_events(self, device, start, end):
        return [HealthEvent(self.at, "PC1", "ERP", "amy", "Hang", "Application hang", "erp.exe", count=1,
                            component_version="5.0", sub_component="scanner.dll", sub_component_version="2.1",
                            exception="Not responding")]

    def app_health_events(self, application, start, end):
        same = [HealthEvent(self.at, f"PC{n}", "ERP", f"u{n}", "Hang", "Application hang", component="erp.exe",
                            sub_component="scanner.dll", sub_component_version="2.1") for n in (2, 3, 4)]
        return same + [HealthEvent(self.at, "PC9", "ERP", "u9", "Crash", component="erp.exe", sub_component="other.dll"),
                       # same place, another kind of failure: not the same failure
                       HealthEvent(self.at, "PC8", "ERP", "u8", "Crash", "Application crash", component="erp.exe",
                                   sub_component="scanner.dll", sub_component_version="2.1")]

    def software_changes(self, device, start, end):
        return [SoftwareChange(T0 - timedelta(days=1), "PC1", "Scanner", "Updated", "2.1", "2.0"),
                SoftwareChange(T0 - timedelta(days=3), "PC1", "Some Viewer", "Installed", "9")]


def test_a_hang_is_traced_to_its_module_the_change_before_it_and_other_pcs():
    usage = [AppUsageSample(Traced.at, "PC1", "ERP", hang_time=12.0)]
    result = amy(erp(lambda i: i == 8), usage=usage, source=Traced)
    why = result["problems"][0]["why"]
    assert why["failure"]["sub_component"] == "scanner.dll" and why["failure"]["at_slow_actions"]
    assert why["text"][0] == "ERP failed in scanner.dll 2.1 (inside erp.exe 5.0): Not responding."
    others = why["others"]
    assert others["pcs"] == 3 and others["versions"] == {"2.1": 3} and others["this_version"] == "2.1"
    assert [p["device"] for p in others["list"]] == ["PC2", "PC3", "PC4"] and others["list"][0]["username"] == "u2"
    assert why["changes"][0]["related"] and not why["changes"][1]["related"]
    assert any("Oct 27 Scanner Updated 2.0 → 2.1 (same name as the failing part)" in t for t in why["text"])
    a = result["answer"]
    assert "ERP failed in scanner.dll 2.1" in a["reason"] and a["found"][0]["why"] == why["text"]
    assert any(t.startswith("Check the Scanner change of 2026-10-27") for t in a["todo"])
    assert len([f for f in a["found"] if f["why"]]) == 1  # the hang and its health event: one finding
    # "who else is affected" is about the same failure, and it is said only there
    assert a["who_else"] == ("3 other PC(s) had the same failure (ERP Hang / Application hang in scanner.dll 2.1 "
                              "(inside erp.exe 5.0)), all on 2.1. This PC has 2.1.")
    assert not any("other PC" in t for t in why["text"])


def test_a_cpu_problem_names_the_busiest_process():
    class Busy(StubAternity):
        def top_processes(self, device, start, end):
            return [ProcessSample(T0 + timedelta(minutes=5 * i), "PC1", p, cpu=c)
                    for i in range(8, 12) for p, c in (("scan.exe", 80.0), ("erp.exe", 10.0))]

    bad = lambda i: 8 <= i < 12  # noqa: E731
    health = [DeviceHealthSample(T0 + timedelta(minutes=5 * i), "PC1", cpu=99.0 if bad(i) else 20.0)
              for i in range(24)]
    why = amy(erp(bad), health, source=Busy)["problems"][0]["why"]
    assert why["processes"][0] == {"process": "scan.exe", "value": 80.0}
    assert why["text"][0] == "Busiest processes then: scan.exe (CPU 80.0%), erp.exe (CPU 10.0%)."


class Starting(StubAternity):
    """amy logs on to PC1 at 09:21 (105 s, usually 34 s) and opens ERP at 09:25."""
    def boots(self, start, end, **k):
        if start < T0 - timedelta(days=1):  # her last 30 days
            return [BootSample(T0 - timedelta(days=d), "PC1", "amy", "HQ", type="Logon", duration=34.0)
                    for d in range(1, 6)]
        return [BootSample(T0 + timedelta(minutes=21), "PC1", "amy", "HQ", type="Logon", duration=105.0),
                BootSample(T0 + timedelta(minutes=100), "PC1", "amy", "HQ", type="Logon", duration=30.0)]

    def boot_components(self, *a, **k):
        return [BootComponent(T0 + timedelta(minutes=21), "PC1", "Logon", "Group Policy", duration=60.0,
                              degradation=45.0)]

    def boot_component_everywhere(self, *a, **k):
        return [BootComponent(T0 + timedelta(minutes=20 + i), f"PC{i}", "Logon", "Group Policy", location="HQ",
                              duration=50.0, degradation=30.0) for i in (1, 2, 3)]


def test_an_app_opened_during_a_slow_logon_is_explained_by_the_logon():
    result = amy(erp(lambda i: i == 5), source=Starting)  # ERP at 09:25, 4 min after the logon began
    top = result["problems"][0]
    assert top["source"] == "Aternity start-up" and top["slow_actions"] == 1
    assert result["answer"]["title"] == "Likely: PC1 was starting up: Logon 105.0 s (usual 34.0 s)"
    why = top["why"]["text"]
    assert why[0] == "Most of the extra time: Group Policy took 60.0 s, 45.0 s longer than normal (Logon)."
    assert why[-1].startswith("Group Policy was longer than normal on 3 PC(s) in this window (HQ 3)")
    assert len(result["problems"]) == 1  # the 30 s logon at 10:40 was near no slow action: not a problem


def test_a_missing_data_source_is_one_plain_line():
    from uxcorrelate.aternity import AternityClient
    from uxcorrelate.config import AternityConfig
    import httpx

    def handler(request):
        return httpx.Response(400, text='<?xml version="1.0"?><error><code>null</code>'
                                        "<message>View 'RAW_DATA_STORAGE' doesn't exist.</message></error>")
    client = AternityClient(AternityConfig(base_url="https://aternity.test/odata"), transport=httpx.MockTransport(handler))
    client.http.retries = 0
    for dev in ("PC1", "PC2", "PC3"):
        client.disk_space(dev, T0, T0 + timedelta(hours=1))
    assert client.warnings == ["Free disk space not checked: this Aternity has no RAW_DATA_STORAGE data source "
                               "(`uxcorrelate verify` shows what to change)."]


def test_a_removal_and_an_install_at_the_same_second_read_as_one_update():
    from uxcorrelate.cause import changes_before

    class Log:  # as a live change log: an update is two rows at the same second
        def software_changes(self, device, start, end):
            row = lambda h, name, kind, v: SoftwareChange(T0 - timedelta(hours=h), device, name, kind, v)  # noqa: E731
            return [row(5, "Viewer", "Uninstalled", "3.1"), row(5, "Viewer", "Installed", "3.2"),
                    row(30, "Editor", "Uninstalled", "7.0"), row(2, "Helper", "Installed", "1.4")]

    ch = changes_before(Log(), "PC5", T0, ["viewer"], "failing part", "first failure")
    assert [(c["name"], c["change"], c["version_before"], c["version"]) for c in ch["changes"]] == [
        ("Helper", "Installed", None, "1.4"), ("Viewer", "updated", "3.1", "3.2"), ("Editor", "Uninstalled", None, "7.0")]
    assert "Viewer updated 3.1 → 3.2 (same name as the failing part)" in ch["text"]
    assert "Editor Uninstalled 7.0" in ch["text"]


class Background(StubAternity):
    """No app on these events (as on a live Aternity): a background process crashes on PC1
    at amy's slow ERP actions, with a module Aternity could not name; PC1 is also low on memory."""
    at = T0 + timedelta(minutes=40)

    def health_events(self, device, start, end):
        return [HealthEvent(self.at, "PC1", None, "amy", "Background Process", "Background Process Crash",
                            "agent.exe (Process)", component_version="7.2 (Process Version)",
                            error="c0000005 (Exception)", details="Fault Offset:0001"),
                HealthEvent(self.at + timedelta(minutes=2), "PC1", None, "amy", "System", "Low Memory Pagefaults",
                            details="Page Faults: 900\nPercent Remaining Memory: 3.1%")]

    def component_health_events(self, component, start, end):
        crash = lambda dev, err, sub=None: HealthEvent(  # noqa: E731
            self.at, dev, None, "u", "Background Process", "Background Process Crash", component,
            component_version="7.2 (Process Version)", sub_component=sub, error=err)
        return [crash("PC2", "c0000005 (Exception)", "x.dll (Dll)"),  # module known there: still the same failure
                crash("PC3", "c0000005 (Exception)"),
                crash("PC4", "c0000409 (Exception)")]  # another error: not the same failure


def test_events_without_an_app_count_and_get_a_why():
    result = amy(erp(lambda i: i in (8, 9)), source=Background)  # slow at 09:40 and 09:45
    texts = {g["text"]: g for g in result["problems"]}
    crash = next(g for t, g in texts.items() if "Background Process Crash" in t)
    memory = next(g for t, g in texts.items() if "Low Memory" in t)
    assert crash["slow_actions"] == 2 and memory["slow_actions"] == 2  # no app: they count at any slow action
    why = crash["why"]
    assert why["text"][0] == ("Aternity recorded Background Process / Background Process Crash on PC1 in "
                              "agent.exe (Process) 7.2 (Process Version): c0000005 (Exception).")
    assert why["others"]["pcs"] == 2 and [p["device"] for p in why["others"]["list"]] == ["PC2", "PC3"]
    assert why["others"]["what"].endswith(", error c0000005 (Exception)")
    assert "Aternity's details: Page Faults: 900 Percent Remaining Memory: 3.1%" in memory["why"]["text"]


def test_aternity_n_a_and_unknown_modules_are_no_value():
    import httpx
    from uxcorrelate.aternity import AternityClient
    from uxcorrelate.config import AternityConfig

    def handler(request):
        return httpx.Response(200, json={"value": [
            {"TIMEFRAME": "2026-10-28T09:40:00Z", "DEVICE_NAME": "PC1", "APPLICATION_NAME": "N/A",
             "HEALTH_EVENT_CATEGORY": "Background Process", "HEALTH_EVENT_NAME": "Background Process Crash",
             "HEALTH_EVENT_COMPONENT": "agent.exe (Process)", "HEALTH_EVENT_SUB_COMPONENT": "unknown (Dll)",
             "HEALTH_EVENT_SUB_COMPONENT_VERSION": "0.0.0.0 (DLL Version)", "HEALTH_EVENT_EXCEPTION_TYPE": "N/A"}]})

    client = AternityClient(AternityConfig(base_url="https://aternity.test/odata"), transport=httpx.MockTransport(handler))
    e = client.health_events("PC1", T0, T0 + timedelta(hours=1))[0]
    assert (e.application, e.sub_component, e.sub_component_version, e.exception) == (None, None, None, None)
    assert e.component == "agent.exe (Process)"
    assert client.query("DEVICE_HEALTH_RAW", raw=True)[0]["APPLICATION_NAME"] == "N/A"  # verify sees it as sent
