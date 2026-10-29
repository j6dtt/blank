"""Who else: other users of the same applications during the user's slow intervals.

Users are grouped by the network point they share: the NetIM interface whose
subnet holds their IP (the gateway), or their /24 when NetIM has no such
subnet. Aternity only knows the locations an admin configured; NetIM knows the
network, which is what lets slow users be traced to one router or subnet.

Each group, and the whole, is compared with the same clock hours on the
previous days: "unusual" means more users slow than on any of those days.
No other threshold is used. The common point is the group holding more than
half of the slow users, if it is unusual.
"""

from __future__ import annotations

import ipaddress
import logging
from collections import Counter
from datetime import datetime, timedelta

log = logging.getLogger("uxcorrelate")
BASELINE_DAYS = 3


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def gateway_index(netim) -> tuple[list[tuple], bool]:
    """[(network, "device interface")] from NetIM interfaces, most specific first.
    The second value is False when the shared NetIM index is not built yet."""
    if netim is None:
        return [], False
    cached = getattr(netim, "cached_interfaces", None)
    try:
        if cached is not None:
            by_device = cached()
            ready = bool(by_device)
        else:  # simple sources (tests) answer directly
            by_device = {d.id: netim.interfaces(d.id) for d in netim.devices()}
            ready = True
        names = {d.id: d.name for d in netim.devices()} if by_device else {}
    except Exception as exc:  # NetIM trouble must not stop the comparison
        log.warning("NetIM: interface index unavailable: %s", exc)
        return [], False
    nets = []
    for dev_id, itfs in by_device.items():
        for itf in itfs:
            for addr in itf.ip_addresses:
                if "/" not in addr:
                    continue
                try:
                    net = ipaddress.ip_interface(addr).network
                except ValueError:
                    continue
                if net.prefixlen < 32:
                    nets.append((net, f"{names.get(dev_id, dev_id)} {itf.name}"))
    nets.sort(key=lambda n: -n[0].prefixlen)
    return nets, ready


def group_of(ip: str | None, nets: list[tuple], cache: dict) -> tuple[str, str]:
    """(kind, name) of the network point an IP sits behind."""
    if not ip:
        return "unknown", "No IP address in Aternity"
    if ip not in cache:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            cache[ip] = ("unknown", "No IP address in Aternity")
            return cache[ip]
        hit = next((f"{label} ({net})" for net, label in nets if addr in net), None)
        if hit:
            cache[ip] = ("gateway", hit)
        else:
            net = ipaddress.ip_network(f"{ip}/24", strict=False) if addr.version == 4 else \
                ipaddress.ip_network(f"{ip}/64", strict=False)
            cache[ip] = ("subnet", f"{net} (not in NetIM)")
    return cache[ip]


def _per_user(rows, who: str, device: str | None, buckets: set, bucket, is_slow) -> dict[str, dict]:
    users: dict[str, dict] = {}
    for s in rows:
        key = s.username or s.device
        if not key or key == who or (device and s.device == device):
            continue
        if bucket(s.timestamp) not in buckets:
            continue
        u = users.setdefault(key, {"username": s.username, "device": s.device, "ips": Counter(),
                                   "locations": Counter(), "slow": [], "activities": 0})
        u["activities"] += 1
        if s.ip_address:
            u["ips"][s.ip_address] += 1
        if s.location:
            u["locations"][s.location] += 1
        if is_slow(s):
            u["slow"].append(s)
    for u in users.values():
        u["ip"] = u.pop("ips").most_common(1)[0][0] if u["ips"] else None
        u["location"] = u.pop("locations").most_common(1)[0][0] if u["locations"] else None
    return users


def _rate(slow: int, users: int) -> float | None:
    return round(slow / users, 3) if users else None


def who_else(aternity, netim, apps: list[str], degraded: set[datetime], minutes: int, bucket, is_slow,
             who: str, device: str | None, user_ip: str | None, user_location: str | None) -> dict | None:
    """Other users of `apps` in the `degraded` buckets, grouped and compared with previous days."""
    if not degraded or not apps or not hasattr(aternity, "app_activity"):
        return None
    start, end = min(degraded), max(degraded) + timedelta(minutes=minutes)
    log.info("Aternity: comparing with other users of %s, %s to %s UTC", ", ".join(apps),
             start.strftime("%H:%M"), end.strftime("%H:%M"))
    users = _per_user(aternity.app_activity(apps, start, end), who, device, degraded, bucket, is_slow)

    nets, ready = gateway_index(netim)
    cache: dict = {}
    for u in users.values():
        u["group"] = group_of(u["ip"], nets, cache)
    own_group = group_of(user_ip, nets, cache) if user_ip else None

    # the same clock hours on previous days
    history = []
    for day in range(1, BASELINE_DAYS + 1):
        shift = timedelta(days=day)
        log.info("Aternity: same hours %d day(s) earlier, for comparison", day)
        rows = aternity.app_activity(apps, start - shift, end - shift)
        old = _per_user(rows, who, device, {t - shift for t in degraded}, bucket, is_slow)
        for u in old.values():
            u["group"] = group_of(u["ip"], nets, cache)
        if old:
            history.append(old)

    def usual(members) -> float | None:
        rates = []
        for old in history:
            sel = [u for u in old.values() if members(u)]
            if sel:
                rates.append(sum(bool(u["slow"]) for u in sel) / len(sel))
        return round(max(rates), 3) if rates else None

    slow_total = sum(bool(u["slow"]) for u in users.values())
    rate = _rate(slow_total, len(users))
    usual_rate = usual(lambda u: True)
    unusual = None if usual_rate is None or rate is None else rate > usual_rate

    groups: dict[tuple, dict] = {}
    for u in users.values():
        g = groups.setdefault(u["group"], {"kind": u["group"][0], "name": u["group"][1], "users": 0, "slow": 0})
        g["users"] += 1
        g["slow"] += bool(u["slow"])
    for key, g in groups.items():
        g["rate"] = _rate(g["slow"], g["users"])
        g["usual_rate"] = usual(lambda u, key=key: u["group"] == key)
        g["unusual"] = None if g["usual_rate"] is None else g["rate"] > g["usual_rate"]
        g["includes_user"] = key == own_group
    ranked = sorted(groups.values(), key=lambda g: (-g["slow"], -g["users"]))
    top = ranked[0] if ranked else None
    common = top if (top and unusual and top["unusual"] and top["slow"] * 2 > slow_total) else None

    for u in users.values():
        u["worst"] = max(u["slow"], key=lambda s: s.response_time) if u["slow"] else None
    affected = sorted((u for u in users.values() if u["slow"]),
                      key=lambda u: (-len(u["slow"]), u["username"] or u["device"]))
    by_loc: dict[str, dict] = {}
    for u in users.values():
        name = u["location"] or "(none)"
        g = by_loc.setdefault(name, {"location": name, "users": 0, "slow": 0})
        g["users"] += 1
        g["slow"] += bool(u["slow"])
    log.info("Aternity: %d other users active then, %d slow%s", len(users), slow_total,
             f"; usually at most {usual_rate:.0%}" if usual_rate is not None else "")
    return {
        "applications": apps, "start": _iso(start), "end": _iso(end),
        "users": len(users), "slow": slow_total, "rate": rate,
        "usual_rate": usual_rate, "baseline_days": len(history), "unusual": unusual,
        "user_group": own_group[1] if own_group else None, "user_location": user_location,
        "netim_index_ready": ready,
        "groups": ranked[:10], "common_point": common,
        "by_location": sorted(by_loc.values(), key=lambda g: (-g["slow"], -g["users"]))[:15],
        "affected_users": [{
            "username": u["username"], "device": u["device"], "location": u["location"],
            "network": u["group"][1], "slow_activities": len(u["slow"]),
            "worst_response_time": round(u["worst"].response_time, 2),
            "worst_app": u["worst"].application,
        } for u in affected[:100]],
        "affected_total": len(affected),
    }
