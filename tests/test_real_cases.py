"""Cases seen on the live system, rebuilt with made-up names and the same shape,
so the whole page can be checked on dev before anything goes to prod.
These tests pin what the page says today; a change that alters it shows here
and in scripts/snapshot.sh."""
from datetime import datetime, timedelta, timezone

from uxcorrelate.config import Settings
from uxcorrelate.correlate import InvestigationRequest, investigate
from uxcorrelate.models import AppUsageSample, BootSample, EndpointInfo, UserSample

OUTLOOK = "Microsoft Outlook"


class OutlookAfterLogon:
    """Nov 6 (live): a user on a pooled virtual desktop logs on at 06:18 (User
    Logon 23.6 s, usual about 34.6 s on their desktops) and Outlook's Launch takes
    61.8 s at 06:20, Aternity: Major, split about evenly PC / network / server.
    15 other Outlook actions are normal. 3 Outlook hangs at other times. Of 63
    other Outlook users 14 are slow at 06:20; on earlier days up to 21 were.
    On all PCs (verify, Nov 4-8): Outlook Launch right after a start-up was slow
    67% of the time on virtual desktops, 43% later; physical 43% and 30%. Here,
    the 24 h before: virtual 40 of 60 right after, 17 of 40 later; physical 11 of
    25 and 7 of 25."""
    START = datetime(2026, 11, 5, 16, 16, tzinfo=timezone.utc)
    END = START + timedelta(days=1)
    LAUNCH = datetime(2026, 11, 6, 6, 20, tzinfo=timezone.utc)
    LOGON = LAUNCH - timedelta(minutes=2)

    def user_activity(self, *a, **k):
        normal = [UserSample(self.LAUNCH + timedelta(minutes=10 + 37 * i), "user1", "VD-1", OUTLOOK,
                             activity="Open Mail", response_time=1.5, client_time=0.5, network_time=0.5,
                             backend_time=0.5, virtual=True, client_device="TC-1") for i in range(15)]
        return [UserSample(self.LAUNCH, "user1", "VD-1", OUTLOOK, activity="Launch", response_time=61.8,
                           client_time=21.0, network_time=19.8, backend_time=21.0, status="Major",
                           virtual=True, client_device="TC-1")] + normal

    def device_health(self, *a, **k):
        return []

    def disk_space(self, *a, **k):
        return []

    def app_usage(self, *a, **k):
        return [AppUsageSample(self.LAUNCH + timedelta(hours=h), "VD-1", OUTLOOK, hang_time=8.0) for h in (-5, 4, 8)]

    def endpoint_info(self, device):
        return EndpointInfo(device=device)

    def virtual_devices(self, *a, **k):  # all of the user's desktops are pooled virtual ones
        return {f"VD-{d}" for d in range(1, 10)}

    def boots(self, start, end, username=None, **k):
        if not username:  # all PCs
            rows = [BootSample(t, dev, type="User Logon", duration=30.0) for dev, t, _ in self._fleet()]
            rows.append(BootSample(self.LOGON, "VD-1", "user1", type="User Logon", duration=23.6))
            return [b for b in rows if start <= b.timestamp <= end]
        if start < self.START - timedelta(days=1):  # the user's last 30 days, on other desktops
            return [BootSample(self.LOGON - timedelta(days=d), f"VD-{d + 1}", "user1", type="User Logon",
                               duration=34.6 + (d % 3 - 1)) for d in range(1, 9)]
        return [BootSample(self.LOGON, "VD-1", "user1", type="User Logon", duration=23.6)]

    def _fleet(self):
        """(PC, its start-up, virtual): 60 virtual desktops and 25 physical PCs, one start-up each."""
        first = self.LAUNCH - timedelta(hours=23)
        return ([(f"VF-{i}", first + timedelta(minutes=10 * i), True) for i in range(60)]
                + [(f"PF-{i}", first + timedelta(minutes=10 * i + 5), False) for i in range(25)])

    def activity_history(self, application, activity, start, end, username=None, device=None):
        if username:  # the user's own Outlook launches on earlier days: right after logon, slow
            return [UserSample(self.LAUNCH - timedelta(days=d), "user1", f"VD-{d + 1}", OUTLOOK, "Launch",
                               response_time=45.0, status="Major", virtual=True) for d in range(1, 8)]
        rows, n = [], {True: 0, False: 0}
        for dev, t, virtual in self._fleet():
            i = n[virtual] = n[virtual] + 1
            slow_after = i <= (40 if virtual else 11)  # of 60 / 25
            rows.append(UserSample(t + timedelta(minutes=2), f"f{dev}", dev, OUTLOOK, "Launch", virtual=virtual,
                                   response_time=45.0 if slow_after else 20.0, status="Major" if slow_after else None))
            if i <= (40 if virtual else 25):  # a launch 3 h later: 17 of 40 / 7 of 25 slow
                slow_later = i <= (17 if virtual else 7)
                rows.append(UserSample(t + timedelta(hours=3), f"f{dev}", dev, OUTLOOK, "Launch", virtual=virtual,
                                       response_time=35.0 if slow_later else 20.0,
                                       status="Major" if slow_later else None))
        return [r for r in rows if start <= r.timestamp <= end]

    def boot_components(self, *a, **k):
        return []

    def app_activity(self, applications, start, end):
        days_back = (self.LAUNCH.date() - start.date()).days
        slow = 14 if days_back == 0 else 21 - days_back % 3  # up to 21 of 63 on earlier days
        return [UserSample(start + timedelta(minutes=1), f"u{i}", f"VD-{100 + i}", OUTLOOK, activity="Launch",
                           response_time=40.0 if i < slow else 3.0, status="Major" if i < slow else None,
                           virtual=True) for i in range(63)]


def outlook_after_logon():
    settings = Settings()
    settings.thresholds.slow_status = ["Minor", "Major"]
    c = OutlookAfterLogon
    return investigate(InvestigationRequest(start=c.START, end=c.END, username="user1"), c(), None, settings)


def test_outlook_launch_after_a_normal_logon_names_the_launch():
    result = outlook_after_logon()
    a = result["answer"]
    assert a["felt"]["slowest"] == {"app": OUTLOOK, "activity": "Launch", "seconds": 61.8,
                                    "when": "2026-11-06T06:20:00Z", "device": "VD-1", "status": "Major"}
    assert a["title"] == ("Microsoft Outlook Launch took 61.8 s (Major) right after a start-up: "
                          "67% of those on virtual desktops are slow")
    assert a["level"] == "possible"
    assert ("On virtual desktops in the 24 h before, Microsoft Outlook Launch right after a start-up was slow "
            "40 of 60 times (67%, median 45.0 s); later 17 of 40 times (42%, median 20.0 s).") in a["reason"]
    assert a["todo"][0].startswith("Microsoft Outlook Launch right after a start-up is slow on many virtual desktops")
    assert not any("right after a start-up" in c for c in a["checked"])  # said once, in the reason
    assert "VD-1 was starting up near it: User Logon 23.6 s (usual 34.6 s)." in a["reason"]
    assert not any("starting up" in g["what"] for g in result["problems"])  # a normal logon is no problem
    assert [r["issue"] for r in a["rollup"]] == [f"{OUTLOOK} not responding"]


class LaunchAsUsual(OutlookAfterLogon):
    """The same, but on all PCs Outlook Launch is slow as often right after a start-up as later."""
    def activity_history(self, application, activity, start, end, username=None, device=None):
        rows = super().activity_history(application, activity, start, end, username, device)
        if username:
            return rows
        return [r.__class__(**{**r.__dict__, "response_time": 20.0, "status": "Major" if i % 3 == 0 else None})
                for i, r in enumerate(rows)]


def test_a_launch_not_slower_after_start_ups_elsewhere_is_not_the_headline():
    settings = Settings()
    settings.thresholds.slow_status = ["Minor", "Major"]
    c = LaunchAsUsual
    a = investigate(InvestigationRequest(start=c.START, end=c.END, username="user1"), c(), None, settings)["answer"]
    assert a["title"] == "Microsoft Outlook Launch took 61.8 s (Major): no cause found"
    said = [x for x in a["checked"] if "right after a start-up" in x]
    assert len(said) == 1 and said[0].endswith("Not slower right after a start-up: not the reason.")
