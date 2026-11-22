"""The page check (dev only: never bundled, nothing in src/ knows about it). Run it with
dev/page_check.sh before every copy to stage that touches a page.

It opens the pages in a real Chrome and counts what is DRAWN against every number that stands for
it. Why (the user, Nov 22 2026, on photos of stage: "That number doesn't add up in stage", then "WE
REVIEWED THE CODES TO ELIMINATE ALL GAPS AND BUGS. WHY HAPPENED AGAIN?"): a button said "7 ▲" and
the tables under it carried 8 ▲. The review of that morning had compared numbers with numbers (the
button against the red flags list: 7 and 7) by a script that was not kept, and never counted the
marks on the page; the user did. A count is right only when it equals what the eye can count under
it AND the list it stands for. So this looks at the page itself, on two pages:

  1. a made-up Software tab with the shapes seen on stage (a version raised both after its update
     and against another version, by two updates, by its update only, the version most have raised
     against another, a change to a lower version, one version under two spellings, one PC on a
     version), judged by the real rule (software.judge);
  2. the dev app as it is, every tab.

Modes (page_check.sh runs both):
  data DIR   in the runtime image: writes DIR/software.html, the made-up page
  look DIR   in the uitest-chrome image: the checks; exit 1 when something does not add up
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

APP = "http://127.0.0.1:8080"
VERSION_KINDS = ("crash", "hang", "slow")
FRESH_MIN = 20  # a list read at a click is compared with the tab's tables only this soon after the tab's own read


# ---------------------------------------------------------------- the made-up Software tab
def made_up(out: str) -> None:
    from uxcorrelate import software as sw

    def row(v, pcs, crash, hang, apcs=0, actions=0, major=0, mpcs=0, new=False):
        return {"version": v, "pcs": pcs, "crash": crash, "hang": hang, "action_pcs": apcs, "actions": actions,
                "major": major, "major_pcs": mpcs, "new": new}

    def up(f, t, pcs, crash, hang, both=0, first="2026-11-12", last="2026-11-20"):
        return {"from": f, "to": t, "pcs": pcs, "both": both, "first": first, "last": last,
                "before": {"crash": crash[0], "hang": hang[0]}, "after": {"crash": crash[1], "hang": hang[1]}} \
            | ({"down": True} if sw.lower_version(t, f) else {})

    def kind(versions, updates=()):
        return {"pcs": sum(max(v["pcs"], v["action_pcs"]) for v in versions), "versions": list(versions),
                "updates": list(updates)}

    def app(name, kinds, read=("actions", "usage")):
        rows = {}
        for k in kinds.values():
            for r in k["versions"]:
                x = rows.setdefault(r["version"], dict(r, virtual=0, action_virtual=0, new=False))
                if x is not r:
                    for key in ("pcs", "crash", "hang", "action_pcs", "actions", "major", "major_pcs"):
                        x[key] += r[key]
        a = {"application": name, "read": sorted(read), "versions": sorted(rows.values(), key=lambda r: -r["pcs"]),
             "partial": False, "totals": None, "kinds": kinds}
        sw.judge(a)
        return a

    B = " (Build.160101)"
    apps = [
        # raised both after its update and against the version most have, on each kind of PC (stage, Nov 22 2026:
        # 147 of 410 against 310 of 2,615, and 22 of 203 after against 7 before: two marks, one line in the list)
        app("Sample Shell", {
            "virtual": kind([row("10.0.22621.7517" + B, 2615, 78, 310), row("10.0.26100.9711" + B, 410, 6, 147),
                             row("10.0.26100.9974" + B, 27, 0, 9, new=True), row("10.0.19045.1" + B, 1, 0, 0)],
                            [up("10.0.22621.7517" + B, "10.0.26100.9711" + B, 203, (2, 3), (7, 22), both=6, first="2026-11-13",
                                last="2026-11-21")]),
            "physical": kind([row("10.0.22621.7517" + B, 600, 12, 40), row("10.0.22621.7711" + B, 29, 1, 9)],
                             [up("10.0.22621.7517" + B, "10.0.22621.7711" + B, 29, (0, 1), (1, 8))])}, read=("usage",)),
        # two versions, each raised for another problem: the version most have crashes more, the other hangs more
        app("Sample Remote", {
            "virtual": kind([row("10.0.22621.7649" + B, 110, 10, 3), row("10.0.26100.9711" + B, 103, 2, 17),
                             row("10.0.19045.2" + B, 5, 0, 0)])}, read=("usage",)),
        # the version most have crashes more, the PCs that ran both show it (a change to a lower version); it
        # stops responding more too, which they do not show: not raised
        app("Sample Mail", {
            "virtual": kind([row("16.4.200", 2475, 147, 1291, 2400, 293837, 8115, 1900), row("16.2.100", 165, 1, 27),
                             row("16.1.050", 8, 0, 1), row("16.0.900", 7, 1, 0)],
                            [up("16.3.150", "16.4.200", 71, (1, 2), (36, 15), both=2, first="2026-11-10"),
                             up("16.4.200", "16.2.100", 53, (8, 0), (7, 9))]),
            "physical": kind([row("16.4.200", 300, 9, 40, 280, 40000, 700, 150), row("16.2.100", 60, 2, 9)])}),
        # raised by its update only: against the other version, 2 more PCs there would undo it
        app("Sample Sync", {
            "virtual": kind([row("26.153.0809.0004", 2076, 84, 17), row("26.063.0405.0002", 83, 0, 1), row("25.1.1", 3, 0, 0)],
                            [up("26.063.0405.0002", "26.153.0809.0004", 1376, (22, 50), (13, 5), both=11)])}, read=("usage",)),
        # raised against another version only
        app("Sample Tasks", {
            "virtual": kind([row("10.0.22621.6133" + B, 300, 24, 16), row("10.0.26100.9549" + B, 55, 0, 0)])}, read=("usage",)),
        # one version raised three ways: against the version most have and after two updates
        app("Sample ERP", {
            "virtual": kind([row("8.3.9", 900, 9, 5, 880, 90000, 900, 300), row("8.4.2", 400, 60, 3, 390, 40000, 420, 150),
                             row("8.2.0", 70, 1, 0)],
                            [up("8.3.9", "8.4.2", 200, (3, 30), (1, 1)), up("8.2.0", "8.4.2", 60, (1, 12), (0, 0), both=3)])}),
        # nothing raised: 101 against 89 on the PCs that ran both (stage, Nov 22 2026)
        app("Sample Chat", {
            "virtual": kind([row("26.2.7049", 2787, 30, 188, 2700, 150000, 3000, 900), row("26.1.9672", 105, 1, 17)],
                            [up("26.1.9672", "26.2.7049", 2403, (20, 22), (101, 89))])}),
        # the version most have against the smallest, a version new this week on more than 1,000 PCs
        app("Sample Browser", {
            "virtual": kind([row("157.0.4341.42", 2066, 269, 0), row("157.0.4341.57", 1021, 59, 0, new=True),
                             row("156.0.4314.58", 706, 81, 0), row("154.0.4258.62", 62, 1, 0), row("150.0.1.1", 1, 0, 0)],
                            [up("156.0.4314.58", "157.0.4341.42", 838, (33, 59), (0, 0), both=22),
                             up("154.0.4258.62", "156.0.4314.58", 266, (15, 20), (0, 0))])}, read=("usage",)),
    ]
    apps[2]["spellings"] = {"16.4.200": ["16.4.200.0"], "16.2.100": ["16.2.100 (Build.7)"]}
    snap = {"versions": {a["application"]: a for a in apps}}
    flags = [dict(f, new=False, days=[]) for f in sw._flags(snap)]
    base = json.load(urllib.request.urlopen(APP + "/api/software", timeout=60))
    if not base.get("ready"):
        sys.exit("The dev app's Software tab has no read yet: wait for it, then run the page check again.")
    base["flags"] = flags + [f for f in base["flags"] if f["kind"] not in VERSION_KINDS]
    base["versions"] = sw._versions_view(snap["versions"])
    base["applications"] = sorted(snap["versions"])
    base |= {"reading": False, "status": "Read", "off": None}
    # the shapes the check is for must be in the data, or it would pass by looking at nothing
    ways = {}
    for a in apps:
        for r in a["raised"]:
            ways.setdefault((a["application"], r["on"], r["kind"], r["version"]), []).append(r["how"])
    twice = [k for k, h in ways.items() if "update" in h and len(set(h)) > 1]
    two_updates = [k for k, h in ways.items() if h.count("update") > 1]
    if not twice or not two_updates:
        sys.exit(f"The made-up data lost its shapes: raised two ways {len(twice)}, by two updates {len(two_updates)}.")
    page = open("/app/src/uxcorrelate/static/index.html", encoding="utf-8").read()
    fake = ("<script>const FAKE = " + json.dumps(base).replace("</", "<\\/") + ";\n"
            "window.fetch = async (url) => new Response(String(url) === 'api/software' ? JSON.stringify(FAKE) : '{}', "
            "{status: String(url) === 'api/software' ? 200 : 404, headers: {'Content-Type': 'application/json'}});</script>\n")
    open(out + "/software.html", "w", encoding="utf-8").write(fake + page)
    print(f"Made-up Software tab: {len(apps)} applications, {len(flags)} red flags about a version, "
          f"{len(twice)} raised two ways, {len(two_updates)} by two updates.")


# ---------------------------------------------------------------- the browser
class Browser:
    def __init__(self):
        import subprocess
        import time
        import websocket
        self.time = time
        self.proc = subprocess.Popen(
            ["chromium", "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--remote-debugging-port=9223",
             "--remote-allow-origins=*", "--user-data-dir=/tmp/prof", "--window-size=1280,1000",
             "--allow-file-access-from-files", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page = None
        for _ in range(80):
            try:
                page = [t for t in json.load(urllib.request.urlopen("http://127.0.0.1:9223/json/list")) if t["type"] == "page"][0]
                break
            except Exception:  # noqa: BLE001  Chrome is still starting
                time.sleep(0.25)
        if not page:
            sys.exit("Chrome did not start in the uitest-chrome image.")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=180)
        self.seq, self.errors, self.where = 0, [], "start"
        for d in ("Runtime", "Page", "Log"):
            self.cmd(d + ".enable")

    def cmd(self, method, **params):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.seq:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})
            self.note(m)

    def note(self, m):
        k = m.get("method")
        if k == "Runtime.exceptionThrown":
            d = m["params"]["exceptionDetails"]
            self.errors.append(f"{self.where}: script error: {d.get('text')} {(d.get('exception') or {}).get('description', '')[:200]}")
        elif k == "Runtime.consoleAPICalled" and m["params"]["type"] == "error":
            self.errors.append(f"{self.where}: console error: "
                               + " ".join(str(a.get("value", a.get("description", ""))) for a in m["params"]["args"])[:240])

    def ev(self, expr):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError(f"{self.where}: {(d.get('exception') or {}).get('description') or d.get('text')}: {expr[:90]}")
        return r["result"].get("value")

    def wait(self, expr, secs=60):
        end = self.time.time() + secs
        while self.time.time() < end:
            if self.ev(expr):
                return True
            self.time.sleep(0.5)
        return False

    def go(self, url, settle=2.0):
        self.cmd("Page.navigate", url=url)
        self.time.sleep(settle)


class Tally:
    def __init__(self):
        self.ok, self.bad = 0, []

    def same(self, what, *values):
        """Every value the same, or a line saying which is not."""
        if all(v == values[0] for v in values[1:]):
            self.ok += 1
        else:
            self.bad.append(f"{what}: {' / '.join(repr(v) for v in values)}")

    def none(self, what, found):
        if not found:
            self.ok += 1
        else:
            self.bad.append(f"{what}: {json.dumps(found, ensure_ascii=False)[:600]}")


# "1 virtual desktops", "1 PCs": a count of one with a plural after it
ONE_OF_MANY = (r"(?:^|[^\d,.\w])1 (?:PCs|users|programs|versions|virtual desktops|physical PCs|installs|days|actions|"
               r"events|people|desktops|devices|applications|updates|changes|lines|hours|minutes|reads|times|months)\b")
JS_TEXT = "(sel) => [...document.querySelectorAll(sel)].filter((e) => !e.hidden && e.offsetParent !== null).map((e) => e.innerText).join('\\n')"


def grammar(b: Browser, t: Tally, where: str, selector: str) -> None:
    text = b.ev(f"({JS_TEXT})({json.dumps(selector)})") or ""
    hits = sorted({text[max(0, m.start() - 40):m.end() + 10].replace("\n", " ").strip() for m in re.finditer(ONE_OF_MANY, text)})
    t.none(f"{where}: a count of 1 with a plural word after it", hits)


# One kind of PC of "Application health by version", as drawn: the button's number, the ▲ in the tables, the red
# flags list's lines for that kind, and where each mark sits.
JS_KIND = r"""
((K) => {
  const box = document.getElementById("sVersions");
  const btn = document.querySelector(`#sKindsSeg [data-swkind="${K}"]`);
  if (btn && !btn.classList.contains("on")) btn.click();
  const said = btn ? (/·\s*([\d,]+)\s*▲/.exec(btn.innerText) || [0, "0"])[1].replace(/,/g, "") : null;
  const cells = [...box.querySelectorAll("button.vcell")];
  const marked = cells.filter((c) => c.querySelector(".warn"));
  const lines = softData.flags.filter((f) => ["crash", "hang", "slow"].includes(f.kind) && (f.on || "all") === K);
  const off = [];   // a line of the list with no ▲, or more than one, or a ▲ on another count than the line quotes
  for (const f of lines) {
    const its = marked.filter((c) => c.dataset.app === f.application && c.dataset.version === f.version && c.dataset.problem === f.kind);
    if (its.length !== 1) off.push(`${f.application} ${f.version} ${f.kind}: ${its.length} marks`);
    else if (Number(its[0].dataset.expect) !== f.pcs) off.push(`${f.application} ${f.version} ${f.kind}: the mark is on ${its[0].dataset.expect}, the line quotes ${f.pcs}`);
  }
  const amber = cells.filter((c) => c.querySelector(".vtrk i.up"));
  const show = [];  // "Show the N PCs" with another N than its own count
  for (const c of cells) {
    const go = c.querySelector(".go"), num = /^([\d,]+) of/.exec((c.querySelector(".vnum") || {}).innerText || "");
    if (go && num && (/Show the ([\d,]+) /.exec(go.innerText) || [])[1] !== num[1] && c.dataset.problem !== "slow") show.push(c.innerText.replace(/\s+/g, " ").slice(0, 90));
  }
  const pct = cells.filter((c) => { const m = /^([\d,]+) of ([\d,]+)/.exec((c.querySelector(".vnum") || {}).innerText || "");
    return m && swPct(Number(m[1].replace(/,/g, "")), Number(m[2].replace(/,/g, ""))) !== c.querySelector(".vpct").innerText; }).length;
  return {
    said: btn ? Number(said) : null, marks: marked.length, glyphs: (box.textContent.match(/▲/g) || []).length, lines: lines.length, off,
    amber: amber.length,
    amberSilent: amber.filter((c) => !c.querySelector(".warn") && !/Counted as one red flag/.test(c.innerText)).map((c) => c.innerText.replace(/\s+/g, " ").slice(0, 90)),
    plain: cells.filter((c) => c.querySelector(".warn") && !c.querySelector(".vtrk i.up")).length,
    counted: amber.filter((c) => !c.querySelector(".warn")).map((c) => c.innerText.replace(/\s+/g, " ").slice(0, 200)),
    openNoMark: [...box.querySelectorAll(".vblock")].filter((x) => !x.querySelector(".warn")).map((x) => x.querySelector(".vhead b").innerText),
    foldedRaised: [...box.querySelectorAll("details.vcalm")].filter((x) => x.querySelector(".warn, .vtrk i.up")).map((x) => x.querySelector("summary").innerText),
    show, pct,
  };
})"""


def software(b: Browser, t: Tally, where: str) -> dict:
    """The Software tab on screen: every count of red flags against what is drawn."""
    seen = {}
    top = b.ev("""(() => ({ badge: Number((document.getElementById("softCount").textContent || "0").replace(/,/g, "")),
        tile: Number(([...document.querySelectorAll("#sTiles .tile")].find((x) => /red flags/.test(x.innerText)) || document.body)
          .querySelector(".big").innerText.replace(/,/g, "")),
        lines: document.querySelectorAll("#sFlags .flag").length, flags: softData.flags.length,
        kinds: [...document.querySelectorAll("#sKindsSeg [data-swkind]")].map((x) => x.dataset.swkind),
        only: softData.versions.length ? Object.keys(softData.versions[0].kinds) : [] }))()""")
    t.same(f"{where}: the tab's number, the \"red flags\" tile and the lines drawn in the list", top["badge"], top["tile"], top["lines"], top["flags"])
    kinds = top["kinds"] or top["only"][:1]
    total = 0
    for K in kinds:
        k = b.ev(f"({JS_KIND})({json.dumps(K)})")
        b.time.sleep(0.2)
        seen[K] = k
        total += k["lines"]
        name = {"virtual": "virtual desktops", "physical": "physical PCs", "all": "all PCs"}.get(K, K)
        if k["said"] is None:  # one kind of PC only: no switch, so no button
            t.same(f"{where}, {name}: the ▲ in the tables and the list's lines about a version", k["marks"], k["lines"])
        else:
            t.same(f"{where}, {name}: the button's number, the ▲ drawn in the tables and the list's lines about a version",
                   k["said"], k["marks"], k["lines"])
        t.same(f"{where}, {name}: every ▲ in the tables is a mark on a count (no ▲ in a sentence)", k["glyphs"], k["marks"])
        t.none(f"{where}, {name}: a line of the list without its one ▲ on the count it quotes", k["off"])
        t.none(f"{where}, {name}: an amber count that neither has the ▲ nor says which red flag it is counted with", k["amberSilent"])
        t.same(f"{where}, {name}: a ▲ that is not on an amber bar", k["plain"], 0)
        t.none(f"{where}, {name}: an application drawn open with no ▲", k["openNoMark"])
        t.none(f"{where}, {name}: a folded application with something raised in it", k["foldedRaised"])
        t.none(f"{where}, {name}: \"Show the N PCs\" with another N than its count", k["show"])
        t.same(f"{where}, {name}: percentages that are not their own count's", k["pct"], 0)
    t.same(f"{where}: the list's lines about a version and the kinds of PC added up",
           b.ev("softData.flags.filter((f) => ['crash', 'hang', 'slow'].includes(f.kind)).length"), total)
    return seen


def look(out: str) -> int:
    b, t = Browser(), Tally()
    # 1. the made-up Software tab
    b.where = "Software, made-up"
    b.go(f"file://{out}/software.html#software", 3)
    if not b.wait("document.querySelectorAll('#sVersions .vblock').length > 0", 40):
        t.bad.append("Software, made-up: the version tables were not drawn")
    else:
        seen = software(b, t, b.where)
        grammar(b, t, b.where, "#softBox")
        doubled = sum(len(k["counted"]) for k in seen.values())
        t.same("Software, made-up: counts raised a second way that say \"Counted as one red flag\" (4 in the data)", doubled, 4)
        for K, k in seen.items():
            for c in k["counted"]:
                print(f"    {K}: {c}")
        # a failed read is said on the tab, with the last read under it
        said = b.ev("""(() => { const v = { ...softData, reading: false, status: "The software read failed (timed out); trying again in 24 hours." };
            renderSoftware(v); const s = document.getElementById("sStatus").innerText; renderSoftware(softData); return [s, document.getElementById("sStatus").innerText]; })()""")
        t.same("Software, made-up: a failed read is said on the tab", "read failed" in said[0] and "last read" in said[0], True)
        t.same("Software, made-up: nothing is said after a good read", said[1], "")
        # Explain of the count raised a second way says where the mark is, with no ▲ of its own
        b.where = "Software, made-up, Explain"
        ex = b.ev("""(() => { const blk = [...document.querySelectorAll("#sVersions .vblock")].find((x) => /Sample Shell/.test(x.querySelector(".vhead b").innerText));
            blk.querySelector('[data-vex$="versions"]').click(); const body = document.getElementById("dlgBody");
            const r = { open: !document.getElementById("dlg").hidden, marks: body.querySelectorAll(".warn").length, text: body.innerText.replace(/\\s+/g, " ") };
            document.getElementById("dlgClose").click(); return r; })()""")
        t.same("Software, made-up: Explain of a count raised a second way has no ▲ and says it is one red flag with its update",
               (ex["open"], ex["marks"], "counted as one red flag with its update" in ex["text"]), (True, 0, True))
        ex = b.ev("""(() => { const blk = [...document.querySelectorAll("#sVersions .vblock")].find((x) => /Sample Shell/.test(x.querySelector(".vhead b").innerText));
            blk.querySelector('[data-vex*="update"]').click(); const body = document.getElementById("dlgBody");
            const r = { marks: body.querySelectorAll(".warn").length, text: body.innerText.replace(/\\s+/g, " ") };
            document.getElementById("dlgClose").click(); return r; })()""")
        t.same("Software, made-up: Explain of the update that carries the mark has its one ▲", ex["marks"], 1)
        for width, mobile in ((420, True),):
            b.cmd("Emulation.setDeviceMetricsOverride", width=width, height=900, deviceScaleFactor=1, mobile=mobile)
            b.time.sleep(0.6)
            t.same(f"Software, made-up: the page is no wider than a phone ({width} px)", b.ev("document.documentElement.scrollWidth > innerWidth + 1"), False)
        b.cmd("Emulation.clearDeviceMetricsOverride")
    made_up_errors = [e for e in b.errors if "404" not in e and "Failed to load" not in e and "Could not load" not in e]
    b.errors.clear()
    # 2. the dev app, every tab
    b.where = "dev app"
    b.go(APP + "/", 4)
    tabs = (("Today", "tabToday", "todayBox", "document.getElementById('tTiles') && document.getElementById('tTiles').children.length > 0"),
            ("Care list", "tabCare", "careBox", "document.querySelectorAll('#cTiles .tile').length > 0"),
            ("Investigate", "tabInv", None, "true"), ("Start-ups", "tabLog", None, "true"),
            ("Software", "tabSoft", "softBox", "typeof softData === 'object' && softData && softData.ready && document.querySelectorAll('#sFlags .flag, #sFlags p').length > 0"),
            ("Inventory", "tabDv", "dvBox", "document.querySelectorAll('#dvTiles .tile').length > 0"))
    for name, tab, box, ready in tabs:
        b.where = f"dev app, {name}"
        b.ev(f"document.getElementById({json.dumps(tab)}).click()")
        if not b.wait(ready, 60):
            t.bad.append(f"{b.where}: the tab did not fill in 60 seconds")
            continue
        b.time.sleep(1.5)
        grammar(b, t, b.where, "main")
        if name == "Software":
            software(b, t, b.where)
        if name == "Care list":
            c = b.ev("""(() => { const tiles = [...document.querySelectorAll("#cTiles .tile")];
                const calm = (x) => [...x.querySelectorAll(".sev")].some((s) => /^(✓ OK|not found)$/.test(s.innerText.trim()));
                return { badge: Number(document.getElementById("careCount").textContent || 0), alert: tiles.filter((x) => !calm(x)).length, tiles: tiles.length }; })()""")
            t.same(f"{b.where}: the tab's number and the tiles drawn in alert", c["badge"], c["alert"])
        if name == "Inventory":
            v = b.ev("""(() => { const tile = [...document.querySelectorAll("#dvTiles .tile")].find((x) => /to look at/.test(x.innerText));
                const off = [...document.querySelectorAll("#dvLines .dvLine .num")].filter((x) => { const m = /^([\\d,]+) PCs? · (\\S+) of ([\\d,]+)/.exec(x.innerText.replace(/\\s+/g, " "));
                  return !m || swPct(Number(m[1].replace(/,/g, "")), Number(m[3].replace(/,/g, ""))) !== m[2]; }).map((x) => x.innerText);
                return { tile: Number(tile.querySelector(".big").innerText), lines: document.querySelectorAll("#dvLines .dvLine").length, off }; })()""")
            t.same(f"{b.where}: the \"things to look at\" tile and the lines drawn", v["tile"], v["lines"])
            t.none(f"{b.where}: a line whose percentage is not its own count's", v["off"])
    errors = made_up_errors + [e for e in b.errors if "favicon" not in e]
    t.none("Script errors on the pages", errors)
    b.proc.terminate()
    counts(t)
    print(f"\nPage check: {t.ok} things counted and compared agree; {len(t.bad)} do not.")
    for line in t.bad:
        print("  ✗", line)
    return 1 if t.bad else 0


# ---------------------------------------------------------------- the app's own numbers against its lists
def counts(t: Tally) -> None:
    """Every count the dev app gives against the list or panel that should match it (the review of Nov 22 2026)."""
    def get(path, **q):
        try:
            return json.load(urllib.request.urlopen(APP + path + ("?" + urllib.parse.urlencode(q) if q else ""), timeout=120))
        except Exception as exc:  # noqa: BLE001
            t.bad.append(f"GET {path} {q}: {exc}")
            return None

    d = get("/api/today")
    if d and d.get("ready"):
        s = d["slow"]
        t.same("Today: Major actions and the hours added up", s["actions"], sum(h["count"] for h in d["hours"]))
        w = get("/api/today/who", kind="slow")
        if w:
            t.same("Today: PCs with a Major action and the PCs in its list", s["pcs"], w["pcs"])
            t.same("Today: users with a Major action and the users in its list", s["users"], w["users"])
            if w["total"] == len(w["rows"]):
                t.same("Today: Major actions and its list's counts added up", s["actions"], sum(r["count"] for r in w["rows"]))
        for a in d["applications"]:
            w = get("/api/today/who", kind="app", name=a["application"])
            if w:
                t.same(f"Today, {a['application']}: PCs and its list's PCs", a["pcs"], w["pcs"])
                if w["total"] == len(w["rows"]):
                    t.same(f"Today, {a['application']}: Major actions and its list's counts", a["actions"], sum(r["count"] for r in w["rows"]))
        for tile in d["tiles"] + d["events"]:
            w = get("/api/today/who", kind="event", name=tile.get("name") or tile.get("event"), category=tile.get("category", ""))
            if w:
                t.same(f"Today, event {tile.get('name')}: PCs and its list's PCs", tile.get("pcs"), w["pcs"])
        act = d["active"]["today"]
        t.same("Today: active PCs and virtual + physical + not known", act["pcs"], act["virtual"] + act["physical"] + act["not_known"])
        t.same("Today: active users and on virtual + on physical - on both", act["users"],
               act["users_virtual"] + act["users_physical"] - act["users_both"])
        t.same("Today: active PCs and the slow panel's active PCs", act["pcs"], s["active_pcs"])
    c = get("/api/care")
    if c and c.get("ready"):
        for u in c["users"]:
            today = [x for x in u.get("days") or [] if x.get("today")]
            if today:
                t.same(f"Care list, a user: the tile's Major actions and today's bar", u["major"], today[0]["major"])
            t.same(f"Care list, a user: the tile's Major actions and its list of them", u["major"], len(u.get("majors") or []))
        t.same("Care list: the tab's number and the users in alert", c["count"], sum(1 for u in c["users"] if u["status"] == "issue"))
        st = get("/api/MSS/care/status")
        if st:
            t.same("Care list: the status feed's alerts and the tab's", sum(1 for u in st["users"] if u["status"] == "alert"), c["count"])
    v = get("/api/inventory")
    if v and v.get("ready"):
        t.same("Inventory: PCs and the kinds added up", v["pcs"], sum(k["n"] for k in v["kinds"]))
        t.same("Inventory: PCs and the agent's statuses added up", v["pcs"], sum(k["n"] for k in v["statuses"]))
        h = v["heard"]
        t.same("Inventory: PCs and last heard, every band", v["pcs"], h["today"] + h["week"] + h["silent"] + h["old"] + h["none"])
        for ln in v["lines"] + v.get("quiet_lines", []):
            got = get("/api/inventory/pcs", line=ln["key"])
            if got:
                t.same(f"Inventory, line {ln['key']}: its count and its list", ln["n"], got["total"])
        lines = {ln["key"]: ln for ln in v["lines"]}
        if "silent" in lines:
            t.same("Inventory: not heard from and last heard's two oldest bands", lines["silent"]["n"], h["silent"] + h["old"])
        if "stopped" in lines:
            t.same("Inventory: agent stopped and the status 'Stopped'", lines["stopped"]["n"],
                   sum(k["n"] for k in v["statuses"] if k["status"] == "Stopped"))
        u = v["used"]
        t.same("Inventory: used + not used and the PCs they are counted among", u["used"] + u["unused"], u["of"])
        us = v["usage"]
        for kd in ["all"] + [k for k in us["figures"] if k != "all"]:
            f = us["figures"].get(kd) or {}
            days = [x["by"][kd]["n"] for x in us["days"] if x.get("by") and kd in x["by"]]
            if f.get("busiest") and days:
                t.same(f"Inventory usage, {kd}: the busiest day and the highest bar", f["busiest"]["n"], max(days))
            mon = us["months"][-1] if us["months"] else None
            if mon and f.get("month"):
                t.same(f"Inventory usage, {kd}: the month so far and the last month's bar", f["month"]["n"], mon["by"][kd]["n"])
        for x in us["days"]:
            if x.get("by"):
                t.same(f"Inventory usage, {x['day']}: all and the kinds added up", x["by"]["all"]["n"],
                       sum(y["n"] for k, y in x["by"].items() if k != "all"))
    sv = get("/api/software")
    # the sample's day moves on: a list read now holds more than a table read hours ago (the page says so at the click)
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(sv["as_of"])).total_seconds() / 60 if sv and sv.get("ready") else 0
    if age > FRESH_MIN:
        t.bad.append(f"Software: the lists behind the counts were not compared: the tab's read is {age:.0f} minutes old. "
                     "Restart the app (docker compose restart uxcorrelate), wait for its read, run this again.")
    if sv and sv.get("ready"):
        for a in sv["versions"] if age <= FRESH_MIN else []:
            for K, k in a["kinds"].items():
                on = {"kind": K} if K != "all" else {}
                for u in k["updates"]:
                    for kd in ("crash", "hang"):
                        if u["after"][kd]:
                            got = get("/api/software/pcs", application=a["application"], version=u["to"], problem=kd, after="1", was=u["from"], **on)
                            if got:
                                t.same(f"Software, {a['application']} {u['from']} to {u['to']}, {K}, {kd} after: its count and its list",
                                       u["after"][kd], got["total"])
                    ba = get("/api/software/update", application=a["application"], **on)
                    if ba:
                        p = next((p for p in ba["pairs"] if (p["from"], p["to"]) == (u["from"], u["to"])), None)
                        t.same(f"Software, {a['application']} {u['from']} to {u['to']}, {K}: Compare's before and after and the table's",
                               p and (p["pcs"], p["before"], p["after"]), (u["pcs"], u["before"], u["after"]))
                for r in k["versions"]:
                    for kd, n in (("crash", r["crash"]), ("hang", r["hang"]), ("slow", r["major_pcs"])):
                        if n:
                            got = get("/api/software/pcs", application=a["application"], version=r["version"], problem=kd, **on)
                            if got:
                                t.same(f"Software, {a['application']} {r['version']}, {K}, {kd}: its count and its list", n, got["total"])
        for p in sv["behind"][:5]:
            got = get("/api/software/program", name=p["name"])
            if got:
                t.same(f"Software, {p['name']}: its versions' PCs and its installs", got["installs"], sum(n for _, n in got["versions"]))
        for w in (sv.get("windows") or {}).get("versions", []):
            for m in w["missing"]:
                t.same(f"Software, Windows {w['windows']} {m['update']}: on + not on and its PCs", m["on"] + m["without"], w["pcs"])
                got = get("/api/software/pcs", update=m["update"], windows=w["windows"])
                if got:
                    t.same(f"Software, {m['update']}: not on N and its list", m["without"], got["total"])


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("data", "look"):
        sys.exit(__doc__)
    if sys.argv[1] == "data":
        made_up(sys.argv[2])
    else:
        sys.exit(look(sys.argv[2]))
