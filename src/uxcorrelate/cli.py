"""Command-line entry point: `uxcorrelate <command>`."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from .config import load_settings
from .correlate import InvestigationRequest, investigate
from .http import ApiError
from .models import parse_time
from .report import render_text


def parse_duration(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)\s*([mhd])", text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("use e.g. 30m, 4h, 2d")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def build_sources(settings, demo: bool):
    """Return (aternity, netim, default_start, default_end)."""
    if demo:
        from .demo import demo_sources
        return demo_sources(settings)
    from .aternity import AternityClient
    from .netim import NetimClient
    aternity = AternityClient(settings.aternity)
    netim = NetimClient(settings.netim) if settings.netim.base_url else None
    end = datetime.now(timezone.utc)
    return aternity, netim, end - timedelta(hours=4), end


def cmd_investigate(args) -> int:
    settings = load_settings(args.config)
    if args.bucket:
        settings.bucket_minutes = args.bucket
    aternity, netim, start, end = build_sources(settings, args.demo)
    if args.end:
        end = parse_time(args.end)
    if args.start:
        start = parse_time(args.start)
    elif args.last:
        start = end - args.last
    user = args.user or (None if args.device or not args.demo else "jdoe")
    app = args.app or ("CRM Portal" if args.demo else None)
    req = InvestigationRequest(start=start, end=end, username=user,
                               device=args.device, application=app)
    result = investigate(req, aternity, netim, settings)
    output = json.dumps(result, indent=2) if args.json else render_text(result)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(output)
        print(f"Wrote {args.out}")
    else:
        print(output)
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from .web import create_app
    uvicorn.run(create_app(args.config, demo=args.demo), host=args.host, port=args.port)
    return 0


def cmd_aternity_sample(args) -> int:
    from .aternity import AternityClient
    client = AternityClient(load_settings(args.config).aternity)
    rows = client.query(args.entity, filter=args.filter, top=args.top)
    print(json.dumps(rows, indent=2, default=str))
    if rows:
        print("\nColumns:", ", ".join(sorted(rows[0])), file=sys.stderr)
    return 0


def cmd_netim_check(args) -> int:
    from .netim import NetimClient
    client = NetimClient(load_settings(args.config).netim)
    devices = client.devices()
    print(f"NetIM reachable: {len(devices)} devices")
    for d in devices[: args.top]:
        print(f"  {d.id:<10} {d.name:<30} {d.ip_address or '':<16} {d.device_type or ''}")
    if args.metric_classes:
        print(json.dumps(client.metric_classes(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="uxcorrelate", description=__doc__)
    p.add_argument("-c", "--config", help="YAML config file (or set UXCORRELATE_CONFIG)")
    sub = p.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("investigate", help="correlate a user's experience with infrastructure")
    inv.add_argument("-u", "--user", help="Aternity username")
    inv.add_argument("-d", "--device", help="Aternity device (hostname)")
    inv.add_argument("-a", "--app", help="Aternity application name")
    inv.add_argument("--start", help="ISO-8601 start time (UTC if no offset)")
    inv.add_argument("--end", help="ISO-8601 end time (default: now)")
    inv.add_argument("--last", type=parse_duration, help="window length ending at --end, e.g. 4h")
    inv.add_argument("--bucket", type=int, help="correlation bucket size in minutes")
    inv.add_argument("--json", action="store_true", help="emit JSON instead of text")
    inv.add_argument("-o", "--out", help="write the report to a file")
    inv.add_argument("--demo", action="store_true", help="use built-in synthetic data")
    inv.set_defaults(func=cmd_investigate)

    srv = sub.add_parser("serve", help="run the web UI and REST API")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8080)
    srv.add_argument("--demo", action="store_true", help="use built-in synthetic data")
    srv.set_defaults(func=cmd_serve)

    smp = sub.add_parser("aternity-sample", help="print sample rows of an Aternity OData entity")
    smp.add_argument("entity", help="e.g. BUSINESS_ACTIVITIES_RAW")
    smp.add_argument("--filter", help="OData $filter")
    smp.add_argument("--top", type=int, default=3)
    smp.set_defaults(func=cmd_aternity_sample)

    chk = sub.add_parser("netim-check", help="verify NetIM connectivity and list devices")
    chk.add_argument("--top", type=int, default=20)
    chk.add_argument("--metric-classes", action="store_true", help="also dump metric classes")
    chk.set_defaults(func=cmd_netim_check)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ApiError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
