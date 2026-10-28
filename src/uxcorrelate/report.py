"""Plain-text rendering of an investigation result."""

from __future__ import annotations


def _bar(score: float, width: int = 20) -> str:
    filled = round(score * width)
    return "#" * filled + "." * (width - filled)


def render_text(result: dict) -> str:
    req, summ = result["request"], result["summary"]
    who = req["username"] or req["device"]
    lines = [
        f"User experience investigation: {who}"
        + (f" / {req['application']}" if req["application"] else ""),
        f"Window: {req['start']} -> {req['end']} ({req['bucket_minutes']} min buckets)",
        "",
        f"VERDICT: {summ['verdict_label']}  (confidence {summ['confidence']:.2f})",
        f"  {summ['headline']}",
        f"  Samples: {summ['samples']}, degraded: {summ['degraded_samples']}, "
        f"degraded intervals: {summ['degraded_intervals']}",
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

    if result["recommendations"]:
        lines += ["", "Next steps:"]
        lines += [f"  - {r}" for r in result["recommendations"]]

    if result["warnings"]:
        lines += ["", "Warnings:"]
        lines += [f"  ! {w}" for w in result["warnings"]]
    return "\n".join(lines)
