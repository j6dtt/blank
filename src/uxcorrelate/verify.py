"""Check a live configuration against what Aternity and NetIM actually report.

Runs on the live host and needs nothing back from it: every result is on
screen, and every FAIL says what to change there.

- Aternity: every configured data source and column is looked up in the
  tenant's own OData `$metadata`; topology names are looked up in recent data;
  numeric columns are sampled so units can be compared with the thresholds.
- NetIM: devices, interfaces, every configured metric class and metric, and
  one real metric fetch each. When a response cannot be read, its key names
  (never values) are printed so the parser in src/ can be fixed on the spot.
"""

from __future__ import annotations

import difflib
from bisect import bisect_right
import textwrap
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from .aternity import AternityClient, is_virtual, odata_quote, odata_time
from .config import (DISK_FREE_COLUMNS, DISK_PARTS, OS_COLUMNS, OVERVIEW_SOURCES, PROCESS_COLUMN, SOFTWARE_COLUMNS,
                     SOFTWARE_OS_UPDATE,
                     SOFTWARE_REMOVED, SOFTWARE_SOURCE, SOFTWARE_USER_SCOPE, SOFTWARE_UPDATE_GAP_MIN, TIME_COLUMNS,
                     NO_VALUE, TOP_STATUS, VDI_COLUMNS, VERSION_OPTIONAL, VERSION_SOURCES, Settings)
from .after_start import AFTER_START_MIN  # "right after a start-up", as investigations
from .correlate import slow_rule
from .http import ApiError
from .models import parse_time, part_label, to_float
from .netim import NetimClient, _items

CODE_FIX = "edit {file} in ./src/uxcorrelate, then: docker compose restart"


@dataclass
class Check:
    system: str
    item: str
    ok: bool | None  # None = information only
    message: str
    fix: str = ""
    paste: str = ""  # YAML to put in config.yaml
    rows: list[str] = field(default_factory=list)  # sample rows, one per line


def _closest(name: str, choices) -> list[str]:
    return difflib.get_close_matches(name, list(choices), n=3, cutoff=0.5)


def _very_close(name: str, hit: str) -> bool:
    """Close enough to paste as the fix (not just to show as a hint)."""
    return difflib.SequenceMatcher(None, name, hit).ratio() >= 0.8


def _closest_text(hits: list[str]) -> str:
    return f" Closest names: {', '.join(hits)}." if hits else ""


def _yaml(path: list[str], value: str) -> str:
    """Nested YAML for one setting, e.g. aternity > fields > activities > cpu."""
    return "\n".join(f"{'  ' * i}{k}:" for i, k in enumerate(path[:-1])) + \
        f"\n{'  ' * (len(path) - 1)}{path[-1]}: {value}"


def _keys(data: Any) -> str:
    if isinstance(data, list):
        data = data[0] if data else {}
    return ", ".join(sorted(data)) if isinstance(data, dict) else type(data).__name__


# ---------------------------------------------------------------- Aternity
def parse_odata_metadata(xml_text: str) -> dict[str, set[str]]:
    """Return {entity set name: {property names}} from an OData $metadata document."""
    root = ET.fromstring(xml_text)
    types: dict[str, set[str]] = {}
    for el in root.iter():
        if el.tag.endswith("}EntityType") or el.tag == "EntityType":
            props = {p.get("Name") for p in el if p.tag.endswith("Property") and p.get("Name")}
            types[el.get("Name")] = props
    sets: dict[str, set[str]] = {}
    for el in root.iter():
        if el.tag.endswith("}EntitySet") or el.tag == "EntitySet":
            type_name = (el.get("EntityType") or "").rsplit(".", 1)[-1]
            sets[el.get("Name")] = types.get(type_name, set())
    return sets


# data sources the investigation can do without: what is skipped when one is missing
OPTIONAL = {
    "health_events": "crash/hang details (where an app failed, who else has the same failure)",
    "app_usage": "app crashes, hang time and UXI",
    "boots": "the Slow start-ups tab",
    "boot_components": "what took the time in a start-up, and its main causes",
    "software_changes": "what changed on the PC before a problem",
    "top_processes": "the busiest processes behind CPU, memory or disk problems",
    "disk_space": "nearly full disks",
    "remote_display": "the delay a virtual desktop user feels (remote display latency)",
}


def check_schema(settings: Settings, schema: dict[str, set[str]]) -> list[Check]:
    """Every configured Aternity data source and column against a $metadata schema."""
    cfg = settings.aternity
    checks = []
    for key, entity in cfg.entities.items():
        if entity not in schema:
            hits = _closest(entity, schema)
            known = _closest_text(hits) or (f" Published: {', '.join(sorted(schema))}." if len(schema) <= 20 else "")
            paste = _yaml(["aternity", "entities", key], hits[0]) if hits and _very_close(entity, hits[0]) else ""
            if key in OPTIONAL and not paste:
                known += f" Not published here, so this is skipped: {OPTIONAL[key]}."
            checks.append(Check("Aternity", f"entities.{key}", False if paste or key not in OPTIONAL else None,
                                f"Data source '{entity}' does not exist.{known}", paste=paste))
            if key == "disk_space":
                checks += _disk_elsewhere(settings, schema)
            continue
        checks.append(Check("Aternity", f"entities.{key}", True, f"Data source '{entity}' exists."))
        columns = schema[entity]
        missing = False
        for logical, column in cfg.fields.get(key, {}).items():
            if not column:
                continue
            if column in columns:
                checks.append(Check("Aternity", f"fields.{key}.{logical}", True,
                                    f"Column '{column}' exists in {entity}."))
                continue
            missing = True
            hits = _closest(column, columns)
            paste = _yaml(["aternity", "fields", key, logical], hits[0]) if hits and _very_close(column, hits[0]) else ""
            checks.append(Check("Aternity", f"fields.{key}.{logical}", False,
                                f"Column '{column}' is not in {entity}.{_closest_text(hits)}"
                                + ("" if paste else " None of them is the same thing: don't use them."),
                                paste=paste))
        if missing and key == "disk_space":
            checks += _disk_elsewhere(settings, schema)
    return checks


def _disk_elsewhere(settings: Settings, schema: dict[str, set[str]]) -> list[Check]:
    """Another published data source with free disk space, as paste-ready YAML."""
    cfg = settings.aternity
    inv = cfg.entities.get("inventory")
    total = cfg.fields.get("inventory", {}).get("total_disk")
    has_total = inv in schema and total in schema[inv]
    for key in ("app_usage", "activities", "boots", "health_events", "boot_components", *cfg.entities):
        entity, f = cfg.entities.get(key), cfg.fields.get(key, {})
        if key == "disk_space" or entity not in schema or not f.get("timestamp") or not f.get("device"):
            continue
        cols = schema[entity]
        if f["timestamp"] not in cols or f["device"] not in cols:
            continue
        for logical, column in DISK_FREE_COLUMNS.items():
            if column in cols and (logical == "free_percent" or has_total):
                other = "free_space" if logical == "free_percent" else "free_percent"
                paste = (f"aternity:\n  entities:\n    disk_space: {entity}\n  fields:\n    disk_space:\n"
                         f"      timestamp: {f['timestamp']}\n      device: {f['device']}\n"
                         f"      {logical}: {column}\n      {other}: \"\"")
                return [Check("Aternity", "free disk space", False,
                              f"Free disk space is published in {entity} ({column}). Read it from there:",
                              paste=paste)]
    cols = set(DISK_FREE_COLUMNS.values())
    others = sorted(e for e, c in schema.items() if c & cols)
    if not others:
        return [Check("Aternity", "free disk space", None,
                      "No published data source has free disk space (" + ", ".join(sorted(cols))
                      + "), so nearly full disks are not checked.")]
    return [Check("Aternity", "free disk space", None,
                  "None of the data sources this tool reads has free disk space, so nearly full disks are "
                  "not checked. Published elsewhere (" + ", ".join(sorted(cols)) + "): " + ", ".join(others[:15])
                  + (f" (+{len(others) - 15} more)" if len(others) > 15 else "")
                  + ". To use one, its time and device columns are needed in src/uxcorrelate/config.py "
                  "(disk_space).")]


def _value_ranges(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """Min / median / max of numeric columns over the last day, next to their thresholds."""
    th = settings.thresholds
    limits = {
        ("activities", "response_time"): f"response_time_s {th.response_time_s}",
        ("activities", "cpu"): f"device_cpu {th.device_cpu}",
        ("activities", "memory"): f"device_memory {th.device_memory}",
        ("activities", "disk_queue"): f"disk_queue {th.disk_queue}",
        ("activities", "network_latency"): f"client_network_latency_ms {th.client_network_latency_ms}",
        ("disk_space", "free_percent"): f"free_disk_percent {th.free_disk_percent}",
        ("disk_space", "free_space"): f"free_disk_percent {th.free_disk_percent} of inventory.total_disk",
        ("app_usage", "uxi"): "none (UXI is shown as Aternity reports it)",
        ("boots", "duration"): "none (compared with medians; this shows the units)",
        ("top_processes", "cpu"): "none (shown as a percent, like the PC's CPU)",
        ("top_processes", "memory_bytes"): "none (shown in MB: bytes / 1048576)",
    }
    cfg = settings.aternity
    end = datetime.now(timezone.utc)
    checks = []
    for key in ("activities", "app_usage", "boots", "disk_space", "top_processes"):
        entity, fields = cfg.entities.get(key), cfg.fields.get(key, {})
        if entity not in schema or fields.get("timestamp") not in schema[entity]:
            continue
        flt = f"{fields['timestamp']} ge {odata_time(end - timedelta(days=1))}"
        try:
            rows = client.query(entity, flt, top=200)
        except ApiError as exc:
            checks.append(Check("Aternity", f"values in {entity}", False, f"Could not read rows: {exc}"))
            continue
        if not rows:
            checks.append(Check("Aternity", f"values in {entity}", None, "No rows in the last day."))
            continue
        kinds = Counter(str(r.get(fields.get("type", ""))) for r in rows if r.get(fields.get("type", "")))
        status = fields.get("status")
        if key == "activities" and status in schema[entity]:
            seen = Counter(str(r[status]) for r in rows if r.get(status))
            now = ", ".join(th.slow_status) or "not set (slow = over response_time_s)"
            checks.append(Check("Aternity", f"values {status}", None,
                                ("Activity status values seen: " + ", ".join(f"{k} ({n})" for k, n in seen.most_common())
                                 if seen else "No activity status values in the last day.")
                                + f". thresholds.slow_status: {now}."
                                + ("" if th.slow_status else " List the values that mean slow there.")))
        if key == "boots" and kinds:
            checks.append(Check("Aternity", f"values {fields['type']}", None,
                                "Measurement types seen: " + ", ".join(f"{k} ({n})" for k, n in kinds.most_common())
                                + ". Logon reports group by these names."))
        for (k, logical), limit in limits.items():
            column = fields.get(logical)
            if k != key or column not in schema[entity]:
                continue
            values = [v for v in (to_float(r.get(column)) for r in rows) if v is not None]
            if not values:
                checks.append(Check("Aternity", f"values {column}", None,
                                    f"No values in {len(rows)} rows of the last day: nothing is checked against "
                                    f"{limit}."))
            if values:
                checks.append(Check("Aternity", f"values {column}", None,
                                    f"min {min(values):g}, median {median(values):g}, max {max(values):g} "
                                    f"over {len(values)} rows; threshold {limit}. Check the units match."))
    return checks


def verify_aternity(settings: Settings, client: AternityClient) -> list[Check]:
    cfg = settings.aternity
    try:
        schema = parse_odata_metadata(client.http.request("GET", "/$metadata", headers={"Accept": "application/xml"}).text)
    except (ApiError, ET.ParseError) as exc:
        return [Check("Aternity", "connection", False, f"Could not read $metadata: {exc}",
                      "Check aternity.base_url, aternity.username and ATERNITY_PASSWORD "
                      "(the REST API access token).")]
    checks = [Check("Aternity", "connection", True,
                    f"Connected; the tenant publishes {len(schema)} data sources.")]
    checks += check_schema(settings, schema)

    # topology names must match Aternity spellings exactly
    act, f = cfg.entities.get("activities"), cfg.fields.get("activities", {})
    since = odata_time(datetime.now(timezone.utc) - timedelta(days=7))
    for kind, logical, names in (("Application", "application", settings.topology.applications),
                                 ("Location", "location", settings.topology.sites)):
        column = f.get(logical)
        if act not in schema or column not in schema[act]:
            continue
        for name in names:
            flt = f"{column} eq {odata_quote(name)} and {f['timestamp']} ge {since}"
            try:
                found = client.query(act, flt, top=1)
            except ApiError as exc:
                checks.append(Check("Aternity", f"topology: {name}", False, f"Lookup failed: {exc}"))
                continue
            checks.append(Check("Aternity", f"topology: {name}", bool(found),
                                f"{kind} '{name}' {'found' if found else 'not found'} in Aternity "
                                "activity of the last 7 days.",
                                "" if found else f"Spell it exactly as Aternity shows it ({column})."))
    return checks + _value_ranges(settings, client, schema) + _health_events(settings, client, schema) \
        + _app_usage(settings, client, schema) + _start_up_parts(settings, client, schema) \
        + _change_log(settings, client, schema) + _vdi(settings, client, schema) \
        + _remote_display(settings, client, schema) + _process_sources(client, schema) \
        + _fleet(settings, client, schema) + _overview(settings, client, schema)


def _raw(row: dict, fields: dict, keys) -> str:
    """One row as 'key=value' for the configured columns, values exactly as Aternity sent them."""
    return ", ".join(f"{k}={row.get(fields[k])!r}" for k in keys if fields.get(k))


def _filled(rows: list[dict], fields: dict, keys) -> str:
    """How many rows have a value (not empty, not N/A) in each configured column."""
    return ", ".join(f"{k} {sum(1 for r in rows if r.get(fields[k]) not in (None, '', 'N/A'))}"
                     for k in keys if fields.get(k))


def _cut(value, n: int = 150):
    return value[:n] if isinstance(value, str) else value


def _health_events(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """How Aternity fills its health events: which kinds exist (crash, hang, ...),
    and for each, which of the columns behind "where it failed" have values."""
    cfg = settings.aternity
    entity, f = cfg.entities.get("health_events"), cfg.fields.get("health_events", {})
    if entity not in schema or not f.get("timestamp"):
        return []
    since = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        rows = client.query(entity, f"{f['timestamp']} ge {odata_time(since)}", top=1000, raw=True)
    except ApiError as exc:
        return [Check("Aternity", "health events", None, f"Could not read them: {exc.reason or exc}")]
    kind = lambda r: f"{r.get(f.get('category', ''))} / {r.get(f.get('name', ''))}"  # noqa: E731
    kinds: dict[str, list] = {}
    for r in rows:
        kinds.setdefault(kind(r), []).append(r)
    where = ("application", "component", "component_version", "sub_component", "sub_component_version",
             "exception", "error", "details", "username")
    checks = [Check("Aternity", "health events", None,
                    f"{entity}, last 7 days: {len(rows)} row(s){' (first 1000)' if len(rows) == 1000 else ''}, "
                    f"{len(kinds)} kind(s) (category / name). Per kind: rows, then how many have each column filled:",
                    rows=[f"{k}: {len(rs)} row(s); {_filled(rs, f, where)}"
                          for k, rs in sorted(kinds.items(), key=lambda kv: -len(kv[1]))[:25]])]
    keys = ("timestamp", "application", "category", "name", "component", "component_version", "sub_component",
            "sub_component_version", "exception", "error")
    checks.append(Check("Aternity", "health events: one row per kind", None,
                        "Values exactly as Aternity sent them (details cut to 150 characters):",
                        rows=[_raw(rs[0], f, keys) + (f", details={_cut(rs[0].get(f['details']))!r}"
                                                       if f.get("details") else "")
                              for _, rs in sorted(kinds.items(), key=lambda kv: -len(kv[1]))[:12]]))
    return checks


def _app_usage(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """What the per-application crash count, hang time and UXI hold."""
    cfg = settings.aternity
    entity, f = cfg.entities.get("app_usage"), cfg.fields.get("app_usage", {})
    if entity not in schema or not f.get("timestamp"):
        return []
    since = datetime.now(timezone.utc) - timedelta(days=1)
    try:
        rows = client.query(entity, f"{f['timestamp']} ge {odata_time(since)}", top=1000, raw=True)
    except ApiError as exc:
        return [Check("Aternity", "application crashes, hang time, UXI", None, f"Could not read them: {exc.reason or exc}")]
    lines = []
    for k in ("crashes", "hang_time", "uxi"):
        if not f.get(k):
            continue
        vals = [v for v in (to_float(r.get(f[k])) for r in rows) if v is not None]
        nz = [v for v in vals if v]
        lines.append(f"{k} ({f[k]}): {len(vals)} row(s) with a value, {len(nz)} not zero"
                     + (f"; not zero: min {min(nz):g}, median {median(nz):g}, max {max(nz):g}" if nz else ""))
    hit = next((r for r in rows if any(to_float(r.get(f.get(k, ""))) for k in ("crashes", "hang_time"))), None)
    if hit:
        lines.append("A row with a crash or hang: " + _raw(hit, f, ("timestamp", "application", "crashes",
                                                                    "hang_time", "uxi")))
    return [Check("Aternity", "application crashes, hang time, UXI", None,
                  f"{entity}, last day: {len(rows)} row(s){' (first 1000)' if len(rows) == 1000 else ''}:",
                  rows=lines)]


def _start_up_parts(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """How Aternity fills the start-up parts: are there parts for the slowest
    start-up of the last day, and under which type names?"""
    cfg = settings.aternity
    be, ce = cfg.entities.get("boots"), cfg.entities.get("boot_components")
    bf, cf = cfg.fields.get("boots", {}), cfg.fields.get("boot_components", {})
    if be not in schema or ce not in schema or not bf.get("duration") or not cf.get("device"):
        return []
    end = datetime.now(timezone.utc)
    since = end - timedelta(days=1)
    try:
        boots = client.query(be, f"{bf['timestamp']} ge {odata_time(since)}", top=500, raw=True)
        parts = client.query(ce, f"{cf['timestamp']} ge {odata_time(since)}", top=500, raw=True)
    except ApiError as exc:
        return [Check("Aternity", "start-up parts", None, f"Could not read start-up rows: {exc.reason or exc}")]
    types = Counter(str(r.get(cf.get("type", ""))) for r in parts)
    slow = sum(1 for r in parts if (to_float(r.get(cf.get("degradation", ""))) or 0) > 0)
    checks = [Check("Aternity", "start-up parts", None,
                    f"{ce}, last day: {len(parts)} row(s){' (first 500)' if len(parts) == 500 else ''} on "
                    f"{len({r.get(cf['device']) for r in parts})} PC(s), {slow} longer than normal. Types: "
                    + (", ".join(f"{k} ({n})" for k, n in types.most_common()) or "none")
                    + ". Start-up types in " + be + ": "
                    + (", ".join(f"{k} ({n})" for k, n in Counter(str(r.get(bf.get("type", ""))) for r in boots)
                                 .most_common()) or "none") + ".")]
    timed = [r for r in boots if to_float(r.get(bf["duration"])) is not None]
    if not timed:
        return checks
    top = max(timed, key=lambda r: to_float(r.get(bf["duration"])))
    dev = str(top.get(bf["device"]))
    since = since - timedelta(hours=2)
    try:  # every row of that PC in the window, put in time order here
        mine_b = client.query(be, f"{bf['timestamp']} ge {odata_time(since)} and {bf['device']} eq {odata_quote(dev)}",
                              select=[bf[k] for k in ("timestamp", "started", "type", "duration") if bf.get(k)], raw=True)
        mine_c = client.query(ce, f"{cf['timestamp']} ge {odata_time(since)} and {cf['device']} eq {odata_quote(dev)}",
                              select=[cf[k] for k in ("timestamp", "type", "degradation") if cf.get(k)], raw=True)
    except ApiError as exc:
        checks.append(Check("Aternity", "slowest start-up and its parts", None,
                            f"Could not read that PC's rows: {exc.reason or exc}"))
        return checks
    order = lambda r, f: str(r.get(f["timestamp"]))  # noqa: E731
    checks.append(Check("Aternity", "slowest start-up's PC: its start-ups", None,
                        f"Slowest start-up read: {_raw(top, bf, ('timestamp', 'started', 'device', 'type', 'duration'))}. "
                        f"All {len(mine_b)} start-up row(s) of that PC since {odata_time(since)}, in time order:",
                        rows=[_raw(r, bf, ("timestamp", "started", "type", "duration"))
                              for r in sorted(mine_b, key=lambda r: order(r, bf))[:20]]))
    groups: dict[str, list] = {}
    for r in sorted(mine_c, key=lambda r: order(r, cf)):
        groups.setdefault(order(r, cf), []).append(r)
    checks.append(Check("Aternity", "slowest start-up's PC: its parts", None,
                        f"All {len(mine_c)} parts row(s) of that PC since {odata_time(since)}, grouped by "
                        f"{cf['timestamp']} ({len(groups)} different value(s)), in time order:",
                        rows=[f"{t}: {len(rs)} row(s), {sum(1 for r in rs if (to_float(r.get(cf.get('degradation', ''))) or 0) > 0)}"
                              " longer than normal; types: " + ", ".join(
                                  f"{k} ({n})" for k, n in Counter(str(r.get(cf.get("type", ""))) for r in rs).most_common())
                              for t, rs in list(groups.items())[:20]]))
    return checks


def _change_log(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """How Aternity fills the software change log: change types, and what the
    version columns hold for each."""
    cfg = settings.aternity
    entity, f = cfg.entities.get("software_changes"), cfg.fields.get("software_changes", {})
    if entity not in schema or not f.get("timestamp"):
        return []
    since = datetime.now(timezone.utc) - timedelta(days=14)
    try:
        rows = client.query(entity, f"{f['timestamp']} ge {odata_time(since)}", top=300, raw=True)
    except ApiError as exc:
        return [Check("Aternity", "software change log", None, f"Could not read it: {exc.reason or exc}")]
    kinds = Counter(str(r.get(f.get("change", ""))) for r in rows)
    sample = {}
    for r in rows:
        sample.setdefault(str(r.get(f.get("change", ""))), r)
    others = sorted(c for c in schema[entity] if any(w in c for w in ("VERSION", "CHANGE", "TYPE", "ACTION")))
    # does one change show up as two rows (e.g. a removal and an install) of the same
    # software on the same PC, and at the same second?
    key = lambda r: (r.get(f.get("device", "")), r.get(f.get("name", "")))  # noqa: E731
    by_sw: dict[tuple, list] = {}
    for r in rows:
        by_sw.setdefault(key(r), []).append(r)
    same, hour, alone = Counter(), Counter(), Counter()
    for r in rows:
        t, kind = parse_time(r.get(f["timestamp"])), str(r.get(f.get("change", "")))
        other = [o for o in by_sw[key(r)] if str(o.get(f.get("change", ""))) != kind]
        gaps = [abs((parse_time(o.get(f["timestamp"])) - t).total_seconds()) for o in other
                if t and parse_time(o.get(f["timestamp"]))]
        (same if gaps and min(gaps) == 0 else hour if gaps and min(gaps) <= 3600 else alone)[kind] += 1
    pairs = ("Rows with a row of another change type for the same software on the same PC: at the same second: "
             + (", ".join(f"{k} {n}" for k, n in same.most_common()) or "none")
             + "; within an hour: " + (", ".join(f"{k} {n}" for k, n in hour.most_common()) or "none")
             + "; none: " + (", ".join(f"{k} {n}" for k, n in alone.most_common()) or "none") + ".")
    out = [Check("Aternity", "software change log", None,
                 f"{entity}, last 14 days: {len(rows)} row(s){' (first 300)' if len(rows) == 300 else ''}. "
                 "Change types: " + (", ".join(f"{k} ({n})" for k, n in kinds.most_common()) or "none")
                 + ". Its version/change/type columns: " + ", ".join(others) + ". " + pairs + " One row per change type:",
                 rows=[_raw(r, f, ("timestamp", "name", "change", "version", "version_before")) for r in sample.values()])]
    # Is a row with no other half at the same second a real removal (or install), or
    # is its other half at another time? The first rows mix every PC in a short span,
    # so look at whole PCs: each PC's full 14 days.
    devices = list(dict.fromkeys(r.get(f["device"]) for r, _ in _unpaired(rows, f) if r.get(f.get("device", ""))))
    for dev in devices[:3]:
        try:
            mine = client.query(entity, f"{f['timestamp']} ge {odata_time(since)} and "
                                f"{f['device']} eq {odata_quote(str(dev))}", top=1000, raw=True)
        except ApiError as exc:
            out.append(Check("Aternity", f"software change log of {dev}", None, f"Could not read it: {exc.reason or exc}"))
            continue
        lone = _unpaired(mine, f)
        gaps = Counter("none in 14 days" if near is None else "within 10 min" if near[0] <= 600
                       else "within a day" if near[0] <= 86400 else "more than a day apart" for _, near in lone)
        out.append(Check(
            "Aternity", f"software change log of {dev}", None,
            f"All its rows of the last 14 days: {len(mine)}{' (first 1000)' if len(mine) == 1000 else ''}; "
            f"{len(lone)} without a row of another change type for the same software at the same second. "
            "Their nearest other change of that software: " + (", ".join(f"{k} {n}" for k, n in gaps.most_common())
                                                               or "-") + ". The first 6:",
            rows=[_raw(r, f, ("timestamp", "name", "change", "version"))
                  + ("  ->  none in 14 days" if near is None else
                     f"  ->  {near[0] / 60:.0f} min apart: " + _raw(near[1], f, ("timestamp", "change", "version")))
                  for r, near in lone[:6]]))
        out += _put_backs(settings, client, schema, str(dev), lone)
    return out


def _put_backs(settings: Settings, client: AternityClient, schema: dict[str, set[str]], dev: str,
               lone: list[tuple]) -> list[Check]:
    """A software removed and put back at the same version, further apart than an
    update: did the PC start up then, with which image build, and did other PCs
    have change-log rows in the same minute (an image refresh or a pushed change)?"""
    cfg = settings.aternity
    f, bf = cfg.fields.get("software_changes", {}), cfg.fields.get("boots", {})
    gap = SOFTWARE_UPDATE_GAP_MIN * 60
    pairs = [(r, near[1]) for r, near in lone if near and near[0] > gap
             and r.get(f.get("version", "")) == near[1].get(f.get("version", ""))
             and str(r.get(f["timestamp"])) < str(near[1].get(f["timestamp"]))]
    if not pairs:
        return []
    be, since = cfg.entities.get("boots"), datetime.now(timezone.utc) - timedelta(days=15)
    image = VDI_COLUMNS["boots"]["image_build"]
    boots, lines = [], []
    if be in schema and bf.get("device") and bf.get("timestamp"):
        cols = [bf[k] for k in ("timestamp", "started", "type") if bf.get(k)] + ([image] if image in schema[be] else [])
        try:
            boots = client.query(be, f"{bf['timestamp']} ge {odata_time(since)} and {bf['device']} eq {odata_quote(dev)}",
                                 select=cols, top=1000, raw=True)
        except ApiError as exc:
            lines.append(f"Its start-ups could not be read: {exc.reason or exc}")
    starts = [(parse_time(r.get(bf.get("started", ""))) or parse_time(r.get(bf["timestamp"])), r) for r in boots]
    starts = [(t, r) for t, r in starts if t]

    def nearest(t):
        if not t or not starts:
            return "no start-up in 15 days"
        d, r = min((((t - b).total_seconds(), r) for b, r in starts), key=lambda x: abs(x[0]))
        return f"nearest start-up {abs(d) / 60:.0f} min {'before' if d >= 0 else 'after'} ({r.get(bf.get('type', ''))!r})"

    for r, back in pairs[:4]:
        t1, t2 = parse_time(r.get(f["timestamp"])), parse_time(back.get(f["timestamp"]))
        lines.append(f"{r.get(f.get('name', ''))!r} {r.get(f.get('version', ''))!r}: "
                     f"{r.get(f.get('change', ''))} {r.get(f['timestamp'])}, {nearest(t1)}; "
                     f"{back.get(f.get('change', ''))} {back.get(f['timestamp'])}, {nearest(t2)}")
    if image in (schema.get(be) or set()):
        seen: dict[str, list] = {}
        for t, b in sorted(starts, key=lambda x: x[0]):
            seen.setdefault(str(b.get(image)), []).append(t)
        lines.append(f"Image builds ({image}) in its {len(starts)} start-ups: " + "; ".join(
            f"{k!r} {len(ts)}x, {ts[0]:%b %d %H:%M} to {ts[-1]:%b %d %H:%M}" for k, ts in seen.items()))
    # the same minute on other PCs
    t = parse_time(pairs[0][0].get(f["timestamp"]))
    if t:
        entity = cfg.entities["software_changes"]
        try:
            near = client.query(entity, f"{f['timestamp']} ge {odata_time(t - timedelta(minutes=1))} and "
                                f"{f['timestamp']} le {odata_time(t + timedelta(minutes=1))}",
                                select=[f["timestamp"], f["device"], f["name"]], top=5000, raw=True)
            pcs = {str(x.get(f["device"])) for x in near} - {dev}
            same = {str(x.get(f["device"])) for x in near if x.get(f["name"]) == pairs[0][0].get(f["name"])} - {dev}
            lines.append(f"Within 1 min of {pairs[0][0].get(f['timestamp'])}: {len(pcs)} other PC(s) have change-log "
                         f"rows, {len(same)} of them for {pairs[0][0].get(f['name'])!r}"
                         + (" (first 5000 rows)" if len(near) == 5000 else "") + ".")
        except ApiError as exc:
            lines.append(f"Other PCs at that minute could not be read: {exc.reason or exc}")
    return [Check("Aternity", f"put-backs on {dev}", None,
                  f"{len(pairs)} software removed and put back at the same version more than "
                  f"{SOFTWARE_UPDATE_GAP_MIN} min later. The first {min(4, len(pairs))}, each time with "
                  "the PC's nearest start-up:", rows=lines)]


def _values(rows: list[dict], column: str, n: int = 8) -> str:
    """The values of one column as sent, most common first."""
    c = Counter(repr(r.get(column)) for r in rows)
    return ", ".join(f"{k} {v}" for k, v in c.most_common(n)) + (f" (+{len(c) - n} more)" if len(c) > n else "")


def _spread(rows: list[dict], column: str) -> str:
    vals = [v for v in (to_float(r.get(column)) for r in rows) if v is not None]
    return (f"{len(vals)} with a value; min {min(vals):g}, median {median(vals):g}, max {max(vals):g}"
            if vals else "no values")


def _vdi(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """Virtual desktops: which VDI and desktop-management columns are published and
    how they are filled, and whether a user keeps one desktop (persistent) or gets
    another each time (pooled)."""
    cfg = settings.aternity
    out, have = [], {}
    for key, cols in VDI_COLUMNS.items():
        entity = cfg.entities.get(key)
        if entity not in schema:
            continue
        have[key] = {k: c for k, c in cols.items() if c in schema[entity]}
        gone = [c for c in cols.values() if c not in schema[entity]]
        out.append(Check("Aternity", f"VDI columns in {entity}", None,
                         "Published: " + (", ".join(have[key].values()) or "none")
                         + (". Not published: " + ", ".join(gone) if gone else "") + "."))
    end = datetime.now(timezone.utc)
    act, f = cfg.entities.get("activities"), cfg.fields.get("activities", {})
    vdi = have.get("activities", {})
    if vdi and f.get("timestamp"):
        try:
            rows = client.query(act, f"{f['timestamp']} ge {odata_time(end - timedelta(days=1))}", top=1000,
                                select=[f[k] for k in ("timestamp", "username", "device")] + list(vdi.values()), raw=True)
        except ApiError as exc:
            return out + [Check("Aternity", "VDI values", None, f"Could not read them: {exc.reason or exc}")]
        lines = [f"{k} ({c}): " + (_spread(rows, c) if k == "display_latency" else _values(rows, c))
                 for k, c in vdi.items()]
        out.append(Check("Aternity", f"VDI values in {act}", None,
                         f"Last day: {len(rows)} row(s){' (first 1000)' if len(rows) == 1000 else ''} on "
                         f"{len({r.get(f['device']) for r in rows})} PC(s). Values as sent, most common first:",
                         rows=lines))
        # one user per kind of desktop (each VIRTUALIZATION value): their PCs over 14 days,
        # and the users of that PC
        kind = vdi.get("virtualization")
        picks: dict[str, dict] = {}
        for r in rows:
            if r.get(f["username"]) and r.get(f["device"]):
                picks.setdefault(repr(r.get(kind)) if kind else "", r)
        since = odata_time(end - timedelta(days=14))
        lines = []
        for k, r in list(picks.items())[:3]:
            user, dev = str(r[f["username"]]), str(r[f["device"]])
            try:
                pcs = client.query(act, f"{f['timestamp']} ge {since} and {f['username']} eq {odata_quote(user)}",
                                   select=[f["device"]], top=5000, raw=True)
                users = client.query(act, f"{f['timestamp']} ge {since} and {f['device']} eq {odata_quote(dev)}",
                                     select=[f["username"]], top=5000, raw=True)
            except ApiError as exc:
                lines.append(f"{user}: could not read: {exc.reason or exc}")
                continue
            cut = lambda x: " (first 5000 rows)" if len(x) == 5000 else ""  # noqa: E731
            lines.append(f"{kind} {k}: user {user} worked on {len({x.get(f['device']) for x in pcs})} PC(s) in "
                         f"14 days ({_values(pcs, f['device'], 4)}){cut(pcs)}; PC {dev} was used by "
                         f"{len({x.get(f['username']) for x in users})} user(s){cut(users)}.")
        if lines:
            out.append(Check("Aternity", "one desktop per user, or pooled?", None,
                             "One user per kind of desktop seen above, their last 14 days:", rows=lines))
    inv, fi = cfg.entities.get("inventory"), cfg.fields.get("inventory", {})
    mgmt = have.get("inventory", {})
    if mgmt and fi.get("device"):
        try:
            rows = client.query(inv, None, top=1000, select=[fi["device"]] + list(mgmt.values()), raw=True)
        except ApiError as exc:
            return out + [Check("Aternity", f"management values in {inv}", None, f"Could not read them: {exc.reason or exc}")]
        out.append(Check("Aternity", f"management values in {inv}", None,
                         f"{len(rows)} device(s){' (first 1000)' if len(rows) == 1000 else ''}. Values as sent:",
                         rows=[f"{k} ({c}): {_values(rows, c)}" for k, c in mgmt.items()]))
    return out


def _remote_display(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """How the remote display latency source is filled (the leg from the user's
    own device to the virtual desktop)."""
    cfg = settings.aternity
    entity, f = cfg.entities.get("remote_display"), cfg.fields.get("remote_display", {})
    if entity not in schema or f.get("timestamp") not in schema[entity]:
        return []
    cols = {k: c for k, c in f.items() if c in schema[entity]}
    try:
        rows = client.query(entity, f"{f['timestamp']} ge {odata_time(datetime.now(timezone.utc) - timedelta(days=1))}",
                            top=1000, select=list(cols.values()), raw=True)
    except ApiError as exc:
        return [Check("Aternity", "remote display latency", None, f"Could not read it: {exc.reason or exc}")]
    lines = [f"{k} ({c}): " + (_spread(rows, c) if k == "latency" else
                               f"{len({r.get(c) for r in rows} - {None, '', 'N/A'})} different" if k in ("device", "username", "client_device")
                               else _values(rows, c, 10))
             for k, c in cols.items() if k != "timestamp"]
    if rows:
        lines.append("One row: " + _raw(rows[0], f, list(cols)))
    return [Check("Aternity", "remote display latency", None,
                  f"{entity}, last day: {len(rows)} row(s){' (first 1000)' if len(rows) == 1000 else ''}. "
                  "Values as sent, most common first:", rows=lines)]


def _process_sources(client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """Every published data source with a process column: which could tell the
    busiest processes on a PC, and does it have rows?"""
    found = sorted(e for e, cols in schema.items() if PROCESS_COLUMN in cols)
    lines = []
    for e in found[:10]:
        t = next((c for c in TIME_COLUMNS if c in schema[e]), None)
        has = "DEVICE_NAME" in schema[e]
        n = "-"
        if t:
            try:
                n = "yes" if client.query(e, f"{t} ge {odata_time(datetime.now(timezone.utc) - timedelta(days=1))}",
                                          top=1, select=[t], raw=True) else "none"
            except ApiError as exc:
                n = f"could not read ({exc.reason or exc})"
        cpu = sorted(c for c in schema[e] if c.startswith("PRC_") and any(w in c for w in ("CPU", "MEMORY", "IO_")))
        lines.append(f"{e}: time column {t or 'none'}, device column {'yes' if has else 'no'}, rows in the last "
                     f"day: {n}; CPU/memory/IO columns: {', '.join(cpu) or 'none'}")
    return [Check("Aternity", "process data", None,
                  f"Published data sources with {PROCESS_COLUMN}: {len(found)}" + (":" if found else
                  ". No source can tell which process used the CPU, memory or disk."), rows=lines)]


FLEET_DAYS = 21  # failures per day, and hangs against updates
LOG_DAYS = 28  # change log rows read: a PC's earliest row of a version is when it arrived
ROWS_PER_READ = 50000


def _fleet(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """The whole fleet, to read an issue's "who else" and "what changed": how many PCs
    and users there are, how far back the data goes, when the widest failures began,
    how software reaches the PCs, whether the log's time is when it happened, and
    whether hangs changed on the PCs an update reached."""
    now = datetime.now(timezone.utc)
    log = _software_log(settings, client, schema, now)
    virtual = _virtual_pcs(settings, client, schema, now)
    return (_fleet_size(settings, client, schema, now) + _widest_failures(settings, client, schema, now)
            + _failures_per_day(settings, client, schema, now) + _rollouts(settings, client, schema, now, log, virtual)
            + _hangs_and_updates(settings, client, schema, now, log)
            + _after_start_up(settings, client, schema, now)
            + _where_users_sit(settings, client, schema, now))


def _pieces(now: datetime, days: int) -> list[tuple[datetime, datetime]]:
    """A week at a time, oldest first: one read of all PCs would stop at max_pages."""
    out = []
    for back in range(days, 0, -7):
        out.append((now - timedelta(days=back), now - timedelta(days=max(back - 7, 0))))
    return out


def _between(column: str, start: datetime, end: datetime) -> str:
    return f"{column} ge {odata_time(start)} and {column} lt {odata_time(end)}"


def _per_day(items) -> str:
    """(time, device) pairs as PCs per day."""
    days: dict = {}
    for t, dev in items:
        days.setdefault(t.date(), set()).add(dev)
    return ", ".join(f"{d.strftime('%b %d')} {len(p)}" for d, p in sorted(days.items()))


def _cut_short(client, mark: int) -> list[str]:
    return [f"PARTIAL: {w}" for w in client.warnings[mark:]]


def _virtual_pcs(settings, client, schema, now) -> dict:
    """PC -> virtual or not, from the last 2 days' activity."""
    act, a = settings.aternity.entities.get("activities"), settings.aternity.fields.get("activities", {})
    column = VDI_COLUMNS["activities"]["virtualization"]
    if act not in schema or column not in schema[act] or not a.get("device"):
        return {}
    try:
        rows = client.query(act, f"{a['timestamp']} ge {odata_time(now - timedelta(days=2))}",
                            select=[a["device"], column], top=ROWS_PER_READ)
    except ApiError:
        return {}
    return {r.get(a["device"]): is_virtual(r.get(column)) for r in rows}


def _software_log(settings, client, schema, now) -> dict:
    """All PCs' change log of LOG_DAYS. "arrived": (name, version) -> PC -> its earliest
    row; "seen": (PC, version) -> the times of those rows; "minutes": PC -> minutes."""
    cfg = settings.aternity
    ent, f = cfg.entities.get("software_changes"), cfg.fields.get("software_changes", {})
    out = {"arrived": {}, "seen": {}, "minutes": {}, "rows": 0, "full": 0, "error": None}
    if ent not in schema or not f.get("device"):
        return out
    for start, end in _pieces(now, LOG_DAYS):
        try:
            rows = client.query(ent, _between(f["timestamp"], start, end),
                                select=[c for c in f.values() if c], top=ROWS_PER_READ)
        except ApiError as exc:
            out["error"] = exc.short()
            continue
        out["rows"] += len(rows)
        out["full"] += len(rows) >= ROWS_PER_READ
        for r in rows:
            t = parse_time(r.get(f["timestamp"]))
            if t is None:
                continue
            dev, version = r.get(f["device"]), r.get(f["version"])
            out["minutes"].setdefault(dev, []).append(t.replace(second=0, microsecond=0))
            if r.get(f.get("change", "")) not in SOFTWARE_REMOVED and version:
                pcs = out["arrived"].setdefault((r.get(f["name"]), version), {})
                pcs[dev] = min(t, pcs.get(dev, t))
                out["seen"].setdefault((dev, version), []).append(t)
    return out


def _fleet_size(settings, client, schema, now) -> list[Check]:
    cfg, rows = settings.aternity, []
    inv, f = cfg.entities.get("inventory"), cfg.fields.get("inventory", {})
    if inv in schema and f.get("device"):
        try:
            pcs = client.query(inv, select=[f["device"]])
            rows.append(f"PCs in the inventory: {len({r.get(f['device']) for r in pcs})}")
        except ApiError as exc:
            rows.append(f"inventory: {exc.short()}")
    act, a = cfg.entities.get("activities"), cfg.fields.get("activities", {})
    if act in schema and a.get("device") and a.get("username"):
        try:
            day = client.query(act, f"{a['timestamp']} ge {odata_time(now - timedelta(days=1))}",
                               select=[a["device"], a["username"]], top=20000)
            rows.append(f"last day's activity ({len(day)} rows read): "
                        f"{len({r.get(a['device']) for r in day})} PCs, {len({r.get(a['username']) for r in day})} users")
        except ApiError as exc:
            rows.append(f"activities: {exc.short()}")
    # how far back: one row in a day of that many days ago ("error": the read failed)
    for key in ("health_events", "activities", "software_changes", "app_usage"):
        ent, ts = cfg.entities.get(key), cfg.fields.get(key, {}).get("timestamp")
        if ent not in schema or not ts:
            continue
        back = []
        for days in (3, 7, 10, 13, 14, 30, 60, 90):
            start = now - timedelta(days=days)
            try:
                hit = client.query(ent, _between(ts, start, start + timedelta(days=1)), select=[ts], top=1)
                back.append(f"{days}d {'yes' if hit else 'no'}")
            except ApiError:
                back.append(f"{days}d error")
        rows.append(f"{ent} has rows: {', '.join(back)}")
    return [Check("Aternity", "fleet: how many PCs, how far back", None, "What \"N other PCs of M\" and "
                  "\"since when\" rest on.", rows=rows)]


def _widest_failures(settings, client, schema, now) -> list[Check]:
    """The failures on the most PCs in the last 2 days: PCs and users per day over 14 days."""
    ent = settings.aternity.entities.get("health_events")
    f = settings.aternity.fields.get("health_events", {})
    if ent not in schema or not f.get("device"):
        return []
    recent = client._events(f"{f['timestamp']} ge {odata_time(now - timedelta(days=2))}", "health events of all PCs",
                            client.EVERYWHERE)
    by: dict[tuple, set] = {}
    for e in recent:
        by.setdefault((e.name or e.category, e.component, e.sub_component), set()).add(e.device)
    rows = [f"last 2 days: {len(recent)} events on {len({e.device for e in recent})} PCs"]
    for (name, comp, sub), pcs in sorted(by.items(), key=lambda kv: -len(kv[1]))[:5]:
        rows.append(f"{name}: {comp}{f' / {sub}' if sub else ''}: {len(pcs)} PCs")
        if not comp:
            continue
        old = [e for e in client.component_health_events(comp, now - timedelta(days=14), now)
               if (e.name or e.category) == name and e.sub_component == sub]
        days: dict = {}
        for e in old:
            days.setdefault(e.timestamp.date(), set()).add(e.device)
        rows.append(f"  {len({e.device for e in old})} PCs, {len({e.username for e in old if e.username})} users in 14 days;"
                    f" versions {dict(Counter(e.component_version for e in old).most_common(3))}")
        rows.append("  PCs per day: " + ", ".join(f"{d.strftime('%b %d')} {len(p)}" for d, p in sorted(days.items())))
        back = []  # when it began: one day each, the same weekday, so the numbers compare
        for weeks in (4, 8, 12, 16, 26):
            start = now - timedelta(days=1, weeks=weeks)
            same = {e.device for e in client.component_health_events(comp, start, start + timedelta(days=1))
                    if (e.name or e.category) == name and e.sub_component == sub}
            back.append(f"{start.strftime('%b %d')} {len(same)}")
        rows.append("  further back, same weekday: " + ", ".join(back))
    return [Check("Aternity", "fleet: widest failures", None, "When each began, and PCs against users.", rows=rows)]


def _failures_per_day(settings, client, schema, now) -> list[Check]:
    """Every kind of health event, and the hangs by program, per day: did anything step up?"""
    ent = settings.aternity.entities.get("health_events")
    f = settings.aternity.fields.get("health_events", {})
    if ent not in schema or not f.get("device"):
        return []
    mark, events = len(client.warnings), []
    for start, end in _pieces(now, FLEET_DAYS):
        events += client._events(_between(f["timestamp"], start, end), "health events of all PCs", client.EVERYWHERE)
    kinds: dict = {}
    hangs: dict = {}
    for e in events:
        kinds.setdefault(e.name or e.category, []).append(e)
        if "hang" in f"{e.category} {e.name}".lower():
            hangs.setdefault(e.application or _label_off(e.component), []).append(e)
    rows = [f"{FLEET_DAYS} days: {len(events)} events on {len({e.device for e in events})} PCs"]
    wide = lambda group: -len({e.device for e in group[1]})  # noqa: E731
    for kind, group in sorted(kinds.items(), key=wide)[:8]:
        rows.append(f"{kind}: {len(group)} events, {len({e.device for e in group})} PCs")
        rows.append("  PCs per day: " + _per_day((e.timestamp, e.device) for e in group))
    for program, group in sorted(hangs.items(), key=wide)[:5]:
        rows.append(f"hang, {program}: {len(group)} events, {len({e.device for e in group})} PCs")
        rows.append("  PCs per day: " + _per_day((e.timestamp, e.device) for e in group))
    if not hangs:
        rows.append("no event of these days is a hang")
    return [Check("Aternity", "fleet: failures per day", None, "Did anything step up, and when?",
                  rows=rows + _cut_short(client, mark))]


def _rollouts(settings, client, schema, now, log, virtual) -> list[Check]:
    """How software reaches the PCs: per PC, in how many separate minutes; per update, on how
    many PCs (virtual desktops and physical PCs) and over how long. And is the log's time
    real? Failures already running a version before the PC's earliest row of it."""
    cfg = settings.aternity
    if cfg.entities.get("software_changes") not in schema or not cfg.fields.get("software_changes", {}).get("device"):
        return []
    if log["error"] and not log["rows"]:
        return [Check("Aternity", "fleet: how software arrives", None, f"Could not read it: {log['error']}")]
    rows = [f"{log['rows']} rows read over {LOG_DAYS} days"]
    if log["full"] or log["error"]:
        rows.append(f"PARTIAL: {log['full']} of the reads hit {ROWS_PER_READ} rows"
                    + (f"; {log['error']}" if log["error"] else ""))
    if log["minutes"]:
        minutes = [len(set(v)) for v in log["minutes"].values()]
        busiest = [Counter(v).most_common(1)[0][1] for v in log["minutes"].values()]
        rows.append(f"{len(minutes)} PCs with changes; separate minutes per PC: median {median(minutes):g}, "
                    f"max {max(minutes)}; most rows in one minute: median {median(busiest):g}, max {max(busiest)}")
    kind = lambda dev: {True: "virtual", False: "physical"}.get(virtual.get(dev), "not known")  # noqa: E731
    for (name, version), pcs in sorted(log["arrived"].items(), key=lambda kv: -len(kv[1]))[:8]:
        t = sorted(pcs.values())
        rows.append(f"{name} {version}: {len(pcs)} PCs, {t[0].strftime('%b %d %H:%M')} to {t[-1].strftime('%b %d %H:%M')}"
                    f", busiest day {Counter(x.strftime('%b %d') for x in t).most_common(1)[0]}, "
                    f"{dict(Counter(kind(d) for d in pcs))}")
    # a failing process at a version the log shows arriving on that PC: before or after?
    h = cfg.fields.get("health_events", {})
    if h.get("device"):
        events = client._events(f"{h['timestamp']} ge {odata_time(now - timedelta(days=7))}", "health events of all PCs",
                                client.EVERYWHERE)
        seen: dict = {}
        for e in events:
            for v in (e.component_version, e.sub_component_version):
                times = log["seen"].get((e.device, _label_off(v)))
                if times:
                    early = e.timestamp < min(times)
                    gap = "" if not early else "under a day" if min(times) - e.timestamp < timedelta(days=1) else "longer"
                    seen.setdefault(kind(e.device), Counter())["before: " + gap if early else "after it"] += 1
        rows.append(f"failures at a version, against the PC's earliest row of it in {LOG_DAYS} days:")
        rows += [f"  {k}: {dict(c)}" for k, c in sorted(seen.items())] or ["  none"]
        twice = sum(len(t) > 1 for t in log["seen"].values())
        rows.append(f"PC and version pairs logged more than once: {twice} of {len(log['seen'])}")
    return [Check("Aternity", "fleet: how software arrives", None, "One image update, or one by one? "
                  "Is the log's time when it happened?", rows=rows)]


def _rate(hangs: dict, pcs, split, start, end) -> tuple[float, float] | None:
    """Rows per 100 PC-days before and after each PC's own time."""
    rows, days = [0, 0], [0.0, 0.0]
    for dev in pcs:
        t = split(dev)
        if not start < t < end:
            continue
        days[0] += (t - start).total_seconds() / 86400
        days[1] += (end - t).total_seconds() / 86400
        for when in hangs.get(dev, ()):
            rows[when >= t] += 1
    return None if not all(days) else (round(rows[0] * 100 / days[0], 1), round(rows[1] * 100 / days[1], 1))


def _hangs_and_updates(settings, client, schema, now, log) -> list[Check]:
    """App usage hangs and crashes per day, and whether hangs changed on the PCs an update
    reached, next to the PCs it did not reach (split at the update's middle day)."""
    ent, f = settings.aternity.entities.get("app_usage"), settings.aternity.fields.get("app_usage", {})
    if ent not in schema or not f.get("hang_time") or not f.get("device"):
        return []
    select, usage, rows = [f[k] for k in ("timestamp", "device", "application", "crashes", "hang_time") if f.get(k)], [], []
    mark = len(client.warnings)
    for start, end in _pieces(now, 14):
        when = _between(f["timestamp"], start, end)
        try:
            read = client.query(ent, f"{when} and ({f['hang_time']} gt 0 or {f['crashes']} gt 0)", select=select,
                                top=ROWS_PER_READ)
        except ApiError as exc:
            rows.append(f"read of hangs only refused ({exc.short()}): reading all rows")
            try:
                read = client.query(ent, when, select=select, top=ROWS_PER_READ)
            except ApiError as again:
                rows.append(f"could not read it: {again.short()}")
                continue
        rows.append(f"{start.strftime('%b %d')} to {end.strftime('%b %d')}: {len(read)} rows")
        usage += read
    by: dict = {"hang": {}, "crash": {}}
    for r in usage:
        t = parse_time(r.get(f["timestamp"]))
        if t is None:
            continue
        for kind, col in (("hang", "hang_time"), ("crash", "crashes")):
            if (to_float(r.get(f[col])) or 0) > 0:
                by[kind].setdefault(r.get(f["application"]), []).append((t, r.get(f["device"])))
    for kind, apps in by.items():
        for app, items in sorted(apps.items(), key=lambda kv: -len({d for _, d in kv[1]}))[:6]:
            rows.append(f"{kind}, {app}: {len(items)} rows, {len({d for _, d in items})} PCs")
            rows.append("  PCs per day: " + _per_day(items))
    everything = [t for items in by["hang"].values() for t, _ in items]
    if everything:
        start, tops = min(everything), sorted(by["hang"].items(), key=lambda kv: -len(kv[1]))[:3]
        rows.append(f"hang rows per 100 PC-days from {start.strftime('%b %d')}, before and after the update:")
        for (name, version), pcs in sorted(log["arrived"].items(), key=lambda kv: -len(kv[1]))[:4]:
            times = sorted(pcs.values())
            middle = times[len(times) // 2]
            others = set(log["minutes"]) - set(pcs)
            for app, items in tops:
                hangs: dict = {}
                for t, dev in items:
                    hangs.setdefault(dev, []).append(t)
                got = _rate(hangs, pcs, pcs.get, start, now)
                rest = _rate(hangs, others, lambda _: middle, start, now)
                rows.append(f"{name} {version}, {app}: reached {got or 'n/a'}, not reached {rest or 'n/a'}")
    return [Check("Aternity", "fleet: hangs against updates", None, "Did hangs change on the PCs an update reached?",
                  rows=rows + _cut_short(client, mark))]


AFTER_START_DAYS = 4  # actions and start-ups read, half a day at a time


def _after_start_up(settings, client, schema, now) -> list[Check]:
    """Are actions slow because they come right after the PC started up (a launch
    just after logon on a fresh pooled desktop)? Of all slow actions, how many came
    up to AFTER_START_MIN after a start-up of the same PC, virtual and physical; then,
    for the activities most often slow there, all their runs right after a start-up
    against later ones: runs, median time, share slow."""
    cfg, th = settings.aternity, settings.thresholds
    act, a = cfg.entities.get("activities"), cfg.fields.get("activities", {})
    boot, b = cfg.entities.get("boots"), cfg.fields.get("boots", {})
    if act not in schema or boot not in schema or not a.get("device") or not b.get("device"):
        return []
    rows, mark = [], len(client.warnings)
    pieces = [(now - timedelta(hours=12 * (i + 1)), now - timedelta(hours=12 * i))
              for i in reversed(range(AFTER_START_DAYS * 2))]
    rows.append(f"Read: {pieces[0][0].strftime('%b %d %H:%M')} to {now.strftime('%b %d %H:%M')} UTC; "
                f"\"right after\" = 0 to {AFTER_START_MIN} min after a start-up row of the same PC")

    ent_key = {act: "activities", boot: "boots"}

    def read(ent, flt, select, what, keep=None):
        """Every piece; a filter Aternity refuses is said once, then every piece is
        read unfiltered and `keep` picks the rows."""
        out = []
        for start, end in pieces:
            when = _between(cfg.fields[ent_key[ent]]["timestamp"], start, end)
            try:
                got = client.query(ent, f"{when} and ({flt})" if flt else when, select=select, top=ROWS_PER_READ)
            except ApiError as exc:
                if not (flt and keep):
                    rows.append(f"{what}, {start.strftime('%b %d %H:%M')}: error {exc.short()}")
                    continue
                rows.append(f"{what}: filter refused ({exc.short()}), reading all rows")
                flt = ""
                try:
                    got = client.query(ent, when, select=select, top=ROWS_PER_READ)
                except ApiError as again:
                    rows.append(f"{what}, {start.strftime('%b %d %H:%M')}: error {again.short()}")
                    continue
            if len(got) >= ROWS_PER_READ:
                rows.append(f"PARTIAL: {what}, {start.strftime('%b %d %H:%M')}: stopped at {ROWS_PER_READ} rows")
            out += [r for r in got if keep(r)] if keep and not flt else got
        return out

    starts: dict = {}
    for r in read(boot, "", [b["timestamp"], b["device"]], "start-ups"):
        t = parse_time(r.get(b["timestamp"]))
        if t is not None:
            starts.setdefault(r.get(b["device"]), []).append(t)
    for times in starts.values():
        times.sort()
    rows.append(f"start-up rows: {sum(map(len, starts.values())):,} on {len(starts):,} PCs")

    def right_after(dev, t) -> bool:
        times = starts.get(dev, ())
        i = bisect_right(times, t)
        return i > 0 and t - times[i - 1] <= timedelta(minutes=AFTER_START_MIN)

    vcol = VDI_COLUMNS["activities"]["virtualization"]
    vcol = vcol if vcol in schema[act] else None
    select = [c for c in (a.get("timestamp"), a.get("device"), a.get("application"), a.get("activity"),
                          a.get("response_time"), a.get("status"), vcol) if c]
    if th.slow_status and a.get("status"):
        slow_filter = " or ".join(f"{a['status']} eq {odata_quote(s)}" for s in th.slow_status)
        slow = lambda r: r.get(a["status"]) in th.slow_status  # noqa: E731
    else:
        slow_filter = f"{a['response_time']} gt {th.response_time_s:g}"
        slow = lambda r: (to_float(r.get(a["response_time"])) or 0) > th.response_time_s  # noqa: E731
    kind = lambda r: ("virtual" if is_virtual(r.get(vcol)) else "physical") if vcol else "all"  # noqa: E731

    def runs(items):
        """(time, row) -> per kind: [right after, later] of (runs, slow, times)."""
        out: dict = {}
        for t, r in items:
            side = out.setdefault(kind(r), [[0, 0, []], [0, 0, []]])[0 if right_after(r.get(a["device"]), t) else 1]
            side[0] += 1
            side[1] += slow(r)
            rt = to_float(r.get(a["response_time"]))
            if rt is not None:
                side[2].append(rt)
        return out

    timed = lambda got: [(t, r) for r in got if (t := parse_time(r.get(a["timestamp"]))) is not None]  # noqa: E731
    slow_rows = timed(read(act, slow_filter, select, "slow actions", slow))
    pct = lambda k, n: f"{k:,} of {n:,} ({k * 100 // n}%)" if n else "0 of 0"  # noqa: E731
    rows.append(f"slow actions ({slow_rule(settings)}): {len(slow_rows):,} on "
                f"{len({r.get(a['device']) for _, r in slow_rows}):,} PCs; right after a start-up:")
    for k, (after, later) in sorted(runs(slow_rows).items()):
        rows.append(f"  {k}: {pct(after[0], after[0] + later[0])}")
    name = lambda r: (r.get(a["application"]), r.get(a.get("activity", "")))  # noqa: E731
    top = Counter(name(r) for t, r in slow_rows if right_after(r.get(a["device"]), t)).most_common(3)
    if top:
        rows.append("most often slow right after a start-up: "
                    + ", ".join(f"{app} {what or ''} {n:,}".replace("  ", " ") for (app, what), n in top))
    # each of those activities, every run (slow or not): right after a start-up against later
    for (app, what), _ in top:
        flt = f"{a['application']} eq {odata_quote(app)}" + (
            f" and {a['activity']} eq {odata_quote(what)}" if what and a.get("activity") else "")
        label = f"{app} {what or ''}".strip()
        same = lambda r, app=app, what=what: name(r) == (app, what)  # noqa: E731
        for k, (after, later) in sorted(runs(timed(read(act, flt, select, label, same))).items()):
            for side, (n, s, times) in (("right after", after), ("later", later)):
                rows.append(f"{label}, {k}, {side}: {n:,} runs, median "
                            + (f"{median(times):.1f} s" if times else "n/a") + f", {pct(s, n)} slow")
    return [Check("Aternity", "fleet: actions right after a start-up", None,
                  "Are actions slow because the PC has just started up (e.g. a launch right after logon)?",
                  rows=rows + _cut_short(client, mark))]


def _label_off(value) -> str | None:
    return (value or "").rsplit(" (", 1)[0] or None


def _where_users_sit(settings, client, schema, now) -> list[Check]:
    """Does Aternity hold any address for the device a virtual desktop user sits at?"""
    cfg = settings.aternity
    act, f = cfg.entities.get("activities"), cfg.fields.get("activities", {})
    virt = f.get("virtualization")
    if act not in schema or virt not in schema[act] or not f.get("timestamp"):
        return []
    cols = sorted(c for c in schema[act] if any(k in c for k in ("CLIENT", "SUBNET", "ADDRESS", "_IP", "GATEWAY", "SITE")))
    try:
        sample = client.query(act, f"{f['timestamp']} ge {odata_time(now - timedelta(days=1))}",
                              select=[virt] + cols, top=1000, raw=True)
    except ApiError as exc:
        return [Check("Aternity", "fleet: where users sit", None, f"Could not read it: {exc.short()}")]
    virtual = [r for r in sample if is_virtual(r.get(virt))]
    rows = [f"{len(virtual)} virtual desktop rows of {len(sample)} read"]
    for c in cols:
        vals = [r.get(c) for r in virtual if r.get(c) not in (None, "", "N/A")]
        rows.append(f"{c}: {len(vals)} filled, {len(set(vals))} different")
    return [Check("Aternity", "fleet: where users sit", None, "A column filled here could place the user's "
                  "own network.", rows=rows)]


def verify_vdi_network(settings: Settings, aternity: AternityClient, netim: NetimClient) -> list[Check]:
    """Which addresses NetIM can place: the virtual desktops' own (their path to the
    servers), the devices users sit at (CLIENT_DEVICE_NAME, looked up in the
    inventory) and physical PCs. Reads every NetIM device's interfaces."""
    from .correlate import _subnet_contains
    cfg = settings.aternity
    act, f = cfg.entities.get("activities"), cfg.fields.get("activities", {})
    inv, fi = cfg.entities.get("inventory"), cfg.fields.get("inventory", {})
    if not f.get("virtualization") or not f.get("ip_address"):
        return []
    since = odata_time(datetime.now(timezone.utc) - timedelta(days=1))
    try:
        rows = aternity.query(act, f"{f['timestamp']} ge {since}", top=1000,
                              select=[f[k] for k in ("device", "ip_address", "virtualization", "client_device") if f.get(k)])
        devices = netim.devices()
        netim.read_interfaces(devices)
    except ApiError as exc:
        return [Check("NetIM", "which addresses NetIM can place", None, f"Not checked: {exc.reason or exc}")]
    itfs = []
    for d in devices:
        try:
            itfs += [(d, i) for i in netim.interfaces(d.id) if i.ip_addresses]
        except ApiError:
            continue  # read_interfaces already skipped it

    def place(ip: str) -> str:
        hit = next(((d, i) for d, i in itfs if _subnet_contains(i.ip_addresses, ip)), None)
        return f"inside {hit[0].name} {hit[1].name}" if hit else "no NetIM subnet"

    def groups(pcs: dict[str, str], what: str) -> Check:
        """One line per /24 (most PCs first): one address of it placed in NetIM."""
        by: dict[str, list] = {}
        for pc, ip in pcs.items():
            by.setdefault(".".join(str(ip).split(".")[:3]) + ".x", []).append((pc, ip))
        lines, inside = [], 0
        for net, members in sorted(by.items(), key=lambda kv: -len(kv[1]))[:10]:
            where = place(members[0][1])
            inside += where != "no NetIM subnet"
            lines.append(f"{net}: {len(members)} PC(s), e.g. {members[0][0]} {members[0][1]}: {where}")
        return Check("NetIM", what, None, f"{len(pcs)} PC(s) in {len(by)} /24 group(s); of the "
                     f"{min(10, len(by))} biggest, {inside} inside a NetIM interface's subnet:", rows=lines)

    virt = {str(r.get(f["device"])): str(r.get(f["ip_address"])) for r in rows
            if is_virtual(r.get(f["virtualization"])) and r.get(f["ip_address"])}
    phys = {str(r.get(f["device"])): str(r.get(f["ip_address"])) for r in rows
            if is_virtual(r.get(f["virtualization"])) is False and r.get(f["ip_address"])}
    out = [groups(virt, "virtual desktops' own addresses"), groups(phys, "physical PCs' addresses")]
    # the devices users sit at: are they Aternity devices with an address?
    clients = Counter(str(r.get(f["client_device"])) for r in rows
                      if f.get("client_device") and is_virtual(r.get(f["virtualization"])) and r.get(f["client_device"]))
    names = [c for c, _ in clients.most_common(20)]
    if names and inv and fi.get("device") and fi.get("ip_address"):
        flt = " or ".join(f"{fi['device']} eq {odata_quote(n)}" for n in names)
        try:
            found = {str(r.get(fi["device"])): r.get(fi["ip_address"])
                     for r in aternity.query(inv, flt, select=[fi["device"], fi["ip_address"]])}
        except ApiError as exc:
            found, names = {}, []
            out.append(Check("NetIM", "devices users sit at", None, f"Inventory not read: {exc.reason or exc}"))
        if names:
            with_ip = {n: str(ip) for n, ip in found.items() if ip}
            out.append(Check(
                "NetIM", "devices users sit at", None,
                f"{len(clients)} different {f['client_device']} on virtual desktops; of the {len(names)} most used, "
                f"{len(found)} are in {inv}, {len(with_ip)} with an address:",
                rows=[f"{n}: " + (f"{with_ip[n]}, {place(with_ip[n])}" if n in with_ip
                                  else "in the inventory, no address" if n in found else "not an Aternity device")
                      for n in names[:10]]))
    return out

def _unpaired(rows: list[dict], f: dict) -> list[tuple]:
    """Rows with no row of another change type for the same software on the same PC at
    the same second, each with its nearest such row: (row, (seconds, row) or None)."""
    by_sw: dict[tuple, list] = {}
    for r in rows:
        by_sw.setdefault((r.get(f.get("device", "")), r.get(f.get("name", ""))), []).append(r)
    out = []
    for r in sorted(rows, key=lambda r: str(r.get(f["timestamp"]))):
        t, kind = parse_time(r.get(f["timestamp"])), str(r.get(f.get("change", "")))
        other = [(abs((parse_time(o.get(f["timestamp"])) - t).total_seconds()), o)
                 for o in by_sw[(r.get(f.get("device", "")), r.get(f.get("name", "")))]
                 if str(o.get(f.get("change", ""))) != kind and t and parse_time(o.get(f["timestamp"]))]
        near = min(other, key=lambda x: x[0]) if other else None
        if near is None or near[0] > 0:
            out.append((r, near))
    return out


# ---------------------------------------------------------------- NetIM
def parse_metric_classes(data: Any) -> dict[str, set[str]]:
    """Return {metric class id/name: {metric names}} from NetIM's metric-class list."""
    classes: dict[str, set[str]] = {}
    for item in _items(data):
        if not isinstance(item, dict):
            continue
        metrics = metric_names(item)
        for k in ("id", "name"):
            if item.get(k):
                classes[str(item[k])] = metrics
    return classes


def metric_names(metric_class: Any) -> set[str]:
    """Metric ids and names of one metric class (its `metrics` list)."""
    names: set[str] = set()
    if not isinstance(metric_class, dict):
        return names
    # a plain list, or NetIM's paged {"items": [...], "meta": {...}}
    for m in _items(metric_class.get("metrics")):
        for name in ([m.get("id"), m.get("name")] if isinstance(m, dict) else [m]):
            if name:
                names.add(str(name))
    return names


def verify_netim(settings: Settings, client: NetimClient) -> list[Check]:
    cfg = settings.netim
    try:
        devices = client.devices()
    except ApiError as exc:
        return [Check("NetIM", "connection", False, f"Could not list devices: {exc}",
                      "Check netim.base_url, netim.username, NETIM_PASSWORD and netim.api_prefix. "
                      "The account must be a local NetIM user, not SAML or TACACS.")]
    checks = [Check("NetIM", "connection", True, f"Connected; NetIM has {len(devices)} devices.")]
    if devices and not any(d.ip_address for d in devices):
        raw = client.http.get_json(f"{cfg.api_prefix}/devices", params={"limit": 1})
        checks.append(Check("NetIM", "device addresses", False,
                            f"No device address could be read. Keys in a device: {_keys(_items(raw))}.",
                            CODE_FIX.format(file="netim.py (devices)")))

    by_name = {d.name.lower(): d for d in devices}
    by_ip = {d.ip_address for d in devices if d.ip_address}
    topo = settings.topology
    wanted = [(n, f"site '{site}'") for site, ns in topo.sites.items() for n in ns]
    wanted += [(n, "always_include") for n in topo.always_include]
    for name, where in wanted:
        ok = name.lower() in by_name
        checks.append(Check("NetIM", f"topology: {name}", ok,
                            f"Device '{name}' ({where}) {'found' if ok else 'is not in NetIM'}."
                            f"{'' if ok else _closest_text(_closest(name, [d.name for d in devices]))}",
                            "" if ok else "Use the device name exactly as NetIM shows it."))
    for app, servers in topo.applications.items():
        for server in servers:
            ok = server in by_ip or server.lower() in by_name
            checks.append(Check("NetIM", f"topology: {server}", True if ok else None,
                                f"Server '{server}' for '{app}' is a NetIM device." if ok else
                                f"Server '{server}' for '{app}' is not a NetIM device; "
                                "only the network in front of it will be checked."))

    # interfaces with IP addresses are how users and servers are placed on the network
    sample_itf = None
    for d in devices[:20]:
        try:
            itfs = client.interfaces(d.id)
        except ApiError as exc:
            checks.append(Check("NetIM", "interfaces", False, f"Could not list interfaces: {exc}",
                                "Check netim.api_prefix."))
            break
        with_ip = [i for i in itfs if i.ip_addresses]
        if with_ip:
            sample_itf = with_ip[0]
            checks.append(Check("NetIM", "interfaces", True,
                                f"Interface addresses read, e.g. {d.name}:{sample_itf.name}."))
            break
    else:
        if devices:
            raw = _items(client.http.get_json(f"{cfg.api_prefix}/devices/{devices[0].id}/interfaces",
                                              params={"limit": 1}))
            checks.append(Check("NetIM", "interfaces", False,
                                "No interface IP address could be read on the first 20 devices, so "
                                f"users cannot be placed on the network. Keys in an interface: {_keys(raw)}.",
                                CODE_FIX.format(file="netim.py (_parse_ip_entries / interfaces)")))

    try:
        classes = parse_metric_classes(client.metric_classes())
    except ApiError as exc:
        classes = {}
        checks.append(Check("NetIM", "metric classes", False, f"Could not list metric classes: {exc}"))
    if classes:
        ids = sorted({c for c in classes if c.isupper() or "_" in c})
        checks.append(Check("NetIM", "metric classes", None,
                            f"{len(ids)} classes: {', '.join(ids)}."))

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)
    sample_device = devices[0] if devices else None
    corrected = {m.key: {"metric_class": m.metric_class, "metric": m.metric} for m in cfg.metrics}
    for metric in cfg.metrics:
        item = f"metrics.{metric.key}"
        try:
            names = metric_names(client.metric_class(metric.metric_class))
        except ApiError:
            hits = _closest(metric.metric_class, classes)
            checks.append(Check("NetIM", item, False,
                                f"Metric class '{metric.metric_class}' does not exist.{_closest_text(hits)}"))
            if hits:
                corrected[metric.key]["metric_class"] = hits[0]
            continue
        if names and metric.metric not in names:
            hits = _closest(metric.metric, names)
            checks.append(Check("NetIM", item, False,
                                f"Metric '{metric.metric}' is not in {metric.metric_class}. "
                                f"It has: {', '.join(sorted(names))}."))
            if hits:
                corrected[metric.key]["metric"] = hits[0]
            continue
        target = sample_itf if metric.object_type == "INTERFACE" else sample_device
        if target is None:
            checks.append(Check("NetIM", item, None, f"No {metric.object_type.lower()} to test with."))
            continue
        client.warnings.clear()
        samples = client.metrics(metric, [target.id], start, end)
        if client.warnings:
            checks.append(Check("NetIM", item, False, client.warnings[0],
                                "Check netim.metric_data_path."))
        elif samples:
            checks.append(Check("NetIM", item, True,
                                f"{metric.metric_class}/{metric.metric}: {len(samples)} samples in the "
                                f"last hour from {target.name}."))
        else:
            from .netim import metric_request_body
            raw = client.http.post_json(cfg.metric_data_path,
                                        metric_request_body(metric, [target.id], start, end))
            inner = raw.get("metricData") if isinstance(raw, dict) else None
            checks.append(Check("NetIM", item, None,
                                f"{metric.metric_class}/{metric.metric}: no samples read from {target.name} "
                                f"in the last hour. Reply keys: {_keys(raw)}"
                                f"{'; metricData keys: ' + _keys(inner) if isinstance(inner, dict) else ''}. "
                                "If the device is polled, the reply layout differs: "
                                + CODE_FIX.format(file="netim.py (parse_metric_response)")))
    client.warnings.clear()
    if any(corrected[m.key] != {"metric_class": m.metric_class, "metric": m.metric} for m in cfg.metrics):
        lines = ["netim:", "  metrics:"]
        for m in cfg.metrics:
            lines += [f"    - key: {m.key}", f'      label: "{m.label}"',
                      f"      object_type: {m.object_type}",
                      f"      metric_class: {corrected[m.key]['metric_class']}",
                      f"      metric: {corrected[m.key]['metric']}",
                      f"      threshold: {m.threshold:g}"]
        checks.append(Check("NetIM", "corrected metrics", None,
                            "Closest real names for the metrics above (netim.metrics replaces the "
                            "whole default list, so all of them are listed).", paste="\n".join(lines)))

    if cfg.alerts_path:
        client.alerts(start, end)
        ok = not client.warnings
        checks.append(Check("NetIM", "alerts", ok,
                            "Active alerts can be read." if ok else client.warnings[0],
                            "" if ok else 'Fix netim.alerts_path, or set it to "" to skip alerts.'))
        client.warnings.clear()
    else:
        checks.append(Check("NetIM", "alerts", None,
                            "Off (netim.alerts_path is empty). NetIM's HELP > API Documentation "
                            "shows the active-alerts path if you want them."))
    return checks


def verify_overview(settings: Settings, client: AternityClient) -> list[Check]:
    """Only the "overview: …" checks (minutes less than the whole verify)."""
    try:
        schema = parse_odata_metadata(client.http.request("GET", "/$metadata", headers={"Accept": "application/xml"}).text)
    except (ApiError, ET.ParseError) as exc:
        return [Check("Aternity", "connection", False, f"Could not read $metadata: {exc.short() if isinstance(exc, ApiError) else exc}",
                      "Check aternity.base_url, aternity.username and ATERNITY_PASSWORD.")]
    return _overview(settings, client, schema)


SW_SAMPLE = 1000  # health events read by verify --versions
SW_PER_VERSION = 5000  # software: rows of name + version (or name) only, added up by Aternity
SW_ONE_NAME = 20000  # software: rows of one program on every PC, or of the PC list


def verify_software(settings: Settings, client: AternityClient, pc: str | None = None) -> list[Check]:
    """Only the "software: …" checks: what INSTALLED_SOFTWARE holds on this tenant
    (with `pc`: only that PC's list, to compare with its Settings > Apps)."""
    try:
        schema = parse_odata_metadata(client.http.request("GET", "/$metadata", headers={"Accept": "application/xml"}).text)
    except (ApiError, ET.ParseError) as exc:
        return [Check("Aternity", "connection", False, f"Could not read $metadata: {exc.short() if isinstance(exc, ApiError) else exc}",
                      "Check aternity.base_url, aternity.username and ATERNITY_PASSWORD.")]
    inv = settings.aternity.entities.get("inventory"), settings.aternity.fields.get("inventory", {}).get("device")
    return _software(client, schema, pc, inv)


def _software(client: AternityClient, schema: dict[str, set[str]], pc: str | None = None,
              inventory: tuple = (None, None)) -> list[Check]:
    """What the installed software list gives the Software tab, in short: its size (PCs,
    programs, installs, installs for one user only), 2+ versions of a program on one PC
    (installs above PCs when asked per name only, checked on the program with the most
    versions), Windows on each PC, and each Windows update on its own Windows version's
    PCs. With `pc`: only that PC's list. No PC or user names (unless typed). Seen on the
    live tenant: `SOFTWARE_USER_SCOPE` in config.py."""
    S = SOFTWARE_SOURCE
    item = "software: installed list"
    if S not in schema:
        return [Check("Aternity", item, None, f"{S} is not published by this Aternity: nothing on installed "
                      "software can be read.")]
    c = {k: v for k, v in SOFTWARE_COLUMNS.items() if v in schema[S]}
    missing = [v for v in SOFTWARE_COLUMNS.values() if v not in schema[S]]
    if not {"device", "name", "version", "pcs"} <= set(c):
        return [Check("Aternity", item, None, f"{S} is published without {', '.join(missing)}: the Software tab "
                      "cannot use it.")]
    D, N, V, P = c["device"], c["name"], c["version"], c["pcs"]
    number = lambda v: int(v) if str(v).isdigit() else 0  # noqa: E731
    checks: list[Check] = []

    def read(what: str, **kw):
        mark, t0 = len(client.warnings), time.monotonic()
        try:
            rows = client.query(S, raw=True, **kw)
        except ApiError as exc:
            checks.append(Check("Aternity", what, None, f"error: {exc.short()}"))
            return None, 0.0, []
        return rows, time.monotonic() - t0, _cut_short(client, mark)

    if pc:  # one PC's whole list, named by the person who typed it
        mine, secs, cut = read("software: one PC", filter=f"{D} eq {odata_quote(pc)}", select=list(c.values()))
        if mine is not None:
            kind = lambda r: ", ".join(str(r.get(c[k])) for k in ("type", "scope") if k in c)  # noqa: E731
            checks.append(Check("Aternity", "software: one PC", None,
                                f"{pc}: {len(mine)} rows ({secs:.1f} s). On that PC, compare with Settings > Apps "
                                "> Installed apps: is each app there also here?",
                                rows=[f"{r.get(N)} {r.get(V)} ({kind(r)})" for r in
                                      sorted(mine, key=lambda r: str(r.get(N)).lower())] + cut))
        return checks

    # its size, added up per name and version by Aternity
    agg, secs, cut = read(item, select=[N, V, P], top=SW_PER_VERSION)
    if not agg:
        if agg is not None:
            checks.append(Check("Aternity", item, None, f"{S} has no rows."))
        return checks
    names: dict = {}
    for r in agg:
        per = names.setdefault(r.get(N), {})
        per[r.get(V)] = per.get(r.get(V), 0) + number(r.get(P))
    installs = sum(sum(per.values()) for per in names.values())
    listed, _, cut2 = read(item, select=[D], top=SW_ONE_NAME)
    total = len({r.get(D) for r in listed}) if listed else 0
    alone = ""
    if "scope" in c:
        got, _, cut3 = read(item, filter=f"{c['scope']} eq {odata_quote(SOFTWARE_USER_SCOPE)}", select=[N, P],
                            top=SW_PER_VERSION)
        cut2 += cut3
        if got is not None:
            alone = (f"; {len({r.get(N) for r in got}):,} programs installed for one user only, "
                     f"{sum(number(r.get(P)) for r in got):,} installs")
    checks.append(Check("Aternity", item, None,
                        f"{total:,} PCs, {len(names):,} programs, {installs:,} installs ({secs:.1f} s){alone}."
                        + (f" Not published: {', '.join(missing)}." if missing else ""), rows=cut + cut2))

    # 2+ versions of a program on one PC: asked per name only, does Aternity count each PC once?
    # Then installs above PCs are PCs with 2+ versions. Checked on the program with the most versions.
    item = "software: 2+ versions on one PC"
    byname, secs, cut = read(item, select=[N, P], top=SW_PER_VERSION)
    if byname:
        pcs_of = {r.get(N): number(r.get(P)) for r in byname}
        over = sorted(((sum(names[n].values()) - k, n, k) for n, k in pcs_of.items()
                       if n in names and sum(names[n].values()) > k), key=lambda x: (-x[0], str(x[1])))
        most = max(names, key=lambda n: (len(names[n]), sum(names[n].values())))
        one, _, cut2 = read(item, filter=f"{N} eq {odata_quote(most)}", select=[D, V], top=SW_ONE_NAME)
        text = (f"{len(over):,} programs have more installs than PCs, {sum(x[0] for x in over):,} more in all "
                f"({secs:.1f} s): a PC with 2 versions counts twice in installs, once in PCs.")
        if one and most in pcs_of:
            per_pc: dict = {}
            for r in one:
                per_pc.setdefault(r.get(D), set()).add(r.get(V))
            two = sum(1 for v in per_pc.values() if len(v) > 1)
            extra = sum(names[most].values()) - pcs_of[most]
            text += (f" Check on {most}: {extra:,} more installs than PCs; read PC by PC, {two:,} PCs have 2+ "
                     "versions" + (" (the same)." if extra == two else " (NOT the same: do not use this count)."))
        checks.append(Check("Aternity", item, None, text + (" Most:" if over else ""),
                            rows=[f"{n}: {x:,} more installs than its {k:,} PCs" for x, n, k in over[:5]] + cut + cut2))

    _windows(client, schema, inventory, read, c, checks)
    return checks


def _windows(client, schema, inventory, read, c, checks) -> None:
    """Windows on each PC (name, version, last updated) and each Windows update on the PCs
    of each Windows version: an update is for one Windows version, so "not on N PCs" counts
    only that version's PCs."""
    inv, dev = inventory
    o = {k: v for k, v in OS_COLUMNS.items() if inv in schema and v in schema[inv]}
    if not inv or not dev or not {"name", "version"} <= set(o):
        checks.append(Check("Aternity", "software: Windows on the PCs", None,
                            f"{inv or 'The inventory'} gives no {OS_COLUMNS['name']} and {OS_COLUMNS['version']}: "
                            "Windows updates cannot be counted per Windows version."))
        return
    mark, t0 = len(client.warnings), time.monotonic()
    try:
        rows = client.query(inv, select=[dev] + list(o.values()), top=SW_ONE_NAME, raw=True)
    except ApiError as exc:
        checks.append(Check("Aternity", "software: Windows on the PCs", None, f"error: {exc.short()}"))
        return
    secs, cut = time.monotonic() - t0, _cut_short(client, mark)
    win = {r.get(dev): f"{r.get(o['name'])} {r.get(o['version'])}" for r in rows}
    per = Counter(win.values())
    weeks: Counter = Counter()  # Monday of the week Windows was last updated; None: 8+ weeks ago
    if "updated" in o:
        now = datetime.now(timezone.utc)
        for r in rows:
            t = parse_time(r.get(o["updated"]))
            if t:
                weeks[None if (now - t).days > 8 * 7 else (t - timedelta(days=t.weekday())).date()] += 1
    dated = sum(weeks.values())
    week_line = ", ".join([f"older {weeks[None]:,}"] * (None in weeks)
                          + [f"{d:%b %d} {n:,}" for d, n in sorted((d, n) for d, n in weeks.items() if d)])
    checks.append(Check("Aternity", "software: Windows on the PCs", None,
                        f"{len(win):,} PCs in {inv} ({secs:.1f} s), {len(per)} Windows versions; last updated "
                        f"known on {dated:,}. PCs per Windows version, then per week Windows was last updated:",
                        rows=[f"{w}: {n:,} PCs" for w, n in per.most_common(4)]
                        + ([f"weeks (Monday): {week_line}"] if weeks else []) + cut))
    if "type" not in c:
        return
    got, secs, cut = read("software: Windows updates per Windows version",
                          filter=f"{c['type']} eq {odata_quote(SOFTWARE_OS_UPDATE)}", select=[c["device"], c["name"]],
                          top=SW_ONE_NAME * 2)
    if not got:
        if got is not None:
            checks.append(Check("Aternity", "software: Windows updates per Windows version", None,
                                f"No {SOFTWARE_OS_UPDATE} rows in {SOFTWARE_SOURCE}."))
        return
    has: dict = {}
    for r in got:
        has.setdefault(win.get(r.get(c["device"]), "not in the inventory"), Counter())[r.get(c["name"])] += 1
    lines = []
    for w, n in per.most_common(3):
        top = has.get(w, Counter()).most_common(3)
        lines += [f"{w} ({n:,} PCs): " + ("; ".join(f"{u} on {k:,}, not on {n - k:,}" for u, k in top) or "none")]
    checks.append(Check("Aternity", "software: Windows updates per Windows version", None,
                        f"{len(got):,} update rows on {len({r.get(c['device']) for r in got}):,} PCs ({secs:.1f} s). "
                        "For the 3 Windows versions on the most PCs, their 3 most common updates:",
                        rows=lines + cut))


VER_DAYS = 7  # versions: the days compared, as the Software tab will
VER_BACK = 400  # versions: how far back to look for the oldest day kept
LOG_BACK = 14  # versions: days of the change log read for updates


def verify_versions(settings: Settings, client: AternityClient) -> list[Check]:
    """Only the "versions: …" checks: each application's version on what users feel."""
    try:
        schema = parse_odata_metadata(client.http.request("GET", "/$metadata", headers={"Accept": "application/xml"}).text)
    except (ApiError, ET.ParseError) as exc:
        return [Check("Aternity", "connection", False, f"Could not read $metadata: {exc.short() if isinstance(exc, ApiError) else exc}",
                      "Check aternity.base_url, aternity.username and ATERNITY_PASSWORD.")]
    return _versions(settings, client, schema)


def _versions(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """Is Aternity's application version filled on app usage (crashes, hang time), on
    actions (Major) and on health events? How far back does each go, and does it come
    added up per version when the PC is not asked for? For the application with the most
    crashes: its versions, PCs, PCs with a crash and with hang time over VER_DAYS, as the
    Software tab will count them. Then the updates in the change log (before and after
    on the same PCs). No PC or user names."""
    checks: list[Check] = []
    now = datetime.now(timezone.utc)
    span = lambda col: _between(col, now - timedelta(days=VER_DAYS), now)  # noqa: E731
    filled = lambda v: v not in (None, "", *NO_VALUE)  # noqa: E731
    amount = lambda v: to_float(v) or 0  # noqa: E731

    def read(entity: str, item: str, **kw):
        mark, t0 = len(client.warnings), time.monotonic()
        try:
            rows = client.query(entity, raw=True, **kw)
        except ApiError as exc:
            checks.append(Check("Aternity", item, None, f"error: {exc.short()}"))
            return None, 0.0, []
        return rows, time.monotonic() - t0, _cut_short(client, mark)

    def oldest(entity: str, col: str) -> str:
        try:
            rows = client.query(entity, filter=f"{col} ge {odata_time(now - timedelta(days=VER_BACK))}", select=[col],
                                orderby=f"{col} asc", top=1, raw=True)
        except ApiError as exc:
            return f"the oldest day kept could not be read ({exc.short()})"
        t = parse_time(rows[0].get(col)) if rows else None
        return f"the oldest day kept is {t:%b %d, %Y}" if t else "no oldest day found"

    def usable(key: str, item: str) -> dict | None:
        spec = VERSION_SOURCES[key]
        entity = spec["entity"]
        missing = [v for k, v in spec.items() if k != "entity" and k not in VERSION_OPTIONAL
                   and v not in schema.get(entity, ())]
        if entity not in schema or missing:
            checks.append(Check("Aternity", item, None, f"{entity} is not published" if entity not in schema else
                                f"{entity} is published without {', '.join(missing)}"))
            return None
        return spec

    def first(rows, top) -> str:
        return f" (the first {len(rows):,})" if len(rows) >= top else ""

    # app usage: crashes and hang time per application version
    u = usable("usage", "versions: app usage")
    if u:
        E, A, V, D = u["entity"], u["application"], u["version"], u["device"]
        rows, secs, cut = read(E, "versions: app usage", filter=span(u["time"]),
                               select=[A, V, u["crashes"], u["hang_time"]], top=SW_PER_VERSION)
        if rows:
            apps: dict = {}
            for r in rows:
                x = apps.setdefault(r.get(A), [set(), 0.0])
                x[0].add(r.get(V))
                x[1] += amount(r.get(u["crashes"]))
            top = sorted(apps.items(), key=lambda kv: -kv[1][1])[:3]
            checks.append(Check("Aternity", "versions: app usage", None,
                                f"{E}, last {VER_DAYS} days, without PCs: {len(rows):,} rows"
                                f"{first(rows, SW_PER_VERSION)} ({secs:.1f} s); the version is filled on "
                                f"{sum(1 for r in rows if filled(r.get(V))):,}; {oldest(E, u['time'])}. "
                                "Applications with the most crashes:",
                                rows=[f"{a}: {len(x[0])} versions, {x[1]:,.0f} crashes" for a, x in top] + cut))
            if top and top[0][1][1] > 0:
                name = top[0][0]
                per, secs, cut = read(E, "versions: crashes by version",
                                      filter=f"{span(u['time'])} and {A} eq {odata_quote(name)}",
                                      select=[D, V, u["crashes"], u["hang_time"]], top=SW_ONE_NAME)
                if per:
                    pairs = {(r.get(D), r.get(V)) for r in per}
                    byv: dict = {}
                    for r in per:
                        x = byv.setdefault(r.get(V), [set(), set(), set()])
                        x[0].add(r.get(D))
                        if amount(r.get(u["crashes"])) > 0:
                            x[1].add(r.get(D))
                        if amount(r.get(u["hang_time"])) > 0:
                            x[2].add(r.get(D))
                    checks.append(Check(
                        "Aternity", "versions: crashes by version", None,
                        f"{name}, last {VER_DAYS} days, per PC: {len(per):,} rows{first(per, SW_ONE_NAME)} for "
                        f"{len(pairs):,} PC-and-version pairs ({secs:.1f} s): "
                        + ("one row per pair, Aternity adds up the days" if len(per) == len(pairs)
                           else "several rows per pair (one per day?)")
                        + f". {len(byv)} versions; per version, the PCs that ran it, with a crash, with hang time:",
                        rows=[f"{v}: {len(x[0]):,} PCs, {len(x[1]):,} with a crash, {len(x[2]):,} with hang time"
                              for v, x in sorted(byv.items(), key=lambda kv: -len(kv[1][0]))[:5]] + cut))

    # actions: Aternity's Major per application version
    a = usable("actions", "versions: actions")
    if a:
        E, A, V = a["entity"], a["application"], a["version"]
        rows, secs, cut = read(E, "versions: actions", filter=span(a["time"]),
                               select=[A, V, a["status"], a["volume"]], top=SW_PER_VERSION)
        if rows:
            apps = {}
            for r in rows:
                n = to_float(r.get(a["volume"]))
                n = 1 if n is None else n
                x = apps.setdefault(r.get(A), [{}, 0.0, 0.0])
                v = x[0].setdefault(r.get(V), [0.0, 0.0])
                v[0] += n
                x[1] += n
                if r.get(a["status"]) == TOP_STATUS:
                    v[1] += n
                    x[2] += n
            top = sorted(apps.items(), key=lambda kv: -kv[1][2])[:3]
            lines = [f"{name}: {len(x[0])} versions, {x[2]:,.0f} {TOP_STATUS} of {x[1]:,.0f} actions" for name, x in top]
            if top:
                lines += [f"  {top[0][0]} {v}: {n[1]:,.0f} {TOP_STATUS} of {n[0]:,.0f}" for v, n in
                          sorted(top[0][1][0].items(), key=lambda kv: -kv[1][0])[:4]]
            checks.append(Check("Aternity", "versions: actions", None,
                                f"{E}, last {VER_DAYS} days, without PCs: {len(rows):,} rows"
                                f"{first(rows, SW_PER_VERSION)} ({secs:.1f} s); the version is filled on "
                                f"{sum(1 for r in rows if filled(r.get(V))):,}; {oldest(E, a['time'])}. "
                                f"Applications with the most {TOP_STATUS} actions, then the first one per version:",
                                rows=lines + cut))

    # health events: the failing part's version
    cfg = settings.aternity
    he, h = cfg.entities.get("health_events"), cfg.fields.get("health_events", {})
    cols = [h.get(k) for k in ("name", "application", "component", "component_version")]
    if he in schema and all(cols) and set(cols) <= schema[he] and h.get("timestamp"):
        rows, secs, cut = read(he, "versions: health events",
                               filter=_between(h["timestamp"], now - timedelta(hours=24), now), select=cols,
                               top=SW_SAMPLE)
        if rows is not None:
            kinds = Counter(tuple(str(r.get(col)) for col in cols) for r in rows)
            checks.append(Check("Aternity", "versions: health events", None,
                                f"{len(rows):,} health events in the last 24 hours{first(rows, SW_SAMPLE)} "
                                f"({secs:.1f} s); the failing part's version is filled on "
                                f"{sum(1 for r in rows if filled(r.get(cols[3]))):,}. Most common (event · "
                                "application · part and version):",
                                rows=[f"{k[0]} · {k[1]} · {k[2]} {k[3]}: {n}" for k, n in kinds.most_common(4)] + cut))

    # the change log: updates, for before and after on the same PCs
    ce, ch = cfg.entities.get("software_changes"), cfg.fields.get("software_changes", {})
    pcs = SOFTWARE_COLUMNS["pcs"]
    cols = [ch.get(k) for k in ("name", "change", "version_before", "version")]
    if ce in schema and all(cols) and set(cols + [pcs]) <= schema[ce] and ch.get("timestamp"):
        rows, secs, cut = read(ce, "versions: updates in the change log",
                               filter=_between(ch["timestamp"], now - timedelta(days=LOG_BACK), now),
                               select=cols + [pcs], top=SW_PER_VERSION)
        if rows:
            kinds, before, ups = Counter(), Counter(), Counter()
            for r in rows:
                n = int(amount(r.get(pcs))) or 1
                kinds[r.get(cols[1])] += n
                if filled(r.get(cols[2])):
                    before[r.get(cols[1])] += n
                    if r.get(cols[2]) != r.get(cols[3]):
                        ups[(r.get(cols[0]), r.get(cols[2]), r.get(cols[3]))] += n
            checks.append(Check("Aternity", "versions: updates in the change log", None,
                                f"Last {LOG_BACK} days, without PCs: {len(rows):,} rows{first(rows, SW_PER_VERSION)} "
                                f"({secs:.1f} s). Changes per type, with the version before filled: "
                                + ", ".join(f"{k} {n:,} ({before[k]:,})" for k, n in kinds.most_common())
                                + ". Updates (before to after) on the most PCs:",
                                rows=[f"{n0}: {b} to {v}: {k:,} PCs" for (n0, b, v), k in ups.most_common(4)] + cut))
    return checks


def _overview(settings: Settings, client: AternityClient, schema: dict[str, set[str]]) -> list[Check]:
    """What a "today's issues" overview could rest on: how much activity there is to
    read, which status and severity words Aternity itself uses (never ours), and
    whether it publishes its own incidents and alerts."""
    cfg, now, checks = settings.aternity, datetime.now(timezone.utc), []

    def per_value(rows, column, device) -> list[str]:
        by: dict = {}
        for r in rows:
            by.setdefault(repr(r.get(column)), []).append(r.get(device))
        return [f"{value}: {len(devs)} rows on {len(set(devs))} PCs"
                for value, devs in sorted(by.items(), key=lambda kv: -len(kv[1]))]

    # activity: how many rows an hour (the load of reading a day), and Aternity's status words
    act, a = cfg.entities.get("activities"), cfg.fields.get("activities", {})
    if act in schema and a.get("status") in schema[act]:
        try:
            for minutes in (60, 10):  # an hour; if that is cut short, 10 minutes for the rate
                mark = len(client.warnings)
                rows = client.query(act, _between(a["timestamp"], now - timedelta(minutes=minutes), now),
                                    select=[a["status"], a["device"]], raw=True)
                cut = _cut_short(client, mark)
                if not cut:
                    break
            rate = len(rows) * 60 // minutes
            checks.append(Check("Aternity", "overview: activity to read", None,
                                f"{len(rows)} activity rows in the last {minutes} min"
                                + (f" (about {rate} an hour)" if minutes != 60 else "")
                                + f"; a working day of 10 such hours is about {rate * 10} rows. "
                                f"Aternity's status ({a['status']}) on them:",
                                rows=per_value(rows, a["status"], a["device"]) + cut))
        except ApiError as exc:
            checks.append(Check("Aternity", "overview: activity to read", None, f"error: {exc.short()}"))

    # health events: Aternity's severity words, and what the biggest events are called
    ev, h = cfg.entities.get("health_events"), cfg.fields.get("health_events", {})
    if ev in schema and h.get("severity") in schema[ev]:
        try:
            mark = len(client.warnings)
            rows = client.query(ev, _between(h["timestamp"], now - timedelta(hours=24), now),
                                select=[h[k] for k in ("device", "severity", "category", "name")], raw=True)
            top: dict = {}
            for r in rows:
                key = (r.get(h["category"]), r.get(h["name"]))
                top.setdefault(key, []).append(r)
            widest = sorted(top.items(), key=lambda kv: -len({r.get(h["device"]) for r in kv[1]}))[:8]
            checks.append(Check("Aternity", "overview: health events", None,
                                f"{len(rows)} health event rows in the last 24 hours. Aternity's severity "
                                f"({h['severity']}) on them, then the 8 events on the most PCs:",
                                rows=per_value(rows, h["severity"], h["device"])
                                + [f"{cat} / {name}: {len({r.get(h['device']) for r in rs})} PCs, severity "
                                   f"{_values(rs, h['severity'], 3)}" for (cat, name), rs in widest]
                                + _cut_short(client, mark)))
        except ApiError as exc:
            checks.append(Check("Aternity", "overview: health events", None, f"error: {exc.short()}"))

    # Aternity's label on each failing part ("OUTLOOK.EXE (Process)", "<model> (Disk)"): the ones
    # read as the PC's disk failing (DISK_PARTS), and every other, so other hardware is added from them
    if ev in schema and h.get("component") in schema[ev]:
        try:
            mark = len(client.warnings)
            rows = client.query(ev, _between(h["timestamp"], now - timedelta(days=7), now),
                                select=[h[k] for k in ("device", "name", "component")], raw=True)
            by: dict = {}
            for r in rows:
                by.setdefault(part_label(r.get(h["component"])) or "(no label)", []).append(r)
            disk = {p.lower() for p in DISK_PARTS}
            checks.append(Check("Aternity", "overview: failing-part labels", None,
                                f"{len(rows)} health event rows in the last 7 days, by Aternity's label on the "
                                f"failing part ({h['component']}). Read as a failing disk: "
                                f"{', '.join(DISK_PARTS)}. Send this list if another label is hardware:",
                                rows=[f"{label}: {len({r.get(h['device']) for r in rs})} PCs, events "
                                      f"{_values(rs, h['name'], 3)}" + (" (read as a failing disk)"
                                                                        if label.lower() in disk else "")
                                      for label, rs in sorted(by.items(), key=lambda kv: -len(kv[1]))]
                                + _cut_short(client, mark)))
        except ApiError as exc:
            checks.append(Check("Aternity", "overview: failing-part labels", None, f"error: {exc.short()}"))

    # Aternity's own incidents and alerts: published? what do they say?
    for entity, spec in OVERVIEW_SOURCES.items():
        item = f"overview: {entity}"
        if entity not in schema:
            checks.append(Check("Aternity", item, None, "Not published by this Aternity: an overview cannot "
                                                        "use it."))
            continue
        have = [c for c in spec["values"] if c in schema[entity]]
        missing = [c for c in spec["values"] if c not in schema[entity]]
        if spec["time"] not in schema[entity]:
            checks.append(Check("Aternity", item, None, f"Published, but without its time column "
                                f"{spec['time']}. Its columns: {', '.join(sorted(schema[entity]))[:300]}"))
            continue
        try:
            rows = client.query(entity, _between(spec["time"], now - timedelta(days=7), now),
                                select=[spec["time"]] + have, top=500, raw=True)
        except ApiError as exc:
            checks.append(Check("Aternity", item, None, f"Published, but reading it gave an error: {exc.short()}"))
            continue
        per = Counter(t.date() for t in (parse_time(r.get(spec["time"])) for r in rows) if t)
        days = ", ".join(f"{d.strftime('%b %d')} {n}" for d, n in sorted(per.items()))
        checks.append(Check("Aternity", item, None,
                            f"Published. {len(rows)} rows in the last 7 days{' (the first 500)' if len(rows) == 500 else ''}"
                            + (f"; per day: {days}." if rows else ": nothing to show from it.")
                            + (f" Not published: {', '.join(missing)}." if missing else ""),
                            rows=[f"{c}: {_values(rows, c, 6)}" for c in have] if rows else []))
    return checks


AI = "AI service"
# question, and the building block (tool) and settings a right pick has (words: in it, any case)
AI_TRIES = (
    ("Which crashes hit the most PCs in the last 7 days?", {"tool": "events", "group_by": "event", "days": 7}),
    ("Are virtual desktops slower than physical PCs today?", {"tool": "slow_actions", "group_by": "pc_kind"}),
    ("Which users had the most slow actions in Microsoft Outlook today?",
     {"tool": "who", "what": "slow", "app": "outlook"}),
    ("Investigate the user mossley", {"tool": "investigate", "username": "mossley"}),
)
AI_SW_TRIES = (  # the Software tab's chat (software_chat.TOOLS)
    ("Which programs are most out of date?", {"tool": "flags", "kind": "behind"}),
    ("Does one Outlook version crash more than the others?", {"tool": "version_problems", "application": "outlook"}),
    ("Compare Outlook 16.0.19725 with 16.0.20430.", {"tool": "compare", "application": "outlook"}),
    ("Did Outlook get worse after its last update?", {"tool": "before_after", "application": "outlook"}),
    ("Do Outlook crashes depend on the Teams Meeting Add-in version?",
     {"tool": "program_split", "application": "outlook", "program": "teams"}),
    ("Which PCs still run Edge 143?", {"tool": "pcs", "program": "edge", "version": "143"}),
    ("What versions of the Teams Meeting Add-in are installed?", {"tool": "versions", "program": "teams"}),
    ("Which PCs crashed in Outlook 16.0.20430?",
     {"tool": "pcs", "application": "outlook", "version": "16.0.20430"}),  # problem: crash, the default
    ("What are the red flags?", {"tool": "flags", "kind": ""}),  # all of them: no kind
    ("Which PCs are missing KB5126652?", {"tool": "pcs_without_update", "update": "kb5126652"}),
)
AI_OUTSIDE = "What is the weather in Paris today?"  # not in the data: no pick is the right answer
AI_COUNTS = {"period": "last 7 days", "partial": False,  # made-up counts, to see if they are repeated right
             "rows": [{"failure": "Sample Agent crash", "pcs": 2885}, {"failure": "Sample Viewer crash", "pcs": 412}]}
AI_SETUP = ("Chat is off: no AI service is set. Add to config.yaml, per service: ai: / services: / "
            "- name: <your label> / base_url: <its API address, up to and including /v1> / model: <the "
            "model's name there> / key_env: <the .env line that holds its key>. Layout: config.example.yaml.")


def _picked(call) -> str:
    if not call:
        return "made no pick"
    return f"picked {call[0]}({', '.join(f'{k}={v}' for k, v in call[1].items())})"


def _right(call, want: dict) -> bool:
    if not call or call[0] != want["tool"]:
        return False
    for key, value in want.items():
        if key == "tool":
            continue
        got = str(call[1].get(key) or "").strip().lower()  # null and left out are the same
        if (value not in got) if isinstance(value, str) and value and key in (
                "app", "username", "application", "program", "version", "update") else (got != str(value)):
            return False
    return True


def verify_ai(settings: Settings, transport=None, env=None) -> list[Check]:
    """Each AI service in ai.services: key, reachable, a plain reply, picking the right
    building block for a question, and repeating the counts it is given."""
    import json
    import os
    import time

    from .ai import LOGINS, AiClient, reply_message, tool_call
    from . import software_chat
    from .chat import SYSTEM, TOOLS
    env = os.environ if env is None else env
    if not settings.ai.services:
        return [Check(AI, "services", None, AI_SETUP)]
    checks: list[Check] = []
    for n, cfg in enumerate(settings.ai.services, 1):
        name = cfg.name or f"service {n}"
        add = lambda item, ok, message, fix="", rows=(): checks.append(  # noqa: E731
            Check(AI, f"{name}: {item}", ok, message, fix, rows=list(rows)))
        where = f"ai.services, the entry named \"{name}\""
        if not cfg.base_url:
            add("settings", False, "base_url not set.", f"Set base_url in config.yaml ({where}).")
            continue
        key = env.get(cfg.key_env, "") if cfg.key_env else ""
        add("key", None, f"{cfg.key_env} is set (not shown)." if key else
            f"No key: {cfg.key_env or 'key_env'} is not set in .env. Trying without one. If the service needs "
            "a key: add the line to .env, then `docker compose up -d` (a restart does not re-read .env).")

        # reachable, and which login header it takes
        client, models, listed, refused = None, [], False, None
        for login in ([cfg.login] if cfg.login else LOGINS) if key else [""]:
            client = AiClient(cfg, key, login, transport)
            started = time.monotonic()
            try:
                models, listed = client.models(), True
            except ApiError as exc:
                if exc.status in (401, 403):
                    refused = exc
                    continue
                if exc.status is None:
                    add("reachable", False, f"No answer from the service: {exc.short()}.",
                        f"Check base_url in config.yaml ({where}) and that this host is allowed to reach the "
                        "AI service. Tell Claude this line.")
                    client = None
                else:  # it answered: only the model list is missing
                    add("reachable", None, f"The service answered, but has no model list at "
                        f"{cfg.models_path} ({exc.short()}). Going on with the model set.")
                refused = None
                break
            refused = None
            took = time.monotonic() - started
            how = f" Login: {login}." if login else ""
            add("reachable", True, f"Answered in {took:.1f} s.{how} {len(models)} models listed: "
                f"{', '.join(models[:12])}{' …' if len(models) > 12 else ''}.")
            if login and not cfg.login and login != LOGINS[0]:
                add("login", None, f"It takes the key in an \"{login}\" header. Add `login: {login}` to "
                    f"config.yaml ({where}) so the chat uses it too.")
            break
        if refused is not None:
            add("reachable", False, (f"The service refused the key ({refused.short()}), as a Bearer key and "
                                     "as an api-key header." if key and not cfg.login else
                                     f"The service refused the key ({refused.short()})." if key else
                                     f"The service wants a key ({refused.short()}) and none is set."),
                f"Check the .env line {cfg.key_env}, then `docker compose up -d` "
                "(a restart does not re-read .env).")
            continue
        if client is None:
            continue
        if not cfg.model:
            add("model", False, "model not set.", f"Set model in config.yaml ({where}) to one of the names "
                "listed above.")
            continue
        if listed and cfg.model not in models:
            close = _closest(cfg.model, models)
            add("model", False, f"\"{cfg.model}\" is not in the service's list.{_closest_text(close)}",
                f"Set model in config.yaml ({where}) to a listed name.")
            continue

        # a plain reply
        started = time.monotonic()
        try:
            raw = client.chat_raw([{"role": "user", "content": "Reply with the single word: ready"}])
        except ApiError as exc:
            fix = (f"No chat API at base_url + {cfg.chat_path}. Ask the AI service's admin for its chat "
                   f"address and set base_url ({where}) to the part before {cfg.chat_path}."
                   if exc.status == 404 else
                   f"The key was refused for chat. Try `login: api-key` (or `login: bearer`) in config.yaml "
                   f"({where}), and check the .env line {cfg.key_env}." if exc.status in (401, 403)
                   else "Tell Claude this line.")
            add("plain reply", False, f"The model did not reply: {exc.short()}.", fix)
            continue
        message = reply_message(raw)
        if message is None:
            add("plain reply", False, f"The reply is not in the layout expected. Its keys: {_keys(raw)}.",
                "Tell Claude these keys: " + CODE_FIX.format(file="ai.py (reply_message)"))
            continue
        add("plain reply", True, f"Replied \"{_cut(str(message.get('content') or '').strip(), 60)}\" in "
            f"{time.monotonic() - started:.1f} s.")

        # picks the right building block: the Today tab's chat, then the Software tab's
        def picks(label: str, system: str, tools: list, tries: tuple) -> tuple | None:
            """(question, reply, call) of the first right pick; None when none is right, False when
            the service refused tool calling."""
            ask = lambda q: [{"role": "system", "content": system}, {"role": "user", "content": q}]  # noqa: E731
            right, rows, first, times, stop = 0, [], None, [], None
            for question, want in tries:
                started = time.monotonic()
                try:
                    reply = client.chat(ask(question), tools)
                except ApiError as exc:
                    stop = exc
                    break
                times.append(time.monotonic() - started)
                call = tool_call(reply)
                if _right(call, want):
                    right += 1
                    first = first or (question, reply, call)
                else:
                    rows.append(f"\"{question}\": {_picked(call)}; a right pick is {want['tool']}("
                                f"{', '.join(f'{k}={v}' for k, v in want.items() if k != 'tool')})")
            if stop is not None:
                add(label, False, "The service refused a question sent with our building "
                    f"block (tool calling): {stop.short()}.", "This model or service may not do tool calling: the "
                    "chat cannot use it as it is. Tell Claude this line.")
                return False
            outside = None
            try:
                outside = tool_call(client.chat(ask(AI_OUTSIDE), tools))
            except ApiError as exc:
                rows.append(f"\"{AI_OUTSIDE}\": {exc.short()}")
            if outside:
                rows.append(f"\"{AI_OUTSIDE}\": {_picked(outside)}; it should make no pick")
            add(label, right == len(tries) and not outside,
                f"Right in {right} of {len(tries)} tries; for the 1 question outside the data it "
                f"{'made a pick (wrong)' if outside else 'made no pick (right)'}. Replies took "
                f"{min(times):.1f} to {max(times):.1f} s.",
                "The chat would run the wrong counts with this model. Tell Claude these lines.", rows)
            return first

        first = picks("picks the right question", SYSTEM, TOOLS, AI_TRIES)
        if first is False:
            continue
        picks("picks the right question (Software tab)", software_chat.system({"as_of_local": "today",
                                                                               "zone": "local time"}),
              software_chat.TOOLS, AI_SW_TRIES)
        ask = lambda q: [{"role": "system", "content": SYSTEM}, {"role": "user", "content": q}]  # noqa: E731

        # repeats the counts it is given
        if first is None:
            add("repeats the counts", None, "Not tried: it needs one right pick first.")
            continue
        question, reply, call = first
        try:
            answer = client.chat(ask(question) + [
                {"role": "assistant", "content": reply.get("content") or "", "tool_calls": reply["tool_calls"]},
                {"role": "tool", "tool_call_id": call[2], "content": json.dumps(AI_COUNTS)}], TOOLS)
        except ApiError as exc:
            add("repeats the counts", False, f"The service refused the counts sent back to it: {exc.short()}.",
                "Tell Claude this line.")
            continue
        text = str(answer.get("content") or "")
        digits = "".join(ch for ch in text if ch not in ",.   ")
        want = [str(r["pcs"]) for r in AI_COUNTS["rows"]]
        found = [w for w in want if w in digits]
        add("repeats the counts", len(found) == len(want),
            f"Given {' and '.join(want)}, its answer had {len(found)} of the {len(want)} counts.",
            "The chat would show wrong numbers in its sentence. Tell Claude these lines.",
            [f"its answer: {_cut(' '.join(text.split()), 220)}"])
    return checks


def render_checks(checks: list[Check]) -> str:
    mark = {True: "OK  ", False: "FAIL", None: "--  "}
    lines = []
    for system in ("Aternity", "NetIM", AI):
        rows = [c for c in checks if c.system == system]
        if not rows:
            continue
        lines.append(f"{system}:")
        for c in rows:
            lines.append(f"  {mark[c.ok]} {c.item}")
            lines += textwrap.wrap(c.message, 72, initial_indent="       ", subsequent_indent="       ")
            if c.ok is False and c.fix:
                lines += textwrap.wrap(c.fix, 72, initial_indent="       fix: ",
                                       subsequent_indent="            ")
            if c.paste:
                lines.append("       put in config.yaml (under the existing top-level key):")
                lines += [f"         {line}" for line in c.paste.splitlines()]
            for row in c.rows:
                lines += textwrap.wrap(row, 72, initial_indent="       · ", subsequent_indent="         ")
    failed = sum(c.ok is False for c in checks)
    lines.append("")
    lines.append(f"{failed} problem(s) found." if failed else "All checks passed.")
    return "\n".join(lines)
