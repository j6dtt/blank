"""The Inventory tab (inventory.py): Aternity's device list read every hour, held in memory; the
rules of "Needs a look", the lists by a click, Find a PC. The user's "build it", Nov 21 2026."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from uxcorrelate import inventory as inv
from uxcorrelate.aternity import AternityClient
from uxcorrelate.config import INVENTORY_COLUMNS, INVENTORY_FILLED, INVENTORY_ROLES, Settings
from uxcorrelate.verify import parse_odata_metadata

NOW = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
META = (Path(__file__).parent / "fixtures" / "aternity_metadata_v2.xml").read_text()


def at(days: float) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pc(name, **over) -> dict:
    """A physical PC with nothing to look at; `over` changes it."""
    row = {"DEVICE_NAME": name, "TIME_LAST_REPORTING": at(0.01), "AGENT_CURRENT_STATUS": "Reporting",
           "DEVICE_TYPE": "Laptop", "DEVICE_MANUFACTURER": "Maker", "DEVICE_MODEL": "Model A",
           "DEVICE_SERIAL_NUMBER": f"SN-{name}", "DEVICE_MEMORY": "16GB", "OS_DISK_TYPE": "SSD",
           "OS_TOTAL_DISK_SPACE": "472GB", "OS_FREE_DISK_SPACE": "200 GB to 500 GB", "BOOT_LAST_TIMESTAMP": at(1),
           "OS_BITLOCKER_PROTECTION_STATUS": "On", "AGENT_VERSION": "12.2.4.113", "MS_STABILITY_INDEX": "8.2",
           "BUSINESS_LOCATION": "Site", "USERNAME": f"user-{name.lower()}", "USER_DEPARTMENT": "Dept"}
    return {**row, **over}


def reader(rows, used=None, fail=None):
    """An Inventory after one read of made-up rows; `used`: the PCs with app usage (None: every PC)."""
    asked = []

    def handler(request):
        path, p = request.url.path, request.url.params
        if path.endswith("/$metadata"):
            return httpx.Response(200, text=META)
        asked.append((path.rsplit("/", 1)[-1], p.get("$select", ""), p.get("$filter", "")))
        if fail and path.endswith("/" + fail):
            return httpx.Response(400, text="no")  # an answer the client does not try again
        if path.endswith("/APPLICATIONS_DAILY"):
            names = [r["DEVICE_NAME"] for r in rows] if used is None else used
            return httpx.Response(200, json={"value": [{"DEVICE_NAME": n} for n in names]})
        cols = p["$select"].split(",")
        return httpx.Response(200, json={"value": [{k: r.get(k, "N/A") for k in cols} for r in rows]})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    tab = inv.Inventory("", clock=lambda: NOW)
    tab.read(client, settings.aternity, NOW)
    return tab, asked


def test_the_columns_and_words_are_riverbeds_own():
    schema = parse_odata_metadata(META)["DEVICE_INVENTORY"]
    assert set(INVENTORY_ROLES.values()) <= schema and set(INVENTORY_COLUMNS.values()) <= schema
    filled = {c for _, items in INVENTORY_FILLED for _, c, _ in items}
    assert set(INVENTORY_ROLES.values()) <= filled  # every rule's column is among those the read asks for
    assert set(inv.LINES) == {"agent", "bitlocker", "unused", "restart", "silent", "stopped", "stability", "free",
                              "spinning"}


def test_a_band_of_free_space_is_little_only_when_it_ends_at_ten_gb_or_under():
    """Aternity gives free space as bands of words (stage, Nov 21 2026: '10 GB to 20 GB', 'Over
    500 GB'); the low ones were not seen by name, so the band's own upper end decides."""
    for text, top in (("10 GB to 20 GB", 20), ("5 GB to 10 GB", 10), ("Less than 5 GB", 5), ("500 MB to 1 GB", 1),
                      ("Under 500 MB", 500 / 1024), ("Over 500 GB", None), ("More than 1 TB", None), ("20GB+", None),
                      ("Unknown", None), (None, None), ("1 TB to 2 TB", 2048)):
        assert inv.band_top_gb(text) == top, text


def test_one_read_gives_the_tiles_the_lines_and_who_is_behind_each():
    rows = [pc(f"PC-{i:02d}") for i in range(1, 11)]
    rows += [pc("PC-11", AGENT_VERSION="12.2.4.79"), pc("PC-12", AGENT_VERSION="12.2.4.115"),  # older / newer than most
             pc("PC-13", OS_BITLOCKER_PROTECTION_STATUS="Off"), pc("PC-14", BOOT_LAST_TIMESTAMP=at(20)),
             pc("PC-15", BOOT_LAST_TIMESTAMP="N/A"), pc("PC-16", TIME_LAST_REPORTING=at(9)),
             pc("PC-17", TIME_LAST_REPORTING="N/A"), pc("PC-18", AGENT_CURRENT_STATUS="Stopped"),
             pc("PC-19", MS_STABILITY_INDEX="3.5"), pc("PC-20", OS_FREE_DISK_SPACE="5 GB to 10 GB"),
             pc("PC-21", OS_DISK_TYPE="HDD-7200"), pc("PC-22"),
             pc("VD-01", DEVICE_TYPE="Virtual Desktop", OS_BITLOCKER_PROTECTION_STATUS="Off", OS_DISK_TYPE="Virtual",
                OS_FREE_DISK_SPACE="10 GB to 20 GB"),
             pc("PC-16", TIME_LAST_REPORTING=at(30), DEVICE_MODEL="its older row")]  # a PC listed twice: its latest
    used = [r["DEVICE_NAME"].upper() for r in rows if r["DEVICE_NAME"] not in ("PC-22", "PC-16")]  # names in any case
    tab, asked = reader(rows, used)
    # two reads: the device list with every published column of the tab, and app usage for each PC once
    assert [a[0] for a in asked] == ["DEVICE_INVENTORY", "APPLICATIONS_DAILY"]
    assert asked[0][1].startswith("DEVICE_NAME,TIME_LAST_REPORTING,AGENT_CURRENT_STATUS,DEVICE_TYPE,")
    assert "ANTIVIRUS_VENDOR" in asked[0][1]  # in Riverbed's list: asked for where an Aternity publishes it
    assert asked[1][1] == "DEVICE_NAME" and "TIMEFRAME ge 2026-11-13" in asked[1][2]
    v = tab.view(NOW, retired={"n": 4, "listed": 23, "names": 0, "as_of_local": "Nov 21, 13:33"})
    assert v["ready"] and v["pcs"] == 23 and v["virtual"] == 1 and v["every_min"] == 60
    assert v["kinds"] == [{"kind": "Laptop", "n": 22}, {"kind": "Virtual Desktop", "n": 1}]
    assert v["heard"] == {"known": True, "today": 21, "week": 0, "silent": 1, "old": 0, "none": 1,
                          "longest": {"days": 9, "day": "Nov 12"}}
    assert v["statuses"] == [{"status": "Reporting", "n": 22}, {"status": "Stopped", "n": 1}]
    assert v["retired"]["n"] == 4 and v["used"] == {"known": True, "of": 22, "used": 21, "unused": 1}
    lines = {x["key"]: (x["n"], x["of"]) for x in v["lines"]}
    assert lines == {"agent": (1, 23), "bitlocker": (1, 22), "unused": (1, 22), "restart": (1, 22), "silent": (1, 22),
                     "stopped": (1, 23), "stability": (1, 23), "free": (1, 23), "spinning": (1, 22)}
    assert v["quiet"] == [] and v["unknown"] == [] and v["notes"] == []
    text = {x["key"]: x["text"] for x in v["lines"]}
    assert text["agent"] == "An Aternity agent older than 12.2.4.113, the version most PCs have."
    assert text["free"] == "Aternity gives free space as bands; those that end at 10 GB or under: '5 GB to 10 GB'."
    assert text["restart"] == "The last start-up Aternity has for the PC. 1 PC has none and is not judged."
    assert text["bitlocker"] == "As the PC reports it to Aternity. 21 PCs in all report it on."
    # who is behind each line, with the line's own value
    who = lambda key: [(r["name"], r["what"]) for r in tab.line_pcs(key, NOW)["rows"]]  # noqa: E731
    assert who("agent") == [("PC-11", "12.2.4.79")] and who("bitlocker") == [("PC-13", "Off")]  # not the virtual desktop
    assert who("unused") == [("PC-22", "physical PC")]  # PC-16 is silent: on its own line, not here
    assert who("restart") == [("PC-14", "last start-up Nov 1, 20 days ago")]
    assert who("silent") == [("PC-16", "last heard Nov 12, 9 days ago")]
    assert who("stopped") == [("PC-18", "Stopped")] and who("stability") == [("PC-19", "3.5")]
    assert who("free") == [("PC-20", "5 GB to 10 GB")] and who("spinning") == [("PC-21", "HDD-7200")]
    one = tab.line_pcs("silent", NOW)
    assert one["what"] == "Not heard from in 8 days or more" and one["total"] == 1 and one["rows"][0]["username"] == "user-pc-16"
    assert tab.line_pcs("nothing", NOW) is None


def test_find_a_pc_by_name_person_or_serial_and_its_card():
    rows = [pc("LT-0412", DEVICE_MODEL="Precision 3460", USERNAME="jsmith"), pc("LT-0413", USERNAME="adoe"),
            pc("VD-2041", DEVICE_TYPE="Virtual Desktop", USERNAME="jsmith", DEVICE_SERIAL_NUMBER="VMware-42",
               OS_BITLOCKER_PROTECTION_STATUS="Off", AGENT_VERSION="12.2.4.79")] + [pc(f"PC-{i}") for i in range(30)]
    tab, _ = reader(rows)
    assert tab.find("l") == {"rows": [], "more": 0, "short": True}
    assert [r["name"] for r in tab.find("lt-04")["rows"]] == ["LT-0412", "LT-0413"]
    assert [r["name"] for r in tab.find("JSMITH")["rows"]] == ["LT-0412", "VD-2041"]  # the last person on it
    assert [r["name"] for r in tab.find("vmware-42")["rows"]] == ["VD-2041"]  # its serial number
    many = tab.find("pc-")
    assert len(many["rows"]) == 20 and many["more"] == 10  # 30 PCs have it: the first 20, and how many more
    card = tab.card("vd-2041", NOW)
    assert card["name"] == "VD-2041" and card["virtual"] and card["used"] is True and card["heard_days"] == 0
    assert card["lines"] == ["On an older agent than most"]  # BitLocker off is not counted on a virtual desktop
    fields = {f["label"]: f["value"] for f in card["fields"]}
    assert fields["Serial number"] == "VMware-42" and fields["BitLocker"] == "Off" and fields["Last user"] == "jsmith"
    assert "Antivirus" not in fields and card["status"] == "Reporting" and card["boot_days"] == 1
    assert fields["Last start-up"] == "Nov 20, 2026, 15:00"  # a time in the page's time zone, not as sent
    assert [f["group"] for f in card["fields"]][0] == "What the PC is"
    assert tab.card("nobody") is None


def test_what_cannot_be_read_is_said_not_counted_as_nothing():
    """A line whose column this Aternity lacks, or app usage that failed, is "not judged", never 0
    PCs; a failed read of the device list keeps the last one."""
    tab, _ = reader([pc("PC-1"), pc("PC-2")], fail="APPLICATIONS_DAILY")
    v = tab.view(NOW)
    assert v["used"] == {"known": False} and "App usage could not be read" in v["notes"][0]
    assert {"label": "Not used in 8 days", "why": "whether a PC is in use could not be read (see the notes)"} in v["unknown"]
    assert tab.card("PC-1", NOW)["used"] is None
    tab.have.discard("AGENT_VERSION")  # as an Aternity that does not publish it
    assert {"label": "On an older agent than most", "why": "this Aternity does not publish the agent's version"} in tab.view(NOW)["unknown"]
    assert tab.line_pcs("agent", NOW) is None
    # nothing read yet: the tab says what the reader is doing
    fresh = inv.Inventory("")
    assert fresh.view() == {"ready": False, "reading": False, "status": "Starting", "off": None, "zone": "UTC",
                            "every_min": 60, "retired": None}


def test_the_reader_reads_at_start_then_hourly_and_keeps_the_last_read_on_an_error():
    rows, calls, waits = [pc("PC-1")], {"n": 0}, []

    def handler(request):
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=META)
        if request.url.path.endswith("/DEVICE_INVENTORY"):
            calls["n"] += 1
            if calls["n"] == 2:
                return httpx.Response(400, text="down")
        cols = request.url.params["$select"].split(",")
        return httpx.Response(200, json={"value": [{k: r.get(k, "N/A") for k in cols} for r in rows]})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    tab = inv.Inventory("", clock=lambda: NOW)

    class Stop(Exception):
        pass

    def sleep(s):
        waits.append(s)
        if len(waits) == 3:
            raise Stop

    try:
        inv.keep_reading(tab, lambda: AternityClient(settings.aternity, transport=httpx.MockTransport(handler)),
                         lambda: settings.aternity, sleep, clock=lambda: NOW)
    except Stop:
        pass
    assert waits == [3600, 900, 3600]  # hourly; 15 minutes after the read that failed
    assert tab.view(NOW)["pcs"] == 1 and tab.status == "Read" and not tab.reading
    off = inv.Inventory("")
    inv.keep_reading(off, lambda: None, lambda: settings.aternity, sleep)
    assert off.off.startswith("Aternity is not set")


def test_the_tab_answers_before_its_first_read_and_from_its_read_after(monkeypatch):
    from fastapi.testclient import TestClient
    from uxcorrelate import web
    app = web.create_app()  # no Aternity set, reader not started
    api = TestClient(app)
    assert api.get("/api/inventory").json() == {"ready": False, "reading": False, "status": "Starting", "off": None,
                                                "zone": "UTC", "every_min": 60, "retired": None}
    assert api.get("/api/inventory/pcs", params={"line": "agent"}).status_code == 404
    assert api.get("/api/inventory/pc", params={"name": "PC-1"}).status_code == 404
    assert api.get("/api/inventory/find", params={"q": "pc"}).json() == {"rows": [], "more": 0, "short": False}
    assert api.get("/api/inventory/software", params={"pc": "PC-1"}).status_code == 503  # read at the click: not set
    assert api.get("/api/inventory/pcs", params={"line": "retired"}).json()["total"] == 0
    # after a read: the same answers as the reader's own (the page's lists cost Aternity nothing)
    tab, _ = reader([pc("PC-1", AGENT_CURRENT_STATUS="Stopped"), pc("PC-2")])
    app.state.inventory.pcs, app.state.inventory.used, app.state.inventory.have = tab.pcs, tab.used, tab.have
    app.state.inventory.as_of, app.state.inventory.status = tab.as_of, "Read"
    app.state.software.snap = {"as_of": "2026-11-21T18:33:00+00:00", "list": {"pcs": 2, "left_out": 3}}
    app.state.software.gone = {"OLD-2", "old-1"}
    v = api.get("/api/inventory").json()
    assert v["pcs"] == 2 and [x["key"] for x in v["lines"]] == ["stopped"]
    assert v["retired"] == {"n": 3, "listed": 2, "names": 2, "as_of_local": "Nov 21, 18:33"}
    assert [r["name"] for r in api.get("/api/inventory/pcs", params={"line": "stopped"}).json()["rows"]] == ["PC-1"]
    gone = api.get("/api/inventory/pcs", params={"line": "retired"}).json()
    assert gone["total"] == 3 and [r["name"] for r in gone["rows"]] == ["old-1", "OLD-2"]
    assert api.get("/api/inventory/pc", params={"name": "pc-2"}).json()["name"] == "PC-2"


def test_the_page_has_the_tab_its_link_and_its_explain():
    page = (Path(__file__).parents[1] / "src" / "uxcorrelate" / "static" / "index.html").read_text()
    assert '<button id="tabDv">Inventory</button></nav>' in page and 'dv: "inventory" }' in page
    assert '["dvBox", "inventory"]' in page and 'dv: "dvBox" }' in page  # Explain on its panels; Ask anything reads it
    from uxcorrelate import explain, usage
    assert explain.TABS["inventory"] == usage.TABS["inventory"] == "Inventory"
    for label in inv.LINES.values():  # every line of "Needs a look" has our written meaning
        assert label in explain.GLOSSARY["inventory"], label
    assert "alert(" not in page.split('id="dvBox"')[1].split('id="invForm"')[0]


# ---- Desktop usage: the PCs in use each finished day and month, against the list (the user, Nov 22 2026) ----
LISTED = [("VD-1", "Virtual Desktop"), ("VD-2", "Virtual Desktop"), ("VD-3", "Virtual Desktop"),
          ("VD-4", "Virtual Desktop"), ("DT-1", "Desktop"), ("DT-2", "Desktop"), ("LT-1", "Laptop")]


def usage_tab(store, days, now=NOW, listed=LISTED, fail=(), meta=META, stray=()):
    """An Inventory and its fake Aternity. `days`: local day -> the PCs with app usage on it, each
    row stamped at that day's midnight as the live Aternity does; `fail`: days whose own read is
    refused. Returns the tab, a function that makes one read (list, then usage) and what was asked."""
    import re
    from uxcorrelate.models import parse_time
    asked = []

    def handler(request):
        path, p = request.url.path, request.url.params
        if path.endswith("/$metadata"):
            return httpx.Response(200, text=meta)
        if path.endswith("/DEVICE_INVENTORY"):
            cols = p["$select"].split(",")
            return httpx.Response(200, json={"value": [{k: pc(n, DEVICE_TYPE=kind).get(k, "N/A") for k in cols}
                                                       for n, kind in listed]})
        assert path.endswith("/APPLICATIONS_DAILY")
        lo = parse_time(re.search(r"TIMEFRAME ge (\S+)", p["$filter"]).group(1))
        hi = re.search(r"TIMEFRAME lt (\S+)", p["$filter"])
        hi = parse_time(hi.group(1)) if hi else None
        rows = [{"TIMEFRAME": f"{d}T00:00:00Z", "DEVICE_NAME": n} for d, names in sorted(days.items()) for n in names
                if lo <= parse_time(f"{d}T00:00:00Z") and (hi is None or parse_time(f"{d}T00:00:00Z") < hi)]
        if p["$select"] == "TIMEFRAME":  # the oldest day kept
            assert p["$orderby"] == "TIMEFRAME asc" and p["$top"] == "1"
            asked.append("oldest")
            return httpx.Response(200, json={"value": [{"TIMEFRAME": r["TIMEFRAME"]} for r in rows[:1]]})
        if hi is None:
            assert p["$select"] == "DEVICE_NAME"
            asked.append("used")  # the tab's "not used in 8 days"
        else:
            first, last = (lo + timedelta(hours=12)).date(), (hi + timedelta(hours=12)).date() - timedelta(days=1)
            asked.append(first.isoformat() if first == last else f"{first.isoformat()[:7]} to {last.isoformat()}")
            # as verify --usage read them on stage: a day with each row's time, a month with the PC only
            assert p["$select"] == ("DEVICE_NAME,TIMEFRAME" if first == last else "DEVICE_NAME")
            if first == last and first.isoformat() in fail:
                return httpx.Response(400, text="no")
            if first == last:  # a row per PC and day; `stray`: a row of another day in the reply is not counted
                got = {(r["DEVICE_NAME"], r["TIMEFRAME"]) for r in rows} | {(n, f"{d}T00:00:00Z") for d, n in stray}
                return httpx.Response(200, json={"value": [{"DEVICE_NAME": n, "TIMEFRAME": t} for n, t in sorted(got)]})
        return httpx.Response(200, json={"value": [{"DEVICE_NAME": n} for n in sorted({r["DEVICE_NAME"] for r in rows})]})

    settings = Settings()
    settings.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    client = AternityClient(settings.aternity, transport=httpx.MockTransport(handler))
    tab = inv.Inventory("", str(store) if store else None, clock=lambda: now)

    def read(at=now):
        del asked[:]
        tab.read(client, settings.aternity, at)
        tab.read_usage(client, at)
        return tab.view(at)["usage"]
    return tab, read, asked


USED = {"2026-09-10": ["VD-1"],  # the oldest day Aternity keeps: September is not whole
        "2026-10-05": ["VD-1", "VD-2", "DT-1"], "2026-10-06": ["VD-1"],
        "2026-11-02": ["vd-1", "VD-2", "VD-3", "DT-1", "OLD-9"],  # other capitals; a PC that is not in the list
        "2026-11-03": ["VD-1", "VD-2"], "2026-11-04": ["VD-1"], "2026-11-14": ["VD-4"],
        "2026-11-19": ["VD-1", "VD-2", "LT-1"],
        "2026-11-21": ["VD-1"]}  # today (the live Aternity has no row for it): never counted, the day is not over


def test_usage_counts_each_finished_day_and_month_against_the_list_and_keeps_counts_only(tmp_path):
    """Design E (the user, Nov 22 2026). What verify --usage showed on stage, Nov 21: a day's rows
    carry local midnight, today has none, Aternity keeps about 92 days and adds a month up itself."""
    tab, read, asked = usage_tab(tmp_path, dict(USED))
    u = read()
    # the first read: how far back, every finished day Aternity keeps (yesterday first), then the whole months
    assert asked[:4] == ["used", "oldest", "2026-11-20", "2026-11-19"] and asked[-3] == "2026-09-10"
    assert asked[-2:] == ["2026-11 to 2026-11-20", "2026-10 to 2026-10-31"] and len(asked) == 2 + 72 + 2
    assert u["kinds"] == [{"key": "all", "n": 7}, {"key": "Virtual Desktop", "n": 4}, {"key": "Desktop", "n": 2},
                          {"key": "Laptop", "n": 1}]
    assert u["open"] == "Virtual Desktop" and u["oldest"] == "2026-09-10" and u["filled"] == "2026-11-21"
    assert not u["off"] and not u["state"] and u["notes"] == []
    days = {d["day"]: d for d in u["days"]}
    assert list(days) == [(date(2026, 11, 20) - timedelta(days=k)).isoformat() for k in range(29, -1, -1)]  # yesterday last
    assert days["2026-11-19"]["by"] == {"all": {"n": 3, "of": 7}, "Virtual Desktop": {"n": 2, "of": 4},
                                        "Desktop": {"n": 0, "of": 2}, "Laptop": {"n": 1, "of": 1}}
    assert days["2026-11-02"]["by"]["Virtual Desktop"] == {"n": 3, "of": 4} and days["2026-11-02"]["out"] == 1
    assert not days["2026-11-19"]["weekend"] and days["2026-11-14"]["weekend"]
    # a day with no row is not "0 in use"
    assert "by" not in days["2026-11-20"] and days["2026-11-20"]["why"] == "Aternity has no rows for this day yet."
    assert days["2026-11-05"]["why"] == "Aternity has no rows for this day."
    # months: only those Aternity keeps whole; each PC once in the month; today is not in "so far"
    assert [m["month"] for m in u["months"]] == ["2026-10", "2026-11"]
    octo, nov = u["months"]
    assert octo["by"]["Virtual Desktop"] == {"n": 2, "of": 4} and octo["by"]["all"] == {"n": 3, "of": 7} and not octo["so_far"]
    assert octo["busiest"]["Virtual Desktop"] == {"n": 2, "day": "2026-10-05"}
    assert nov["by"]["Virtual Desktop"] == {"n": 4, "of": 4} and nov["by"]["all"] == {"n": 6, "of": 7}
    assert nov["so_far"] and nov["through"] == "2026-11-19" and nov["out"] == 1  # its last day with rows
    assert nov["busiest"]["Virtual Desktop"] == {"n": 3, "day": "2026-11-02"}
    # the four figures: typical = the middle value (the higher of two), of days that were counted
    f = u["figures"]["Virtual Desktop"]
    assert f["work"] == {"n": 2, "of": 4, "days": 4} and f["weekend"] == {"n": 1, "of": 4, "days": 1}
    assert f["busiest"] == {"n": 3, "of": 4, "day": "2026-11-02"}
    assert f["month"] == {"n": 4, "of": 4, "month": "2026-11", "through": "2026-11-19"}
    assert u["figures"]["all"]["busiest"] == {"n": 4, "of": 7, "day": "2026-11-02"}
    # on disk: counts per kind of device, never a PC's name
    saved = (tmp_path / "inventory" / "usage.json").read_text()
    assert not any(name.lower() in saved.lower() for name in ("VD-1", "DT-1", "LT-1", "OLD-9"))
    kept = json.loads(saved)
    assert kept["days"]["2026-11-19"] == {"used": {"Virtual Desktop": 2, "Laptop": 1}, "out": 0, "final": False,
                                          "list": {"Virtual Desktop": 4, "Desktop": 2, "Laptop": 1}}
    assert kept["days"]["2026-11-18"] == {"none": True, "final": True} and kept["months"]["2026-10"]["final"]
    assert not kept["months"]["2026-11"]["final"]


def test_usage_reads_a_day_again_until_it_has_settled_and_then_never(tmp_path):
    data = dict(USED)
    tab, read, asked = usage_tab(tmp_path, data)
    read()
    # the next read, the same day: only the days that may still move; the month only when one of them did
    read(NOW + timedelta(hours=1))
    assert asked == ["used", "2026-11-20", "2026-11-19"]
    data["2026-11-20"] = ["VD-1", "DT-2"]  # Aternity adds yesterday's rows
    u = read(NOW + timedelta(hours=2))
    assert asked == ["used", "2026-11-20", "2026-11-19", "2026-11 to 2026-11-20"]
    assert u["days"][-1]["by"]["all"] == {"n": 2, "of": 7} and u["months"][-1]["by"]["Desktop"] == {"n": 2, "of": 2}
    # the day after: how far back is asked again, the new yesterday is read, Nov 19 for the last time
    u = read(NOW + timedelta(days=1))
    assert asked == ["used", "oldest", "2026-11-21", "2026-11-20", "2026-11-19", "2026-11 to 2026-11-21"]
    assert u["days"][-1]["day"] == "2026-11-21" and u["days"][-1]["by"]["Virtual Desktop"] == {"n": 1, "of": 4}
    read(NOW + timedelta(days=1, hours=1))
    assert asked == ["used", "2026-11-21", "2026-11-20"]
    # after a restart the saved counts are used: nothing kept for good is read again
    again, read2, asked2 = usage_tab(tmp_path, data, now=NOW + timedelta(days=1, hours=2))
    u2 = read2()
    assert asked2 == ["used", "oldest", "2026-11-21", "2026-11-20"]  # the month was counted today and no day moved
    assert [m["month"] for m in u2["months"]] == ["2026-10", "2026-11"] and u2["filled"] == "2026-11-21"
    assert again.view(NOW)["ready"] and usage_tab(tmp_path, data)[0].oldest == date(2026, 9, 10)  # known before the first read
    # December 3: November is over and its last day has settled, so it is counted one last time
    read(datetime(2026, 12, 3, 15, 0, tzinfo=timezone.utc))
    assert "2026-11 to 2026-11-30" in asked and json.loads((tmp_path / "inventory" / "usage.json").read_text())["months"]["2026-11"]["final"]
    read(datetime(2026, 12, 4, 15, 0, tzinfo=timezone.utc))
    assert not any(a.startswith("2026-11 to") for a in asked) and "2026-12 to 2026-12-03" in asked


def test_usage_says_what_it_could_not_read_and_never_counts_it_as_zero(tmp_path, monkeypatch):
    fail = {"2026-11-19"}
    tab, read, asked = usage_tab(tmp_path, dict(USED), fail=fail)
    u = read()
    day = {d["day"]: d for d in u["days"]}["2026-11-19"]
    assert "by" not in day and day["why"] == "Could not be read; tried again at the next read."
    assert u["notes"] == ["The app usage of Thu Nov 19 could not be read (HTTP 400): tried again at the next read."]
    assert "2026-11-19" not in json.loads((tmp_path / "inventory" / "usage.json").read_text())["days"]
    fail.clear()
    u = read(NOW + timedelta(hours=1))  # the next read counts it
    assert {d["day"]: d for d in u["days"]}["2026-11-19"]["by"]["Laptop"] == {"n": 1, "of": 1} and u["notes"] == []
    # three days in a row that cannot be read end the pass: Aternity is not answering
    tab2, read2, asked2 = usage_tab(None, dict(USED), fail={"2026-11-20", "2026-11-19", "2026-11-18"})
    u2 = read2()
    assert asked2[:5] == ["used", "oldest", "2026-11-20", "2026-11-19", "2026-11-18"] and "2026-11-17" not in asked2
    assert u2["notes"][-1] == "69 more days were not tried at this read."
    # a device list cut short: nothing is counted against it
    monkeypatch.setattr(inv, "ROWS", len(LISTED))
    tab3, read3, asked3 = usage_tab(None, dict(USED))
    u3 = read3()
    assert u3["notes"] == ["Aternity's device list was not read whole, so the PCs in use were not counted against it at "
                           "this read."] and not any("by" in d for d in u3["days"]) and u3["months"] == []
    monkeypatch.undo()
    # an Aternity that does not publish daily app usage: said, nothing charted, the tab's other panels as before
    bare = META.replace('<EntitySet Name="APPLICATIONS_DAILY"', '<EntitySet Name="APPLICATIONS_GONE"')
    tab4, read4, asked4 = usage_tab(None, dict(USED), meta=bare)
    u4 = read4()
    assert u4["off"] == "This Aternity does not publish daily app usage (APPLICATIONS_DAILY): there is nothing to chart."
    assert asked4 == [] and tab4.view(NOW)["pcs"] == 7


def test_a_row_of_another_day_in_a_days_reply_is_not_counted_for_it():
    """Nothing is assumed of how Aternity cuts a day: each row is counted by its own time."""
    tab, read, asked = usage_tab(None, dict(USED), stray=[("2026-11-18", "DT-2"), ("2026-11-20", "LT-1")])
    days = {d["day"]: d for d in read()["days"]}
    assert days["2026-11-19"]["by"] == {"all": {"n": 3, "of": 7}, "Virtual Desktop": {"n": 2, "of": 4},
                                        "Desktop": {"n": 0, "of": 2}, "Laptop": {"n": 1, "of": 1}}
    assert days["2026-11-18"]["by"]["Desktop"] == {"n": 1, "of": 2} and days["2026-11-20"]["by"]["all"] == {"n": 1, "of": 7}


def test_this_month_is_said_through_its_last_day_with_rows(tmp_path):
    data = dict(USED)
    tab, read, asked = usage_tab(tmp_path, data)
    assert read()["months"][-1]["through"] == "2026-11-19"  # yesterday, Nov 20, has no rows yet
    data["2026-11-20"] = ["VD-1"]
    assert read(NOW + timedelta(hours=1))["months"][-1]["through"] == "2026-11-20"


def test_usage_sets_a_saved_file_it_cannot_read_aside(tmp_path):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "usage.json").write_text("{ not json")
    tab, read, asked = usage_tab(tmp_path, dict(USED))
    assert "could not be read" in tab.use_files[0] and "kept as usage.unreadable" in tab.use_files[0]
    assert (tmp_path / "inventory" / "usage.unreadable").read_text() == "{ not json"
    u = read()
    assert u["notes"] == [] and json.loads((tmp_path / "inventory" / "usage.json").read_text())["filled"] == "2026-11-21"


def test_one_kind_of_device_or_none_published_gives_all_pcs_only():
    tab, read, asked = usage_tab(None, dict(USED), listed=[(n, "Desktop") for n, _ in LISTED])
    u = read()
    assert u["kinds"] == [{"key": "all", "n": 7}] and u["open"] == "all" and list(u["figures"]) == ["all"]
    assert {d["day"]: d for d in u["days"]}["2026-11-19"]["by"] == {"all": {"n": 3, "of": 7}}


def test_the_usage_charts_words_have_a_written_meaning():
    """Explain reads the first panel as the page shows it: each term on it has our meaning."""
    from uxcorrelate import explain
    page = (Path(inv.__file__).parent / "static" / "index.html").read_text()
    for term in ("Desktop usage", "in use", "In Aternity's list = 100%", "last bar is yesterday", "typical working day",
                 "typical weekend day", "busiest day", "used at least once", "so far"):
        assert term in explain.GLOSSARY["inventory"] and term.lower() in page.lower(), term
    lines = ["Desktop usage", "Virtual Desktop: in use each day, last 30 days", "52% typical working day 2,270 of 4,326 in use"]
    found = dict(explain.terms_on("inventory", "Inventory", lines, "What is a typical working day?"))
    assert list(found)[0] == "typical working day" and "middle value" in found["typical working day"]
    assert "Desktop usage" in found and "in use" in found


def test_the_dark_look_is_written_the_same_wherever_it_is_written():
    """The switch Parchment | Dark (the user, Nov 22 2026: "Build A and admin too"). CSS needs the
    dark colours twice per page (the PC's own setting, and the switch); the admin page has them
    too. They must not drift apart, and both pages keep the choice under one name."""
    import re
    static = Path(inv.__file__).parent / "static"
    looks = {}
    for name in ("index.html", "admin.html"):
        page = (static / name).read_text()
        by_pc = re.search(r'@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="light"\]\) \{(.*?)\}', page, re.S)
        by_switch = re.search(r'\n  :root\[data-theme="dark"\] \{(.*?)\}', page, re.S)
        assert by_pc and by_switch, name
        tokens = lambda text: dict(re.findall(r"(--[a-z0-9-]+|color-scheme):\s*([^;]+);", text))  # noqa: E731
        assert tokens(by_pc.group(1)) == tokens(by_switch.group(1)) and tokens(by_switch.group(1))["color-scheme"] == "dark", name
        looks[name] = tokens(by_switch.group(1))
        assert page.count('localStorage.getItem("uxTheme")') == 1 and 'const THEME_KEY = "uxTheme"' in page, name
        assert 'data-theme-pick="light">Parchment</button><button type="button" data-theme-pick="dark">Dark</button>' in page, name
        assert "color: #fff" not in page, name  # text on a filled colour follows the look (--on)
    shared = set(looks["index.html"]) & set(looks["admin.html"])
    assert {"--bg", "--panel", "--text", "--muted", "--border", "--accent", "--bad", "--warn", "--ok", "--head", "--brass", "--on"} <= shared
    assert all(looks["index.html"][k] == looks["admin.html"][k] for k in shared)
