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
```

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
  an "Uninstalled" + "Installed" row of the same software at the same
  second = one update), and main causes =
  that part longer than normal on every PC (`boot_component_everywhere`).
  No "slow" threshold, no assumed type names. Unit
  from `boot_unit` (inferred as seconds from live values, not documented).
  Used by "What the user felt", the "Slow start-ups" tab
  (`/api/logon-jobs`) and the `logons` CLI command.
- `impact.py`: "who else" — other users of the same apps in the user's slow
  intervals, grouped by the NetIM gateway (or /24) they sit behind and
  compared with the same hours on previous days ("unusual" = more slow
  than any of those days; common point = an unusual group holding more
  than half the slow users). This NetIM grouping is what Aternity can't do.
- `cause.py`: the main point of an investigation, "why". For the problems
  at the most slow actions: where the app failed (component, module,
  versions, error, from `DEVICE_HEALTH_RAW`), other PCs failing the same
  way, software changes in the 14 days before
  (`INSTALLED_SOFTWARE_CHANGE_LOG`), busiest processes for CPU/memory/disk
  (`RAW_DATA_TOP_PROCESSES`). "Same failure" = same category+name, same
  component and sub-component (version shown, not matched); see
  `docs/who-else-is-affected.md`. Every health event kind gets a why (an
  event with no app, e.g. a background process, low memory, a blue screen,
  counts at any slow action; who else is found by its process). Same
  failure also needs the same error code when both have one; the module
  only when Aternity knows it. Each finding is a sentence about a record;
  a source with nothing says so. A hang and its own health event share one
  cause.
- `answer.py`: the plain-language answer at the top of the page and the
  CLI report (title, what the user felt, who else is affected, what we found,
  what to do), plus `problem_text`, the sentence for each problem. Keep it
  free of scores and jargon. The page has one timeline and one problems
  list; don't add views that repeat them.
- `verify.py`: a missing optional source (`OPTIONAL`) is a note saying what
  is skipped, not a FAIL; a wrong name gets paste-ready YAML only when the
  real name is very close. It checks every configured Aternity data source/column (against
  OData `$metadata`), NetIM metric class/metric (against NetIM's list, then
  one real fetch) and topology name, and prints the closest real names.
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
  `verify_tls` setting back.
- The web UI has no authentication. Don't add features that assume it does.
- Keep secrets out of `config.yaml` (it is gitignored). Pass them as env vars.
