"""Writes the logic drawings into this folder, one per tab, all in one style: every rule behind the
tab, top to bottom (when it reads, what it reads, what it works out, its safety rules, what the page
shows, what a click does). The numbers come from the code's own constants (src/uxcorrelate), so the
drawings are made again when one changes. No host, PC or user names.

    docker run --rm --user 1000:1000 --entrypoint python -v "$PWD":/app -w /app uxcorrelate-runtime:4 \\
        docs/diagrams/make_diagrams.py
    then each .drawio to .png and .pdf with the draw.io image (rlespinasse/drawio-desktop-headless -x -f png).

01-today-tab-logic, 02-care-list-logic, 03-investigate-logic, 04-start-ups-logic,
05-software-tab-logic, 06-admin-page-logic, 07-inventory-tab-logic, 08-every-tab-logic (Ask anything,
Explain and the top bar: what every tab shares).
The export that works on the dev host (draw.io's Electron needs both options there):
    docker run --rm --shm-size=1g --security-opt seccomp=unconfined -u $(id -u):$(id -g) -e HOME=/tmp \
        -v "$PWD/docs/diagrams":/data -w /data rlespinasse/drawio-desktop-headless:v1.74.0 \
        -x -f png -o NAME.png NAME.drawio        (and: -x -f pdf --crop -o NAME.pdf NAME.drawio)
"""
import html
import math
import re
import sys
from pathlib import Path

OUT = Path(__file__).parent
sys.path.insert(0, str(OUT.parent.parent / "src"))
from uxcorrelate import (actions, after_start, agent, assess, care, cause, chat, correlate, impact, logons,  # noqa: E402
                         names, normal, secureview, status, usage, web)
from uxcorrelate import inventory as I  # noqa: E402, E741
from uxcorrelate import software as S  # noqa: E402
from uxcorrelate import today as T  # noqa: E402
from uxcorrelate.config import REFRESH_RANGE, SOFTWARE_USER_SCOPE as S_USER, TILE_SEVERITIES, TOP_STATUS  # noqa: E402

# the same kind of part, the same colour in every drawing (fill, line)
C = {"when": ("#eef1f5", "#8a94a6"), "source": ("#e3f0fb", "#1e6fb8"), "step": ("#ffffff", "#5d6676"),
     "safe": ("#fdecea", "#c62828"), "result": ("#fff4d6", "#b7791f"), "page": ("#e7f5ea", "#2e7d32"),
     "click": ("#f1eafa", "#6b3fa0"), "band": ("#f8f9fb", "#dde1e7")}
LEGEND = (("Timing", "when"), ("Read", "source"), ("Worked out", "step"), ("Safety rule", "safe"),
          ("Result", "result"), ("On the page", "page"), ("By a click", "click"))
TEXT, MUTED = "#1d2330", "#5d6676"
W, LEFT, RIGHT, GAP = 1660, 190, 1630, 18
SIZE = 12
SEV = " or ".join(TILE_SEVERITIES)


def mins(td) -> int:
    return int(td.total_seconds() // 60)


class D:
    def __init__(self):
        self.cells, self.n = [], 2

    def id(self):
        self.n += 1
        return f"c{self.n}"

    def box(self, x, y, w, h, text, kind, size=SIZE, align="left", valign="top", color=TEXT, extra=""):
        fill, line = C[kind]
        st = (f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={line};fontSize={size};"
              f"fontColor={color};align={align};verticalAlign={valign};arcSize=4;spacingLeft=8;spacingRight=8;"
              f"spacingTop=6;spacingBottom=6;{extra}")
        self.cells.append(f'<mxCell id="{self.id()}" value="{html.escape(text)}" style="{st}" vertex="1" parent="1">'
                          f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')

    def text(self, x, y, w, h, text, size=SIZE, color=TEXT, align="left", valign="middle"):
        self.cells.append(f'<mxCell id="{self.id()}" value="{html.escape(text)}" style="text;html=1;fontSize={size};'
                          f'fontColor={color};align={align};verticalAlign={valign};whiteSpace=wrap;spacing=2;" '
                          f'vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')

    def arrow(self, x, y1, y2):
        st = "endArrow=block;endFill=1;html=1;strokeColor=#5d6676;strokeWidth=2;"
        self.cells.append(f'<mxCell id="{self.id()}" value="" style="{st}" edge="1" parent="1"><mxGeometry relative="1" '
                          f'as="geometry"><mxPoint x="{x}" y="{y1}" as="sourcePoint"/><mxPoint x="{x}" y="{y2}" '
                          f'as="targetPoint"/></mxGeometry></mxCell>')

    def save(self, name, w, h):
        xml = (f'<mxfile host="drawio"><diagram name="{name}" id="{name}"><mxGraphModel dx="{w}" dy="{h}" grid="0" '
               f'gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
               f'pageWidth="{w}" pageHeight="{h}" math="0" shadow="0" background="#ffffff"><root><mxCell id="0"/>'
               f'<mxCell id="1" parent="0"/>' + "".join(self.cells) + "</root></mxGraphModel></diagram></mxfile>")
        (OUT / f"{name}.drawio").write_text(xml)


def item(title: str, *lines: str) -> str:
    return f"<b>{title}</b>" + "".join(f"<br>{x}" for x in lines)


def height(text: str, w: int) -> int:
    """Tall enough for the text at SIZE in a box w wide (a line per <br>, wrapped)."""
    per = (w - 20) / 6.0  # characters a line
    lines = sum(max(1, math.ceil(len(re.sub(r"<[^>]+>", "", part)) / per)) for part in text.split("<br>"))
    return int(lines * 15 + 24)


def band(d: D, y: int, label: str, sub: str, kind: str, rows: list[list[str]]) -> int:
    """A band: its name at the left, rows of boxes of one kind (each row's boxes the same height)."""
    tops, yy = [], y + 14
    for row in rows:
        w = (RIGHT - LEFT - GAP * (len(row) - 1)) // len(row)
        h = max(height(t, w) for t in row)
        tops.append((yy, w, h, row))
        yy += h + GAP
    bottom = yy - GAP + 14
    d.box(20, y, W - 40, bottom - y, "", "band", extra="arcSize=2;")
    d.text(34, y + 10, LEFT - 54, 24, f"<b>{label}</b>", size=14, valign="top")
    d.text(34, y + 34, LEFT - 54, max(bottom - y - 40, 20), sub, size=11, color=MUTED, valign="top")
    for yy, w, h, row in tops:
        for i, t in enumerate(row):
            d.box(LEFT + i * (w + GAP), yy, w, h, t, kind)
    return bottom


def drawing(name: str, title: str, sub: str, bands: list, foot: str = "") -> None:
    """bands: (label, sub, kind, rows); an arrow leads from each band to the next, except into a
    band of safety rules (they guard the band above them)."""
    d = D()
    d.text(20, 14, 860, 34, f"<b>{title}</b>", size=20)
    d.text(20, 46, 1100, 22, sub, size=12, color=MUTED)
    lx = 1000
    for label, kind in LEGEND:
        w = 22 + int(len(label) * 6.8)
        d.box(lx, 22, w, 24, label, kind, size=11, align="center", valign="middle", extra="spacingTop=0;spacingBottom=0;")
        lx += w + 8
    y = 84
    for i, (label, s, kind, rows) in enumerate(bands):
        if i:
            if kind == "safe":
                y += 8
            else:
                d.arrow(W // 2, y, y + 26)
                y += 26
        y = band(d, y, label, s, kind, rows)
    d.text(20, y + 8, W - 40, 22, (foot + " " if foot else "") + "The numbers are the code's own settings on the day the "
           "drawing was made. Names of PCs and users are never in a saved count unless said.", size=11, color=MUTED)
    d.save(name, W, y + 44)
    print(name, W, y + 44)


# ------------------------------------------------------------------------------------ 01 Today
def today():
    drawing("01-today-tab-logic", "Today tab: every rule behind it",
            "What Aternity itself rated today, against the same kind of day before. Read top to bottom.", [
        ("1. When", "One background reader for the whole app", "when", [[
            item("Every few minutes", f"The reader reads Aternity every {T.REFRESH_MIN} minutes unless config.yaml sets "
                 f"another pace ({REFRESH_RANGE[0]} to {REFRESH_RANGE[1]}). A read starts that long after the one before "
                 "started, however long a read takes. The names list and the Care list go with it."),
            item("Which hours", f"At start: the last {T.START_HOURS} hours, an hour at a time. Then the current hour at "
                 f"each read, and each hour once more {T.LATE_MIN} minutes after it ends, because Aternity sends rows "
                 "late."),
            item("Health events", f"Today's at each read. The {T.HEALTH_DAYS} days before: once a day."),
            item("What is kept", f"Each finished day's counts are saved for {T.KEEP_FILES_DAYS} days (counts and "
                 "application names; no user, no PC), because Aternity keeps activity only about 8 days. Assessments: "
                 f"{assess.KEEP_DAYS} days."),
        ]]),
        ("2. Read", "From Aternity only", "source", [[
            item("Measured actions", "Each action a user did in an application: the application, the activity, how "
                 f"long, Aternity's own rating (the worst is {TOP_STATUS}), the PC, the user, virtual or physical."),
            item("Health events", f"Events Aternity recorded on a PC, with its severity ({SEV} make tiles), the "
                 "application, the failing part and how many times."),
            item("Start-ups", "Boots and logons with their time, per Aternity type. Today's again every "
                 f"{mins(assess.BOOTS_EVERY)} minutes, each day before once."),
            item("AI service", "Only to word the assessment and answers. It is given counts and names our code "
                 "counted; it reads nothing itself."),
        ]]),
        ("3. Worked out", "Counts only, A to F", "step", [[
            item("A. Active users and PCs", "A user or PC with at least one measured action, any rating, each once. "
                 "PCs split virtual / physical by Aternity's own mark. Shown for today, and for the last full hour "
                 "with this one so far."),
            item("B. Event tiles", f"One tile per kind of {SEV} event today: the PCs it hit, each once. Other events: "
                 f"the {T.TOP} on the most PCs, in a table."),
            item("C. Slow actions", f"Actions Aternity rated {TOP_STATUS}: per hour, and per application (top "
                 f"{T.TOP})."),
        ], [
            item("D. The usual", "Each count is set against the same kind of day before (working Monday to Friday, "
                 f"or weekend): the latest {T.USUAL_DAYS} such days, up to the same time of day. With {T.MIN_USUAL} or "
                 "more of them it says \"more than any of them\", \"fewer than any\" or \"as usual\". Only days read "
                 "in full count."),
            item("E. Three small charts", "One point per hour of today, with a band from the lowest to the highest of "
                 f"the same hour on those days: the share of actions rated {TOP_STATUS} (needs {assess.MIN_ACTIONS} "
                 "actions in the hour), PCs with a crash so far, and the typical logon time (needs "
                 f"{assess.MIN_STARTS} start-ups)."),
            item("F. The assessment", f"At {', '.join(f'{h:02d}:00' for h in assess.HOURS)}. Our code counts the facts; "
                 f"the verdict and what to look at first follow fixed rules (a verdict needs {assess.VERDICT_PCS} PCs). "
                 f"Reach: isolated under {assess.ISOLATED} PCs, widespread from {assess.WIDESPREAD}, limited between. "
                 "The AI words it under Changed / Watch / Earlier / Start-ups."),
        ]]),
        ("Safety rules", "They guard the counts above", "safe", [[
            item("A failed read is never \"nothing\"", "A read that failed or was cut short is counted as failed: the "
                 "\"last good read\" time does not move, and the tab says which hours are missing."),
            item("A day not read in full", "It is left out of the usual, and on the week chart it is a gap, never "
                 "0. It had counted as a quiet day."),
            item("The AI's words are checked", "A number or a fixed word (isolated, widespread, above its usual "
                 "range) that our lines do not give: the AI's text is refused and our own text is shown."),
            item("The reader never stops", "An error the code did not expect is logged and the reader goes on to "
                 "the next read."),
        ]]),
        ("4. On the page", "The Today tab, top to bottom", "page", [[
            item("Assessment box", "The verdict in its colour, the words, \"Written at HH:MM\". Beside it: "
                 "Yesterday in short (written once) and the week, Monday to Sunday, this week and last: PCs a day "
                 f"with a Critical event, a Major event, and {TOP_STATUS} slow actions."),
            item("Active strip and charts", "Active users and PCs now and today. Then the three small charts from "
                 "E."),
            item("Tiles and tables", f"The {SEV} event tiles with their usual; other crashes; slow actions per "
                 "hour and per application."),
            item("Ask anything", "The one chat, at the bottom of every tab. It is told which tab is open and what "
                 "it shows."),
        ]]),
        ("5. By a click", "Names of PCs and users appear only here", "click", [[
            item("Who is behind a tile or row", f"The users and PCs behind it, most first ({T.WHO} at most), from "
                 "rows the reader already holds: no extra read of Aternity."),
            item("Investigate", "Runs an investigation of that user and PC: from "
                 f"{mins(T.SLOW_PAD)} minutes before their first {TOP_STATUS} action to {mins(T.SLOW_PAD)} after the "
                 f"last, or {mins(T.EVENT_PAD)} minutes around an event."),
            item("Explain", "A panel's own lines and our meaning of each term go to the AI, which words them. A "
                 "number not in the lines: our own words instead."),
            item("Links", "#today opens this tab. Each tab opens where it was left."),
        ]]),
    ])


# ------------------------------------------------------------------------------------ 02 Care list
def care_list():
    drawing("02-care-list-logic", "Care list tab: every rule behind it",
            "A short list of people watched one by one: is something wrong for them right now? Read top to bottom.", [
        ("1. When", "After each read of the Today reader", "when", [[
            item("Every read", f"Each user on the list (at most {care.CARE_MAX}) is checked after every read of the "
                 "Today reader, from rows it already holds. The list file is read again at every check."),
            item("Once a day", f"Each user's own {care.HISTORY_DAYS} days of activity (for their usual) and their "
                 f"{care.BOOT_DAYS} days of start-ups are read once a day."),
            item("Investigations", "One at a time, at most one per user in "
                 f"{mins(care.RUN_EVERY)} minutes, queued by the tab itself."),
            item("What is kept", f"Each user's issues and latest investigation, {care.KEEP_DAYS} days (usernames "
                 "on disk, agreed). The list itself is a file changed from the page."),
        ]]),
        ("2. Read", "From Aternity, and names from AD", "source", [[
            item("Their actions today", f"From the Today reader: every action rated {TOP_STATUS}, with its time, "
                 "application, activity and PC."),
            item("Their health events", f"Events of severity {SEV} recorded with their username today, and "
                 "whether one is a failing disk."),
            item("Their start-ups", f"Today's, one read per check for the whole list ({care.BOOT_BATCH} usernames a "
                 "read). Everyone's start-ups today come from the Today tab's read."),
            item("Active Directory", "Each user's name, department and whether the account is disabled; asked "
                 "after the tiles are drawn."),
        ]]),
        ("3. Worked out", "A to F", "step", [[
            item("A. What is an issue", f"1. A {SEV} event with their username today.",
                 f"2. More {TOP_STATUS} actions by this time of day than on any of their own days before (their "
                 f"latest {T.USUAL_DAYS} days with a PC in use, any day of the week; {T.MIN_USUAL} needed). Each action "
                 "is judged by its own time of day.",
                 f"3. A slow start-up: longer than every one of theirs in {care.BOOT_DAYS} days ({care.BOOT_MIN} "
                 "needed) and longer than 1 in 10 of everyone's today."),
            item("B. Spells", "Slow actions are one issue while they go on. The spell is over after "
                 f"{mins(care.QUIET)} minutes with no {TOP_STATUS} action, or when the day is back within their "
                 "usual. A later one is a new issue. Events of one kind are grouped the same way."),
            item("C. The alert light", "A tile lights while a read brought more of an issue than was seen before, "
                 f"whatever the rows' own time. It goes off at the {care.ALERT_READS}rd read after that. A failing "
                 "disk stays lit all day. A restart does not light old issues again."),
        ], [
            item("D. The investigation", "Queued while one of today's issues is not covered by the latest one. Its "
                 f"window: from {mins(care.INV_PAD)} minutes before their first issue of the day to now."),
            item("E. Order of the tiles", "Lit tiles first (a failing disk, Critical, Major; an event before slow "
                 f"actions), then users not seen in {names.KEEP_DAYS} days, then OK. Inside each group: the list's "
                 "own order, set by \"Arrange tiles\"."),
            item("F. The bars on a tile", f"Their {TOP_STATUS} actions by this time of day on each of the last "
                 f"{care.KEEP_DAYS} days, today last. The days of the usual are dark; a day not read is dashed."),
        ]]),
        ("Safety rules", "They guard the checks above", "safe", [[
            item("No usual, no verdict", "Too few days with a PC in use, or their history cut short: the tile says "
                 "\"not compared\" and why. It is read again at the next check."),
            item("Start-ups not read whole", "A read cut short is no comparison. A start-up Aternity did not "
                 "record is never made up."),
            item("The list file", "A broken file keeps the last list and is never written over. One unreadable "
                 "user file does not stop the others."),
            item("Changes need a PIN", "Add, remove, relabel and arrange need today's date as a PIN, and are "
                 "logged with the caller's address. It keeps a slip out, not someone who knows the rule."),
        ]]),
        ("4. On the page", "One tile per user", "page", [[
            item("A lit tile", "Its issues, the most serious first, each with when Aternity sent it and \"Clears "
                 "at the HH:MM read if no new one\"."),
            item("An OK tile", f"The day's counts against their usual by now, and their latest {TOP_STATUS} "
                 "actions (3 on the tile, all under Show more)."),
            item("Title and AD", "The typed label, else the name from AD; department; \"disabled in AD\" or \"not "
                 "in AD\"."),
            item("Show more", "Today's issues, those that are over, the last issue before today, each start-up "
                 "of today against theirs and everyone's, and the investigation's title."),
        ]]),
        ("5. By a click", "And the feed for another platform", "click", [[
            item("Change the list", "\"+ Add a user\" (by username, or found by name in AD), \"Remove from the Care "
                 "list\", \"Change label\", \"Arrange tiles\" (drag, arrows or keys)."),
            item("Investigate", "\"Investigate again now\" runs one for the same window. The saved one opens "
                 "without a re-run."),
            item("Open in Start-ups", "From a slow start-up: that tab, for that user."),
            item("Status feed", "Another platform asks every 5 minutes: each user alert / ok / unknown, since "
                 f"when, a summary of at most {care.SUMMARY_MAX} characters in our own words. Times in UTC."),
        ]]),
    ])


# ------------------------------------------------------------------------------------ 03 Investigate
def investigate():
    drawing("03-investigate-logic", "Investigate tab: every rule behind it",
            "Why was this user slow, or what went wrong for them? Plain counts, no scores. Read top to bottom.", [
        ("1. When", "Only when asked", "when", [[
            item("How it starts", "The Investigate button, any \"Investigate\" link on the other tabs, Ask "
                 "anything, or the Care list by itself."),
            item("How it runs", "As a job in the background; the page asks for it every second. A finished one "
                 f"is kept {web.KEEP_DONE_MIN} minutes."),
            item("What it covers", "One user (or PC) and a window of time. Up to "
                 f"{correlate.MAX_PCS} of the user's PCs: those with a slow action first, then the most used."),
            item("Nothing is saved", "Except the Care list's own latest investigation per user."),
        ]]),
        ("2. Read", "From Aternity and NetIM", "source", [[
            item("The user's actions", "Each action with its time, application, Aternity's rating, and the PC's "
                 "CPU, memory, disk queue and network round trip at that action."),
            item("What happened on the PC", "Health events, application crashes and hang time, start-ups and "
                 "their parts, the busiest programs (per hour), the software change log (14 days)."),
            item("For comparison", f"The user's own last {normal.BASELINE_DAYS} days (Aternity keeps about 8), "
                 f"other users of the same applications, the same hours on the {impact.DAYS_BACK} days before, other "
                 "PCs with the same failure."),
            item("NetIM", "The network devices and servers behind the PC and the application: their values over "
                 "limits and their alerts."),
        ]]),
        ("3. Worked out", "A to H", "step", [[
            item("A. Which actions were slow", "Aternity's own rating decides. Only when no rating is set to "
                 "count as slow: an action over the response time in config.yaml."),
            item("B. Problems collected", "PC values over their limits, a failing disk, health events, crashes "
                 "and hangs, a start-up with a slow action in it or just after, NetIM values and alerts."),
            item("C. Which problem counts for which slow action", "A problem counts for the slow actions within "
                 "5 minutes of it (bucket_minutes). A PC reading counts only for its own action. A crash or hang "
                 "of an application counts only for that application. Never across PCs."),
            item("D. Why, for each issue", "Where the application failed (part, module, version, error). How "
                 "many other PCs fail the same way, of how many using it, and since when. A software change is a "
                 "possible cause only with the same name or version as the failing part, before it, and with no "
                 "other PC failing on another version."),
        ], [
            item("E. Compared with normal", "Each slow application and activity against the user's own earlier "
                 f"runs ({normal.MIN_HISTORY} needed) and other users at the same time. \"Became slow\" needs "
                 f"{normal.MIN_SLOW} slow runs and most runs slow since."),
            item("F. Other users", "Other users of the same applications in the slow minutes, grouped by the "
                 "network point they sit behind (from NetIM), against the same hours on the days before. "
                 "\"Unusual\": more slow than on any of those days. A common point: an unusual group with more "
                 "than half the slow users."),
            item("G. Right after a start-up", f"A slow action up to {after_start.AFTER_START_MIN} minutes after a "
                 f"start-up, against the same activity on all PCs of its kind in the {after_start.FLEET_HOURS} hours "
                 f"before. \"Slower then\": {after_start.RATIO:g} times, with {after_start.MIN_RUNS} runs on each side."),
            item("H. The headline", "The most serious thing leads: a failing disk, a crash, a hang, then slow "
                 "actions. For slow actions the first that fits: a problem at them; other users slow at once; "
                 "became slow after a change; became slow, no change; right after a start-up; always slow; "
                 "others as usual; nothing found."),
        ]]),
        ("Safety rules", "They guard the answer above", "safe", [[
            item("A failed read is never \"nothing found\"", "It becomes \"not known\", \"at least N\", \"not "
                 "checked\" or \"not read in full\", and is listed under Warnings."),
            item("One source down does not stop it", "A failing NetIM call or an optional Aternity source is a "
                 "warning; the investigation goes on with the rest."),
            item("No guessed cause", "A program is named only when its hourly CPU is at least half the PC's "
                 "reading. Windows' own modules are never blamed. A change logged after the failure is said so."),
            item("Left out is said", "PCs beyond the limit, problems beyond "
                 f"{cause.MAX_EXPLAIN} explained, and anything cut short are named."),
        ]]),
        ("4. On the page", "The answer, then the detail", "page", [[
            item("The answer", "A title in two sentences: what the user felt, then one verdict. Then the reason "
                 "and \"Also slow\" lines."),
            item("Sections", "What the user felt · Other users of these apps · What went wrong (one row per "
                 "issue) · What changed · What to do · What was checked."),
            item("One timeline, one list", "Every slow action and problem on one timeline and in one problems "
                 "list. No second view repeats them."),
            item("Warnings", "Every read that failed or was cut short, in plain words."),
        ]]),
        ("5. By a click", "", "click", [[
            item("Explain", "On every panel: the panel's lines worded by the AI, checked against the lines."),
            item("Open in Start-ups", "From a start-up in the answer: that tab for the same user or PC."),
            item("Ask anything", "Can run one whole investigation per question and read its report (at most "
                 f"{agent.MAX_READS} reads of Aternity per question)."),
            item("Link", "#investigate opens this tab; a result stays while other tabs are used."),
        ]]),
    ])


# ------------------------------------------------------------------------------------ 04 Start-ups
def startups():
    drawing("04-start-ups-logic", "Start-ups tab: every rule behind it",
            "How long boots and logons took, against each PC's own usual, and what took the time. Read top to bottom.", [
        ("1. When", "Only when asked", "when", [[
            item("How it starts", "The Show button for a window of time, for everyone or one user or PC; "
                 "\"Open in Start-ups\" from the Care list, an investigation or Ask anything."),
            item("How it runs", "As a job in the background, like an investigation; kept "
                 f"{web.KEEP_DONE_MIN} minutes when done."),
            item("Nothing is saved", "It is read from Aternity each time."),
        ]]),
        ("2. Read", "From Aternity only", "source", [[
            item("Start-up rows", "Each boot or logon Aternity measured: the PC, the user, the time, how long, "
                 "and Aternity's own type name. No type name is assumed by us."),
            item("Their parts", "What ran during a start-up and how long each part took, with Aternity's own "
                 "measure of how much longer than expected."),
            item("History", f"Each PC's start-ups of the {logons.BASELINE_DAYS} days before, for its own usual."),
            item("Change log", "Software installed, updated or removed on the PC before a start-up became slow."),
        ]]),
        ("3. Worked out", "A to G", "step", [[
            item("A. What is one start-up", "Rows of one PC within an hour, with no type twice, are one "
                 "start-up: one line, a column per type."),
            item("B. Per type, everyone", "The typical time, and the time 1 in 10 are over. That second number "
                 f"is given only from {logons.P90_MIN} measured start-ups; with fewer the slowest is shown."),
            item("C. Against the PC's own usual", "Each start-up against that PC's earlier ones "
                 f"({logons.MIN_HISTORY} needed). There is no \"slow\" threshold of ours."),
            item("D. What took the time", "A part belongs to the latest start-up of its PC at or before it, "
                 f"within an hour. Looked up for the {logons.COMPONENTS_FOR} slowest; the part Aternity found the "
                 "most degraded is named."),
        ], [
            item("E. Main causes", f"A part that was longer than normal on every PC that had it (up to "
                 f"{logons.MAIN_CAUSES}): not one PC's problem."),
            item("F. Since when", "The earliest point from which every start-up of a type took longer than 9 "
                 f"in 10 of those before it ({logons.MIN_HISTORY} earlier needed), and the software changes just "
                 f"before. Looked for on the {logons.WHY_FOR} slowest."),
            item("G. Virtual desktops", "They are pooled: a user's start-ups on their virtual desktops are "
                 "compared with each other, not with one desktop's history of many users."),
        ]]),
        ("Safety rules", "", "safe", [[
            item("Nothing read is not \"no start-ups\"", "When the read failed, the tab shows the warnings."),
            item("No unit is promised", "The time unit is read from live values as seconds; it is not "
                 "documented by Riverbed."),
            item("A calm start-up is not a problem", "In an investigation, a start-up with no slow part that is "
                 "normal for its PC is only mentioned, never an issue."),
        ]]),
        ("4. On the page", "", "page", [[
            item("The list", "Each start-up: when, PC, user, each type's time, against the PC's usual, and what "
                 "took the time."),
            item("Per type and main causes", "The typical time and \"1 in 10 over\" per type; the main causes "
                 "across PCs."),
            item("By location", "The same start-ups grouped by the location Aternity gives each PC."),
        ]]),
        ("5. By a click", "", "click", [[
            item("A start-up's why", "Its parts, the PC's history, and the changes before it."),
            item("Investigate", "Runs an investigation of that user and PC around the start-up."),
            item("Explain", "On every panel, as on the other tabs. Link: #startups."),
        ]]),
    ])


# ------------------------------------------------------------------------------------ 05 Software
def software():
    K = S.KIND
    drawing("05-software-tab-logic", "Software tab: every rule behind it",
            "What is installed, on which versions, and where a version has more problems. Read top to bottom.", [
        ("1. When", "One background reader", "when", [[
            item("Once a day, and at start", f"The whole read runs every {S.READ_EVERY_H} hours and when the app starts. It "
                 "takes a few minutes. The page shows the last read until the new one is done."),
            item("When Aternity does not answer", "Nothing is saved (it would empty the day's flags). The last good read "
                 f"stays on the page and the read is tried again every {S.RETRY_MIN} minutes."),
            item("What is kept", f"Each day's counts and names are saved for {S.KEEP_DAYS} days: programs, versions, flags. "
                 "They give each flag its history and the word \"new\"."),
            item("A reply cut short", "Counts from a reply that was cut short are marked partial on the page. A read "
                 "Aternity refuses as too long is asked once more with its own $fetch_all_rows=yes."),
        ]]),
        ("2. Read", "From Aternity only", "source", [[
            item("Installed software list", "Aternity adds it up for us:", "· PCs per program and version",
                 "· PCs per program", "· PCs with a program installed for one user only",
                 "· the names of the PCs in the list", "· Windows updates on each PC (type \"OS Update\")"),
            item("Device list", "One row per PC Aternity still knows:", "· the PC's name", "· its Windows version",
                 "· when Windows was last updated", "Aternity takes a PC off this list about a month after it last "
                 "heard from it."),
            item(f"App usage ({S.UPDATE_DAYS} days, day by day) and actions ({S.DAYS} days)", "Per application, version and PC:",
                 "· PCs that ran it", "· PCs with a crash", "· PCs with hang time",
                 f"· actions, and those Aternity rated {TOP_STATUS}"),
            item(f"Crash events, last {S.DAYS} days", "Every crash by application, program file and version.",
                 "Used to answer questions about a program Aternity lists under another name (\"Edge\")."),
        ]]),
        ("3. Worked out", "In this order, A to G", "step", [[
            item("A. PCs that are gone are left out", "Uses: installed list + device list.",
                 "A PC in the installed list but not in the device list is gone: Aternity dropped the PC and kept its "
                 f"software. Their software is read ({S.GONE_BATCH} PCs a read) and taken off Aternity's totals: each PC "
                 "once per program and version, per program, per one-user program. A program only those PCs have "
                 "disappears."),
            item("B. 2+ versions on one PC", "Uses: installed list.",
                 "A program with more installs than PCs has a PC counted twice. Each such program (the "
                 f"{S.MULTI_MAX} with the most extra installs) is read PC by PC: PCs with 2 or more versions, and which "
                 "versions go together."),
            item("C. Behind most PCs", "Uses: installed list.",
                 "For each program whose versions are all numbers: the version on the most PCs is the reference. A PC "
                 "on an older version is \"behind\". A PC with 2 versions counts once, and only when every version on "
                 "it is older."),
        ], [
            item("D. Windows updates per Windows version", "Uses: device list + installed list.",
                 "PCs are grouped by Windows version. An update is counted only on its own Windows version's PCs. It is "
                 f"\"missing\" when more than {S.WIN_MOST:.0%} of those PCs have it and at least {S.MIN_PROBLEM} do not. For "
                 "the PCs without it: what they have instead (a later update may replace it)."),
            item("E. Does one version have more problems?", "Uses: app usage (day by day) and actions. Virtual desktops and "
                 "physical PCs are looked at apart.",
                 f"1. Before and after an update: PCs whose version changed once in {S.UPDATE_DAYS} days, for each PC as many "
                 "days after as before; the PCs with a crash (or hang time) after against those before.",
                 f"2. Each version against the version most PCs have ({S.MIN_PCS}+ PCs on each side; a version new this week "
                 "is left to 1).",
                 f"Raised: {S.RATIO:g} times the other side's share, {S.MIN_PROBLEM}+ PCs with the problem, and still so "
                 f"with {S.STILL} more PCs with it on the other side ({S.STILL_ACTIONS} more actions rated Major for "
                 "slow actions).",
                 "The same PCs decide: a version that is worse than another in 2 is raised only when the PCs that ran "
                 f"both versions show it too (when {S.MIN_PROBLEM}+ of them had the problem on one of the two, they must "
                 "pass the same checks). Two versions side by side are different PCs.",
                 "One version that Aternity writes in two ways (3.0.20 and 3.0.20.0) is counted as one version. A move "
                 "to a lower version is a \"change of version\", not an update."),
        ], [
            item("F. Installed for one user only", "Uses: installed list.",
                 "Aternity marks every install with who it is for: the whole PC (\"ALL\") or one person "
                 f"(\"{S_USER}\"). An install for one person sits in that person's own Windows profile, not in "
                 "Program Files: usually something the person installed themselves, with no admin rights, and "
                 "often not rolled out by IT (examples: Zoom, a browser's per-user copy, a chat app).",
                 "Counted: for each program, the PCs that have it installed this way, next to all its PCs.",
                 "Why it is raised: such a copy is outside the usual roll-out and patching, another person on the "
                 "same PC does not have it, and one PC can carry several copies, one per person."),
            item("G. Rare programs", "Uses: installed list.",
                 f"Programs on {S.FEW_PCS} PCs or fewer, grouped by how many PCs have them (1, 2-5, 6-20, ...). "
                 "Windows updates are not counted as programs."),
        ]]),
        ("Safety rules", "They guard the steps above", "safe", [[
            item("When A cannot be done", "Nothing is left out and a note on the page says so, when: the "
                 "device list is not read whole; the gone PCs' software is not read whole; or more than "
                 f"{S.GONE_MOST:.0%} of the PCs are missing from the device list (then the two lists do not match)."),
            item("Cut-short Windows updates", "PCs past the cut would look as if they had no update. The "
                 "Windows flag then says its counts are partial."),
            item("An app whose every PC failed", "It has no other version to compare with, so it is never "
                 "raised. It is shown, with the PCs that failed, but not as a red flag."),
        ]]),
        ("4. Red flags", "One line per kind, most PCs first", "result", [[
            item(K["crash"], "From E. One line per raised version:", "\"it crashed on N% of its PCs (n of m); on the version most have: ...\", with the counts of the PCs that ran both versions, or \"more crashes after the update\"."),
            item(K["hang"], "From E. One line per raised version:", "The same, for PCs where it stopped responding."),
            item(K["slow"], "From E. One line per raised version:", f"\"N% of its actions were rated {TOP_STATUS} (n of m, on P PCs)\"."),
            item(K["windows"], "From D. One line: how many updates are missing somewhere, and the one missing on the "
                 "most PCs."),
        ], [
            item(K["multi"], "From B. One line: how many programs, PCs in all, and the program with the most."),
            item(K["behind"], f"From C. Programs with {S.MIN_PROBLEM}+ PCs behind. One line, naming the one with the most."),
            item(K["user"], "From F. One line: how many programs are installed that way, how many installs, and the "
                 "program on the most PCs."),
            item(K["few"], "From G. One line: how many of the programs are that rare."),
        ], [
            item("Ranked", "By PCs, most first. Equal: crashes, stops responding, slower, then Windows, 2+ versions, "
                 "behind, one user, rare."),
            item("New", "A flag is \"new\" when the latest day before that had any flag did not have it. A day saved "
                 "empty does not make every flag new."),
            item("Bars", "Each flag's PCs per day, from the saved days."),
            item("Never a flag", "A count our code worked out as a share or ratio; an application split by another "
                 "program's version (it can differ by chance)."),
        ]]),
        ("5. On the page", "The Software tab", "page", [[
            item("Tiles", "PCs in the list (and how many more were left out as gone), programs, installs (and those "
                 "for one user), red flags."),
            item("Red flags", "The list from 4. A click on a line opens its section or the PCs behind it."),
            item("Summary for management", "The AI words up to 5 sentences from the top flags. Every number must be one "
                 "of the flags' own; otherwise our own sentence is shown."),
        ], [
            item("Application health by version", "Virtual desktops and physical PCs by a switch. Per application: "
                 "version by version first, then before and after each update. A ▲ sits on the count it is about, "
                 "with what it is against; no box of sentences above the tables. A version not raised because of "
                 "the PCs that ran both says so on its count. A table with a percentage beside each count."),
            item("Other sections", "Compare versions · Behind most PCs · 2+ versions on one PC · Installed for one "
                 "user · Windows · rare programs. Every table has a percentage beside each count."),
        ]]),
        ("6. By a click", "Read now. Names of PCs and users appear only here", "click", [[
            item("PCs with a program", "The PCs with a program, or one version of it, each with its versions. PCs that "
                 "are gone are left out, as in the counts."),
            item("PCs with a problem", f"The PCs with a crash, hang time or {TOP_STATUS} actions on one version in the "
                 f"last {S.DAYS} days: user, days, the last time. \"Investigate\" runs an investigation. After an update: "
                 "the PCs of that change of version only. When the list counts other than the tab's daily read "
                 "did, it says both."),
            item("Compare and before / after", f"Two versions side by side on every kind of issue. Or the same PCs "
                 f"before and after their update ({S.UPDATE_DAYS} days read). Both count as the tab's own tables do: "
                 "the kind of PC on screen, for each PC as many days after as before."),
            item("PCs without a Windows update", "The PCs of that Windows version without the update, with what each "
                 "has instead. Same names as the daily read, so the total matches the flag."),
        ]]),
    ], foot="Ask anything uses the same look-ups for questions about software.")


# ------------------------------------------------------------------------------------ 06 Admin page
def admin():
    drawing("06-admin-page-logic", "Admin page: every rule behind it",
            "Three tabs behind a login: Statistics (how the app is used and what it costs Aternity), SecureView, Actions.", [
        ("1. When and who", "/admin, no link from the main page", "when", [[
            item("Login", f"Its own password, over HTTPS only. A login lasts {web.ADMIN_HOURS} hours; a restart logs "
                 f"out. {web.ADMIN_TRIES} wrong passwords from one address lock it for {web.ADMIN_LOCK_MIN} minutes."),
            item("Refresh", "Only the open tab is read, every minute. Actions is read only when asked."),
            item("What is kept", f"Usage: one file a day, {usage.KEEP_DAYS} days. SecureView: {secureview.KEEP_DAYS} "
                 "days. Actions: nothing."),
            item("Counting", "Every call the app makes to Aternity, NetIM, the AI services and AD is counted as "
                 "it happens, under what the app was doing."),
        ]]),
        ("2. Read", "", "source", [[
            item("Visits", "The main page reports each tab a visitor opens, with the visitor's address. No "
                 "login there, so no name."),
            item("Aternity, by address", "The Today reader keeps which user had actions from which address, to "
                 "put a name to a visit."),
            item("Aternity, the device people sit at", "For SecureView: each action's device name, when it "
                 "matches the pattern in config.yaml. Before its own records: Aternity's hourly, then daily "
                 "data."),
            item("Active Directory", "Read only: a user's name and department, a desktop's unit; a person "
                 "found by name."),
        ]]),
        ("3. Worked out", "A to F", "step", [[
            item("A. Who visited", "The user nearest the visit from that address, within "
                 f"{mins(usage.MATCH)} minutes. Several at once: \"one of N\". None yet: \"waiting for Aternity\", "
                 f"then \"not known\" after {mins(usage.GIVE_UP) // 60} hours."),
            item("B. Usage", "Per day: people, tabs opened, investigations by where they were started, "
                 "questions asked and look-ups that failed."),
            item("C. Load", "Per system and per purpose: calls, rows, waiting time, retries, failed, cut "
                 "short. An answer the app expects (an AI service with no model list) is not a failure."),
        ], [
            item("D. SecureView", "Per device and day: first and last heard, hours, people, desktops. A day "
                 "is complete when every hour was read after it ended. Today against the usual by now ("
                 f"{T.USUAL_DAYS} complete days of the same kind). History: silent 30 / 60 / 90 days."),
            item("E. SecureView search", f"Every day kept, by part of a username, device or desktop "
                 f"({secureview.SEARCH_MIN}+ characters; {secureview.SEARCH_MAX} matches of each). Nothing "
                 "found: one search in AD by a person's real name."),
            item("F. Actions", "Any user's actions of today, one read from local midnight to now, at most "
                 f"{actions.ROWS_MAX:,} rows, latest first."),
        ]]),
        ("Safety rules", "", "safe", [[
            item("Never a guessed name", f"When {usage.ONE_ADDRESS} or more visits all come from one address (a "
                 "proxy), the page says people cannot be told apart."),
            item("Aternity's times never move ours", "What SecureView saw itself keeps its minute; Aternity's "
                 "hourly data only widens a day (\"08:xx\"), its daily data gives no time (\"day only\")."),
            item("Cut short is said", "Actions cut at its limit, a reply cut short, a day not read: each is "
                 "said, never \"no action\"."),
            item("AD", "A refused login is never tried again in that run (each try counts towards a "
                 "lockout). Not answering: a pause before the next try."),
        ]]),
        ("4. On the page", "Three folder tabs", "page", [[
            item("STATISTICS", "People using the app now and per day, what they open, investigations, "
                 "questions, and the Load panel per system with its hours."),
            item("SECUREVIEW", "Today (devices heard, by hour, by department), History (every device, silent "
                 "ones), Search."),
            item("ACTIONS", "One user's actions today: time to the second, application, activity, response "
                 "time, rating, desktop, device sat at."),
        ]]),
        ("5. By a click", "", "click", [[
            item("A department", "Lists its devices; again for every department."),
            item("A username", "Opens Search with it; \"All actions today\" opens Actions for that person."),
            item("CSV", "Search results and Actions can be saved as a file."),
        ]]),
    ])


# ------------------------------------------------------------------------------------ 07 Inventory
def inventory():
    lines = len(I.LINES)
    drawing("07-inventory-tab-logic", "Inventory tab: every rule behind it",
            "What Aternity knows about each PC, the PCs that need a look, and how many PCs are in use. Read top to bottom.", [
        ("1. When", "One background reader", "when", [[
            item("The device list", f"Read every {I.READ_EVERY_MIN} minutes. When Aternity does not answer: again after "
                 f"{I.RETRY_MIN} minutes, and the last read stays on the page. Which columns Aternity publishes is asked "
                 f"again every {I.SCHEMA_HOURS} hours."),
            item("PCs in use", "After each read of the list: the finished days not kept yet, a day at a time. A day is "
                 f"read again until it is {I.SETTLE_DAYS} days old, then kept for good. The first read takes every day "
                 "Aternity keeps (about 90 small reads, once)."),
            item("What is held in memory only", "The device list itself: PC names, last users, serial numbers. "
                 "Nothing of it is written to disk."),
            item("What is kept", f"Only the usage charts' counts, per kind of device: days {I.KEEP_DAYS} days, months "
                 f"{I.KEEP_MONTHS} months. No PC or user name."),
        ]]),
        ("2. Read", "From Aternity only", "source", [[
            item("Device list", "One row per PC, its latest: every column Aternity publishes of those the tab can "
                 "show (kind of device, model, serial number, memory, system disk, last start-up, agent version and "
                 "status, stability index, last user, department, location, when it was last heard from)."),
            item(f"App usage, last {I.USE_DAYS} days", "Which PCs ran an application, each PC once. For \"not used\"."),
            item("App usage, day by day", "For each finished day: the PCs with a row of that day (a row counts only "
                 "when its own time is of that day). Aternity has no row for today until the day is over."),
            item("App usage, a month", "One read asking for the PC only, so Aternity adds the month's days up: a row "
                 "per PC used at least once. Only while Aternity keeps the month whole."),
        ]]),
        ("3. Worked out", "A to F", "step", [[
            item("A. Tiles", "PCs in the list, by Aternity's own kind of device. Heard from in the last 7 days. "
                 "Retired (the Software tab's count: software still in Aternity, PC no longer in its list). Things "
                 "to look at."),
            item(f"B. Needs a look: {lines} lines", "An agent older than the version most PCs have · not used in "
                 f"{I.USE_DAYS} days · not restarted in over {I.RESTART_DAYS} days · not heard from in {I.SILENT_DAYS} "
                 f"days or more · agent stopped · stability index under {I.STABILITY_LOW} · little free space "
                 f"(Aternity's bands that end at {I.FREE_LOW_GB} GB or under) · a spinning hard disk.",
                 "Each line: its PCs, and the PCs it is counted among (a count with its share)."),
            item("C. Last heard from", f"PCs by when Aternity last heard from them: today, 1 to 7 days, "
                 f"{I.SILENT_DAYS} to {I.OLD_DAYS - 1} days, {I.OLD_DAYS} or more, no date. And the agent's status in "
                 "Aternity's own words."),
        ], [
            item("D. In use each day", "The PCs an application ran on, per kind of device, against the PCs of that "
                 f"kind in the list at that read. The last {I.CHART_DAYS} finished days; yesterday is the last bar."),
            item("E. In use each month", "PCs used at least once in the month, counted by Aternity itself; the "
                 f"busiest day of the month from the days kept. Up to {I.CHART_MONTHS} months, growing as the tab "
                 "keeps each month. This month is \"so far\"."),
            item("F. Four figures", "A typical working day and a typical weekend day (the middle value of the days "
                 "shown), the busiest day, this month so far. Each a count with its share of the list."),
        ]]),
        ("Safety rules", "They guard the counts above", "safe", [[
            item("Not judged is not 0", "A line whose column this Aternity does not publish, or \"not used\" when app "
                 "usage could not be read, says \"not judged\" and why."),
            item("No row is a gap", "A day Aternity has no row for is a gap on the chart, never 0. A day that could "
                 f"not be read, or was cut short, is said and tried again; {I.FAILS_STOP} failed days in a row end a pass."),
            item("Only PCs in the list count", "A PC with usage that is not in the device list is counted apart and "
                 "said on its bar, so a share is never over 100%."),
            item("What cannot be counted is not", "A month Aternity no longer keeps whole is never counted. A saved "
                 "file that cannot be read is set aside, not written over."),
        ]]),
        ("4. On the page", "The Inventory tab, top to bottom", "page", [[
            item("Tiles and Desktop usage", "The tiles, then two charts: bars for the PCs in use, a line for the PCs "
                 "in the list (100%). A switch between all PCs and each kind of device; the four figures under them."),
            item("Needs a look", "The lines, most PCs first. Lines with no PC are named in one sentence."),
            item("Last heard from · Retired PCs", "By age, with the agent's status. Retired PCs, and PCs in the "
                 "list that nobody uses."),
            item("Find a PC", "By part of a PC name, a person or a serial number."),
        ]]),
        ("5. By a click", "Names of PCs and users appear only here", "click", [[
            item("A line's PCs", f"Up to {I.PCS_SHOWN}, each with its own value for that line, \"Investigate\" and "
                 "its card. From the read held: no extra read of Aternity."),
            item("Find a PC", f"From {I.FIND_MIN} characters: up to {I.FIND_MAX} PCs whose name, last user or serial "
                 "number has them."),
            item("A PC's card", "Everything Aternity fills for it, the lines it is on, \"Investigate\", \"Its "
                 "start-ups\", and \"Its software\": the one read of Aternity made at a click."),
            item("Explain · link", "\"Explain\" on each panel, with the tab's own meanings. #inventory opens this "
                 "tab."),
        ]]),
    ], foot="No Active Directory on this tab: the main page has no login, and AD look-ups there stay with the Care list.")


# ------------------------------------------------------------------------------------ 08 What every tab shares
def every_tab():
    drawing("08-every-tab-logic", "Ask anything, Explain and the top bar: every rule behind them",
            "What every tab shares: the one chat, the explanation of a panel, the connection lights, the names offered as "
            "you type.", [
        ("1. When", "", "when", [[
            item("Ask anything", "Only when a question is asked. It runs as a job in the background; the page shows "
                 "each look-up as it starts."),
            item("Explain", "Only at a click on a panel's \"Explain\". Nothing is saved."),
            item("Connection lights", f"One small read of each system every {status.CHECK_EVERY_S} seconds "
                 f"(it waits {status.CHECK_TIMEOUT_S:g} seconds at most, with no second try)."),
            item("Names", "Every username and PC name seen in Aternity's activity, kept by the Today reader. A name "
                 f"not seen for {names.KEEP_DAYS} days is dropped."),
        ]]),
        ("2. The AI gets", "It reads nothing itself", "source", [[
            item("The question", f"With the last {agent.HISTORY} questions and answers, and a line of what was looked "
                 "up for them."),
            item("The tab that is open", "As the page shows it, with our written meaning of each term on that tab."),
            item("Aternity's own names", f"The kinds of event Aternity uses (up to {agent.KINDS_MAX}, the most PCs "
                 "first), so the AI picks the ones meant: \"hard drive issue\" is not a name Aternity has."),
            item("Look-ups", "A list of look-ups our code can run. The AI asks for them; our code counts and hands "
                 "back rows."),
        ]]),
        ("3. Worked out", "A to F", "step", [[
            item("A. Look-ups from what is already held", "No read of Aternity: find a user or PC by name · events · "
                 "measured actions · the day's overview · what a term means · the tab on screen."),
            item("B. Look-ups that read Aternity now", "Who logged in where · start-ups · a PC's software · a PC's "
                 "changes · programs per PC · the Software tab's look-ups (versions, compare, before and after, PCs) "
                 "· one whole investigation."),
            item("C. Limits per question", f"{agent.MAX_STEPS} look-ups, {agent.MAX_READS} of them reading Aternity, "
                 f"{agent.MAX_INVESTIGATIONS} investigation ({agent.INVESTIGATE_HOURS} hours unless asked, "
                 f"{agent.INVESTIGATE_MAX_H} at most). A step shows up to {agent.ROWS_MAX} rows."),
        ], [
            item("D. The answer", f"One line that answers, 2 to 4 points, a last line \"Next: ...\" (at most "
                 f"{chat.LINES_MAX} lines). In the AI's own words, from the rows it was handed."),
            item("E. Explain", "A panel's own lines and our meaning of each term go to the AI, which words them. A "
                 "question can be asked under it. For a Software comparison our own walk-through is shown first."),
            item("F. A name sent the wrong way", "A name known only as a user, sent as a PC (or the other way), is "
                 "looked up as what it is, and that is said."),
        ]]),
        ("Safety rules", "They guard the answers above", "safe", [[
            item("Numbers are checked", "Ask anything: a number in none of the look-ups, or a comparison the AI "
                 "worked out (\"twice as many\"), is marked on the answer. Explain: such an answer is refused and "
                 "our own written meanings are shown."),
            item("A failed look-up is said", "It is a step that says what failed, never a lost question. A read "
                 "cut short says its counts are partial."),
            item("No AI service", "Explain shows our own written meanings. The lights prove connection and "
                 "login, not the quality of a model's answers."),
            item("No login on the main page", "So Ask anything never asks Active Directory: on the main page only "
                 "the Care list does, for its own users."),
        ]]),
        ("4. On the page", "On every tab", "page", [[
            item("Ask anything", "At the bottom of every tab, with examples for the tab that is open. Answers stay "
                 "when the tab changes. \"What I looked at\" lists every step."),
            item("Explain", "At the bottom right of a panel: a dialog with the explanation, \"What the terms mean\" "
                 "and a box for a question."),
            item("Top bar", "The lights for Aternity, NetIM and each AI service: green, amber (the model is not in "
                 "the service's list), red and flashing (no answer, or login refused). The look: Parchment | Dark."),
            item("Tabs", "Today · Care list · Investigate · Start-ups · Software · Inventory. Each has a link and "
                 "opens where it was left. Scrolled down, the bar stays as one slim row with \"Top\"."),
        ]]),
        ("5. By a click", "", "click", [[
            item("A row of a step", "\"Investigate\" runs an investigation of that user and PC. \"Open this "
                 "investigation\" shows the one the chat ran, without a second run."),
            item("A light", "One \"Connections\" dialog: since when, the reply time, which model the chat uses, and "
                 "what in config.yaml is not used."),
            item("A name box", f"Names are offered as you type (up to {names.MATCHES}); any name can still be typed."),
            item("The look", "Kept in the browser; the main page and the admin page share it when opened under one "
                 "address. Until someone picks, the PC's own light or dark setting decides."),
        ]]),
    ])


today()
care_list()
investigate()
startups()
software()
admin()
inventory()
every_tab()
