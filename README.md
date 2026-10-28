# UX Correlator — Aternity × NetIM

Troubleshoot "the app is slow" tickets. The tool pulls a user's end-user
experience from **Riverbed Aternity** and infrastructure metrics from
**Riverbed NetIM** over their REST APIs. It lines both up on one timeline and
says whether the slowdown most likely came from the **client device**, the
**network**, or the **application backend**, along with the evidence for that
call.

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

1. **User experience (Aternity OData API).** Gets the user's activity response
   times, UXI and the client / network / backend time breakdown for the
   window. Each time bucket (5 minutes by default) is flagged *degraded* when a
   sample goes over the response-time threshold or under the UXI threshold.
2. **Endpoint (Aternity).** Gets device health (CPU, memory, disk queue, Wi-Fi
   signal, network latency) and inventory (IP address, location).
3. **Path mapping (NetIM API).** Maps the user and the application onto NetIM
   objects:
   | role | how it is found |
   |---|---|
   | `client-gateway` | a NetIM L3 interface whose subnet contains the endpoint IP |
   | `site` | devices listed in `topology.sites` for the endpoint's Aternity location |
   | `app-server` | a NetIM device matching a server in `topology.applications` |
   | `app-server-network` | an L3 interface whose subnet contains the app server IP |
   | `always` | devices in `topology.always_include` |
4. **Infrastructure (NetIM).** Gets interface utilization, errors, discards and
   device CPU, memory and latency for those objects, plus active alerts. A
   sample over its threshold is an anomaly.
5. **Correlation.** Each domain gets a score from two parts:
   - how many degraded buckets have an anomaly in that domain, with a small
     penalty when the anomalies also show up while the user was fine;
   - the share of degraded response time that Aternity puts in that domain.

   The highest-scoring domain is the verdict. Below 0.25 the verdict is
   *inconclusive*.

## Quick start

```bash
pip install -e .            # or: pip install -e '.[dev]' for tests

# Try it with built-in synthetic data (no Riverbed systems needed)
uxcorrelate investigate --demo
uxcorrelate serve --demo    # web UI at http://127.0.0.1:8080
```

### Against your environment

```bash
cp config.example.yaml config.yaml          # edit URLs, thresholds, topology
export ATERNITY_PASSWORD=...  NETIM_PASSWORD=...

# 1. Confirm connectivity and the real field names
uxcorrelate -c config.yaml aternity-sample BUSINESS_ACTIVITIES_RAW --top 2
uxcorrelate -c config.yaml aternity-sample DEVICE_HEALTH_RAW --top 2
uxcorrelate -c config.yaml netim-check --metric-classes

# 2. Investigate
uxcorrelate -c config.yaml investigate -u jdoe -a "CRM Portal" --last 4h
uxcorrelate -c config.yaml investigate -d LT-JDOE-01 --start 2026-10-27T09:00 --end 2026-10-27T12:00 --json -o report.json

# 3. Web UI + REST API
uxcorrelate -c config.yaml serve --host 0.0.0.0 --port 8080
```

Note that `-c` comes before the sub-command.

## REST API

`uxcorrelate serve` serves the UI at `/` and interactive API docs at `/docs`.

```bash
curl -s localhost:8080/api/investigate -H 'Content-Type: application/json' -d '{
  "username": "jdoe", "application": "CRM Portal",
  "start": "2026-10-27T09:00:00Z", "end": "2026-10-27T12:00:00Z"}'
```

The response holds `summary` (verdict, confidence, headline), `scores` per
domain, `evidence`, `recommendations`, `endpoints`, `mapped_objects`, the
per-bucket `timeline` and `warnings`.

## Adapting to your Riverbed versions

Aternity column names and NetIM endpoint paths and metric names differ
between releases and deployments, so **all of them are configurable**:

- **Aternity**: `aternity.entities` and `aternity.fields` map logical fields
  to OData entities and columns. Columns that are missing are skipped, not
  treated as errors. The client authenticates with basic auth (user name +
  REST API access token), follows `@odata.nextLink`, and filters on
  `TIMEFRAME`.
- **NetIM**: devices and interfaces come from `{api_prefix}/devices` and
  `/devices/{id}/interfaces` (paged with `offset`/`limit`). Metrics come from a
  `POST` to `metric_data_path` with `objectType`, `objectIds`, `metricClass`,
  `metrics`, `startTime` and `endTime` (epoch ms). The parser accepts flat
  (one point per item) and nested (series per object) responses. If a metric
  or the alerts endpoint fails, the run continues and a warning is added to
  the report. Check the exact paths in your NetIM's `https://<netim>:8543/api-docs`.

To add another data source (for example NetProfiler or AppResponse), implement
the small `AternitySource` / `NetimSource` protocols in
`src/uxcorrelate/correlate.py`.

## Layout

```
src/uxcorrelate/
  aternity.py    Aternity OData client
  netim.py       NetIM REST client + tolerant metric parsing
  correlate.py   mapping, bucketing, scoring
  demo.py        synthetic scenario (branch WAN saturation)
  report.py      text report
  cli.py         command line
  web.py         FastAPI app;  static/index.html  UI
tests/           pytest suite (HTTP mocked with httpx.MockTransport)
```

Run the tests with `pytest`.
