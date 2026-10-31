"""Writes the page drawings of the UX Correlator web UI into this folder:
01-investigate-page.drawio, 02-slow-startups-page.drawio, 03-matching-rule.drawio.
Values are made-up examples; no real host names or addresses."""
import html
from pathlib import Path

OUT = Path(__file__).parent

# the page's own colours: the same kind of part, the same colour everywhere
C = {"page": ("#f6f7f9", "#dde1e7"), "panel": ("#ffffff", "#dde1e7"), "head": ("#ffffff", "#dde1e7"),
     "btn": ("#2563eb", "#2563eb"), "tab": ("#ffffff", "#dde1e7"), "input": ("#f6f7f9", "#dde1e7"),
     "pc": ("#8b5cf6", "#8b5cf6"), "network": ("#0ea5e9", "#0ea5e9"), "server": ("#f59e0b", "#f59e0b"),
     "slow": ("#d33c3c", "#d33c3c"), "normal": ("#9aa3b2", "#9aa3b2"), "note": ("#fff8e1", "#f9a825"),
     "ok": ("#e8f5e9", "#2e7d32"), "no": ("#ffebee", "#c62828"), "card": ("#ffffff", "#9aa3b2")}
TEXT, MUTED = "#1d2330", "#5d6676"


class D:
    def __init__(self):
        self.cells, self.n = [], 2

    def id(self):
        self.n += 1
        return f"c{self.n}"

    def box(self, x, y, w, h, text="", kind="panel", size=12, bold=False, color=TEXT, align="center",
            valign="middle", rounded=1, extra=""):
        i, (fill, line) = self.id(), C[kind]
        t = f"<b>{text}</b>" if bold and text else text
        st = (f"rounded={rounded};whiteSpace=wrap;html=1;fillColor={fill};strokeColor={line};fontSize={size};"
              f"fontColor={color};align={align};verticalAlign={valign};arcSize=6;spacingLeft=6;spacingRight=6;{extra}")
        self.cells.append(f'<mxCell id="{i}" value="{html.escape(t)}" style="{st}" vertex="1" parent="1">'
                          f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
        return i

    def text(self, x, y, w, h, text, size=12, color=TEXT, align="left", bold=False, valign="middle"):
        i = self.id()
        t = f"<b>{text}</b>" if bold else text
        self.cells.append(f'<mxCell id="{i}" value="{html.escape(t)}" style="text;html=1;fontSize={size};'
                          f'fontColor={color};align={align};verticalAlign={valign};whiteSpace=wrap;spacing=2;" '
                          f'vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
        return i

    def dot(self, x, y, r, kind, opacity=100):
        i, (fill, line) = self.id(), C[kind]
        self.cells.append(f'<mxCell id="{i}" value="" style="ellipse;fillColor={fill};strokeColor=none;opacity={opacity};" '
                          f'vertex="1" parent="1"><mxGeometry x="{x - r}" y="{y - r}" width="{2 * r}" height="{2 * r}" as="geometry"/></mxCell>')

    def bar(self, x, y, w, h, kind, opacity=100):
        i, (fill, _) = self.id(), C[kind]
        self.cells.append(f'<mxCell id="{i}" value="" style="rounded=0;fillColor={fill};strokeColor=none;opacity={opacity};" '
                          f'vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')

    def line(self, x1, y1, x2, y2, color="#dde1e7", width=1, arrow=False, dashed=False):
        i = self.id()
        st = (f"endArrow={'block;endFill=1' if arrow else 'none'};html=1;strokeColor={color};strokeWidth={width};"
              f"{'dashed=1;' if dashed else ''}")
        self.cells.append(f'<mxCell id="{i}" value="" style="{st}" edge="1" parent="1"><mxGeometry relative="1" as="geometry">'
                          f'<mxPoint x="{x1}" y="{y1}" as="sourcePoint"/><mxPoint x="{x2}" y="{y2}" as="targetPoint"/>'
                          f'</mxGeometry></mxCell>')

    def pill(self, x, y, kind, label):
        self.box(x, y, 64, 20, label, kind, size=11, bold=True, color="#ffffff", extra="arcSize=50;")

    def num(self, x, y, n):
        """A numbered marker; the notes column explains each number."""
        self.box(x, y, 24, 24, str(n), "note", size=12, bold=True, extra="ellipse;")

    def table(self, x, y, widths, rows, head=True, row_h=28, size=11):
        for r, row in enumerate(rows):
            cx = x
            for w, cell in zip(widths, row):
                if isinstance(cell, tuple):  # (kind, label) pill
                    self.pill(cx + 6, y + (row_h - 20) / 2, *cell)
                else:
                    self.text(cx + 4, y, w - 8, row_h, cell, size=size, color=MUTED if head and r == 0 else TEXT,
                              bold=head and r == 0)
                cx += w
            self.line(x, y + row_h, x + sum(widths), y + row_h)
            y += row_h
        return y

    def notes(self, x, y, w, items, title="How to use it"):
        self.text(x, y, w, 24, title, size=14, bold=True)
        y += 30
        for n, t in items:
            self.num(x, y, n)
            h = 16 * (1 + len(t) // 46)
            self.text(x + 32, y - 2, w - 32, max(28, h + 8), t, size=12, valign="top")
            y += max(34, h + 18)

    def save(self, name, w, h):
        xml = (f'<mxfile host="drawio"><diagram name="{name}" id="{name}"><mxGraphModel dx="{w}" dy="{h}" grid="0" '
               f'gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
               f'pageWidth="{w}" pageHeight="{h}" math="0" shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
               + "".join(self.cells) + "</root></mxGraphModel></diagram></mxfile>")
        (OUT / f"{name}.drawio").write_text(xml)


def header(d, active):
    d.box(0, 0, 1000, 50, "", "head", rounded=0)
    d.text(20, 10, 140, 30, "UX Correlator", size=16, bold=True)
    d.text(160, 10, 360, 30, "Aternity end-user experience × NetIM infrastructure", size=12, color=MUTED)
    for i, label in enumerate(["Investigate", "Slow start-ups"]):
        on = label == active
        d.box(770 + i * 110, 12, 100, 26, label, "btn" if on else "tab", size=12, bold=on,
              color="#ffffff" if on else MUTED)


def form(d, y, fields, button):
    d.box(20, y, 960, 70, "", "panel")
    x = 36
    for label, w, value in fields:
        d.text(x, y + 8, w, 16, label, size=10, color=MUTED)
        d.box(x, y + 26, w, 28, value, "input", size=11, align="left", color=TEXT if value else MUTED)
        x += w + 10
    d.box(x, y + 26, 980 - 16 - x, 28, button, "btn", size=12, bold=True, color="#ffffff")


# ------------------------------------------------------------------ 01 investigate
def investigate():
    d = D()
    d.box(0, 0, 1000, 1592, "", "page", rounded=0)
    header(d, "Investigate")
    form(d, 66, [("Username", 120, "a.user"), ("Device", 110, ""), ("Application", 110, "optional"),
                 ("Start (local)", 150, "Oct 25, 08:00 AM"), ("End (local)", 150, "Oct 28, 06:00 PM"),
                 ("Match within (min)", 110, "5")], "Investigate")
    d.num(990 - 12, 70, 1)

    # answer
    y = 150
    d.box(20, y, 960, 70, "", "panel")
    d.bar(20, y, 6, 70, "slow")
    d.text(40, y + 8, 920, 30, "Likely: Outlook was not responding on LAPTOP-01 (up to 41 s)", size=18, bold=True)
    d.text(40, y + 38, 920, 24, "It happened within 5 min of 3 of the 4 slow actions. Outlook failed in AddinX.dll 2.1 "
                                "(inside outlook.exe 16.0): Not responding.", size=12)
    d.num(990 - 12, y + 4, 2)

    # felt / who else
    y = 236
    d.box(20, y, 470, 170, "", "panel")
    d.text(36, y + 8, 300, 22, "What the user felt", size=13, bold=True)
    d.text(36, y + 32, 440, 130,
           "<b>4</b> of their 37 actions were slow (Outlook 3, Teams 1), between Oct 27, 08:40 AM – Oct 28, 11:55 AM.<br><br>"
           "Slowest: <b>92.9 s</b> in Outlook, Oct 28, 11:53 AM.<br><br>"
           "Aternity measured the slow time: PC 82%, network 10%, server 8%.<br><br>"
           "Slowest start-up: Oct 27, 06:38 AM on LAPTOP-01: Machine Boot <b>41 s</b>, User Logon <b>33.5 s</b> (usual 46.1 s).",
           size=11, valign="top")
    d.box(510, y, 470, 170, "", "panel")
    d.text(526, y + 8, 300, 22, "Who else is affected?", size=13, bold=True)
    d.text(526, y + 32, 440, 70,
           "41 other PC(s) had the same failure (Outlook failing in AddinX.dll 2.1 (inside outlook.exe 16.0)), "
           "all on 2.1. This PC has 2.1.", size=11, valign="top")
    d.text(526, y + 100, 440, 30, "Searched: every PC's health events for Outlook, Oct 25, 08:00 AM – "
                                  "Oct 28, 06:00 PM.", size=10, color=MUTED, valign="top")
    d.text(526, y + 136, 120, 24, "Hide who", size=12, bold=True, color="#2563eb")
    # "Show who" opened: the other PCs with the same failure
    y = 422
    d.box(20, y, 960, 152, "", "panel")
    d.table(36, y + 8, [180, 160, 200, 120, 220], [
        ["PC", "User", "Version", "Times", "Last seen"],
        ["LAPTOP-17", "b.user", "2.1", "9", "Oct 28, 04:12 PM"],
        ["LAPTOP-22", "c.user", "2.1", "6", "Oct 28, 11:03 AM"],
        ["DESKTOP-04", "d.user", "2.1", "4", "Oct 27, 02:47 PM"],
    ])
    d.text(36, y + 122, 400, 22, "… and 38 more", size=11, color=MUTED)
    d.num(990 - 12, 240, 3)

    # found and why / todo
    y = 590
    d.box(20, y, 960, 252, "", "panel")
    d.text(36, y + 8, 400, 22, "What we found, and why", size=13, bold=True)
    d.text(36, y + 34, 920, 20, "• Outlook was not responding on LAPTOP-01 (up to 41 s): "
                                "<b>at 3 of 4 slow actions</b>, from Oct 27, 08:40 AM", size=11)
    d.box(56, y + 58, 900, 64, "", "page", rounded=0)
    d.bar(56, y + 58, 3, 64, "btn")
    d.text(68, y + 60, 880, 60,
           "<b>Why:</b><br>◦ Outlook failed in AddinX.dll 2.1 (inside outlook.exe 16.0): Not responding.<br>"
           "◦ Changed on LAPTOP-01 in the 14 days before Oct 27: Oct 26 AddinX Updated 2.0 → 2.1 (same name as the failing part).",
           size=11, valign="top")
    d.text(36, y + 128, 920, 20, "• LAPTOP-01's disk could not keep up (queue 6.2, limit 2): "
                                "<b>at 2 of 4 slow actions</b>, from Oct 27, 08:41 AM", size=11)
    d.box(56, y + 152, 900, 40, "", "page", rounded=0)
    d.bar(56, y + 152, 3, 40, "btn")
    d.text(68, y + 154, 880, 36, "<b>Why:</b> ◦ Busiest processes then: MsMpEng.exe (disk I/O 48.2), "
                                 "OneDrive.exe (disk I/O 6.1).", size=11)
    d.text(36, y + 200, 920, 46,
           "• 9 other problem(s) happened only at other times, not near a slow action.<br>"
           "• The network was not checked: this PC's address is not in any NetIM subnet.", size=11, valign="top")
    d.num(990 - 12, y + 4, 4)
    y = 858
    d.box(20, y, 960, 96, "", "panel")
    d.text(36, y + 8, 300, 22, "What to do", size=13, bold=True)
    d.text(36, y + 32, 920, 60,
           "• 41 other PCs fail the same way: treat it as an AddinX.dll problem (version, update, vendor), not this PC alone.<br>"
           "• Check the AddinX change of 2026-10-26 on LAPTOP-01: it came before the first failure.<br>"
           "• On LAPTOP-01: look at MsMpEng.exe, the busiest process at those times.",
           size=11, valign="top")

    # timeline
    y = 970
    d.box(20, y, 960, 270, "", "panel")
    d.text(36, y + 8, 300, 22, "Timeline", size=13, bold=True)
    L, R, top, plot = 110, 960, y + 40, 120
    for k, v in enumerate(["100 s", "75 s", "50 s", "25 s", "0 s"]):
        yy = top + k * plot / 4
        d.line(L, yy, R, yy)
        d.text(L - 52, yy - 8, 46, 16, v, size=9, color=MUTED, align="right")
    normal = [(130, 1), (190, 2), (260, 1), (330, 3), (400, 1), (470, 2), (540, 1), (600, 1), (640, 2),
              (700, 1), (760, 2), (820, 1), (880, 1), (930, 2)]
    for x, v in normal:
        d.dot(x, top + plot - v * plot / 100 - 2, 3, "normal", 70)
    slow = [(300, 22), (520, 24), (790, 93), (800, 36)]
    for x, v in slow:
        d.dot(x, top + plot - v * plot / 100, 4.5, "slow")
    lanes = [("PC", "pc", [(298, 100), (302, 100), (518, 100), (150, 25), (420, 25), (680, 25), (870, 25), (788, 100)]),
             ("Network", "network", []),
             ("Server", "server", [(610, 25)])]
    for j, (label, kind, ticks) in enumerate(lanes):
        ly = top + plot + 14 + j * 20
        d.text(L - 70, ly - 2, 64, 16, label, size=10, color=MUTED, align="right")
        d.line(L, ly + 6, R, ly + 6)
        for x, op in ticks:
            d.bar(x - 2, ly, 4, 12, kind, op)
    labels = ["Oct 25, 08:00 AM", "Oct 26, 02:40 AM", "Oct 26, 09:20 PM", "Oct 27, 04:00 PM",
              "Oct 28, 10:40 AM", "Oct 28, 06:00 PM"]
    for i, lab in enumerate(labels):
        cx = L + i * (R - L) / (len(labels) - 1)
        x0, al = (cx, "left") if i == 0 else (cx - 120, "right") if i == len(labels) - 1 else (cx - 60, "center")
        d.text(x0, y + 232, 120, 16, lab, size=9, color=MUTED, align=al)
    lx = 36
    for kind, lab in (("slow", "Slow action"), ("normal", "Normal action"), ("pc", "PC problem"),
                      ("network", "Network problem"), ("server", "Server problem")):
        d.bar(lx, y + 254, 10, 10, kind)
        d.text(lx + 14, y + 250, 110, 18, lab, size=10, color=MUTED)
        lx += 125
    d.text(lx, y + 250, 220, 18, "Faded mark = not near a slow action", size=10, color=MUTED)
    d.num(990 - 12, y + 4, 5)

    # problems
    y = 1256
    d.box(20, y, 960, 160, "", "panel")
    d.text(36, y + 8, 300, 22, "Problems found", size=13, bold=True)
    ty = d.table(36, y + 36, [120, 80, 520, 200], [
        ["At slow actions", "Where", "Problem", "When"],
        ["<b>3 of 4</b>", ("pc", "PC"), "Outlook was not responding on LAPTOP-01 (up to 41 s)", "Oct 27, 08:40 AM – Oct 28, 11:55 AM"],
        ["<b>2 of 4</b>", ("pc", "PC"), "LAPTOP-01's disk could not keep up (queue 6.2, limit 2)", "Oct 27, 08:41 AM – Oct 28, 11:50 AM"],
    ])
    d.text(36, ty + 10, 400, 24, "Show 9 problem(s) at other times ▸", size=12, bold=True, color="#2563eb")
    d.num(990 - 12, y + 4, 6)

    # details
    y = 1432
    d.text(20, y, 300, 24, "Details for IT ▸", size=12, bold=True, color="#2563eb")
    d.box(20, y + 30, 960, 110, "", "panel")
    d.text(36, y + 38, 920, 104,
           "Slow = Aternity marked it Major.<br>A problem counts for a slow action recorded up to 5 min before or after it. "
           "A crash, hang or health event of one application counts only for that application.<br>"
           "Aternity UXI: avg 7.9, lowest 4.1.<br><br>"
           "<b>LAPTOP-01</b> &nbsp;Not Mapped · Windows 11 &nbsp;&nbsp;|&nbsp;&nbsp; NetIM object checked: none on this PC's path.",
           size=11, valign="top")
    d.num(990 - 12, y + 34, 7)

    d.notes(1030, 20, 350, [
        (1, "Fill in a user name or a device (or both), the time range and, if you like, one application. "
            "'Match within' = how many minutes before or after a slow action a problem still counts. "
            "Click Investigate: a progress list shows each step until the result appears."),
        (2, "The answer: the problem seen at the most slow actions. Red edge = Likely (more than half), "
            "amber = Possibly, grey = Cause not found, green = nothing was slow."),
        (3, "Who else is affected? For an app failure: how many other PCs had the same failure, and on which "
            "version. 'Show who' opens the list below (PC, user, version, times, last seen); click again "
            "to close. For a network problem it lists the other slow users and the network point they "
            "share instead."),
        (4, "The main part: each problem at the slow actions, and under it what caused it, from Aternity's own "
            "records: where the app failed (module and version), whether other PCs fail the same way, what was "
            "installed or updated before, and the busiest processes. 'What to do' follows from these."),
        (5, "Every action as a dot (red = slow) and every problem as a mark in its lane. A solid mark is near a "
            "slow action; a faded one is not. Hover any dot or mark for its date, time and value."),
        (6, "Only the problems at slow actions, with 'at N of M'. 'Show N problem(s) at other times' opens the "
            "rest; click again to close."),
        (7, "Closed at first. Opens the rules used, the PC and the NetIM objects checked. Click again to close."),
    ])
    d.save("01-investigate-page", 1440, 1592)


# ------------------------------------------------------------------ 02 start-ups
def startups():
    d = D()
    H = 1140
    d.box(0, 0, 1000, H, "", "page", rounded=0)
    header(d, "Slow start-ups")
    form(d, 66, [("Username", 130, "optional"), ("Device", 130, "optional"), ("Location", 150, "optional"),
                 ("Start (local)", 150, "Oct 27, 06:00 PM"), ("End (local)", 150, "Oct 28, 06:00 PM")],
         "Find slow start-ups")
    d.text(20, 138, 600, 18, "Leave the first three empty to search everyone. Start/End: the last 24 hours by default.",
           size=11, color=MUTED)
    d.num(990 - 12, 70, 1)

    y = 166
    d.box(20, y, 960, 130, "", "panel", extra="arcSize=2;")
    d.text(36, y + 10, 920, 26, "Slowest: a.user on LAPTOP-07, Oct 28, 07:52 AM: Total Boot Duration 212 s",
           size=16, bold=True)
    d.text(36, y + 38, 920, 20, "1,240 start-ups. For each Aternity measurement: the typical time, and the time "
                                "1 in 10 start-ups are slower than.", size=11, color=MUTED)
    for i, (t, typ, p90) in enumerate([("Machine Boot", "38 s", "70 s"), ("User Logon", "34 s", "50 s"),
                                       ("Total Boot Duration", "80 s", "140 s")]):
        d.box(36 + i * 230, y + 64, 220, 48,
              f"<b>{t}</b><br>typical <b>{typ}</b> · 1 in 10 over {p90}", "card", size=11, align="left")
    d.num(990 - 12, y + 4, 2)

    # main causes, the first one opened
    y = 312
    d.box(20, y, 960, 300, "", "panel")
    d.text(36, y + 8, 400, 22, "Main causes", size=13, bold=True)
    d.text(36, y + 34, 920, 34,
           "1. <b>Group Policy</b> <font color='#5d6676'>(User Logon)</font> was longer than normal on <b>23 PC(s)</b>, "
           "31 time(s) in this window; behind 6 of the slowest start-ups. "
           "<font color='#5d6676'>Branch A 21, HQ 2</font>  <b><font color='#2563eb'>Hide who ▾</font></b>",
           size=11, valign="top")
    d.table(56, y + 72, [150, 110, 120, 80, 200, 180], [
        ["PC", "User", "Location", "Times", "Most longer than normal", "Last seen"],
        ["LAPTOP-07", "a.user", "Branch A", "3", "45 s", "Oct 28, 07:52 AM"],
        ["LAPTOP-31", "e.user", "Branch A", "2", "41 s", "Oct 28, 08:10 AM"],
        ["LAPTOP-12", "b.user", "HQ", "2", "38 s", "Oct 28, 08:05 AM"],
    ])
    d.text(56, y + 186, 400, 20, "… and 20 more", size=11, color=MUTED)
    d.text(36, y + 214, 920, 34,
           "2. <b>User profile</b> <font color='#5d6676'>(User Logon)</font> was longer than normal on <b>4 PC(s)</b>, "
           "5 time(s) in this window; behind 2 of the slowest start-ups. "
           "<font color='#5d6676'>HQ 4</font>  <b><font color='#2563eb'>Show who ▸</font></b>",
           size=11, valign="top")
    d.text(36, y + 256, 920, 34,
           "3. <b>Antivirus service</b> <font color='#5d6676'>(Machine Boot)</font> was longer than normal on "
           "<b>2 PC(s)</b>, 2 time(s) in this window; behind 1 of the slowest start-ups. "
           "<font color='#5d6676'>Branch A 1, HQ 1</font>  <b><font color='#2563eb'>Show who ▸</font></b>",
           size=11, valign="top")
    d.num(990 - 12, y + 4, 3)

    # start-ups, the first one opened
    y = 628
    d.box(20, y, 960, 320, "", "panel")
    ty = d.table(36, y + 12, [26, 140, 90, 120, 100, 120, 120, 170], [
        ["", "When", "User", "PC", "Location", "Machine Boot", "User Logon", "Total Boot Duration ▼"],
        ["▾", "Oct 28, 07:52 AM", "a.user", "LAPTOP-07", "Branch A", "<b>96 s</b>", "<b>109 s</b>", "<b>212 s</b>"],
    ])
    d.box(36, ty, 886, 120, "", "page", rounded=0)
    d.text(62, ty + 4, 850, 36, "What took the time: <b>Group Policy</b> 55 s (45 s longer than normal) "
                                "<font color='#5d6676'>User Logon</font> · <b>User profile</b> 20 s (5 s longer than normal) "
                                "<font color='#5d6676'>User Logon</font>", size=11, valign="top")
    d.box(62, ty + 40, 850, 72, "", "panel", rounded=0)
    d.bar(62, ty + 40, 3, 72, "btn")
    d.text(74, ty + 42, 830, 68,
           "<b>Why:</b><br>"
           "◦ Most of the extra time: Group Policy took 55 s, 45 s longer than normal (User Logon).<br>"
           "◦ Changed on LAPTOP-07 in the 14 days before Oct 28: Oct 27 VPN Client Updated 5.1 → 5.2; "
           "Oct 22 Office Updated.<br>"
           "◦ Same cause as main cause 1 above (Group Policy): see there who else is affected.",
           size=11, valign="top")
    ty = d.table(36, ty + 120, [26, 140, 90, 120, 100, 120, 120, 170], [
        ["▸", "Oct 28, 08:05 AM", "b.user", "LAPTOP-12", "HQ", "<b>71 s</b>", "<b>88 s</b>", "<b>165 s</b>"],
        ["▸", "Oct 27, 09:14 PM", "c.user", "LAPTOP-03", "Branch A", "<b>64 s</b>", "<b>52 s</b>", "<b>121 s</b>"],
        ["", "Oct 28, 07:30 AM", "d.user", "LAPTOP-21", "HQ", "–", "<b>47 s</b>", "–"],
    ], head=False)
    d.text(36, ty + 6, 900, 34, "One row per start-up: Aternity's measurements of the same PC within an hour. "
                                "Click a row to see what took the time and why.", size=11, color=MUTED)
    d.num(990 - 12, y + 4, 4)

    y = 964
    d.box(20, y, 960, 160, "", "panel")
    d.text(36, y + 8, 300, 22, "By location", size=13, bold=True)
    d.table(36, y + 36, [220, 120, 180, 180, 220], [
        ["Location", "Start-ups", "Typical Machine Boot", "Typical User Logon", "Typical Total Boot Duration"],
        ["Branch A", "84", "52 s", "41 s", "104 s"],
        ["Not Mapped", "910", "39 s", "35 s", "82 s"],
        ["HQ", "246", "33 s", "30 s", "70 s"],
    ])
    d.num(990 - 12, y + 4, 5)

    d.notes(1030, 20, 350, [
        (1, "Search everyone, or narrow to a user, a PC or an Aternity location, between Start and End (last 24 "
            "hours by default). Click 'Find slow start-ups': a progress list shows each step until the result "
            "appears."),
        (2, "The slowest start-up in one line, then one card per measurement type, named as Aternity names them: "
            "the typical time and the time 1 in 10 start-ups are slower than."),
        (3, "Main causes: the parts Aternity measured as most 'longer than normal' in the 10 slowest start-ups. "
            "Each line counts every PC where that part was longer than normal in the same Start/End, by location. "
            "'Show who' opens the list of those PCs; 'Hide who' closes it."),
        (4, "One row per start-up, slowest first by the type marked ▼. With one user or PC searched, each value "
            "shows 'usual N s' under it (that PC's 30 days before Start). Rows with ▸ open what took the time "
            "and, for the 10 slowest, why: the slowest part, what changed on the PC in the 14 days before, and "
            "which main cause it belongs to. Click again to close."),
        (5, "Shown when there is more than one location: the typical time of each type per location, slowest "
            "first."),
    ])
    d.save("02-slow-startups-page", 1440, H)


# ------------------------------------------------------------------ 03 matching rule
def matching():
    d = D()
    d.box(0, 0, 1100, 410, "", "page", rounded=0)
    d.text(20, 10, 1060, 30, "How a problem is matched to a slow action (Match within = 5 min)", size=16, bold=True)
    # the slow action and its window
    d.box(40, 60, 1020, 60, "", "panel")
    d.line(80, 90, 1020, 90, color="#5d6676", width=2)
    d.bar(400, 66, 300, 48, "slow", 15)
    d.dot(550, 90, 7, "slow")
    d.text(470, 64, 160, 18, "slow action 10:42, Outlook", size=11, align="center", bold=True)
    d.text(400, 96, 120, 18, "10:37", size=10, color=MUTED)
    d.text(640, 96, 60, 18, "10:47", size=10, color=MUTED, align="right")
    d.text(80, 64, 200, 18, "time →", size=10, color=MUTED)
    rows = [
        ("pc", "PC", "10:40  Outlook not responding", "ok", "Counts: same app, inside the window"),
        ("pc", "PC", "10:41  Notepad crashed", "no", "Doesn't count: another application"),
        ("pc", "PC", "10:44  CPU 95% (limit 85)", "ok", "Counts: CPU slows every app"),
        ("network", "Network", "10:45  Branch router Gi0/1 utilization 97%", "ok", "Counts: inside the window"),
        ("pc", "PC", "11:30  Disk queue 6 (limit 2)", "no", "Doesn't count: 48 min later"),
    ]
    y = 140
    for kind, label, what, verdict, why in rows:
        d.pill(40, y + 6, kind, label)
        d.box(116, y, 380, 32, what, "panel", size=12, align="left")
        d.line(496, y + 16, 556, y + 16, color="#5d6676", width=1.5, arrow=True)
        d.box(560, y, 340, 32, why, verdict, size=12, align="left", color="#1b5e20" if verdict == "ok" else "#b71c1c")
        y += 44
    d.box(920, 140, 160, 208, "Each problem gets a count: <b>at N of M slow actions</b>.<br><br>The one at the most "
                              "slow actions is the answer: <b>Likely</b> if more than half, <b>Possibly</b> if fewer.",
          "note", size=11, align="left", valign="top", extra="spacingTop=8;")
    d.text(40, 362, 1040, 40, "A crash, hang or health event of one application counts only for that application's "
                              "slow actions. CPU, memory, disk, network and server problems count for every app. "
                              "Values are examples.", size=11, color=MUTED, valign="top")
    d.save("03-matching-rule", 1100, 410)


investigate()
startups()
matching()
