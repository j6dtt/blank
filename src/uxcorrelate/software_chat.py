"""The chat on the Software tab ("Ask about software", POST /api/software/chat).

It works as the Today tab's chat (`chat.converse`): the AI picks one building
block and its settings, our code counts, the AI words the answer in two to four
sentences, and every number in it must be one of our counts (or in the question):
otherwise our own sentence is shown, and says so. The counts are always shown.

Building blocks (TOOLS):
- flags: the red flags of the latest read, or one kind's whole list (out of date,
  2+ versions on one PC, Windows updates ...): from the latest read.
- versions: one program's versions, PCs on each, most / newest / oldest, behind.
- version_problems: an application's versions with the PCs that ran each and
  those with a crash, hang time or a Major action (flag 1), raised or not.
- compare, before_after, program_split, pcs, pcs_without_update: read from
  Aternity now, as a click on the tab does (`software.compare`, `before_after`,
  `split_by_program`, `pcs_with_program`, `pcs_with_problem`,
  `pcs_without_update`); program_split is never a red flag.
Names are matched to the latest read (`pick`): exactly, else the one name that
has the words asked, else the one that ends with them ("Edge": Microsoft Edge).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .chat import ROWS, SHAPE, _n, _result, _tool, converse
from .config import NO_PROGRAM, SOFTWARE_OS_UPDATE, TOP_STATUS
from .http import ApiError
from .software import (DAYS, FEW_PCS, KIND, MIN_PCS, MIN_PROBLEM, RATIO, UPDATE_DAYS, Software, app_now, before_after,
                       crashes_by_version, named_like, words,
                       compare, missing_on, order, pcs_with_problem, pcs_with_program, pcs_without_update,
                       program_view, shown, split_by_program, starts_with, version_of)

LIKE_NOW = 5  # applications named like what was asked that are read now, at most
PCS_ROWS = 20  # PCs given to the AI and shown in a list; the tab's lists show more
PROBLEM = {"crash": "a crash", "hang": "hang time", "slow": f"a {TOP_STATUS} action"}

APP = {"type": "string", "description": "An application as Aternity names it, e.g. Microsoft Outlook."}
PROG = {"type": "string", "description": "An installed program's name, e.g. Microsoft Edge."}
VER = {"type": "string", "description": "A version, or how it starts, e.g. 143 or 16.0.20430.20180."}
PROB = {"type": "string", "enum": ["crash", "hang", "slow"],
        "description": "Crashes, hang time, or actions Aternity rated Major (slow)."}

TOOLS = [
    # "all" first and required: with only the kinds offered, GPT answered "What are the red flags?"
    # with the crash flags alone (stage, Nov 2026)
    _tool("flags", "The red flags across all installed software and application versions, or one kind's whole list. "
                   "Not for a question about one named application or program: use version_problems, versions, "
                   "compare or pcs for those. Not for which application crashed the most: use most.", {
        "kind": {"type": "string", "enum": ["all"] + list(KIND),
                 "description": "all = every red flag, one line each: use it unless the question asks for one kind. "
                                "One kind, with its whole list: crash, hang, slow = application versions with "
                                "more crashes, hang time or Major slow actions than their other versions; windows = "
                                "Windows updates not on every PC; multi = 2 or more versions of a program on one PC; "
                                "behind = programs with PCs on a version older than most PCs have (out of date); "
                                "user = installed for one user only; few = programs on few PCs."}}, ("kind",)),
    # GPT answered "which software crashed the most?" from the crash red flags (Outlook, 200 PCs),
    # which leave out an application that is not compared: Trellix ENS, 415 PCs (stage, Nov 2026)
    _tool("most", "Which applications have the most PCs with a crash, hang time or Major slow actions in the last "
                  f"{DAYS} days: a plain ranking of every application, raised or not. Use it for \"which software, "
                  "application or program crashed (hung, was slow) the most\". Not the red flags: those compare an "
                  "application's own versions.", {"problem": PROB}),
    _tool("versions", "One installed program's versions: the PCs on each, the version most PCs have, the newest, the "
                      "oldest, and the PCs behind.", {"program": PROG}, ("program",)),
    # Nemotron took "Does one Outlook version crash more than the others?" to the crash flags of
    # every application (stage, Nov 2026): a named application's question is said to be this one
    _tool("version_problems", f"One application's versions with the PCs that ran each and the PCs with a crash, hang "
                              f"time or Major slow actions, over the last {DAYS} days, and which version is raised. "
                              "Use it when the question names an application and asks whether one of its versions "
                              "crashes, hangs or is slower than the others. With no application, the applications "
                              "with the most.", {"application": APP}),
    _tool("compare", "Two versions of an application side by side on every kind of issue (crashes, hang time, health "
                     "events, Major slow actions, its busiest activities), read now.",
          {"application": APP, "a": VER, "b": VER}, ("application",)),
    _tool("before_after", f"The same PCs before and after an update of an application (its version changed once in "
                          f"the last {UPDATE_DAYS} days): days with a crash or hang time on each side, read now.",
          {"application": APP}, ("application",)),
    _tool("program_split", "An application's crashes, hang time or Major slow actions split by the version of "
                           "another installed program on the same PCs (for example Outlook crashes by Teams Meeting "
                           "Add-in version), read now.",
          {"application": APP, "program": PROG, "problem": PROB}, ("application", "program")),
    _tool("pcs", "The PCs with an installed program (at a version), or the PCs with a crash, hang time or Major slow "
                 "action on an application version; each PC can be investigated.",
          {"program": PROG, "version": VER, "application": APP, "problem": PROB}),
    _tool("pcs_without_update", "The PCs missing a Windows update (e.g. a KB security update) that most PCs of their "
                                "Windows version have, read now, with what they have in its place; each PC can be "
                                "investigated.",
          {"update": {"type": "string", "description": "The Windows update, e.g. KB5126652."}}, ("update",)),
]

SYSTEM = ("You answer questions from IT staff and management about the software installed on the company's PCs and "
          "how each application version behaves, from Aternity. Numbers come only from the tools: call exactly one "
          "tool to answer: the one for what the question names (one application or program, else the red flags of "
          "all). If no tool fits the question, say so in one sentence and call no tool. Never state a "
          "number a tool did not give, and do not work out new ones (no sums, no percentages, no ratios). When the "
          "counts come back, answer as a helpful colleague would, " + SHAPE + ". Say that a version crashes, hangs or "
          "is slower more than the others only "
          "when a row or note says it is raised (a red flag); otherwise give the counts side by side without judging "
          "them. Never call a count high, low, good or bad yourself. Name what stands out: the program, version, "
          "Windows update or PC at the top. End with one next step that fits: a follow-up question to ask here (for "
          "example compare two versions, before and after an update, which PCs), or Investigate a PC. Use only the "
          "few numbers that matter, written exactly as given.")
NO_COUNT = ("I can only give numbers from the counts: the red flags, a program's versions, an application's crashes, "
            "hang time and slow actions per version, two versions side by side, before and after an update, "
            "which PCs, or which PCs lack a Windows update.")
RULES = (f"Raised (a red flag): a version whose share is at least {RATIO:g} times its other versions' share, with "
         f"{MIN_PCS} PCs or more on each side and {MIN_PROBLEM} or more with the problem; not raised: no difference "
         "by these rules.")


def pick(asked, names) -> tuple[str | None, list[str]]:
    """The one name meant (exactly, any case; else the one that has the words asked; else
    the one of those that ends with them), or (None, the names that have them)."""
    q = " ".join(str(asked or "").lower().split())
    if not q:
        return None, []
    names = list(dict.fromkeys(n for n in names if n))
    low = {n: " ".join(str(n).lower().split()) for n in names}
    exact = [n for n in names if low[n] == q]
    if exact:
        return exact[0], []
    has = ([n for n in names if q in low[n]] or [n for n in names if all(w in low[n] for w in q.split())]
           or [n for n in names if named_like(n, asked)])  # "Microsoft Edge" finds a name with "edge" alone
    if len(has) == 1:
        return has[0], []
    ends = [n for n in has if low[n].endswith(" " + q)]
    if len(ends) == 1:
        return ends[0], []
    return None, sorted(has, key=str.lower)


def _period(view: dict) -> str:
    return f"the latest read of the installed list, {view.get('as_of_local')} ({view.get('zone')})"


def _live(days: int = DAYS) -> str:
    return f"the last {days} days, read from Aternity now"


def _not_one(block: str, args: dict, what: str, asked, names: list[str], period: str) -> dict:
    """No name, or several, for what was asked: the names to choose from."""
    if not asked:
        return _result(block, args, f"No {what} named", period, [what.capitalize()], [], [f"Name the {what}."],
                       say=f"Name the {what}, and ask again.")
    if not names:
        return _result(block, args, f"No {what} named like \"{asked}\"", period, [what.capitalize()], [],
                       [f"No {what} named like \"{asked}\" in the latest read."],
                       say=f"No {what} named like \"{asked}\" in the latest read; ask with its full name.")
    rows = [[n] for n in names[:ROWS]]
    return _result(block, args, f"{len(names):,} {what}s named like \"{asked}\"", period, [what.capitalize()], rows,
                   [f"No {what} is named exactly \"{asked}\"; several have it in their name: ask again with one "
                    "of these names."],
                   say=f"Several {what}s have \"{asked}\" ({'; '.join(names[:3])}): which one?")


# -- from the latest read ------------------------------------------------------------
def flags(sw: Software, view: dict, args: dict, kind: str | None = None) -> dict:
    period, snap = _period(view), sw.snap or {}
    kind = kind if kind in KIND else None
    if kind is None:
        fl = view.get("flags", [])
        rows = [[f["label"], f["text"], f["pcs"], "new" if f.get("new") else ""] for f in fl[:ROWS]]
        say = ("No red flag in the latest read." if not fl else
               f"Red flags: {len(fl):,}; most PCs: " + " ".join(f"{f['label']}: {f['text']}" for f in fl[:2]))
        return _result("flags", args, "Red flags", period, ["Red flag", "What", "PCs", "New since the day before"],
                       rows, [RULES], total=_n(len(fl), "red flag"), say=say)
    label = KIND[kind]
    if kind in ("crash", "hang", "slow"):
        rows, only = [], []
        for a in snap.get("versions", {}).values():
            rows += [[a["application"], r["version"], r["n"], r["of"], r["others_n"], r["others_of"]]
                     for r in a["raised"] if r["kind"] == kind]
            if kind in ("crash", "hang") and a.get("only"):  # neither is compared (software.judge)
                only.append(a["application"])
        rows.sort(key=lambda r: (-r[2], r[0].lower()))
        unit = "actions" if kind == "slow" else "PCs"
        with_ = {"crash": "PCs with a crash", "hang": "PCs with hang time", "slow": f"Actions rated {TOP_STATUS}"}[kind]
        notes = [RULES] + ([f"Not compared: {', '.join(sorted(only))}: every PC listed had a crash or hang time "
                            "(app usage holds it only where it failed)."] if only else [])
        say = (f"{label}: " + "; ".join(f"{r[0]} {r[1]}: {r[2]:,} of {r[3]:,} {unit}, its other versions {r[4]:,} of "
                                        f"{r[5]:,}" for r in rows[:3]) + "." if rows else
               f"No application version is raised for {PROBLEM[kind]} in the latest read, by the rules of the tab.")
        return _result("flags", args, f"{label}: raised versions", f"the last {DAYS} days, as of the latest read",
                       ["Application", "Version", with_, f"of {unit}", f"Its other versions: {with_.lower()}",
                        f"of {unit}"], rows[:ROWS], notes, total=f"{len(rows):,} raised", say=say)
    progs = [program_view(p) for p in snap.get("programs", {}).values()]
    apps = [p for p in progs if p["type"] != SOFTWARE_OS_UPDATE]
    if kind == "behind":
        b = sorted((p for p in apps if p["behind"]), key=lambda p: (-p["behind"], p["name"].lower()))
        rows = [[p["name"], p["behind"], p["pcs"], p["most"], p["newest"]] for p in b[:ROWS]]
        cols = ["Program", "PCs behind", "of PCs", "Version most PCs have", "Newest"]
        notes = ["Behind: on a version older than the one most of the program's PCs have; a PC that also has that "
                 "version is not behind."]
        say = (f"Programs with PCs behind: {len(b):,}; most: " + "; ".join(
            f"{r[0]}, {r[1]:,} of {r[2]:,} PCs older than {r[3]}" for r in rows[:3]) + ".") if b else \
            "No program has PCs on a version older than the one most of its PCs have."
        total = _n(len(b), "program")
    elif kind == "multi":
        m = sorted((p for p in apps if p["multi"] and p["multi"]["pcs"]), key=lambda p: (-p["multi"]["pcs"], p["name"]))
        rows = [[p["name"], p["multi"]["pcs"], p["pcs"], p["multi"]["combos"][0][0] if p["multi"]["combos"] else None]
                for p in m[:ROWS]]
        cols, notes = ["Program", "PCs with 2+ versions", "of PCs", "Most often together"], []
        say = (f"Programs with 2 or more versions on one PC: {len(m):,}; most: " + "; ".join(
            f"{r[0]} on {r[1]:,} of {r[2]:,} PCs" for r in rows[:3]) + ".") if m else \
            "No program has 2 versions on one PC in the latest read."
        total = _n(len(m), "program")
    elif kind == "user":
        u = sorted((p for p in progs if p["user"]), key=lambda p: (-p["user"], p["name"].lower()))
        rows = [[p["name"], p["user"], p["pcs"]] for p in u[:ROWS]]
        cols, notes = ["Program", "PCs with it for one user", "of PCs"], []
        say = (f"Programs installed for one user only: {len(u):,}; most: " + "; ".join(
            f"{r[0]} on {_n(r[1], 'PC')}" for r in rows[:3]) + ".") if u else "No program is installed for one user only."
        total = _n(len(u), "program")
    elif kind == "windows":
        win = snap.get("windows") or {}
        gaps = sorted(((m, w) for w in win.get("versions", []) for m in w["missing"]),
                      key=lambda x: (-x[0]["without"], x[0]["update"]))
        rows = [[w["windows"], m["update"], m["without"], w["pcs"],
                 m["instead"][0]["update"] if m["instead"] else None, m["instead"][0]["on"] if m["instead"] else None]
                for m, w in gaps[:ROWS]]
        cols = ["Windows", "Update", "PCs without it", "of PCs", "What most of those have", "on PCs"]
        notes = ["An update on more than half the PCs of its Windows version and not on every one; a later update "
                 "may be in its place (what most of those have)."]
        say = (f"Windows updates not on every PC of their Windows version: {len(gaps):,}; most: " + "; ".join(
            f"{r[1]} not on {r[2]:,} of the {r[3]:,} PCs on {r[0]}" for r in rows[:2]) + ".") if gaps else \
            "Every Windows update on most PCs of a Windows version is on all of them."
        total = _n(len(gaps), "update")
    else:  # few
        tail = view.get("tail", [])
        rows = [[t["label"], t["programs"]] for t in tail]
        cols = ["PCs a program is on", "Programs"]
        few = sum(1 for p in apps if p["pcs"] <= FEW_PCS)
        notes = [f"{few:,} of the {len(apps):,} programs are on {FEW_PCS} PCs or fewer."]
        say = notes[0]
        total = _n(len(apps), "program")
    return _result("flags", args, label, period, cols, rows, notes, total=total, say=say)


def versions(sw: Software, view: dict, args: dict, program: str | None = None) -> dict:
    period = _period(view)
    name, names = pick(program, (sw.snap or {}).get("programs", {}))
    if name is None:
        return _not_one("versions", args, "program", program, names, period)
    p = sw.program(name)
    vs = sorted(p["versions"], key=lambda x: (-x[1], order(x[0])))
    shownv = vs[:ROWS] + [x for x in p["versions"] if x[0] in (p["newest"], p["oldest"]) and x not in vs[:ROWS]]
    tag = lambda v: ", ".join(w for w, k in (("most PCs", p["most"]), ("newest", p["newest"]),  # noqa: E731
                                             ("oldest", p["oldest"])) if v == k)
    rows = [[shown(v), n, tag(v)] for v, n in shownv]
    notes = [f"{_n(len(vs), 'version')} on {_n(p['pcs'], 'PC')} ({_n(p['installs'], 'install')})."]
    if p["behind"] is not None:
        notes.append(f"Behind: {p['behind']:,} PCs on a version older than {p['most']}, the one most PCs have.")
    if not p["dotted"]:
        notes.append("Not every version is a dotted number, so none is called newest, oldest or behind.")
    if p["multi"] and p["multi"]["pcs"]:
        notes.append(f"2 or more versions on one PC: {p['multi']['pcs']:,} PCs.")
    if p["user"]:
        notes.append(f"Installed for one user only on {p['user']:,} PCs.")
    say = (f"{name}: {_n(len(vs), 'version')} on {_n(p['pcs'], 'PC')}; most PCs ({vs[0][1]:,}) have "
           f"{shown(vs[0][0])}"
           + (f"; newest {p['newest']}, oldest {p['oldest']}; {p['behind']:,} PCs behind" if p["behind"] is not None
              else "") + ".")
    return _result("versions", args, f"{name}: its versions", period, ["Version", "PCs", ""], rows, notes,
                   total=_n(p["pcs"], "PC"), say=say)


def version_problems(sw: Software, view: dict, args: dict, application: str | None = None, client_of=None,
                     known=(), now: datetime | None = None) -> dict:
    apps = {a["application"]: a for a in view.get("versions", [])}
    period = f"the last {DAYS} days, as of the latest read ({view.get('as_of_local')})"
    if not application:
        rows = [[a["application"], len(a["versions"]), sum(v["crash"] for v in a["versions"]),
                 sum(v["hang"] for v in a["versions"]), sum(v["major_pcs"] for v in a["versions"]), a["raised"]]
                for a in list(apps.values())[:ROWS]]
        say = ("Applications read per version, most problems first: " + "; ".join(
            f"{r[0]}: {r[1]} versions, a crash on {r[2]:,} PCs, {r[5]} raised" for r in rows[:3]) + ".") if rows else \
            "No application had 2 versions or more with a problem in the latest read."
        return _result("version_problems", args, "Applications by version", period,
                       ["Application", "Versions", "PCs with a crash", "PCs with hang time",
                        f"PCs with a {TOP_STATUS} action", "Versions raised"], rows,
                       [RULES, "Read per version: the applications with 2 versions or more and a crash, hang time or "
                               f"{MIN_PROBLEM} {TOP_STATUS} actions."], total=_n(len(apps), "application"), say=say)
    # one application only when nothing else is named like it: "outlook" is Outlook, but "edge"
    # is not Microsoft Edge Update just because it is the only one read per version
    q = " ".join(str(application).lower().split())
    like = [n for n in apps if " ".join(n.lower().split()) == q] or [n for n in apps if named_like(n, application)]
    other = {e["application"] or e["part"] for e in (sw.snap or {}).get("crashes") or []
             if any(x and named_like(x, application) for x in (e["application"], e["part"]))} - set(like)
    if len(like) == 1 and (not other or " ".join(like[0].lower().split()) == q):
        return _by_version(args, apps[like[0]], period, [])
    # not one of the applications read every day: the application as Aternity names it, read now
    # (stage, Nov 2026: "Microsoft Edge" was answered with "several applications have Microsoft
    # Edge: ask with the full name", naming Microsoft Edge Update and Microsoft Edge WebView2)
    daily = (f"not among the {len(apps)} applications read every day (2 versions or more and a crash, hang time "
             f"or {MIN_PROBLEM} {TOP_STATUS} actions in {DAYS} days)")
    name, names = pick(application, list(apps) + list(known or ()))
    several = name is None and bool(names)  # then Aternity is asked for the name exactly as typed
    name = name or " ".join(str(application).split())
    client = client_of() if client_of else None
    if client is None:
        if several:
            return _not_one("version_problems", args, "application", application, names, period)
        return _result("version_problems", args, f"{name}: not read per version", period, ["Application"], [],
                       [f"{name} is {daily}."], say=f"{name} is {daily}.")
    a = app_now(client, now or datetime.now(timezone.utc), name)
    if not a["versions"] and not a["error"]:
        # no application of exactly that name: everything named like it, with its counts (never
        # only a list of names to ask again with: the user's finding on stage, three times)
        like = _crashes_like(args, sw, view, client, now or datetime.now(timezone.utc), application, name)
        if like is not None:
            return like
    if not a["versions"]:
        why = (f"Aternity could not be read ({a['error']}): ask again in a minute." if a["error"] else
               f"Aternity recorded no crash in the last {DAYS} days, in its application usage or its crash events, "
               f"of an application or program file with \"{' '.join(words(application))}\" in its name.")
        notes = [why] + a["notes"]
        if not a["error"] and any(named_like(p, application) for p in view.get("programs", [])):
            notes.append("The installed list has programs of that name: ask how many versions are installed.")
        return _result("version_problems", args, f"{name}: no crash recorded", _live(), ["Application"], [], notes,
                       say=" ".join(notes[:2]))
    return _by_version(args, a, _live(), [f"Read now: {name} is {daily}."] + a["notes"])


def most(sw: Software, view: dict, args: dict, problem: str = "crash") -> dict:
    """Every application by its PCs with the problem, most first: a plain count, each PC once."""
    problem = problem if problem in PROBLEM else "crash"
    snap, rows, only, unnamed = sw.snap or {}, [], [], 0
    for a in view.get("versions", []):
        t = a.get("totals") or {"pcs": sum(v["pcs"] for v in a["versions"]),  # a read from before totals
                                "crash": sum(v["crash"] for v in a["versions"]),
                                "hang": sum(v["hang"] for v in a["versions"]),
                                "action_pcs": sum(v["action_pcs"] for v in a["versions"]),
                                "slow": sum(v["major_pcs"] for v in a["versions"])}
        if not t[problem] or ("actions" if problem == "slow" else "usage") not in a["read"]:
            continue
        key = "major_pcs" if problem == "slow" else problem
        top = max(a["versions"], key=lambda v: v[key])
        rows.append([a["application"], t[problem], t["action_pcs" if problem == "slow" else "pcs"],
                     f"{top['version']} ({top[key]:,})"])
        if problem in a["only"]:
            only.append(a["application"])
    if problem == "crash":  # and what only Aternity's crash events hold (one version, a program file)
        have = {r[0] for r in rows}
        best: dict[str, dict] = {}
        for e in snap.get("crashes") or []:
            name = e["application"] or e["part"]
            if name and (name not in best or e["pcs"] > best[name]["pcs"]):
                best[name] = e
        for name, pcs in (snap.get("crash_names") or {}).items():
            if name.lower() in NO_PROGRAM:  # not a program: Windows could not tell which one failed
                unnamed += pcs
            elif name not in have and pcs:
                e = best.get(name)
                rows.append([name, pcs, None, f"{e['version'] or '(no version)'} ({e['pcs']:,})" if e else None])
    rows.sort(key=lambda r: (-r[1], str(r[0]).lower()))
    what = PROBLEM[problem]
    notes = [f"A plain count, each PC once, most first: every application with {what} in the last {DAYS} days, "
             "raised or not. A red flag is another question (one version against the application's other versions)."]
    if only:
        notes.append(f"{', '.join(only[:5])}{' …' if len(only) > 5 else ''}: every PC Aternity lists had {what} "
                     "(app usage holds these only where they failed), so \"PCs that ran it\" is not all its PCs.")
    if problem == "crash":
        notes.append("\"PCs that ran it\" is empty where the count is from Aternity's crash events alone (an "
                     "application on one version, or a program file with no application name).")
        if unnamed:
            notes.append(f"Not listed, as it is not a program: {_n(unnamed, 'PC')} had a crash where Windows could "
                         f"not tell which program failed (Aternity names it {NO_PROGRAM[0]}).")
        if snap.get("crashes") is None:
            notes.append("The crash events were not read with the latest read: only applications with 2 versions "
                         "or more are listed.")
    if len(rows) > ROWS:
        notes.append(f"The {ROWS} with the most PCs are shown, of {len(rows)}.")
    say = (f"Most PCs with {what}, last {DAYS} days: " + "; ".join(f"{r[0]}: {_n(r[1], 'PC')}" for r in rows[:3])
           + ".") if rows else f"No application had {what} in the last {DAYS} days, by the latest read."
    return _result("most", args, f"Applications with the most PCs with {what}",
                   f"the last {DAYS} days, as of the latest read ({view.get('as_of_local')})",
                   ["Application or program file", f"PCs with {what}", "PCs that ran it", "Version with the most (PCs)"],
                   rows[:ROWS], notes, total=_n(len(rows), "application"), say=say)


def _crashes_like(args: dict, sw: Software, view: dict, client, now: datetime, asked, name: str) -> dict | None:
    """Crashes per version of everything whose name has the words asked for: the applications
    read per version (app usage: with the PCs that ran each) and every crash event of the daily
    read, by application or by the program file that crashed (msedge.exe has "edge"). None:
    nothing is named like it."""
    rows = []
    for a in view.get("versions", []):
        if named_like(a["application"], asked) and "usage" in a["read"]:
            rows += [[a["application"], v["version"], v["crash"], v["pcs"]] for v in a["versions"] if v["crash"]]
    # applications in app usage with a crash that the daily read did not read per version: read now
    daily = {a["application"] for a in view.get("versions", [])}
    more = sorted((n for n, crashes in ((sw.snap or {}).get("usage") or {}).items()
                   if crashes and n not in daily and named_like(n, asked)), key=str.lower)[:LIKE_NOW]
    for n in more:
        rows += [[n, v["version"], v["crash"], v["pcs"]] for v in app_now(client, now, n)["versions"] if v["crash"]]
    in_usage = {r[0] for r in rows} | daily & {a for a in daily if named_like(a, asked)}
    events = (sw.snap or {}).get("crashes")
    if events is None:  # a read from before this was kept: the exact name's events, read now
        ev = crashes_by_version(client, now, name)
        events = [{"application": name, "part": ev["part"], "version": r["version"], "pcs": r["pcs"],
                   "events": r["events"], "kinds": ev["kinds"]} for r in ev["rows"]]
    kinds: set = set()
    for e in events:
        who = [x for x in (e["application"], e["part"]) if x and named_like(x, asked)]
        if not who or e["application"] in in_usage or (e["part"] or "").lower() in NO_PROGRAM:
            continue
        label = e["application"] if e["application"] and e["part"] in (None, e["application"]) else \
            f"{e['application']} ({e['part']})" if e["application"] else e["part"]
        rows.append([label, e["version"] or "(no version)", e["pcs"], None])
        kinds |= set(e["kinds"])
    if not rows:
        return None
    rows.sort(key=lambda r: (-r[2], str(r[0]).lower(), order(str(r[1]))))
    top, names = rows[0], list(dict.fromkeys(r[0] for r in rows))
    word = " ".join(words(asked))
    notes = [f"Aternity has no application named exactly \"{name}\": this is every crash of the last {DAYS} days "
             f"with \"{word}\" in the name of the application or of the program file that crashed "
             f"({_n(len(names), 'name')}: {', '.join(names[:5])}{' …' if len(names) > 5 else ''}).",
             f"The most PCs with a crash: {top[0]} {top[1]}, {_n(top[2], 'PC')}.",
             "\"PCs that ran it\" is known only for applications in Aternity's application usage; where it is "
             "empty the count is from crash events alone" + (f" ({', '.join(sorted(kinds)[:3])})" if kinds else "")
             + ". Counts, not shares: a version on more PCs has more crashes by that alone, and none is raised."]
    if len(rows) > ROWS:
        notes.append(f"The {ROWS} with the most PCs are shown, of {len(rows)}.")
    say = (f"Crashes named like \"{word}\", last {DAYS} days, most PCs first: " + "; ".join(
        f"{r[0]} {r[1]}: {_n(r[2], 'PC')}" for r in rows[:3]) + ".")
    return _result("version_problems", args, f"Crashes by version: names with \"{word}\"",
                   f"the last {DAYS} days, as of the latest read ({view.get('as_of_local')})",
                   ["Application or program file", "Version", "PCs with a crash", "PCs that ran it"], rows[:ROWS],
                   notes, total=f"{_n(len(rows), 'version')} with a crash", say=say)


def _by_version(args: dict, a: dict, period: str, first: list[str]) -> dict:
    name = a["application"]
    usage, acts = "usage" in a["read"], "actions" in a["read"]
    rows = [[v["version"], v["pcs"] if usage else None, v["crash"] if usage else None, v["hang"] if usage else None,
             v["actions"] if acts else None, v["major"] if acts else None, ", ".join(KIND[k] for k in v["raised"])]
            for v in sorted(a["versions"], key=lambda v: (-max(v["pcs"], v["action_pcs"]), order(v["version"])))[:ROWS]]
    notes = first + [RULES]
    if a["only"]:
        notes.append(f"Every PC listed for {name} had a crash or hang time: app usage holds it only where it failed, "
                     "so no share is compared and no version is raised for either.")
    raised = [f"{r[0]} ({r[6]})" for r in rows if r[6]]
    most = max(rows, key=lambda r: r[2] or 0) if usage else None
    say = (f"{name}, last {DAYS} days: " + "; ".join(
        f"{r[0]}: a crash on {r[2]:,} of {r[1]:,} PCs" if usage else f"{r[0]}: {r[5]:,} of {r[4]:,} actions rated "
        f"{TOP_STATUS}" for r in rows[:3]) + (f"; raised: {', '.join(raised)}." if raised else "; none raised."))
    if most and most[2]:
        notes.append(f"The most PCs with a crash: version {most[0]}, {most[2]:,} of the {most[1]:,} PCs that ran it "
                     "(by count; a version on more PCs has more crashes by that alone).")
    elif usage:
        notes.append(f"No PC had a crash of {name} on any version in the last {DAYS} days.")
    return _result("version_problems", args, f"{name} by version", period,
                   ["Version", "PCs that ran it", "with a crash", "with hang time", "Actions", f"rated {TOP_STATUS}",
                    "Raised"], rows, notes, total=_n(len(a["versions"]), "version"), say=say)


def _app(view: dict, known, asked) -> tuple[str | None, list[str]]:
    """An application as Aternity names it: matched to those in the latest read and today's.
    A name no one has, or that several have in theirs, is kept as asked: Aternity is asked for
    it exactly, and the names like it are told when it has nothing (`_like`). "Microsoft Edge"
    once ended at "several applications have it: ask with the full name" (stage, Nov 2026)."""
    name, names = pick(asked, list(view.get("applications", [])) + list(known or ()))
    if name is None and asked:
        return " ".join(str(asked).split()), names
    return name, names


def _like(name: str, names: list[str]) -> list[str]:
    """For a read that found nothing under a name kept as asked: the names like it."""
    return [f"No application is named exactly \"{name}\" in the latest read or today's activity; names like it: "
            f"{', '.join(names[:5])}{' …' if len(names) > 5 else ''}. Ask again with one of them."] if names else []


def compare_block(client, cfg, now: datetime, view: dict, known, args: dict, application=None, a=None, b=None):
    name, names = _app(view, known, application)
    if name is None:
        return _not_one("compare", args, "application", application, names, _live())
    c = compare(client, cfg, now, name, a, b)
    if c["a"] is None:
        notes = c["notes"] + (_like(name, names) if not c["versions"] else [])
        return _result("compare", args, f"{name}: nothing to compare", _live(), ["What"], [], notes,
                       say=" ".join(notes) or f"{name}: nothing to compare.")
    A, B = c["a"], c["b"]
    rows = [[f"{x['what']}: actions rated {TOP_STATUS}" if x["group"] == "activity" else x["what"],
             x["a"][0], x["a"][1], x["b"][0], x["b"][1]] for x in c["lines"]]
    notes = list(c["notes"])
    if c.get("kind"):
        k = c["kind"]
        notes.append(f"On virtual desktops: {k['a'][0]:,} of {k['a'][1]:,} PCs on {A}, {k['b'][0]:,} of "
                     f"{k['b'][1]:,} on {B}.")
    asked = [v for v in (a, b) if v and version_of(v, c["versions"]) is None]
    if asked:
        notes.append(f"Not one version of {name} in the last {DAYS} days for {', '.join(asked)}: compared the two "
                     "with the most PCs instead.")
    notes.append("Side by side, not judged: the red flags use the rules of the Software tab.")
    say = (f"{name} {A} against {B}, last {DAYS} days: " + "; ".join(
        f"{r[0]}: {r[1]:,} of {r[2]:,} against {r[3]:,} of {r[4]:,}" for r in rows[:3]) + ".")
    return _result("compare", args, f"{name}: {A} against {B}", _live(),
                   ["What", f"{A}: with it", "of", f"{B}: with it", "of"], rows, notes,
                   total=f"{_n(len(c['versions']), 'version')} ran", say=say)


def before_after_block(client, now: datetime, view: dict, known, args: dict, application=None):
    name, names = _app(view, known, application)
    if name is None:
        return _not_one("before_after", args, "application", application, names, _live(UPDATE_DAYS))
    ba = before_after(client, now, name)
    rows = [[p["from"], p["to"], p["pcs"], p["before"][0], p["before"][1], p["before"][2], p["after"][0],
             p["after"][1], p["after"][2]] for p in ba["pairs"]]
    notes = list(ba["notes"]) + ["PC-days: each PC's days on each side, added up; a day with 2 versions is on "
                                 "neither side."]
    if ba["pairs"]:
        p = ba["pairs"][0]
        notes.append(f"{p['from']} to {p['to']}: changed between {p['first']} and {p['last']}.")
        say = (f"{name}, last {UPDATE_DAYS} days: {p['pcs']:,} PCs went from {p['from']} to {p['to']}; before: a "
               f"crash on {p['before'][1]:,} of {p['before'][0]:,} PC-days; after: {p['after'][1]:,} of "
               f"{p['after'][0]:,}.")
    else:
        say = f"No PC changed its version of {name} once in the last {UPDATE_DAYS} days."
        notes += _like(name, names)
    return _result("before_after", args, f"{name}: before and after an update", _live(UPDATE_DAYS),
                   ["From", "To", "PCs", "PC-days before", "with a crash", "with hang time", "PC-days after",
                    "with a crash", "with hang time"], rows, notes, total=_n(sum(r[2] for r in rows), "PC"), say=say)


def program_split(client, now: datetime, sw: Software, view: dict, known, args: dict, application=None,
                  program=None, problem="crash"):
    name, names = _app(view, known, application)
    if name is None:
        return _not_one("program_split", args, "application", application, names, _live())
    prog, pnames = pick(program, (sw.snap or {}).get("programs", {}))
    if prog is None:
        return _not_one("program_split", args, "program", program, pnames, _live())
    r = split_by_program(client, now, name, prog, problem)
    slow = r["problem"] == "slow"
    rows = [[shown(x["version"]) if x["version"] is not None else f"without {prog}", x["pcs"],
             *((x["actions"], x["major"]) if slow else (x["with"],))] for x in r["rows"]]
    if len(rows) > ROWS:
        rows = rows[:ROWS - 1] + [rows[-1]] if r["rows"][-1]["version"] is None else rows[:ROWS]
    cols = ["Version of " + prog, f"PCs that ran {name}"] + (
        ["Actions", f"rated {TOP_STATUS}"] if slow else [f"with {PROBLEM[r['problem']]}"])
    notes = list(r["notes"]) + [f"A PC with 2 versions of {prog} counts under each.",
                                "Not judged and never a red flag: split by another program's version, some "
                                "differences come by chance. Compare versions or Investigate a PC before acting."]
    unit = "actions rated " + TOP_STATUS if slow else "PCs with " + PROBLEM[r["problem"]]
    say = (f"{name} on the PCs of each {prog} version, last {DAYS} days: " + "; ".join(
        f"{x[0]}: {x[3] if slow else x[2]:,} {unit}, of {x[2] if slow else x[1]:,} {'actions' if slow else 'PCs'}"
        for x in rows[:3]) + ".") if rows else f"No PC ran {name} in the last {DAYS} days."
    if not r["pcs"]:
        notes = _like(name, names) + notes
    return _result("program_split", args, f"{name}: {PROBLEM[r['problem']]} by {prog} version", _live(), cols, rows,
                   notes, total=f"{_n(r['pcs'], 'PC')} ran {name}", say=say)


def pcs(client, now: datetime, sw: Software, view: dict, known, args: dict, program=None, version=None,
        application=None, problem=None):
    by_problem = bool(application) and (problem in PROBLEM or not program)
    if by_problem:
        problem = problem if problem in PROBLEM else "crash"
        name, names = _app(view, known, application)
        if name is None:
            return _not_one("pcs", args, "application", application, names, _live())
        mine = next((a for a in view.get("versions", []) if a["application"] == name), None)
        vs = [v["version"] for v in mine["versions"]] if mine else []
        ver = version_of(version, vs) if vs else (version or None)
        if not ver:
            rows = [[v] for v in sorted(vs, key=order)[:ROWS]]
            return _result("pcs", args, f"{name}: which version?", _live(), ["Version"], rows,
                           ["Name the version: the list holds one version's PCs."] + _like(name, names),
                           say=f"Which version of {name}? " + (f"Its versions: {', '.join(r[0] for r in rows[:5])}."
                                                               if rows else "Name it in full."))
        got = pcs_with_problem(client, now, name, ver, problem)
        rows, runs = [], []
        for x in got["rows"][:PCS_ROWS]:
            last = datetime.fromisoformat(x["last"])
            rows.append([x["device"], x["username"], x["days"], x["count"], f"{last:%b %d}"])
            runs.append({"device": x["device"], "username": x["username"] or "", "start": last.isoformat(),
                         "end": min(now, last + timedelta(days=1)).isoformat()})
        cols = ["PC", "User", "Days with it", "Crashes" if problem == "crash" else f"{TOP_STATUS} actions"
                if problem == "slow" else "(no unit)", "Last day"]
        if problem == "hang":
            rows, cols = [r[:3] + r[4:] for r in rows], cols[:3] + cols[4:]
    else:
        name, names = pick(program, (sw.snap or {}).get("programs", {}))
        if name is None:
            return _not_one("pcs", args, "program", program, names, _period(view))
        p = sw.program(name)
        vs = [v for v, _ in p["versions"]]
        ver = version_of(version, vs) if version else None
        prefix = bool(version) and ver is None and any(starts_with(v, str(version).strip()) for v in vs)
        if version and ver is None and not prefix:
            rows = [[shown(v)] for v in sorted(vs, key=order)][-ROWS:]
            return _result("pcs", args, f"{name}: no version {version}", _period(view), ["Version"], rows,
                           [f"No PC has {name} {version} in the latest read; its newest versions are listed."],
                           say=f"No PC has {name} {version} in the latest read.")
        got = pcs_with_program(client, name, str(version).strip() if prefix else ver, prefix=prefix)
        rows = [[x["device"], x["version"]] for x in got["rows"][:PCS_ROWS]]
        runs = [{"device": x["device"], "start": (now - timedelta(hours=4)).isoformat(), "end": now.isoformat()}
                for x in got["rows"][:PCS_ROWS]]
        cols = ["PC", "Version"]
    notes = [f"The first {len(rows)} of {got['total']:,} shown; the Software tab lists more."] \
        if got["total"] > len(rows) else []
    if by_problem:
        notes.append(f"Investigate ▸ runs for the PC's last day with {PROBLEM.get(problem, 'it')}.")
    else:
        notes.append("Investigate ▸ runs for the PC's last 4 hours.")
    say = f"{got['what']}: {_n(got['total'], 'PC')}" + (f", e.g. {', '.join(r[0] for r in rows[:3])}" if rows else "") + "."
    return _result("pcs", args, got["what"], _live() if by_problem else "read from Aternity now",
                   cols, rows, notes, total=_n(got["total"], "PC"), runs=runs, say=say)


def without_update(client, cfg, now: datetime, sw: Software, view: dict, args: dict, update=None):
    period = "read from Aternity now"
    listed = sorted({m["update"] for w in ((sw.snap or {}).get("windows") or {}).get("versions", [])
                     for m in w["missing"]}, key=str.lower)
    name, names = pick(update, listed)
    if name is None:
        if update and not names:
            return _result("pcs_without_update", args, f"{update}: not listed as missing", _period(view), ["Update"],
                           [], [f"{update} is not an update on most PCs of a Windows version but not on every one, in "
                                "the latest read. Ask which PCs have it instead."],
                           say=f"{update} is not listed as missing on any Windows version in the latest read.")
        return _not_one("pcs_without_update", args, "update", update, names, _period(view))
    wins, instead = missing_on(sw.snap, name)
    got = pcs_without_update(client, cfg, name, wins, instead)
    shown_rows = got["rows"][:PCS_ROWS]
    rows = [[x["device"]] + ([x["windows"]] if len(wins) > 1 else []) + ([", ".join(x["instead"]) or None]
                                                                        if instead else []) + [x["updates"]]
            for x in shown_rows]
    cols = ["PC"] + (["Windows"] if len(wins) > 1 else []) + (["Has in its place"] if instead else []) + \
        ["Windows updates listed"]
    runs = [{"device": x["device"], "start": (now - timedelta(hours=4)).isoformat(), "end": now.isoformat()}
            for x in shown_rows]
    notes = list(got["notes"])
    if instead:
        notes.append(f"{got['with_instead']:,} of them have {' or '.join(instead)} in its place (a later update may "
                     "replace it); the others first.")
    if got["total"] > len(rows):
        notes.append(f"The first {len(rows)} of {got['total']:,} shown; the Software tab lists more.")
    notes.append("Investigate ▸ runs for the PC's last 4 hours.")
    say = (f"{name}: not on {_n(got['total'], 'PC')} of {', '.join(wins)}"
           + (f"; {got['with_instead']:,} of them have {' or '.join(instead)}" if instead else "")
           + (f"; e.g. {', '.join(r[0] for r in rows[:3])}" if rows else "") + ".")
    return _result("pcs_without_update", args, got["what"], period, cols, rows, notes, total=_n(got["total"], "PC"),
                   runs=runs, say=say)


# -- the question ---------------------------------------------------------------------
def run_block(name: str, args: dict, sw: Software, view: dict, client_of, cfg, known, now: datetime) -> dict | None:
    args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    take = lambda *keys: {k: args[k] for k in keys if k in args}  # noqa: E731
    try:
        if name == "flags":
            return flags(sw, view, args, **take("kind"))
        if name == "versions":
            return versions(sw, view, args, **take("program"))
        if name == "most":
            return most(sw, view, args, **take("problem"))
        if name == "version_problems":
            try:
                return version_problems(sw, view, args, **take("application"), client_of=client_of, known=known, now=now)
            except ApiError as exc:
                return _result(name, args, "Aternity could not be read", "now", [], [], [exc.short()],
                               say=f"Aternity could not be read ({exc.short()}): ask again in a minute.")
        if name not in ("compare", "before_after", "program_split", "pcs", "pcs_without_update"):
            return None
        client = client_of()
        if client is None:
            return _result(name, args, "Aternity is not set", "", [], [], ["aternity.base_url in config.yaml."],
                           say="Aternity is not set, so nothing can be read now.")
        try:
            if name == "compare":
                return compare_block(client, cfg, now, view, known, args, **take("application", "a", "b"))
            if name == "before_after":
                return before_after_block(client, now, view, known, args, **take("application"))
            if name == "pcs_without_update":
                return without_update(client, cfg, now, sw, view, args, **take("update"))
            if name == "program_split":
                return program_split(client, now, sw, view, known, args, **take("application", "program", "problem"))
            return pcs(client, now, sw, view, known, args, **take("program", "version", "application", "problem"))
        except ApiError as exc:
            return _result(name, args, "Aternity could not be read", "now", [], [], [exc.short()],
                           say=f"Aternity could not be read ({exc.short()}): ask again in a minute.")
    except (TypeError, ValueError, KeyError):
        return None


def system(view: dict) -> str:
    return (f"{SYSTEM} The latest read of the installed list was {view.get('as_of_local')} ({view.get('zone')}); "
            f"application versions cover the last {DAYS} days.")


def ask(question: str, history: list, client, sw: Software, client_of, cfg, known, now: datetime) -> dict:
    """One question on the Software tab: the AI picks a block, we count, it words the answer."""
    view = sw.view()
    return converse(question, history, client, system(view), TOOLS,
                    lambda name, args: run_block(name, args, sw, view, client_of, cfg, known, now), NO_COUNT)
