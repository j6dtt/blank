from datetime import datetime, timedelta, timezone

from synthetic import DemoAternity

from uxcorrelate.logons import logon_report

START = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=3)


def test_everyone_slowest_logons_point_at_the_site_and_the_cause():
    lg = logon_report(DemoAternity(START), START, END)
    assert lg["total"] == 51 and lg["types"][0]["type"] == "Logon"
    first = lg["slowest"][0]
    assert first["location"] == "Branch-Denver" and first["duration"] >= 90 and first["x_median"] > 2
    assert first["components"][0]["component"] == "Group Policy"  # most degradation first
    assert first["usual"] is None  # no per-device history when searching everyone
    top = [g for g in lg["by_location"] if g["type"] == "Logon"][0]
    assert top["location"] == "Branch-Denver" and top["count"] == 21  # 20 users + jdoe


def test_one_user_is_compared_with_their_own_last_30_days():
    lg = logon_report(DemoAternity(START), START, END, username="user03")
    b = lg["slowest"][0]
    assert lg["baseline_days"] == 30 and b["usual"] == 33.0 and b["x_usual"] == 2.8


def test_no_boot_data_is_an_empty_report():
    class NoBoots:
        pass
    assert logon_report(NoBoots(), START, END)["total"] == 0
