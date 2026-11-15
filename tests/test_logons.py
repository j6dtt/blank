from datetime import datetime, timedelta, timezone

from synthetic import DemoAternity

from uxcorrelate.logons import group_starts, logon_report
from uxcorrelate.models import BootSample

START = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=3)


def test_everyone_slowest_start_ups_point_at_the_site_and_the_cause():
    lg = logon_report(DemoAternity(START), START, END, unit="s")
    assert lg["total"] == 51 and lg["types"][0]["type"] == "Logon" and lg["sort_type"] == "Logon"
    first = lg["starts"][0]
    assert first["location"] == "Branch-Denver" and first["values"]["Logon"]["duration"] >= 90
    assert first["components"][0]["component"] == "Group Policy"  # most degradation first
    assert first["values"]["Logon"]["usual"] is None  # no per-device history when searching everyone
    assert lg["by_location"][0]["location"] == "Branch-Denver" and lg["by_location"][0]["count"] == 21


def test_one_user_is_compared_with_their_own_last_30_days():
    lg = logon_report(DemoAternity(START), START, END, username="user03")
    v = lg["starts"][0]["values"]["Logon"]
    assert lg["baseline_days"] == 30 and v["usual"] == 33.0 and v["duration"] == 93.0


def test_measurements_of_one_start_up_become_one_row():
    rows = [BootSample(START, "PC1", type="Boot", duration=40.0),
            BootSample(START + timedelta(minutes=2), "PC1", type="Logon", duration=30.0),
            BootSample(START, "PC1", type="Total", duration=70.0),
            BootSample(START + timedelta(minutes=30), "PC1", type="Logon", duration=20.0),  # logon again: new row
            BootSample(START, "PC2", type="Logon", duration=25.0)]
    assert sorted(len(s) for s in group_starts(rows)) == [1, 1, 3]


def test_no_boot_data_is_an_empty_report():
    class NoBoots:
        pass
    assert logon_report(NoBoots(), START, END)["total"] == 0


def test_slow_start_ups_get_a_cause_and_who_else_is_affected():
    lg = logon_report(DemoAternity(START), START, END, unit="s")
    cause = lg["causes"][0]
    assert (cause["component"], cause["type"], cause["pcs"]) == ("Group Policy", "Logon Phase", 21)
    assert cause["by_location"] == {"Branch-Denver": 21} and cause["list"][0]["worst"] == 45.0
    first = lg["starts"][0]
    assert first["cause"] == 0
    assert first["why"][0] == "Most of the extra time: Group Policy took 55.0 s, 45.0 s longer than normal (Logon Phase)."
    assert first["why"][-1].startswith("Same cause as main cause 1 above (Group Policy)")


def test_parts_go_to_the_start_up_before_them_whatever_their_type():
    from uxcorrelate.models import BootComponent
    boot1, boot2, logon = START + timedelta(minutes=10), START + timedelta(minutes=80), START + timedelta(minutes=170)

    class Shaped:  # parts keep their own type names; services come a few minutes after the boot
        def boots(self, start, end, **k):
            return [BootSample(boot1, "WS-9", "kim", "Site-2", type="Full Start", duration=400.0),
                    BootSample(boot2, "WS-9", "kim", "Site-2", type="Machine Start", duration=200.0),
                    BootSample(logon, "WS-9", "kim", "Site-2", type="Sign-in", duration=80.0)]

        def boot_components(self, devices, start, end):
            part = lambda t, typ, comp, d: BootComponent(t, "WS-9", typ, comp, duration=d)  # noqa: E731
            return [part(boot1, "Phase: Loader", None, 30.0), part(boot1, "Phase: After Start", None, 310.0),
                    part(boot2, "Driver Call", "drv7", 12.0),
                    part(boot2 + timedelta(minutes=6), "Service Call", "svcA", 40.0)]

        def boot_component_everywhere(self, *a, **k):
            return []

    lg = logon_report(Shaped(), START, END, unit="s")
    by_type = {next(iter(st["values"])): st for st in lg["starts"]}
    assert [c["component"] for c in by_type["Full Start"]["components"]] == ["Phase: After Start", "Phase: Loader"]
    assert [c["component"] for c in by_type["Machine Start"]["components"]] == ["svcA", "drv7"]
    assert by_type["Sign-in"]["components"] == []
    assert by_type["Sign-in"]["why"][0] == "Aternity recorded no breakdown for this start-up."
    assert by_type["Full Start"]["why"][0] == ("Aternity measured no part as longer than normal; the longest was "
                                               "Phase: After Start (310.0 s).")


def test_a_start_up_says_since_when_it_is_longer_and_what_changed_just_before():
    from uxcorrelate.models import SoftwareChange
    day = lambda d, m=0: START - timedelta(days=d) + timedelta(minutes=m)  # noqa: E731

    class Lab:  # LAB-7: Sign-in about 20 s, about 70 s since 6 days ago; a VPN client came the day before
        def boots(self, start, end, **k):
            if start < START - timedelta(days=1):
                return [BootSample(day(d), "LAB-7", "pat", "Lab", type="Sign-in", duration=20.0 + d % 3)
                        for d in range(30, 10, -2)] + \
                    [BootSample(day(d), "LAB-7", "pat", "Lab", type="Sign-in", duration=70.0 + d) for d in (6, 4, 2, 1)]
            return [BootSample(START + timedelta(minutes=30), "LAB-7", "pat", "Lab", type="Sign-in", duration=75.0)]

        def software_changes(self, device, start, end):
            c = SoftwareChange(day(7, 600), device, "VPN Client", "Installed", "9.0")
            return [c] if start <= c.timestamp <= end else []

    lg = logon_report(Lab(), START, END, device="LAB-7", unit="s")
    st = lg["starts"][0]
    assert st["unusual"] is True and st["changes_since"] == ["VPN Client"]
    assert st["why"][0] == ("Since Oct 22, every Sign-in on LAB-7 took longer than 9 in 10 of those before: "
                            "typical 21.0 s before, 74.0 s since (5 since, 10 before).")
    assert st["why"][1] == "Changed on LAB-7 between Oct 16 and Oct 22: Oct 21 VPN Client Installed 9.0."
    assert st["why"][2] == "Aternity recorded no breakdown for this start-up."
    assert len(st["why"]) == 3  # the 14-day change list is not repeated


def test_on_pooled_virtual_desktops_a_start_up_is_compared_with_the_users_own():
    # live: one user on 11 virtual desktops in 14 days, one desktop used by 4 users
    day = lambda d: START - timedelta(days=d)  # noqa: E731

    class Pool:
        def boots(self, start, end, username=None, **k):
            if start < START - timedelta(days=1):
                return [BootSample(day(d), f"VD-{d}", "ann", type="Sign-in", duration=20.0 + d % 3)
                        for d in range(12, 0, -1)]
            return [BootSample(START + timedelta(minutes=30), "VD-40", "ann", type="Sign-in", duration=75.0)]

        def virtual_devices(self, start, end, username=None, device=None):
            assert username == "ann"
            return {f"VD-{d}" for d in range(1, 13)} | {"VD-40"}

    lg = logon_report(Pool(), START, END, username="ann", unit="s")
    st = lg["starts"][0]
    assert st["pooled"] is True and st["values"]["Sign-in"]["usual"] == 21.0  # all her desktops
    assert st["why"][0] == ("Sign-in took 75.0 s, longer than 9 in 10 of ann's 12 earlier start-ups on virtual "
                            "desktops in the last 30 days (up to 22.0 s); the one before, Oct 27, took 21.0 s.")
    assert st["why"][1] == ("The start-up before that was on VD-1, another virtual desktop: what changed is "
                            "read on VD-40 only.")

    # the same user on a physical PC: that PC's own history, as before
    class Physical(Pool):
        def virtual_devices(self, *a, **k):
            return set()

    st = logon_report(Physical(), START, END, username="ann", unit="s")["starts"][0]
    assert st["pooled"] is False
    assert st["why"][0].startswith("VD-40 has only 0 earlier Sign-in")


def test_reads_that_fail_are_said_not_read_as_nothing_there():
    """A read Aternity fails (a warning, as its client says) is never "too few earlier start-ups",
    "no breakdown recorded" or "nothing was installed"."""
    from uxcorrelate.models import BootSample as B

    class Down:
        def __init__(self, fail):
            self.fail, self.warnings = fail, []

        def _no(self, what):
            self.warnings.append(f"{what} not checked: Aternity gave an error (504).")
            return []

        def boots(self, start, end, **k):
            if start < START - timedelta(days=1):  # the last 30 days
                return self._no("Boot/logon data") if "history" in self.fail else \
                    [B(START - timedelta(days=d), "LAB-7", "pat", type="Sign-in", duration=20.0) for d in range(1, 12)]
            return [B(START + timedelta(minutes=30), "LAB-7", "pat", type="Sign-in", duration=75.0)]

        def boot_components(self, *a, **k):
            return self._no("Boot/logon components")

        def software_changes(self, *a, **k):
            return self._no("Software changes")

    why = logon_report(Down({"history"}), START, END, device="LAB-7", unit="s")["starts"][0]["why"]
    assert why == ["Aternity's read of its earlier start-ups failed or was cut short: not compared with what is "
                   "normal.", "Aternity's read of what took the time failed or was cut short.",
                   "The change log of LAB-7 could not be read in full: what changed in the 14 days before Oct 28 is "
                   "not known."]
    st = logon_report(Down(set()), START, END, device="LAB-7", unit="s")["starts"][0]
    assert st["unusual"] is True and "could not be read in full" in st["why"][1]  # between the normal and the slow
    assert "parts_unread" not in st


def test_1_in_10_over_is_only_given_from_10_start_ups():
    from uxcorrelate.logons import _stats
    from uxcorrelate.report import render_logons
    two = _stats([10.0, 20.0])  # it said "1 in 10 over 27.0 s": past the slowest one
    assert two["p90"] is None and two["max"] == 20.0
    assert _stats([float(v) for v in range(1, 11)])["p90"] is not None
    lg = logon_report(DemoAternity(START), START, END, unit="s")
    lg["types"] = [{"type": "Logon", "count": 2, **two}]
    assert "Logon: typical 15.0 s, slowest 20.0 s (2 measured)" in render_logons(lg)


def test_a_logons_report_with_nothing_read_says_the_read_failed():
    """The CLI's logons report said "No boots or logons in the window." of a read Aternity refused."""
    from uxcorrelate.report import render_logons
    lg = {"filters": {"device": "LAB-7"}, "unit": "s", "start": "2026-10-28T09:00:00Z", "end": "2026-10-28T12:00:00Z",
          "total": 0, "warnings": ["Boot/logon data not checked: Aternity BOOTS gave an error (HTTP 500)."]}
    text = render_logons(lg)
    assert "No boots or logons could be read in this window:" in text and "HTTP\n500" in text  # wrapped at 72
    assert "No boots or logons in the window." in render_logons(dict(lg, warnings=[]))
