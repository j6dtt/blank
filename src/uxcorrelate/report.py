"""Plain-text rendering of an investigation result."""

from __future__ import annotations


def _bar(score: float, width: int = 20) -> str:
    filled = round(score * width)
    return "#" * filled + "." * (width - filled)


def _t(iso: str | None) -> str:
    return f"{iso[:10]} {iso[11:16]} UTC" if iso else ""


def _start_line(st: dict, unit: str) -> str:
    vals = "  ".join(f"{t} {v['duration']}{unit}" + (f" (usual {v['usual']}{unit})" if v["usual"] is not None else "")
                     for t, v in st["values"].items())
    return f"{_t(st['time'])}  {st['username'] or '?'}  {st['device']}  {st['location'] or '?'}  {vals}"


def _answer_lines(a: dict, who: str) -> list[str]:
    """The plain-language part, same order as the web page."""
    lines = [f"ANSWER: {a['title']}"]
    if a["reason"]:
        lines.append(f"  {a['reason']}")
    f = a["felt"]
    lines += ["", "What the user felt:"]
    if f["slow_actions"]:
        apps = ", ".join(f"{app} {n}" for app, n in f["apps"])
        lines.append(f"  {f['slow_actions']} of their {f['actions']} actions were slow ({apps}),")
        lines.append(f"  at {_t(f['first'])}" if f["first"] == f["last"] else
                     f"  between {_t(f['first'])} and {_t(f['last'])}")
        s = f["slowest"]
        if s:
            lines.append(f"  slowest: {s['seconds']} s in {s['app']}" + (f" ({s['activity']})" if s["activity"] else "")
                         + f" at {_t(s['when'])}")
    else:
        lines.append(f"  no slow actions ({f['actions']} measured)")
    if f["logon"]:
        unit = f" {f['logon_unit']}" if f["logon_unit"] else ""
        lines.append(f"  slowest start-up: {_start_line(f['logon'], unit)}")
    tw = a["time_went"]
    if tw:
        lines += ["", "Where Aternity measured the slow time:"]
        for label, key in (("PC", "pc"), ("Network", "network"), ("Server", "server")):
            lines.append(f"  {label:<8} {_bar(tw[key])} {tw[key]:.0%}")
    if a["who_else"]:
        lines += ["", "Who else is affected?", f"  {a['who_else']}"]
    if a["found"]:
        lines += ["", "What we found, and why:"]
        for x in a["found"]:
            lines.append(f"  - {x['text']}" + (f": {x['count']}, first {_t(x['when'])}" if x["count"] else ""))
            if x.get("why"):
                lines.append("    Why:")
                lines += [f"      * {w}" for w in x["why"]]
    if a["todo"]:
        lines += ["", "What to do:"] + [f"  - {x}" for x in a["todo"]]
    return lines


def render_text(result: dict) -> str:
    req, summ = result["request"], result["summary"]
    who = req["username"] or req["device"]
    lines = [
        f"User experience investigation: {who}"
        + (f" / {req['application']}" if req["application"] else ""),
        f"Window: {_t(req['start'])} -> {_t(req['end'])}",
        "",
    ]
    lines += _answer_lines(result["answer"], who)
    lines += ["", "=" * 40, "Details for IT", "=" * 40,
              f"Slow = {summ['slow_rule']}.",
              f"A problem counts for a slow action up to {req['bucket_minutes']} min before or after it.",
              "Aternity UXI: " + (f"avg {summ['uxi_avg']}, lowest {summ['uxi_min']}"
                                  if summ.get("uxi_avg") is not None else "n/a")]
    if result["problems"]:
        lines += ["", "Every problem in the window:"]
        for g in result["problems"][:30]:
            at = (f"at {g['slow_actions']} of {g['of_slow']} slow actions" if g["slow_actions"]
                  else f"not near a slow action ({g['times']}x)")
            end = "" if g["last"] == g["first"] else f" to {_t(g['last'])}"
            lines += [f"  [{g['kind']}] {g['text']}", f"      {at}; {_t(g['first'])}{end}"]
    same, im = result["answer"].get("same_failure"), result.get("impact")
    if same and same["pcs"]:
        lines += ["", f"Other PCs with the same failure ({same['what']}):"]
        for p in same["list"][:20]:
            lines.append(f"  {p['device']}  {p['username'] or '?'}  version {p['version']}  "
                         f"{p['times']}x, last {_t(p['last'])}")
        if same["pcs"] > 20:
            lines.append(f"  ... {same['pcs'] - 20} more (see --json or the web UI)")
    elif im:
        lines += ["", f"Who else by network point ({', '.join(im['applications'])}, same times):"]
        for g in im["groups"][:8]:
            usual = "" if g["usual_rate"] is None else f", usually up to {g['usual_rate']:.0%}"
            mark = " <- this user" if g["includes_user"] else ""
            lines.append(f"  {g['slow']:>4} of {g['users']:<4} slow  {g['name']}{usual}{mark}")
        for u in im["affected_users"][:10]:
            lines.append(f"  - {u['username'] or u['device']}  {u['device']}  {u['network']}  "
                         f"slowest action {u['worst_response_time']} s in {u['worst_app']}")
        if im["affected_total"] > 10:
            lines.append(f"  ... {im['affected_total'] - 10} more (see --json or the web UI)")
    if result["endpoints"]:
        lines += ["", "PCs:"]
        for e in result["endpoints"]:
            lines.append(f"  {e['device']}  ip={e['ip_address'] or '?'}  "
                         f"location={e['location'] or '?'}  os={e['os'] or '?'}")
    if result["mapped_objects"]:
        lines += ["", "NetIM objects checked:"]
        for m in result["mapped_objects"]:
            lines.append(f"  [{m['role']:<18}] {m['object_type']:<9} {m['name']}  ({m['reason']})")
    if result["warnings"]:
        lines += ["", "Warnings:"]
        lines += [f"  ! {w}" for w in result["warnings"]]
    return "\n".join(lines)


def render_logons(lg: dict) -> str:
    """Plain-text slow start-ups report."""
    f, unit = lg["filters"], f" {lg['unit']}" if lg.get("unit") else ""
    who = ", ".join(f"{k} {v}" for k, v in f.items() if v) or "everyone"
    lines = [f"Start-ups (boots and logons): {who}", f"Window: {_t(lg['start'])} -> {_t(lg['end'])}", ""]
    if not lg["total"]:
        return "\n".join(lines + ["No boots or logons in the window."])
    lines.append(f"{lg['total']} start-ups. Typical and 1-in-10 slowest, per Aternity measurement:")
    for t in lg["types"]:
        lines.append(f"  {t['type']}: typical {t['typical']}{unit}, 1 in 10 over {t['p90']}{unit} "
                     f"({t['count']} measured)")
    if lg.get("causes"):
        lines += ["", "Main causes (parts longer than normal, and on how many PCs):"]
        for i, c in enumerate(lg["causes"], 1):
            where = ", ".join(f"{k} {n}" for k, n in c["by_location"].items())
            lines.append(f"  {i}. {c['component']} ({c['type']}): {c['pcs']} PC(s), {c['times']} time(s); {where}")
            for p in c["list"][:5]:
                lines.append(f"       {p['device']}  {p['username'] or '?'}  {p['location'] or '?'}  "
                             f"{p['times']}x, up to {p['worst']}{unit} longer than normal")
    lines += ["", f"Slowest start-ups (by {lg['sort_type']}):"]
    for st in lg["starts"][:20]:
        lines.append(f"  {_start_line(st, unit)}")
        for c in st["components"][:3]:
            extra = f", {c['degradation']}{unit} longer than normal" if c["degradation"] else ""
            lines.append(f"      {c['component']} ({c['type']}): {c['duration']}{unit}{extra}")
        for w in st.get("why", []):
            lines.append(f"      why: {w}")
    if len(lg["by_location"]) > 1:
        lines += ["", f"Locations, slowest first (typical {lg['sort_type']}):"]
        for g in lg["by_location"][:10]:
            lines.append(f"  {g['location']:<24} {g['typical'].get(lg['sort_type'], '-')}{unit}  "
                         f"({g['count']} start-ups)")
    for w in lg.get("warnings", []):
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)
