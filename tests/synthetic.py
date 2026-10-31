"""Synthetic Aternity + NetIM sources for the engine tests.

Scenario: user `jdoe` on laptop LT-JDOE-01 at Branch-Denver uses "CRM Portal"
(server 10.100.5.20 in the data center). Between +60 and +105 minutes into the
window the branch WAN uplink saturates; CRM response time climbs and Aternity
attributes most of it to network time. A brief, unrelated laptop CPU spike
happens later while the user experience is fine.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from uxcorrelate.config import NetimMetric, Settings, Topology
from uxcorrelate.models import (
    AppUsageSample, BootComponent, BootSample, DeviceHealthSample, EndpointInfo, HealthEvent, MetricSample, NetimAlert, NetimDevice,
    NetimInterface, UserSample,
)

INCIDENT = (60, 105)  # minutes from window start
CPU_SPIKE = (150, 160)


def demo_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    end = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 5)
    return end - timedelta(hours=3), end


def demo_topology() -> Topology:
    return Topology(
        applications={"CRM Portal": ["10.100.5.20"]},
        sites={"Branch-Denver": ["den-br-rtr01"]},
        always_include=[],
    )


def _in(minute: float, window: tuple[int, int]) -> bool:
    return window[0] <= minute < window[1]


class DemoAternity:
    def __init__(self, start: datetime):
        self.origin = start
        self.rng = random.Random(42)

    def _minutes(self, start, end, step):
        t = max(start, self.origin)
        while t <= end:
            yield t, (t - self.origin).total_seconds() / 60
            t += timedelta(minutes=step)

    def user_activity(self, start, end, username=None, device=None, application=None):
        if username not in (None, "jdoe") or device not in (None, "LT-JDOE-01"):
            return []
        if application not in (None, "CRM Portal"):
            return []
        out = []
        for t, m in self._minutes(start, end, 2):
            client = self.rng.uniform(0.2, 0.4)
            backend = self.rng.uniform(0.5, 0.9)
            network = self.rng.uniform(0.2, 0.4)
            if _in(m, INCIDENT):
                network = self.rng.uniform(4.0, 7.5)
            if _in(m, CPU_SPIKE):
                client += 0.3
            rt = client + backend + network
            out.append(UserSample(
                timestamp=t, username="jdoe", device="LT-JDOE-01", application="CRM Portal",
                activity="Open Account", response_time=round(rt, 3),
                client_time=round(client, 3), network_time=round(network, 3),
                backend_time=round(backend, 3),
            ))
        return out

    def device_health(self, device, start, end):
        out = []
        for t, m in self._minutes(start, end, 5):
            out.append(DeviceHealthSample(
                timestamp=t, device=device,
                cpu=96.0 if _in(m, CPU_SPIKE) else self.rng.uniform(15, 40),
                memory=self.rng.uniform(55, 70),
                disk_queue=self.rng.uniform(0.1, 0.6),
                network_latency=self.rng.uniform(180, 260) if _in(m, INCIDENT) else self.rng.uniform(20, 40),
            ))
        return out

    def app_activity(self, applications, start, end):
        # other CRM users: 20 more at Branch-Denver (slow during the incident, like jdoe)
        # and 30 at other sites (fine)
        # (earlier days are answered too, all normal, for the "usual" comparison)
        out = []
        for n in range(50):
            loc = "Branch-Denver" if n < 20 else "HQ"
            ip = f"10.20.30.{100 + n}" if n < 20 else f"10.50.0.{n}"
            t = start
            while t <= end:
                m = (t - self.origin).total_seconds() / 60
                slow = loc == "Branch-Denver" and _in(m, INCIDENT)
                out.append(UserSample(
                    timestamp=t, username=f"user{n:02d}", device=f"PC-{n:02d}", application="CRM Portal",
                    location=loc, ip_address=ip, response_time=6.0 if slow else 1.2,
                    client_time=0.3, network_time=5.0 if slow else 0.3, backend_time=0.7,
                ))
                t += timedelta(minutes=5)
        return out

    def boots(self, start, end, username=None, device=None, location=None):
        # one logon a day per user, 1 h into the window; Denver logons are slow today
        out = []
        for n in range(-1, 50):
            user, dev = ("jdoe", "LT-JDOE-01") if n < 0 else (f"user{n:02d}", f"PC-{n:02d}")
            loc = "Branch-Denver" if n < 20 else "HQ"
            if (username and user != username) or (device and dev != device) or (location and loc != location):
                continue
            day = self.origin + timedelta(hours=1)
            while day > start:
                day -= timedelta(days=1)
            day += timedelta(days=1)
            while day <= end:
                today = day >= self.origin
                slow = today and loc == "Branch-Denver"
                out.append(BootSample(timestamp=day, device=dev, username=user, location=loc, type="Logon",
                                      duration=(90.0 if slow else 30.0) + (n % 5), started=day))
                day += timedelta(days=1)
        return out

    def _components(self, start, end):
        # slow Denver logons: Group Policy takes 45 s longer than normal
        return [BootComponent(timestamp=b.timestamp, device=b.device, type="Logon", component=c,
                              duration=d, degradation=g, username=b.username, location=b.location)
                for b in self.boots(start, end) if b.duration > 60
                for c, d, g in (("Group Policy", 55.0, 45.0), ("User profile", 20.0, 5.0))]

    def boot_components(self, devices, start, end):
        return [c for c in self._components(start, end) if c.device in devices]

    def boot_component_everywhere(self, component, type, start, end):
        return [c for c in self._components(start, end) if c.component == component and c.type == type]

    def health_events(self, device, start, end):
        # one application crash during the CPU spike (the user was fine then)
        return [HealthEvent(timestamp=t, device=device, application="Outlook", category="Crash",
                            name="Application crash", component="OUTLOOK.EXE", count=1)
                for t, m in self._minutes(start, end, 5) if _in(m, CPU_SPIKE)][:1]

    def app_usage(self, device, start, end, application=None):
        return [AppUsageSample(timestamp=t, device=device, application="CRM Portal",
                               uxi=3.5 if _in(m, INCIDENT) else 8.5)
                for t, m in self._minutes(start, end, 5)]

    def endpoint_info(self, device):
        return EndpointInfo(device=device, ip_address="10.20.30.45",
                            location="Branch-Denver", os="Windows 11")


class DemoNetim:
    DEVICES = [
        NetimDevice("d1", "den-br-rtr01", "10.20.30.1", "router", "Branch-Denver"),
        NetimDevice("d2", "dc-core-sw01", "10.100.0.2", "switch", "DC-East"),
        NetimDevice("d3", "crm-app01", "10.100.5.20", "server", "DC-East"),
    ]
    INTERFACES = {
        "d1": [NetimInterface("i11", "d1", "Gi0/0 (LAN)", ["10.20.30.1/24"], 1e9),
               NetimInterface("i12", "d1", "Gi0/1 (WAN-MPLS)", ["172.16.20.2/30"], 50e6)],
        "d2": [NetimInterface("i21", "d2", "Vlan100", ["10.100.5.1/24"], 10e9),
               NetimInterface("i22", "d2", "Te1/1", [], 10e9)],
        "d3": [NetimInterface("i31", "d3", "eth0", ["10.100.5.20/24"], 10e9)],
    }

    def __init__(self, start: datetime):
        self.origin = start
        self.rng = random.Random(7)
        self.warnings: list[str] = []

    def devices(self):
        return list(self.DEVICES)

    def interfaces(self, device_id):
        return list(self.INTERFACES.get(device_id, []))

    def metrics(self, metric: NetimMetric, object_ids, start, end):
        out = []
        t = start
        while t <= end:
            m = (t - self.origin).total_seconds() / 60
            for oid in object_ids:
                out.append(MetricSample(metric.object_type, oid, metric.key, t,
                                        self._value(metric.key, oid, m)))
            t += timedelta(minutes=5)
        return out

    def _value(self, key, oid, m):
        r = self.rng.uniform
        wan_hot = oid == "i12" and _in(m, INCIDENT)
        return {
            "if_util_in": r(85, 99) if wan_hot else r(5, 35),
            "if_util_out": r(90, 99) if wan_hot else r(5, 30),
            "if_errors": 0.0,
            "if_discards": r(200, 900) if wan_hot else r(0, 5),
            "dev_cpu": r(60, 75) if oid == "d1" and _in(m, INCIDENT) else r(10, 35),
            "dev_mem": r(40, 60),
            "dev_latency": r(170, 240) if oid == "d1" and _in(m, INCIDENT) else r(2, 20),
        }.get(key, 0.0)

    def alerts(self, start, end):
        a_start = self.origin + timedelta(minutes=INCIDENT[0] + 5)
        a_end = self.origin + timedelta(minutes=INCIDENT[1] - 5)
        if a_end < start or a_start > end:
            return []
        return [NetimAlert("d1", "den-br-rtr01", "major",
                           "Gi0/1 (WAN-MPLS) outbound utilization above 90%", a_start, a_end)]


def demo_sources(settings: Settings, now: datetime | None = None):
    start, end = demo_window(now)
    settings.topology = demo_topology()
    return DemoAternity(start), DemoNetim(start), start, end
