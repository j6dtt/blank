""" "Explain this page": a panel of the page put in plain English by the AI.

Proposal and the user's choices: docs/explain-this-page.md (2026-11-10). The
AI never reads Aternity and never counts: it is given the panel's own lines
(the numbers the page shows) and our written meaning of each term on that
tab (`GLOSSARY`), and words them. Every number in its answer must be in
those lines, the meanings or the question (`refused`), as in the chats.

Step 1: the meanings, the instructions, the check, and the made-up panels
`uxcorrelate verify --ai` measures both models with (`SAMPLES`,
`long_page`). Step 2: `explain`, the answer for `/api/explain` (Today and
the Care list so far); the page sends the panel as it shows it.

`GLOSSARY` is the one place a term's meaning is written. Each is what the
page does (from the code and its constants), not what Aternity documents:
docs/data-meanings.md says how sure we are of Aternity's own values.
"""

from __future__ import annotations

import re
import unicodedata

from . import after_start, care, impact, inventory, logons, normal, software, today
from .assess import HOURS, ISOLATED, MIN_ACTIONS, MIN_STARTS, VERDICT_PCS, WIDESPREAD
from .chat import RATIO, allowed, clean, numbers
from .cause import DISK_BACK
from .config import TILE_SEVERITIES, TOP_STATUS

SENTENCES = (2, 5)
_SEV = " or ".join(TILE_SEVERITIES)
_QUIET_H = int(care.QUIET.total_seconds() // 3600)

COMMON = {
    f"{TOP_STATUS} slow action": f"One action of a user in an application (open a mail, launch a program) that "
                                 f"Aternity itself rated {TOP_STATUS}, the worst rating it gives here. The page "
                                 "counts Aternity's rating; it sets no limit of its own.",
    "event": "A health event Aternity recorded on a PC: an application crash or hang, a blue screen, low memory, "
             "a failing disk. Critical, Major and Minor are Aternity's own severity.",
    "virtual desktop": "A desktop Aternity marks as virtual: the user reaches it from another device. Users "
                       "move between them, so one user can have several in a week.",
    "Investigate ▸": "Runs an investigation of that user or PC on the Investigate tab, over the time around "
                     "what was found.",
}

_REACH = (f"How far a count reaches: isolated is fewer than {ISOLATED} PCs (too few to make the day's verdict), "
          f"limited is {ISOLATED} to {WIDESPREAD - 1} PCs, widespread is {WIDESPREAD} PCs or more.")
_MOVED = ("Since the assessment before: rose or fell, or steady when the same. A count of the day so far only "
          f"rises or stays; the share of actions rated {TOP_STATUS} in an hour can fall.")

GLOSSARY: dict[str, dict[str, str]] = {
    "today": {
        "Today": "",  # set_refresh: how often Aternity is read
        "Assessment": "A short reading of the day for executives, written at "
                      + ", ".join(f"{h:02d}:00" for h in HOURS) + ": the "
                      "verdict and what the day means for people working, then points labelled Changed (since the "
                      "assessment before), Watch (above its usual range now), Earlier (above it earlier today, "
                      "not now) and Start-ups, and what to look at first. Our code counts the facts (counts and "
                      "names of events and applications, no user or PC), the AI words them, and every number and "
                      "rating word is checked against them. The verdict is by fixed rules, and says why: \"Needs "
                      "attention\" (a connection to Aternity or NetIM down, or a Critical event above its usual "
                      f"range on {VERDICT_PCS} PCs or more), \"Worse than usual\" (a Major count above its usual "
                      f"range on {VERDICT_PCS} PCs or more), \"A normal day so far\", or \"No usual to compare with "
                      "yet\". A count above its usual range on fewer PCs is isolated: listed, but it does not make "
                      "the verdict.",
        "This week, Mon to Sun": (
            "The small lines under Yesterday: for each day of this week, Monday to Sunday, the PCs with an "
            "event Aternity rated Critical, with one rated Major, and with actions it rated "
            f"{TOP_STATUS}, each PC once a day. The solid line is this week, the hollow point today so far, "
            "the dashed line last week on the same days. Each row has its own scale. A day not read in full "
            "is a gap, never 0. Point at a day for both numbers."),
        "isolated": _REACH, "limited": _REACH, "widespread": _REACH,
        "usual range": "The lowest to the highest count of the same kind of day before (working days with "
                       f"working days), up to {today.USUAL_DAYS} days, by the same time of day. Above it: more "
                       "than on any of those days (the tiles say \"More than usual\"); below it: fewer than on "
                       "any; within it: in between.",
        "rose": _MOVED, "fell": _MOVED, "steady": _MOVED,
        "cleared": "Above its usual range at an earlier assessment or hour of today, and not now.",
        "Share of actions rated Major": f"Per hour of today: the actions Aternity rated {TOP_STATUS} out of all the "
                                        "actions it measured in that hour. The grey band is the lowest to the "
                                        "highest share of the same hour on the working days before. An hour with "
                                        f"fewer than {MIN_ACTIONS} measured actions has no point.",
        "PCs with a crash, so far today": "The PCs that have had a crash event today by the end of each hour, each "
                                          "PC once. The grey band is the same count on the days before.",
        "Typical": "The middle value: half took less, half took more. For a start-up or logon, per hour of today "
                   f"(an hour with fewer than {MIN_STARTS} has no point) and for the day so far.",
        "Active": "Users and PCs with at least one action Aternity measured in an application, whatever its "
                  "rating, each counted once: today so far, and since the start of the last full hour. PCs are "
                  "split by Aternity's own virtual or physical mark. A PC that is on with nobody working in a "
                  "measured application is not counted; a user who moved between desktops is one user and "
                  "several PCs.",
        "tile": f"One kind of event Aternity rated {_SEV} today, with the number of PCs that had it. A click "
                "lists the users and PCs.",
        "usual": f"Today against the same kind of day before (working days with working days, weekends with "
                 f"weekends), up to {today.USUAL_DAYS} days, at the same time of day. More than usual: more "
                 f"than on any of those days. Fewer than usual: fewer than on any of them. Otherwise as usual. "
                 f"It needs {today.MIN_USUAL} saved days; until then no usual is said.",
        f"{TOP_STATUS} slow actions per hour": f"One bar per hour of today: the actions Aternity rated "
                                               f"{TOP_STATUS} in that hour. The line across a bar is the "
                                               "middle value of the same hour on the days compared with.",
        f"Applications with the most {TOP_STATUS} slow actions": f"Per application today: its actions rated "
                                                                 f"{TOP_STATUS} and the PCs they were on. The "
                                                                 "top one by count is not always the worst by "
                                                                 "share.",
        "Other crashes and events on the most PCs today": "The kinds of event on the most PCs today, whatever "
                                                          "their severity, "
                                                          "each with its PCs per day over the last 7 days.",
    },
    "care": {
        "Care list": "",  # set_refresh
        "issue": f"An event Aternity rated {_SEV} with that user's name today, more {TOP_STATUS} slow "
                 "actions by this time of day than on any of their own days before (each one judged by its own "
                 "time of day, whenever it is checked), or a slow start-up.",
        "Slow start-up": (f"A start-up of that user today (a logon or a boot, Aternity's own types) that took longer "
                          f"than every one of that type of theirs in the {care.BOOT_DAYS} days before "
                          f"({care.BOOT_MIN} of them needed) and longer than 1 in 10 of everyone's of that type "
                          "today. Both: a few seconds over their own longest, or a morning slow for everyone, is "
                          "not one. Aternity gives start-ups no severity. Show more lists each start-up of today "
                          "with both comparisons, or why it was not compared."),
        "disabled in AD": ("Active Directory says that user's account is disabled: they may have left. \"not in "
                           "AD\": no account of that name. The name and department under the label are AD's "
                           "(looked up once a day); with no label, the tile shows AD's name."),
        "their usual by now": f"The fewest and the most {TOP_STATUS} slow actions that user had by this time "
                              f"of day on their own latest {care.USUAL_DAYS} days with a PC in use, any day of "
                              f"the week ({care.MIN_USUAL} needed).",
        "Rated Major today": f"Each of that user's actions today that Aternity rated {TOP_STATUS}, latest first: "
                             "the time, the application and activity, how long it took (and the PC when they "
                             "used more than one). The tile shows the latest 3; Show more lists them all.",
        "alert": "",  # set_refresh
        "Clears at": "",  # set_refresh
        "over": f"An issue (one count, one investigation) ends after {_QUIET_H} hour with no new one. A "
                "slow-actions issue also ends when the user is back within their usual. A later one is a new issue.",
        "✓ OK": "No alert on for that user now. The tile says how many actions were measured today, how "
                f"many were rated {TOP_STATUS} and their usual by now. \"More than their usual by now\" with OK: "
                f"no new {TOP_STATUS} action came in the last reads (the alert is on only while reads bring new "
                "ones).",
        "not found": f"The name was not seen in Aternity's activity in {care.HISTORY_DAYS} days: it may be "
                     "misspelled.",
        "Major actions each day": (
            f"Small bars: that user's actions Aternity rated {TOP_STATUS} by this time of day on each of the last "
            f"{care.KEEP_DAYS} days, today last, from Aternity's own records. Today's bar is the tile's \"rated "
            f"{TOP_STATUS}\"; the dark bars before it are the days their usual comes from (the lowest to the "
            f"highest of them is the usual range), faded bars are older days. Dashed: not read. The issues of "
            "the days before are under Show more, the latest in \"Last issue before today\"."),
        "Last issue before today": "The latest issue the Care list raised for that user before today, with its day.",
        "not compared with their usual": (
            f"Today's {TOP_STATUS} actions could not be compared: their usual needs {care.MIN_USUAL} days with "
            f"a PC in use among the {care.HISTORY_DAYS} days of activity read (Aternity keeps about 8), as "
            "for someone new or seldom on a PC. Slow actions alone do not light their tile until then; events "
            "still do."),
    },
    "investigate": {
        "headline": "Two sentences: what the user felt (the action, how long it took, on which PC), then one "
                    "verdict on why, or what is known. \"Likely\" and \"Possibly\" say how strong it is.",
        "What the user felt": "The slow actions, hangs, crashes and start-ups of that user in the time asked "
                              "for, on each PC they worked on.",
        "What went wrong": "One row per issue found on the user's PCs, those at the slow actions first. Each "
                           "has what is known of it, possible causes and who else has it.",
        "counted for a slow action": "The problem was within the \"Match within\" minutes of a slow action on "
                                     "the same PC. \"Not counted\" means it was found but at another time.",
        "Aternity's split": "Aternity's own division of an action's time between the PC, the network and the "
                            "server.",
        "possible cause": "A software change on that PC before the failure, with the same name or version as "
                          "the part that failed. It is not said when other PCs fail the same way on another "
                          "version.",
        "who else": "Other PCs with the same failure (same event, same failing part), out of the PCs that use "
                    "that application.",
        "Other users of these apps": f"Other users of the same applications who were slow at the same time, "
                                     f"against the same hours on the {impact.DAYS_BACK} days before. It is the "
                                     "verdict only when Aternity did not put most of the slow time on the "
                                     "user's own PC.",
        "usually": f"The user's own normal time for that action, from up to {normal.BASELINE_DAYS} days; the "
                   "days found are said, as Aternity keeps about 8.",
        "failing disk": f"Aternity recorded a failing disk or drive on the PC. It counts for the slow actions "
                        f"on that PC from its first record, looked for up to {DISK_BACK.days} days before.",
        "right after a start-up": f"An action up to {after_start.AFTER_START_MIN} minutes after a start-up of "
                                  "its PC, compared with the same action later on all PCs of that kind.",
        "What changed": "The software installed, updated or removed on each PC, from Aternity's change log, "
                        "from 14 days before the time asked for to its end. An issue names a change only when "
                        "it matches the part that failed.",
        "What to do": "The steps for IT, the most important first.",
        "checked": "What was looked at and ruled out, with counts.",
        "Other things on": "Problems found on the user's PCs that were not counted for a slow action, so they "
                           "are not part of the answer at the top. Each says why.",
        "Widespread": "How far an issue reaches on other PCs. It does not say that it is, or is not, the cause "
                      "here.",
        "Compared with normal": "Each slow action next to the user's own usual time for it, and whether "
                                "Aternity marked most of its runs slow on some days, and since when.",
        "Timeline": "Every action of the user and every problem found, in time order. A faded mark was not "
                    "near a slow action.",
        "Problems found": "The full list of problems found, one row each, with the slow actions each was "
                          "counted for.",
    },
    "startups": {
        "start-up": "A start of Windows or a logon as Aternity measured it. Rows of one PC within an hour are "
                    "one start-up; each column is one of Aternity's measurements.",
        "typical": "The middle value: half the start-ups took less, half took more.",
        "1 in 10 over": "One start-up in ten took longer than this; nine in ten (90%) took less. Given from 10 "
                        "start-ups of a type; with fewer, the slowest one is shown instead.",
        "usual for this PC": f"That PC's own middle value over its last {logons.BASELINE_DAYS} days; "
                             f"{logons.MIN_HISTORY} earlier start-ups are needed. For a user on virtual "
                             "desktops, their start-ups on all their desktops are compared with each other.",
        "what took the time": "The part of the start-up (a start-up program, a policy, the profile) that "
                              "Aternity marked as the most longer than normal.",
        "main causes": "A part that was longer than normal on every PC that had it, not on one PC only.",
        "By location": "The same start-ups grouped by the location Aternity gives each PC.",
    },
    "software": {
        "Software tab": f"What Aternity lists as installed on the PCs, read once a day, and each application's "
                        f"versions over the last {software.DAYS} days.",
        "left out": "PCs that have installed software in Aternity but are no longer in its device list. Aternity "
                    "takes a PC off its device list about a month after it last heard from it and keeps its "
                    "software, so these are PCs that are gone. They are not counted anywhere on the tab.",
        "red flag": "Something on the installed software worth raising to management. One line per kind, the "
                    "most PCs first.",
        "Crashes more / Hangs more / Slower": f"A version of an application is raised (▲) when the problem (PCs with "
                                              f"a crash, PCs where it stopped responding, or actions rated "
                                              f"{TOP_STATUS}) shows on at least {software.RATIO:g} times the share "
                                              f"of what it is compared with, {software.MIN_PROBLEM} PCs or more "
                                              f"have it, and it would still be {software.RATIO:g} times with "
                                              f"{software.STILL} more PCs with it on the other side (for slow "
                                              f"actions: with {software.STILL_ACTIONS} more of the other side's "
                                              f"actions rated {TOP_STATUS}), so one or two never decide it. Two "
                                              "comparisons: before and after an update, and each version against "
                                              "the version most PCs have.",
        "Crashed": f"The application closed by itself. A PC counts once, however many times it happened in the "
                   f"{software.DAYS} days. From Aternity's daily app usage.",
        "Stopped responding": "The application froze for a while (Windows shows \"Not responding\"). Aternity calls "
                              "it hang time and gives no unit for how long, so none is shown. A PC counts once if "
                              "it had any.",
        "Slow actions": f"The share of a version's actions that Aternity rated {TOP_STATUS}, its word for clearly "
                        "slower than usual.",
        "the version most PCs have": f"The yardstick: of an application's versions on {software.MIN_PCS} PCs or more, "
                                     "the one on the most PCs (not one that is new this week). Every other version "
                                     "is compared with it.",
        "new this week": f"Most of the version's PCs got it within the last {software.DAYS} days, so it has had less "
                         "time to go wrong than the others. It is judged before and after its update, not side by "
                         "side.",
        "Before and after the update": f"PCs whose version of the application changed once in the last "
                                       f"{software.UPDATE_DAYS} days. For each PC the same number of days is counted "
                                       "before and after, so neither side had more time to go wrong. The same PCs "
                                       "and people are on both sides, so the version is what changed.",
        "too few to judge": f"A version on under {software.MIN_PCS} PCs of that kind. One PC with a problem would "
                            "move its share too far to mean anything, so it is listed but not compared.",
        "virtual desktops and physical PCs": "Aternity marks each PC as one or the other. They are compared apart, "
                                             "because the same version can behave differently on each: mixed "
                                             "together, the version would be blamed for what is the kind of PC.",
        "not compared": "Every PC listed for that application had the problem, so no version can be said to "
                        "be worse than another.",
        "Behind most PCs": "PCs on a version older than the one most PCs of that program have.",
        "2+ versions on one PC": "The same program installed in two or more versions on one PC.",
        "Installed for one user": "Aternity lists the install as for a specific user, not for the whole PC.",
        "Windows update not on every PC": f"An update that more than half the PCs of a Windows version have, "
                                          "and the PCs of that version without it. Some have a later update "
                                          "in its place.",
        f"On {software.FEW_PCS} PCs or fewer": f"Programs found on {software.FEW_PCS} PCs or fewer in the "
                                               "whole company.",
        "new": "The flag was not there on the day before.",
        "Compare versions": "Two versions of one application side by side, read from Aternity when asked: the "
                            f"PCs with a crash, with hang time, with each kind of health event, and the actions "
                            f"rated {TOP_STATUS}, each out of that version's PCs or actions. Side by side, not "
                            "judged: only a red flag says a version is worse.",
        "Before and after an update": f"The same PCs before and after their version of an application changed "
                                      f"once in the last {software.UPDATE_DAYS} days: their days with a crash "
                                      "or hang time on each side.",
        "Programs by how many PCs have them": "How many programs are on 1 PC, on a few, on many. A click lists "
                                              "the programs of a column.",
        "A program": "One installed program: the PCs on each of its versions, and the PCs behind.",
        "PCs per day": f"Small bars: the flag's PCs on each saved day (kept {software.KEEP_DAYS} days).",
    },
    "inventory": {
        "Inventory tab": f"Every PC in Aternity's device list, read every {inventory.READ_EVERY_MIN} minutes. The "
                         "lists and the cards answer from that read; only the usage charts' counts are saved.",
        "PCs in Aternity's device list": "A PC is in the list while Aternity's agent on it reports. Aternity seems "
                                         "to take a PC off the list about a month after it last heard from it.",
        "virtual desktop": "A PC whose kind of device, as Aternity gives it, says virtual. Every other kind (a "
                           "desktop, a laptop) is a physical PC.",
        "heard from": "When Aternity's agent on the PC last reported to Aternity. It says nothing about whether "
                      "someone used the PC.",
        "No date from Aternity": "Aternity gives no time for when it last heard from the PC. That does not mean "
                                 "the PC is gone.",
        "retired": "A PC with installed software in Aternity that is no longer in its device list. Nothing else is "
                   "known of it, and the Software tab leaves it out of every count.",
        "Desktop usage": f"The PCs an application ran on, against the PCs in Aternity's device list: each of the "
                         f"last {inventory.CHART_DAYS} finished days, and each month (up to "
                         f"{inventory.CHART_MONTHS}). A switch shows all PCs or one kind of device, as Aternity "
                         "names it. Counts per kind are kept by the tab; no PC or user name is.",
        "in use": "An application ran on the PC that day, by Aternity's daily app usage. Each PC is counted once. "
                  "Only PCs in Aternity's device list are counted; a PC with app usage that is not in the list is "
                  "left out, and how many is said on each bar.",
        "In Aternity's list = 100%": "The PCs of that kind in Aternity's device list on that day or month. Every "
                                     "percentage is PCs in use out of these. Before the tab kept counts, the list "
                                     "of its first day is used.",
        "last bar is yesterday": "Aternity has no rows of daily app usage for today: it adds a day once the day "
                                 f"is over. A day is read again until it is {inventory.SETTLE_DAYS} days old, in "
                                 "case its rows come late, then kept as it is.",
        "typical working day": "The middle value of the PCs in use on the Monday to Friday days shown: half of "
                               "those days had this many or more. A public holiday is one of them.",
        "typical weekend day": "The middle value of the PCs in use on the Saturdays and Sundays shown.",
        "busiest day": "The day with the most PCs in use: of the days shown, or of a month on the monthly chart.",
        "used at least once": "PCs an application ran on, on at least one day of the month, each PC once. Aternity "
                              "adds the month up itself while it keeps all its days; once a month is over the "
                              "tab keeps its count.",
        "so far": "The month is not over: its count covers its finished days, up to yesterday.",
        "Needs a look": "One line per kind of thing to check, the most PCs first. A click lists the PCs. Each "
                        "count has its share of the PCs it is counted among, which the line names (\"of N\").",
        inventory.LINES["agent"]: "The PC's Aternity agent is an older version than the one most PCs have.",
        inventory.LINES["bitlocker"]: "A desktop or laptop that reports BitLocker as off to Aternity. Virtual "
                                      "desktops are not counted.",
        inventory.LINES["unused"]: f"In the list and not silent, but no application ran on the PC in "
                                   f"{inventory.USE_DAYS} days, by Aternity's daily app usage.",
        inventory.LINES["restart"]: f"The last start-up Aternity has for the PC is more than "
                                    f"{inventory.RESTART_DAYS} days ago. A PC with no start-up time is not judged.",
        inventory.LINES["silent"]: f"Aternity last heard from the PC {inventory.SILENT_DAYS} days ago or more. It "
                                   "is still in the list.",
        inventory.LINES["stopped"]: "Aternity's own status of the PC's agent says it does not run. It sends "
                                    "nothing until it runs again.",
        inventory.LINES["stability"]: f"Microsoft's own score of how stable a PC is, from 1 (least stable) to 10. "
                                      f"Listed under {inventory.STABILITY_LOW}.",
        inventory.LINES["free"]: f"Aternity gives a PC's free space as a band of sizes, not a number. Listed: the "
                                 f"bands that end at {inventory.FREE_LOW_GB} GB or under.",
        inventory.LINES["spinning"]: "Aternity's kind of disk says a spinning hard disk, not an SSD or NVMe.",
        "In the list, but not used": f"PCs that report to Aternity but ran no application in "
                                     f"{inventory.USE_DAYS} days: spare stock, a kiosk, or a desktop nobody is "
                                     "assigned to.",
        "Find a PC": "By PC name, the last person on it, or its serial number, from the latest read. The card "
                     "shows what Aternity's device list holds for the PC, as Aternity sends it.",
    },
}

TABS = {"today": "Today", "care": "Care list", "investigate": "Investigate", "startups": "Start-ups",
        "software": "Software", "inventory": "Inventory"}

SYSTEM = (f"You explain one panel of a web page to IT staff in plain English. The page shows the experience of "
          f"the company's PC users, from Riverbed Aternity. You are given the panel's lines exactly as the page "
          f"shows them, and what the page's terms mean. Answer the question as plain text with no markdown and no "
          f"table, in this shape: first one short sentence that answers it (for \"explain\": what the panel "
          f"shows); then {SENTENCES[0]} to {SENTENCES[1] - 1} lines that each start with \"- \" and say what one "
          f"number or term on the panel means, or what stands out, by name; then, only when the lines or the "
          f"meanings give one, a last line that starts with \"Next: \" with one next step. Use the terms' meanings as "
          f"written; do not add causes, advice or facts that are not in the lines or the meanings. Use only "
          f"numbers that are in the lines, written exactly as given, and do not work out new ones (no sums, no "
          f"percentages, no \"twice as many\"). If the lines and meanings do not answer the question, say so in "
          f"one sentence.")
# our own "2 times the share" said as "twice" is not a comparison of the model's (Nemotron, stage)
SAME = {"twice": ("2 times", "two times"), "double": ("2 times", "two times")}
COMPARES = re.compile(r"\s+(?:as|the|their|its|his|her|that|what|more|many|much|than)\b", re.I)
NO_ANSWER = "The panel's lines and the meanings of its terms do not answer that."


def set_refresh(minutes: int) -> None:
    """config.yaml's refresh_minutes into the two meanings that say how often Aternity is read
    (written at import with the default; web.py calls this at start)."""
    GLOSSARY["today"]["Today"] = (f"From midnight (in the time zone shown) to the last read. Aternity is read every "
                                  f"{minutes} minutes.")
    GLOSSARY["care"]["Care list"] = (f"Named users checked after every read of Aternity ({minutes} minutes), up to "
                                     f"{care.CARE_MAX}. Every user has a tile.")
    GLOSSARY["care"]["alert"] = (f"A tile is lit when a read of Aternity brings something new of an issue (Aternity "
                                 f"sends rows late: new is what that read saw first). It goes off at the "
                                 f"{_nth(care.ALERT_READS)} read ({care.ALERT_READS * minutes} minutes) after the last read "
                                 "that brought a new one. A failing disk (Aternity's (Disk) or (Drive) label on the "
                                 "failing part) stays on all day.")
    GLOSSARY["care"]["Clears at"] = (f"The read at which the alert goes off if no read brings a new one: the "
                                     f"{_nth(care.ALERT_READS)} read ({care.ALERT_READS * minutes} minutes) after the last "
                                     "read that brought one. Reads start every "
                                     f"{minutes} minutes.")
    GLOSSARY["care"]["Aternity sent it at"] = ("When a read of Aternity first had the row: Aternity sends rows late, "
                                               "sometimes most of an hour after it happened. The alert counts its "
                                               "reads from then.")


def _nth(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


set_refresh(today.REFRESH_MIN)  # the default; web.py sets config.yaml's


# the terms of the Software tab's "Application health by version", for its own Explain dialogs
VERSION_TERMS = ("the version most PCs have", "Crashed", "Stopped responding", "Slow actions",
                 "Crashes more / Hangs more / Slower", "new this week", "Before and after the update",
                 "too few to judge", "virtual desktops and physical PCs")


def version_terms() -> list[dict]:
    return [{"term": t, "meaning": GLOSSARY["software"][t]} for t in VERSION_TERMS]


def meanings(tab: str) -> dict[str, str]:
    """The terms of one tab, with those every tab shares."""
    return {**COMMON, **GLOSSARY.get(tab, {})}


def given(tab: str, panel: str, lines: list[str]) -> str:
    """What the AI is given about a panel: its lines, then the terms' meanings."""
    terms = "\n".join(f"- {k}: {v}" for k, v in meanings(tab).items())
    return (f"Tab: {TABS.get(tab, tab)}\nPanel: {panel}\n\nThe panel's lines:\n" + "\n".join(lines)
            + f"\n\nWhat the terms mean:\n{terms}")


def messages(tab: str, panel: str, lines: list[str], question: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{given(tab, panel, lines)}\n\nQuestion: {question}"}]


def refused(text: str, tab: str, panel: str, lines: list[str], question: str) -> str | None:
    """Why an answer is not shown (None: fine): empty, a number that is not on the panel,
    or a comparison worked out from its numbers."""
    text = clean(text)
    if not text:
        return "it gave no answer"
    extra = numbers(text) - allowed(given(tab, panel, lines), question, SYSTEM)
    if extra:
        return "a number that is not on the panel (" + ", ".join(f"{x:g}" for x in sorted(extra)[:3]) + ")"
    have = plain(given(tab, panel, lines) + " " + question)
    ok = numbers(" ".join(lines))  # the panel's own numbers, not those of the meanings
    for ratio in RATIO.finditer(text):  # "half the start-ups took less" is our own meaning of typical
        said = plain(ratio.group(0))
        if said in have or any(x in have for x in SAME.get(said, ())):
            continue
        if said == "twice" and 2.0 in ok and not COMPARES.match(text[ratio.end():]):
            continue  # a count, as "it hung twice" for the panel's "at 2 of them": not "twice as many"
        around = text[max(0, ratio.start() - 40):ratio.end() + 40].strip()
        return f"a comparison the panel does not give (\"{ratio.group(0)}\" in \"…{around}…\")"
    return None


def plain(text: str) -> str:
    """Lower case, one space between words, no invisible characters, every dash a "-": a
    model may write a no-break space inside a name ("Sample\u00a0ERP") or a no-break hyphen
    ("VD\u201177": Nemotron's answer was judged without VD-77 in it, stage, Nov 2026)."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = "".join("-" if unicodedata.category(c) == "Pd" or c == "\u2212" else c
                   for c in text if unicodedata.category(c) != "Cf")
    return " ".join(text.lower().split())


def says(text: str, words) -> bool:
    return any(plain(w) in plain(text) for w in words)


def odd(text: str) -> str:
    """The unusual spaces and invisible characters of an answer, for a verify row."""
    found = sorted({f"U+{ord(c):04X}" for c in str(text or "")
                    if ord(c) > 127 and unicodedata.category(c) in ("Zs", "Cf", "Zl", "Zp", "Pd")})
    return ", ".join(found)


# -- an answer for the page (`/api/explain`) -------------------------------------------------------
QUESTION = "What does this panel show, and what do its numbers mean?"
QUESTION_MAX, PANEL_MAX, OUR_TERMS = 500, 200, 6
LINES_MAX = 40_000  # both models read a made-up page of this size right (stage, Nov 2026)


def trim(lines: list[str], limit: int = LINES_MAX) -> tuple[list[str], bool]:
    """The panel's lines, cut at `limit` characters; whether any were left out."""
    out, total = [], 0
    for x in lines:
        x = " ".join(str(x).split())
        if not x:
            continue
        total += len(x) + 1
        if total > limit:
            return out, True
        out.append(x)
    return out, False


def terms_on(tab: str, panel: str, lines: list[str], question: str = "") -> list[tuple[str, str]]:
    """The terms the question or the panel has, each with our written meaning: those asked
    about first. Shown under every answer ("What the terms mean") and as our own answer."""
    def has(term: str, text: str) -> bool:  # a whole word, or its plural: not "event" in "prevent"
        return re.search(r"(?<!\w)" + re.escape(plain(term)) + r"s?(?!\w)", text) is not None
    asked, have = plain(question), plain(" ".join([panel, *lines]))
    all_terms = meanings(tab)
    first = [k for k in all_terms if has(k, asked)]
    return [(k, all_terms[k]) for k in first + [k for k in all_terms if k not in first and has(k, have)]][:OUR_TERMS]


def our_words(tab: str, panel: str, lines: list[str], question: str = "") -> str:
    """Our own answer, when the AI's cannot be shown: the written meaning of each term the
    panel (or the question) has, one per line."""
    terms = terms_on(tab, panel, lines, question)
    if not terms:
        return NO_ANSWER
    return "\n".join(f"{k}: {v}" for k, v in terms)


ASK = ("What stands out here?", "What should I look at first, and why?")  # questions offered under an answer


def suggestions(terms: list[tuple[str, str]], question: str) -> list[str]:
    """Follow-up questions to offer: two general ones and what the first terms mean."""
    out = list(ASK) + [f"What does \"{k}\" mean here?" for k, _ in terms[:2]]
    return [q for q in out if plain(q) != plain(question)]


def explain(client, tab: str, panel: str, lines: list[str], question: str = "", service: str = "") -> dict:
    """What /api/explain returns: the AI's wording of a panel, held to its numbers, else our
    own written meanings. `client` None: no AI service is set."""
    import time

    from .http import ApiError
    question = " ".join((question or "").split())[:QUESTION_MAX] or QUESTION
    panel = " ".join((panel or "").split())[:PANEL_MAX]
    lines, cut = trim(lines)
    started, text, note = time.monotonic(), "", None
    if client is None:
        note = "No AI service is set (ai.services in config.yaml): these are our own written meanings."
    else:
        try:
            text = clean(client.chat(messages(tab, panel, lines, question)).get("content"))
            why = refused(text, tab, panel, lines, question)
            if why:
                text, note = "", f"The AI's answer had {why}: these are our own written meanings instead."
        except ApiError as exc:
            note = (f"The AI service {service or 'set'} did not answer ({exc.short()}): these are our own "
                    "written meanings.")
    terms = terms_on(tab, panel, lines, question)
    return {"question": question, "panel": panel, "answer": text or our_words(tab, panel, lines, question),
            "ours": not text, "note": note, "cut": cut, "seconds": round(time.monotonic() - started, 1),
            "service": service if text else "",
            # always with the answer: our written meaning of each term on the panel, and what to ask next
            "terms": [{"term": k, "meaning": v} for k, v in terms], "ask": suggestions(terms, question)}


# -- what `verify --ai` measures with (made-up names and numbers) --------------------------------
# tab, panel, its lines, the question, and words a right answer has at least one of (any case)
SAMPLES = (
    ("today", "Major slow actions", [
        "Major slow actions: 318 on 141 PCs, 152 users",
        "By 14:00: 126 PCs. More than usual (usual by 14:00: 61 to 97 PCs, 5 working days before)",
    ], "Explain this panel.", ("more than usual", "more than on any", "141", "126")),
    ("today", "Applications with the most Major slow actions", [
        "Application | Major slow actions | PCs",
        "Sample Mail | 96 | 71", "Sample ERP | 54 | 12", "Sample Browser | 31 | 29",
        "Note: the highest share of Major among applications with 20 runs or more: Sample ERP, 54 of 210 actions",
    ], "Which application should I look at first, and why?", ("sample erp", "sample mail")),
    ("care", "Tile: Front desk (ab1234)", [
        "Major since 09:40", "Front desk", "ab1234", "VD-31 (virtual)",
        "More Major slow actions than usual: 14 by 10:15 (their usual by now: 0 to 3, 4 days before)",
        "Major actions each day, by 10:15, last 8 days (today last): 0, 1, 0, 3, 2, 0, 1, 14; an issue on the last",
    ], "Why does this user have an issue?", ("14", "0 to 3", "usual")),
    ("care", "Tile: Shift lead (cd5678)", [
        "✓ OK", "Shift lead", "cd5678", "PC-204 (physical)",
        "212 actions today, 2 rated Major (usual by now: 0 to 4).",
        "Major actions each day, by 10:15, last 8 days (today last): 1, 0, 2, 4, 3, 1, 0, 2; an issue on the last",
    ], "What does usual by now mean on this tile?", ("usual", "0 to 4", "10:15")),
    ("investigate", "Headline and What to do", [
        "Sample Mail Launch took 89.2 s on PC-77 (Major). Likely: PC-77's disk is failing (Aternity recorded "
        "Faulty HD S.M.A.R.T. Status and HD Failure)",
        "A failing disk slows everything on its PC. Aternity recorded it failing from Nov 10, 12:45 AM and put "
        "100% of the slow time on the PC. See What went wrong.",
        "What to do: 1. On PC-77: back up the user's data and replace the disk (XY250-MODEL): Aternity recorded "
        "it failing.",
        "Other users: 2 of the 14 other Sample Mail users were slow at the same time. Aternity put 100% of this "
        "user's slow time on their PC, so this is not taken as the cause.",
    ], "Explain this investigation to me.", ("disk", "pc-77")),
    ("investigate", "What went wrong: Sample Notes crashed", [
        "Sample Notes crashed on PC-77: 1 record, not counted for a slow action",
        "What we know: it failed in notes.dll 4.2.1. No change with that name or version was logged before it.",
        "Who else: 37 other PCs of the 412 that use Sample Notes had the same crash in the last 7 days.",
    ], "Is this crash the reason the user was slow?", ("not counted", "another time", "no", "not")),
    ("startups", "Start-ups: each measurement", [
        "412 start-ups. For each Aternity measurement: the typical time, and the time 1 in 10 start-ups are "
        "slower than.",
        "Machine Boot: typical 38.2 s · 1 in 10 over 96.0 s",
        "User Logon: typical 21.7 s · 1 in 10 over 74.5 s",
    ], "What do typical and 1 in 10 over mean here?", ("half", "middle", "one in ten", "1 in 10", "ten")),
    ("startups", "A start-up of VD-12", [
        "VD-12, user ef9012: User Logon 143.0 s (usual for this user's virtual desktops: 24.5 s)",
        "What took the time: Sample Sync Agent, 96.0 s longer than normal",
        "Main causes: none: no part was longer than normal on every PC",
    ], "Why was this start-up slow?", ("sample sync agent", "96")),
    ("software", "Red flag: Crashes more", [
        "Crashes more | Sample Viewer 12.4, physical PCs: it crashed on 25% of its physical PCs (61 of 240). On the "
        "version most have, 12.3: 2.9% (9 of 310). | 61 PCs "
        "| new",
    ], "Explain this red flag.", ("12.4", "61")),
    ("software", "Red flag: Windows update not on every PC", [
        "Windows update not on every PC | 3 Windows updates are on most PCs of their Windows version but not on "
        "every one; most: KB5550001 not on 2,037 of the 4,636 PCs on Sample Windows 11 23H2; 1,204 of those have "
        "KB5550002. | 2,037 PCs",
    ], "Are those 2,037 PCs unpatched?", ("later", "in its place", "kb5550002", "1,204")),
    ("inventory", "Needs a look", [
        "On an older agent than most | An Aternity agent older than 12.9.1.40, the version most PCs have. | 310 PCs "
        "· 12% of 2,600",
        "Still on a spinning hard disk | Aternity's kind of disk starts with 'HDD': 'HDD-7200'. | 12 PCs · 2.4% of 500",
    ], "What does on an older agent than most mean?", ("12.9.1.40", "version", "older")),
    ("inventory", "Last heard from", [
        "Today 1,900 · 73%", "1 to 7 days ago 420 · 16%", "8 to 30 days ago 80 · 3.1%",
        "No date from Aternity 200 · 7.7%",
        "A PC with no date is not gone: Aternity just gives no time for it.",
    ], "Are the 200 PCs with no date gone?", ("not gone", "does not mean", "no time", "not")),
)
SIZES = (4_000, 12_000, 40_000)  # characters of panel lines: a tile, a whole tab, a whole investigation
LONG_QUESTION = "How many PCs does the line about update KB5559999 give, and what does that line mean?"
LONG_ANSWER = "1,713"


def long_page(size: int) -> list[str]:
    """Made-up panel lines of about `size` characters, with the one line the question is about
    three quarters of the way down (a model that reads only the top misses it)."""
    lines, n, total = [], 0, 0
    while total < size:
        n += 1
        line = (f"Sample Program {n:04d} | version 1.{n % 9}.{n % 37} | {(n * 7) % 97 + 3} PCs | "
                f"{(n * 3) % 11} with a crash")
        if total < size * 3 // 4 <= total + len(line) + 1:
            line = (f"Windows update not on every PC | KB5559999 not on {LONG_ANSWER} of the 4,902 PCs on Sample "
                    "Windows 11 24H2")
        lines.append(line)
        total += len(line) + 1
    return lines
