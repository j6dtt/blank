# Software tab: proposal

Status: agreed Nov 10, 2026 (decisions below); step 1 next. Numbers
are from stage (`uxcorrelate verify --software`, Nov 10), as
Aternity lists them. "N" marks a count not measured yet.

## How it works

Once a day the app reads two things from Aternity:
- the installed software list, which Aternity adds up per program
  and version (2,245 rows, 0.5 s);
- per application version, the PCs that ran it and the PCs that
  had a crash, a hang or Major slow actions on it. Aternity writes
  the application's version on each of these records itself.

From these it raises red flags. Each flag is a plain count of PCs
("3,408 of 5,328 PCs"), never a score. They are ranked by PCs
affected and shown at the top of the tab. A chat on the tab
("Ask about software") answers questions from the same counts. GPT
picks what to count and words the answer; every number in its
sentence must be one of our counts.

```
Aternity
 ├ INSTALLED_SOFTWARE (per program + version)
 └ app version on crashes, hangs, actions
     │ once a day and at start
     ▼
Software reader ──▶ data/software/<date>.json
     │               (counts only, no PC names)
     ▼
Software tab
 ├ Red flags, ranked by PCs ── click: details
 ├ one section per kind of flag
 └ Ask about software (GPT words, we count)
     │ click a version
     ▼
its PCs (read now, 0.5 s) ── Investigate ▸
```

## What Aternity gives

- Installed list: 5,336 PCs, 1,545 programs, 229,701 installs
  (about 43 per PC). All 12 columns are filled. Type: Application,
  OS Update (Windows updates, no version) or Update. Scope: ALL
  (for everyone on the PC) or Specific User.
- Application version on what users feel: `APPLICATION_VERSION`
  on app usage (crashes, hang time) and on actions (Aternity's
  Major), daily and hourly. These names come from Riverbed's
  $metadata. Whether the live tenant fills them, and how far back
  they go, is not checked yet (step 1).
- Not given: a vendor's latest version, known vulnerabilities,
  known conflicting programs. The live host has no internet. None of
  these is guessed.
- Aternity's `VERSION_COUNT` is 1 on every row, even on a PC with
  2 versions of a program, so it is not used.
- Not known: whether the list holds every app in Settings > Apps
  (43 per PC is few). The page says "as Aternity lists them"; the
  question goes to the Aternity admin.

## The red flags

### 1. One version crashes, hangs or is slow more than the others

Rule, per application, over 7 days:
- crash: the PCs with a crash on a version, against the PCs that
  ran it, compared with the same count on its other versions;
- hang: the same, with PCs that had hang time (Aternity gives no
  unit for hang time, so none is shown);
- slow: the share of its actions Aternity rated Major, compared
  with its other versions.

A version is raised when its share is at least 2 times the other
versions' share, with at least 20 PCs on each side and at least 5
PCs with the problem. The ratio and the minimums are settings
(decision 1); the 20 is the same as the start-up comparison.

Shape (N = not measured yet):
- Outlook 16.0.20430.20180: N of N PCs crashed; its other versions:
  N of N PCs. Raised: "crashes more than its other versions".

On demand only, never raised by itself: an application's crashes
split by the version of an installed program. Example: Outlook
crashes on the PCs of each Teams Meeting Add-in version (1,599 PCs
on 1.26.18801, 255 on 1.26.21803, 189 on 1.26.25904 …). There are
1,545 programs, so raising every such split would raise chance
findings. They are asked for in the chat or with a click.

Why it matters: it ties a version to what users feel, so you can
hold or roll back an update.

### Compare versions (any two, or all)

A view of its own, opened from flag 1, from any application, or
from the chat. Pick an application and 2 versions (or all), and each
kind of issue Aternity records is lined up side by side, as shares
of the PCs (or actions) on each version:
- PCs with a crash, and PCs with a hang;
- PCs with each kind of health event, from Aternity's own events
  (`DEVICE_HEALTH_RAW` writes the failing part's version);
- the share of actions Aternity rated Major, overall and for each
  of the application's busiest activities;
- the PCs on each version that are virtual desktops or physical PCs.
  A version used mostly on virtual desktops is not compared as if it
  ran on the same PCs.

**Before and after an update, on the same PCs.** The change log
gives when each PC moved from one version to the next (an
"Uninstalled" and an "Installed" row minutes apart). For the PCs
that updated, it counts the PCs with a crash, a hang or Major slow
actions in the 7 days before and the 7 days after. The same PCs and
users are on both sides, so the version is the difference that is
left. Shape: "Teams Meeting Add-in 1.26.08901 to 1.26.18801: N PCs
updated; Outlook crashed on N of them before, N after."

Why it matters: "it got worse after the update" can then be shown
on the same PCs, not argued.

### 2. Behind most PCs

Rule: per program, the PCs on a version older than the one most PCs
have. Newest and oldest are shown too. "Newest" means the newest
seen in the company.

On stage:
- Microsoft Edge: 20 versions on 5,328 PCs. 1,920 on
  156.0.4314.58 (the most, and the newest), so 3,408 PCs are older.
  Oldest 143.0.3650.139 (1 PC).
- Microsoft 365 Apps: 14 versions on 5,071 PCs. 3,163 on
  16.0.20430.20180 (the most, and the newest), so 1,908 are older.
- Edge WebView2 Runtime: 20 versions on 1,263 PCs. 843 on the
  newest, so 420 are older.
- Teams Meeting Add-in: 21 versions on 2,594 installs. 1,599 on
  1.26.18801 (the most); newest 1.26.25904 (189); oldest
  1.24.13006 (3).
- Firefox (Mozilla Maintenance Service): 17 versions on 408. 104 on
  158.0; newest 159.0.1 (56); oldest 123.0 (2).
- Google Chrome: 17 versions on 513. 255 on 156.0.8078.80.

Why it matters: browsers and Office behind the rest are missing
security fixes. Many versions mean updates are not landing centrally.

### 3. Installed for one user only

Rule: scope "Specific User". The program is in one person's
profile; IT did not install it for the PC.

On stage: 36 programs, 1,822 installs, among them:
- Microsoft Teams classic: 1,040 PCs
- S/MIME Control for Outlook on the web: 292
- Teams Meeting Add-in: 267
- Polycom RealPresence Desktop: 54
- SAFE Agent: 34; Adobe Connect: 23; Outlook (PWA): 12; Postman: 10

Microsoft has retired classic Teams (to confirm with your Microsoft
365 admin), yet 1,040 PCs still have it.

Why it matters: outside IT's packaging and patching.

### 4. Several versions of one program on one PC

Rule: a PC with 2 or more versions of the same program.

On stage it is rare so far: the Teams Meeting Add-in on 2 of 2,592
PCs, and Teams classic at 2 versions on 1 of the 3 PCs read in full.
How: per program, installs against PCs (step 1 checks that Aternity
counts each PC once).

Why it matters: conflicts, crashes, and an old copy left unpatched.

### 5. Windows updates not on every PC

Rule: for each Windows version, each update and the PCs of that
version without it. Also when Windows was last updated, per PC
(`OS_VERSION_UPDATE_DATE`).

On stage: 112 updates, 21,782 installs. For example, Security
Update KB5126652 is on 2,622 PCs. It is counted per Windows version
because each version gets its own updates (step 1).

Why it matters: missing security updates.

### 6. Programs on few PCs

Rule: programs on 5 PCs or fewer.

On stage: 1,009 of the 1,545 programs, for example 7-Zip 22.01 and
ArcGIS Desktop 10.6.1.

Why it matters: one-offs nobody manages. An "approved" list in
config.yaml could hide the reviewed ones (later).

## Raising them

- **Red flags at the top of the tab:** one line each with its counts,
  ranked by PCs affected, each marked ▲ with its kind in words.
  Click one to open its details.
- **The tab's label carries the count:** "Software (6)", so the flags
  are seen from any tab.
- **Summary for management:** a button. GPT writes up to 5 sentences
  from the flags, every number checked against our counts; if one
  doesn't match, our own sentences are shown. It can be copied into
  an email.
- **New since yesterday:** a flag that was not there the day before
  is marked "new". Daily counts are kept for that and for trends.
- **No e-mail or Teams alerts** (as with the Care list), unless you
  want them later (decision 4).

## Ask about software (the chat)

It works like "Ask about today": GPT picks one building block, our
code counts, GPT words the answer in 2 to 4 sentences, and the counts
table is shown under it. A sentence with a number not in the counts
is replaced by ours. `verify --ai` tests the new blocks.

Building blocks:
- `flags`: the red flags, all or one kind.
- `versions`: a program's versions, PCs on each, behind, newest,
  oldest.
- `version_problems`: an application's versions with PCs that ran
  each and PCs with a crash, a hang or Major slow actions (flag 1).
- `program_split`: an application's crashes, hangs or slow actions,
  split by the version of an installed program (on demand only).
- `compare`: 2 versions of an application (or all of them), side by
  side on every kind of issue; or before and after an update, on the
  same PCs.
- `pcs`: the PCs on a program version, each with Investigate ▸.

Example questions:
- "Which programs are most out of date?"
- "Does one Outlook version crash more than the others?"
- "Compare Outlook 16.0.19725 with 16.0.20430."
- "Did Outlook get worse after its last update?"
- "Do Outlook crashes depend on the Teams Meeting Add-in version?"
- "Who still has Teams classic?"
- "Which PCs still run Edge 143?"

The box sits at the bottom of the Software tab, with its own blocks.
GPT picks better from fewer choices, and "Ask about today" stays as
it is.

## The page

Charts first. Each one has its exact numbers a click away
("Table ▾"), and hovering any bar or piece shows its numbers.

```
┌──────────────────────────────────────────────────┐
│ Software (6)   as Aternity lists them, Nov 10    │
│ ┌────────┐ ┌────────┐ ┌─────────┐ ┌────────┐     │
│ │ 5,336  │ │ 1,545  │ │ 229,701 │ │   6    │     │
│ │ PCs    │ │programs│ │ installs│ │▲ flags │     │
│ └────────┘ └────────┘ └─────────┘ └────────┘     │
├──────────────────────────────────────────────────┤
│ Red flags                    Summary for mgmt ▸  │
│ ▲ crashes more  Outlook 16.0.x  N of N  ╱‾  new  │
│ ▲ behind        Edge  3,408 of 5,328    _╱‾      │
│ ▲ one user      Teams classic  1,040    ‾‾‾      │
├──────────────────────────────────────────────────┤
│ Behind most PCs        PCs by version, old ▸ new │
│ Microsoft Edge      ░▒▒▓▓▓▓████████  3,408 behind│
│ Microsoft 365 Apps  ░▒▓▓█████████    1,908 behind│
│ Edge WebView2       ░▒▓▓▓▓██████       420 behind│
│                                         more ▾   │
├──────────────────────────────────────────────────┤
│ … one chart per kind of flag (below) …           │
├──────────────────────────────────────────────────┤
│ Ask about software  [                     ] Ask  │
└──────────────────────────────────────────────────┘
```

## Charts

Drawn as inline SVG, as on the Today tab: the live host has no
internet, so there is no chart library. They use the page's own
colours (parchment, archive green, brass).

**Red flags.** Each line has a small line of its count over the
last 90 days, with the last day marked, and "new" when the flag
was not there the day before.

**Behind most PCs (flag 2): version strips.** One strip per
program: all its PCs, cut by version, oldest on the left to newest
on the right, light to dark green. The version most PCs have is
outlined in brass. Versions under 1% of its PCs are merged into one
piece, and hovering it lists them. One dark block means the program
is in step; many pale pieces mean it is scattered.

```
Microsoft Edge     ░▒▒▓▓▓▓▓█████████  3,408 of 5,328 behind
Teams Meeting Add  ▒▒▓▓▓█████▓▓▓▓     N of 2,594 behind
Google Chrome      ░▒▒▓▓▓▓▓████▓      N of 513 behind
```

**Crashes, hangs, slow by version (flag 1): small multiples.** For
each application, 3 charts side by side: crash, hang and slow. There
is one row per version, most PCs first, and the bar is the share of
its PCs with the problem. A dashed line marks its other versions'
share. A raised version gets ▲. A version with under 20 PCs is
grey, "too few PCs".

```
Outlook, 7 days      crash        hang        slow
16.0.20430.20180   ██▏ ┆        █▏ ┆        ███┆
16.0.19725.20170 ▲ ██████┆      █▏ ┆        ███┆▌
16.0.18025.20214   ░ too few PCs
                     ┆ its other versions
```

**Compare versions: a dumbbell chart.** One line per kind of issue,
all on one scale (share of PCs, or of actions, 0 to the highest).
One dot is each version, joined by a line. The longer the line, the
bigger the difference, and the dot further right is worse. Below
it, the same for the busiest activities. Before and after an update
uses the same chart, with "before" and "after" as the two dots.

```
Outlook  ● 16.0.19725.20170   ○ 16.0.20430.20180
                     0%            5%           10%
PCs with a crash     ○────────────●
PCs with a hang      ○─●
Application Hang     ●──○
Major actions        ○──────●
  Send Mail          ○─────────────●
  Open Calendar      ●○
```

**Installed for one user, 2+ versions on one PC (flags 3, 4):
ranked bars.** Most PCs first, the top 10, then "more ▾".

```
Microsoft Teams classic  ████████████████  1,040
S/MIME Control           ████▌               292
Teams Meeting Add-in     ████▏               267
Polycom RealPresence     ▉                    54
```

**Windows (flag 5): 2 charts.**
- When Windows was last updated: PCs per week, the last 12 weeks
  and "older", the newest on the right.
- Each update on a Windows version: a filled bar (PCs that have it)
  inside the track of all PCs on that version.

```
PCs by the week Windows was last updated
                              █
                         ▃    █
 ▂    ▁    ▁    ▂    ▃   █    █
older …   Oct 20 Oct 27 Nov 03 Nov 10

KB5126652 on <Windows version>  ███████░░░  N of N
```

**Programs on few PCs (flag 6): the long tail.** Columns of programs
by how many PCs have them: 1, 2-5, 6-20, 21-100, 101-1,000, over
1,000. On stage, 1,009 of the 1,545 are in the first two columns.
Click a column for its programs.

**Opened program:** its versions as bars, oldest to newest, the
most common outlined in brass. Click a bar for its PCs.

```
Microsoft Edge · 20 versions · 5,328 PCs
 143.0.3650.139  ▏                      1  oldest
 …
 156.0.4314.58   ████████████████   1,920  most, newest
```

**Chat answers:** the chart that fits the answer (a version strip,
version bars or ranked bars) sits above its counts, and the table
folds under "Table ▾".

How they are kept honest:
- A colour stays with its version or kind, never with its rank.
- The warning colour is only for raised flags, always with ▲ and
  words.
- One scale per chart, and no chart with two axes.
- Every chart has its table.
- The colours are checked with the palette checker, colour-blind
  readers included, before the page ships.

Every way to use it:
- Tab "Software (N)", after Slow start-ups; link `#software`.
- Red flags: click a line to open its chart under it; Close folds
  it. "Summary for mgmt ▸" opens GPT's summary in an in-page
  dialog, with Copy and Close.
- Any chart: hover a bar or piece for its numbers. "Table ▾" shows
  the exact numbers; "more ▾" shows all rows.
- Click a program or a version strip: its versions open in place. In
  flag 1, each version also shows its crash, hang and slow bars.
- Click a version: its PCs, read from Aternity now. Each has
  Investigate ▸, which runs the Investigate tab for that PC.
- Ask about software: type a question or pick an example. The answer,
  its chart, its counts and any Investigate ▸ buttons appear under
  the box.
- The "as of" time shows when the lists were read (once a day).

## Load and storage

- Aternity, once a day: the installed list in about 5 reads (under a
  minute). Versions against crashes, hangs and actions: measured in
  step 1; read added up per application and version where Aternity
  allows it. A click: one read, 0.5 s.
- Disk: `data/software/<date>.json`, with counts, program and
  application names only, no PC or user names. Kept 90 days.
- Stage and live each read on their own, as the Today tab does.

## Steps

1. One more check, one copy (`verify --software` and
   `verify --versions`):
   - application version on app usage and actions: filled? how far
     back? added up per application and version? how long to read?
   - the failing part's version on health events: filled?
   - updates in the change log: how many PCs per update, to compare
     before and after on the same PCs;
   - several versions on one PC, per program;
   - Windows version and last update per PC; Windows updates per
     Windows version.
2. Build the tab: red flags, sections, charts, opening in place,
   Compare versions, and the summary. Tests and screenshots on dev.
3. Build "Ask about software" and its `verify --ai` tests.
4. Stage for a week, then live when you decide.

## Decisions (taken Nov 10, 2026: all as recommended)

1. Flag 1: raise at 2 times the other versions' share, with 20 PCs
   on each side and 5 with the problem (recommended), or other
   values?
2. "Behind": older than the version most PCs have (recommended), or
   older than the newest seen?
3. Tab after Slow start-ups, its own chat box, counts kept 90 days
   (recommended).
4. Raising: on the page, the tab label and the summary only
   (recommended); e-mail or Teams alerts later?
