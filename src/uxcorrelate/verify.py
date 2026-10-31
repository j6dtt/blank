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
import textwrap
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from .aternity import AternityClient, odata_quote, odata_time
from .config import DISK_FREE_COLUMNS, Settings
from .http import ApiError
from .models import parse_time, to_float
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
    }
    cfg = settings.aternity
    end = datetime.now(timezone.utc)
    checks = []
    for key in ("activities", "app_usage", "boots", "disk_space"):
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
    return checks + _value_ranges(settings, client, schema) + _start_up_parts(settings, client, schema) \
        + _change_log(settings, client, schema)


def _raw(row: dict, fields: dict, keys) -> str:
    """One row as 'key=value' for the configured columns, values exactly as Aternity sent them."""
    return ", ".join(f"{k}={row.get(fields[k])!r}" for k in keys if fields.get(k))


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
        boots = client.query(be, f"{bf['timestamp']} ge {odata_time(since)}", top=500)
        parts = client.query(ce, f"{cf['timestamp']} ge {odata_time(since)}", top=500)
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
                              select=[bf[k] for k in ("timestamp", "started", "type", "duration") if bf.get(k)])
        mine_c = client.query(ce, f"{cf['timestamp']} ge {odata_time(since)} and {cf['device']} eq {odata_quote(dev)}",
                              select=[cf[k] for k in ("timestamp", "type", "degradation") if cf.get(k)])
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
        rows = client.query(entity, f"{f['timestamp']} ge {odata_time(since)}", top=300)
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
    return [Check("Aternity", "software change log", None,
                  f"{entity}, last 14 days: {len(rows)} row(s){' (first 300)' if len(rows) == 300 else ''}. "
                  "Change types: " + (", ".join(f"{k} ({n})" for k, n in kinds.most_common()) or "none")
                  + ". Its version/change/type columns: " + ", ".join(others) + ". " + pairs + " One row per change type:",
                  rows=[_raw(r, f, ("timestamp", "name", "change", "version", "version_before")) for r in sample.values()])]


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


def render_checks(checks: list[Check]) -> str:
    mark = {True: "OK  ", False: "FAIL", None: "--  "}
    lines = []
    for system in ("Aternity", "NetIM"):
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
