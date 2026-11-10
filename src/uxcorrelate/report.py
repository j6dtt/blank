"""Plain-text rendering of an investigation result."""

from __future__ import annotations

import re
import textwrap

from .models import local, parse_time

WIDTH = 72  # report lines wrap here, for narrow terminals


def _wrap(lines: list[str]) -> str:
    """Long lines wrapped, each continuation under its own text (after "- ", "* ", "! ")."""
    out = []
    for line in lines:
        m = re.match(r"(\s*(?:[-*!] )?)", line)
        lead = " " * len(m.group(1))
        out += textwrap.wrap(line, WIDTH, subsequent_indent=lead, break_on_hyphens=False) or [""]
    return "\n".join(out)


def _of(c: dict) -> str:
    """A start-up part's type in brackets, unless the part is named by its type."""
    return f" ({c['type']})" if c["type"] != c["component"] else ""


def _bar(score: float, width: int = 20) -> str:
    filled = round(score * width)
    return "#" * filled + "." * (width - filled)


def _t(iso: str | None) -> str:
    """A time in config.yaml's timezone, as the sentences' days (models.set_zone)."""
    return f"{local(parse_time(iso)):%Y-%m-%d %H:%M %Z}" if iso else ""


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
        apps = ", ".join(f"{app}: {n}" for app, n in f["apps"])
        lines.append(f"  {f['slow_actions']} of their {f['actions']} actions "
                     f"{'was' if f['slow_actions'] == 1 else 'were'} slow ({apps}),")
        lines.append(f"  at {_t(f['first'])}" if f["first"] == f["last"] else
                     f"  between {_t(f['first'])} and {_t(f['last'])}")
        pcs = f.get("pcs") or []
        if len(pcs) > 1:
            lines.append(f"  they used {len(pcs)} PCs: "
                         + ", ".join(f"{p['device']} ({_pc_kind(p)}{p['slow']} of {p['actions']} slow)" for p in pcs))
        elif pcs and pcs[0].get("virtual"):
            lines.append(f"  on virtual desktop {pcs[0]['device']}" + _pc_from(pcs[0]))
        s = f["slowest"]
        if s:
            lines.append(f"  slowest: {s['seconds']} s in {s['app']}" + (f" ({s['activity']})" if s["activity"] else "")
                         + (f" on {s['device']}" if s.get("device") else "") + f" at {_t(s['when'])}"
                         + (f", Aternity: {s['status']}" if s.get("status") else ""))
        if f.get("by_status"):
            lines.append("  Aternity marked them: " + ", ".join(f"{n} {k}" for k, n in f["by_status"].items()))
    else:
        lines.append(f"  no slow actions ({f['actions']} measured)")
        pcs = f.get("pcs") or []
        if len(pcs) > 1:
            lines.append(f"  they used {len(pcs)} PCs: "
                         + ", ".join(f"{p['device']} ({_pc_kind(p).rstrip('; ') or 'PC'})" for p in pcs))
        elif pcs and pcs[0].get("virtual"):
            lines.append(f"  on virtual desktop {pcs[0]['device']}" + _pc_from(pcs[0]))
    unit = f" {f['logon_unit']}" if f["logon_unit"] else ""
    for st in f["logons"]:
        quiet = f["slow_actions"] and any(p["device"] == st["device"] and not p["slow"]
                                          for p in f.get("pcs") or [])
        vals = ", ".join(f"{t} {v['duration']}{unit}" + (f" (usual {v['usual']}{unit})" if v["usual"] is not None
                                                          else "") for t, v in st["values"].items())
        lines.append(f"  {'slow start-up' if st.get('unusual') else 'start-up'}: {_t(st['time'])} on {st['device']}"
                     + (f" ({st['location']})" if st.get("location") else "") + f": {vals}"
                     + (" (no slow action on this PC)" if quiet else ""))
    tw = a["time_went"]
    if tw:
        lines += ["", "Where Aternity measured the slow time:"]
        for label, key in (("PC", "pc"), ("Network", "network"), ("Server", "server")):
            lines.append(f"  {label:<8} {_bar(tw[key])} {tw[key]:.0%}")
    if a["who_else"]:
        lines += ["", "Other users of these apps:", f"  {a['who_else']}"]
    if a["found"]:
        lines += ["", "What went wrong:"]
        for x in a["found"]:
            lines.append(f"  - {x['text']}" + (f": {x['count']}, first {_t(x['when'])}" if x["count"] else ""))
            lines += [f"      * {w}" for w in x["why"]]
            lines += [f"      * Possible cause: {w}" for w in x["possible"]]
            if x["who"]:
                lines.append(f"      * Who else: {x['who']}")
    if a.get("elsewhere"):
        lines += ["", f"Other things on {a.get('pcs_label') or 'these PCs'} (not counted for a slow action):"]
        for x in a["elsewhere"]:
            lines.append(f"  - {x['text']}" + (f": {x['count']}, first {_t(x['when'])}" if x["count"] else ""))
            lines += [f"      * {w}" for w in x["why"]]
            lines += [f"      * Possible cause: {w}" for w in x["possible"]]
            if x["who"]:
                lines.append(f"      * Who else: {x['who']}")
    if a.get("changes"):
        devs = [c["device"] for c in a["changes"]]
        lines += ["", "What changed on " + (f"the {len(devs)} PCs" if len(devs) > 2 else " and ".join(devs)) + ":"]
        for c in a["changes"]:
            if not c["changes"]:
                lines.append(f"  {c['device']}: nothing in the window or the 14 days before it")
                continue
            lines.append(f"  {c['device']}:")
            lines += [f"    {_t(x['time'])}  {x['text']}" for x in c["changes"]]
            if c["more"]:
                lines.append(f"    ... {c['more']} more")
    if a.get("normal"):
        lines += ["", "Compared with normal:"]
        for c in a["normal"]:
            lines.append(f"  - {c[0]}")
            lines += [f"      {x}" for x in c[1:]]
    if a.get("checked"):
        lines += ["", "What was checked:"] + [f"  - {x}" for x in a["checked"]]
    if a["todo"]:
        lines += ["", "What to do:"] + [f"  - {x}" for x in a["todo"]]
    return lines


def _pc_from(p: dict) -> str:
    return f", from {', '.join(p['from'])}" if p.get("from") else ""


def _pc_kind(p: dict) -> str:
    """ "virtual desktop from X; " for a virtual desktop, else nothing."""
    return f"virtual desktop{_pc_from(p).replace(',', '', 1)}; " if p.get("virtual") else ""


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
                  else f"not near a slow action ({g['times']}x)" if g["of_slow"] else f"{g['times']}x")
            end = "" if g["last"] == g["first"] else f" to {_t(g['last'])}"
            lines += [f"  [{g['kind']}] {g['text']}", f"      {at}; {_t(g['first'])}{end}"]
    im = result.get("impact")
    for x in [x for x in result["answer"]["found"] + result["answer"].get("elsewhere", []) if x["list"]][:3]:
        lines += ["", f"Other PCs ({x['list_of']}):"]
        for p in x["list"][:10]:
            lines.append(f"  {p['device']}  {p['username'] or '?'}  {p['version']}  "
                         f"{p['times']}x, last {_t(p['last'])}")
        if len(x["list"]) > 10:
            lines.append("  ... more (see --json or the web UI)")
    if im:
        lines += ["", f"Other users by network point ({', '.join(im['applications'])}, same times):"]
        for g in im["groups"][:8]:
            usual = "" if g["usual_rate"] is None else f", usually up to {g['usual_rate']:.0%}"
            mark = " <- this user" if g["includes_user"] else ""
            lines.append(f"  {g['slow']:>4} of {g['users']:<4} slow  {g['name']}{usual}{mark}")
        for u in im["affected_users"][:10]:
            lines.append(f"  - {u['username'] or u['device']}  {u['device']}  {u['network']}  "
                         f"slowest action {u['worst_response_time']} s in {u['worst_app']}"
                         + (f" ({u['worst_status']})" if u.get("worst_status") else ""))
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
    return _wrap(lines)


def render_logons(lg: dict) -> str:
    """Plain-text slow start-ups report."""
    f, unit = lg["filters"], f" {lg['unit']}" if lg.get("unit") else ""
    who = ", ".join(f"{k} {v}" for k, v in f.items() if v) or "everyone"
    lines = [f"Start-ups (boots and logons): {who}", f"Window: {_t(lg['start'])} -> {_t(lg['end'])}", ""]
    if not lg["total"]:
        return _wrap(lines + ["No boots or logons in the window."])
    lines.append(f"{lg['total']} start-ups. Typical and 1-in-10 slowest, per Aternity measurement:")
    for t in lg["types"]:
        lines.append(f"  {t['type']}: typical {t['typical']}{unit}, 1 in 10 over {t['p90']}{unit} "
                     f"({t['count']} measured)")
    if lg.get("causes"):
        lines += ["", "Main causes (parts longer than normal, and on how many PCs):"]
        for i, c in enumerate(lg["causes"], 1):
            where = ", ".join(f"{k} {n}" for k, n in c["by_location"].items())
            lines.append(f"  {i}. {c['component']}{_of(c)}: {c['pcs']} PC(s), {c['times']} time(s); {where}")
            for p in c["list"][:5]:
                lines.append(f"       {p['device']}  {p['username'] or '?'}  {p['location'] or '?'}  "
                             f"{p['times']}x, up to {p['worst']}{unit} longer than normal")
    lines += ["", f"Slowest start-ups (by {lg['sort_type']}):"]
    for st in lg["starts"][:20]:
        lines.append(f"  {_start_line(st, unit)}")
        for c in st["components"][:3]:
            extra = f", {c['degradation']}{unit} longer than normal" if c["degradation"] else ""
            lines.append(f"      {c['component']}{_of(c)}: {c['duration']}{unit}{extra}")
        for w in st.get("why", []):
            lines.append(f"      why: {w}")
    if len(lg["by_location"]) > 1:
        lines += ["", f"Locations, slowest first (typical {lg['sort_type']}):"]
        for g in lg["by_location"][:10]:
            lines.append(f"  {g['location']:<24} {g['typical'].get(lg['sort_type'], '-')}{unit}  "
                         f"({g['count']} start-ups)")
    for w in lg.get("warnings", []):
        lines.append(f"WARNING: {w}")
    return _wrap(lines)
