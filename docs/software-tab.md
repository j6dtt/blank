# Software tab: proposal

Status: agreed Nov 10, 2026 (decisions below); step 1 done on stage
(Nov 10, a29ed09); steps 2 and 3 (the tab and its chat) built on dev
(Nov 10); next, one stage copy. Numbers are from stage
(`uxcorrelate verify --software` and `--versions`), as Aternity
lists them.

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
("Ask about software"; since 2026-11-12 Ask anything, using its
look-ups) answers questions from the same counts. GPT
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
  Major), from Riverbed's $metadata. On stage, last 7 days:
  - app usage: filled on 610 of 639 rows; actions: 115 of 120;
  - the daily tables go back to Aug 08 (usage) and Aug 09
    (actions): about 90 days, so trends and before/after come
    from Aternity itself from day one;
  - asked per PC and version, Aternity adds up the days: one row
    per PC and version (416 rows for 416 pairs, 0.3 s).
- Health events: the failing part's version is filled on 156 of
  165 events in 24 hours. It carries its kind, e.g.
  "10.0.22621.5415 (Process Version)". Compare versions matches each
  event to the version the PC ran (app usage), so this text is not
  compared.
- The change log, 14 days: "Updated" rows carry the version before
  (5,018 of 5,019 PCs); "Installed" (28,154) and "Uninstalled"
  (31,055) rows do not, and are paired as the investigation already
  does (an "Uninstalled" and an "Installed" row minutes apart).
  Biggest updates: Adobe Acrobat 26.002.21901 to 26.002.22031 on
  805 PCs; Edge WebView2 one step after another on 709-727 PCs.
- Windows: `DEVICE_INVENTORY` has 5,434 PCs (the installed list
  5,337). `OS_NAME` is "MS Windows 11" and `OS_VERSION` "Microsoft
  Windows 11 Enterprise 23H2", so the page shows `OS_VERSION`
  alone.
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

On stage, last 7 days (actions without PCs; the tab counts PCs):
- Outlook 16.0.20430.20180: 7,338 Major of 309,317 actions;
  16.0.20326.20142: 343 of 31,600; 16.0.20228.20188: 36 of 4,014.
- Most crashes: Trellix ENS - Threat Prevention 980 (2 versions),
  Edge 400 (15 versions), Outlook 265 (8 versions).

**When every PC listed had the problem.** Trellix ENS - Threat
Prevention: 406 of 406 PCs on 10.7.20.14066 had a crash, and 10 of
10 on 25.11.0.890. Every PC listed had a crash, so app usage holds
it only on PCs where it failed: the PCs that ran it without a crash
are not known and no share can be compared. Such an
application gets no ratio: the tab shows its PCs with a crash per
version and says why ("both versions crash: not new with
25.11.0.890").

On demand only, never raised by itself: an application's crashes
split by the version of an installed program. Example: Outlook
crashes on the PCs of each Teams Meeting Add-in version (1,599 PCs
on 1.26.18801, 255 on 1.26.21803, 189 on 1.26.25904 …). There are
1,545 programs, so raising every such split would raise chance
findings. They are asked for in the chat ("Ask about software").

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
seen in the company. A PC that also has that version is not behind:
it counts under 2+ versions on one PC (read PC by PC).

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

On stage: 143 programs have more installs than PCs, 2,179 more in
all. Asked per name only, Aternity counts each PC once (checked PC by
PC on the Teams Meeting Add-in: 2 = 2). Most:
- Trellix Data Exchange Layer for TA: 1,176 more than its 1,174 PCs
- DIEE (All Users): 168 more than its 193 PCs
- Intel(R) Management Engine Components: 145 more than its 145 PCs
- Microsoft Windows Desktop Runtime - 7.0.20 (x64): 122 more than
  its 125 PCs
- Content Manager Client Components x64: 77 more than its 5,293 PCs

How: installs against PCs finds the programs; the daily read then
reads each of them PC by PC (143 reads, about a minute), so the flag
counts PCs with 2+ versions, not installs, and names the versions
found together ("N PCs have version A and version B"). Whether two
versions are a real conflict or two parts of one product is then
seen, not guessed.

Why it matters: conflicts, crashes, and an old copy left unpatched.

### 5. Windows updates not on every PC

Rule: for each Windows version, each update and the PCs of that
version without it. Also when Windows was last updated, per PC
(`OS_VERSION_UPDATE_DATE`).

On stage: 21,781 update rows on 5,334 PCs. Per Windows version:
- 23H2, 4,636 PCs: Update KB5027397 on 3,751 (not on 885);
  Security Update KB5126652 on 2,599 (not on 2,037); KB5126653 on
  2,598.
- 25H2, 732 PCs: KB5054156 on 723 (not on 9); Security Update
  KB5127647 on 697 (not on 35).
- 24H2, 49 PCs: Security Update KB5124007 on 23 (not on 26).

A PC without an update may have a later one in its place. So, for
the PCs without it, the flag shows which updates they have instead
(from the same rows), and that is read, not assumed.

When Windows was last updated (`OS_VERSION_UPDATE_DATE`): over 8
weeks ago on 4,897 of 5,418 PCs, so it is not the monthly patching;
it is shown under Aternity's own name.

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
is replaced by ours. `verify --ai` tests the new blocks (10 questions,
every block at least once, and one outside the data).

Building blocks (as built on dev, Nov 10):
- `flags`: the red flags, or one kind's whole list (programs behind,
  2+ versions on one PC, Windows updates, raised versions ...).
- `versions`: a program's versions, PCs on each, behind, newest,
  oldest.
- `version_problems`: an application's versions with PCs that ran
  each and PCs with a crash, a hang or Major slow actions (flag 1),
  and which are raised.
- `compare`: 2 versions of an application side by side on every kind
  of issue (read now).
- `before_after`: the same PCs before and after an update (read now).
- `program_split`: an application's crashes, hangs or slow actions,
  split by the version of an installed program (read now; on demand
  only, never a red flag).
- `pcs`: the PCs on a program version (or every version starting
  with what was asked: "Edge 143"), or with a problem on an
  application version; each with Investigate ▸.
- `pcs_without_update`: the PCs without a Windows update that most
  PCs of their Windows version have, with what each has in its place
  (read now, as "not on N ▸" in the Windows section).

Names are matched to the latest read: exactly, else the one name that
has the words asked, else the one that ends with them ("Edge" is
Microsoft Edge, not Edge WebView2). With several, the answer lists
them and asks which one.

Example questions:
- "Which programs are most out of date?"
- "Does one Outlook version crash more than the others?"
- "Compare Outlook 16.0.19725 with 16.0.20430."
- "Did Outlook get worse after its last update?"
- "Do Outlook crashes depend on the Teams Meeting Add-in version?"
- "Who still has Teams classic?"
- "Which PCs still run Edge 143?"
- "Which PCs are missing KB5126652?"

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
piece (each version its own piece, at least 3 px wide; hovering one
gives its PCs). One dark block means the program is in step; many
pale pieces mean it is scattered.

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
- When Windows was last updated: PCs per week, the last 12 weeks,
  the newest on the right; "older" is a number beside it (on stage
  4,897 of 5,418: as a bar it would flatten every week).
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

Every way to use it (as built on dev):
- Tab "Software (N)", after Slow start-ups; link `#software`. N is the
  red flags, shown on the tab from any tab.
- Red flags: one line per kind (each raised version its own line),
  most PCs first, with its PCs per day and "new". Click a line (or
  Enter): the page goes to its chart and outlines it.
- "Summary for management ▸": an in-page dialog with the summary,
  Copy (or, over plain HTTP, the text selected for Ctrl+C) and Close
  (or Esc, or a click outside it).
- Crashes, hangs and slow by version: click a bar for the PCs with that
  problem on that version, read from Aternity now; "Compare versions ▸"
  fills Compare with that application.
- Compare versions: type or pick an application, Compare (its two
  versions with the most PCs); pick other versions and Compare again.
  "Before and after an update ▸": the PCs whose version changed once in
  the last 14 days.
- Windows: "not on N ▸" beside an update lists those PCs, read now
  (the same count as the daily read): what each has in its place and
  how many Windows updates it lists; the PCs with nothing in its place
  first.
- Any program name (Behind, 2+ versions, one user, a long-tail column's
  list) opens "A program" at the bottom: its versions as bars; click a
  version for its PCs, read now.
- A PCs list opens under the chart that was clicked, ✕ Close folds it.
  Each PC has Investigate ▸: the Investigate tab runs for that PC (its
  last day with the problem, or the last 4 hours for a program).
- Any chart: hover for its numbers; "Table ▾" for all of them; "more ▾"
  for the rest of a list.
- The "read" time shows when the lists were read (once a day); while a
  new read runs, the page keeps the last one and says so.
- "Ask a question ▾" (beside the summary) goes to "Ask about software"
  at the bottom: type a question or click an example; each answer has
  its counts under it, PCs with Investigate ▸; "Clear the answers"
  empties the box (the answers are not kept when the page reloads).

## Load and storage

- Aternity, once a day: the installed list in about 5 reads (under a
  minute). Versions against crashes, hangs and actions: measured in
  step 1; read added up per application and version where Aternity
  allows it. A click: one read, 0.5 s.
- Disk: `data/software/<date>.json`, with counts, program and
  application names only, no PC or user names. Kept 90 days.
- Stage and live each read on their own, as the Today tab does.

## Steps

1. Done (a29ed09). One more check, one copy (`verify --software` and
   `verify --versions`):
   - application version on app usage and actions: filled? how far
     back? added up per application and version? how long to read?
   - the failing part's version on health events: filled?
   - updates in the change log: how many PCs per update, to compare
     before and after on the same PCs;
   - several versions on one PC, per program;
   - Windows version and last update per PC; Windows updates per
     Windows version.
2. Built on dev (Nov 10): red flags, sections, charts, opening in
   place, Compare versions, and the summary. Tests and screenshots
   (desktop, phone, dark) on dev.
3. Built on dev (Nov 10): "Ask about software" and its `verify --ai`
   tests.
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
