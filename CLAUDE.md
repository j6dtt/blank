# CLAUDE.md

UX Correlator: a Python tool that correlates Riverbed Aternity end-user
experience with Riverbed NetIM infrastructure metrics (both via REST) and
attributes a user's slowdown to the client device, network, or application
backend. See README.md for user-facing docs.

## Commands

```bash
pip install -e '.[dev]'                 # install with pytest
pytest                                  # run tests (no network needed)
uxcorrelate -c config.yaml verify       # check config names against live systems
uxcorrelate -c config.yaml <command>    # -c goes BEFORE the sub-command
docker compose up -d --build            # web UI on :8080 (needs config.yaml)
scripts/make-bundle.sh                  # tests, then bundle for the live host
scripts/snapshot.sh DIR                 # every test investigation's report + page data
docker compose -f dev/compose.yaml up -d  # dev only: made-up Aternity + NetIM + AD (then restart the app)
```

Before and after any change to what an investigation says: run
`scripts/snapshot.sh` into two folders and `diff -r` them; every changed
line must be one you meant (`tests/conftest.py` does the saving).
`tests/test_real_cases.py` rebuilds cases seen on prod (made-up names):
render one on dev and look at the whole page before prod.

Without installing: `PYTHONPATH=src python3 -m uxcorrelate.cli ...`.
The dev host has no Riverbed and no pytest: tests run in the runtime
image (`make-bundle.sh` shows how). The dev app reads a made-up Riverbed,
`dev/sample_riverbed.py` (README.md, "Sample data for the dev app"): a
server outside the product, never bundled, answering the app's own clients
with the real `$metadata` and NetIM layouts, so `verify` passes there and
every tab has data. Its rows are worked out from their time (the same at
every read), its story is set against today in its `ZONE`; at start it
saves the past days' counts in `data/days`. Every column it sends must be
in the real `$metadata` (`tests/test_sample_riverbed.py`). It is not a
demo mode: nothing in `src/` knows about it.

## Layout

- `src/uxcorrelate/config.py`: dataclass settings loaded from YAML and
  overridden by env vars (`ATERNITY_*`, `NETIM_*`). Also holds the default
  Aternity column map and NetIM metric list.
- `aternity.py`: Aternity OData client. It filters on the timestamp column and
  follows `@odata.nextLink`.
- `netim.py`: NetIM client (devices, interfaces, metric data, alerts).
  `parse_metric_response` accepts several response shapes.
- `correlate.py`: the core. Slow = Aternity's own `SLA_STATUS` in
  `thresholds.slow_status` (else over `response_time_s`). It maps endpoints
  and apps to NetIM objects (roles), collects problems (PC values over their
  limits incl. a start-up of the PC (start to end, kept only when a slow
  action is in it or just after; why from `logons.py`), a nearly full disk (`disk_space` source, % or free
  space / inventory size), health events, app crashes/hangs, NetIM
  values and alerts) and counts the slow actions each one was within
  `bucket_minutes` of. An app's crash/hang/event counts only for that app.
  A failing disk (`models.failing_disk`: Aternity's "(Disk)"/"(Drive)"
  label on a health event's failing part, `DISK_PARTS`, and its category
  Hardware, `DISK_CATEGORIES`: under "(Drive)" Aternity also files Low
  Disk Space and Corrupted FS, category System, which the label alone
  read as a failing disk; verify --overview, stage, Nov 16 2026) is the
  PC's state, not a moment: its
  records are one issue per PC (`DISK_WHAT`), counted from its first
  record (window, or that PC's 14 days before: `_disk_state`) for the
  PC's slow actions that Aternity's split does not put mostly on the
  network or server (`_disk_counts`); `_disk_context` says what it
  covers. It leads (class A) and its step is first (live, Nov 2026).
  No scores: the answer is plain counts. The data sources are the
  `AternitySource`/`NetimSource` protocols.
- `logons.py`: slow start-ups (`WINDOWS_MACHINE_BOOTS`,
  `MACHINE_BOOT_COMPONENTS`). Rows of one device within an hour, no type
  twice, are one start-up (one row, a column per Aternity type). Per type:
  typical and 90th percentile (`P90_MIN` 10 measured, else None and the
  slowest is shown: from 2 of 10 s and 20 s it said "1 in 10 over 27 s");
  per row: the device's own 30-day usual and
  what took the time (a part belongs to the latest start-up row of its PC
  at or before it, within an hour; part types never equal start-up types
  on a live Aternity, so types are not compared). Why: the part with the most Aternity degradation,
  software changes before (`cause.changes_before`; "N/A" = no version,
  change log read by `cause._updates`: an "Uninstalled" + "Installed" row
  of one software within `SOFTWARE_UPDATE_GAP_MIN` = one update (or
  reinstall); further apart at the same version = "removed, then put back",
  told last and never a change to check; the other rows of a software that
  comes and goes like that are told last too ("comes and goes in
  Aternity's log"); the log is read past the problem time to find a row's
  other half; "Updated" is one row), and main
  causes =
  that part longer than normal on every PC (`boot_component_everywhere`).
  No "slow" threshold, no assumed type names. Unit
  from `boot_unit` (inferred as seconds from live values, not documented).
  Used by "What the user felt", the "Start-ups" tab
  (`/api/logon-jobs`) and the `logons` CLI command.
- `normal.py`: each slow app + activity next to its own normal (user's
  last 30 days at most, the days found are said: prod keeps about 8; other users at the same time), days where Aternity marked most
  runs slow and since when, the change log between the last normal day and
  the first slow one, and CPU/memory/disk queue at those actions vs usual.
  With no problem at the slow actions, a change before "since" makes the
  headline (class C in `docs/headline-rules.md`). `logons.py` does the same for
  start-ups: since when every start-up of a type took longer than 9 in 10
  of those before it (earliest such point; 5 earlier start-ups needed), and
  the changes just before. Virtual desktops (Horizon) are pooled: for a
  user, start-ups on their virtual desktops (`virtual_devices`, from
  `VIRTUALIZATION` / `VIRTUAL_VALUES`) are compared with each other, not
  with one desktop's history of many users; when the run before was on
  another desktop, the answer says what changed is read on this one only.
  "What the user felt" marks a virtual desktop and the device the user
  sat at (`CLIENT_DEVICE_NAME`); "checked" says the network between them
  is not checked (Aternity gives no delay for it). A start-up with no slow part that is normal for
  its PC is "weak": not a problem anywhere (`correlate.investigate` drops
  it from the problems and the timeline), only a sentence in the reason
  (`calm`: it was near the slow action, nothing in it was long). The answer's "checked" list
  says what was ruled out, with counts.
- `impact.py`: "Other users of these apps" — other users of the same apps in
  the user's slow intervals, grouped by the NetIM gateway (or /24) they sit
  behind and compared with the same hours on the days of the week before,
  working days with working days (`DAYS_BACK`; prod keeps activities about
  8 days) ("unusual" = more slow than any of those days; common point = an
  unusual group holding more than half the slow users, a gateway or /24
  only, never "no IP address"). It is the verdict (class B) only when
  Aternity's split does not put most of the user's slow time on their PC
  (`answer.on_pc`); counts, never "many". Virtual desktop
  users are one group, never a network point (their address is the
  desktop's). This NetIM grouping is what Aternity can't do. An issue's own
  "who else" comes from `cause.py`.
- `cause.py`: the main point of an investigation: what we know of each
  issue. For every PC
  problem (those at slow actions first, then what the user felt, then the
  rest: a crash is an issue by itself; up to `MAX_EXPLAIN`, each Aternity
  question asked once): where the app failed (component, module,
  versions, error, from `DEVICE_HEALTH_RAW`), other PCs failing the same
  way (out of the PCs using the app, `app_usage_everywhere`, or
  `known_pcs`; since when, `_since`, for up to `MAX_HISTORY`), software
  changes (`INSTALLED_SOFTWARE_CHANGE_LOG`, read once per PC): a change is a
  "possible cause" only with the same name (`same_name`: every word of one
  in the other; only a file ending is cut, so "Microsoft.Notes" is never
  "Microsoft Edge") or version as the failing part,
  before it, and with no other PC failing the same way on another version
  (then it is "not new with this change"). Logged after the failure is said;
  on a virtual desktop that does not clear it (the log runs late there,
  usually by under a day; on physical PCs its time holds); `NO_PROGRAM` and
  `WINDOWS_MODULES` (config) are never blamed; busiest processes for CPU/memory/disk and low memory events
  (`top_processes` = `APPLICATION_RESOURCES_HOURLY` on the live tenant: a row
  per process and hour at any minute, so rows within `PROCESS_ROW_SPAN_MIN`
  are read; CPU %, memory in bytes shown as MB, no disk IO). "Same failure" = same category+name, same
  component and sub-component (version shown, not matched); see
  `docs/who-else-is-affected.md`. Every health event kind gets a why (an
  event with no app, e.g. a background process, low memory, a blue screen,
  counts at any slow action; who else is found by its process). Same
  failure also needs the same error code when both have one; the module
  only when Aternity knows it. Each finding is a sentence about a record;
  a source with nothing says so. A hang and its own health event share one
  cause.
- `after_start.py`: a slow action up to `AFTER_START_MIN` (10) after a
  start-up of its PC, against the same app activity on all PCs of its
  kind (virtual / physical) in the `FLEET_HOURS` (24) before: runs right
  after a start-up and later, share slow and median (`activity_history`
  and `boots` for everyone). "Slower" = `RATIO` times the share or the
  median, `MIN_RUNS` on each side. With nothing at the slow actions and
  no change before, the slowest action's comparison is the headline
  ("… right after a start-up: 67% of those on virtual desktops are
  slow", level possible); the others go to "checked". Live: launches
  right after logon are slow, most on virtual desktops (verify, Nov 2026).
- A PC reading (CPU, memory, disk queue, round trip) is from one action's
  own row: it counts only for that action (`correlate._matches`), never
  for a slow action minutes away; a PC's problems never count for
  another PC's slow actions. With slow actions, problems not counted
  for one are `answer["elsewhere"]` ("Other things on these PCs",
  below the answer, not in the rollup).
  Two actions can share a timestamp (live): a reading counts for a slow
  action only when it is that action's own value. A crash/hang within
  minutes of another app's slow action is said, not counted
  (`_other_app_near`). A background crash or PC limit there keeps its
  step with its item ("Next step: …"), not in "What to do". 5+
  unpaired "Uninstalled" rows in one minute (`cause.batch_removals`)
  are one line everywhere and never a change to check. "What the user
  felt" lists each PC's start-up (`felt.logons`).
- PC values over a limit (`correlate._limit_context`): Aternity reads CPU,
  memory and disk queue with each of the user's actions, so the highest
  reading is told against the nearest slow action (and that action's own
  value) and a start-up just before it. Per-program figures are hourly:
  said so; a program is named as the one to look at only when its hourly
  CPU is half the PC's reading or more, or for a low-memory event. A PC
  limit gives way to the start-up comparison as the headline. A hang's
  value (no documented unit) is not in the text. An "Uninstalled" row
  with no install, of software that failed after it, is not a cause;
  5+ such rows in one minute are one line in "What changed".
- `answer.py`: the plain-language answer at the top of the page and the
  CLI report (title, what the user felt, other users, what went wrong, what
  changed, what to do), plus `problem_text`, the sentence for each problem. The
  headline only counts and points ("see Other users", "Compared with normal");
  `rollup` is the top of "What went wrong": one row per issue across the
  user's PCs, a `verdict` by fixed rules (`cause._trend`: last 7 days against
  the 6 before, working days, `TREND_RATIO`, `TREND_MIN_PCS`; hangs have no
  trend, app usage is kept about 8 days) and other PCs per day as bars;
  each detail is said once, in its own section. The headline is two sentences,
  what the user felt then one verdict, by the classes A-H in
  `docs/headline-rules.md` (`_pick_headline`; order provisional; "became
  slow" only when slow on most runs since, else occasional, never the
  headline); each
  other slow app + activity is an "Also slow" line in the reason; the
  most serious thing the user hit leads, wherever in the window
  (`_lead`, `_lead_rank`: a failing disk, a crash, a hang recorded as an
  event, then slow actions; the user's rule, Nov 2026): it is the title
  with what is known of why (`_lead_verdict`), first under What went
  wrong and What to do, and the slow actions' own headline follows in
  the reason ("Also: …"); PCs
  are named ("Other things on VD-41"), never "these PCs". The question is
  what went wrong for the user, not only what was slow: with no slow action
  the title counts hangs, app crashes and system events (`level` "issues",
  background crashes in the reason); a crash Aternity names no application
  for (a System Crash: the PC itself) is told by its own name, first after
  a failing disk, and still ranks as a crash in `_lead_rank` (`_issue_counts`). Each problem has an `issue` kind
  (`correlate._issue`); "found" is what the user felt first, then what spans
  many PCs, limits passed last (`ISSUE_ORDER`), each with "What we know"
  (`why`), `possible` causes and `who` (`others_text`, one shape: N other
  PCs, users, of how many, versions, since when). "What changed" is listed
  once per PC (`changes`). An app usage
  crash/hang and its health event are one row (`correlate._merge_usage`,
  names compared without ".exe", "(Process)" or spaces). "What the user
  felt" names each PC the user worked on. The CLI report wraps at 72. Keep it
  free of scores and jargon. The page has one timeline and one problems
  list; don't add views that repeat them.
- `verify.py`: the change log section also reads the whole 14 days of up to
  3 PCs that have rows with no other-type row of the same software at the
  same second, and prints each with its nearest one (removals, or updates
  whose rows are minutes apart?). The first 300 rows of all PCs cover only
  a short span, so pairing is judged per PC.
  For software put back at the same version: the PC's nearest start-up,
  its image builds, other PCs in that minute. VDI (`VDI_COLUMNS`,
  `remote_display` in `config.py`, read only by verify so far): which
  columns are filled, pooled or one desktop per user, remote display
  latency, and (`verify_vdi_network`, run by the CLI when both systems
  are set) which addresses NetIM can place: virtual desktops', physical
  PCs' (one per /24) and the devices users sit at (looked up in the
  inventory). "Process data" lists every source with `PROCESS_COLUMN`;
  value ranges include top_processes' CPU and memory. "fleet: …" (`_fleet`):
  PCs and users, how far back each source goes, the widest failures per day,
  how software arrives and whether the log's time is real, whether actions
  are slow right after a start-up of their PC (`_after_start_up`: share of
  slow actions 0-10 min after one, virtual and physical; for the activities
  most often slow there, runs right after against later ones), and which
  columns could place a virtual desktop user. What Aternity does not give
  us is listed for its admin in `docs/aternity-admin-questions.md`.
  A missing optional source (`OPTIONAL`) is a note saying what
  is skipped, not a FAIL; a wrong name gets paste-ready YAML only when the
  real name is very close. It checks every configured Aternity data source/column (against
  OData `$metadata`), NetIM metric class/metric (against NetIM's list, then
  one real fetch) and topology name, and prints the closest real names.
- `names.py`: the usernames and device names offered as you type in the
  Username and Device boxes (`/api/names`): every one seen in Aternity's
  activity, fed by the Today reader; dropped after 8 days unseen. Any name
  can still be typed. "Some may be missing" while a span cut short is not
  read whole again and is within the 8 days (`cut`; it stayed for ever).
- `today.py`: the "Today" tab, first (`/api/today`): what Aternity itself
  rated today, from midnight in `timezone` (config.yaml; empty = UTC):
  health events with severity in `TILE_SEVERITIES` (Critical, Major) as
  tiles, other events on the most PCs, `TOP_STATUS` (Major) slow actions
  per hour and per application. Each against the same kind of day before
  (working Mon-Fri / weekend; `usual`: more / fewer than any of them, or as
  usual), at the same time of day. One background reader per process
  (`keep_reading`; HTTP and HTTPS share it): activity an hour at a time,
  the last 24 h at start, then every `refresh_minutes` (config.yaml, 15
  unless set, 5 to 60: `REFRESH_RANGE`; `Today.refresh_min`; the names
  and the Care list go with it; a read starts refresh_min after the one
  before started, whatever a read takes: `Today.next_read`, it was a
  sleep after each read; each read is guarded, `_guarded`: an error the
  code did not expect is logged and the reader goes on, it once could
  stop the Today tab, names, Care list and assessments; a reply cut
  short counts in `failures`, so `read_ok` does not move, and a cut-short
  health read keeps today's rows it left out) the current hour and each hour once more
  15 min after it ends (late rows logged); health events today at each
  read, the 14 days before once a day. Activity is kept ~8 days, so
  each finished day's counts are saved in `$UXCORRELATE_DATA/days/<date>.json`
  (`./data`, writable by uid 1000; counts and app names, no user or PC);
  only complete days count as usual. A tile or row opens who is behind it
  (`/api/today/who`, `Today.who`: user + PC, most first, `WHO` at most,
  from the reader's own rows: Major actions with activity and time, health
  events with username); its Investigate ▸ runs an investigation of that
  user + PC (`SLOW_PAD` / `EVENT_PAD` around their first and last). The
  reader also keeps each action's PC kind (`VIRTUALIZATION`: virtual /
  physical, saved nightly as `kinds`) and each health event's application.
- Active users and PCs (the user's request, 2026-11-11): `Today.active`
  (at least one measured action, any rating, each once; PCs by
  Aternity's virtual / physical mark, users by the PCs they used), in
  `/api/today` as `active.today` and `active.recent` (the last full hour
  with this one so far, `since`), a strip above the Today tiles
  (`#tActive`), the chat's `active` block, the Explain meaning "Active".
  PCs that only report to Aternity (`TIME_LAST_REPORTING`) are not
  counted: not checked on prod yet.
- How an AI answer is laid out (`chat.SHAPE`, in the three SYSTEM texts;
  the user: "better format than just running sentences"): one line that
  answers, 2-4 lines starting "- ", a last line "Next: …". `clean` keeps
  the lines (any bullet becomes "- ", `LINES_MAX`); the page draws them
  (`answerHtml`, class `ans`); our own sentence is the first line alone.
  Explain also returns `terms` (`terms_on`: our meaning of each term on
  the panel, under every answer as "What the terms mean", open when it
  is the whole answer) and `ask` (`suggestions`: chips in the dialog).
- `assess.py`: the assessment for executives, first panel of the Today
  tab (`/api/assess`, `#asBox`; the user's request and "all recommended",
  2026-11-11). Every 3 hours from 06:00 (`HOURS` 6, 9, 12, 15, 18, the user 2026-11-12
  local; the first read after an hour writes it, `Assessments.tick`,
  called by the Today reader's `after=`; after a restart the latest hour
  passed is written with the time it is). Our code counts the facts as
  lines (`snapshot`: counts and names of events and applications, never
  a user or PC, with what the charts give, `chart_facts`), the verdict
  and what to look at first are by fixed rules (`verdict`, `first`: an
  AI service down is said, never first; the verdict says why,
  `_judged`, needs `VERDICT_PCS` PCs, and what it rests on is always
  among the lines). The richer wording (the user's pick, 2026-11-12):
  `meaning` (what the day means for people working: slow actions, PCs
  with a crash, the typical logon, each against its usual range), then
  lines under four labels (`PARTS`): Changed (`changed_lines`: new,
  rose, steady since the one before; the share of actions rated Major
  rose / fell, an hour against an hour), Watch (`watch_lines`: above its
  usual range now and not moved), Earlier (`earlier_lines`: hours above
  the range, or a count an earlier assessment had above it, cleared
  since), Start-ups (`startup_lines`); the day so far (`today_lines`).
  Fixed words: `reach` (isolated under `ISOLATED` = `VERDICT_PCS` PCs,
  limited, widespread from `WIDESPREAD` 50: my suggestion, not
  confirmed), above / within / below its usual range (`_against`; saved
  words of before Nov 12 read by `_pos`). The AI words them (`SYSTEM`,
  `word`; each point starts with its label), held to the lines' numbers
  and to the fixed words (`refused`: a word no line gives), else our own
  text (`ours`, kept with each period). Written once and saved in
  `<data>/assess/<date>.json`, `KEEP_DAYS`. The day before gets one
  summary (`_yesterday`, from `Today.saved` and `health_days`), shown as
  "Yesterday": written once, so evening and morning never differ.
  Under it the week (`week`, `drawWeek`, `#asWeek`; the user's pick A
  of three mocked, Nov 17 2026): this week and last, Monday to Sunday,
  PCs a day, each once, with a Critical / Major event
  (`TILE_SEVERITIES`, each kind's severity, `health_days` of days read
  whole) and `TOP_STATUS` slow actions (saved `top_pcs`, complete days
  only); today so far; a day not read in full is None, a gap, never 0.
  `verify --ai`: "writes the assessment" (`SAMPLE`; it counts the
  points with their label). Layout (Proposal 1, the user's pick,
  2026-11-12): the box is the banner in the verdict's colour (`--as`)
  and the words (`answerHtml(text, labels)`: a label is a small tag,
  the verdict sentence bold) with yesterday beside them (`#asYest`);
  no row of numbers. The three charts (`charts`, `miniChart`) are a row
  of their own in Today's issues under the active strip (`#tMinis`):
  one point per hour of today so far, the lowest to highest of the same
  hour on the `USUAL_DAYS` days before (same kind: the caption says "5 working
  days" or "weekend days", `kind`) as a band when `MIN_USUAL` have
  one: the share of actions rated Major (`MIN_ACTIONS` an hour, else no
  point: the one worked-out percentage on the page, the user's choice),
  the PCs with a crash so far today (each PC once, from `health_days`'
  first times), the typical logon time (`MIN_STARTS`; when there is
  none, `boots_note` says why). Start-ups: `_boots` reads `client.boots`
  for today again every `BOOTS_EVERY` and each day before once
  (`boots_of`: per Aternity type, how many, the middle value, per hour;
  the type with "logon" in its name is charted, no type name is
  assumed). Software is not in it (the user). The page does not show the
  lines a text was written from (frozen at its hour, they read as a
  mismatch beside the live numbers); the text is captioned "Written at
  HH:MM". The Today tiles have no small per-day chart (the user,
  2026-11-12); the "Other crashes" table keeps its bars.
- `care.py`: the "Care list" tab, second (`/api/care`, `/api/care/user`;
  link `#care`, as `#today`, `#investigate`, `#startups`): users in
  `<data>/care_list.json` (`LIST_FILE`, read at every check, `CARE_MAX`
  50; user's request 2026-11-10) are changed on the page: "+ Add a user"
  (`POST /api/care/users`, `Care.add`: Aternity's spelling via `NameList`,
  no repeats, checked at once) and a tile's "Remove from the Care list…"
  (`DELETE /api/care/users`, `Care.remove`: their saved issues and
  investigation go too), one in-page dialog (`careDialog`). The file is
  made once from `care_list` in config.yaml (the list's place before),
  which is not read after that; a broken file keeps the last list and is
  never written over. No login: each change is logged with the caller's
  address, and needs a PIN (`Care.pin`: today's date YYYYMMDD in the
  page's time zone, the user's choice 2026-11-11; `care_pin` in web.py,
  403 and a log line when wrong; it keeps a slip out, not someone who
  knows the rule). "Change label…" (`PUT /api/care/users`,
  `Care.relabel`) in the same dialog (`careDialog(user, edit)`). One check or change at a time (`Care.checking`). Users are checked
  after each read of the Today reader (`keep_reading(..., care=)`), from
  rows it holds (`Hour.user_devs`/`user_runs`/`dev_kind`). Issue: an event
  in `TILE_SEVERITIES` with their username today, a slow start-up, or more Major actions by
  this time of day than on any of their own days before with a PC in use
  (each Major action judged by its own time of day, the whole day at every
  check: `_replay`; judged only at the check against the usual by then,
  which grows all day, a Major action no check judged in time was lost:
  "1 rated Major" and no "Earlier today", stage, Nov 15 2026; a slow
  issue is not "back within their usual" while the usual is not known;
  `nearest` is still in the view, for the status summary only). The
  tiles show no "Earlier today", no "Why" and no `nearest` (the user,
  Nov 17 2026: "Completely remove: Earlier today, and Why"; two usuals
  on one tile read as a contradiction, "back within it at 05:19" as if
  something happened then, and "Why" was the investigation's headline,
  which can lead with a Minor action the tile did not list): an OK tile
  is the day's counts against their usual by now (`okText`, "more than
  their usual by now" when an alert went off above it); Show more lists
  today's issues, those over "(over)", and the investigation. The status
  feed for the other platform is unchanged (`_ok_words`). Every tile
  lists the user's Major actions of today, latest first (`majors`: time,
  application · activity · seconds, the PC when more than one; the latest
  3 on the tile, all under Show more): "1 rated Major" with nothing to
  show which it was read as a bug (stage, Nov 15 2026)
  (the latest `USUAL_DAYS`, any day of the week: people on the Care list
  often work weekends, the user, Nov 15 2026; the working / weekend split
  stays for the fleet's Today tab and assessment, where a Saturday hour had
  735 activity rows against about 10,550 on a working day; `MIN_USUAL`; their `HISTORY_DAYS` read
  by username once a day, spelled as Aternity writes it via `NameList`).
  An investigation is queued (`Care.work`, one at a time, one per user
  per `RUN_EVERY`) while one of today's issues is not covered by the
  latest one (`_uncovered`: since outside its window, or `found` after it
  ran: Aternity sent it late, or `RUN_EVERY` held it; the user's note of
  Nov 13), `INV_PAD` before their first issue of the day (open or over) to
  now, and "Investigate again now" the same (`_inv_start`, the tile's
  `start`); its
  title is in Show more, the page opens it without a re-run. Issues
  and the latest investigation: `<data>/care/<user>.json`, `KEEP_DAYS`
  (usernames on disk, agreed). A slow issue is a spell: over (`cleared`,
  `ended`) after `QUIET` (1 h) with no Major action (the day's count alone
  held it all day on stage) or back within their usual; a later spell is a
  new issue (key `slow <day> <n>`), a late row within `QUIET` rejoins it.
  Events of one kind are spells too (user's choice): over after `QUIET`
  with no new one; matched to saved issues by kind and overlap. A spell
  is grouping only (one issue, one count, one investigation). The alert
  (the tile's colour, the tab's count, `/api/MSS/care/status`; the user's
  rule, 2026-11-14): `alert` is on while a read brought more of an issue
  than it had seen (`seen`: events' `HEALTH_EVENT_VOLUME` added up, 1
  when empty, as investigations; slow: the day's Major actions while the
  spell is open) (`fresh`: that read's time), whatever the rows' own
  time (Aternity sends rows late), and goes off at the `ALERT_READS` (3)rd
  read of the Today reader after it (`left`, counted by `_count_read`,
  reads not minutes: 15 minutes held 2 reads when reads were slow,
  stage, Nov 14; `check(..., read=False)` after a change on the page is
  not a read and does not move `as_of`; `off`: the read that put it out,
  logged "no longer lit"); a failing disk (`disk`: `models.failing_disk`,
  the Today reader's 7th field) all day. A restart does not
  light again (`seen`, `left` saved; one lit before a restart goes on
  counting unless `ALERT_READS` reads' time passed, `_restarted`; an issue
  saved before has no `seen`: what it had is not new, none with `left`:
  the reads its time leaves); at a user's first check (`checked`: added,
  or after a restart) only an occurrence within the alert's time lights
  it. The tile: `issues` (alert on, `_clears`: "Aternity sent it at
  18:10." when that was more than a read after it happened (a 17:21 crash
  first sent at 18:10 read as an hour-long light, stage), then "Clears at
  the 18:25 read if no new Application Crash" (`Today.next_read`), or
  "Stays on today: a failing disk is the PC's state"; a PC's lit disk
  events are one line, `_one_disk`), the most serious first (disk,
  Critical, Major, events before slow), `earlier` (Show more only,
  "(over)"), `before` (the latest issue before today, "Last issue before
  today"). The tile's bars (`_major_days`, the user's choice of Nov 15
  2026): their Major actions by this time of day on each of the last
  `KEEP_DAYS` days, today last, from the days read for the usual
  (`history`), so today's bar is "N rated Major" and the days of the
  usual (dark) give its range, dashed is not read. No red dots (the
  user, Nov 17 2026: "makes no sense": a dot marked any saved issue of
  a day, a crash on a day with no Major action, at any hour on a bar
  counted up to now). They were the issues this app saved: none for a
  day it did not check or a Major action within the usual ("3 rated
  Major" with nothing behind it, stage). A history read cut short is
  no usual (`cut`: not judged, read again at the next check). An OK tile with Major actions their usual could not judge
  says "not compared" and why (`usual`'s `need`, `read`: too few days
  with a PC in the 8 days read; it was every Saturday while weekends were
  compared with weekends only, stage, Nov 14).
  Files: one unreadable no longer stops the others loading; a user's
  file under an older spelling is replaced by the newest
  (`load`, `save`). The investigation worker is guarded (`work`). Every user has a tile: alert (a
  failing disk, Critical, Major: as a tile's own issues), then not seen
  in 8 days, then "✓ OK"; inside each, the list's own order (`place`),
  set by "⠿ Arrange tiles" (the user's request, 2026-11-14: drag by ⠿,
  ↑ ↓, arrow keys; `PUT /api/care/order`, `Care.reorder`, PIN, 409 when
  the list changed meanwhile; the 5-minute refresh does not draw over
  it). `view` is not ready until the first check. `status` is `GET /api/MSS/care/status` (the user's request,
  2026-11-14, for another platform that asks every 5 minutes): ready,
  as_of (`Today.read_ok`: the last read with no failed read,
  `Today.failures`), refresh_minutes, note, users in the tab's order with
  username, label, status alert / ok / unknown, level, since (the alert's
  issue; OK: when the last alert ended, `_alert_end`: its `off` read, else local
  midnight), summary (`_said`, `_ok_words`, `_fit`: our words, never AI,
  `_ascii`, `SUMMARY_MAX` 120, "+N more: ..."); all times UTC, no PC
  field (PCs only in the summary). Step 2 (the user's "build it A and
  B", 2026-11-17; alerts out of the app: not for now): a slow start-up
  (`_starts`, kind "startup", level None, the tile's `tag` `START_TAG`:
  Aternity rates none) is a start-up of theirs today (`group_starts`)
  with a type longer than every one of theirs in `BOOT_DAYS` (30) days
  before (`BOOT_MIN` 5: A) and than 1 in 10 of everyone's today (B:
  `Assessments.fleet_boots`, `boots_of`'s `p90`, `P90_MIN`; read at the
  check when not read since the start, a new day at its first read);
  rows with their username only (`client.boots(usernames=)`, one read
  of today per check, `BOOT_BATCH`, rows added to earlier reads';
  `_read_boot_history` once a day, cut short: not compared). One issue
  per start-up (its PC, times overlapping); "Found at the HH:MM read"
  (not "Aternity sent it"); `detail`'s `starts` (each type against both,
  or `why`) and "Open in Start-ups ▸" (`cStarts`). AD on the tab (the
  user's "all three", 2026-11-17): `GET /api/care/ad` (the list's own
  users only, `ad.Lookup.look(page=)`: name, department, disabled,
  found; asked after the tiles are drawn, `loadCareAd`), the tile's
  title `careTitle` (label, else AD's name), `careSub`, "disabled in
  AD"; `POST /api/care/find` (PIN, 3+ characters: `Lookup.named`, each
  with `aternity_spelling`), the dialog's `#cFindRow`; the status feed
  keeps the typed label.
- `software.py`: the "Software" tab, fifth (`/api/software`, `#software`;
  docs/software-tab.md, decisions of 2026-11-10). One background reader
  (`keep_reading`, every `READ_EVERY_H`, a few minutes) reads the
  installed list added up by Aternity (`SOFTWARE_COLUMNS`), each program
  with more installs than PCs PC by PC (`MULTI_MAX`), Windows per PC and
  its updates (`OS_COLUMNS`, `SOFTWARE_OS_UPDATE`), and app versions on
  usage and actions over `DAYS` (`VERSION_SOURCES`, per PC for up to
  `APPS_MAX` apps with a problem on 2+ versions; `VERSION_OPTIONAL`
  columns only when published, via $metadata). When Aternity does not answer for the list itself
  (`snap["unread"]`), nothing is saved (it would empty the day's flags),
  the last good read stays and it is tried again every `RETRY_MIN` (15;
  it waited a day before: stage, Nov 2026). Red flags (`_flags`, one
  line per kind, each raised version of flag 1 its own): `judge` (`RATIO`,
  `MIN_PCS`, `MIN_PROBLEM`; an app whose every listed PC failed, as
  Trellix ENS on live, is held only where it failed: no crash or hang
  share, `only`), behind (older than the version most PCs have, decision
  2; PC by PC when read so), 2+ versions, one user, Windows updates on
  more than `WIN_MOST` of a version's PCs (and what the PCs without one
  have: a later one may be in its place), `FEW_PCS`. Saved per day in
  `<data>/software/<date>.json` (counts and names, no PC or user),
  `KEEP_DAYS`, for "new" and each flag's days. A click reads now:
  `pcs_with_program`, `pcs_with_problem`, `compare` (health events matched
  by the version the PC ran), `before_after` (PC-days on each side),
  `pcs_without_update` (where `missing_on` the latest read lists it, same
  names as the daily read so the total is its "not on N"; with what each
  has in its place, those with none first).
  `summary`: GPT for management, held to the flags' numbers (chat's
  `numbers`/`allowed`/`RATIO`), else `our_summary`. `split_by_program`:
  an app's problems by another program's version (chat only, never a flag).
- `software_chat.py`: the Software tab's look-ups. Ask anything uses its
  blocks as they are (`agent.SOFTWARE_TOOLS`); its own chat "Ask about
  software" (`/api/software/chat`, the same loop as `chat.converse`) is
  no longer on the page (step 3); `verify --ai` measures its blocks'
  picks ("picks the right software look-up"). Blocks (`TOOLS`): flags (kind "all", first and
  required: offered only the kinds, GPT answered "the red flags" with
  crash alone on stage; or one kind's whole list; a question naming one
  application goes to version_problems, said in both descriptions, as
  Nemotron took it to the flags), versions, version_problems (from the latest read), compare,
  before_after (its description says when to use it: "got worse after its last update"
  went to version_problems with Nemotron, verify --ai on stage, Nov 14), program_split, pcs, pcs_without_update (read now, as a
  click; Aternity's error is said in the answer). Names are matched to the latest read (`pick`:
  exact, the one that has it, the one that ends with it: "Edge" =
  Microsoft Edge), versions by their start (`software.version_of`,
  `starts_with`: "143" = every 143.x). A version is said to crash more
  only when raised (`RULES` note, "Raised" column).
  pcs with a problem (crash, hang, slow) and no application takes the
  name given as the application (Nemotron sent the Outlook crash
  question as program=, which listed the PCs with it installed: stage,
  Nov 2026); verify accepts both picks.
  version_problems for an application the daily read left out (1 version,
  under `MIN_PROBLEM`, beyond `APPS_MAX`) reads it now (`software.app_now`,
  same rows and rules as the daily read: `_app_rows`, `_judged`), asking
  Aternity for the name as typed when several names have it: "Which
  version of Microsoft Edge crashed the most?" was answered "several
  applications have Microsoft Edge" (Edge Update, Edge WebView2) on
  stage. A note names the version with the most PCs with a crash.
  With nothing in app usage under that name (the Edge browser on stage:
  only Edge Update and Edge WebView2 are there), the answer comes from
  its crash events by the failing part's version
  (`software.crashes_by_version`: counts, no shares, never raised); the
  chat's known names include the health events' applications (web.py).
  most (`most`): every application by its PCs with a crash, hang time
  or Major actions, each PC once (`totals` from `_judged`; for a crash
  also what only the crash events hold, `snap["crash_names"]`), a plain
  ranking, raised or not: "which software crashed the most?" had been
  answered from the crash red flags (Outlook, 200 PCs), which leave out
  what is not compared (Trellix ENS, 415 PCs: stage, Nov 2026).
  Name search, one rule (`software.words`, `named_like`; the user, Nov
  2026, after three fixes that each looked for an exact name Aternity
  never uses: "DO NOT FIX 'EDGE' SPECIFICALLY"): the telling words of
  what was asked ("Microsoft Edge" -> edge, `GENERIC` words dropped)
  against every name Aternity has: applications of app usage (all of
  them: `snap["usage"]`) and every crash event of `DAYS` days by
  application, program file and version (`software._crashes`,
  `snap["crashes"]`: names and counts, read a day at a time with the
  daily read). With no application of exactly that name,
  version_problems shows the crashes per version of everything named
  like it (`_crashes_like`; app usage applications not read daily are
  read now, `LIKE_NOW` at most), never only a list of names to ask
  again with. `pick` and the Today chat's `_match` end with the same
  rule.
  compare, before_after, program_split and pcs do the same through
  `_app` (the name as typed when several have it; `_like` names them
  when Aternity has nothing).
- `chat.py`: the block chat "Ask about today" (`/api/chat`; no longer on
  the page since step 3 of Ask anything, the endpoint kept; `verify --ai`
  no longer measures it: its picks and "repeats the counts" went, Nov 13,
  after Nemotron failed the counts on stage; its helpers `logins`, `_match`, `numbers`/`allowed` serve Ask
  anything); its question loop `converse` is shared with `software_chat.py`. The AI
  (`ai.chat` by name, else the first of `ai.services`; GPT on prod) picks
  one building block of `TOOLS` (slow_actions, events, who, investigate),
  our code counts from what the Today reader holds (no extra Aternity
  load), the AI words it in 2-4 sentences (`SYSTEM`): usual or not, only
  as the counts say; what stands out, by name; one next step. Every
  slow_actions block for all apps carries the Today tile's usual and,
  when its rows name no app, the top 3 apps (else the answer is dry:
  seen on stage). Words about usual ("as usual", "more … than usual",
  "unusual") must match a note or row's own verdict, or read a Usual
  column with values (`usual_wrong`; GPT said "As usual" with no usual
  yet on stage), else our sentence. By application: a note names the
  highest Major share of apps with `SHARE_RUNS` runs or more (the top by
  count is not always the worst). Events by event, today: a note gathers
  the rows' usual words, more than usual first (GPT skipped the column
  on stage, but uses notes). Every number in its sentence (digits, and
  words two to twenty: `WORD_NUMBERS`, "one" is a pronoun) must be in the
  counts given, the question or the settings (`numbers`/`allowed`), and
  no worked-out comparison (`RATIO`, "3 times worse"); otherwise our own
  sentence (`say`) is shown, with a note. The counts table is always
  shown. investigate only resolves names (`NameList`, exact or the one
  match) and a window: its button runs the Investigate tab. events and
  who (what=event) take `app` (only events Aternity recorded with that
  application; no usual then, it is known for all applications only):
  without it "how many Outlook crashed today" counted every
  application's crashes and its who list led to users whose Outlook had
  not crashed (stage, Nov 2026). One rule for an application's name in
  every block (`_match`): the exact name, else every name that has it,
  said in a note. who (what=event) lists every kind the name matches,
  with an Event column; per day says which kinds it added up. Notes
  `AFTER_HANG` (an investigation lists that event under hangs) and
  `EVENTS_ONLY` (hang time from app usage is not an event; only with
  an application's events). who (what=event) takes `days` (`_who_days`:
  the PCs over up to `HEALTH_DAYS`+1 days from `health_days`, which
  keeps the PC, not the user; `WHO_ROWS`): "List PCs with System crash
  this week" had given today's one PC, then counts per day (stage, Nov
  2026). The AI is given Aternity's own names of the kinds of event with
  each question (`event_kinds`, `kinds_text`, `KINDS_MAX`, in
  `system`) and picks the ones meant, several separated by "|"
  ("What PCs had hard drive issue this week?" was "no event named
  Hard drive": HD Failure and Faulty HD S.M.A.R.T. Status, stage,
  Nov 2026). A name no kind has is matched by a shared telling word
  (`_kinds_named`), said in a note; with none, the note names the
  kinds there are; a plural is read as Aternity's singular (`_singular`: crashes
  -> Application Crash). A question naming a kind of event needs
  `name` (by application without it, every kind is counted: Nemotron
  took "Which crashes hit the most PCs?" so on stage, Nov 2026). logins (the user's request 2026-11-12, chat only: no tab, no
  page of its own): who logged in to a PC, or which PC a user logged in
  to. The latest (`LOGIN_DAYS` back) or one local day: Aternity's
  start-up and logon rows read now for that one user or PC
  (`client.boots(username=, device=)`, via `aternity_of` passed to
  `ask` / `run_block`; rows of one user and PC within `LOGIN_GAP` are
  one logon, with the types Aternity recorded; no type name assumed).
  `current`: from the Today reader's `Hour.user_devs`, the latest hour
  of today with a measured action, no read of Aternity; it says where
  they last worked, not that a session is open. Names by `_named`
  (exact, the one that has it, else as typed). A logon Aternity did
  not record is never made up; how long it keeps them is not known.
  Hangs per
  app and software changes are not blocks yet (to measure with verify).
- `agent.py`: "Ask anything" (`/api/ask`, `#tAsk`: the one chat, at the
  bottom of every tab since step 3; step 1 of the open chat, the user's request 2026-11-12: "HOW
  DO WE MAKE THIS LIKE NORMAL REGULAR CHAT", after the block chat
  answered "no event named Hard drive"). The AI looks things up with
  general tools (`TOOLS`: find, events, activity, overview, meanings),
  several per question (`MAX_STEPS`), sees rows with a summary of the
  counts (`ROWS_MAX`, `RESULT_CHARS`), and answers in its own words
  (`SYSTEM`; Aternity's kinds of event named in `system`). Every step
  goes to the page ("What I looked at", Investigate ▸ per row from
  `runs`); a number in none of them, or a worked-out comparison
  (`COMPARED`: "twice as", not "twice", a count), is marked
  (`flagged`), not replaced. Names the AI sends are read with plain
  dashes and spaces (`_norm`; `explain.plain` for verify: Nemotron
  writes no-break hyphens, stage, Nov 2026). History: the answers and a line of the
  look-ups (`looked`). Those five read only what the Today reader and
  the names hold. Step 2 (2026-11-12, "build step 2"): look-ups that
  read Aternity now, given a `Reach` by web.py (none: they say it is
  not set): logons (`chat.logins`), startups (`logons.logon_report`,
  "1 in 10 over" only from 10 measured), pc_software (one PC's
  `INSTALLED_SOFTWARE` + its Windows, columns via `software._Reads`),
  pc_changes (`cause._updates`, a batch of removals one row as
  `answer._changed`), programs_per_pc (`software.programs_per_pc`:
  stage asked "which PC has the most software installed?" and got "I
  can't tell"; every PC's list in one read is refused on live,
  "Returned data too long"; INSTALLED_SW_COUNT added up per PC was 511
  for a list of 619 rows: used only when it equals the different
  program names (or rows) of the own lists of the PCs with the most and
  fewest, said which; else the numbers, no count; kept
  `PER_PC_KEEP_MIN`; pc_software's "programs" are names too), the
  Software tab's blocks as they are
  (`software_chat.TOOLS`/`run_block`, `_block` makes a step), and
  investigate (a whole investigation, `INVESTIGATE_HOURS` by default,
  `INVESTIGATE_MAX_H` at most; the AI reads `render_text`, cut to
  `TEXT_CHARS`; the page's "Open this investigation ▸" shows the result
  without a re-run, "Open in Start-ups ▸" runs that tab). At most
  `MAX_READS` reads and `MAX_INVESTIGATIONS` per question. `/api/ask`
  is a job (`/api/jobs/{id}`): the page shows each look-up as it
  starts. A failed look-up is a step that says so, never a lost
  question. Earlier look-ups go in the instructions, not after the
  answers (the AI wrote "(I looked at: …)" into its own, stage). Step 3
  (2026-11-12, "finish 1, 2, and 3"): it replaced "Ask about today",
  "Ask about software" and "Ask about this page"; the page sends the tab
  open as shown (`tabShown()`, `shown()` of `TAB_SHOWS`) with each
  question, `page` gives it to the AI with our meaning of each term on
  that tab (`explain.given`, cut at `explain.LINES_MAX`; a step's text is
  given whole by `for_ai`), and the instructions name the tab. Examples
  per tab (`ASK_EXAMPLES`); answers stay when the tab changes. `verify --ai`:
  "answers open questions" (`AI_ASK` over `ask_records`, made-up;
  `AskAternity` made-up logons; the step 2 questions must use their
  look-up; step 3's asks about a made-up Care list tab, `ASK_PAGE`, and
  must use page). An investigation asked for with no time starts
  `care.INV_PAD` before the person's first Critical or Major event or
  Major action today, the last 4 hours at least (`_first_issue`; with
  the last 4 hours only, one asked at 12:04 left out a 06:04 crash).
  A name the names know exactly as a user only, sent as the PC (or a
  PC's sent as the user), is looked up as what it is, said in a note
  (`_user_or_pc`, the look-ups in `BOTH`; Nemotron sent
  startups(pc=sample02) and found nothing: verify --ai, stage, Nov 13).
- `ai.py`: client for the self-hosted AI services (`ai.services` in
  config.yaml, key from the env var named in `key_env`). OpenAI-style API
  (`verify.verify_ai`, passed on prod for both services: key, reachable,
  login header, plain reply, then the right software look-up in 12 tries
  covering every block (`software_chat.TOOLS`, `AI_SW_TRIES`) + no pick
  outside the data, Explain, the assessment, Ask anything; the old Today
  chat's checks went with it from the page).
- `verify --overview` (`verify._overview`, also at the end of a full verify):
  what a "today's issues" overview could rest on: activity rows an hour,
  Aternity's own status and severity words with counts, and whether its
  incidents and alerts (`OVERVIEW_SOURCES` in `config.py`) are published,
  and Aternity's label on each failing part over 7 days (`part_label`;
  under those in `DISK_PARTS` each event with Aternity's category and
  severity, said "a failing disk" or "not a failing disk: category …"
  by `models.failing_disk`; stage, Nov 16: Low Disk Space and Corrupted
  FS under "(Drive)" are System, HD Failure and Faulty HD S.M.A.R.T.
  Status Hardware), so other hardware is added from it, and
  from the same read whether one row holds several times
  (`_times_in_a_row`: `HEALTH_EVENT_VOLUME` above 1, by event).
- `secureview.py`: SecureView, the admin page's SECUREVIEW tab
  (`/admin/api/secureview`, `/admin/api/secureview/device`; the user's
  "build it", 2026-11-17; docs/admin-roadmap.md). The devices people sit
  at (`CLIENT_DEVICE_NAME`) matching config.yaml's `secureview.match`
  (`SecureViewConfig`, any case; empty: off and the column is not read;
  a bad pattern is a `config_notes` note and `error`). The Today reader
  gives it every hour it reads, any day (`Today.secureview`, in
  `_read_hour` before the other-day return): each matching row widens
  its device's local day (`add_hour`: first, last, local hours, people
  with their own first and last, desktops; never narrows); an hour read
  after it ended is `final`, cut short `cut`; a day `complete` when every
  hour is final. Saved after each read (`save`, atomic, dirty days),
  `KEEP_DAYS` 400. Before the records: `backfill` (thread
  `keep_backfilling`, after the first read, then 03:00 local), day by
  day for days not read in full, newest first (the user, 2026-11-17:
  people's history and "first heard and last heard also for all
  dates"): `SECUREVIEW_HOURLY` while it has rows (`_hourly`,
  `HOURLY_SPAN` 6 h a read, cut short: hour by hour; its TIMEFRAME read
  as the hour's start; how long it is kept is not known: two days in a
  row with none, one after a day with some, a failed read, or rows that
  name no device end it), then `SECUREVIEW_DAILY` (`_daily`, the
  `TIMEFRAME` + 12 h local day), each with USERNAME and DEVICE_NAME,
  added up by Aternity. Kept apart from the records (`back`: by, h,
  people) and never moving a time SecureView saw (`ends`: the records'
  minute unless Aternity's hour is earlier / later, "08:xx"; `_people`,
  `_times`); `daily` ok / none / failed with `hour` (the days by the
  hour), said on History; `STATE_V` 2: days read before (devices only)
  are read again. `view`: Today (usual by now from `USUAL_DAYS`
  complete days of the same kind, `MIN_USUAL`; hours with their band;
  "new" only with `NEW_DAYS` of records) and History (every device heard,
  silent days, the last person with their own times, per day with `part`
  days, silent 30+ day by day, `unread` days). No AD per device (THIN
  devices are not in AD); the page groups by the people's AD department
  (`svByDept`, the shared `adLook`, `AD_ASK` names a request), drawn
  across the page in columns (`svDeptRows`, the user's pick B of four
  mocked, 2026-11-17: beside the hour chart 15-20 departments left the
  space under it empty); a row is a button that sets the Department
  box (`svDeptPick`; History's "no person recorded" too,
  `SV_NO_PERSON`). Page:
  `sv*` functions in admin.html, `SV` state kept across its own
  minute's redraw (not while a box of it has focus) and while the
  other tab is open. SEARCH (the third sub-tab; the user's "build
  it", 2026-11-17: "I need to be able to search usage history"):
  `/admin/api/secureview/search?q=` (`SEARCH_MIN` 2 characters),
  `search`: every day kept, people (their own times per device-day),
  devices, desktops whose name has q, latest first, `SEARCH_MAX` 20
  of each (`more`); every day a row ("day only", "08:xx": `SV_TIMES`
  says it under each list); the page shows `SV_DAYS_SHOWN` 30 days
  then "Show all". When nothing it
  recorded has q (3+ characters, a letter), web.py asks AD once by a
  real name (`ad.Lookup.named`: every word in displayName / givenName
  / sn, `NAMED_MAX`, the same off / refused / `RETRY_MIN` rules as
  `look`, nothing kept) and searches those accounts (`_sam`: the
  account part of Aternity's username); each with its Aternity
  spelling from the names list (`aternity`) for "All actions today
  ▸". A username anywhere in SecureView is a link (`svWho`, `data-sq`)
  to SEARCH with it; CSV (`svSCsv`).
- `actions.py`: the admin page's ACTIONS tab (`/admin/api/actions?user=`,
  `#actions`; the user, 2026-11-17: "search by user for today all
  actions with timestamp", "in admin page", any user, not only
  SecureView's). `read`: one query of the activity for that username
  from local midnight to now, `COLUMNS` only, `ROWS_MAX` 5,000 (+1 to
  know it was cut: `cut`), a reply cut short `partial` (the warnings'
  count), both said in `notes`; latest first, local times to the
  second. `spelled`: Aternity's spelling from the names list, else as
  typed with a note. One read at a time (`actions_lock`), on the Load
  panel as "admin page" (`/admin/api`). Nothing kept. Page: `act*`
  functions and `ACT` in admin.html; read only on Show / Read again /
  "All actions today ▸" (`actFor`), no minute; suggestions from
  `/api/names`; an application box, CSV, "Show all" after 200.
- `ad.py` + `verify --ad` (`verify.verify_ad`, the user's "build it",
  2026-11-15; docs/admin-roadmap.md): Active Directory, read only.
  verify and the app's look-up share `find_dcs`, `ports_of`, `best`,
  `usable` (one rule). The admin page's names (`ad.Lookup`, the user's
  "build it", 2026-11-16; passed on prod first: verify --ad with and
  without the account): `POST /admin/api/ad` (admin login) with the
  page's usernames and desktops, asked after the page is drawn; each
  name read at most once in `KEEP_H` (24), in memory only (`user_view`,
  `pc_view`), the DC that answered kept (`Lookup.dc`); not answering:
  `RETRY_MIN` (5) before the next try, whatever the page asks; a refused
  login (`Directory.refused`, a 49) never tried again in that process;
  searches told to http.METER as "Active Directory" (purpose "admin
  page"); `ASK_MAX`, `NAME_MAX`. The page (`adLook`, `adFill`, `adLine`):
  name · department after the username (title, office on hover), each
  desktop's OU and description in the day's table only, "disabled in
  AD" / "not in AD", one line for not set / did not answer / refused.
  Domain: `ad.domain` (config.yaml `ad:`,
  `AdConfig`), else the host's resolv.conf search domain and those
  above it (`candidates`); DCs: `ad.servers`, else DNS SRV
  (`SRV_NAMES`: `_ldap._tcp.dc._msdcs.<domain>`, then `_ldap._tcp`),
  asked of `ad.dns` (names allowed: a DC's address from it, `Dc.addr`)
  or the host's; up to `AD_DCS` DCs' 636 / 389 / StartTLS; the RootDSE
  with no login. The account is `AD_USERNAME` / `AD_PASSWORD` in .env
  only (in config.yaml it is a ValueError). Login (`Directory.login`):
  LDAPS simple (UPN `name@domain`, AD's implicit UPN), StartTLS
  simple, LDAPS NTLM, 389 NTLM (`DOMAIN\name` as given, else the DNS
  domain: the NetBIOS name is not known before a login); `open`
  refuses a simple bind over plain LDAP; a 49 stops the tries (each
  wrong one counts towards AD's lockout); AD's data codes in plain
  words (`WHY`; 52e is "the name or password is wrong": Samba and AD
  send it for a name they do not know too). Certificates not checked
  (as `http.py`). Look-ups in OR batches of `BATCH`, values escaped:
  users by sAMAccountName / userPrincipalName (`USER_ATTRS`), PCs by
  sAMAccountName `NAME$` (15 characters), cn, dNSHostName
  (`COMPUTER_ATTRS`; lastLogonTimestamp is FILETIME, kept by AD within
  about 2 weeks). Then `AD_SAMPLE` (50) most active usernames and PCs of
  the last hour (`AD_ROWS`): "N of 50 … are in AD", disabled, and how
  Aternity writes them; counts only, names only when typed (`--user`,
  `--pc`: details, or `similar` names). Runs in the full verify once
  `ad:` or `AD_USERNAME` is set. Dev: `sample-ad` (dev/compose.yaml), a
  real Samba DC filled by `dev/sample_ad.sh`; Samba has no Microsoft
  "Sicily" NTLM bind, so NTLM is not proven on dev. Tests: ldap3's
  MOCK_SYNC with `OFFLINE_AD_2012_R2` (`tests/test_ad.py`).
- `verify --clients` (`verify.verify_clients`, only on its own; the
  user's request, Nov 16 2026: track virtual desktops used from devices
  not starting with WT, and are those the ones on VPN?): `--except`
  (beginnings set apart), `--hours` (`CLIENT_HOURS`), `CLIENT_ROWS`.
  Virtual desktop actions by `CLIENT_DEVICE_NAME` (documented by
  Riverbed: the device connecting to the VDI; data-meanings M6): how the
  names begin (`_begins`), `CLIENT_NAMES` of the others by name (device
  names and desktops shown, the user asked for names to check; users
  counted), each `LOCATION_ON_VPN` value as sent (`CLIENT_COLUMNS`,
  never read on live before) against WT / not WT, physical PCs apart.
  Which value means VPN is not assumed: every value is listed.
- `verify --inventory` (`verify.verify_inventory`, only on its own; the
  user, Nov 20 2026: how does Aternity handle PCs that no longer exist,
  is there a "last heard"? Riverbed's pages give no rule): from
  `DEVICE_INVENTORY`, PCs by when last heard (`INVENTORY_COLUMNS`:
  `TIME_LAST_REPORTING`, never read on live before; `INV_AGES`, local
  days), the oldest kept, `AGENT_CURRENT_STATUS` as sent with each
  value's range of last reports (no value's meaning assumed), and the
  PCs silent `INV_SILENT` (8)+ days still in `INSTALLED_SOFTWARE`. With
  none silent it says "not answered", never "none is gone". No PC names.
- `verify --software` (`verify._software`, only on its own), short: what
  `INSTALLED_SOFTWARE` (`SOFTWARE_SOURCE`, `SOFTWARE_COLUMNS`) gives the
  Software tab: PCs, programs, installs and `SOFTWARE_USER_SCOPE`
  installs (name + version alone comes added up, `DEVICE_COUNT`; ~43
  programs per PC on live), 2+ versions on one PC (asked per name only:
  installs above PCs, checked PC by PC on the program with the most
  versions; `VERSION_COUNT` is 1 on every live row, useless), Windows
  per PC (`OS_COLUMNS`, `_windows`) and each `SOFTWARE_OS_UPDATE` on its
  own Windows version's PCs. What was settled on stage (filled columns,
  versions per program) is no longer printed. `--pc NAME`: only that
  PC's list. No PC or user names (unless typed).
- `verify --versions` (`verify._versions`): Aternity's own
  `APPLICATION_VERSION` on what users feel (`VERSION_SOURCES`: daily app
  usage with crashes / hang time, daily actions with Major), filled? the
  oldest day kept? added up when the PC is not asked for? For the app
  with the most crashes: per version, PCs, with a crash, with hang time
  (`VER_DAYS`). Health events' failing-part version; change log updates
  before to after (`LOG_BACK`). For the Software tab
  (`docs/software-tab.md`, decisions taken 2026-11-10). Checks for prod are verify options, never pasted
  scripts: the user cannot paste on the prod hosts.
- `explain.py`: "Explain this page" (docs/explain-this-page.md, agreed
  2026-11-10; all 4 steps built): a panel's own lines
  plus our meaning of each term on its tab (`GLOSSARY`, `COMMON`: the one
  place a term's meaning is written, built from the code's constants;
  the user reviews it) are given to the AI, which words them (`SYSTEM`,
  `messages`); `refused`: a number not in the lines, the meanings or the
  question, or a worked-out comparison (the chats' `numbers`/`allowed`/
  `RATIO`). `verify --ai` measures it (`verify._explains`): "explains a
  panel" (`SAMPLES`, 2 made-up panels per tab) and "explains a long
  page" (`long_page`, `SIZES`: the line asked about is three quarters
  down; both models right at 40,000 characters on stage: `LINES_MAX`).
  Step 2 (Today, Care list): `/api/explain` (`explain.explain`) gets the
  panel as the page shows it (`shown()` in index.html: text, a table row
  or a tile as one line, a chart's bar titles; `noExplain` and buttons
  left out), so nothing is formatted twice; a refused or missing AI
  answer becomes `our_words` (the meanings of the terms asked about,
  then the panel's). "Explain ▸" (`button.explain`, `data-tab`) at the
  bottom right of a panel, one dialog (`explainNow`), with a question
  box. A question about a whole tab is Ask anything's (`page`, which
  gives the AI `given` of the tab); "Ask about this page" was removed
  (step 3). Nothing saved. Steps 3-4 (2026-11-11): the link is added by
  script to every panel of Software, Investigate and Start-ups; a
  button's text is sent only when it is a name shown as a link (an
  issue, a program).
- `status.py`: the connection lights at the right of the top bar
  (`/api/status`, user's request 2026-11-10): Aternity, NetIM and each
  `ai.services` entry, one small read each every `CHECK_EVERY_S` (60) by
  one background thread (an activity row with $top=1; the device list
  with limit 1; the AI service's model list with the chats' key and
  header), `CHECK_TIMEOUT_S`, no second try. ok / down (flashes red in
  the bar; no answer, or login refused) / warn (the model set is not in
  the service's list, or the list is empty: it said "is listed") / off
  (not set). A click on any light opens the
  one "Connections" dialog (since when, reply time, which model the
  chats use; what config.yaml has that is not used, `config.config_notes`:
  a key at the top that is no setting, refresh_minutes out of range, logged
  once; verify prints them first, with a wrong time zone, which the page
  leaves to the Today tab). State changes are logged. No URL on the page. It proves
  connection and login, not the model's answers (`verify --ai`).
- `usage.py` + `static/admin.html`: the admin page (`/admin`, no link
  from the main page; docs/admin-roadmap.md, the user's request and
  choices, 2026-11-15). Folder tabs as the main page's (the user,
  2026-11-17): STATISTICS (the usage panels, first), SECUREVIEW and
  ACTIONS; `openTab`, the link `#statistics` / `#secureview` /
  `#actions` (`TABS`,
  `tabOfLink`, hashchange); only the open tab is read and keeps its
  minute (`load`, `svLoad` return on the other tab; `TAB.seq` drops a
  read overtaken by a newer one or sent before a log-out); the Day box
  is Statistics' only (`#statsBar`, kept in its place so the bar does
  not jump; `scrollbar-gutter: stable` for the same). Login: `ADMIN_PASSWORD` in .env, HTTPS only
  (`admin_https`: the scheme, X-Forwarded-Proto behind the ingress),
  cookie `uxadmin` Path=/admin 8 h (`ADMIN_HOURS`), in memory (a restart
  logs out), `ADMIN_TRIES` 5 wrong from one address lock it
  `ADMIN_LOCK_MIN`; logged. Who: the page posts each tab opened
  (`/api/seen`, `seenTab` in `mode()`, left out of the log); the address
  goes into `Today.watch` and the reader keeps that address's rows
  (`Hour.addr`, `Today.at_address`; `DEVICE_IP_ADDRESS` added to the
  hour's select); `Usage._who`: the user nearest the visit within `MATCH`
  (several at once: "one of N"), for good when Aternity has a row at
  that time, else `GIVE_UP` (2 h) after it ("not known"; before that
  "waiting for Aternity": it said "not known" from the first second,
  before any read had looked, prod Nov 15); only named people are
  counted as people (`_days` `unnamed`); every visit
  from one address: a note, never a guess. Investigations by where they
  started (`InvestigateBody.source`: the page's `invFrom`, a tab or
  "ask"; "care-auto" is the Care list's own). Every call through
  `RestClient.get_json`/`post_json` is told to `http.METER` under
  `http.PURPOSE` (a ContextVar: the middleware sets it by path,
  `PURPOSES`; threads by `as_doing`; NetIM's pool copies it); a read
  cut short to `http.CUT`. The Today reader's read times
  (`Today.read_took`, the start read apart: `start_took`). A day is
  `<data>/usage/<date>.json` (local), written at most every minute,
  after each read and at shutdown; `KEEP_DAYS` 30, then deleted.
- `http.py` is the shared REST client (basic auth, retries on 429/5xx);
  `models.py` holds the dataclasses and `parse_time`. `get_json(...,
  answered=)`: statuses that are an expected answer, still raised, counted
  by METER as answered (`ai.NO_LIST` on the model list: GPT-5.4's 404 to
  the light's check every minute was "816 failed" a day on the admin
  page, prod Nov 18 2026). `usage._counts` keeps per source the calls
  whose reply had rows: none (an AI service) is `rows` None, not said.
- `report.py` renders the text report, `cli.py` is the argparse entry point,
  and `web.py` is the FastAPI app. Investigations and start-ups run as
  jobs (`/api/jobs`, a thread each, the page asks every second); a
  finished job is kept `KEEP_DONE_MIN` (10) for its tab, a running one
  always (20 kept by count dropped running ones).
- `static/index.html` is the single-page UI. It is dependency-free and draws
  its charts with inline SVG. Tabs, in order: Today, Care list,
  Investigate, Start-ups, Software (`mode()`; each with a link,
  `TAB_LINK`). A tab opens where it was left (`scrollOf`), at its top when
  a result is put in it (`mode(tab, true)`) or it is clicked when open.
  Scrolled down to the tabs, the bar pins as one slim row (`pinBar`,
  `#headWrap` keeps its height: nothing jumps) and "↑ Top" shows (the
  user's request, 2026-11-13: long pages). Every reply is read by
  `readJson` (a proxy's HTML 502/504 was "Unexpected token '<'") and a
  running job by `jobNow` (a dropped connection or 502-504 tried again,
  `JOB_TRIES`: one blip ended the wait while the job ran on). Dialogs are in-page (`swDialog`), never `alert()`. A CSS
  `display` on an element with `hidden` needs its own `[hidden]` rule.
  Every "Investigate ▸" (Today lists, the chats, the Care list, the
  Software lists) goes through `investigateNow()`: fill the Investigate
  form and run it at once. One chat on every tab: Ask anything
  (`#tAsk`, last in `main`; `tabNow` is the tab open; "Ask a question ▾"
  on Today and Software scrolls to it).
  Two scripts must not declare the same top-level name (the later one wins
  silently: a second `renderWho` once broke the Today list).
- `tests/`: pytest. HTTP is mocked with `httpx.MockTransport`; the engine is
  tested with `tests/synthetic.py` (fake Aternity/NetIM, branch WAN
  saturation scenario) and stubs. There is no demo mode in the product; don't
  add one back. Files deleted after N days are judged by `Today.clock` /
  `Software.clock` (Care by its Today's): a test with a fixed day gives it
  (`clock=lambda: NOW`), or it fails once the real date passes N days
  after it (the Care tests, dated Nov 9, failed on Nov 18 2026).

## Conventions and gotchas

- New features run on a stage instance (a second Docker host) before the
  live one; the user copies files there by hand. Each step names the
  instance it is for.

- **Riverbed names and layouts come from Riverbed's own material only**,
  never from guesses: a real Aternity `$metadata` and NetIM layouts from the
  `steelscript-netim` SDK, kept in `tests/fixtures/` (sources in its
  README) and pinned by `tests/test_contract.py`. A new Aternity column or
  NetIM field must be in those fixtures or a Riverbed source first.
  The live hosts are air-gapped and one-way: nothing comes back to dev, so
  `verify.py` must tell the person on the live host what to change (paste-
  ready YAML, or key names plus the function to fix in `src/`).
  Keep names configurable in `config.py`, never hard-code them elsewhere,
  and keep parsing tolerant of missing or unknown fields. Never put example
  values that look real (hosts, apps, sites) in `config.example.yaml`.
- A failing NetIM metric or alerts call, or an optional Aternity source
  (health events, app UXI), must not abort an investigation.
  It goes into `netim.warnings` / `aternity.warnings`, which `investigate()` copies into the result.
  A read that failed or was cut short is never "nothing found" (review of
  Nov 15 2026: "Only this PC", "nothing changed" and "nothing wrong on the
  network" were said of reads that had failed): callers compare the
  warnings' count before and after (`_optional` adds one per failure,
  logged once; `cause._Once` keeps `short`, `cause._short`,
  `cause._read_changes`, `normal._said`, `logons._said`,
  `software._partial`), and say it: who else "not known" / "at least N"
  with no verdict, "its change log could not be read in full" (class D
  title too), Other users not compared (`impact` `partial`; a failed
  window or day is a warning), "Network: not checked" / "not fully
  checked" (`answer` `net_unread`), "these counts are partial"
  (`software.CUT`). The second review ("REVIEW ALL", Nov 15) found more of
  the same, each said now: "What was checked" counts per source the reads
  that failed (`checked["failed"]`: "not read in full (see Warnings)");
  the answer's reason "Not all of it could be read: N warnings"
  (`answer.not_all`; class H title unchanged); Other users with every day
  before unread (`impact` `days_unread`); a health day before cut short is
  left out of the usual (`Today.health_cut`, a note on the tab; it had
  counted as a quiet day); the chats' and Ask anything's event look-ups
  say it (`chat._health_notes`, not "not read yet"), their activity
  look-ups name hours cut short or a failed read (`Today.activity_gaps`:
  "No user X had a measured action today" was said of an hour not read);
  logins with nothing read say "not known" (it said "recorded no logon");
  the Start-ups tab and the CLI `logons` report with nothing read show the
  warnings (they said "No boots or logons"); a software "No PC …" from a
  cut-short read is "in what could be read" (`software_chat._of_read`),
  a daily read's own cut short is said in version_problems; the
  assessment's Yesterday (written once) waits for its health events until
  `assess.UNREAD_WAIT_H` (noon), then says they were not read or cut short
  (it said "No crash or event" for good). NetIM lists stop when NetIM does
  not move on (a next_offset not past this one, the same page again) and
  after `netim.MAX_PAGES`, a warning: either read for ever. verify: each
  part is guarded (`verify.part`: one error printed nothing at all, as
  every result is printed at the end), a part's reads cut short or failed
  are a "…: not read in full" line, a check's own row limit is said
  (`_limit`, `_first`), days are in config.yaml's timezone (`set_zone` in
  `cmd_verify`), every group is printed (`render_checks` left out
  config.yaml's notes, counted as problems), an empty model list is not a
  FAIL (as the Connections light). An investigation reads up to `correlate.MAX_PCS` (8)
  of a user's PCs, those with a slow action first, then the most used; a
  warning names the ones left out. A reply that is not JSON is an
  `ApiError` ("the reply was not JSON"); the page says a source that did
  not answer in our words (`web._did_not_answer`), and only what was asked
  or config.yaml is a 400 (any ValueError inside an investigation was).
- Aternity writes "N/A" for no value: `AternityClient.query` turns it into
  None (verify reads `raw=True` to show values as sent). A module named
  "unknown (Dll)" / version "0.0.0.0" is no module. App hang time
  (APPLICATION_HANG_TIME_TOTAL) has no documented unit: never print one.
- Every timestamp is a timezone-aware UTC datetime. Use `models.parse_time`,
  which accepts ISO strings and epoch seconds or milliseconds; a value that
  is no time is None (the row is skipped: it raised, and one odd value
  stopped the whole read); the CLI says a typed one (`cli._time_arg`).
  Days are compared by the local clock (`Today.clock_secs`; elapsed
  seconds since midnight were an hour off on Nov 1 2026, 25 hours), a day
  ends at its own midnight (`midnight(d + 1 day)`, never 24 h), and Other
  users' days before are the same clock hours (`impact`: a 24-hour step in
  UTC put 09:00 against 10:00). Days and
  times written in sentences, and day groupings, are in config.yaml's
  `timezone` (`models.day`, `clock`, `local_date`; `set_zone` per
  investigation), as the page's lists: a UTC day once put a 10:37 PM
  change on the next day (live, Nov 2026).
- Role → domain mapping lives in `ROLE_DOMAIN`. When a device is mapped twice,
  it keeps its first role (for example, an app server stays `app-server` even
  if its subnet also matches).
- The `investigate()` result dict is the contract for the CLI, the REST API
  and the UI. If you change its keys, update `report.py`, `static/index.html`
  and the tests together.
- Docker: the image is runtime only (Python + pyproject dependencies); the
  code is mounted from `./src`, so code fixes need only a restart. Keep code
  out of the image. The live host is air-gapped: it loads the image from the
  bundle and never builds; bump `x-image` in `compose.yaml` when
  dependencies change. Base is the official `python:3.13-alpine`, two
  stages into `FROM scratch`, no pip; only pyproject dependencies, every
  version pinned in `constraints.txt` (a rebuild once pulled whatever was
  newest). Image 4 (2026-11-15, the user: "a new image with AD
  users/computers lookup tools") = image 3's libraries + ldap3, pyasn1,
  dnspython (domain controllers from DNS SRV records) and pycryptodome
  (MD4 for ldap3's NTLM login: OpenSSL 3 has none; its SelfTest folder
  is deleted); bundle 16 MB. `ad.py` (verify --ad) uses ldap3 and
  dnspython; pycryptodome is ldap3's MD4 for NTLM.
- Kubernetes: `k8s/uxcorrelate.yaml` (one pod, `Recreate`; code, data and
  config.yaml on an NFS volume as with compose; Secret from `.env`). The
  pod serves 8080 and 8444 (TLS, Secret `uxcorrelate-tls` at /certs) as
  compose does; the way in is its own file, switched by apply + delete
  with no edit (the user, 2026-11-16: "switch back and forth between
  ingress and lb"): `k8s/ingress.yaml` (TLS at the ingress, to 8080) or
  `k8s/loadbalancer.yaml` (`uxcorrelate-lb`, LoadBalancer 8444 to 8444,
  externalTrafficPolicy Local), both at once allowed. The TLS port's
  uvicorn Config takes forwarded headers from 127.0.0.1 only (`cli.py`):
  with "*" a browser could claim any address, or HTTPS on plain http and
  log in to /admin in the clear. Keep it in step with `compose.yaml`: stage runs on Docker
  compose, prod on Kubernetes (the user, 2026-11-13); every change works
  on both. Its checks ask `/api/health` (no config read, left out of the
  log: `cli.QUIET`; Docker's HEALTHCHECK still asks for "/" from inside),
  and `FORWARDED_ALLOW_IPS` "*" makes the caller the user's address from
  the ingress's X-Forwarded-For on 8080 (the admin page, Care list change
  and PIN log lines); compose leaves it unset (the direct caller).
- The bundle (`dist/uxcorrelate-<version>.tar`) must stay under 25 MB
  (prod transfer limit; the script refuses bigger). It is made only when
  every test passes. Adding a dependency means checking that size.
- No Wi-Fi data: the user doesn't use it (removed 2026-10-29). Don't add
  it back.
- TLS certificates are deliberately not checked (`verify=False` in
  `http.py`; Riverbed hosts use self-signed certs). Don't add a
  `verify_tls` setting back. (Serving the UI over HTTPS is separate:
  `serve --tls-port/--cert/--key`, both ports in one process with one
  stop for both; certs in `./certs`, gitignored; host 8444, as haproxy
  holds 8443.)
- The brand is "J6 Preservation Society" (parchment, archive green,
  brass; colours only through the `:root` variables, serif from fonts
  already on Windows: the live host has no internet). The title after it
  is `title` in config.yaml (the user's pick, 2026-11-13: the site's name
  stays out of git), put in by `web.page()` at each load (`<!--title-->`,
  `<title>`; empty: the brand alone). The top bar (proposal B + folder
  tabs, the user's picks): crest, brand | title, lights at the right;
  then the tabs in capitals, the open one parchment, joined to the page.
  On a phone the tabs slide sideways. The lights show `ai.services`'
  `name` as written; `ai.chat` matches it whatever the capitals.
- The web UI has no authentication. Don't add features that assume it does.
  Only `/admin` has a login (its own password; the main page stays open).
- Keep secrets out of `config.yaml` (it is gitignored). Pass them as env vars.
