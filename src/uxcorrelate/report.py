"""Plain-text rendering of an investigation result."""

from __future__ import annotations


def _bar(score: float, width: int = 20) -> str:
    filled = round(score * width)
    return "#" * filled + "." * (width - filled)


def _t(iso: str | None) -> str:
    return f"{iso[11:16]} UTC" if iso else ""


def _answer_lines(a: dict, who: str) -> list[str]:
    """The plain-language part, same order as the web page."""
    lines = [f"ANSWER: {a['title']}"]
    if a["reason"]:
        lines.append(f"  {a['reason']}")
    f = a["felt"]
    lines += ["", "What the user felt:"]
    if f["slow_actions"]:
        lines.append(f"  {f['slow_actions']} slow of {f['actions']} actions, {_t(f['first'])} to {_t(f['last'])}")
        s = f["slowest"]
        if s:
            lines.append(f"  slowest: {s['seconds']} s in {s['app']}" + (f" ({s['activity']})" if s["activity"] else "")
                         + f" at {_t(s['when'])}")
    else:
        lines.append(f"  no slow actions ({f['actions']} measured)")
    if f["logon"]:
        lg = f["logon"]
        usual = f", usually {lg['usual']}" if lg["usual"] is not None else ""
        lines.append(f"  slowest {lg['type'].lower()}: {lg['duration']} at {_t(lg['time'])}{usual} (Aternity's units)")
    tw = a["time_went"]
    if tw:
        lines += ["", "Where the time went (Aternity):"]
        for label, key in (("PC", "pc"), ("Network", "network"), ("Server", "server")):
            v = tw[key]
            lines.append(f"  {label:<8} {_bar(v or 0)} {'' if v is None else f'{v:.0%}'}")
    if a["just_them"]:
        lines += ["", "Is it just them?", f"  {a['just_them']}"]
    if a["found"]:
        lines += ["", "What we found:"]
        lines += [f"  - {x['text']}" + (f", first at {_t(x['when'])}" if x["when"] else "") for x in a["found"]]
    if a["todo"]:
        lines += ["", "What to do:"] + [f"  - {x}" for x in a["todo"]]
    return lines


def render_text(result: dict) -> str:
    req, summ = result["request"], result["summary"]
    who = req["username"] or req["device"]
    lines = [
        f"User experience investigation: {who}"
        + (f" / {req['application']}" if req["application"] else ""),
        f"Window: {req['start']} -> {req['end']}",
        "",
    ]
    lines += _answer_lines(result["answer"], who)
    lines += ["", "=" * 40, "Details for IT", "=" * 40,
              f"VERDICT: {summ['verdict_label']}  (confidence {summ['confidence']:.2f})",
              f"  {summ['headline']}",
              f"  Samples: {summ['samples']}, degraded: {summ['degraded_samples']}, "
              f"degraded intervals: {summ['degraded_intervals']} ({req['bucket_minutes']} min buckets)",
              "  Aternity UXI: "
              + (f"avg {summ['uxi_avg']}, lowest {summ['uxi_min']}" if summ.get("uxi_avg") is not None else "n/a")
              + f"; device health events: {summ.get('health_events', 0)}"]
    im = result.get("impact")
    if im:
        lines += ["", f"Who else by network point ({', '.join(im['applications'])}, same slow intervals):"]
        for g in im["groups"][:8]:
            usual = "" if g["usual_rate"] is None else f", usually up to {g['usual_rate']:.0%}"
            mark = " <- this user" if g["includes_user"] else ""
            lines.append(f"  {g['slow']:>4} of {g['users']:<4} slow  {g['name']}{usual}{mark}")
        for u in im["affected_users"][:10]:
            lines.append(f"  - {u['username'] or u['device']}  {u['device']}  {u['network']}  "
                         f"slowest action {u['worst_response_time']} s in {u['worst_app']}")
        if im["affected_total"] > 10:
            lines.append(f"  ... {im['affected_total'] - 10} more (see --json or the web UI)")
    lg = result.get("logons")
    if lg and lg["total"]:
        lines += ["", "Boots and logons in the window (Aternity's units):"] + _logon_lines(lg, 5)
    lines += [
        "",
        "Domain scores (score | overlap with degraded intervals | Aternity time share):",
    ]
    for s in result["scores"].values():
        share = "n/a" if s["breakdown_share"] is None else f"{s['breakdown_share']:.2f}"
        lines.append(f"  {s['label']:<20} [{_bar(s['score'])}] {s['score']:.2f} | "
                     f"{s['overlap']:.2f} | {share}")

    if result["endpoints"]:
        lines += ["", "Endpoints:"]
        for e in result["endpoints"]:
            lines.append(f"  {e['device']}  ip={e['ip_address'] or '?'}  "
                         f"location={e['location'] or '?'}  os={e['os'] or '?'}")

    if result["mapped_objects"]:
        lines += ["", "NetIM objects in scope:"]
        for m in result["mapped_objects"]:
            lines.append(f"  [{m['role']:<18}] {m['object_type']:<9} {m['name']}  ({m['reason']})")

    if result["evidence"]:
        lines += ["", "Evidence during degraded intervals:"]
        for ev in result["evidence"][:15]:
            worst = "" if ev["worst"] is None else f" worst={ev['worst']}"
            thr = "" if ev["threshold"] is None else f" (threshold {ev['threshold']})"
            lines.append(f"  {ev['coverage']*100:5.1f}%  {ev['domain']:<8} {ev['source']:<12} "
                         f"{ev['object']}: {ev['metric']}{worst}{thr}")

    if result["warnings"]:
        lines += ["", "Warnings:"]
        lines += [f"  ! {w}" for w in result["warnings"]]
    return "\n".join(lines)


def _logon_lines(lg: dict, limit: int) -> list[str]:
    lines = []
    for t in lg["types"]:
        lines.append(f"  {t['type']}: {t['count']} measured, median {t['median']}, "
                     f"90% under {t['p90']}, slowest {t['max']}")
    for b in lg["slowest"][:limit]:
        vs = f"{b['x_median']}x median" if b["x_median"] is not None else ""
        if b["x_usual"] is not None:
            vs += f", {b['x_usual']}x its usual {b['usual']}"
        lines.append(f"  - {b['time'][:16]}  {b['type']:<12} {b['duration']:>8}  {b['username'] or '?'}  "
                     f"{b['device']}  {b['location'] or '?'}  ({vs})")
        for c in b["components"][:3]:
            lines.append(f"      {c['component']}: degradation {c['degradation']}, duration {c['duration']}")
    return lines


def render_logons(lg: dict) -> str:
    """Plain-text slow boots/logons report."""
    f = lg["filters"]
    who = ", ".join(f"{k} {v}" for k, v in f.items() if v) or "everyone"
    lines = [f"Boots and logons: {who}", f"Window: {lg['start']} -> {lg['end']}  ({lg['total']} measured)",
             "Durations are in Aternity's units (uxcorrelate verify prints their range).", ""]
    if not lg["total"]:
        return "\n".join(lines + ["No boots or logons in the window."])
    lines += _logon_lines(lg, 20)
    lines += ["", "Slowest locations (median per type):"]
    for g in lg["by_location"][:10]:
        lines.append(f"  {g['type']:<12} {g['location']:<24} median {g['median']}  ({g['count']} measured)")
    for w in lg.get("warnings", []):
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)

