# UX Correlator — Aternity × NetIM

Troubleshoot "the app is slow" tickets. The tool pulls a user's end-user
experience from **Riverbed Aternity** and infrastructure metrics from
**Riverbed NetIM** over their REST APIs. It lists every problem recorded on
the PC, the network or the application's servers, and counts how many of
the user's slow actions each one was at.

Example report (start):

```
ANSWER: Likely: den-br-rtr01:Gi0/1 (WAN-MPLS): Interface utilization in (%) reached 95.51 (limit 80.0)
  It happened within 5 min of 23 of the 23 slow actions.

What the user felt:
  23 of their 91 actions were slow (CRM Portal 23),
  between 2026-10-28 10:00 UTC and 2026-10-28 10:44 UTC

Other users of these apps:
  This user had 23 slow actions: Open Account in CRM Portal (23). At the
  same time, 20 of the 50 other people using CRM Portal also had a slow
  action (40%). That is more than normal: at this time on other days,
  at most 0% had one. 20 of the 20 slow users are behind the same
  network point: den-br-rtr01 Gi0/0 (LAN) (10.20.30.0/24).
```

## How it works

1. **Slow actions (Aternity).** The user's activities come from
   `BUSINESS_ACTIVITIES_RAW`. An activity is slow when Aternity's own
   status for it (`SLA_STATUS`) is one of `thresholds.slow_status`.
   The page shows that status beside slow actions (e.g. Minor or Major),
   with how many of each.
   `verify` lists the status values your tenant uses. Until you set it,
   slow means over `thresholds.response_time_s`.
2. **Problems on the PC (Aternity).** Each activity row carries the PC's
   CPU, memory, disk queue and network round trip at that moment; a value
   over its limit is a problem. Free disk space comes from its own data
   source (`disk_space`: `RAW_DATA_STORAGE` by default; if the tenant
   doesn't publish it, `verify` prints where it is, e.g. `OS_FREE_DISK_SPACE`
   as a share of the drive size from `DEVICE_INVENTORY`); under
   `free_disk_percent` is a problem. Device health events (`DEVICE_HEALTH_RAW`) and each app's
   crashes and hang time (`APPLICATIONS_RAW`) are problems too.
3. **Path mapping (NetIM).** The user and the application are mapped onto
   NetIM objects:
   - `client-gateway`: an L3 interface whose subnet holds the PC's IP
   - `site`: devices in `topology.sites` for the PC's Aternity location
   - `app-server`: a NetIM device matching `topology.applications`
   - `app-server-network`: an L3 interface whose subnet holds the server
   - `always`: devices in `topology.always_include`

   If nothing maps, the answer says the network was **not checked**; if
   NetIM could not be read, it says so ("not checked" / "not fully
   checked", see Warnings), never that nothing was wrong.
4. **Problems on the network or servers (NetIM).** Interface utilization
   and device CPU of those objects (add metrics in the config; `verify`
   lists what NetIM has); a value over its limit is a problem, and so is
   a NetIM alert.
5. **Matching.** A problem counts for a slow action recorded up to
   `bucket_minutes` (5) before or after it. A crash, hang or health event
   of one application counts only for that application's slow actions.
   A failing disk is the PC's state, not a moment: when Aternity labels a
   health event's failing part "(Disk)" or "(Drive)" and files the event
   under Hardware (HD Failure, Faulty HD S.M.A.R.T. Status; Low Disk Space
   and Corrupted FS, filed under System, are ordinary events), the PC's disk counts
   from its first record (in the window, or in the 14 days before on that
   PC) for every slow action there whose time Aternity put mostly on the
   PC. Its records are one issue, it leads ("Likely: VD-41's disk is
   failing (Aternity recorded HD Failure)") and its step comes first: back
   up the user's data and replace the disk.
   The answer names the problem at the most slow actions: "likely" when
   that is more than half of them, "possibly" when fewer. No scores.
   With nothing at the slow actions it names the slowest one: "Microsoft
   Outlook Launch took 61.8 s (Major): no cause found". When that action
   came up to 10 min after the PC started up, the same activity on all PCs
   of its kind (virtual desktops or physical PCs) in the 24 h before is
   counted, right after a start-up and later. If it is slower right after
   a start-up there, the headline says so: "Microsoft Outlook Launch took
   61.8 s (Major) right after a start-up: 67% of those on virtual desktops
   are slow".
6. **What went wrong (the main point).** Slow actions are one kind of
   issue, next to hangs, app crashes and health events: with no slow
   action the answer still counts what happened ("7 hangs, 2 app crashes,
   no slow actions"). Issues the user felt come first, then those on many
   PCs, then limits passed with nothing felt. For each one the tool reads
   Aternity's own records:
   - where the application failed: the component and the module inside
     it, their versions and the error (`DEVICE_HEALTH_RAW`);
   - who else: other PCs that fail the same way, out of how many, on which
     versions, and since when (see `docs/who-else-is-affected.md`);
   - a possible cause: a change on the PC with the same name (every word
     of one name in the other: "Microsoft.Notes" is "Microsoft Sticky
     Notes", never "Microsoft Edge") or the same version as the failing
     part, before it. Every other change is listed
     once per PC under "What changed on these PCs", with its time
     (`INSTALLED_SOFTWARE_CHANGE_LOG`, 14 days before the window to its end);
   - for CPU, memory or disk problems, the busiest processes at those
     times (`APPLICATION_RESOURCES_HOURLY`: one row per process and hour,
     so rows within an hour are read; CPU in %, memory in MB; it has no
     disk use, so a disk problem says so). Low memory events get them too.
     A limit passed away from any slow action is a fact, not a step to do.

   Each is a plain sentence under the finding; when a source has nothing,
   it says so.
   Two names are not blamed: `bad_module_info` (Windows could not tell
   which program failed) and Windows files such as `KERNELBASE.dll` (they
   report a failure; the program they ran in is looked at instead).
7. **Rollup.** The top of "What went wrong": one row per issue, with this
   user's count, other PCs, a verdict by fixed rules (Only this PC, On 1
   other PC, New, Widespread, plus growing, steady or falling) and bars of
   other PCs per day. The verdict says how far an issue reaches, not whether
   it caused this user's problem. A new investigation clears the last one.
8. **Other users.** For slow actions: the same applications' activity for
   every other user at the same times, grouped by the NetIM gateway (or
   /24) each user sits behind and compared with the same hours on the
   working days of the week before (a live Aternity keeps about 8 days of
   activities, so earlier weeks are gone). A virtual desktop's address is the
   desktop's, so those users are one group, never a network point.
   It is the answer only when Aternity did not put most of this user's slow
   time on their own PC; it gives counts ("2 of the 14 other users"), not
   "many".
9. **Start-ups.** The user's boots and logons (`WINDOWS_MACHINE_BOOTS`):
   Aternity's measurements of one PC within an hour form one start-up,
   shown next to that PC's own last 30 days, with what took the time
   (`MACHINE_BOOT_COMPONENTS`). For the slowest, the cause is the part
   Aternity measured as most "longer than normal", plus what changed on
   the PC before; the main causes list every PC where that part was
   longer than normal. The "Start-ups" tab and
   `uxcorrelate logons --last 24h [-l <location>]` search everyone.

## The web page

The look: a switch at the right of the top bar, **Parchment | Dark**.
Parchment is the page's own look; Dark is the same page on a warm
near-black. A click changes every tab at once and the browser remembers
it (the admin page has the same switch and follows the same choice when
both are opened under one address). Until someone picks on a browser, the
page follows the PC's own light or dark setting. The choice is the
browser's, not the person's: the main page has no login. When the page is
scrolled down, the slim bar keeps the tabs and the lights; the switch is
at the top of the page.

Today tab (opens first): what Aternity itself rated today, from midnight
in `timezone` (config.yaml). Tiles for crashes and events Aternity rates
Critical or Major and for its Major slow actions, each against the same
kind of day before ("More than usual", "As usual", "Fewer than usual");
Major slow actions per hour and per application; other crashes and events
on the most PCs, each with its 7 days before. It reads Aternity every 15
minutes, or every `refresh_minutes` set in config.yaml (5 to 60; the
Care list is checked after each read). The open tab fetches every 5.
Click a tile ("Show who"), an application row or an event row for the
users and PCs behind it, most first (50 at most): a slow action list
gives each one's slowest action, an event list how many times. Their
"Investigate ▸" opens the Investigate tab and runs it at once for that
user and PC, from 30 minutes before their first one to 30 minutes after
the last (an hour each side for events; for an application, only that
application).
"Ask anything" (at the bottom of every tab, the same chat with the same
answers whichever tab is open; "Ask a question ▾" on Today and Software
jumps there): ask in your own words ("What PCs had hard drive issues
this week?", "Tell me about sample02", "What does this page show?"). The
examples under the box change with the tab. It sees the tab you have
open, as shown, with what its terms mean (look-up "The … tab, as
shown"), so "what does this mean?" is answered from what you see.
The AI looks things up in this app's data, as many steps as it needs
(find a name, health events, today's actions, today's overview, the
meanings of our words), and answers in its own words. It can also read
Aternity now, at most 5 times a question: a user's or PC's logons,
their start-ups and why they were slow, a PC's installed software and
Windows, what was installed, updated or removed on a PC, how many
programs every PC has (the most or fewest first), the Software tab's
look-ups, and one investigation of a user or PC (from 30 minutes before
their first Critical or Major event or Major action today, the last 4
hours at least, unless a time is asked for; 24 at most; up to a few
minutes). While it
works, the line under the box says which look-up it is on. "What I
looked at" under each answer shows every step with its rows
(Investigate ▸ on each; "Open this investigation ▸" shows the one it
ran on the Investigate tab, "Open in Start-ups ▸" runs that tab);
a number found in none of them is marked, not hidden.
The usual for actions builds up over the first week from the counts saved
in `./data` each night (`mkdir -p data && chmod a+rwx data` before the
first start).

Assessment (top of the Today tab): a short reading of the day for
executives, written at 06:00, 09:00, 12:00, 15:00 and 18:00, under a banner
(green, amber or red with the verdict): the verdict and what the day
means for people working, then points labelled Changed (since the one
before), Watch (above its usual range now), Earlier (above it earlier
today, not now) and Start-ups, and what to look at first. Yesterday's
summary is beside it, and under it a small chart of this week, Monday to
Sunday: PCs a day with a Critical event, with a Major event and with
Major slow actions (each PC once a day, each row its own scale); this
week is the solid line, today a hollow point (so far), last week dashed;
a day not read in full is a gap, never 0; point at a day for both
numbers. It reads nothing more from Aternity: the health events the Today
reader keeps and each day's saved counts. The words that rate a count are fixed: isolated
(under 5 PCs), limited (5 to 49), widespread (50 or more); above,
within or below its usual range (the lowest to the highest of the same
kind of day before, by this time); rose, fell, steady, new, cleared.
Counts and names of events and applications only, never a user or a PC.
The AI words it and every number and rating word is checked against the
counts of its hour; otherwise our own text is shown. Earlier ones of
the day are one click away; Copy puts it on the clipboard. Kept 8 days
in `./data/assess`. Three small charts of today up to the hour (the
share of actions rated Major, PCs with a crash so far, the typical
logon time), each against the same hour on the days before, are a row
of their own in Today's issues, under the active users and PCs; Aternity's
start-ups are read once an hour for them.

Care list tab (second): up to 50 users, each checked after every read of
Aternity (every 15 minutes, or `refresh_minutes` in config.yaml).
"+ Add a user" (top right of the tab) opens a box: the username as
Aternity writes it (names seen in Aternity are offered as you type) and
an optional label shown big on the tile; the tile is there at once and
fills in within seconds. With Active Directory set (`ad:` in config.yaml,
the account in `.env`), the box also finds a person by name: type the PIN,
then part of their name ("pat lee") and **Find**; it lists who AD has
(name · department · the username Aternity writes for them, "not seen in
Aternity in 8 days", "disabled in AD"); a click puts the username in the
box, then **Add**. The label can stay empty: the tile then shows AD's
name.

```
+ Add a user ─► "pat lee" + PIN ─► Find
   └─► Pat Lee · Finance · CORP\plee  (click)
        └─► Username box: CORP\plee ─► Add
             └─► tile "Pat Lee", under it "CORP\plee · Finance"
```

Each tile (with AD set) shows under its title the username, AD's name
(when a label is the title) and department, "disabled in AD" in red (the
account is disabled: they may have left) or "not in AD". Looked up once a
day, in memory only, after the tiles are drawn (the page never waits on
AD); only the list's own users. AD not answering or refusing the login is
a line under the tiles. The status for other platforms is unchanged
(its label is the typed one). Click a tile, then "Remove from the Care
list…": after a confirm, the tile, the user's saved issues and their
latest investigation are deleted. The list is `data/care_list.json`
(`[{"username": …, "label": …}]`; it can also be edited by hand, seen at
the next check). It is made once from `care_list` in config.yaml, where
the list was before; config.yaml's list is not read after that. The page
has no login, so anyone who can open it can change the list; each change
is logged with the address it came from.
An issue is an event Aternity rates Critical or Major, a slow start-up,
or more Major slow actions by this time of day than on any of the user's
own latest 5 days with a PC in use, any day of the week (people on the
list often work weekends).
A slow start-up (Aternity rates start-ups with no severity; the tile's tag
is "Slow start-up"): a start-up of theirs today with a type (Aternity's
own: a logon, a boot) that took longer than every one of that type of
theirs in the 30 days before (5 needed) and longer than 1 in 10 of
everyone's of that type today. Both: a few seconds over their own
longest is not one, nor a morning slow for everyone.

```
08:29 PC-007 User Logon 113 s
  their 20 before: 14.5 to 25.7 s   longer ✓
  everyone's today: 1 in 10 > 85 s  longer ✓
  └─► "Slow start-up on PC-007 at 08:29: …"
```

Today's start-ups of every user on the list are read at each check (one
read); their 30 days before once a day per user; everyone's of today
come from the hourly read for the logon chart. Show more lists each
start-up of today with both comparisons, or why it was not compared
("3 of theirs in the 30 days before, 5 needed"), and **Open in
Start-ups ▸** runs the Start-ups tab for that user from midnight: what
took the time and what changed before. Only rows Aternity writes with
their username count (on the dev data, logons; boots have none). Each Major action is judged by its own time of day, whenever it
is checked, so one found late (after a restart, or sent late by Aternity)
still counts. An OK tile says the day's counts against one usual, their
usual by now ("176 actions today, 3 rated Major (usual by now: 0 to 4)");
"more than their usual by now" on an OK tile means no new Major action
came in the last reads. Every tile lists the user's actions Aternity rated Major today, latest
first ("10:12 Outlook · Send Mail · 9.4 s"): the latest 3 on the tile, all
of them under Show more.
What lights the tile (its alert): a read of Aternity that brings something
new of an issue (Aternity sends rows late, so "new" is what that read saw
first, whatever its time: the tile then says "Aternity sent it at 18:10."
when that was more than a read after it happened). The alert goes off at
the 3rd read after the last read that brought a new one ("Clears at the
14:20 read if no new Application Hang"; reads start every refresh_minutes,
so 15 minutes at a 5-minute refresh); a repeat, or another event, keeps the
tile on. A failing disk (Aternity's "(Disk)" or "(Drive)" label on the
failing part and its category Hardware, as HD Failure; not Low Disk Space,
category System) stays on all day: "Stays on today". Then the tile is
OK again (Show more lists the issue as over). Repeats of one kind less than
an hour apart are one issue (one count, one investigation); after an hour
with none it is over (slow actions also once the user is back within their
usual), and a later one is a new issue. A restart does not light again what
was already seen; a user just added is lit only by what happened within the
alert's time. Every user has a tile: those with an alert on first (a failing
disk, then Critical, then Major), then names not seen in 8 days, then green
"✓ OK" tiles (actions today, how many Major, their usual by now, or "not
compared" when there are too few days with a PC in the 8 days Aternity
keeps to know it (3 needed)). The tiles have no "Earlier today" and no
"Why" (the user, Nov 17 2026: two usuals on one tile read as a
contradiction, and "Why" could be about an action the tile did not
list). Under each, small bars: their Major actions by this
time of day on each of the last 8 days, today last, from Aternity's own
records, so today's bar is the tile's "rated Major" and the dark bars before
it are the days their usual comes from (its range is their lowest to
highest; faded: older days; dashed: not read); "Last issue before today" says
the latest issue, Show more lists those of the 7 days before (no red dots: a
dot marked any issue of a day, a crash too, on a bar of Major actions counted
only up to now, so it sat on bars it had nothing to do with). Inside each of those, the tiles keep the list's own
order: "⠿ Arrange tiles" shows a small tile per user in that order; drag one
by ⠿ (a mouse can take it anywhere, a finger only by ⠿ so the page still
scrolls) or move it with ↑ ↓ or the arrow keys, then "Save order" (PIN);
Cancel leaves the order as it was. The status below follows the same
order. An investigation runs by itself (at most one an hour per
user) whenever one of today's issues is not covered by the latest one: a new
issue, or one Aternity sent late. Click a
tile: today's issues (those over marked "(over)"), the investigation ("Open the investigation ▸" shows it
on the Investigate tab without running it again; "Investigate again now ▸"
runs a new one) and the issues of the 7 days before. The tab's red count is
the users with an alert on now; `…/#care` opens the tab.
Issues are kept 8 days in `./data/care` (with usernames).
Every change of the list (add, "Change label…", remove, the order) asks for a PIN; your
administrator has it. A wrong PIN changes nothing and is logged.

For another platform (a wall screen, a service desk tool), every tile's
status is JSON at `GET /api/MSS/care/status`, to ask every few minutes. All
times are UTC; there is no PC field (PC names can be in the summary):

```json
{"ready": true, "as_of": "2026-11-14T15:40:12Z", "refresh_minutes": 5, "note": null,
 "users": [{"username": "jdoe", "label": "Front desk", "status": "alert", "level": "Major",
            "since": "2026-11-14T14:50:00Z",
            "summary": "Application Crash - Outlook, 3 times 14:50-15:20 UTC, on VD-41"}]}
```

`status` is `alert`, `ok` or `unknown` (the name was not seen in Aternity in
8 days); `level` is Aternity's (Critical, Major) on an alert, else null;
`since` is when the alert's issue began, or when the user has been OK since
(midnight in config.yaml's time zone when nothing happened today). The
summary is our own words, plain ASCII, at most 120 characters, the most
serious thing first. Users are in the tab's order. `as_of` is the last read
in which every read of Aternity worked: older than 2 reads, the statuses are
out of date (`note` then says why). `ready` is false until the first read
and check are done.

Investigate tab, top to bottom:

- **Answer**: the likely cause in one line, and at how many slow actions;
  with no slow action, what went wrong, counted (amber), or that nothing did.
- **What the user felt**: slow actions per app, the slowest one, the time
  split Aternity measured, the slowest start-up.
- **Other users of these apps**: only when something was slow: how many
  other users were slow, against the earlier days, and the network point
  they share ("Show who" lists them).
- **What went wrong**: each issue, with "What we know" under it: where it
  failed, a "Possible cause" when a change matches it, and "Who else"
  ("Show who" under it lists PC, user, version, times, last seen; "Hide
  who" closes it).
- **What changed on these PCs**: every change, once per PC, with its time.
- **Compared with normal**: each slow app + activity (e.g. an app's
  launch) next to its own normal: how long it usually takes for this user
  (last 30 days) and for other users at the same time, on how many days
  Aternity marked most of its runs slow and since when, what changed on the
  PC between the last normal day and the first slow one, and the PC's CPU,
  memory and disk queue at those actions next to their usual.
- **What was checked**: what else was looked at, with counts (PC values
  and their limits, health events, crashes and hangs, start-ups, network),
  so "nothing found" says what is ruled out. A read of Aternity or NetIM
  that failed or was cut short is never "nothing found": who else is "not
  known" or "at least N", a change log "could not be read in full", other
  users are not compared with other days, a source not read in full says
  "not read in full (see Warnings)" here, the answer adds "Not all of it
  could be read", and the Warnings in Details for IT say which read. Up to
  8 of the user's PCs are read (those with a slow action first); a warning
  names any left out. The same holds on every tab and in the chat: a day
  of health events Aternity cut short is left out of the usual (said on
  Today), a look-up says the hours or days it could not read, and
  `uxcorrelate verify` marks each check whose reads were cut short
  ("…: not read in full") and goes on when one check fails.
- **What to do**: steps from those causes.
- **Timeline**: every action (red = slow) and every problem (faded = not
  near a slow action). Hover for date, time and value.
- **Problems found**: the problems at slow actions, with "at N of M";
  "Show N problem(s) at other times" opens the rest.
- **Details for IT**: the rules used, the PCs, the NetIM objects checked.

Start-ups tab: searches the Start/End range in its form (last 24
hours by default; empty user, PC and location = everyone). One card per
Aternity measurement type (typical, 1 in 10 slower than; with fewer than
10 measured, the slowest one instead); **Main causes**:
the parts behind the slowest start-ups and every PC where they were longer
than normal ("Show who"); one row per start-up with a column per type
(click a row for what took the time and why); a table by location.

Connection lights (right of the tabs, on every tab): one per API and AI
model: Aternity, NetIM and each AI service in `ai.services`. Green =
connected, a flashing red "… down" = no answer or the login was refused,
amber = the service answers but does not list the model set (or lists
none), grey = not set. Checked every minute with one small read each. Click
any light for the "Connections" panel: since when, the reply time, which
model Ask anything, Explain and the summaries use, and anything in
config.yaml that is not used (a misspelt setting, refresh_minutes out of
range; `uxcorrelate verify` prints these first, with a time zone this host
does not know). Changes are written to the log ("Connection to NetIM: down.").

Software tab (fifth, `…/#software`): red flags on installed software and
application versions, as Aternity lists them, read once a day (and at
start; the first read takes a few minutes). The tab's count is the red
flags. Top to bottom:
- **Red flags**, most PCs first, each with its PCs per day and "new" when
  it was not there the day before: a version that crashes, stops
  responding or is slow more than the version most PCs have, or more
  after its update than before (at least 2 times, 20 PCs on each side, 5
  with the problem, and still so with 2 more PCs with it on the other
  side, or 5 more actions rated Major for slow actions; a version that
  is worse than another is raised only when the PCs that ran both
  versions show it too), programs behind
  most PCs, 2+ versions on one
  PC, a Windows update most PCs of a Windows version have but not every
  one, programs installed for one user, programs on 5 PCs or fewer. Click
  a line to go to its chart. **Summary for management ▸** opens a short
  summary (GPT, every number checked; else our own sentences) with Copy.
- **Application health by version**: virtual desktops and physical PCs
  apart (a switch; each button says its red flags). One red flag is one
  line in the Red flags list, one raised count in the tables (amber bar,
  bold words, ▲) and one on its button: a version that is worse for one
  problem both after its update and against another version is raised
  once, on the count the list's line quotes (its update); the other count
  is drawn as not raised and says "not raised a second time: this version
  is raised after its update, below". An application
  with a ▲ is open, the others are one folded line. Two comparisons, each
  shown whole: first **version by version** (each version next to the
  version most PCs have, shaded; every PC that ran it in the last 7
  days), then **before and after the update** (the same PCs, for each PC
  as many days after as before; only those days, so a version's
  percentage there is not its percentage version by version). A bar is a
  share, with its percentage and the count behind it; a bar is full at
  its column's highest share, or at 10% when every share is lower.
  - There is no headline box over an application: the ▲ is on the count
    it is about, with what it is against and "Show the N PCs ▸".
  - **The same PCs decide.** Two versions side by side are different
    PCs. When 20 PCs or more went from one of the two versions to the
    other, they ran both: if 5 of them or more had the problem on one of
    the two, the version that looks worse is raised only when they show
    it themselves by the same checks (2 times as many of them with it on
    that version, and still so with 2 more on the other). Else it is not
    raised and its count says so ("not raised: the 53 PCs that ran both
    had it on 7 with this version and on 9 with ...", or "... on 101
    with this version and on 89 with ...: not 2 times as many"). When
    they show it, the ▲ gives their counts.
  - **One version, one row.** Aternity writes some versions in more
    than one way (3.0.20 and 3.0.20.0, 24.221.1103.0003 and
    24.221.1103.3, with and without "(WinBuild...)") and puts a PC's
    crash under the second spelling. Spellings with the same number are
    counted as one version, in every table, flag, list and comparison;
    the row says "also written ...". They were two versions, the second
    with a crash on every one of its PCs.
  - PCs that went to a lower version did not get an update: their table
    is titled **Before and after the change of version**.
  - **Table ▾** under the versions: every version on the kind of PC on
    screen, each count with its percentage.
  - **Explain ▸** beside a comparison walks through it with its own
    numbers: why something is raised or not, and what to do next (our
    words; a question under it goes to the AI). The "i" beside the title
    says how to read a row. Click a count for those PCs (read now; the
    list says when it counts other than the tab's daily read did), each
    with Investigate ▸ for its last day with it.
- **Compare versions**: two versions of an application on every kind of
  issue (crash, hang time, its health events, Major actions per
  activity), on the kind of PC the tables above are on, each share with
  its counts; **Before and after an update ▸**: the PCs whose version
  changed once in 14 days, counted as the tables above (PCs with a crash
  or hang time, as many days after as before). Both are read now and say
  so. Every **Table ▾** on the tab has a percentage beside each count.
- **Behind most PCs** (version strips, oldest to newest), **2+ versions on
  one PC**, **Installed for one user**, **Windows** (per version, the week
  it was last updated, updates not on every PC and what those PCs have;
  "not on N ▸" lists those PCs, read now, the ones with nothing in its
  place first), **Programs by how many PCs have them** (click a column), **A program**
  (its versions; click one for its PCs, read now).
- **Ask anything** (bottom; "Ask a question ▾" beside the summary jumps
  there): the same chat as on every tab, with this tab's examples. It uses
  the tab's look-ups as they are: an application's crashes split by the
  version of another program ("Do Outlook crashes depend on the Teams
  Meeting Add-in version?"; never a red flag), which PCs have a version
  ("Which PCs still run Edge 143?") or lack a Windows update ("Which PCs
  are missing KB…?").
Each chart has "Table ▾" with its numbers. Counts and names are saved in
`$UXCORRELATE_DATA/software/<date>.json` (no PC or user names), 90 days.

Inventory tab (sixth; link `#inventory`): the PCs in Aternity's device
list. It is read every hour (about 1.4 s for 5,211 PCs), with one more
light read of which PCs ran an application in 8 days, and held in memory:
every list and card answers from that read. Only the usage charts'
counts are saved (`data/inventory/usage.json`: counts per kind of
device, no PC or user name).
- **Tiles**: PCs in the list by Aternity's kind of device, heard from in
  the last 7 days, retired (the Software tab's count), things to look at.
- **Desktop usage**, on the same first panel: the PCs an application ran
  on, as bars, against the PCs in Aternity's list, as a line (= 100%).
  Left, each of the last 30 finished days, with percent marks at the
  right; right, each month ("used at least once", its percentage over
  the bar, the busiest day of the month as a second line), up to 12
  months. A switch shows all PCs or one kind of device, as Aternity
  names it; it opens on virtual desktops. Four figures under the
  charts: a typical working day and a typical weekend day (the middle
  value of the days shown), the busiest day, this month so far. Point
  at a bar for its counts.
  - Aternity has no rows for today until the day is over, so the last
    bar is yesterday. A day is read again each hour until it is 2 days
    old, then kept. A day with no rows is a gap, never 0.
  - Aternity keeps about 92 days of daily app usage. At its first read
    the tab reads every day it keeps (about 90 small reads, once) and
    the whole months among them; from then on it keeps each day and
    month itself, so the monthly chart grows to 12.
  - Only PCs in the device list are counted; how many more had app
    usage is on each bar. Days and months from before the tab's first
    read are set against the list of that day.
- **Needs a look**, most PCs first, each count with its share of the PCs
  it is counted among: an older Aternity agent than most, not used in
  8 days, not restarted in over 14 days, not heard from in 8 days or
  more, agent stopped, a stability index under 5, little free space
  (Aternity's bands that end at 10 GB or under), still on a spinning
  hard disk. A click lists the PCs, each with
  "Investigate ▸" and its card. A line this Aternity has no column for is
  said "not judged", never 0.
- **Last heard from**, with the agent's status in Aternity's own words.
- **Retired PCs** (software in Aternity, no longer in its device list) and
  **In the list, but not used**.
- **Find a PC** by name, last person or serial number: a card of every
  column Aternity fills for it, as it sends it, the lines it is on, and
  "Investigate ▸", "Its software ▸" (its installed list, read then), "Its
  start-ups ▸".
The limits are constants at the top of `inventory.py`. Step 2 (the
charts of what the PCs are, with a virtual / physical switch) and Ask
anything's look-ups are not built yet.

## The admin page

Who uses the app and how, at `/admin`. No link from the main page: you
need the address and the password. It has the main page's **Parchment |
Dark** switch at the right of its top bar, also on the log-in screen.

How it works: the main page tells the app each tab opened (at load and at
each switch). The app keeps each use with the caller's address and time.
The main app has no login, so the address is looked up in Aternity: the
Today reader already reads every action, now with `DEVICE_IP_ADDRESS`, and
keeps the rows of the addresses that opened the app. The user Aternity saw
on that address, nearest the visit within an hour, is the person.

```
desktop VD-03 opens Today ─► app keeps: 10:02, 192.0.2.11, Today
Aternity row: 10:01 sample07 on VD-03 at 192.0.2.11
next Today read (≤15 min) ─► 10:02 sample07, VD-03
```

**Log in.** Put `ADMIN_PASSWORD=...` in `.env`, then
`docker compose up -d` (a restart does not read `.env` again). Open
`https://<docker-host>:8444/admin` (on Kubernetes, the ingress address
+ `/admin`, or `https://<load balancer>:8444/admin`). Plain HTTP shows a link to the HTTPS address instead of the
form: the password could be read on the network. A login lasts 8 hours,
ends with **Log out** or a restart. 5 wrong passwords from one address
lock it for 15 minutes. Each login, wrong password and lock is a log
line with the caller's address.

**Three tabs** under the top bar: **STATISTICS** (opens first),
**SECUREVIEW** (the devices people sit at to work outside the office;
see "SecureView" below) and **ACTIONS** (every action of one user
today; see "Actions" below). Each has its link, `/admin#statistics`,
`/admin#secureview`, `/admin#actions`; a reload or a login stays on the
tab in the link. Statistics and SecureView are read every minute while
open; the other tabs are read again the moment they are opened and keep
where they were (SecureView's sub-tab, search, department box and open
device; Actions' user and list). Actions reads Aternity only when you
press Show. **Log out** is on every tab.

**Statistics** (one day; **Day** at the top, shown on this tab only,
picks another; today updates every minute):
- **People using the app**: Now (a visit in the last 15 minutes: person,
  desktop, tab, last visit); then the day: each person's first and last
  visit, tabs used and what they ran. "Waiting for Aternity (address)":
  looked up again at each read (every 15 minutes; Aternity sends rows
  late). "Not known (address)": still no action from that address 2 hours
  after the visit (no agent, a VPN address, no action near the visit).
  Only named people are counted as people.
  "One of N on it then": a desktop many users share. When every visit
  comes from one address, a note says something in front of the app
  hides the desktops.
  With `AD_USERNAME` / `AD_PASSWORD` in `.env`, each person's name and
  department from Active Directory follow their username (title and
  office on hover), and in the day's table each desktop's OU and
  description (its Windows on hover). "Disabled in AD" and "not in AD"
  are said. See "Names from Active Directory" below.
- **Use of the app**: tabs opened by hour and by tab; investigations
  (where they were started, how long they took, what each found first);
  start-ups reports, Software clicks, Explain uses.
- **Ask anything**: questions, look-ups, failed look-ups, answers with a
  number flagged, time to answer, and the questions themselves.
- **Care list**: users listed, times a tile lit (a read that brought
  something new of an issue), investigations it ran, changes
  on the page and wrong PINs; the other platform's status polls (about
  288 a day) and the last one, with a warning after 15 minutes without.
- **Days kept**: people per day; click a day to open it.
- **Load on Riverbed and the AI services** (and Active Directory, once it
  has been read): calls, rows, seconds waiting,
  retries, failed and cut short, per system, by hour, by what the app was
  doing and by data source; how long the Today reader's reads take
  against the minutes between them, and each start of the app. An AI
  service with no model list answers the connection light's check (every
  minute) with "not found": that is an answer, not a failed call. Rows
  are left out for a system whose replies have none (the AI services).

Kept in `$UXCORRELATE_DATA/usage/<date>.json`, 30 days, then deleted:
usernames, desktop names, addresses and Ask anything's questions. Not
counted: the Software reader's daily `$metadata` read.

**Names from Active Directory.** How it works: once the page is drawn,
it asks the app for the AD details of the usernames and desktops on it.
The app looks each one up with the read-only service account at most
once in 24 hours. The details stay in memory, never on disk, so a
restart looks them up again. The page never waits on AD: the names fill
in when it answers.

```
/admin drawn ─► /admin/api/ad: names not known yet
app: not read in 24 h? ─► AD: one search for users, one for PCs
─► sample40 · Pat Lee · Finance;  VD-04 in Desktops / Virtual
```

The domain controllers are found as `verify --ad` finds them (one
rule), and the DC that answered is kept for the next look-up. One line
at the top of the panel says when it cannot work:
- **not set**: no `AD_USERNAME` / `AD_PASSWORD` in `.env`;
- **did not answer** (and why): tried again after 5 minutes, however
  often the page asks; names already looked up stay;
- **refused the login** (a wrong name or password): not tried again
  until the app restarts, because each wrong try counts towards AD's
  lockout of the account. Correct `.env`, then `docker compose up -d`.

Each look-up is a log line ("Active Directory: looked up 4 users and 4
PCs on dc1… (3 and 3 found)").

### SecureView

How it works: the Today reader already reads every action each hour.
With SecureView on, it also reads the device the person sat at
(Aternity's `CLIENT_DEVICE_NAME`, the device a virtual desktop is opened
from). Each row whose device matches `secureview.match` widens that
device's day: first and last time heard, each person with their own
times, the desktops. Rows Aternity sends late, or a second read of an
hour, only widen a day.

```
14:07 sample46 on VD-10 from HV00046-SAMPLE46THIN
  └─► 2026-11-16: HV00046-SAMPLE46THIN
      first 07:08, last 14:07; sample46 07:08-14:07
```

Aternity has no connect or disconnect record: "heard" is an action on a
virtual desktop opened from the device, and a session nobody uses sends
nothing. Only devices Aternity has heard from are known (they are not in
AD).

**Turn it on** in config.yaml, then `docker compose up -d`:

```yaml
secureview:
  match: '^[A-Za-z0-9]{7}-.*THIN.*$'   # 7 characters, a dash, then THIN
```

Any case. Empty: off, and the column is not read. A pattern the app
cannot read is a note in `verify`, in the Connections dialog and on the
panel.

**The days before.** Once the first read is done, and each night at
03:00, Aternity is read day by day for each of the 90 days before today
not read in full, newest first:
- its **hourly data** while it has rows: each device's and person's
  first and last hour, and the desktops (6 hours a read; a read cut
  short is read again hour by hour). How long Aternity keeps it is not
  known: the read goes back until a day has none.
- then its **daily data** (kept about 90 days): the people and desktops
  of each day, no times.

```
Mon Nov 16  own records   07:10-15:46   (to the minute)
Fri Nov 13  hourly data   07:xx-17:xx   (the hour only)
Mon Oct 12  daily data    day only      (person, desktop)
```

What Aternity gives is kept apart from SecureView's own records: it
never moves a time SecureView saw itself. A day shows the records' minute
unless Aternity's hour is earlier (later): "08:05" stays when Aternity's
first hour is 08, "15:xx" replaces 14:10 when its last hour is 15. The
History tab says what it got ("88, back to Thu Aug 20. 34 with each
person's first and last hour (its hourly data, back to Tue Oct 13); 54
days only, with the people and desktops (its daily data)"), or that the
daily data names no device, or why it could not be read (tried again the
next night). Days read before Nov 17 2026 (devices only) are read once
more for their people.

**Where and how:** `/admin`, log in, the **SECUREVIEW** tab (or
`/admin#secureview` straight away).
- **TODAY** (opens first): devices in use today against the usual by
  this time of day (the latest 5 days of the same kind with times),
  heard in the last 30 min, people, first heard, first time ever heard.
  In use by hour (the band is the usual) with the 5 longest in use
  beside it. By department (the people's, from AD): a row each, across
  the page in 3 columns read down each one, most first; the groups that
  are not a department last, in grey. Click a row: the list shows that
  department's devices (the department box says it); click it again:
  every department. The list: search (device, person,
  department, desktop), a department box, sort by first heard, last heard
  or name; a green dot is heard in the last 30 min; "Show all" under 25.
- **HISTORY**: devices heard since the records began, heard in the last
  30 days, in use on a working day, first heard in the last 30 days.
  Click a box to list its devices: silent 30, 60 or 90 days or more,
  heard in the last 30 days, first heard in the last 30 days. Search and
  the department box (the last person's) narrow the list; "Show 25 more";
  **Download the list (CSV)** saves the list as shown. Click a device:
  its last 90 days as squares (faded: weekend), its latest 10 days with
  times, people and desktops, and **every day in Search ▸**.
  Click it again to close. Charts: devices per day (hollow: a day not
  read in full) and silent 30 days or more, day by day. By department
  (the last person's): heard in the last 30 days of those heard, "· 2
  silent 90+", in 2 columns; click a row: the list above shows that
  department (brought into view), "no person recorded" too. Then heard
  on the most days and the most different people.
- **SEARCH**: part of a username, a device or a desktop (2 characters
  or more, any case), then Enter or **Search**. It looks through every
  day kept (up to 400) and lists, latest day first:
  - **People**: each day, the device they sat at, their own first and
    last time, the desktops; "+1 other person" when someone else used
    that device that day; **All actions today ▸** opens ACTIONS for
    them.
  - **Devices**: each day, first and last heard, the people with their
    own times, the desktops.
  - **Desktops**: each day, the devices it was opened from and the
    people on it.

  The latest 30 days of a match, then **Show all N days ▸**; at most 20
  matches of each kind ("type more of the name"). Click any name in the
  results to search it. A username on TODAY or HISTORY (or in a device's
  days) opens SEARCH with it. **Download CSV**: a row per day and
  person, with how its times are known.

  Every day is a row: to the minute from SecureView's own records,
  "07:xx" (the hour only) from Aternity's hourly data, "day only" from
  its daily data.

  A person's real name ("Pat Lee"): when nothing SecureView recorded has
  it, one read-only AD search, every word in the display name, first
  name or surname. It lists who AD has (name, department, title, the
  account; **All actions today ▸** when Aternity has their username),
  then their days as above. AD not set, refused or not answering is
  said instead.

  ```
  "user 45" ─► SecureView: nothing
     └─► AD: Sample User 45 (account sample45)
          └─► SecureView: sample45's days
  ```

Names and departments come from AD as for People using the app (the
same look-up, 200 names a request). A device no one was named on, by
SecureView or Aternity, is "no person recorded".

Kept in `$UXCORRELATE_DATA/secureview/<date>.json` (device names and
usernames), 400 days, then deleted; `daily.json` notes the days read
from Aternity and which of them by the hour.

### Actions

Every action Aternity measured today for one person: any username
Aternity knows, THIN device or not. How it works: **Show** sends one
read of Aternity's activity for that username, from local midnight to
now, with only the columns shown and at most 5,000 rows. Nothing is
kept; it counts on the Load panel as "admin page".

```
sample49 ─► Show ─► Aternity: sample49, 00:00 to now
  └─► 38 actions, 00:00:58-04:25:37, 0 rated Major
       04:25:37  Sample Docs  Save Document  1.29 s ...
```

**Where and how:** `/admin`, log in, the **ACTIONS** tab
(`/admin#actions`), or **All actions today ▸** on SecureView's SEARCH.
- Type a username: the names Aternity sent in the last 8 days are
  offered as you type (the Investigate tab's list). A name typed in
  other capitals is asked for as Aternity writes it; one it has not
  sent is asked for as typed, and a note says so. Enter or **Show**.
- The top: the person (name and department from AD), how many actions,
  first to last, how many Aternity rated Major, applications, the
  desktops and the devices sat at.
- The list, latest first: time to the second, application, activity,
  response time, Aternity's rating (Major red, Minor amber), the desktop
  (virtual or physical), the device sat at. The first 200, then **Show
  all**.
- **Every application** box: one application only. **Read again**: a
  new read. **Download CSV**: the list as shown.
- More than 5,000 actions, or a reply Aternity cut short, is said above
  the list; a cut-short reply with nothing in it is "not known", never
  "no action".

## Quick start

The tool needs a live Aternity and NetIM; see "Connect to Riverbed" below.

### Docker

```bash
docker compose up -d --build
docker compose ps                 # STATUS shows "(healthy)" after ~30 s
```

The image holds only Python and the libraries; `compose.yaml` mounts the
code from `./src`. To fix code, edit `src/uxcorrelate/*.py` and run
`docker compose restart`; rebuild only when dependencies in
`pyproject.toml` change (every library's version is pinned in
`constraints.txt`; bump `x-image` with it). The settings block at the top of `compose.yaml`
lists what to do before the first start. Healthy means the web server is up; it
does not mean Aternity or NetIM are reachable (`verify` checks that).

**Web UI** ("J6 Preservation Society", then `title` from config.yaml
in the top bar and the browser tab). Open `http://<docker-host>:8080`
or `https://<docker-host>:8444`. The `ports` setting in `compose.yaml`
decides who can reach it: `"127.0.0.1:8080:8080"` is the Docker host only,
`"8080:8080"` is the whole network. The UI has no login.

HTTPS is port 8443 in the container, 8444 on the host (the host's 8443 is
haproxy's). It needs the certificate and key in `./certs`
(`ssl.lab.int.crt`, `ssl.lab.int.key`, not in git), readable by uid 1000,
the container's user. Missing or unreadable, the log says "HTTPS (port
8443) is off" and the page stays on http. To turn HTTPS off, comment out
the TLS lines of `command` and the `8444:8443` port in `compose.yaml`.

**CLI.** Run it inside the running container:

```bash
docker compose exec uxcorrelate uxcorrelate verify
docker compose exec uxcorrelate uxcorrelate investigate -u <user> -a "<app>" --last 4h
docker compose exec uxcorrelate uxcorrelate investigate -u <user> --json > report.json
docker compose exec uxcorrelate uxcorrelate investigate --help
```

`-o` writes inside the container. To keep a report, redirect
stdout to a file on the host, as shown above. Without Docker, run the same
commands as `uxcorrelate -c config.yaml <command>` (`-c` goes before the
command).

### Air-gapped live host

The live host has no internet, so it never builds. On the dev host:

```bash
scripts/make-bundle.sh        # -> dist/uxcorrelate-<version>.tar
```

Copy that one file to the live host. It holds the runtime image, `src/`,
`compose.yaml`, `config.example.yaml`, this README and `INSTALL.txt`, which
has the install, code-fix and upgrade steps.

### Kubernetes

`k8s/uxcorrelate.yaml` runs the same image as one pod, with `src/`, `data/`
and `config.yaml` in the app folder on NFS, and passwords in a Secret made
from `.env`. The pod serves both ports, as compose does: 8080 (http) and
8444 (https, with the certificate from the Secret `uxcorrelate-tls`). The
way in is a file of its own, and switching is one `apply` and one `delete`,
with nothing to edit:

```
k8s/ingress.yaml       browser ─https─▶ ingress ─▶ :8080
k8s/loadbalancer.yaml  browser ─https─▶ LB :8444 ─▶ :8444
```

Port 8080 takes the user's address from the ingress's X-Forwarded-For
(`FORWARDED_ALLOW_IPS`). Port 8444 never takes one from a browser
(`cli.py`): there a browser could claim any address, or HTTPS on plain
http. On the load balancer, `externalTrafficPolicy: Local` keeps the
nodes from replacing the users' addresses; the admin page shows whether
the load balancer itself does ("every visit from one address").
Kubernetes checks the pod at `/api/health` (left out of the log). The
top of `k8s/uxcorrelate.yaml` has the full steps: what to fill in, first
time, switching the way in, day to day (code fix, passwords, certificate,
logs, verify) and going back to compose. One pod only: a second would
read Aternity twice.

### Sample data for the dev app

The dev host has no Riverbed. `dev/sample_riverbed.py` is a made-up Aternity
and NetIM the dev app reads through its own clients, exactly as a real
tenant: the real Aternity `$metadata` (`tests/fixtures`), its filters,
paging and added-up rows, and NetIM in Riverbed's layouts. It is not part
of the product (there is no demo mode) and the bundle leaves `dev/` out.

```
docker compose -f dev/compose.yaml up -d    # the made-up Riverbed
docker compose restart                      # the app reads it from now on
dev/page_check.sh                           # the page check, before a copy to stage
```

The page check opens the pages in a real Chrome (the `uitest-chrome` image
on the dev host) and counts what is drawn against every number that stands
for it: a button's number against everything that looks raised under it
(amber bars, bold words, ▲, each counted on its own) and the lines of the list,
a tab's number against its tile and its lines, "Show the N PCs" against its
count, a count of 1 with a plural after it, script errors; then every count
the app gives against the list behind it. It looks at a made-up Software tab
with the shapes seen on stage and at the dev app's every tab. Run it right
after a restart of the app; it must end with "0 do not".

How it fits together:

```
dev/compose.yaml --> sample-riverbed:8000 (on the app's network)
   at start: past 21 days' counts --> data/days (the Today usual)
app (config.yaml) --> /aternity.odata/v2.0  and  /netim
```

The dev `config.yaml` points at it (both base URLs
`http://sample-riverbed:8000/...`, `timezone: America/New_York`, the
made-up NetIM names under `topology`, a `care_list` of made-up users).
Users, PCs, programs and addresses are all made up (sample01..50,
PC-001..036, VD-01..16, TEST-NET addresses). The same rows come back at
every read; the story is set against today:

- sample07 / PC-007: a failing disk (Faulty HD S.M.A.R.T. Status since
  3 days ago, HD Failure today).
- Sample Mail updated last night on PC-001..014; crashes in mailcore.dll
  on the new version today.
- Sample Branch South: Sample ERP slow 10:30-11:15, the branch's WAN
  link (south-br-rtr1 Gi0/1) over 90% in NetIM.
- PC-019: CPU 88-98% 11:00-13:00 (sampleindexer.exe).
- sample12 / PC-012: a System Crash at 14:05, then a start-up.
- sample23 / PC-023: Sample Chat hangs every 20 minutes, every second
  one a crash (the Care list's alert stays on).
- Virtual desktops: slow logons this morning (User Profile).

The settings block at the top of `dev/sample_riverbed.py` holds the time
zone, how long each kind of data is kept (activity 8 days, as prod; the
hourly sums 35 days, made up: prod's is not known) and the seed;
`uxcorrelate verify` passes against it.

The same compose file starts a made-up Active Directory for
`verify --ad`: `sample-ad`, a real Samba domain controller, so DNS SRV
records, LDAPS, StartTLS and logins are real. `dev/sample_ad.sh` fills
it: domain `ad.sample.test`, sample01..47 (sample48 disabled; sample49
and 50 not in AD), PC-001..034 and VD-01..16 (PC-034 disabled; PC-035
and 036 not in AD), and the service account named by `AD_USERNAME` /
`AD_PASSWORD` in the repo's `.env`. Dev's `config.yaml` has
`ad: {domain: ad.sample.test, dns: [sample-ad]}`. Samba has no
Microsoft NTLM bind for LDAP, so the NTLM tries are not run on dev.

## Connect to Riverbed

### What you need

- **Aternity:** the tenant's OData URL (it ends in
  `/aternity.odata/v2.0`), an Aternity user name, and that user's REST API
  access token. An Aternity admin creates the token on the REST API Access
  page in Aternity's settings; the URL is shown on the same page.
- **NetIM:** the NetIM core URL (`https://<netim-core>:8543`) and a
  **local** NetIM user with ADMIN rights. Riverbed's docs say the NetIM
  metric APIs take HTTP basic auth only, so SAML and TACACS users do not
  work.
- **Network:** the Docker host must reach both URLs over HTTPS. Their
  certificates are not checked, so self-signed and internal-CA certificates
  work.

### Steps

1. Create the config and fill in every `<REQUIRED>` value (the two URLs and
   two user names):

   ```bash
   cp config.example.yaml config.yaml
   chmod -R o+rX src config.yaml   # container user uid 1000 must read them
   ```

2. Put the passwords in `.env` (gitignored, never in the image):

   ```bash
   cat > .env <<'ENV'
   ATERNITY_PASSWORD=<REST API access token>
   NETIM_PASSWORD=<NetIM password>
   ENV
   chmod 600 .env
   ```

3. Start it: `docker compose up -d --build`.

4. Check every name against the live systems:

   ```bash
   docker compose exec uxcorrelate uxcorrelate verify
   ```

   It prints one line per check, `OK`, `FAIL` (with a `fix:` line) or `--`
   (skipped), and ends with "All checks passed." or the number of problems.

5. For each `FAIL`, change the named setting in `config.yaml` to the real
   name that `verify` suggests and run `verify` again. `config.yaml` is
   re-read on every run, so no restart is needed.

6. Fill in `topology` (which servers each app runs on and which NetIM
   routers serve each location), run `verify` again, then investigate:

   ```bash
   docker compose exec uxcorrelate uxcorrelate investigate -u <user> -a "<app>" --last 4h
   ```

### What `verify` checks

Everything it finds stays on the live host's screen; nothing has to come
back to dev.

- **Aternity:** it logs in and reads `$metadata`, the tenant's own list of
  data sources and columns. Every configured data source and column must be
  in it; for a missing one it prints the closest real name **as YAML to
  paste into `config.yaml`**. Every `topology` application and location must
  have activity in the last 7 days. Then it prints the min / median / max of
  response time, CPU, memory, disk queue, free disk space, round-trip time
  and UXI over the last day next to each threshold, so you can see the
  units match, and the activity status values seen (for
  `thresholds.slow_status`).
- **NetIM:** it logs in and lists devices; every `topology` device must be
  one of them. It checks interface IP addresses can be read (that is how
  users are placed on the network), lists every metric class, checks each
  configured class and metric (printing the class's real metric names), and
  fetches each metric for real for the last hour. Corrections come as one
  `netim.metrics` block to paste.
- **Virtual desktops (VDI):** which VDI and desktop-management columns
  the tenant publishes and how they are filled; for one user per kind of
  desktop, how many PCs they used in 14 days and how many users their PC
  had (one desktop each, or pooled); the remote display latency source if
  published; with NetIM set too, which addresses NetIM can place: the
  virtual desktops', physical PCs' and the devices users sit at (it reads
  every NetIM device's interfaces, about half a minute).
- **Put-backs:** for software removed and put back at the same version on
  a PC, the PC's nearest start-up each time, its image builds, and how many
  other PCs have change-log rows in the same minute.
- **Process data:** every published data source with a process column,
  whether it has rows, and its CPU/memory/IO columns.
- **PCs that are gone, on the Software tab**: Aternity takes a PC off
  its device list about a month after it last heard from it, but keeps
  its installed software (stage, Nov 20 2026: 1,266 of 6,464 PCs).
  The Software tab counts only PCs still in the device list: once a
  day it reads the device list, then the software of the PCs not in
  it (25 PCs a read), and takes that off Aternity's totals. The first
  tile says "N more left out: no longer in Aternity's device list";
  the lists opened by a click leave the same PCs out. When the device
  list or their software cannot be read whole, or more than half the
  PCs are missing from the device list, nothing is left out and a
  note says so. `verify --inventory` (4.) times that read.
- **What Aternity knows of each PC** (`verify --inventory --columns`,
  the 5th answer of `verify --inventory`): for each column an
  Inventory tab could show (model, memory, system disk, last start-up,
  antivirus, agent version, ...), on how many PCs it is
  filled and what its values look like. One read of the device list.
  No PC, user or site names.
- **Which applications have measured actions** (`verify --actions`,
  only on its own): the applications Aternity measured actions of in
  the last 7 days, each with its actions, those rated Major and its
  activities; the activities of the first ones by name; the
  applications in use with no measured action. Two reads added up by
  Aternity. Application and activity names only.
- **PCs in use, per day and per month** (`verify --usage`, only on its
  own): what the Inventory tab's usage charts rest on, read the same
  way. 1. How far back Aternity keeps daily app usage. 2. For
  each of the last 14 days, the PCs an application ran on, by
  Aternity's kind of device, each with its share of that kind's PCs in
  the device list; a typical working day, the busiest day, a typical
  weekend day. 3. Whether Aternity fills the username there, and people
  a day. 4. The PCs used at least once in this month and the three
  before, as far as Aternity keeps them. About 20 small reads. Counts
  only: no PC or user names. A day Aternity has no rows for (today, on
  the live one) says so; it is not "0 in use".
- **PCs that are gone** (`verify --inventory`, only on its own): three
  answers in plain sentences, no PC names. 1. Aternity's device list:
  how many PCs it heard from today, 1 to 7, 8 to 30 and 31+ days ago,
  how many have no date, and the one silent the longest. 2. The status
  word Aternity gives each PC, as it sends it. 3. The PCs that are in
  the installed software list but not in the device list, and how many
  of them ran an application in the last 8 days: those that did not are
  most likely retired PCs the Software tab still counts. Riverbed's
  pages give no rule for a PC that no longer exists, so this counts it.
- **Devices people sit at** (`verify --clients --except WT`, only on its
  own; `--hours N`, 1 to 24, default 1): virtual desktop actions by the
  device the user sat at (`CLIENT_DEVICE_NAME`, Riverbed: "the hostname
  of a device which is connecting to a VDI"): how many, how their names
  begin, the 25 not starting with WT with the most actions by name (the
  desktops each was on, its users counted, its `LOCATION_ON_VPN` values),
  and each `LOCATION_ON_VPN` value as sent: how many devices under it
  start with WT and how many not, and physical PCs. Several beginnings:
  `--except WT,TC`.
- **Active Directory** (`verify --ad`, read only; also at the end of a
  full `verify` once an `ad:` block or `AD_USERNAME` is set): the
  domain (`ad.domain`, else this host's DNS search domain and those
  above it), its domain controllers from DNS SRV records (`ad.servers`
  to name them, `ad.dns` to ask another DNS server), on up to 3 of them
  whether LDAPS (636), 389 and StartTLS answer, and a DC's own header
  with no login (its name, the domain's base, its level). With
  `AD_USERNAME` / `AD_PASSWORD` in `.env` (a read-only service
  account): the login, over LDAPS, then StartTLS, then NTLM. A password
  never goes over plain LDAP, and a refused password stops the tries
  (each wrong one counts towards AD's lockout). Then: how many of
  Aternity's 50 most active usernames and PCs of the last hour are in
  AD, and how Aternity writes them (`DOMAIN\name`, `name@domain`, the
  name alone). Counts only. `--user NAME` / `--pc NAME` show that one
  user's or PC's AD details (name, title, department, office, enabled;
  OS, OU, description, last logon), or the names close to it.
- **A reply it cannot read** (device addresses, interface addresses, metric
  data): it prints the reply's key names, never values, and which function
  in `./src/uxcorrelate/netim.py` to fix. Edit it there and run
  `docker compose restart`; then bring the same fix to dev by hand.

### Virtual desktops

Aternity marks each action on a virtual desktop (`VIRTUALIZATION`). Live
desktops are pooled (a user gets another one each time), so a user's
start-ups on virtual desktops are compared with each other, and "Compared
with normal" says when the last normal run was on another desktop. "What
the user felt" names the virtual desktop and the device the user sat at;
"What was checked" says the network between them is not checked (Aternity
gives no delay for it).
What Aternity does not give us yet (remote display latency, network round
trip, top processes, ...) is listed for the Aternity admin in
`docs/aternity-admin-questions.md`.

### Defaults and where they come from

Only the URLs, users, passwords and topology are yours to fill in. The rest
has defaults in `src/uxcorrelate/config.py`, each taken from Riverbed's own
material and pinned by `tests/test_contract.py` (`scripts/make-bundle.sh`
refuses to build a bundle if a test fails):

- **Aternity** data sources and columns: all in a real Aternity API 2.0
  `$metadata` (`tests/fixtures/aternity_metadata_v2.xml`).
- **NetIM** paths, paging, the metric-data request and reply layout,
  `IFC_UTIL` (`utilizationIn`, `utilizationOut`): Riverbed's
  `steelscript-netim` SDK. `CPU_UTIL` (`cpuUtil`) and basic auth with a
  local user: Riverbed's NetIM documentation.
- **Not in any public Riverbed source**, so left to `verify` on the live
  host: the layout of interface IP addresses, the units of each value, and
  the alerts path (alerts are off until you set `netim.alerts_path` from
  NetIM's HELP > API Documentation).

## REST API

`uxcorrelate serve` serves the UI at `/` and interactive API docs at `/docs`.

```bash
curl -s localhost:8080/api/investigate -H 'Content-Type: application/json' -d '{
  "username": "<user>", "application": "<app>",
  "start": "2026-10-27T09:00:00Z", "end": "2026-10-27T12:00:00Z"}'
```

The response holds `answer` (the plain-language part), `summary` (counts
and the slow rule), `problems` (one row per object and measurement, with
`slow_actions` of `of_slow`), `impact` (other users), `timeline` (`actions`
and `marks`), `endpoints`, `mapped_objects` and `warnings`.

## How the clients call Riverbed

- **Aternity**: basic auth (user name + REST API access token). Queries the
  data sources in `aternity.entities`, filters on the timestamp column,
  follows `@odata.nextLink`. Columns missing from a row are skipped.
- **NetIM** (as Riverbed's `steelscript-netim` SDK does): devices and
  interfaces come from `{api_prefix}/devices` and `/devices/{id}/interfaces`,
  paged with `offset`/`limit` and `meta.next_offset`. Metrics come from a
  `POST` to `metric_data_path` with `elementIds`, `elementType` (`VNES_OE`
  for devices, `VNES_INTERFACE`), `metricClass`, `metrics`, `startTime`,
  `endTime` (epoch ms), `includeSamples` and `metricEpochEnum: RAW`; the
  reply is read from `metricData.metricElementDataList[].timestampToValuesMap`.
  If a metric or the alerts endpoint fails, the run
  continues and a warning is added to the report.

To add another data source (for example NetProfiler or AppResponse), implement
the small `AternitySource` / `NetimSource` protocols in
`src/uxcorrelate/correlate.py`.

## Layout

```
src/uxcorrelate/
  aternity.py    Aternity OData client
  netim.py       NetIM REST client + tolerant metric parsing
  correlate.py   mapping, problems, matching to slow actions
  normal.py      each slow action next to its own normal and what changed
  verify.py      checks the config against live Aternity / NetIM
  report.py      text report
  cli.py         command line
  inventory.py   the Inventory tab: Aternity's device list, hourly, in memory
  usage.py       the admin page's counts and who used the app
  secureview.py  the admin page's SecureView (devices people sit at)
  actions.py     the admin page's Actions (one user's actions today)
  web.py         FastAPI app;  static/index.html  UI, static/admin.html  /admin
tests/           pytest suite (HTTP mocked with httpx.MockTransport;
                 synthetic.py is a fake Aternity/NetIM for engine tests)
dev/             made-up Aternity + NetIM for the dev app (not bundled)
```

Run the tests with `pytest`.
