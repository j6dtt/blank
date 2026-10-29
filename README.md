# UX Correlator — Aternity × NetIM

Troubleshoot "the app is slow" tickets. The tool pulls a user's end-user
experience from **Riverbed Aternity** and infrastructure metrics from
**Riverbed NetIM** over their REST APIs. It lines both up on one timeline and
says whether the slowdown most likely came from the **client device**, the
**network**, or the **application backend**, along with the evidence for that
call.

Example report:

```
VERDICT: Network  (confidence 0.93)
  9 degraded interval(s); most likely cause: Network (score 0.93).

Domain scores (score | overlap with degraded intervals | Aternity time share):
  Client device        [....................] 0.02 | 0.00 | 0.04
  Network              [###################.] 0.93 | 1.00 | 0.85
  Application backend  [#...................] 0.05 | 0.00 | 0.10

Evidence during degraded intervals:
  100.0%  network  NetIM        den-br-rtr01:Gi0/1 (WAN-MPLS): Interface utilization out (%) worst=98.36 (threshold 80.0)
  100.0%  network  NetIM        den-br-rtr01:Gi0/1 (WAN-MPLS): Interface discards worst=811.85 (threshold 50.0)
   88.9%  network  NetIM alert  den-br-rtr01: major: Gi0/1 (WAN-MPLS) outbound utilization above 90%
```

## How it works

1. **User experience (Aternity OData API).** Gets the user's activities from
   `BUSINESS_ACTIVITIES_RAW`: response time and its client / network /
   backend split. Each time bucket (5 minutes by default) is flagged
   *degraded* when an activity is slower than the response-time threshold.
2. **Endpoint (Aternity).** The same activity rows carry the device's CPU,
   memory, disk queue, round-trip time, IP address and location at that
   moment. Device health events (crashes, hangs, errors) come from
   `DEVICE_HEALTH_RAW`, and each app's UXI, crashes and hang time from
   `APPLICATIONS_RAW`. Events, crashes and hangs count as client-device
   evidence; UXI is shown as Aternity reports it and does not change the
   verdict.
   **Who else.** For the user's slow intervals, the same application's
   activity for every other user is read (`$select` keeps it small). Most
   other users at the user's location slow means the site; most users
   elsewhere slow too means everyone (the application); otherwise only
   this user. The affected users are listed, and the result's cause line
   combines this with the strongest NetIM measurement.
   **Boots and logons.** The user's boots and logons in the window come
   from `WINDOWS_MACHINE_BOOTS`, each compared with the median of its type
   and with that device's own last 30 days, and the slowest ones list what
   slowed them (`MACHINE_BOOT_COMPONENTS`). The "Slow logons" tab and
   `uxcorrelate logons --last 24h [-l <location>]` search everyone.
3. **Path mapping (NetIM API).** Maps the user and the application onto NetIM
   objects:
   | role | how it is found |
   |---|---|
   | `client-gateway` | a NetIM L3 interface whose subnet contains the endpoint IP |
   | `site` | devices listed in `topology.sites` for the endpoint's Aternity location |
   | `app-server` | a NetIM device matching a server in `topology.applications` |
   | `app-server-network` | an L3 interface whose subnet contains the app server IP |
   | `always` | devices in `topology.always_include` |
4. **Infrastructure (NetIM).** Gets interface utilization and device CPU for
   those objects (add more metrics in the config; `verify` lists what your
   NetIM has). A sample over its threshold is an anomaly.
5. **Correlation.** Each domain gets a score from two parts:
   - how many degraded buckets have an anomaly in that domain, with a small
     penalty when the anomalies also show up while the user was fine;
   - the share of degraded response time that Aternity puts in that domain.

   The highest-scoring domain is the verdict. Below 0.25 the verdict is
   *inconclusive*.

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
  response time, CPU, memory, disk queue, round-trip time and UXI
  over the last day next to each threshold, so you can see the units match.
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

The response holds `summary` (verdict, confidence, headline), `scores` per
domain, `evidence`, `recommendations`, `endpoints`, `mapped_objects`, the
per-bucket `timeline` and `warnings`.

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
  correlate.py   mapping, bucketing, scoring
  verify.py      checks the config against live Aternity / NetIM
  report.py      text report
  cli.py         command line
  web.py         FastAPI app;  static/index.html  UI
tests/           pytest suite (HTTP mocked with httpx.MockTransport;
                 synthetic.py is a fake Aternity/NetIM for engine tests)
```

Run the tests with `pytest`.
