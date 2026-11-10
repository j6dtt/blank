"""Cases seen on the live system, rebuilt with made-up names and the same shape,
so the whole page can be checked on dev before anything goes to prod.
These tests pin what the page says today; a change that alters it shows here
and in scripts/snapshot.sh."""
import re
from datetime import datetime, timedelta, timezone

from uxcorrelate.config import Settings
from uxcorrelate.correlate import InvestigationRequest, investigate
from uxcorrelate.models import (AppUsageSample, BootSample, DeviceHealthSample, EndpointInfo, HealthEvent, ProcessSample,
                                SoftwareChange, UserSample)

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
    # both shares: slow right after a start-up, and how often it is slow later anyway
    assert a["title"] == ("Microsoft Outlook Launch took 61.8 s on VD-1 (Major), 2 min after a start-up. "
                          "On virtual desktops 67% are slow then, 42% later")
    assert a["level"] == "possible"
    assert ("On virtual desktops in the 24 h before, Microsoft Outlook Launch right after a start-up was slow "
            "40 of 60 times (67%, median 45.0 s); later 17 of 40 times (42%, median 20.0 s).") in a["reason"]
    assert a["todo"][0].startswith("Microsoft Outlook Launch on virtual desktops is slow 67% of the time right "
                                   "after a start-up, 42% later, for many users, not only user1.")
    assert not any("right after a start-up" in c for c in a["checked"])  # said once, in the reason
    assert "VD-1 was starting up near it: User Logon 23.6 s (usual 34.6 s)." in a["reason"]
    assert not any("starting up" in g["what"] for g in result["problems"])  # a normal logon is no problem
    # the Outlook hangs at other times: below the answer, not in "What went wrong"
    assert a["rollup"] == [] and [x["what"] for x in a["elsewhere"]] == [f"{OUTLOOK} not responding"]


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
    # slow on each day the user ran it: not new, which comes before "others as usual"
    assert a["title"] == ("Microsoft Outlook Launch took 61.8 s on VD-1 (Major), usually 45.0 s. Aternity "
                          "marked most runs slow on each of the 7 days user1 used it: not new")
    said = [x for x in a["checked"] if "right after a start-up" in x]
    assert len(said) == 1 and said[0].endswith("Not slower right after a start-up: not the reason.")


class TeamsAfterLogonBusyPC:
    """Nov 8 (live): a user on two pooled virtual desktops. On VD-14 they log on at
    09:29 (User Logon 37.4 s, usual 47.6 s) and Teams Launch takes 43.8 s (Minor) at
    09:32, CPU 58.5% at that action; a normal action at 09:33 has CPU 88.98%.
    Aternity's hourly rows per program: explorer 9.0%, Outlook 5.5%, Teams 3.1%.
    On VD-41 Adobe Acrobat hangs twice on Nov 6; the log has, on Nov 5 at 13:11,
    25 programs "Uninstalled" with no install (Acrobat among them) and real updates.
    On all PCs: Teams Launch right after a start-up slow 89 of 96 (virtual),
    later 70 of 113."""
    START = datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc)
    END = datetime(2026, 11, 8, 11, 0, tzinfo=timezone.utc)
    LOGON = datetime(2026, 11, 8, 9, 29, tzinfo=timezone.utc)
    LAUNCH = LOGON + timedelta(minutes=3)
    IMAGE = datetime(2026, 11, 5, 13, 11, tzinfo=timezone.utc)

    def user_activity(self, *a, **k):
        act = lambda t, dev, what, rt, cpu, status=None: UserSample(  # noqa: E731
            t, "user2", dev, "Microsoft Teams" if what == "Launch" else OUTLOOK, activity=what, response_time=rt,
            client_time=rt * .82, network_time=rt * .07, backend_time=rt * .11, status=status, cpu=cpu,
            memory=37.0, disk_queue=0.1, virtual=True, client_device="TC-2")
        return ([act(self.LAUNCH, "VD-14", "Launch", 43.8, 58.5, "Minor"),
                 act(self.LAUNCH + timedelta(minutes=1), "VD-14", "Open Mail", 1.0, 88.98)]
                + [act(self.LAUNCH + timedelta(minutes=10 + 5 * i), "VD-14", "Open Mail", 1.0, 20.0) for i in range(6)]
                + [act(self.START + timedelta(hours=2, minutes=10 * i), "VD-41", "Open Mail", 1.0, 7.0)
                   for i in range(12)])

    def device_health(self, device, start, end):  # as the live client: the PC's values on the user's actions
        return [DeviceHealthSample(s.timestamp, s.device, cpu=s.cpu, memory=s.memory, disk_queue=s.disk_queue)
                for s in self.user_activity() if s.device == device and start <= s.timestamp <= end]

    def disk_space(self, *a, **k):
        return []

    def app_usage(self, device, start, end, application=None):
        rows = [AppUsageSample(datetime(2026, 11, 6, 11, m, tzinfo=timezone.utc), "VD-41", "Adobe Acrobat",
                               hang_time=2856.0) for m in (0, 5)]
        return [u for u in rows if u.device == device and start <= u.timestamp <= end]

    def endpoint_info(self, device):
        return EndpointInfo(device=device)

    def virtual_devices(self, *a, **k):
        return {"VD-14", "VD-41"} | {f"VD-{50 + d}" for d in range(1, 9)} | {f"VF-{i}" for i in range(220)}

    def _fleet(self):
        first = self.LAUNCH - timedelta(hours=23)
        return [(f"VF-{i}", first + timedelta(minutes=6 * i)) for i in range(113)]

    def boots(self, start, end, username=None, **k):
        if not username:
            rows = [BootSample(t, dev, type="User Logon", duration=40.0) for dev, t in self._fleet()]
            rows.append(BootSample(self.LOGON, "VD-14", "user2", type="User Logon", duration=37.4))
            return [b for b in rows if start <= b.timestamp <= end]
        if start < self.START - timedelta(days=1):
            return [BootSample(self.LOGON - timedelta(days=d), f"VD-{50 + d}", "user2", type="User Logon",
                               duration=47.6) for d in range(1, 9)]
        return [BootSample(self.LOGON, "VD-14", "user2", type="User Logon", duration=37.4)]

    def boot_components(self, *a, **k):
        return []

    def activity_history(self, application, activity, start, end, username=None, device=None):
        if username or application != "Microsoft Teams":
            return []
        rows = []
        for i, (dev, t) in enumerate(self._fleet()):
            if i < 96:  # 96 launches right after a start-up, 89 slow
                rows.append(UserSample(t + timedelta(minutes=2), f"f{i}", dev, "Microsoft Teams", "Launch",
                                       response_time=66.7 if i < 89 else 20.0, status="Minor" if i < 89 else None,
                                       virtual=True))
            rows.append(UserSample(t + timedelta(hours=3), f"f{i}", dev, "Microsoft Teams", "Launch",  # 113 later
                                   response_time=33.6 if i < 70 else 20.0, status="Minor" if i < 70 else None,
                                   virtual=True))
        return [r for r in rows if start <= r.timestamp <= end]

    def top_processes(self, device, start, end):
        t = self.LAUNCH.replace(minute=0)
        rows = [ProcessSample(t, "VD-14", p, a, cpu=c) for p, a, c in
                (("explorer", "Windows Explorer", 9.0), ("outlook", OUTLOOK, 5.5), ("ms-teams", "Microsoft Teams", 3.1))]
        return [p for p in rows if p.device == device and start <= p.timestamp <= end]

    def software_changes(self, device, start, end):
        if device != "VD-41":
            return []
        gone = ["Adobe Acrobat (64-bit)", "Google Chrome", "Microsoft Edge", "Java 8 Update 501"] + \
            [f"Runtime {i}" for i in range(21)]
        rows = [SoftwareChange(self.IMAGE, device, n, "Uninstalled", "1.0") for n in gone] + [
            SoftwareChange(self.IMAGE, device, "Aternity Agent", "Uninstalled", "12.2.4.79"),
            SoftwareChange(self.IMAGE, device, "Aternity Agent", "Installed", "12.2.4.113")]
        return [c for c in rows if start <= c.timestamp <= end]


def teams_after_logon():
    settings = Settings()
    settings.thresholds.slow_status = ["Minor", "Major"]
    c = TeamsAfterLogonBusyPC
    return investigate(InvestigationRequest(start=c.START, end=c.END, username="user2"), c(), None, settings)


def test_a_busy_pc_after_logon_is_told_plainly():
    a = teams_after_logon()["answer"]
    result = teams_after_logon()
    # the fleet's count is the answer, with how often it is slow later anyway
    assert a["title"] == ("Microsoft Teams Launch took 43.8 s on VD-14 (Minor), 3 min after a start-up. "
                          "On virtual desktops 93% are slow then, 62% later")
    # the CPU reading over its limit came with another action a minute later: it does not
    # count for the slow launch (its own reading, 58.5%, was under the limit)
    assert ("No problem was recorded within 5 min of the slow action, other than a PC reading over its limit "
            "taken with another action (see Other things on VD-14 and VD-41).") in a["reason"]
    assert a["found"] == [] and a["rollup"] == []
    cpu = next(f for f in a["elsewhere"] if f["text"].startswith("VD-14: CPU"))
    assert cpu["count"] == "1 of 8 readings, none with a slow action"
    assert a["todo"] == [
        "Microsoft Teams Launch on virtual desktops is slow 93% of the time right after a start-up, 62% later, "
        "for many users, not only user2. Aternity does not say why: a question for whoever builds the virtual "
        "desktops."]
    assert a["checked"][0].startswith("PC during the slow action, as Aternity read it with it: highest CPU 58.5 %")
    assert cpu["why"][0] == ("Aternity reads the PC's CPU with each of the user's actions: the highest reading was "
                             "88.98%, 1 min after the slow Microsoft Teams Launch (at that action itself: 58.5%).")
    assert cpu["why"][1] == "It was 4 min after VD-14 started up (User Logon)."
    assert not any("find which process" in t for t in a["todo"])
    assert cpu["why"][2].startswith("Aternity keeps CPU per program only per hour, so it cannot show which program")
    assert not any("look at explorer" in t for t in a["todo"])  # 9% cannot explain 88.98%
    # the Acrobat hangs: another PC, two days before: below the answer, no number without a
    # unit (not even on the timeline's hover), and an "Uninstalled" row is no cause
    hang = next(f for f in a["elsewhere"] if f["text"].startswith("Adobe Acrobat"))
    assert "(see Other things on VD-14 and VD-41)" in a["reason"]
    assert not any("2856" in m[2] for m in result["timeline"]["marks"])
    assert hang["text"] == "Adobe Acrobat was not responding on VD-41" and hang["possible"] == []
    # its "uninstalled" row is one of the 27 in one minute: told once in What changed, not here
    assert not any("as uninstalled on Nov 5" in w for w in hang["why"])
    assert not any("Adobe Acrobat" in t and "Check the" in t for t in a["todo"])
    # the 25 removals of one minute: one line; the real update listed
    vd41 = next(c for c in a["changes"] if c["device"] == "VD-41")
    texts = [c["text"] for c in vd41["changes"]]
    assert any(t.startswith("25 programs logged as uninstalled in the same minute, with no install logged: ")
               for t in texts)
    assert any(t.startswith("Aternity Agent updated 12.2.4.79 → 12.2.4.113") for t in texts)


class TeamsAndOutlookBusyPC(TeamsAfterLogonBusyPC):
    """The same day as seen on the live page: besides the Teams Launch, slow Outlook
    actions; one of them on VD-14 has its own CPU reading over the limit (86.2%, a
    made-up value: the live page counted "2 of 26 readings, with 1 of 4 slow actions"
    but told only the highest reading, which was with no slow action)."""
    def user_activity(self, *a, **k):
        rows = super().user_activity(*a, **k)
        extra = UserSample(self.LAUNCH + timedelta(minutes=20), "user2", "VD-14", OUTLOOK, activity="Preview Mail",
                           response_time=9.0, client_time=7.0, network_time=1.0, backend_time=1.0, status="Minor",
                           cpu=86.2, memory=37.0, disk_queue=0.1, virtual=True, client_device="TC-2")
        return sorted(rows + [extra], key=lambda s: s.timestamp)


def test_a_reading_with_a_slow_action_is_the_one_told():
    settings = Settings()
    settings.thresholds.slow_status = ["Minor", "Major"]
    c = TeamsAndOutlookBusyPC
    a = investigate(InvestigationRequest(start=c.START, end=c.END, username="user2"), c(), None, settings)["answer"]
    cpu = next(f for f in a["found"] if f["text"].startswith("VD-14: CPU"))
    assert cpu["count"] == "2 of 9 readings, with 1 of 2 slow actions"
    # the highest is told with where it came from; the one that counts is told too
    assert cpu["why"][0].startswith("Aternity reads the PC's CPU with each of the user's actions: the highest "
                                    "reading was 88.98%, 1 min after the slow Microsoft Teams Launch")
    assert "Over its limit with a slow action: 86.2% with the slow Microsoft Outlook Preview Mail." in cpu["why"]


class SameMinuteReading(TeamsAfterLogonBusyPC):
    """As on the live page: another action in the same minute as the slow Teams Launch
    has CPU 87.41%; the launch's own row has 58.5%. Aternity gives both the same time."""
    def user_activity(self, *a, **k):
        rows = super().user_activity(*a, **k)
        other = UserSample(self.LAUNCH, "user2", "VD-14", OUTLOOK, activity="Open Mail", response_time=1.0,
                           cpu=87.41, memory=37.0, disk_queue=0.1, virtual=True, client_device="TC-2")
        return sorted(rows + [other], key=lambda s: s.timestamp)


def test_a_reading_in_the_same_minute_is_not_the_slow_actions_own():
    settings = Settings()
    settings.thresholds.slow_status = ["Minor", "Major"]
    c = SameMinuteReading
    a = investigate(InvestigationRequest(start=c.START, end=c.END, username="user2"), c(), None, settings)["answer"]
    assert a["found"] == []  # the launch's own CPU (58.5%) was under the limit
    cpu = next(f for f in a["elsewhere"] if f["text"].startswith("VD-14: CPU"))
    assert cpu["count"] == "2 of 9 readings, none with a slow action"
    assert not any("with a slow action:" in w for w in cpu["why"])
    assert a["title"].startswith("Microsoft Teams Launch took 43.8 s on VD-14 (Minor), 3 min after a start-up.")


class OutlookPreviewNew(SameMinuteReading):
    """The full Nov 6-8 page (live), names made up: besides the Teams Launch on VD-14,
    on VD-41 (logon 08:12, CPU 91.75% with a normal action at 08:15) Outlook Launch
    takes 40.6 s at 09:15 (others typical 33.5 s) while an AAD token broker hang
    starts in the same minute; Preview Mail is slow once on each desktop (VD-41 3.6 s on Nov 6,
    VD-14 5.2 s on Nov 8 09:58), each 1 of 11 times: occasional, not a slowdown, so not
    the headline; VD-14 has no change logged (usually 0.2 s for
    the user, on the 3 days before, last on another desktop; others 0.1 s). VD-41's
    log on Nov 5 13:11: three updates (Teams Meeting Add-in, OneDrive, the agent)
    and 27 "Uninstalled" rows with no install (Microsoft 365 Apps among them)."""
    D6 = datetime(2026, 11, 6, tzinfo=timezone.utc)
    LOGON41 = D6 + timedelta(hours=8, minutes=12)
    OL_LAUNCH = D6 + timedelta(hours=9, minutes=15)
    PREVIEW = [D6 + timedelta(hours=10, minutes=5), datetime(2026, 11, 8, 9, 58, tzinfo=timezone.utc)]

    def user_activity(self, *a, **k):
        mk = lambda t, what, rt, cpu, status=None, dev="VD-41": UserSample(  # noqa: E731
            t, "user2", dev, OUTLOOK, activity=what, response_time=rt, client_time=rt * .8,
            network_time=rt * .1, backend_time=rt * .1, status=status, cpu=cpu, memory=50.0, disk_queue=0.0,
            virtual=True, client_device="TC-3" if dev == "VD-41" else "TC-2")
        rows = super().user_activity(*a, **k) + [
            mk(self.LOGON41 + timedelta(minutes=3), "Open Mail", 1.0, 91.75),
            mk(self.OL_LAUNCH, "Launch", 40.6, 30.0, "Minor"),
            # one slow Preview Mail on each desktop, each slow 1 of 11 times (prod, Nov 9 run):
            # VD-41 3.6 s on Nov 6, VD-14 5.2 s on Nov 8
            mk(self.PREVIEW[0], "Preview Mail", 3.6, 7.9, "Minor"),
            mk(self.PREVIEW[1], "Preview Mail", 5.2, 7.9, "Minor", "VD-14")] + [
            mk(self.PREVIEW[0] + timedelta(hours=h), "Preview Mail", 0.2, 7.9) for h in range(1, 11)] + [
            mk(self.PREVIEW[1] + timedelta(minutes=m), "Preview Mail", 0.2, 7.9, None, "VD-14")
            for m in range(1, 11)]
        return sorted(rows, key=lambda s: s.timestamp)

    def boots(self, start, end, username=None, **k):
        rows = super().boots(start, end, username, **k)
        if username and start >= self.START - timedelta(days=1):
            rows.append(BootSample(self.LOGON41, "VD-41", "user2", type="User Logon", duration=30.0))
        return rows

    def app_usage(self, device, start, end, application=None):
        rows = super().app_usage(device, start, end, application)
        aad = [AppUsageSample(self.OL_LAUNCH, "VD-41", "AAD token broker plugin", hang_time=120.0)]
        return rows + [u for u in aad if u.device == device and start <= u.timestamp <= end]

    def activity_history(self, application, activity, start, end, username=None, device=None):
        if application != OUTLOOK:
            return super().activity_history(application, activity, start, end, username, device)
        rows = []
        if activity == "Preview Mail":
            for d in (3, 4, 5):  # the user's 3 days before, normal, on another desktop
                rows += [UserSample(datetime(2026, 11, d, 9, m, tzinfo=timezone.utc), "user2", "VD-2E",
                                    OUTLOOK, "Preview Mail", response_time=0.2, virtual=True) for m in (0, 20)]
            rows += [UserSample(t + timedelta(minutes=1), f"o{i}", f"VF-{i}", OUTLOOK, "Preview Mail",
                                response_time=0.1, virtual=True) for t in self.PREVIEW for i in range(5)]
        elif activity == "Launch":
            rows.append(UserSample(datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc), "user2", "VD-2E", OUTLOOK,
                                   "Launch", response_time=30.0, virtual=True))
            rows += [UserSample(self.OL_LAUNCH, f"o{i}", f"VF-{i}", OUTLOOK, "Launch", response_time=33.5,
                                virtual=True) for i in range(4)]
        if username:
            rows = [r for r in rows if r.username == username]
        return [r for r in rows if start <= r.timestamp <= end]

    def software_changes(self, device, start, end):
        if device != "VD-41":
            return []
        t = self.IMAGE
        rows = [SoftwareChange(t, device, n, ch, v) for n, a, b in (
            ("Microsoft Teams Meeting Add-in for Microsoft Office", "1.26.08901", "1.26.18801"),
            ("Microsoft OneDrive", "26.032.0217.0003", "26.063.0405.0002"),
            ("Aternity Agent", "12.2.4.79", "12.2.4.113")) for ch, v in (("Uninstalled", a), ("Installed", b))]
        gone = ["Microsoft 365 Apps for enterprise - en-us",
                "Microsoft Visual C++ 2015-2022 Redistributable (x86) - 14.44.35211"] + [f"Update {i}" for i in range(25)]
        rows += [SoftwareChange(t, device, n, "Uninstalled", "1.0") for n in gone]
        return [c for c in rows if start <= c.timestamp <= end]


def test_the_full_page_of_nov_8_has_no_slowdown_to_blame():
    settings = Settings()
    settings.thresholds.slow_status = ["Minor", "Major"]
    c = OutlookPreviewNew
    a = investigate(InvestigationRequest(start=c.START, end=c.END, username="user2"), c(), None, settings)["answer"]
    # Preview Mail was slow 1 of 11 times on each desktop: occasional, not a slowdown, so it
    # never leads; the Teams launch after a logon does, as on most virtual desktops
    assert a["title"] == ("Microsoft Teams Launch took 43.8 s on VD-14 (Minor), 3 min after a start-up. "
                          "On virtual desktops 93% are slow then, 62% later")
    assert ("Also slow: Microsoft Outlook Launch 40.6 s on VD-41, others typical 33.5 s; Microsoft Outlook Preview "
            "Mail 3.6 s on VD-41 and 5.2 s on VD-14: slow 2 of 22 times from Nov 6, usually 0.2 s (before: 0 of 6 "
            "slow).") in a["reason"]
    # no step to check changes that are not a suspect
    assert not any("check what changed" in t for t in a["todo"])
    assert a["pcs_label"] == "VD-14 and VD-41" and "these PCs" not in str(a)
    preview = next(n for n in a["normal"] if n[0].startswith("Microsoft Outlook Preview Mail"))
    assert "most runs were not slow (0 of 6 slow)." in " ".join(preview)
    assert "Since Nov 6: slow 2 of 22 times: occasional, not a slowdown." in preview
    changed = next(x for x in preview if x.startswith("Changed on VD-41"))
    assert "27 programs logged as uninstalled in the same minute, with no install logged (not changes to check)" \
        in changed and "Visual C++" not in changed
    # the AAD hang: not counted for Outlook, but said to be in the same minute
    aad = next(f for f in a["elsewhere"] if f["text"].startswith("AAD token broker plugin"))
    assert any(w.startswith("One was in the same minute as the slow Microsoft Outlook Launch on VD-41.")
               for w in aad["why"])
    # each desktop's start-up; the CPU readings with other actions are below the answer
    assert [st["device"] for st in a["felt"]["logons"]] == ["VD-14", "VD-41"] or \
        sorted(st["device"] for st in a["felt"]["logons"]) == ["VD-14", "VD-41"]
    assert a["found"] == []
    assert {f["text"] for f in a["elsewhere"] if "CPU" in f["text"]} == {
        "VD-14: CPU reached 88.98% (limit 85.0)", "VD-41: CPU reached 91.75% (limit 85.0)"}


class EmptyNetim:
    """A NetIM that knows none of the user's addresses."""
    warnings: list = []

    def devices(self):
        return []

    def interfaces(self, device_id):
        return []

    def metrics(self, metric, object_ids, start, end):
        return []

    def alerts(self, start, end):
        return []


class WithAddresses(OutlookPreviewNew):
    def endpoint_info(self, device):
        return EndpointInfo(device=device, ip_address={"VD-41": "10.9.0.41", "VD-14": "10.9.0.14"}[device])


def test_the_network_line_names_every_pc():
    settings = Settings()
    settings.thresholds.slow_status = ["Minor", "Major"]
    c = WithAddresses
    a = investigate(InvestigationRequest(start=c.START, end=c.END, username="user2"), c(), EmptyNetim(),
                    settings)["answer"]
    assert ("Network: not checked, the addresses of VD-41 (10.9.0.41) and VD-14 (10.9.0.14) are not in any "
            "NetIM subnet.") in a["checked"]
    aad = next(f for f in a["elsewhere"] if f["text"].startswith("AAD token broker plugin"))
    assert aad["count"] == "1x, not counted for a slow action"


class FailingDisk:
    """Nov 10 (live), names made up, New York time: a physical PC starts up at 00:06 (User
    Logon 180.7 s, usually 43.9 s); at 00:25 Aternity records the disk's S.M.A.R.T. fault
    and Microsoft.Notes hangs and crashes; at 00:48 Outlook Launch takes 89.2 s (Major),
    all of it on the PC by Aternity's split, usually 19.6 s; HD Failure at 01:08; disk queue
    32.57 at 01:35 with a normal action. 2 of 14 other Outlook users are slow then (Minor),
    at most 2 of 16 on other days. The change log has Edge and WebView2 updates, Adobe
    and Java. The page made "2 of 14 other users" the headline, sent IT to Outlook's servers,
    and named every Edge update a possible cause of the Notes crash."""
    START = datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc)  # 00:00 in New York
    END = START + timedelta(hours=2, minutes=10)
    BOOT = START + timedelta(minutes=6)
    SMART = START + timedelta(minutes=25)
    LAUNCH = START + timedelta(minutes=48)
    HD = START + timedelta(hours=1, minutes=8)
    DISK = "XY250-MODEL (Disk)"
    BEFORE: list = []  # disk records in the 14 days before the window

    def _actions(self):
        mk = lambda t, what, rt, cpu, dq, status=None: UserSample(  # noqa: E731
            t, "user7", "PC-77", OUTLOOK, activity=what, response_time=rt, client_time=rt, network_time=0.0,
            backend_time=0.0, status=status, cpu=cpu, memory=24.2, disk_queue=dq, virtual=False)
        return [mk(self.LAUNCH, "Launch", 89.2, 10.5, 1.1, "Major"),
                mk(self.START + timedelta(hours=1, minutes=20), "Open Mail", 1.0, 20.0, 0.5),
                mk(self.START + timedelta(hours=1, minutes=35), "Send Mail", 2.0, 20.0, 32.57)]

    def user_activity(self, *a, **k):
        return self._actions()

    def device_health(self, *a, **k):
        return [DeviceHealthSample(s.timestamp, s.device, cpu=s.cpu, memory=s.memory, disk_queue=s.disk_queue)
                for s in self._actions()]

    def endpoint_info(self, device):
        return EndpointInfo(device=device, ip_address="10.9.0.77", location="HQ")

    def _events(self):
        notes = dict(application="Microsoft.Notes", username="user7", name="Application Crash (After Hang)",
                     component="Microsoft.Notes.exe (Process)", component_version="6.0.2.0", count=1)
        return [HealthEvent(self.SMART, "PC-77", name="Faulty HD S.M.A.R.T. Status", component=self.DISK,
                            details="Index: 0"),
                HealthEvent(self.SMART, "PC-77", **notes),
                HealthEvent(self.HD, "PC-77", name="HD Failure", component="\\Device\\Harddisk0\\DR0 (Drive)")] + self.BEFORE

    def health_events(self, device, start, end):
        return [e for e in self._events() if e.device == device and start <= e.timestamp <= end]

    def named_health_events(self, name, start, end):
        return [e for e in self._events() if e.name == name and start <= e.timestamp <= end]

    def app_health_events(self, application, start, end):
        return [e for e in self._events() if e.application == application and start <= e.timestamp <= end]

    def known_pcs(self):
        return 5456

    def app_usage(self, device, start, end, application=None):
        return [AppUsageSample(self.SMART, "PC-77", "Microsoft.Notes", crashes=1)]

    def app_usage_everywhere(self, application, start, end):
        return [AppUsageSample(self.SMART, f"PC-{i}", application) for i in range(30)]

    def boots(self, start, end, username=None, **k):
        types = (("Machine Boot", 382.5, 335.6), ("User Logon", 180.7, 43.9), ("Total Boot Duration", 579.1, 482.7))
        rows = [BootSample(self.BOOT, "PC-77", "user7", type=t, duration=now) for t, now, _ in types]
        rows += [BootSample(self.BOOT - timedelta(days=d), "PC-77", "user7", type=t, duration=usual)
                 for d in range(1, 6) for t, _, usual in types]
        return [b for b in rows if start <= b.timestamp <= end]

    def boot_components(self, *a, **k):
        return []

    def activity_history(self, application, activity, start, end, username=None, device=None):
        rows = [UserSample(datetime(2026, 11, d, 14, 0, tzinfo=timezone.utc), "user7", "PC-77", OUTLOOK, "Launch",
                           response_time=rt, status="Major" if rt > 40 else None, virtual=False)
                for d, rt in ((4, 19.6), (5, 18.0), (6, 45.0), (8, 21.0), (9, 19.0))]
        rows.append(UserSample(self.LAUNCH - timedelta(minutes=1), "o1", "PC-1", OUTLOOK, "Launch",
                               response_time=43.9, virtual=False))
        rows = [r for r in rows if r.activity == activity and (username is None or r.username == username)]
        return [r for r in rows if start <= r.timestamp <= end]

    def app_activity(self, applications, start, end):
        today = start.date() == self.LAUNCH.date()
        users, slow = (14, 2) if today else (16, 2)
        return [UserSample(start + timedelta(minutes=1), f"u{i}", f"PC-{100 + i}", OUTLOOK, activity="Launch",
                           response_time=40.0 if i < slow else 3.0, status="Minor" if i < slow else None,
                           virtual=False) for i in range(users)]

    def software_changes(self, device, start, end):
        t = lambda d, h, m: datetime(2026, 10 if d > 20 else 11, d, h, m, tzinfo=timezone.utc)  # noqa: E731
        edge, wv = "Microsoft Edge", "Microsoft Edge WebView2 Runtime"
        rows = []
        for when, old, new in ((t(3, 3, 35), "156.0.4314.56", "156.0.4314.58"),
                               (t(2, 12, 48), "156.0.4314.50", "156.0.4314.56"),
                               (t(30, 2, 2), "156.0.4314.42", "156.0.4314.50"),
                               (t(29, 15, 59), "155.0.4283.62", "156.0.4314.42")):
            rows += [SoftwareChange(when, device, edge, "Uninstalled", old),
                     SoftwareChange(when + timedelta(minutes=1), device, edge, "Installed", new),
                     SoftwareChange(when + timedelta(minutes=2), device, wv, "Updated", new, old)]
        rows += [SoftwareChange(t(30, 1, 52), device, "Adobe Acrobat (64-bit)", "Updated", "26.002.22031",
                                "26.002.21901"),
                 SoftwareChange(t(29, 16, 50), device, "Java 8 Update 503", "Uninstalled", "8.0.5030.1"),
                 SoftwareChange(t(29, 16, 52), device, "Java 8 Update 511", "Installed", "8.0.5110.32")]
        return [c for c in rows if start <= c.timestamp <= end]


def failing_disk(case=FailingDisk):
    settings = Settings()
    settings.thresholds.slow_status = ["Minor", "Major"]
    settings.timezone = "America/New_York"
    return investigate(InvestigationRequest(start=case.START, end=case.END, username="user7"), case(), None,
                       settings)


def test_a_failing_disk_leads_and_no_microsoft_update_is_blamed_for_notes():
    result = failing_disk()
    a = result["answer"]
    # the disk is the PC's state from its first record on: it counts for the launch 23 min later
    assert a["title"] == ("Microsoft Outlook Launch took 89.2 s on PC-77 (Major). Likely: PC-77's disk is failing "
                          "(Aternity recorded Faulty HD S.M.A.R.T. Status and HD Failure)")
    assert a["level"] == "likely"
    assert a["reason"].startswith("A failing disk slows everything on its PC. Aternity recorded it failing from "
                                  "Nov 10, 12:25 AM and put 100% of the slow time on the PC. See What went wrong.")
    disk = a["found"][0]
    assert disk["count"] == "2 records, counted for the slow action" and disk["what"] == "Disk failing"
    assert disk["why"][:2] == [
        "Aternity recorded the disk failing on PC-77: Faulty HD S.M.A.R.T. Status in XY250-MODEL (Disk) at Nov 10, "
        "12:25 AM; HD Failure in \\Device\\Harddisk0\\DR0 (Drive) at Nov 10, 01:08 AM.",
        "Nothing about this disk in the 14 days before this window."]
    assert ("A failing disk slows everything on its PC. Aternity put 100% of the time of the slow action from its "
            "first record on the PC.") in disk["why"]
    assert any(w.startswith("Also on PC-77 from its first record: Application Crash (After Hang) (Microsoft.Notes.exe)"
                            " on PC-77; PC-77's disk could not keep up (queue 32.57, limit 2.0)") for w in disk["why"])
    assert ("Start-up at Nov 10, 12:06 AM, before its first record, longer than usual: Machine Boot 382.5 s (usual "
            "335.6 s), User Logon 180.7 s (usual 43.9 s), Total Boot Duration 579.1 s (usual 482.7 s).") in disk["why"]
    assert disk["who"] == "Only this PC in this window, of 5,456 PCs in Aternity's inventory."
    assert a["todo"][0] == "On PC-77: back up the user's data and replace the disk (XY250-MODEL): Aternity recorded it failing."
    # 2 of 14 other users slow is said, but Aternity put this user's time on their PC: no servers step
    assert "so this is not taken as the cause" in a["who_else"]
    assert not any("service and its servers" in t or "Many" in t for t in a["todo"])
    # "Microsoft.Notes" is not "Microsoft": no Edge or WebView2 update is a possible cause
    notes = next(x for x in a["elsewhere"] if "Microsoft.Notes" in x["text"])
    assert notes["possible"] == [] and not any("Edge" in t for t in a["todo"])
    assert "Within minutes of it on this PC: its disk failing (Faulty HD S.M.A.R.T. Status, HD Failure)." in notes["why"]
    # a disk is never "the process that failed"; the disk's records are one issue, not three
    assert not any("the process that failed" in t for t in a["todo"])
    assert [x["issue"] for x in a["rollup"]] == ["Disk failing"]
    assert not any("S.M.A.R.T" in x["text"] for x in a["elsewhere"])
    # days in sentences are New York days: the 22:37 change of Nov 2 is not "Nov 3"
    assert any("Other users within 5 min: typical 43.9 s (1 user)." == t for t in a["normal"][0])


class FailingDiskBefore(FailingDisk):
    """The same PC, with the disk already recorded failing on Nov 3."""
    BEFORE = [HealthEvent(datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc), "PC-77", name="Faulty HD S.M.A.R.T. Status",
                          component=FailingDisk.DISK)]


def test_a_disk_failing_before_the_window_covers_all_of_it():
    disk = failing_disk(FailingDiskBefore)["answer"]["found"][0]
    assert ("It was failing before this window too: Aternity recorded it on 1 day of the 14 days before, first at "
            "Nov 3, 09:00 AM.") in disk["why"]
    assert any(w.startswith("Start-up at Nov 10, 12:06 AM, after its first record") for w in disk["why"])


class FailingDiskServerSide(FailingDisk):
    """The same, but Aternity put the launch's time on the server: the disk is not counted for it."""
    def _actions(self):
        rows = super()._actions()
        rows[0].client_time, rows[0].backend_time = 9.2, 80.0
        return rows


def test_a_failing_disk_is_not_counted_where_aternity_puts_the_time_elsewhere():
    a = failing_disk(FailingDiskServerSide)["answer"]
    assert "disk is failing" not in a["title"] and a["found"] == []
    assert "Also: PC-77's disk is failing, not counted for the slow action (see Other things on PC-77)." in a["reason"]
    disk = a["elsewhere"][0]
    assert disk["count"] == "2 records, not counted for a slow action"
    assert ("Not counted for 1 slow action: Aternity put most of its time on the network or the server."
            in disk["why"])
    assert a["todo"][0].startswith("On PC-77: back up the user's data")


class NoDiskRecords(FailingDisk):
    """The same page without the disk's records: 2 of 14 other users slow is still not the
    headline, as Aternity put all of this user's time on their PC."""
    def _events(self):
        return [e for e in super()._events() if e.application]


def test_other_users_slow_are_not_the_headline_when_the_time_was_on_the_pc():
    a = failing_disk(NoDiskRecords)["answer"]
    assert a["title"] == ("Microsoft Outlook Launch took 89.2 s on PC-77 (Major), slow 1 of 1 times from Nov 10, "
                          "usually 19.6 s: occasional. No cause found")
    assert "Aternity measured most of the slow time on the PC (100%)." in a["reason"]
    assert not any("servers" in t for t in a["todo"])


class FailingDiskNoSlow(FailingDisk):
    """The same PC with no slow action: the disk still leads the title."""
    def _actions(self):
        rows = super()._actions()
        rows[0].status, rows[0].response_time, rows[0].client_time = None, 9.0, 9.0
        return rows


def test_a_failing_disk_leads_the_title_with_no_slow_action():
    a = failing_disk(FailingDiskNoSlow)["answer"]
    assert a["title"] == "PC-77's disk is failing and 1 hang, no slow actions" and a["level"] == "issues"
    assert a["found"][0]["count"] == "2 records" and a["todo"][0].startswith("On PC-77: back up the user's data")


def test_names_match_by_whole_words_and_days_are_new_york_days():
    from uxcorrelate.cause import change_text, name_words, same_name
    from uxcorrelate.models import set_zone
    notes = name_words("Microsoft.Notes")
    assert notes == ("microsoft", "notes") and name_words("Microsoft.Notes.exe (Process)") == notes
    assert not same_name(notes, "Microsoft Edge WebView2 Runtime") and same_name(notes, "Microsoft Sticky Notes")
    assert same_name(name_words("OUTLOOK.EXE"), "Microsoft Outlook") and not same_name(name_words("msedge.exe"),
                                                                                       "Microsoft Edge")
    change = {"time": datetime(2026, 11, 3, 3, 37, tzinfo=timezone.utc), "name": "Microsoft Edge", "change": "Updated",
              "version_before": "1", "version": "2"}
    try:
        set_zone("America/New_York")
        assert change_text(change) == "Nov 2 Microsoft Edge Updated 1 → 2"  # 10:37 PM in New York
    finally:
        set_zone("")
    assert change_text(change) == "Nov 3 Microsoft Edge Updated 1 → 2"


def test_every_real_case_tells_each_slow_action_at_the_top():
    """Every case seen on prod: the headline says what was slow and how slow, every slow app
    and activity is named at the top (headline or reason), and no PC is called "these PCs"."""
    settings = Settings()
    settings.thresholds.slow_status = ["Minor", "Major"]
    for c, user in ((OutlookAfterLogon, "user1"), (LaunchAsUsual, "user1"), (TeamsAfterLogonBusyPC, "user2"),
                    (TeamsAndOutlookBusyPC, "user2"), (SameMinuteReading, "user2"), (OutlookPreviewNew, "user2"),
                    (FailingDisk, "user7")):
        result = investigate(InvestigationRequest(start=c.START, end=c.END, username=user), c(), None, settings)
        a, top = result["answer"], result["answer"]["title"] + " " + result["answer"]["reason"]
        assert re.search(r"\d+\.\d s", a["title"]), (c.__name__, a["title"])
        # each slow app + activity (Compared with normal names them: "Microsoft Teams Launch took …")
        for name in [n[0].split(" took ")[0] for n in a["normal"]]:
            assert name in top, (c.__name__, name)
        assert "these PCs" not in str(a), c.__name__
