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

from . import after_start, care, impact, logons, normal, software, today
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
        "issue": f"Either an event Aternity rated {_SEV} with that user's name today, or more {TOP_STATUS} slow "
                 "actions by this time of day than on any of their own days before.",
        "their usual by now": f"The fewest and the most {TOP_STATUS} slow actions that user had by this time "
                              f"of day on their own days before with a PC in use (same kind of day, up to "
                              f"{care.USUAL_DAYS}; {care.MIN_USUAL} needed).",
        "alert": "",  # set_refresh
        "Clears at": "",  # set_refresh
        "Earlier today": f"Today's issues of that user whose alert is off: no read of Aternity brought anything new "
                         f"of them in the last {care.ALERT_READS} reads.",
        "over": f"An issue (one count, one investigation) ends after {_QUIET_H} hour with no new one. A "
                "slow-actions issue also ends when the user is back within their usual. A later one is a new issue.",
        "✓ OK": "No alert on for that user now. The tile says how many actions were measured today and how "
                f"many were rated {TOP_STATUS}.",
        "not found": f"The name was not seen in Aternity's activity in {care.HISTORY_DAYS} days: it may be "
                     "misspelled.",
        "Why": "The headline of the investigation that ran by itself for that user's issues of today (at most one "
               "an hour per user; it runs again for an issue it did not cover, such as one Aternity sent late).",
        "Issues per day": f"Small bars: that user's issues on each of the last {care.KEEP_DAYS} days.",
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
        "1 in 10 over": "One start-up in ten took longer than this; nine in ten (90%) took less.",
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
        "red flag": "Something on the installed software worth raising to management. One line per kind, the "
                    "most PCs first.",
        "Crashes more / Hangs more / Slower": f"One version of an application has the problem (PCs with a crash, "
                                              f"PCs with a hang, or actions rated {TOP_STATUS}) at at least "
                                              f"{software.RATIO:g} times the share of its other versions, with "
                                              f"at least {software.MIN_PCS} PCs on each side and "
                                              f"{software.MIN_PROBLEM} PCs with the problem.",
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
}

TABS = {"today": "Today", "care": "Care list", "investigate": "Investigate", "startups": "Start-ups",
        "software": "Software"}

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
                                 f"sends rows late: new is what that read saw first). It stays on for "
                                 f"{care.ALERT_READS} reads ({care.ALERT_READS * minutes} minutes) after the last read "
                                 "that brought a new one. A failing disk (Aternity's (Disk) or (Drive) label on the "
                                 "failing part) stays on all day.")
    GLOSSARY["care"]["Clears at"] = (f"When the alert goes off if no read brings a new one: {care.ALERT_READS} reads "
                                     f"({care.ALERT_READS * minutes} minutes) after the last read that brought one.")


set_refresh(today.REFRESH_MIN)  # the default; web.py sets config.yaml's


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
        "More Major slow actions than usual: 14 by 10:15 (their usual by now: 0 to 3, 4 working days before)",
        "Why: Sample Mail Open took 21.4 s on VD-31 (Major). Likely: Sample Mail hung on VD-31 at 2 of them",
        "Issues per day, last 8 days: 0 0 1 0 0 0 0 1",
    ], "Why does this user have an issue?", ("14", "0 to 3", "usual")),
    ("care", "Tile: Shift lead (cd5678)", [
        "✓ OK", "Shift lead", "cd5678", "PC-204 (physical)",
        "No alert now: 212 actions today, 2 rated Major (usual by now: 0 to 4).",
        "Earlier today: more Major slow actions than usual from 08:10, the last at 09:05. Over: back within their "
        "usual at 09:35.",
        "Issues per day, last 8 days: 0 0 0 0 0 0 0 1",
    ], "What does Earlier today mean on this tile?", ("earlier", "alert", "08:10", "09:05", "09:35")),
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
        "Crashes more | Sample Viewer 12.4: a crash on 61 of 240 PCs; its other versions: 9 of 310 PCs. | 61 PCs "
        "| new",
    ], "Explain this red flag.", ("12.4", "61")),
    ("software", "Red flag: Windows update not on every PC", [
        "Windows update not on every PC | 3 Windows updates are on most PCs of their Windows version but not on "
        "every one; most: KB5550001 not on 2,037 of the 4,636 PCs on Sample Windows 11 23H2; 1,204 of those have "
        "KB5550002. | 2,037 PCs",
    ], "Are those 2,037 PCs unpatched?", ("later", "in its place", "kb5550002", "1,204")),
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
