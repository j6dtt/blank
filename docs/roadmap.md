# Roadmap

What is being built, what is agreed, what waits on a check or a
decision, and the ideas not chosen yet. Updated as we agree on
things; each decision is dated. Made 2026-11-15.

Status words:
- **Now**: being built.
- **Agreed**: decided, not started.
- **Waiting on you**: a check on stage / prod, or a choice.
- **Idea**: proposed, not chosen.
- **Parked**: on hold, kept in step.

## Now

### Admin page (`/admin`)
Usage statistics and active users, behind a login, no link from
the main app. First version committed 16d699b (2026-11-15).
Its own roadmap: `docs/admin-roadmap.md` (decided, first
version, limits, later ideas, the discussion so far).

### SecureView (admin page)
The devices people sit at to work outside the office (THIN,
`secureview.match`): first and last heard per device per day,
Today and History, silent 30 / 60 / 90 days, by department from
AD. Built on dev 2026-11-17 ("build it"), 294bd37; on stage the
same day: the daily data names the devices, History back to
Aug 19. Then the admin page in two tabs, Statistics and
SecureView, and By department across the page (option B), both
built on dev 2026-11-17 ("build it"), bb76884. Then SEARCH and
ACTIONS, built on dev 2026-11-17 ("build it", with the AD name
search): SecureView's third sub-tab searches every day kept by a
username, device, desktop or a person's real name (AD); a third
admin tab, ACTIONS, lists every action of any user today, read
from Aternity on Show; pushed 8301765, on stage. Then every day of
people and times in Search (the user: "first heard and last heard
also for all dates"), built on dev 2026-11-17: Aternity's hourly
data, then its daily data with the people and desktops. See
`docs/admin-roadmap.md`.

## Waiting on you

- **Software tab: the headline box is gone, the same PCs decide,
  and a full review for discrepancies (the user, 2026-11-22, on two
  photos of stage: "Am I seeing discrepancies here? Look at the
  headline panels and comparison below. It's either confusing still
  or don't match"; "What's the purpose of the headliner and when do I
  see it?"; "Fix the discrepancies, I still don't know what they are
  'cause it's confusing to look at. GET RID OF THE HEADLINE, I think
  it add more confusing. Thooughts?"; "Add percentage to tables";
  "Move version by version on top of before and after"; "DO FULL
  CODES REVIEW FOR GAPS, DISCREPANCIES ON SOFTWARE and OTHER").
  Pushed db7c701, 434 tests; on stage 2026-11-22 (two photos):
  the rule works, 5 of the 9 version flags still wrong; the user:
  "fix it"; fixed the same day (47d6300, 436 tests); on stage
  2026-11-22 (third photo): works. Seven more photos the same
  day: one count does not add up; fixed with a page check in
  the repo (a0acbd5, below); on stage the same day: the
  second count still read as a raise; fixed again on dev;
  stage copy of 1 file next, then prod (11 files).**
  - On stage (photo, same application, virtual desktops, a read
    a day later so the counts moved a little): no headline box,
    "Version by version" on top. X crashed on 149 of 2,476 (6%),
    ▲ against 1.2% on Y (2 of 163), "the 51 PCs that ran both:
    10 with this version, 0 with Y": the table under it says 10
    of 51 before and 0 of 51 after. X stopped responding on
    1,294 of 2,476 (52%) against 29 of 163 (18%): "not raised:
    the 51 PCs that ran both had it on 6 with this version and
    on 8 with Y", and the table says 6 of 51 and 8 of 51. The
    71 PCs that came to X: a crash on 1 before and 2 after, "not
    raised: only 2 PCs with a crash after, 5 needed". Every ▲
    and every "not raised" matches its table.
  - Gap seen on that photo (fixed with the three below): the 51
    PCs went from X to Y, and Y has the lower version number: a
    move to an older build, which the page titled "Before and
    after the update".
  - On stage, second photo (2026-11-22, the red flags list): 14
    lines, 9 of them about a version (6 "Crashes more", 3 "Stops
    responding more"). 4 hold: Edge (269 of 2,066 against 1 of
    62), Outlook (149 of 2,476 against 2 of 163, the 51 PCs: 10
    and 0), Windows Explorer after its update (190 PCs: 19 after,
    6 before), Remote Desktop Connection (17 of 103 against 3 of
    110). 5 should not be there as they are:
    - 4 are one version under two spellings (the gap of Nov 21,
      held then as "not on its own"; see Agreed): Desktop Window
      Manager 10.0.22621.7219 (46 of 46 against 0 of 38 on the
      same number with "(WinBuild...)"), VLC media player
      3.0.20.0 (43 of 43 against 0 of 798 on 3.0.20), OneDrive
      24.221.1103.3 (27 of 27 against 0 of 416 on
      24.221.1103.0003, physical PCs), Task Manager
      10.0.22621.6133 (24 of 24 against 0 of 292). On Nov 21 the
      rule passed such twins over for other reasons (new that
      week, too few PCs); it no longer does for these four.
    - 1 is a gap in the rule of this entry: Teams, 16 of 102
      (16%) against 191 of 2,801 (6.8%), with "of the 2,403 PCs
      that ran both, 101 with this version and 89 with the
      other". The rule drops a flag only when no more of the
      same PCs had it on this version; 101 is more than 89, so
      it stayed, though the same PCs do not show "more" by the
      tab's own measure.
  - **Fixed on dev (the user, 2026-11-22: "fix it"), 436 tests,
    not committed; one stage copy of 6 files, prod after:**
    1. One version, one row. Spellings with the same number are
       one version (the dotted number at the start, each part as a
       number, no zeros at its end; a label in brackets left out,
       other text has to be the same), put together before
       anything is counted, in the rows and in each PC's days.
       The row says "also written ..."; Explain, Compare, the
       chat and `verify --versions` say what was put together.
       Different from the plan of Nov 21 in one point: the name
       kept is not simply "the spelling on the most PCs" but, in
       this order, the one the tab already shows, not the plain
       a.b.c.d of the rows with a crash (all three cases seen on
       stage), then the most PCs. Why: a check of every count on
       dev showed Compare keeping the other spelling than the
       tab; and Desktop Window Manager on stage has more PCs
       under the crash rows' spelling (46) than under the other
       (38), so "most PCs" could flip from day to day.
    2. The same PCs must show it. When 5 or more of the PCs that
       ran both had the problem on one of the two versions, the
       version is raised only when they pass the tab's own checks
       themselves (2 times as many of them with it, still so with
       2 more on the other); else "not raised" with their counts
       and why ("not 2 times as many", "2 more with ... would undo
       it"). Teams on the photo: 101 against 89, not raised.
       Outlook's crash flag: 10 against 0, stays.
    3. A move to a lower version is a "change of version": the
       table's title, the count's words, the flag's sentence, the
       list behind it, Explain, Compare, verify.
    - Expected on stage (not certain: the merged versions are
      judged again with their real counts): the 4 twin flags go,
      Teams goes, Edge, Outlook, Windows Explorer and Remote
      Desktop Connection stay.
    - Checked on dev before the copy: the sample now writes a row
      with a crash under a second spelling, as stage does; every
      count against its list again by script (129 agree, the one
      that does not is Compare read a minute after the tab, said
      on the page); a scripted Chrome over the dev page and over
      a made-up page with the stage's numbers (101 against 89, a
      move to a lower version, two spellings): the words, the
      list behind a count, Compare, Explain, dark, a phone's
      width, no script error; `verify --versions` on dev.
    - Not changed, said plainly: crash events by the failing
      part's version (another source; no twin seen there); the
      installed list's "2+ versions on one PC" (two installs are
      two rows there, whatever their spelling); investigations'
      match of a change's version with a failing part's (text,
      its own tests; no miss seen on prod).
  - On stage with 47d6300 (third photo, 2026-11-22, the red flags
    list after its read; a photo before it was of the read before,
    taken too early): 13 lines, 8 about a version. Gone, as
    expected: the 4 twins (Desktop Window Manager, VLC, OneDrive
    24.221.1103.3, Task Manager "24 of 24") and Teams (101 against
    89). Stay: Edge (269 of 2,066 against 1 of 62), Outlook (149
    of 2,476 against 2 of 163; the 51 PCs: 10 and 0), Remote
    Desktop Connection stops responding (17 of 103 against 3 of
    110), Windows Explorer on virtual desktops after its update
    (now 203 PCs: 22 after, 7 before; it was 190: PCs with a day
    under the other spelling are in it now). New, each by the
    rule with its real counts now that a crash is on its own
    version: OneDrive 26.153.0809.0004 after its update (1,376
    PCs: 50 after, 22 before; narrow, 48 needed); Task Manager
    10.0.22621.6133 (24 of 300 against 0 of 55; it was "24 of 24"
    beside "0 of 292"; narrow); Remote Desktop Connection
    10.0.22621.7649 crashes (10 of 110 against 2 of 103: the same
    two versions as its other flag, the other way round, another
    problem); Windows Explorer 10.0.22621.7711 on physical PCs
    after its update (29 PCs: 8 after, 1 before).
  - Gap found while reading the code for that photo, not fixed
    (proposed for the next copy): when a read of the Software tab
    fails and an earlier read is on the page, the tab says
    nothing (`sStatus` is filled only while reading; the Inventory
    tab says it) and the next try is 24 hours later.
  - On stage, seven photos (2026-11-22, the six applications
    open on virtual desktops and the red flags list; the user:
    "That number doesn't add up in stage"). Gap, mine: the
    switch says "Virtual desktops · 7 ▲" and the tables under
    it carry 8 ▲. One version of one application is marked
    twice for one problem (stops responding: 147 of 410 against
    12% in "Version by version", and 22 of 203 after against 7
    before in "Before and after the update"); the red flags
    list has one line for it and the switch counts as the list
    (the review of db7c701 changed the switch from 2 to 1 and
    checked it against the list by script, not against the
    marks drawn under it). The rest adds up: 13 lines in the
    list = the tab's 13 = 8 about a version (7 virtual, 1
    physical) + 5 of other kinds; every ▲ and every "not
    raised" matches its own table's counts.
    - The user: "WE REVIEWED THE CODES TO ELIMINATE ALL GAPS AND
      BUGS. WHY HAPPENED AGAIN? GETTING VERY FRUSTRATED". Why: the
      review compared numbers with numbers by a script that was
      not kept, and never counted what the page draws. So the
      method changed, not only this count.
    - Fixed on dev the same day (436 tests; not committed):
      one red flag = one line in the list = one ▲ in the tables
      = one on its button (`swMarks` in `index.html`). The ▲ is
      on the count the list's line quotes (its update first, the
      biggest first, else its row in "Version by version"); a
      second raised count of the same version and problem keeps
      its amber bar and says "Counted as one red flag with its
      update, below" (Explain and the "i" legend say the same).
      On stage the button should stay "7 ▲" with 7 ▲ under it.
    - The page check, in the repo: `dev/page_check.sh`
      (`dev/page_check.py`). A real Chrome opens a made-up
      Software tab with stage's shapes and the dev app's every
      tab and counts what is drawn against every number that
      stands for it, then every count the app gives against its
      list. On the page as it is on stage it said "button 8 / ▲
      drawn 11 / list 8"; on the fixed page 178 agree, 0 do not.
      To run before every copy to stage that touches a page.
    - Two more gaps it found, fixed with it: "1 smaller version,
      1 virtual desktops" (`index.html`); "1 programs have ...",
      "1 PCs in all", "1 Windows updates are ..." in the red
      flags' sentences when a count is one (`software.py`; the
      test had the wrong words as its expectation).
    - The failed read is said on the tab now (`index.html`: the
      reader's own sentence, "The page shows the last read until
      then", as the Inventory tab). Not changed: the next try
      after an error the code did not expect is 24 hours later.
    - Not fixed, not on a page (for the next copy of those
      files): "N programs" with a count of one in a chat note
      (`software_chat.py`) and in a line of verify --software
      (`verify.py`).
    - Stage copy: `src/uxcorrelate/static/index.html` and
      `src/uxcorrelate/software.py`. Prod after it: the eight
      files in one go.
    - On stage with a0acbd5 (photo, 2026-11-22): the new page
      is there. The button says "7 ▲"; the count "147 of 410"
      has its amber bar and no ▲ and says "Counted as one red
      flag with its update, below"; the sentence beside the
      buttons says what their number is. The user: "Still says
      the same" (the button's 7, which was left as it was on
      purpose: the extra ▲ went, not the number). Asked which
      number the button should show: "7 is right".
    - Wrong, said plainly: the user had not meant the number.
      "No. Button says 7 but I count 8 raises just like
      before". a0acbd5 took the ▲ off "147 of 410" and left
      its amber bar and bold words, so it still looked raised
      (the page's own legend: an amber bar is raised). The
      page check had counted ▲ only: the same mistake as the
      morning's, checking my own idea of "drawn" and not what
      a reader counts.
    - Fixed on dev (436 tests; not committed): a red flag is
      drawn raised once. A raise is the amber bar, the bold
      words, the ▲ and "Show the N PCs" together (`swCell`'s
      `up`), never one without the others. A second count of
      the same version and problem that passes the checks too
      is drawn as every other count that is not raised: a
      green bar and the muted words "not raised a second time:
      this version is raised after its update, below" (a
      second update: "... after the change from X, above").
      Explain says the same and does not count it as raised.
      The page check now counts each sign of a raise on its
      own (amber bars, bold words, ▲, "Show the N PCs") against
      the button and the list: on the page of a0acbd5 it says
      "button 8 / amber 11 / bold 11 / ▲ 8 / list 8", on the
      new page 174 agree, 0 do not. It also caught a script
      error of mine in Explain before any copy. A picture of
      the new table was sent to the user before the copy step.
      Stage copy: `src/uxcorrelate/static/index.html` only.
    - Prod list corrected: 11 files, not 8. The 8 took it that
      prod already had the usage charts and the look switch
      (cfea4d4); nothing I have says so. The last version
      known on prod is the one of Nov 21 (596cd86, its verify
      checks ran there), so prod gets every file of `src`
      changed since: `cli.py`, `config.py`, `explain.py`,
      `inventory.py`, `software.py`, `software_chat.py`,
      `usage.py`, `verify.py`, `web.py`, `static/admin.html`,
      `static/index.html`. No change to the image, compose,
      the Kubernetes files or config.yaml.
  - Seen on stage now: the "also written" line, "Before and
    after the change of version" (51 PCs that went to a lower
    version). Not seen yet: the Compare dialog, the Inventory
    tab without BitLocker.
  - What the photos showed (an application on virtual desktops; X =
    the version most have, 2,475 PCs; Y = the next, 165 PCs; Z =
    the version before X):
    - Headline "X stops responding more": 52% (1,291 of 2,475)
      against 16% on Y (27 of 165). But on the same PCs with the
      same days on each side: the 53 that went from X to Y stopped
      responding on 7 with X and on 9 with Y; the 71 that came to X
      from Z, on 36 before and on 15 after.
    - Headline "X crashes more": 5.9% (147 of 2,475) against 0.6%
      (1 of 165). The 53 PCs bear it out (8 with X, 0 with Y), but
      its "Its update" line named only the 71 (1 before, 2 after).
    - X had three percentages for one thing (5.9%, 2.8%, 15%):
      different PCs and days each time, which the page did not say.
  - Built:
    1. The same PCs decide. A version worse than another is raised
       only when the PCs that ran both versions (20 or more went
       from one to the other) do not say otherwise: when 5 of them
       or more had the problem on one of the two and no more of
       them had it on this version, it is not raised and its count
       says why with their numbers. On the photos: the "stops
       responding" flag goes, the crash flag stays and gives the 53
       PCs' counts. The tab's count of flags will move at the first
       read after the copy; `verify --versions` (the rule before and
       the rule now) names each one that went for this reason.
    2. No headline box over an application. The ▲ is on the count it
       is about, with what it is against, the PCs that ran both and
       "Show the N PCs ▸". Why and what next are in "Explain ▸".
    3. "Version by version" is on top, "Before and after the update"
       under it. Each says the days it counts.
    4. Percentages in every "Table ▾" of the tab, beside each count;
       the Compare chart writes each share beside its counts too.
  - The review, Software tab: 8 more gaps found and closed (listed
    in docs/software-tab.md): Compare counted all PCs together under
    tables per kind of PC, and its before and after counted PC-days;
    the list behind "more after the update" held PCs of other
    updates; the switch counted a version twice; "the version most
    have" was said of a version with fewer PCs in a roll-out week; a
    1.3% bar was as long as a 52% one beside it; "Hangs more" beside
    "Stopped responding"; the table under an application was all PCs
    mixed; a list read now against a count read in the morning.
  - The review, the other tabs: 118 counts compared on dev by a
    script, each against the list or panel that should match it
    (Today's Major actions, PCs and users against its hours, its
    lists and each application's list; active PCs and users; the
    Care list's tiles against their bars, their lists, the tab's
    count and the status feed; the Inventory tab's tiles, lines,
    lists, last heard, statuses and usage charts; the Software
    tab's flags, counts, lists, Compare and before and after). All
    agree after the fixes; the one that did not was Compare read a
    few minutes after the tab (3,431 actions against 3,429), which
    the page now says.
  - Not covered by the review, said plainly: the dev day was a
    Sunday with no Critical or Major event, so Today's event tiles
    were compared by reading the code (one list, `Today.health`,
    feeds the tile and its list), not by numbers; investigations'
    wording was not re-read (it has its own snapshot diff and
    real-case tests); the admin page was not cross-checked.
  - The Software tab's drawing (docs/diagrams/05) has the new rule.
  - **The logic drawings (the user, 2026-11-22: "Update logic
    diagrams and add missing if needed").** All eight are made from
    the code's own settings by `docs/diagrams/make_diagrams.py`.
    - Updated: 05 Software (the same PCs decide; the page without
      the headline box, version by version first, percentages in
      the tables; Compare and before / after counted as the tables;
      "Stops responding more").
    - New: 07 Inventory tab (the hourly read, what stays in memory,
      the 8 "Needs a look" lines, last heard from, PCs in use per
      day and per month, the safety rules, what a click does).
    - New: 08 Ask anything, Explain and the top bar (what every tab
      shares: what the AI is given, the look-ups with and without a
      read of Aternity, the limits per question, how numbers are
      checked, the connection lights, the names offered, the look).
    - Checked and left as they are: 01 Today, 02 Care list, 03
      Investigate, 04 Start-ups, 06 Admin page (made again from the
      code: no number in them changed, and each sentence still says
      what the code does).
    - The export crashed on dev ("Illegal instruction") until
      draw.io ran with more shared memory and without the
      container's system-call filter; the command that works is in
      the script's header.
  - Also seen on the photos: the dark look is on stage and reads.

- **The two checks, run on 2026-11-21 (596cd86; three photos).**
  Next: the user's questions on them, then Inventory ("we'll
  discuss Inventory later").
  - `verify --inventory --columns`: 5,211 PCs in 1.4 s; 18 of the
    23 columns are published. Kind of device: 4,326 'Virtual
    Desktop', 721 'Desktop', 164 'Laptop'. Filled on (nearly) every
    PC: manufacturer, model (28), processor (36), memory and disk
    size as text ('10GB', '63GB'), kind of disk ('Virtual', 'SSD',
    'NVMe', 'HDD-7200'), agent version (3), Microsoft stability
    index (1 to 10), location (9), last user (2,721), department
    (94); serial number on 5,194. As bands of words, not numbers:
    free space ('10 GB to 20 GB') and days since the last start-up
    ('Less than 24 hours', '7 - 14 days'). In part: last start-up
    on 4,354 (Jul 13 to Nov 21), BitLocker 'Off' 5,074 / 'On' 120
    (not split by kind of device yet), SCCM client on 900. Not
    published: antivirus, real-time protection, health status,
    health score, physical memory: the mock-up's antivirus line
    cannot be built.
  - `verify --actions`: 18 applications with measured actions in 7
    days, 469,390 actions, 12,153 rated Major; Outlook 374,002
    (Preview Mail 299,823). Major on 6 only: Outlook 9,369, Teams
    1,505, OneNote 762 of 963, Word 244, PowerPoint 153, Firefox
    120 of 263. 12 of the 18 have one activity. Two are of kind
    'Web' (73 and 40 actions). App usage lists 295 applications:
    277 have no measured action, "Web Browsing" the most used.
  - The user's questions on it, answered from Riverbed's help
    (help.aternity.com, the 12.3 glossary and the Edge extension
    page, read through search extracts: the pages did not open):
    a web application is its own entry only when an admin adds it
    as managed, and its activities are page loads (or custom
    ones) measured by the Aternity browser extension; other sites
    are the one entry "Web browsing". Edge is supported (Chromium
    88+, Agent 12.1+, the Aternity Extension for Microsoft Edge;
    without it only clicks and navigation, no page load). Status:
    one response time against the activity's recent expected one;
    automatic limits per activity and location, Minor at the
    slowest 10%, Major at the slowest 3%; or manual per activity
    (Gear > Managed Applications > Managed Activities > Edit).
    Riverbed's caveat: a baseline moves with chronic slowness.
    My reading, not confirmed: 762 of 963 and 0 of 16,291 both do
    not fit a 3% limit, so some activities have manual limits or
    none. Offered, not built: `verify --actions` with every
    application's activities and each status counted.

- **Stage copy of the load counter fix** (built on dev 2026-11-18,
  the user's photo of prod: "AI service GPT-5.4 824 calls, 0 rows;
  816 failed", the light green): `http.py`, `ai.py`, `status.py`,
  `usage.py`, `today.py`, `software.py`, `care.py`,
  `static/admin.html`, restart. Then the admin page's Load panel:
  GPT-5.4's failed count stops growing (today's earlier calls stay
  counted), no "rows" for it. f3b8459 (pushed 2026-11-19). Works
  (the user's screenshots, 2026-11-20): connection lights 1,123 ->
  1,132 calls in 10 minutes, failed 626 both times, no rows. Left:
  the prod copy, if that panel was stage.
- **Prod copy of the Software tab changes** (all on stage
  2026-11-20, the user: "All good on stage, note is gone"):
  7ae2a74 only PCs in Aternity's device list are counted (stage:
  5,198 PCs, "1,266 more left out"), 4b3e190 a read refused as too
  long is asked again with `$fetch_all_rows=yes` (the Windows
  updates are back), with `verify --inventory` (d3fd27b, a95d34c).
  Files since f3b8459: `aternity.py`, `software.py`,
  `software_chat.py`, `agent.py`, `web.py`, `verify.py`, `cli.py`,
  `config.py`, `explain.py`, `static/index.html`; restart.
- **"Application health by version": on stage, prod copy next**
  (the Software tab's versions panel; built on dev 2026-11-21; on
  stage the same day with 7689c53, cf8106b, b5d92aa: works, 15 red
  flags; the user: "Are we good with Software tab? Can we move to
  the Inventory?"). Its prod copy is the 10 files of the entry
  above (no other file changed). Open, written below: the two
  spellings; what was taken as chosen. The
  user, on a photo of stage: "very hard to understand charts", "I
  don't ... know where you get the 2 of 515 numbers", "Is there a
  better way to compare?", "Yes to D", then after three mock-ups
  "Let's go ahead and build the version comparison now"; the title
  is the user's third ask ("Which version gives more trouble?"
  "sounds harsh", "Application versions, side by side" not liked).
  - Was: each version against "its other versions" added together,
    a number shown only as a dashed marker and in a hover; one more
    crash among Word's other 515 PCs would have undone its flag;
    virtual desktops and physical PCs mixed.
  - Now: virtual desktops and physical PCs apart (a switch); before
    and after an update on the same PCs (as many days after as
    before, counted in PCs); each version against the version most
    PCs have; raised at 2 times, 5 PCs with it, and still so with
    2 more PCs on the other side; plain bars with the share as a
    percentage beside its count; every flag with "Why" and "Next:";
    "Explain" beside each comparison (our words, a question goes to
    the AI); "i" beside the title; applications with nothing raised
    folded. Rule and reasons: `docs/software-tab.md`, flag 1.
  - Taken as chosen, never confirmed word for word: percentages,
    the "Next:" lines' wording, "2 more" being 2, the title.
  - Files: `software.py`, `software_chat.py`, `explain.py`,
    `web.py`, `verify.py`, `static/index.html`; restart.
  - On stage, Nov 21 2026 (pushed 7689c53; a photo of `verify
    --versions`): the day-by-day read works. Trellix ENS: 2,075 rows
    in 0.7 s with its actions (605 rows for the 7 days added up:
    3.4 times, not the 10 estimated, for this application); Aternity
    gives the virtual / physical mark (45 virtual desktops, 462
    physical PCs, each once; the 605 are PC-and-version pairs, a PC
    on two versions in the week is two); 112 physical PCs went from
    one version to the next. Nothing raised, rightly: every PC
    Aternity lists for it had a crash, so the tab compares nothing
    for it (the rule from before).
  - Gaps in the check, fixed on dev the same day (`verify.py` only,
    406 tests; the tab is unchanged): it read only the application
    with the most crashes, on stage the one the tab cannot compare,
    and did not say so ("a crash on 112 before and on 112 after"
    read like a comparison; "Nothing is raised" had no reason); it
    measured one small read; "1 of its 3 versions are". Now: the 3
    applications with the most crashes, each with its rows and
    seconds and the three together; one that cannot be compared
    gets a sentence and no before / after counts.
  - The tab on stage, Nov 21 2026 (three photos; read 13:33): it
    works. 5,193 PCs, 16 red flags, 12 of them from this panel (11
    on virtual desktops, 1 on physical PCs); no read cut short; 76
    applications had crashes and hangs on 2 versions or more, the
    40 with the most were compared.
  - Bug seen there, fixed on dev (`static/index.html`): an update
    flag always said "Next: look at the N PCs before more PCs get"
    the version, also when most PCs already had it (OneNote on
    physical PCs: "the only version on 20 physical PCs or more").
    Now: "open the N PCs and investigate one. Most physical PCs
    already run this version." The same in its Explain.
  - Built on dev after those photos (the user: "build it for 2 and
    3"; 407 tests; checked in a browser with the photos' numbers):
    1. Slow actions get the third check: still 2 times with 5 more
       of the other side's actions rated Major (`STILL_ACTIONS`).
       Was: none; Teams on virtual desktops was raised on 1,316 of
       7,112 actions against 19 of 207, and one more Major action
       among the 207 undid it. Now it is not raised, and its bar
       says "not raised: 5 more actions rated Major on (the other
       version) would undo it". The version most PCs have now says
       so for crashes and hangs too ("2 more PCs on ..."): before,
       only the other versions' bars gave the reason.
    2. A version raised against another says its own update when
       the panel shows one: "Its update: On the 69 virtual desktops
       just updated to it from ..., it stopped responding on 36
       before the update and on 15 after. See Before and after the
       update below." Was: Outlook's flag and the table under it
       said two things, not joined. Explain says it too. No rule
       change.
    The number 5 is my suggestion, taken as chosen with "build it".
  - On stage, Nov 21 2026 (pushed cf8106b; two photos): works. 15
    red flags (10 on virtual desktops, 1 on physical PCs from this
    panel): Teams' "Slower" flag is gone, Outlook's flags have "Its
    update", OneNote's "Next:" says most already run the version.
  - The user, on those photos: "How did we go from 28 red flags to
    15 after this update? Did we break something or there was a
    really bad bug?" Not broken as far as the code and the photos
    say: the rule before was looser (the one the user questioned);
    16 to 15 is Teams. Which flags went, one by one, only the
    stage's saved read can say. Built on dev for it (410 tests):
    - `verify --versions`, "the red flags, by the rule before and
      the rule now": the tab's saved read judged by both rules; how
      many each raises; each one raised before and not now with
      why. No Aternity read.
    - Two gaps found in before and after while looking, both in the
      new code, both able to hide a real flag:
      1. A PC that ran both versions on the day of its update was
         left out whole (the comment said only that day). On stage
         69 of 2,475 virtual desktops on a version were in its
         before and after. Now the day is on neither side and the
         PC stays; the panel says how many did.
      2. Only the 2 updates with the most PCs (per kind of PC) were
         judged, as only they are shown: a third could never be
         raised. Now every update 20 PCs or more made is judged;
         the panel shows the 2 biggest and every raised one, and
         says how many more had nothing raised.
      The Compare dialog's "Before and after an update" keeps a PC
      the same way.
  - On stage with b5d92aa, Nov 21 2026 (two photos of `verify
    --versions`, "Software tab"): the saved read of 40 applications.
    The rule before raises 24 versions, the rule now 11; 7 of the
    24 by both; 3 raised now and not before (the version most PCs
    have, against another). The 17 that went, as the check printed
    them (my grouping of its lines):
    - 6 were not real: a version listed twice, the second spelling
      only on PCs with a crash, so "100% crashed". OneDrive
      26.153.809.4 (84 of 84) and 24.221.1103.3 (26 of 26, beside
      24.221.1103.0003 on 416 PCs); Windows Explorer 10.0.22621.7517
      (79 of 79) and 10.0.26100.9974 (44 of 44, beside
      "10.0.26100.9974 (WinBuild.160101.0800)" on 581 PCs); Settings
      10.0.22621.7219 (20 of 20); Windows PowerShell 10.0.26100.9818
      (26 of 26). The check files them under "new this week", "too
      few" and "the only version": the rule now does not raise them,
      but not because it knows they are one version.
    - 3 thin: more on the other side would undo it (Credential
      Manager UI Host 35 of 2,590 against 7 of 1,056; Windows Shell
      Experience Host 13 of 1,548 against 0 of 451; Teams' slow
      actions).
    - 5 were a version that is the only one, or nearly, on physical
      PCs, against versions on virtual desktops: OneDrive
      24.221.1103.0003 (stops responding, 21 of 416 against 17 of
      2,362), PowerPoint, Excel and OneNote 16.0.20527.20194, Edge
      WebView2 157.0.4341.46. Version and kind of PC go together
      there: the rule now cannot say which it is.
    - 3 real versions new this week, left to before and after, not
      raised there: Teams 26276.513.5241.9675 (crashes, 7 of 349),
      SearchHost 2609.12000.100.0 (crashes, 7 of 461), Windows
      Explorer "10.0.26100.9974 (WinBuild...)" (stops responding,
      200 of 581 against 510 of 3,482).
  - Gap found there, in the rule before and the rule now (built
    2026-11-22, see "Software tab: the headline box is gone" under
    Waiting on you): **one version under two spellings is counted
    as two versions.** Aternity writes a version one way on app usage rows
    and another on rows with a crash (my reading of the photo:
    "(WinBuild...)" left off, "0003" as "3"). So the crashes of such
    a version are not on its own row: its crash share is too low
    and the twin's is 100%. Plan, not to be built on its own (the
    user, 2026-11-21; see Agreed): two
    spellings are one version when their numbers are the same (the
    dotted number at the start, each part as a number, the text
    after it left out); counted and shown under the spelling on the
    most PCs; a click asks Aternity for every spelling; the same in
    Compare, before and after, the chat's look-ups; `verify
    --versions` says which spellings were put together.
  - Known limits, said in the docs: an application with one version
    in the last 7 days is not read per version, so an update every
    PC had over a week ago is not compared by itself; flags of
    these three kinds show "new" once (their key now names the kind
    of PC); "Before and after an update" under Compare versions
    still counts days.
  - Dev shows one raised flag (the sample browser stops responding
    more after its update).
- To watch on prod with Care step 2: how many slow start-ups a day
  (noisy or not), how many days of start-ups Aternity keeps (Show
  more says "N days with one, from ..."), which types carry the
  username.

## Agreed

- **What actions Aternity measures** (the user, 2026-11-21: "Add
  to do, what actions are measure in Aternity", then "I want to add
  verify for which applications in prod have measured actions",
  "build it for A and B"). Step 1 built on dev the same day:
  `verify --actions` (which applications have measured actions,
  each with its actions, those rated Major and its activities; the
  activities of the first ones; the applications in use with none).
  Waiting on its photo from prod (see Waiting on you). After it:
  where on the page, if anywhere. Not decided.
- **One version under two spellings: not on its own** (the user,
  2026-11-21: "I don't want to fix the spellings alone"). Built
  2026-11-22 with two other Software fixes, on the user's "fix
  it" (4 of the 9 version flags on stage were it); see "Waiting
  on you".

## Ideas

- **Device inventory: first cut works on stage (6cc9fac, photo of
  2026-11-21); next the user's pick of the usage charts**
  (the user, 2026-11-21: "build it"; 420 tests; clicked through in
  a browser on dev). The tab INVENTORY, sixth: tiles, Needs a look
  (nine lines), Last heard from with the agent's status, Retired
  PCs and not used, Find a PC with a card. Files for the copy:
  `inventory.py` (new), `web.py`, `software.py`, `explain.py`,
  `usage.py`, `config.py`, `static/index.html`; restart.
  - Stage, Nov 21, 18:32: 5,212 PCs (4,326 Virtual Desktop, 721
    Desktop, 165 Laptop), 4,456 heard from in 7 days, 670 with no
    date, 1,269 retired. Needs a look, all nine judged: low
    stability index 1,578 of 5,194; not used in 8 days 1,420 of
    5,126 (1,342 of them virtual desktops); an older agent 885 of
    5,212; BitLocker off on a physical PC 755 of 886; not
    restarted in over 14 days 303 of 4,354; not heard from in 8
    days 86 of 4,542; a spinning disk 32 of 875; little free
    space 24 of 5,194; agent stopped 7. The three open points are
    settled: the low free-space bands are 'Up to 0.5 GB', '3 GB
    to 4 GB', '5 GB to 10 GB' (the rule takes them); BitLocker is
    off on most physical PCs; the kinds are the three above.
  - **Asked next (the user, 2026-11-21, on that photo): "We need
    analytics of desktops usage charts on first panel, provide
    proposals with mockups".** Three proposals mocked, nothing
    built (a design canvas, link in the session; numbers marked
    "example" are made up):
    - A. Used each day: PCs an application ran on, per day, 14
      days, each column against the PCs of its kind in the list;
      a switch All / Virtual desktops / Desktops / Laptops; a
      click lists the day's PCs. No new read: the tab's hourly
      read of daily app usage (`APPLICATIONS_DAILY`, now PC only
      over 8 days) keeps the day too; 14 days read day by day as
      the Software tab does.
    - B. In use at once: PCs an application ran on in each hour
      of a day against the same hours on the working days before,
      and the busiest hour of each day against the list. Needs
      hourly app usage (`APPLICATIONS_HOURLY`: in Riverbed's
      column list, never read on the live Aternity): a verify
      line first (published? rows and seconds for a day?).
    - C. How often: each PC by the number of the last 8 days it
      was used on (none, 1, 2 to 3, 4 to 5, 6 to 8), virtual
      desktops and physical PCs side by side; a click lists the
      PCs. Same read as A.
    - Not offered: hours of use. Aternity's usage time has no
      documented unit (never printed).
    - My recommendation: A and C first (one read, already made),
      B after its verify line.
  - **Second round (the user, 2026-11-22, on A-C: "Looking for
    daily/monthly usage, mockups with bar and line charts
    mixed").** Three more mocked on the same canvas (its first
    page; A-C are its second), nothing built, every chart number
    made up:
    - D. Desktops as bars, people as a line: each day for 30 days
      and each month for 12 months, side by side.
    - E. Desktops in use as bars, the PCs in Aternity's list as a
      line (what is used against what there is); the monthly
      chart also has the busiest day of the month as a line.
    - F. One chart of 90 days: each day a bar, the desktops used
      at least once in each month as a stepped line; a typical
      working day (the middle value) written per month.
    - All three: the switch All / Virtual desktops / Desktops /
      Laptops, one axis per chart (no second scale), "in use" =
      an application ran (app usage), each PC once.
    - What they need, not known yet: 1. how far back Aternity
      keeps daily app usage (a verify line; the Software tab
      reads 14 days of it): months before the build exist only
      that far, later months are counted by the tab as they pass;
      2. "used at least once in the month" counts each PC once a
      month: from Aternity when it keeps the month, else the tab
      keeps the month's PC names on disk (as SecureView does; the
      Inventory tab keeps none today: the user's decision);
      3. people (D): `USERNAME` in daily app usage is in
      Riverbed's column list, never read on the live Aternity (a
      verify line); 4. E's line: the count in the list is saved
      per day from the build on.
    - My recommendation: E (it answers "how much of what we have
      is used", needs no usernames), with D's people line added
      once its verify line passes.
    - **"Add percentage of usage" (the user, 2026-11-22), added
      to D, E and F on the canvas.** Percentage = desktops in use
      (a day) or used at least once (a month) out of the desktops
      of that kind in Aternity's list; a month against that
      month's list. Shown three ways: marks at the right of each
      daily chart on the same scale as the counts (in E the list
      line is 100%; no second scale), a percentage above every
      monthly bar, and four headline figures under the charts in
      place of the sentences (typical working day, busiest day,
      typical weekend day, the month so far; "typical" = the
      middle value). Needs the count in the list kept per day
      (from the build on); the kind switch gives each kind its
      own list.
    - **Chosen (the user, 2026-11-22): "Your recommendations".**
      So: E with the percentages on the first panel, D's people
      line after its verify line. My recommendations on the open
      points, taken with it: only PCs in Aternity's device list
      are counted (a percentage never passes 100%; how many were
      left out is said); 30 days by day, 12 months by month,
      filled as far back as Aternity keeps daily app usage and
      growing from there; counts saved per day, no PC names; a
      month's "used at least once" from Aternity when it keeps
      the whole month, else the current month's PC names in one
      file (PCs only, no users), removed when the month is
      counted; the chart opens on Virtual desktops. Build order:
      1. `verify --usage` (how far back daily app usage goes,
      rows and seconds, PCs in use per day by kind with the
      percentage for the last 14 days, whether the username is
      filled, PCs in app usage that are not in the list); 2. the
      daily chart with percentages; 3. the monthly chart; 4. the
      people line.
    - **"Build it" (the user, 2026-11-22): step 1 built on dev,
      `verify --usage`** (423 tests; run against the dev sample).
      Next: its run on stage (copy `verify.py`, `cli.py`; no
      restart needed for a verify), then step 2 from what it
      prints. It answers the four unknowns above in one run: the
      oldest day of daily app usage and how a day's rows are
      timed, the PCs in use per day and kind with their share of
      the list for 14 days (the daily chart's own numbers), the
      username filled or not with people a day, and this month and
      the three before read one read each (the monthly chart's
      numbers), saying whether a month's count can come from
      Aternity itself.
    - **Stage run of `verify --usage` (dfed07a, photos of
      2026-11-21, a Saturday evening): the check works; what it
      says.**
      - Kept: back to Aug 21, 92 days, so 2 whole months before
        this one (September, October). 12 months cannot be filled
        from Aternity: the tab has to keep each month itself.
      - A day's rows carry one time: local midnight, sent with its
        UTC offset. The 12-hour rule puts them on the right day
        (the weekend lows fall on Saturday and Sunday, and on
        Wednesday Nov 11, a public holiday).
      - **Today has no rows** (0 for every kind at the evening):
        Aternity adds a day after it is over. A chart of it ends
        with yesterday. Gap in the check: it printed
        "0 0% so far", which reads as nobody in use.
      - The list: 5,211 PCs (4,326 Virtual Desktop, 720 Desktop,
        165 Laptop). 197 PCs with app usage in the 14 days are not
        in it and are left out.
      - Virtual desktops: a typical working day 2,270 of 4,326
        (52%); the busiest day Thu Nov 19 with 2,370 (55%); a
        typical weekend day 445 (10%); on at least one of the 14
        days 2,992 (69%).
      - Desktops: 503 of 720 (70%); busiest 527 (73%); weekend 248
        (34%); 687 (95%) in the 14 days. Laptops: 72 of 165 (44%);
        busiest 113 (68%); weekend 37 (22%); 155 (94%).
      - People: the username is filled on all 33,788 rows; 2,969
        people on a typical working day, 4,282 different people in
        the 14 days. A people line (step 4) is possible.
      - Months, used at least once (virtual desktops | desktops |
        laptops): November so far 3,426 (79%) | 696 (97%) | 157
        (95%); October 3,047 (70%) | 686 (95%) | 137 (83%);
        September 2,560 (59%) | 661 (92%) | 139 (84%); August from
        Aug 21 only. One read a month, a row per PC: a month's
        count comes from Aternity itself, no PC name kept. Not
        known from it: how many of an earlier month's PCs have left
        the list since (only PCs in today's list are counted, so
        September may read lower than it was).
      - Load: 14 days 33,788 rows in 5.2 s (with the username;
        the slowest day 0.5 s); 4 months 17,677 rows in 2.0 s.
    - **Steps 2 and 3 built on dev (2026-11-22): the daily and the
      monthly chart, design E, on the Inventory tab's first panel
      under the tiles** ("Desktop usage"; 431 tests; looked at in
      a browser with the dev sample and with stage-sized made-up
      numbers, 3 and 12 months, light, dark and phone width).
      - Bars = PCs in use, a line = the PCs in Aternity's list
        (100%). Left: the last 30 finished days, percent marks at
        the right. Right: each month, "used at least once", its
        percentage over the bar, the busiest day of the month as a
        second line, this month so far lighter. A switch: All and
        each kind of device as Aternity names it, opening on the
        virtual one. Four figures: typical working day, busiest
        day, typical weekend day, this month so far (typical = the
        middle value of the days shown). A bar's counts on hover.
      - What the stage run changed against the plan: 1. no bar for
        today (Aternity has no rows): the last bar is yesterday,
        and a day is read again each hour until it is 2 days old,
        in case rows come late (how late they come is not known);
        a day with no rows is a gap, never 0. 2. The monthly chart
        starts with the whole months Aternity keeps (September and
        October on stage) and this month so far; August is not
        shown (kept from Aug 21 only); it grows to 12 as the tab
        keeps each month. 3. A month is counted through yesterday,
        as the days are.
      - Kept in `data/inventory/usage.json`: per day and per month,
        the PCs in use and the PCs in the list per kind of device,
        and how many PCs with usage were not in the list. No PC or
        user name. Days 400, months 24.
      - Load: the first read after the copy takes every day
        Aternity keeps, about 90 small reads once (the PC and the
        row's time, no username: fewer rows than the check's), and
        3 month reads; then 3 day reads an hour and a month read
        when a day moved or once a day.
      - **"Moving files in and out of stage/prod is very very
        difficult, make sure codes are correct before asking me to
        move files to stage/prod. MAKE NOTE" (the user, 2026-11-22,
        after the copy step; `verify.py` was in it a second time
        because of the check's gap).** Noted for every copy step.
        Checked again before the copy, two things changed: 1. a
        day's read leaned on Aternity cutting a day exactly as
        asked, which the stage run did not show: now each row is
        counted by its own time, as the check did on stage (a month
        is read as the check read it); 2. "this month so far,
        through …" named yesterday even when its rows were not in
        yet: now the month's last day with rows. Also done on dev:
        a first read from an empty folder (the same counts), a real
        click on the switch and on Explain in a browser, the
        browser's console (no error), restarts (only the days not
        settled are read again).
      - Said on the page: days and months from before the tab's
        first read are set against the list of that day (a PC that
        has left the list since is not counted in them); how many
        PCs with usage are not in the list, on each bar.
      - The check, fixed with it: a day with no rows is "no rows
        from Aternity" ("yet: today is not over" for today) and
        counts for no typical day; the month lines are under each
        other. Its day rule now lives in `inventory.py`
        (`daily_span`, `daily_day`): one rule for the check and
        the tab.
      - Not built: step 4, the people line (possible: the username
        is filled); a click on a bar that lists the day's PCs (no
        PC name is kept: it would be a read of Aternity at the
        click); the dev sample still gives rows for today, which
        the charts do not use.
- **BitLocker is off the Inventory tab (the user, 2026-11-22:
  "remove bitlocker"). Built on dev, 432 tests; goes to stage in
  the same copy as the usage charts and the switch.**
  - Why it had nothing to say: on stage BitLocker is 'Off' on
    5,074 PCs and 'On' on 120, and the line "BitLocker off on a
    physical PC" held 755 of 886. It was kept on 2026-11-21
    ("2. keep it") to see the count on stage first.
  - Gone: the "Needs a look" line (8 lines now), "BitLocker" on a
    PC's card, its meaning in Explain, and the column itself: the
    tab no longer asks Aternity for it, and
    `verify --inventory --columns` no longer lists it (22 columns
    asked about, it was 23). The tile "things to look at" counts
    the 8 lines.
  - One more file to move than the six of the charts and the
    switch: `config.py` (the column lives there, with the others).
  - Dev: the sample's device list no longer sends a made-up
    BitLocker status.
- **A switch between the current look and a dark one (the user,
  2026-11-22: "Add dark and current theme switch on page", then
  "Mockups of dark themes", then "Build A and admin too"). Built on
  dev, 432 tests.**
  - Built: "Parchment | Dark" at the right of the lights on the
    main page and at the right of the admin page's bar (also on its
    log-in screen). A click changes every tab at once; the browser
    keeps the choice (one name for both pages, so they agree when
    opened under one address); until someone picks, the PC's own
    setting decides, as before. Dark is A, "Night archive".
  - What had to change for dark to read: text on a filled colour
    (buttons, the Critical / Major tags, the tab badges, the
    assessment's band, a light that is down) was white on every
    look; it is near-black on dark, where those fills are lighter.
    Three faint tints were black, invisible on dark: they are a mix
    of the text colour now. The PC / network / server colours of an
    investigation got lighter values for dark.
  - Different from the plan: the switch is not in the slim bar that
    stays when the page is scrolled (with four lights there is no
    room); it is at the top of the page.
  - Gap found on the way, fixed: between 641 and about 830 px wide
    (a tablet, a narrow window) the six tabs were wider than the
    page, so the whole page scrolled sideways. Now the tabs slide
    sideways from 860 px down, as on a phone.
  - Checked in a real browser on dev, scripted: first visit, a click
    on Dark, a reload, the PC set to dark with nothing picked,
    Parchment clicked on a dark PC, a pick in another window, the
    admin page opening in the main page's look and the other way
    round; every tab in dark (Today, Care list, an investigation,
    a start-ups report, Software, Inventory), the Explain and
    Connections dialogs, the admin page's log-in, Statistics,
    SecureView and Actions; the top bar with four lights and a long
    title at 1280, 1000, 760 and 420 px; no script error.
  - Not done: the choice cannot follow a person to another PC (no
    login on the main page).
  - Plan given: a switch at the right of the top bar, beside the
    lights, "Parchment | Dark"; a click changes every tab at once;
    the browser remembers it (no login on the main page, so it
    cannot follow a person); until someone clicks, the page follows
    the PC's own setting as it does today. The page already has a
    dark palette, shown only when the PC is set to dark. Only
    `index.html` changes; the admin page is left out unless asked
    ("admin too": one more file to move). Before any copy step,
    every tab is looked at in dark in a browser.
  - Three dark looks mocked on a design canvas (link in the
    session), the same screen in each: the top bar with the switch,
    the Inventory tab's first panel with the usage charts, "Needs a
    look", a Care list tile in alert and one OK, a button, a link, a
    box to type in. Looked at in a browser first.
    - A. Night archive: today's dark palette (warm near-black, the
      dark green top bar), with the green, the alert red and the
      warning amber lightened so small text reads on it.
    - B. Archive green: the top bar's green carried through the
      whole page; the bar is the lighter band; mint bars and links.
    - C. Ink: a cool blue-black, close to Windows' own dark look;
      the least of the brand.
    - My recommendation: A (the brand as it is, the least new to
      check: its palette is already on the page).
  - The stage copy of the usage charts can wait for it: the switch
    lands in `index.html`, which is already in that copy.
  - Different from what was said before the build, each for a
    reason: the lists and the card answer from the hourly read held
    in memory, not from a read at the click (no load on Aternity;
    only "Its software" reads then); "not used in 8 days" needs one
    more light read of daily app usage, as the Today reader's names
    hold only 24 hours after a restart; no name from AD on the
    card (the main page has no login and AD look-ups there stay
    with the Care list), it shows Aternity's own last user and
    department; "no restart in over 14 days" is counted from the
    start-up date, not Aternity's bands, so its count can differ a
    little from the 301 of the bands.
  - To see on stage: which of Aternity's low bands of free space
    the line "Little free space" names (they were not seen by
    name; the rule takes any band that ends at 10 GB or under);
    the count on "BitLocker off on a physical PC"; the tile's
    kinds of device.
  - Not built: step 2 (the hardware charts with the virtual /
    physical switch), step 3 (Ask anything). Its drawing was made
    on 2026-11-22 (docs/diagrams/07).
  - The choices (the user, 2026-11-21, on the four asked: "1. hourly, 2.
  keep it, 3. you recommendations, 4. both"). So: 1. read every
  hour; 2. the BitLocker line stays ("BitLocker off on a physical
  PC"); 3. limits as suggested: not heard from in 8 days, not used
  in 8 days, no restart in over 14 days, stability index under 5,
  an agent older than the one most PCs have; 4. a count with its
  share beside it. Asked for: (the
  user, 2026-11-21: "Provide proposal for Inventory with data
  collected", after step 1's photo from prod; a second mock-up was
  shown, on the same link as the first). Nothing built. Earlier the
  same day: "Can we move to the Inventory?", "For Inventory, first
  step is verify?", "build it for A and B" (`verify --inventory`'s
  5th answer). First asked: "Need to integrate
  device inventory section, use the data from verify --inventory to
  start. I'll let you decide: it's on a separate tab or same as
  Software, helpful data to add on top of verify --inventory.
  Provide proposals and mockups").
  - Proposal 2, what the data changed: 4,326 of 5,211 PCs are
    virtual desktops, 885 physical, so every panel gets the
    Software tab's switch (virtual desktops / physical PCs). Gone:
    antivirus, real-time protection, health status and score (not
    published); "8 GB of memory or less" as a warning ('7GB' on
    873 PCs looks chosen). New lines: an older agent than most
    (884), no restart in over 14 days (301, Aternity's two longest
    bands), still on a spinning hard disk (25). Free space and
    days since the last start-up are shown in Aternity's own
    bands. The read is 1.4 s: every hour, not once a day.
  - Panels: tiles; Needs a look (9 lines: older agent, BitLocker
    off on a physical PC, not used in 8 days, no restart in over
    14 days, not heard from in 8 days, agent stopped, low
    stability index, little free space, spinning hard disk); Last
    heard from with the agent's status; Retired and not used; What
    the PCs are (kind, model, memory, kind of disk); Restarts,
    disk space and agents; Find a PC (a card of every filled
    column, the person's name from AD, Investigate / software /
    start-ups).
  - The four choices asked of the user: 1. read every hour (my
    recommendation) or once a day; 2. the BitLocker line: only 120
    PCs report 'On', so keep it only if BitLocker is how the disks
    are encrypted; 3. the limits (8 days not heard, 8 days not
    used, over 14 days no restart or a number of days from the
    date, stability index under 5, an agent older than most); 4. a
    share beside each count or counts only.
  - Build order: 1. first cut (the read, tiles, Needs a look, Last
    heard from, Retired and not used, Find a PC); 2. the two
    hardware panels with the switch; 3. Ask anything.
  - The first proposal, before the data:
  - My decision: its own tab, INVENTORY, last. Software is already
    the longest tab and answers "what is installed"; this answers
    "what PCs do we have and which need a look". No new load: the
    Software reader's one daily read of the device list asks for
    more columns; lists of PCs are read on a click.
  - Panels mocked: tiles (PCs in the list, heard from in 7 days,
    retired, things to look at); "Needs a look" (one line per
    kind, most PCs first); Last heard from, with the agent's
    status; Retired PCs, and "in the list but not used" (from the
    Today reader's names); What the PCs are (model, memory, system
    disk; Windows stays on Software); Restarts and protection
    (days since the last restart, BitLocker, antivirus real-time
    protection, agent version); Find a PC (a card for one PC).
  - The other option mocked: one "Devices" panel on the Software
    tab (last heard, three lines).
  - Order to build: 1. `verify --inventory` answer 5: which of
    these `DEVICE_INVENTORY` columns the live Aternity fills and
    how (all are in Riverbed's metadata fixture, 97 columns; the
    live one may fill fewer); 2. the tab's first cut (tiles, last
    heard, retired, Needs a look, Find a PC); 3. hardware and
    protection for the columns that are filled; 4. Ask anything.
  - Limits suggested, not agreed: not heard from 8 days, not used
    8 days, no restart over 30 days, 8 GB of memory or less, under
    10% free disk, an agent older than the one most PCs have.
- **Assessment**: a weekly roll-up; sending it by email (the
  app sends nothing today); weekends (it runs every day now).
  Does Aternity mark an event resolved (`HEALTH_EVENT_STATUS`,
  never read on prod)? A verify line offered.
- **Active users and PCs**: PCs that only report to Aternity
  (`TIME_LAST_REPORTING`, needs a verify line first); a verify
  line listing PCs marked virtual / physical / not marked for
  the desktop team.
- **Data meanings register** (`docs/data-meanings.md`, M1-M22,
  paused 2026-11-09): `# meaning Mx` tags in src, a test keeping
  them in step, "Meaning checks" in verify.
- **Ask anything**: hangs per app and software changes as
  look-ups (measure their row counts with verify first).
- **Software tab**, to confirm with data: version spread,
  one-user installs, programs on few PCs.

## Parked

- **Kubernetes**: prod runs on Docker for now (user,
  2026-11-15); `k8s/uxcorrelate.yaml` is kept in step with
  `compose.yaml`. The way in switches between the ingress and
  a load balancer with the pod's own TLS (2026-11-16):
  `k8s/ingress.yaml`, `k8s/loadbalancer.yaml`, one apply + one
  delete. Not used until the users' real addresses reach the
  pod (the user, 2026-11-16: "I'm not using k8s til I figure
  out passing real client IPs"); the admin page is the check
  (each visitor named, or "every visit from one address").
  Also open: cpu / memory numbers, only one user could connect.

## Done (latest)

- dbcf407: the week under "Yesterday" (the user's pick A of three
  mocked: PCs a day with a Critical event, a Major event and Major
  slow actions, Monday to Sunday, this week and last; not chosen:
  B a line per kind of event, C the last full week only) and no
  red dots on Care tiles ("makes no sense"). 16eeed7: Care list
  step 2, slow start-ups as an issue (A and B) and Active
  Directory on the tab (add by name, AD's name, "disabled in
  AD"; with it the AD lookup is done: verify --ad, the admin
  page's names, the Care list). Both on stage and prod 2026-11-17 (the user: "All good
  both stage and prod").
- f905606, a807a50, SecureView history, `verify --ai`,
  `verify --clients --except WT`: on stage / prod 2026-11-17 (the
  user: "1-5 are all good, stage/prod are same version now").

- Failing disk needs Aternity's category Hardware, not only its
  "(Disk)" / "(Drive)" label (built on dev 2026-11-16, from
  `verify --overview` on stage: Low Disk Space 8 PCs and
  Corrupted FS 2 PCs under "(Drive)" are System; they had led
  investigations and lit Care tiles all day). a807a50 (pushed);
  on stage 2026-11-16 (verify --overview: Low Disk Space and
  Corrupted FS "not a failing disk", HD Failure and S.M.A.R.T.
  "a failing disk"); prod copy next.

- 912aeab: AD names on the admin page; on prod 2026-11-16 (the
  user: the display name shows under "People using the app").
  With it the admin page fix of 5c9a735.

- c4435b0: image 4 and `verify --ad`; on prod 2026-11-16,
  all passed with and without the account.
- 13a023b: Care list judges each Major action by its own time
  of day; every tile lists its Major actions today.
  On stage (2026-11-16: its three files match there).
- 1c291e0: Care tile bars are Major actions per day from
  Aternity.
- 3d0f674: gap review: a read that failed or was cut short is
  said, never "nothing found".
- fa41a96: Care list "Arrange tiles".
