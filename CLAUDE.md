# CLAUDE.md

UX Correlator: a Python tool that correlates Riverbed Aternity end-user
experience with Riverbed NetIM infrastructure metrics (both via REST) and
attributes a user's slowdown to the client device, network, or application
backend. See README.md for user-facing docs.

## Commands

```bash
pip install -e '.[dev]'                 # install with pytest
pytest                                  # run tests (no network needed)
uxcorrelate investigate --demo          # CLI on synthetic data
uxcorrelate serve --demo --port 8080    # web UI + REST API on synthetic data
uxcorrelate -c config.yaml <command>    # -c goes BEFORE the sub-command
```

Without installing: `PYTHONPATH=src python3 -m uxcorrelate.cli ...`.

## Layout

- `src/uxcorrelate/config.py`: dataclass settings loaded from YAML and
  overridden by env vars (`ATERNITY_*`, `NETIM_*`). Also holds the default
  Aternity column map and NetIM metric list.
- `aternity.py`: Aternity OData client. It filters on the timestamp column and
  follows `@odata.nextLink`.
- `netim.py`: NetIM client (devices, interfaces, metric data, alerts).
  `parse_metric_response` accepts several response shapes.
- `correlate.py`: the core. It maps endpoints and apps to NetIM objects
  (roles), groups samples into time buckets, flags anomalies, and scores the
  domains. The data sources are the `AternitySource`/`NetimSource` protocols.
- `demo.py`: deterministic synthetic sources (branch WAN saturation scenario)
  used by `--demo` and by the tests.
- `report.py` renders the text report, `cli.py` is the argparse entry point,
  and `web.py` is the FastAPI app.
- `static/index.html` is the single-page UI. It is dependency-free and draws
  its chart with inline SVG.
- `tests/`: pytest. HTTP is mocked with `httpx.MockTransport`; the engine is
  tested with the demo sources and stubs.

## Conventions and gotchas

- **Riverbed field names and paths are unverified defaults.** Aternity
  columns, NetIM endpoint paths and NetIM metric class/names differ by version.
  Keep them configurable in `config.py` and never hard-code them elsewhere.
  Parsing must tolerate missing or unknown fields.
- A failing NetIM metric or alerts call must not abort an investigation. It
  goes into `netim.warnings`, which `investigate()` copies into the result and
  then clears.
- Every timestamp is a timezone-aware UTC datetime. Use `models.parse_time`,
  which accepts ISO strings and epoch seconds or milliseconds.
- Role → domain mapping lives in `ROLE_DOMAIN`. When a device is mapped twice,
  it keeps its first role (for example, an app server stays `app-server` even
  if its subnet also matches).
- The `investigate()` result dict is the contract for the CLI, the REST API
  and the UI. If you change its keys, update `report.py`, `static/index.html`
  and the tests together.
- The web UI has no authentication. Don't add features that assume it does.
- Keep secrets out of `config.yaml` (it is gitignored). Pass them as env vars.
