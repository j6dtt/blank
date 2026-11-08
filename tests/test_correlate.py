from datetime import datetime, timedelta, timezone
from dataclasses import replace

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
    assert a["title"].startswith("23 slow actions, slowest CRM Portal Open Account 8.2 s on LT-JDOE-01. "
                                 "Likely: den-br-rtr01:Gi0/1 (WAN-MPLS): Interface utilization")
    assert top["kind"] == "network" and top["slow_actions"] == top["of_slow"] == result["summary"]["slow"] == 23
    first_slow = min(t for t, rt, slow, app, _ in result["timeline"]["actions"] if slow)
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
    # no slow action, but a crash is an issue by itself
    assert result["answer"]["level"] == "issues"
    assert result["answer"]["title"] == "1 app crash, no slow actions"
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
    assert a["title"] == ("4 slow actions, slowest ERP 6.0 s on PC1. Likely: PC1: CPU reached 99.0% (limit 85.0) "
                          "at all of them") and result["problems"][0]["slow_actions"] == 4
    assert any("NetIM is not configured" in w for w in result["warnings"])
    assert "Network: not checked, NetIM is not configured." in a["checked"]


def test_a_crash_of_another_app_is_not_blamed():
    # seen on prod: a Notepad crash was blamed for slow Outlook
    at = T0 + timedelta(minutes=40)
    usage = [AppUsageSample(at, "PC1", "Notepad", crashes=2), AppUsageSample(at, "PC1", "ERP", hang_time=12.0)]
    result = amy(erp(lambda i: i == 8), usage=usage)
    by_app = {g["app"]: g for g in result["problems"]}
    assert by_app["Notepad"]["slow_actions"] == 0
    assert by_app["ERP"]["slow_actions"] == 1
    assert result["answer"]["title"] == "ERP took 6.0 s on PC1. Likely: ERP was not responding on PC1"


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
    assert result["answer"]["title"] == ("4 slow actions, slowest ERP 6.0 s on PC1. Likely: PC1's disk was nearly "
                                         "full (3.0% free, limit 10.0%) at all of them")


def test_nothing_near_the_slow_actions_says_so():
    # one slow action; the CPU problem came 35 minutes later
    result = amy(erp(lambda i: i == 3), [DeviceHealthSample(T0 + timedelta(minutes=50), "PC1", cpu=99.0)])
    a = result["answer"]
    assert a["level"] == "unsure" and a["title"] == "ERP took 6.0 s on PC1. No cause found"
    assert "Aternity measured most of the slow time on the PC" in a["reason"]
    assert result["problems"][0]["slow_actions"] == 0


def test_who_else_finds_the_common_network_point_and_compares_with_earlier_days():
    result, _ = run_demo()
    im = result["impact"]
    assert im["users"] == 50 and im["slow"] == 20 and im["baseline_days"] >= 1
    assert im["usual_rate"] == 0.0 and im["unusual"] is True
    cp = im["common_point"]
    assert cp["kind"] == "gateway" and cp["name"] == "den-br-rtr01 Gi0/0 (LAN) (10.20.30.0/24)"
    assert cp["slow"] == 20 and cp["includes_user"]
    hq = next(g for g in im["groups"] if g["kind"] == "subnet")
    assert hq["name"] == "10.50.0.0/24 (not in NetIM)" and hq["slow"] == 0
    assert im["affected_users"][0]["network"] == cp["name"]


def test_answer_is_plain_language():
    a = run_demo()[0]["answer"]
    assert "At the same time, 20 of the 50 other people using CRM Portal also had a slow action (40%)." in a["who_else"]
    assert "are behind the same network point: den-br-rtr01 Gi0/0 (LAN)" in a["who_else"]
    assert a["todo"][0].startswith("Many users are slow behind den-br-rtr01")
    assert a["found"][0]["count"] == "at 23 of 23 slow actions"
    assert a["time_went"]["network"] > 0.8 and a["felt"]["slow_actions"] == 23
    # the problems at other times are listed with their own why, not just counted
    # in their own section, below the answer
    elsewhere = [f["text"] for f in a["elsewhere"]]
    assert not any(f in a["found"] for f in a["elsewhere"]) and all(f["count"] for f in a["elsewhere"])
    # what the user felt first, a limit passed last
    assert elsewhere == ["Application crash (OUTLOOK.EXE) on LT-JDOE-01",
                         "LT-JDOE-01: CPU reached 96.0% (limit 85.0)"]


def test_who_else_says_when_only_this_user_is_slow():
    class WithPeers(StubAternity):
        def app_activity(self, applications, start, end):
            return [UserSample(start, f"u{n}", f"P{n}", "ERP", location="HQ", response_time=1.0)
                    for n in range(10)]

    result = amy(erp(lambda i: i == 3), source=WithPeers)
    assert result["impact"]["slow"] == 0
    assert result["answer"]["who_else"].endswith("None of the 10 other people using ERP had a slow action at the same time.")


def test_the_users_slowest_start_up_is_in_what_they_felt():
    f = run_demo()[0]["answer"]["felt"]
    assert f["logons"][0]["username"] == "jdoe" and f["logons"][0]["values"]["Logon"]["duration"] == 94.0
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
    assert why["possible"] == ["Oct 27 Scanner Updated 2.0 → 2.1 (same name as the failing part, before it)."]
    assert not any("Some Viewer" in t for t in why["text"])  # no link to the failure: listed once, per PC
    a = result["answer"]
    # the why is said once, under the finding; the headline only counts
    assert a["reason"] == "It happened within 5 min of the slow action." and a["found"][0]["why"] == why["text"]
    assert any(t.startswith("Check the Scanner change of 2026-10-27") for t in a["todo"])
    assert len([f for f in a["found"] if f["why"]]) == 1  # the hang and its health event: one finding
    # who else is said once, on the issue, in one shape
    assert a["found"][0]["who"].startswith("3 other PCs (3 users), all on 2.1. This PC has 2.1.")
    assert not any("other PC" in t for t in why["text"])
    assert [(c["device"], [x["text"] for x in c["changes"]]) for c in a["changes"]] == [
        ("PC1", ["Scanner Updated 2.0 → 2.1", "Some Viewer Installed 9"])]


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
    assert why["text"][0] == ("Aternity reads the PC's CPU with each of the user's actions: the highest reading was "
                              "99.0%, with the slow ERP.")
    assert why["text"][1] == ("Aternity keeps CPU per program only per hour, so it cannot show which program it was "
                              "at that moment. In the hours around it, the highest per program: scan.exe CPU up to "
                              "80.0%, erp.exe CPU up to 10.0%.")
    assert why["todo"] == ["On PC1: look at scan.exe: CPU up to 80.0% in the hours around it, against the PC's 99.0%."]


def test_a_memory_problem_names_the_busiest_process_from_hourly_rows_in_mb():
    # live APPLICATION_RESOURCES_HOURLY: one row per process and hour, memory in bytes,
    # the row's time at any minute (here 40 min after the problem)
    class Hourly(StubAternity):
        def top_processes(self, device, start, end):
            t = T0 + timedelta(minutes=80)
            rows = [ProcessSample(t, "PC1", "scsrvc", "Trellix Solidcore", cpu=1.984, memory_bytes=227840000.0),
                    ProcessSample(t, "PC1", "erp", "ERP", cpu=0.3, memory_bytes=3.5e9),
                    ProcessSample(T0 + timedelta(hours=5), "PC1", "late", memory_bytes=9e9)]  # not near
            return [p for p in rows if start <= p.timestamp <= end]

    bad = lambda i: 6 <= i < 9  # noqa: E731
    health = [DeviceHealthSample(T0 + timedelta(minutes=5 * i), "PC1", memory=97.0 if bad(i) else 50.0)
              for i in range(24)]
    why = amy(erp(bad), health, source=Hourly)["problems"][0]["why"]
    assert why["text"][1] == ("Aternity keeps memory per program only per hour, so it cannot show which program it "
                              "was at that moment. In the hours around it, the highest per program: erp memory up to "
                              "3337.9 MB, scsrvc (Trellix Solidcore) memory up to 217.3 MB.")
    assert not any("look at" in t for t in why["todo"])  # MB per program against the PC's %: not comparable


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
    assert result["answer"]["title"] == "ERP took 6.0 s on PC1. Likely: PC1 was starting up: Logon 105.0 s (usual 34.0 s)"
    why = top["why"]["text"]
    assert why[0] == ("Logon took 105.0 s, longer than 9 in 10 of PC1's 5 earlier start-ups in the last 30 days "
                      "(up to 34.0 s); the one before, Oct 27, took 34.0 s.")
    assert why[1] == "Most of the extra time: Group Policy took 60.0 s, 45.0 s longer than normal (Logon)."
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
    client.cfg.entities["disk_space"] = "RAW_DATA_STORAGE"
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
    assert why["text"][0] == "Aternity recorded Background Process Crash on PC1 in agent.exe 7.2: c0000005."
    assert why["others"]["pcs"] == 2 and [p["device"] for p in why["others"]["list"]] == ["PC2", "PC3"]
    assert why["others"]["what"].endswith(", error c0000005")
    assert "Aternity's details: Page Faults: 900 Percent Remaining Memory: 3.1%" in memory["why"]["text"]


def test_a_background_crash_away_from_the_slow_actions_keeps_its_step_below_the_answer():
    # slow only at 09:00: the background crash at 09:40 is not why amy was slow
    a = amy(erp(lambda i: i == 0), source=Background)["answer"]
    crash = next(f for f in a["elsewhere"] if f["text"].startswith("Background Process Crash"))
    assert any(w.startswith("Next step: 2 other PCs fail the same way: treat it as a agent.exe problem")
               for w in crash["why"])
    assert not any("treat it as a" in t for t in a["todo"])


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


class Usual(Starting):
    """amy's logon at 09:21 took 105 s, but her PC always takes about that: no part was slow."""
    def boots(self, start, end, **k):
        if start < T0 - timedelta(days=1):
            return [BootSample(T0 - timedelta(days=d), "PC1", "amy", "HQ", type="Logon", duration=100.0 + d)
                    for d in range(1, 9)]
        return [BootSample(T0 + timedelta(minutes=21), "PC1", "amy", "HQ", type="Logon", duration=105.0)]

    def boot_components(self, *a, **k):
        return []


def test_a_normal_start_up_is_not_the_cause_nor_an_issue():
    result = amy(erp(lambda i: i == 5), source=Usual)
    a = result["answer"]
    assert a["level"] == "unsure" and "starting up" not in a["title"]
    assert "PC1 was starting up near it: Logon 105.0 s" in a["reason"]
    assert "No part of that start-up took longer than normal." in a["reason"]
    assert not any("starting up" in r["issue"] for r in a["rollup"])
    # not a problem anywhere: not in the list, not on the timeline
    assert not any("starting up" in g["what"] for g in result["problems"])
    assert not any("starting up" in m[2] for m in result["timeline"]["marks"])
    assert not any("Slow start-ups tab" in t for t in a["todo"])
    assert a["todo"][-1].startswith("Nothing measured explains it")


def test_what_was_checked_is_listed_with_counts():
    result = amy(erp(lambda i: i == 3), [DeviceHealthSample(T0 + timedelta(minutes=50), "PC1", cpu=99.0)])
    assert result["answer"]["checked"] == [
        "PC: Aternity gave no CPU, memory or disk queue for the slow actions.",
        "Health events on PC1: 0 in the window, 0 within 5 min of a slow action.",
        "App crashes and hangs on PC1: 0 in the window, 0 within 5 min of a slow action.",
        "Start-ups on PC1: 0 in the window, 0 within 5 min of a slow action.",
        "Network: not checked, NetIM is not configured.",
    ]


class Billing(StubAternity):
    """lee on NB-12: "Open Invoice" took 2 s until 10 days ago, 9 s since; an agent
    was updated the evening before. Other users take 2.1 s."""
    def history(self, start, end, username=None, device=None):
        if username:  # lee's last 30 days, twice a day
            return [UserSample(T0 - timedelta(days=d, hours=h), "lee", "NB-12", "Billing", "Open Invoice",
                               response_time=9.0 if d <= 10 else 2.0, cpu=20.0, memory=50.0, disk_queue=0.1)
                    for d in range(1, 31) for h in (2, 6)]
        return [UserSample(T0 + timedelta(minutes=m), u, f"NB-{u}", "Billing", "Open Invoice", response_time=2.1)
                for m in (10, 40) for u in ("ana", "raj", "zoe")] + \
            [UserSample(T0 + timedelta(minutes=10), "lee", "NB-12", "Billing", "Open Invoice", response_time=9.0)]

    activity_history = lambda self, app, act, start, end, **k: self.history(start, end, **k)  # noqa: E731

    def software_changes(self, device, start, end):
        t = T0 - timedelta(days=10, hours=12)
        rows = [SoftwareChange(t, device, "Print Agent", "Uninstalled", "4.1"),
                SoftwareChange(t, device, "Print Agent", "Installed", "4.2")]
        return [c for c in rows if start <= c.timestamp <= end]


def test_each_slow_action_is_compared_with_its_own_normal_and_what_changed():
    samples = [UserSample(T0 + timedelta(minutes=10 * i), "lee", "NB-12", "Billing", "Open Invoice",
                          response_time=9.0, cpu=35.0, memory=72.0, disk_queue=0.3) for i in range(3)]
    result = investigate(InvestigationRequest(start=T0, end=T0 + timedelta(hours=2), username="lee"),
                         Billing(samples), None, Settings())
    a = result["answer"]
    assert a["normal"] == [[
        "Billing Open Invoice took 9.0 s at its 3 slow actions (slowest 9.0 s).",
        "Usually 2.0 s for lee (typical of 60 in the last 30 days).",
        "Other users within 5 min: typical 2.1 s (3 users).",  # those at 09:40 are not near
        "Aternity marked most runs slow on 10 of the 30 days lee used it in the last 30 days, every day since Oct 18.",
        "Changed on NB-12 between Oct 17 and Oct 18: Oct 17 Print Agent updated 4.1 → 4.2.",
        "NB-12 at those slow actions: CPU up to 35.0 % (usually 20.0 %), memory up to 72.0 % (usually 50.0 %), "
        "disk queue up to 0.3 (usually 0.1).",
    ]]
    assert "On NB-12: check what changed before Billing Open Invoice became slow (Print Agent)." in a["todo"]
    assert a["level"] == "possible" and a["title"] == ("Billing Open Invoice on NB-12: slow 23 of 23 times from "
                                                      "Oct 18 (9.0 s, usually 2.0 s); normal on Oct 17. "
                                                      "Possibly: Print Agent updated on Oct 17")
    # how much the usual rests on, so a reader can judge it
    assert a["reason"] == ("No problem was recorded within 5 min of any of the 3 slow actions. The usual time is "
                           "from 60 runs in the last 30 days. What changed on NB-12 is listed under What changed.")


def test_a_crash_at_another_time_still_gets_its_why():
    # ERP hangs at 09:40, but the only slow action is at 09:00: the hang is an issue by itself
    usage = [AppUsageSample(Traced.at, "PC1", "ERP", hang_time=12.0)]
    result = amy(erp(lambda i: i == 0), usage=usage, source=Traced)
    a = result["answer"]
    hang = next(f for f in a["elsewhere"] if f["count"] and "not near a slow action" in f["count"])
    assert hang["why"][0] == "ERP failed in scanner.dll 2.1 (inside erp.exe 5.0): Not responding."
    assert hang["who"].startswith("3 other PCs (3 users)")
    assert any(t.startswith("Check the Scanner change of 2026-10-27") for t in a["todo"])
    assert not any(f["text"].endswith("not near a slow action.") for f in a["found"] + a["elsewhere"])


def test_one_earlier_run_is_too_few_to_say_what_is_normal():
    class Once(Billing):
        def history(self, start, end, username=None, device=None):
            rows = super().history(start, end, username=username, device=device)
            return rows[:1] if username else rows

    samples = [UserSample(T0 + timedelta(minutes=10), "lee", "NB-12", "Billing", "Open Invoice",
                          response_time=9.004, cpu=35.04)]
    result = investigate(InvestigationRequest(start=T0, end=T0 + timedelta(hours=2), username="lee"),
                         Once(samples), None, Settings())
    a = result["answer"]
    assert a["normal"] == [[
        "Billing Open Invoice took 9.0 s at its slow action.",
        "Only 1 earlier Billing Open Invoice for lee in the activity Aternity keeps: too few to say what is normal for lee.",
        "Other users within 5 min: typical 2.1 s (3 users).",
        "NB-12 at that slow action: CPU up to 35.0 %.",
    ]]
    assert a["title"] == "Billing Open Invoice took 9.0 s on NB-12. No cause found"  # none built on one earlier run
    assert "highest CPU 35.0 % (limit 85.0 %)" in a["checked"][0]


def test_one_crash_counted_by_app_usage_and_health_events_is_one_row():
    class Snip(StubAternity):  # usage names the app, the event its process
        def health_events(self, device, start, end):
            return [HealthEvent(T0 + timedelta(minutes=t), "PC1", None, "amy", "Application", "Application Crash",
                                "SnippingTool.exe (Process)", error="c0000005 (Exception)") for t in (30, 50)]

    usage = [AppUsageSample(T0 + timedelta(minutes=t), "PC1", "Snipping Tool", crashes=1.0) for t in (30, 50, 70)]
    result = amy(erp(lambda i: i == 0), usage=usage, source=Snip)
    rows = [g for g in result["problems"] if "nipping" in g["text"]]
    assert len(rows) == 1 and rows[0]["source"] == "Aternity event" and rows[0]["times"] == 2
    assert rows[0]["last"] == "2026-10-28T10:10:00Z"  # the usage row at 10:10 is part of it


def test_two_pcs_are_told_apart_and_usual_slowness_is_not_blamed():
    class Usual(StubAternity):
        def app_activity(self, applications, start, end):  # 3 of 30 other users slow now and every day
            return [UserSample(t.replace(second=0), f"u{i}", f"PC-{i}", "ERP", response_time=6.0 if i < 3 else 1.0)
                    for t in (start + timedelta(minutes=1),) for i in range(30)]

    samples = erp(lambda i: i == 4) + [UserSample(T0 + timedelta(minutes=7), "amy", "PC2", "ERP", response_time=1.0)]
    result = amy(samples, source=Usual)
    a = result["answer"]
    assert [(p["device"], p["actions"], p["slow"], p["virtual"]) for p in a["felt"]["pcs"]] == [
        ("PC1", 24, 1, False), ("PC2", 1, 0, False)]
    assert a["felt"]["slowest"]["device"] == "PC1"
    assert a["title"] == "ERP took 6.0 s on PC1. Other ERP users were as slow as usual at this time"
    assert a["reason"].endswith("Other ERP users had slow actions about as often as normal at this time (see Other users).")


def test_the_text_report_fits_a_narrow_terminal():
    from uxcorrelate.report import WIDTH, render_text
    text = render_text(run_demo()[0])
    assert max(len(line) for line in text.splitlines()) <= WIDTH
    # a wrapped line continues under its own text
    assert "      * Logon took 94.0 s, longer than 9 in 10 of LT-JDOE-01's 30\n        earlier" in text


def test_the_change_log_reads_like_the_live_one():
    # shapes seen on a live Aternity: an update as two rows 2 min apart, one "Updated" row,
    # the same version removed and put back a day later, and a plain removal
    from uxcorrelate.cause import changes_between
    day = lambda d, h, m, s=0: datetime(2026, 10, d, h, m, s, tzinfo=timezone.utc)  # noqa: E731

    class Log:
        def software_changes(self, device, start, end):
            rows = [SoftwareChange(day(19, 16, 55, 19), device, "Web Viewer", "Uninstalled", "154.0"),
                    SoftwareChange(day(19, 16, 57, 16), device, "Web Viewer", "Installed", "155.0"),
                    SoftwareChange(day(19, 16, 7), device, "Mail Client", "Updated", "7.2"),
                    SoftwareChange(day(17, 19, 40), device, "Text Editor", "Uninstalled", "8.9"),
                    SoftwareChange(day(18, 23, 0), device, "Text Editor", "Installed", "8.9"),
                    SoftwareChange(day(18, 9, 0), device, "Old Agent", "Uninstalled", "3.0")]
            return [c for c in rows if start <= c.timestamp <= end]

    ch = changes_between(Log(), "WS-4", day(17, 0, 0), day(20, 0, 0))
    assert ch["text"] == ("Changed on WS-4 between Oct 17 and Oct 20: Oct 19 Web Viewer updated 154.0 → 155.0; "
                          "Oct 19 Mail Client Updated 7.2; Oct 18 Old Agent Uninstalled 3.0; "
                          "Oct 17 Text Editor removed, then put back at 8.9 (Oct 18).")
    # what is different afterwards: the put-back is told, not counted as a change to check
    assert [c["name"] for c in ch["changes"]] == ["Web Viewer", "Mail Client", "Old Agent"]


def test_software_that_comes_and_goes_in_the_log_is_not_a_change_to_check():
    # live: Chrome removed Oct 18, back Oct 19, removed Oct 20, back Oct 21 (same version);
    # a problem on Oct 20 must not point at "Chrome Uninstalled", and a put-back after
    # the problem is read too
    from uxcorrelate.cause import changes_before
    day = lambda d, h: datetime(2026, 10, d, h, 0, tzinfo=timezone.utc)  # noqa: E731

    class Log:
        def software_changes(self, device, start, end):
            rows = [SoftwareChange(day(18, 13), device, "Browser", "Uninstalled", "154.0"),
                    SoftwareChange(day(19, 13), device, "Browser", "Installed", "154.0"),
                    SoftwareChange(day(20, 9), device, "Browser", "Uninstalled", "154.0"),
                    SoftwareChange(day(21, 13), device, "Browser", "Installed", "154.0"),
                    SoftwareChange(day(19, 16), device, "Mail Client", "Updated", "7.2")]
            return [c for c in rows if start <= c.timestamp <= end]

    ch = changes_before(Log(), "WS-8", day(20, 12), ["browser"], "failing part", "first failure")
    assert ch["text"] == ("Changed on WS-8 in the 14 days before Oct 20: Oct 19 Mail Client Updated 7.2; "
                          "Oct 20 Browser removed, then put back at 154.0 (Oct 21); "
                          "Oct 18 Browser removed, then put back at 154.0 (Oct 19).")
    assert ch["todo"] is None  # nothing related that left the PC different
    assert [c["related"] for c in ch["changes"]] == [False, False, False]

    class NotBackYet(Log):  # the latest removal has no put-back yet: it came and went before
        def software_changes(self, device, start, end):
            return [c for c in super().software_changes(device, start, end) if c.timestamp != day(21, 13)]

    ch = changes_before(NotBackYet(), "WS-8", day(20, 12), ["browser"], "failing part", "first failure")
    assert "Oct 20 Browser Uninstalled 154.0 (it comes and goes in Aternity's log)" in ch["text"]
    assert ch["todo"] is None


def test_on_a_pooled_virtual_desktop_the_answer_says_which_desktops():
    class Pooled(Billing):  # lee gets another virtual desktop each day
        def history(self, start, end, username=None, device=None):
            return [replace(s, device=f"VD-{(T0 - s.timestamp).days}") if s.username == "lee" else s
                    for s in super().history(start, end, username=username, device=device)]

    samples = [UserSample(T0 + timedelta(minutes=10 * i), "lee", "VD-0", "Billing", "Open Invoice",
                          response_time=9.0, virtual=True, client_device="TC-5") for i in range(3)]
    result = investigate(InvestigationRequest(start=T0, end=T0 + timedelta(hours=2), username="lee"),
                         Pooled(samples), None, Settings())
    a = result["answer"]
    assert ("The last normal Billing Open Invoice was on VD-11, another virtual desktop: what changed is "
            "read on VD-0 only.") in a["normal"][0]
    assert a["felt"]["pcs"] == [{"device": "VD-0", "actions": 3, "slow": 3, "virtual": True, "from": ["TC-5"]}]
    from uxcorrelate.report import render_text
    assert "  on virtual desktop VD-0, from TC-5" in render_text(result)
    assert ("Network between VD-0 (a virtual desktop) and the device the user sat at (TC-5): not checked, "
            "Aternity gives no delay for it.") in a["checked"]


class Wrong(StubAternity):
    """No slow action, but a day of failures on a virtual desktop, as seen on a live Aternity."""
    at = T0 + timedelta(minutes=30)
    update = at - timedelta(minutes=20)

    def __event(self, minutes, *args, **kw):
        return HealthEvent(self.at + timedelta(minutes=minutes), "VD-1", *args, **kw)

    def health_events(self, device, start, end):
        return [self.__event(0, None, "amy", "Background Process", "Background Process Crash", "bad_module_info (Process)",
                             exception="00000000 (Exception)", details="Fault Offset:00000000"),
                self.__event(1, None, "amy", "Background Process", "Background Process Crash", "tagger.exe (Process)",
                             component_version="26.0.1 (Process Version)", exception="c0000005 (Exception)",
                             details="Fault Offset:012e0030"),
                self.__event(2, "Mail", "amy", "Application", "Application Crash", "MAIL.EXE (Process)",
                             component_version="16.2 (Process Version)", sub_component="KERNELBASE.dll (Dll)",
                             sub_component_version="10.0 (Dll Version)", exception="e0434352 (Exception)"),
                self.__event(60, None, "amy", "System", "Low Memory Pagefaults", details="Percent Remaining Memory: 1.91%"),
                self.__event(70, "Web", "amy", "Application", "Application Crash", "web.exe (Process)",
                             component_version="9.9 (Process Version)", exception="c0000005 (Exception)")]

    def component_health_events(self, component, start, end):
        if start < T0 - timedelta(days=1):  # the 14 days before: the tagger crash began 3 days ago
            return [HealthEvent(T0 - timedelta(days=3), "VD-7", None, "u7", "Background Process",
                                "Background Process Crash", "tagger.exe (Process)")] if "tagger" in component else []
        return [HealthEvent(self.at, f"VD-{n}", None, "u1", "Background Process", "Background Process Crash", component)
                for n in (2, 3, 4)]

    def app_health_events(self, application, start, end):
        return [HealthEvent(self.at, "VD-2", "Mail", "u2", "Application", "Application Crash", "MAIL.EXE (Process)",
                            sub_component="KERNELBASE.dll (Dll)")] if application == "Mail" else []

    def app_usage_everywhere(self, application, start, end):
        return [AppUsageSample(self.at, f"VD-{n}", application, hang_time=9.0 if n < 3 else None) for n in range(1, 6)]

    def known_pcs(self):
        return 1200

    def virtual_devices(self, start, end, username=None, device=None):
        return {"VD-1"}

    def app_failures_everywhere(self, application, start, end):
        return [AppUsageSample(T0 - timedelta(days=5), "VD-2", application, hang_time=9.0)]

    def app_usage(self, device, start, end, application=None):
        return [AppUsageSample(self.at, "VD-1", "Sheets", hang_time=25885.0)]

    def software_changes(self, device, start, end):
        return [SoftwareChange(self.update, "VD-1", "Classifier", "Updated", "26.0.1", "24.1"),
                SoftwareChange(self.update, "VD-1", "Office Suite", "Updated", "16.2", "16.1"),
                SoftwareChange(self.update, "VD-1", "Viewer", "Updated", "3", "2"),
                SoftwareChange(self.at + timedelta(minutes=75), "VD-1", "Web Runtime", "Updated", "9.9", "9.8")]

    def top_processes(self, device, start, end):
        return [ProcessSample(self.at + timedelta(minutes=80), "VD-1", "erp", memory_bytes=3.5e9)]


def test_a_failure_on_another_version_is_not_new_with_this_change():
    class OlderToo(Wrong):
        def component_health_events(self, component, start, end):
            return [HealthEvent(self.at, f"VD-{n}", None, "u1", "Background Process", "Background Process Crash", component,
                                component_version="24.1 (Process Version)") for n in (2, 3)]

    samples = [UserSample(T0 + timedelta(minutes=i), "amy", "VD-1", "ERP", response_time=1.0, status="Normal",
                          virtual=True) for i in range(5)]
    found = amy(samples, source=OlderToo)["answer"]["found"]
    tagger = next(f for f in found if "tagger" in f["text"])
    assert tagger["possible"] == []
    assert any("other PCs fail the same way on 24.1, so this is not new with this change" in w for w in tagger["why"])


def test_the_rollup_is_one_row_per_issue_with_a_rule_for_each_verdict():
    from uxcorrelate.answer import verdict
    from uxcorrelate.cause import _trend

    class Fleet(Wrong):
        def named_health_events(self, name, start, end):
            if start < T0 - timedelta(days=1):  # steady on the working days before
                return [HealthEvent(T0 - timedelta(days=d), f"VD-{n}", None, "u", "System", name)
                        for d in range(1, 14) for n in range(10, 18)]
            return [HealthEvent(self.at, f"VD-{n}", None, "u", "System", name) for n in (2, 3)]

    samples = [UserSample(T0 + timedelta(minutes=i), "amy", "VD-1", "ERP", response_time=1.0, status="Normal",
                          virtual=True) for i in range(5)]
    a = amy(samples, source=Fleet)["answer"]
    rows = {r["issue"]: r for r in a["rollup"]}
    assert len(a["rollup"]) == len(rows) and "_others" not in a["found"][0]
    memory = rows["Low Memory Pagefaults"]
    assert memory["others"] == "2 of 1,200" and memory["verdict"] == "Widespread, steady" and memory["times"] == 1
    assert [d["pcs"] for d in memory["days"]][:2] == [8, 8] and memory["days"][-1]["window"]
    assert sum("cannot name the program" in t for t in a["todo"]) == 1  # bad_module_info, not low memory
    assert a["found"][memory["first"]]["what"] == "Low Memory Pagefaults"

    other = {"pcs": 3, "history": {"since": "x", "trend": "growing"}}
    assert verdict(other) == "Widespread, growing"
    assert verdict({"pcs": 0, "history": None}) == "Only this PC"
    assert verdict({"pcs": 3, "history": {"since": None, "trend": None}}) == "New"
    assert verdict({"pcs": 1, "history": None}) == "On 1 other PC"
    assert verdict(None) is None
    day = lambda k: (T0.date() - timedelta(days=k)).isoformat()  # noqa: E731
    rising = {day(k): 20 if k <= 7 else 10 for k in range(1, 14)}
    assert _trend(rising, T0) == "growing"
    assert _trend({day(k): 10 if k <= 7 else 20 for k in range(1, 14)}, T0) == "falling"
    assert _trend({day(k): 1 for k in range(1, 14)}, T0) is None  # too few to tell


def test_slow_actions_carry_aternitys_own_status():
    from uxcorrelate.report import render_text

    samples = [UserSample(T0 + timedelta(minutes=i), "amy", "PC1", "ERP", response_time=9.0 if i == 0 else 4.0,
                          status="Major" if i == 0 else "Minor") for i in range(3)]
    result = amy(samples)
    felt = result["answer"]["felt"]
    assert felt["by_status"] == {"Minor": 2, "Major": 1} and felt["slowest"]["status"] == "Major"
    assert result["timeline"]["actions"][0][4] == "Major"
    text = render_text(result)
    assert "Aternity marked them: 2 Minor, 1 Major" in text and "Aternity: Major" in text


def test_a_hang_says_how_long_other_pcs_already_had_it():
    samples = [UserSample(T0 + timedelta(minutes=i), "amy", "VD-1", "ERP", response_time=1.0, status="Normal",
                          virtual=True) for i in range(5)]
    found = amy(samples, source=Wrong)["answer"]["found"]
    assert "Already on other PCs 5 days before this window." in next(f for f in found if f["issue"] == "hang")["who"]


def test_with_no_slow_action_the_answer_is_what_went_wrong():
    samples = [UserSample(T0 + timedelta(minutes=i), "amy", "VD-1", "ERP", response_time=1.0, status="Normal",
                          virtual=True) for i in range(5)]
    result = amy(samples, source=Wrong)
    a = result["answer"]
    assert a["level"] == "issues"
    assert a["title"] == "1 hang, 2 app crashes and 1 Low Memory Pagefaults event, no slow actions"
    assert a["reason"] == "Also 2 background crashes: the same crashes hit 3 other PCs."
    # what the user felt first
    assert [f["issue"] for f in a["found"]] == ["hang", "crash", "crash", "system", "background", "background"]
    by = {f["text"].split(" on ")[0]: f for f in a["found"]}

    # a change is a possible cause only when it matches the failing part and came first
    tagger = by["Background Process Crash (tagger.exe)"]
    assert tagger["possible"] == ["Oct 28 Classifier Updated 24.1 → 26.0.1 (same version as the failing part, before it)."]
    assert tagger["who"] == "3 other PCs (1 user) of 1,200 PCs in Aternity's inventory. On other PCs since Oct 25."
    assert not any("Fault Offset" in w for w in tagger["why"])
    web = by["Application Crash (web.exe)"]
    assert web["possible"] == [] and any("logged it after this failure. On a virtual desktop" in w for w in web["why"])
    assert a["changes"][0]["device"] == "VD-1" and len(a["changes"][0]["changes"]) == 4

    # names that are not the software at fault
    unknown = by["Background Process Crash (bad_module_info)"]
    assert "bad_module_info is not a program: Windows could not tell which program failed." in unknown["why"]
    assert not any("look at bad_module_info" in t or "a bad_module_info problem" in t for t in a["todo"])
    assert "3 other PCs fail the same way and Windows cannot name the program: see when it started, " \
           "and what was rolled out then." in a["todo"]
    mail = by["Application Crash (MAIL.EXE)"]
    assert "KERNELBASE.dll is a Windows file: it reports a failure, the cause is usually in MAIL.EXE." in mail["why"]
    assert mail["why"][0] == "Mail failed in KERNELBASE.dll 10.0 (inside MAIL.EXE 16.2): e0434352."
    assert mail["possible"] == ["Oct 28 Office Suite Updated 16.1 → 16.2 (same version as the failing part, before it)."]
    assert "On VD-1: look at MAIL.EXE 16.2, where Mail failed (repair or update it; if it is an add-in, try without it)." in a["todo"]
    assert not any("KERNELBASE.dll problem" in t or "other PCs fail" in t and "Mail" in t for t in a["todo"])  # 1 other PC

    # a hang with no details: who else from app usage, out of the PCs using the app
    hang = by["Sheets was not responding"]
    assert hang["text"] == "Sheets was not responding on VD-1"
    assert hang["who"] == "1 other PC of 4 PCs using Sheets. Already on other PCs 5 days before this window."
    assert "Within minutes of it on this PC: bad_module_info" not in " ".join(hang["why"])
    assert any(w.startswith("Within minutes of it on this PC: ") for w in hang["why"])
    # low memory names who used it; the panel of other users is for slow actions only
    low = by["Low Memory Pagefaults"]
    assert any(w.startswith("Aternity keeps memory per program only per hour") and "erp memory up to" in w
               for w in low["why"])
    assert any(t.startswith("On VD-1: look at erp: memory up to ") and t.endswith("the most of any program.")
               for t in a["todo"])
    assert a["who_else"] is None
    from uxcorrelate.report import render_text
    text = render_text(result)
    assert "What went wrong:" in text and "Possible cause: Oct 28 Classifier" in text and "What changed on VD-1:" in text


def test_a_limit_passed_away_from_slow_actions_is_a_fact_not_a_step():
    class Busy(StubAternity):
        def top_processes(self, device, start, end):
            return [ProcessSample(start + timedelta(minutes=30), "PC1", "erp", cpu=70.0)]

    health = [DeviceHealthSample(T0 + timedelta(minutes=5 * i), "PC1", cpu=97.0 if i == 5 else 10.0) for i in range(24)]
    a = amy(erp(lambda i: False), health=health, source=Busy)["answer"]
    row = next(f for f in a["found"] if "CPU" in f["text"])
    assert row["count"] == "1 of 24 readings" and any("erp CPU up to 70.0%" in w for w in row["why"])
    assert not any("look at erp" in t for t in a["todo"])
    assert a["level"] == "none" and a["title"] == "No problems found"


def test_virtual_desktops_are_never_a_network_point():
    class Pool(StubAternity):
        def app_activity(self, applications, start, end):
            # now 6 of 10 slow on one desktop subnet; earlier weeks none
            quiet = start < T0 - timedelta(days=1)
            return [UserSample(start + timedelta(minutes=1), f"u{i}", f"VD-{i}", "ERP", ip_address=f"10.9.9.{i}",
                               response_time=6.0 if i < 6 and not quiet else 1.0, status="Normal", virtual=True)
                    for i in range(10)]

    samples = [replace(s, virtual=True) for s in erp(lambda i: i == 4)]
    result = amy(samples, source=Pool)
    im = result["impact"]
    assert im["groups"][0]["kind"] == "virtual" and im["common_point"] is None and im["user_group"].startswith("Virtual")
    assert not any("behind" in t for t in result["answer"]["todo"])


def test_other_users_are_compared_with_earlier_working_days():
    days = []

    class Peers(StubAternity):
        def app_activity(self, applications, start, end):
            days.append(start.weekday())
            return []

    amy(erp(lambda i: i == 4), source=Peers)
    assert len(days) == 6 and max(days) < 5  # the window and 5 of the 7 days before: no weekend
