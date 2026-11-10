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

   If nothing maps, the answer says the network was **not checked**.
4. **Problems on the network or servers (NetIM).** Interface utilization
   and device CPU of those objects (add metrics in the config; `verify`
   lists what NetIM has); a value over its limit is a problem, and so is
   a NetIM alert.
5. **Matching.** A problem counts for a slow action recorded up to
   `bucket_minutes` (5) before or after it. A crash, hang or health event
   of one application counts only for that application's slow actions.
   A failing disk is the PC's state, not a moment: when Aternity labels a
   health event's failing part "(Disk)" or "(Drive)", the PC's disk counts
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
   longer than normal. The "Slow start-ups" tab and
   `uxcorrelate logons --last 24h [-l <location>]` search everyone.

## The web page

Today tab (opens first): what Aternity itself rated today, from midnight
in `timezone` (config.yaml). Tiles for crashes and events Aternity rates
Critical or Major and for its Major slow actions, each against the same
kind of day before ("More than usual", "As usual", "Fewer than usual");
Major slow actions per hour and per application; other crashes and events
on the most PCs, each with its 7 days before. It updates every 15 minutes
(the open tab fetches every 5).
Click a tile ("Show who"), an application row or an event row for the
users and PCs behind it, most first (50 at most): a slow action list
gives each one's slowest action, an event list how many times. Their
"Investigate ▸" opens the Investigate tab and runs it at once for that
user and PC, from 30 minutes before their first one to 30 minutes after
the last (an hour each side for events; for an application, only that
application). "Ask about today" (at the bottom; "Ask a question ▾" at
the top jumps there): type a question, or click an example. The AI
picks what to count and words a sentence; this app does the counting, and
the counts are shown under the sentence. A sentence with a number that is
not in the counts is replaced by the app's own. It answers about Major
slow actions (by application, hour, activity, day, virtual or physical),
crashes and events (today and the 14 days before), who had them today,
and "Investigate <user or PC>" (a button that runs it). Earlier questions
are remembered for follow-ups until "Clear the answers" or a reload.
The usual for actions builds up over the first week from the counts saved
in `./data` each night (`mkdir -p data && chmod a+rwx data` before the
first start).

Care list tab (second): up to 50 users, each checked every 15 minutes.
"+ Add a user" (top right of the tab) opens a box: the username as
Aternity writes it (names seen in Aternity are offered as you type) and
an optional label shown big on the tile; the tile is there at once and
fills in within seconds. Click a tile, then "Remove from the Care
list…": after a confirm, the tile, the user's saved issues and their
latest investigation are deleted. The list is `data/care_list.json`
(`[{"username": …, "label": …}]`; it can also be edited by hand, seen at
the next check). It is made once from `care_list` in config.yaml, where
the list was before; config.yaml's list is not read after that. The page
has no login, so anyone who can open it can change the list; each change
is logged with the address it came from.
An issue is an event Aternity rates Critical or Major, or more Major slow
actions by this time of day than on any of the user's own days before.
Either is over after an hour with no new one (slow actions also once the
user is back within their usual by that time of day); a later one is a new
issue. The tile keeps a line saying when it was and how it ended. Every user has a tile:
those with an issue first (Critical first), then names not seen in 8 days,
then green "✓ OK" tiles (actions today, how many Major, their usual by
now, and any slow spell that cleared). A new issue runs an investigation
by itself (at most one an hour per user) and its headline is the tile's
"Why". Click a tile: today's issues, the investigation ("Open the
investigation ▸" shows it on the Investigate tab without running it again;
"Investigate again now ▸" runs a new one) and the issues of the 7 days
before. The tab's
red count is the users with an issue now; `…/#care` opens the tab.
Issues are kept 8 days in `./data/care` (with usernames).

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
  so "nothing found" says what is ruled out.
- **What to do**: steps from those causes.
- **Timeline**: every action (red = slow) and every problem (faded = not
  near a slow action). Hover for date, time and value.
- **Problems found**: the problems at slow actions, with "at N of M";
  "Show N problem(s) at other times" opens the rest.
- **Details for IT**: the rules used, the PCs, the NetIM objects checked.

Slow start-ups tab: searches the Start/End range in its form (last 24
hours by default; empty user, PC and location = everyone). One card per
Aternity measurement type (typical, 1 in 10 slower than); **Main causes**:
the parts behind the slowest start-ups and every PC where they were longer
than normal ("Show who"); one row per start-up with a column per type
(click a row for what took the time and why); a table by location.

Connection lights (right of the tabs, on every tab): one per API and AI
model: Aternity, NetIM and each AI service in `ai.services`. Green =
connected, a flashing red "… down" = no answer or the login was refused,
amber = the service answers but does not list the model set, grey = not
set. Checked every minute with one small read each. Click any light for
the "Connections" panel: since when, the reply time, and which model the
chats use. Changes are written to the log ("Connection to NetIM: down.").

Software tab (fifth, `…/#software`): red flags on installed software and
application versions, as Aternity lists them, read once a day (and at
start; the first read takes a few minutes). The tab's count is the red
flags. Top to bottom:
- **Red flags**, most PCs first, each with its PCs per day and "new" when
  it was not there the day before: a version that crashes, hangs or is
  slow more than its others (at least 2 times their share, 20 PCs on each
  side, 5 with the problem), programs behind most PCs, 2+ versions on one
  PC, a Windows update most PCs of a Windows version have but not every
  one, programs installed for one user, programs on 5 PCs or fewer. Click
  a line to go to its chart. **Summary for management ▸** opens a short
  summary (GPT, every number checked; else our own sentences) with Copy.
- **Crashes, hangs and slow actions by version**: per application, each
  version's share of PCs (or actions) with the problem, its other
  versions' as a dashed line. Click a bar for those PCs (read now), each
  with Investigate ▸ for its last day with it.
- **Compare versions**: two versions of an application on every kind of
  issue (crash, hang time, its health events, Major actions per activity,
  virtual desktops); **Before and after an update ▸**: the PCs whose
  version changed once in 14 days, days with a crash or hang time before
  and after.
- **Behind most PCs** (version strips, oldest to newest), **2+ versions on
  one PC**, **Installed for one user**, **Windows** (per version, the week
  it was last updated, updates not on every PC and what those PCs have;
  "not on N ▸" lists those PCs, read now, the ones with nothing in its
  place first), **Programs by how many PCs have them** (click a column), **A program**
  (its versions; click one for its PCs, read now).
- **Ask about software** (bottom; "Ask a question ▾" beside the summary
  jumps there): as on the Today tab, GPT picks what to count and words
  the answer, our counts are shown under it, and a sentence with a number
  not in them is replaced by ours. It also answers what the charts do
  not: an application's crashes split by the version of another program
  ("Do Outlook crashes depend on the Teams Meeting Add-in version?"; never
  a red flag), which PCs have a version ("Which PCs still run Edge 143?")
  or lack a Windows update ("Which PCs are missing KB…?"), each with
  Investigate ▸.
Each chart has "Table ▾" with its numbers. Counts and names are saved in
`$UXCORRELATE_DATA/software/<date>.json` (no PC or user names), 90 days.

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
`pyproject.toml` change. The settings block at the top of `compose.yaml`
lists what to do before the first start. Healthy means the web server is up; it
does not mean Aternity or NetIM are reachable (`verify` checks that).

**Web UI** (titled "J6 Preservation Society"). Open `http://<docker-host>:8080`
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
and `config.yaml` in the app folder on NFS, passwords in a Secret made from
`.env`, and TLS at the ingress. The top of the file has the full steps:
what to fill in, first time, day to day (code fix, passwords, certificate,
logs, verify) and going back to compose. One pod only: a second would read
Aternity twice.

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
  web.py         FastAPI app;  static/index.html  UI
tests/           pytest suite (HTTP mocked with httpx.MockTransport;
                 synthetic.py is a fake Aternity/NetIM for engine tests)
```

Run the tests with `pytest`.
