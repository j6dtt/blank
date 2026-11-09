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
```

Before and after any change to what an investigation says: run
`scripts/snapshot.sh` into two folders and `diff -r` them; every changed
line must be one you meant (`tests/conftest.py` does the saving).
`tests/test_real_cases.py` rebuilds cases seen on prod (made-up names):
render one on dev and look at the whole page before prod.

Without installing: `PYTHONPATH=src python3 -m uxcorrelate.cli ...`.
The dev host has no Riverbed and no pytest: tests run in the runtime
image (`make-bundle.sh` shows how), and `verify` there only reports the
unset `<REQUIRED>` base URLs.

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
  No scores: the answer is plain counts. The data sources are the
  `AternitySource`/`NetimSource` protocols.
- `logons.py`: slow start-ups (`WINDOWS_MACHINE_BOOTS`,
  `MACHINE_BOOT_COMPONENTS`). Rows of one device within an hour, no type
  twice, are one start-up (one row, a column per Aternity type). Per type:
  typical and 90th percentile; per row: the device's own 30-day usual and
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
  Used by "What the user felt", the "Slow start-ups" tab
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
  unusual group holding more than half the slow users). Virtual desktop
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
  "possible cause" only with the same name or version as the failing part,
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
  other slow app + activity is an "Also slow" line in the reason; PCs
  are named ("Other things on VD-41"), never "these PCs". The question is
  what went wrong for the user, not only what was slow: with no slow action
  the title counts hangs, app crashes and system events (`level` "issues",
  background crashes in the reason). Each problem has an `issue` kind
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
  activity (those two columns only), the last 24 hours at start, then each
  hour; dropped after 8 days unseen. One background read per process (the
  HTTP and HTTPS servers share it). Any name can still be typed.
- `ai.py`: client for the self-hosted AI services (`ai.services` in
  config.yaml, key from the env var named in `key_env`) the chat will
  use; the chat is not built yet. The AI never reads Aternity or counts:
  it picks a building block (`COUNT_TOOL`, provisional), our code counts.
  OpenAI-style API is assumed until `verify --ai` has run on prod
  (`verify.verify_ai`: key, reachable, login header, plain reply, right
  pick in 3 tries + no pick outside the data, counts repeated right).
- `verify --overview` (`verify._overview`, also at the end of a full verify):
  what a "today's issues" overview could rest on: activity rows an hour,
  Aternity's own status and severity words with counts, and whether its
  incidents and alerts (`OVERVIEW_SOURCES` in `config.py`) are published.
  The overview and chat page are not built yet.
- `http.py` is the shared REST client (basic auth, retries on 429/5xx);
  `models.py` holds the dataclasses and `parse_time`.
- `report.py` renders the text report, `cli.py` is the argparse entry point,
  and `web.py` is the FastAPI app.
- `static/index.html` is the single-page UI. It is dependency-free and draws
  its chart with inline SVG.
- `tests/`: pytest. HTTP is mocked with `httpx.MockTransport`; the engine is
  tested with `tests/synthetic.py` (fake Aternity/NetIM, branch WAN
  saturation scenario) and stubs. There is no demo mode in the product; don't
  add one back.

## Conventions and gotchas

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
- Aternity writes "N/A" for no value: `AternityClient.query` turns it into
  None (verify reads `raw=True` to show values as sent). A module named
  "unknown (Dll)" / version "0.0.0.0" is no module. App hang time
  (APPLICATION_HANG_TIME_TOTAL) has no documented unit: never print one.
- Every timestamp is a timezone-aware UTC datetime. Use `models.parse_time`,
  which accepts ISO strings and epoch seconds or milliseconds.
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
  stages into `FROM scratch`, no pip; only pyproject dependencies.
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
- The page is titled "J6 Preservation Society" (parchment, archive green,
  brass; colours only through the `:root` variables, serif from fonts
  already on Windows: the live host has no internet).
- The web UI has no authentication. Don't add features that assume it does.
- Keep secrets out of `config.yaml` (it is gitignored). Pass them as env vars.
