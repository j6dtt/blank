from datetime import datetime, timedelta, timezone

import pytest

from uxcorrelate.config import Settings
from uxcorrelate.correlate import InvestigationRequest, investigate
from synthetic import CPU_SPIKE, INCIDENT, DemoAternity, DemoNetim, demo_sources
from uxcorrelate.models import DeviceHealthSample, UserSample

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def run_demo(**overrides):
    settings = Settings()
    aternity, netim, start, end = demo_sources(settings, now=NOW)
    req = InvestigationRequest(start=start, end=end, username="jdoe", application="CRM Portal")
    for k, v in overrides.items():
        setattr(req, k, v)
    return investigate(req, aternity, netim, settings), start


def test_demo_points_at_network():
    result, start = run_demo()
    summary = result["summary"]
    assert summary["verdict"] == "network"
    assert summary["confidence"] > 0.6
    assert result["scores"]["network"]["overlap"] == 1.0
    assert result["scores"]["client"]["score"] < 0.25
    objects = {e["object"] for e in result["evidence"]}
    assert "den-br-rtr01:Gi0/1 (WAN-MPLS)" in objects

    degraded = [t for t in result["timeline"] if t["degraded"]]
    first = datetime.fromisoformat(degraded[0]["time"].replace("Z", "+00:00"))
    assert first == start + timedelta(minutes=INCIDENT[0])


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
    assert result["summary"]["verdict"] == "none"
    # the CPU spike is still visible on the timeline, just not blamed
    assert any(t["client"] for t in result["timeline"])


def test_requires_user_or_device():
    settings = Settings()
    with pytest.raises(ValueError):
        investigate(InvestigationRequest(start=NOW - timedelta(hours=1), end=NOW),
                    DemoAternity(NOW), DemoNetim(NOW), settings)


class StubAternity:
    def __init__(self, samples, health):
        self.samples, self.health = samples, health

    def user_activity(self, *a, **k):
        return self.samples

    def device_health(self, *a, **k):
        return self.health

    def endpoint_info(self, device):
        from uxcorrelate.models import EndpointInfo
        return EndpointInfo(device=device)


def test_client_verdict_without_netim():
    t0 = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    samples, health = [], []
    for i in range(24):
        t = t0 + timedelta(minutes=5 * i)
        bad = 8 <= i < 12
        samples.append(UserSample(t, "amy", "PC1", "ERP", response_time=6.0 if bad else 1.0,
                                  client_time=5.0 if bad else 0.3, network_time=0.3, backend_time=0.4))
        health.append(DeviceHealthSample(t, "PC1", cpu=99.0 if bad else 20.0, memory=50.0))
    settings = Settings()
    result = investigate(InvestigationRequest(start=t0, end=t0 + timedelta(hours=2), username="amy"),
                         StubAternity(samples, health), None, settings)
    assert result["summary"]["verdict"] == "client"
    assert result["scores"]["client"]["overlap"] == 1.0
    assert any("NetIM not configured" in w for w in result["warnings"])


def test_health_events_and_uxi_reach_the_result():
    result, _ = run_demo()
    s = result["summary"]
    assert s["health_events"] == 1 and s["uxi_min"] == 3.5 and s["uxi_avg"] is not None
    assert result["summary"]["verdict"] == "network"  # the crash came while the user was fine
    crash = [a for b in result["timeline"] for a in b["client"] if a["source"] == "Aternity event"]
    assert crash and crash[0]["metric"] == "Crash / Application crash (OUTLOOK.EXE)"
    slow = [b["avg_uxi"] for b in result["timeline"] if b["degraded"]]
    assert slow and set(slow) == {3.5}


def test_aternity_time_split_alone_is_not_a_verdict():
    # one slow activity, nothing measured lined up with it (seen on prod)
    t0 = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    samples = [UserSample(t0 + timedelta(minutes=5 * i), "amy", "PC1", "ERP",
                          response_time=38.0 if i == 3 else 0.5,
                          client_time=38.0 if i == 3 else 0.2, network_time=0.0, backend_time=0.0)
               for i in range(24)]
    health = [DeviceHealthSample(t0 + timedelta(minutes=5 * 10), "PC1", cpu=99.0)]  # later, not during
    result = investigate(InvestigationRequest(start=t0, end=t0 + timedelta(hours=2), username="amy"),
                         StubAternity(samples, health), None, Settings())
    s = result["summary"]
    assert s["verdict"] == "inconclusive" and s["confidence"] == 0.0
    assert "Aternity counts 100% of the slow time as client device time" in s["headline"]


def test_who_else_finds_the_site_and_the_affected_users():
    result, _ = run_demo()
    impact = result["impact"]
    assert impact["scope"] == "site" and impact["location"] == "Branch-Denver"
    assert impact["same_location"] == {"users": 20, "slow": 20}
    assert impact["other_locations"] == {"users": 30, "slow": 0}
    assert impact["affected_total"] == 20 and impact["affected_users"][0]["location"] == "Branch-Denver"
    assert impact["slow_time_split"]["network"] > 0.7
    cause = result["summary"]["cause"]
    assert cause.startswith("Site Branch-Denver: 20 of 20 other users") and "Measured: den-br-rtr01" in cause


def test_who_else_says_when_only_this_user_is_slow():
    t0 = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    samples = [UserSample(t0 + timedelta(minutes=5 * i), "amy", "PC1", "ERP", location="HQ",
                          response_time=6.0 if i == 3 else 1.0) for i in range(24)]

    class WithPeers(StubAternity):
        def app_activity(self, applications, start, end):
            return [UserSample(start, f"u{n}", f"P{n}", "ERP", location="HQ", response_time=1.0)
                    for n in range(10)]

    result = investigate(InvestigationRequest(start=t0, end=t0 + timedelta(hours=2), username="amy"),
                         WithPeers(samples, []), None, Settings())
    assert result["impact"]["scope"] == "user"
    assert result["impact"]["text"].startswith("Only amy: 0 of 10 other users at HQ")


def test_investigation_includes_the_users_logons():
    result, _ = run_demo()
    lg = result["logons"]
    assert lg["total"] == 1 and lg["slowest"][0]["username"] == "jdoe"
