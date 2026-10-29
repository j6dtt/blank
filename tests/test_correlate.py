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
