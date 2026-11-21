"""The Inventory tab (inventory.py): Aternity's device list read every hour, held in memory; the
rules of "Needs a look", the lists by a click, Find a PC. The user's "build it", Nov 21 2026."""
from datetime import datetime, timedelta, timezone
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
