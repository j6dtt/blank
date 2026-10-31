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

Who else is affected?
  20 of 50 other CRM Portal users were slow (40%), more than on any of
  the last 3 days (at most 0%). 20 of the 20 slow users share one network
  point: den-br-rtr01 Gi0/0 (LAN) (10.20.30.0/24).
```

## How it works

1. **Slow actions (Aternity).** The user's activities come from
   `BUSINESS_ACTIVITIES_RAW`. An activity is slow when Aternity's own
   status for it (`SLA_STATUS`) is one of `thresholds.slow_status`.
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
   The answer names the problem at the most slow actions: "likely" when
   that is more than half of them, "possibly" when fewer. No scores.
6. **Why (the main point).** For the problems at the most slow actions,
   the tool looks for what caused them, from Aternity's own records:
   - where the application failed: the component and the module inside
     it, their versions and the error (`DEVICE_HEALTH_RAW`);
   - whether other PCs fail the same way, and on which versions;
   - what was installed, updated or removed on the PC in the 14 days
     before (`INSTALLED_SOFTWARE_CHANGE_LOG`);
   - for CPU, memory or disk problems, the busiest processes at those
     times (`RAW_DATA_TOP_PROCESSES`).

   Each is a plain sentence under the finding; when a source has nothing,
   it says so.
7. **Who else.** The same applications' activity for every other user at
   the same times, grouped by the NetIM gateway (or /24) each user sits
   behind and compared with the same hours on the 3 previous days.
8. **Start-ups.** The user's boots and logons (`WINDOWS_MACHINE_BOOTS`):
   Aternity's measurements of one PC within an hour form one start-up,
   shown next to that PC's own last 30 days, with what took the time
   (`MACHINE_BOOT_COMPONENTS`). For the slowest, the cause is the part
   Aternity measured as most "longer than normal", plus what changed on
   the PC before; the main causes list every PC where that part was
   longer than normal. The "Slow start-ups" tab and
   `uxcorrelate logons --last 24h [-l <location>]` search everyone.

## The web page

Investigate tab, top to bottom:

- **Answer**: the likely cause in one line, and at how many slow actions.
- **What the user felt**: slow actions per app, the slowest one, the time
  split Aternity measured, the slowest start-up.
- **Who else is affected?** (see `docs/who-else-is-affected.md`) For an app failure: the other PCs with the same
  failure ("Show who" lists PC, user, version, times, last seen). For a
  network problem: the other slow users and the network point they share.
- **What we found, and why**: each problem at the slow actions, with what
  caused it underneath.
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

**Web UI.** Open `http://<docker-host>:8080`. The `ports` setting in
`compose.yaml` decides who can reach it: `["127.0.0.1:8080:8080"]` is the
Docker host only, `["8080:8080"]` is the whole network. The UI has no login.

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
- **A reply it cannot read** (device addresses, interface addresses, metric
  data): it prints the reply's key names, never values, and which function
  in `./src/uxcorrelate/netim.py` to fix. Edit it there and run
  `docker compose restart`; then bring the same fix to dev by hand.

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
`slow_actions` of `of_slow`), `impact` (who else), `timeline` (`actions`
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
  verify.py      checks the config against live Aternity / NetIM
  report.py      text report
  cli.py         command line
  web.py         FastAPI app;  static/index.html  UI
tests/           pytest suite (HTTP mocked with httpx.MockTransport;
                 synthetic.py is a fake Aternity/NetIM for engine tests)
```

Run the tests with `pytest`.
