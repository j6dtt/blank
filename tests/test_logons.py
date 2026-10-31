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
