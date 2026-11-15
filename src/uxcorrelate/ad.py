"""Active Directory, read only: users and computers (docs/admin-roadmap.md; the user's request
of 2026-11-15, "AD users/computers lookup tools", then "verify --ad").

- The domain: `ad.domain` in config.yaml, else this host's DNS search domain (and the domains
  above it: a host in ops.corp.example finds the DCs of corp.example).
- The domain controllers: `ad.servers`, else DNS SRV records (Microsoft's
  _ldap._tcp.dc._msdcs.<domain>, then _ldap._tcp.<domain>), asked of `ad.dns` or the host's DNS.
- A DC's own header (RootDSE: its name, the domain's base DN, its level) is read with no
  login, as AD allows.
- The login, a read-only service account (AD_USERNAME, AD_PASSWORD in .env): over LDAPS (636),
  then StartTLS on 389, with a simple bind, then NTLM. A password never goes over plain LDAP.
  An answer that the name or password is wrong stops the tries: each wrong one counts towards
  AD's lockout of the account.
- Certificates are not checked, as the Riverbed hosts' (self-signed; http.py).
- Users by sAMAccountName (or userPrincipalName when the name has an @), computers by
  sAMAccountName (NAME$), cn or dNSHostName; up to BATCH names in one search. Attribute names
  are Microsoft's (AD schema), held in USER_ATTRS / COMPUTER_ATTRS.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

LDAPS_PORT, LDAP_PORT = 636, 389
SRV_NAMES = ("_ldap._tcp.dc._msdcs.{}", "_ldap._tcp.{}")  # Microsoft's DC locator records, then plain LDAP's
RESOLV_CONF = "/etc/resolv.conf"
BATCH = 50  # names in one search (an OR filter)
SIMILAR_MAX = 200  # names read for "names close to it" (a typed name not found)
DISABLED = 0x2  # userAccountControl ACCOUNTDISABLE (Microsoft)
USER_ATTRS = ("sAMAccountName", "userPrincipalName", "displayName", "title", "department",
              "physicalDeliveryOfficeName", "userAccountControl")
COMPUTER_ATTRS = ("sAMAccountName", "cn", "dNSHostName", "description", "operatingSystem",
                  "userAccountControl", "lastLogonTimestamp")
ROOT_ATTRS = ("defaultNamingContext", "dnsHostName", "domainFunctionality", "ldapServiceName")
# AD's level of the domain (domainFunctionality, Microsoft MS-ADTS 7.1.4.4)
LEVELS = {"0": "2000", "1": "2003 interim", "2": "2003", "3": "2008", "4": "2008 R2", "5": "2012",
          "6": "2012 R2", "7": "2016", "10": "2025"}
# AD's reason under "invalid credentials" (data <code> in its message; Microsoft's codes)
WHY = {"525": "no such user", "52e": "the name or password is wrong", "530": "not allowed to log in at this time",
       "531": "not allowed to log in from this computer", "532": "password expired",
       "533": "account disabled", "701": "account expired", "773": "must change its password first",
       "775": "account locked out"}


@dataclass
class Dc:
    host: str
    port: int = LDAP_PORT  # from the SRV record (389) or ad.servers
    addr: str | None = None  # its address from ad.dns (the host's DNS may not know the AD's names)
    ldaps: str | None = None  # None: answers; else why not
    ldap: str | None = None
    starttls: str | None = None  # None: works; else why not; "" not tried
    root: dict = field(default_factory=dict)  # its RootDSE (no login)


def _words(exc: Exception) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:200]


# -- the domain and its DCs ----------------------------------------------------
def host_domains(path: str = RESOLV_CONF) -> list[str]:
    """This host's DNS search domains (resolv.conf "search" / "domain"), in its order."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in lines:
        parts = line.split()
        if parts and parts[0] in ("search", "domain"):
            out += [p.rstrip(".") for p in parts[1:] if p.rstrip(".") not in out]
    return out


def candidates(domain: str) -> list[str]:
    """The domain and those above it, down to two labels (ops.corp.example, corp.example)."""
    labels = domain.strip(".").split(".")
    return [".".join(labels[i:]) for i in range(max(1, len(labels) - 1))] if domain else []


def addresses(names: list[str]) -> list[str]:
    """ad.dns as addresses: a name in it (a DC's) is looked up in this host's DNS."""
    out: list[str] = []
    for n in names:
        try:
            for info in socket.getaddrinfo(n, 53, type=socket.SOCK_DGRAM):
                if info[4][0] not in out:
                    out.append(info[4][0])
        except socket.gaierror:
            continue
    return out


def resolver(dns_servers: list[str], timeout: float):
    """dnspython's resolver: ad.dns, else this host's (resolv.conf). ValueError: none of ad.dns is known."""
    import dns.resolver
    try:
        r = dns.resolver.Resolver(configure=not dns_servers)
    except dns.resolver.NoResolverConfiguration:
        r = dns.resolver.Resolver(configure=False)
    if dns_servers:
        found = addresses(dns_servers)
        if not found:
            raise ValueError(f"none of ad.dns ({', '.join(dns_servers)}) is an address or a name this host knows")
        r.nameservers = found
    r.lifetime = timeout
    return r


def address_of(name: str, dns_servers: list[str], timeout: float) -> str | None:
    """A DC's address from ad.dns (None: ad.dns not set, or it does not know the name: the
    host's DNS is asked when connecting)."""
    if not dns_servers:
        return None
    import dns.exception
    try:
        answer = resolver(dns_servers, timeout).resolve(name, "A")
        return str(next(iter(answer)))
    except (dns.exception.DNSException, ValueError, StopIteration):
        return None


def srv_dcs(domain: str, dns_servers: list[str], timeout: float) -> tuple[list[Dc], str, list[str]]:
    """The DCs from DNS: (DCs, the record that gave them, what was tried and why it gave none)."""
    import dns.exception
    import dns.resolver
    try:
        r = resolver(dns_servers, timeout)
    except ValueError as exc:
        return [], "", [str(exc)]
    tried = []
    for name in (n.format(domain) for n in SRV_NAMES):
        try:
            answer = r.resolve(name, "SRV")
        except dns.resolver.NXDOMAIN:
            tried.append(f"{name}: no such name")
            continue
        except dns.resolver.NoAnswer:
            tried.append(f"{name}: no SRV record")
            continue
        except dns.resolver.NoNameservers:
            tried.append(f"{name}: the DNS server refused or failed")
            continue
        except dns.exception.Timeout:
            tried.append(f"{name}: no answer in {timeout:g} s")
            continue
        except dns.exception.DNSException as exc:
            tried.append(f"{name}: {_words(exc)}")
            continue
        rows = sorted(answer, key=lambda rr: (rr.priority, -rr.weight, str(rr.target)))
        dcs = [Dc(str(rr.target).rstrip("."), int(rr.port)) for rr in rows if str(rr.target) not in (".", "")]
        for dc in dcs:
            dc.addr = address_of(dc.host, dns_servers, timeout)
        if dcs:
            return dcs, name, tried
        tried.append(f"{name}: an empty record")
    return [], "", tried


def reachable(host: str, port: int, timeout: float) -> str | None:
    """None when the port answers; else why not, in plain words."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except socket.gaierror:
        return "its name is not known to this host's DNS"
    except TimeoutError:
        return f"no answer in {timeout:g} s (a firewall?)"
    except ConnectionRefusedError:
        return "refused (nothing listens there)"
    except OSError as exc:
        return _words(exc)


# -- LDAP ------------------------------------------------------------------------
class Directory:
    """The LDAP side. _server and _connection are where tests put a made-up AD (ldap3's mock)."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def _server(self, host: str, port: int, use_ssl: bool):
        from ldap3 import NONE, Server, Tls
        return Server(host, port=port, use_ssl=use_ssl, get_info=NONE, connect_timeout=self.timeout,
                      tls=Tls(validate=ssl.CERT_NONE))

    def _connection(self, server, **kw):
        from ldap3 import Connection
        return Connection(server, receive_timeout=max(1, int(self.timeout * 3)), read_only=True, raise_exceptions=False,
                          auto_referrals=False, **kw)

    def open(self, host: str, how: str, user: str | None = None, password: str | None = None,
             ntlm: bool = False):
        """(a bound connection or None, why not, AD's result code). how: ldaps, starttls or ldap.
        No user: an anonymous bind (the RootDSE). A simple bind never goes over plain LDAP: its
        password would cross the network as written."""
        from ldap3 import ANONYMOUS, NTLM, SIMPLE
        from ldap3.core.exceptions import LDAPException, LDAPSessionTerminatedByServerError, LDAPStartTLSError
        if user is not None and how == "ldap" and not ntlm:
            return None, "not tried: a password over plain LDAP", None
        server = self._server(host, LDAPS_PORT if how == "ldaps" else LDAP_PORT, how == "ldaps")
        kind = ANONYMOUS if user is None else NTLM if ntlm else SIMPLE
        try:
            conn = self._connection(server, user=user, password=password, authentication=kind)
            conn.open()
            if how == "starttls" and not conn.start_tls():
                return None, f"StartTLS refused ({conn.result.get('description') or 'no reason given'})", None
            if conn.bind():
                return conn, "", None
            res = conn.result or {}
            return None, self.refusal(res), res.get("result")
        except LDAPStartTLSError as exc:
            return None, f"StartTLS failed ({_words(exc)})", None
        except LDAPSessionTerminatedByServerError:
            return None, "the DC closed the connection at this login (it does not take it here)", None
        except LDAPException as exc:
            return None, _words(exc), None
        except OSError as exc:
            return None, _words(exc), None

    @staticmethod
    def refusal(res: dict) -> str:
        """AD's answer to a bind, in plain words."""
        code, desc, msg = res.get("result"), res.get("description") or "", res.get("message") or ""
        if code == 49:
            import re
            m = re.search(r"data ([0-9a-f]{3})", msg)
            return f"login refused: {WHY.get(m.group(1), 'invalid credentials') if m else 'invalid credentials'}" + (
                f" (AD's code {m.group(1)})" if m else "")
        if code == 8:
            return "the DC wants a signed or encrypted login here (strongerAuthRequired)"
        if code == 13:
            return "the DC wants TLS here (confidentialityRequired)"
        if code == 53:
            return "the DC will not do this login (unwillingToPerform)"
        return f"refused: {desc or code}{f' ({msg[:120]})' if msg else ''}"

    def root(self, host: str, how: str) -> tuple[dict, str]:
        """The DC's own header with no login: ({attribute: value}, why not)."""
        from ldap3 import BASE
        conn, why, _ = self.open(host, how)
        if conn is None:
            return {}, why
        try:
            if not conn.search("", "(objectClass=*)", BASE, attributes=list(ROOT_ATTRS)):
                return {}, f"its header could not be read ({conn.result.get('description')})"
            entry = next((e for e in conn.response if e.get("type") == "searchResEntry"), None)
            attrs = (entry or {}).get("attributes") or {}
            return {k: _one(attrs.get(k)) for k in ROOT_ATTRS if _one(attrs.get(k))}, ""
        finally:
            conn.unbind()

    def login(self, host: str, user: str, password: str, domain: str, ports: dict) -> tuple:
        """(connection, how it logged in, [each try and why it failed]). Tries what the ports
        allow, in order; a wrong name or password stops the tries (AD's lockout counts them)."""
        upn = user if ("@" in user or "\\" in user) else f"{user}@{domain}" if domain else user
        # NTLM's DOMAIN\name: as given, else the DNS domain (its short NetBIOS name is not known
        # before a login; AD_USERNAME can give it)
        down = user if "\\" in user else f"{domain}\\{user.split('@')[0]}" if domain else user
        tries = [("ldaps", False, upn, "LDAPS (636), simple bind"), ("starttls", False, upn, "StartTLS on 389, simple bind"),
                 ("ldaps", True, down, "LDAPS (636), NTLM"), ("ldap", True, down, "389 with no TLS, NTLM")]
        failed: list[str] = []
        for how, ntlm, name, words in tries:
            if ports.get(LDAPS_PORT if how == "ldaps" else LDAP_PORT, "not checked") is not None:
                continue  # the port does not answer (None: it does): not tried
            if how == "starttls" and ports.get("starttls"):
                continue
            if ntlm and "\\" not in name:
                failed.append(f"{words}: not tried (NTLM needs DOMAIN\\name: AD_USERNAME so, or ad.domain)")
                continue
            conn, why, code = self.open(host, how, name, password, ntlm=ntlm)
            if conn is not None:
                return conn, words, failed
            failed.append(f"{words}: {why}")
            if code == 49:  # the name or password is wrong: every other try would count against the account too
                break
        return None, "", failed

    def search(self, conn, base: str, flt: str, attrs, size: int = 0) -> list[dict]:
        """The entries found. size: at most that many (0: AD's own limit, 1,000 a page). Hitting
        a limit (AD's sizeLimitExceeded) keeps the entries it sent."""
        from ldap3 import SUBTREE
        if not conn.search(base, flt, SUBTREE, attributes=list(attrs), size_limit=size,
                           time_limit=int(self.timeout * 3)):
            code = (conn.result or {}).get("result")
            if code == 32:
                raise LookupError(f"the base {base} is not in this directory")
            if code not in (0, 4):  # 0: none found; 4: a size limit reached
                raise LookupError(f"the search was refused ({(conn.result or {}).get('description') or code})")
        return [{k: v for k, v in (e.get("attributes") or {}).items()} | {"dn": e.get("dn", "")}
                for e in conn.response if e.get("type") == "searchResEntry"]


def _one(v):
    """One value of an attribute, as text (ldap3 gives a list, a value, or bytes)."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return v if v not in (None, "", []) else None


def _esc(value: str) -> str:
    from ldap3.utils.conv import escape_filter_chars
    return escape_filter_chars(value)


# -- names as Aternity writes them ---------------------------------------------------
def user_parts(name: str) -> tuple[str, str | None]:
    """(sAMAccountName, userPrincipalName or None) of a username: DOMAIN\\name, name@domain or name."""
    name = name.strip()
    if "\\" in name:
        return name.split("\\", 1)[1], None
    if "@" in name:
        return name.split("@", 1)[0], name
    return name, None


def pc_parts(name: str) -> tuple[str, str | None]:
    """(the PC's short name, its full DNS name or None)."""
    name = name.strip().rstrip(".")
    return (name.split(".", 1)[0], name) if "." in name else (name, None)


def user_style(name: str) -> str:
    return "DOMAIN\\name" if "\\" in name else "name@domain" if "@" in name else "name alone"


def pc_style(name: str) -> str:
    return "with its domain (name.domain)" if "." in name.strip(".") else "the short name alone"


def users(d: Directory, conn, base: str, names: list[str]) -> dict[str, dict | None]:
    """Each name -> its AD user (attributes) or None."""
    out: dict[str, dict | None] = {n: None for n in names}
    for i in range(0, len(names), BATCH):
        part = names[i:i + BATCH]
        terms = []
        for n in part:
            sam, upn = user_parts(n)
            terms.append(f"(sAMAccountName={_esc(sam)})")
            if upn:
                terms.append(f"(userPrincipalName={_esc(upn)})")
        found = d.search(conn, base, f"(&(objectCategory=person)(objectClass=user)(|{''.join(terms)}))", USER_ATTRS)
        by_sam = {str(_one(e.get("sAMAccountName")) or "").lower(): e for e in found}
        by_upn = {str(_one(e.get("userPrincipalName")) or "").lower(): e for e in found}
        for n in part:
            sam, upn = user_parts(n)
            out[n] = (by_upn.get(upn.lower()) if upn else None) or by_sam.get(sam.lower())
    return out


def computers(d: Directory, conn, base: str, names: list[str]) -> dict[str, dict | None]:
    """Each PC name -> its AD computer object or None."""
    out: dict[str, dict | None] = {n: None for n in names}
    for i in range(0, len(names), BATCH):
        part = names[i:i + BATCH]
        terms = []
        for n in part:
            short, fqdn = pc_parts(n)
            terms += [f"(sAMAccountName={_esc(short[:15])}$)", f"(cn={_esc(short)})"]
            if fqdn:
                terms.append(f"(dNSHostName={_esc(fqdn)})")
        found = d.search(conn, base, f"(&(objectCategory=computer)(|{''.join(terms)}))", COMPUTER_ATTRS)
        by_cn = {str(_one(e.get("cn")) or "").lower(): e for e in found}
        by_sam = {str(_one(e.get("sAMAccountName")) or "").lower().rstrip("$"): e for e in found}
        by_dns = {str(_one(e.get("dNSHostName")) or "").lower(): e for e in found}
        for n in part:
            short, fqdn = pc_parts(n)
            out[n] = (by_dns.get(fqdn.lower()) if fqdn else None) or by_cn.get(short.lower()) \
                or by_sam.get(short[:15].lower())
    return out


def similar(d: Directory, conn, base: str, word: str, computer: bool = False, limit: int = 5) -> list[str]:
    """Up to `limit` names in AD that have this word (a name typed and not found)."""
    w = _esc(word)
    flt = (f"(&(objectCategory=computer)(|(cn=*{w}*)(dNSHostName=*{w}*)))" if computer else
           f"(&(objectCategory=person)(objectClass=user)(|(sAMAccountName=*{w}*)(displayName=*{w}*)))")
    try:
        found = d.search(conn, base, flt, ("cn",) if computer else ("sAMAccountName",), size=SIMILAR_MAX)
    except LookupError:
        return []
    names = sorted({str(_one(e.get("cn" if computer else "sAMAccountName")) or "") for e in found} - {""})
    return names[:limit]


def disabled(entry: dict) -> bool:
    try:
        return bool(int(_one(entry.get("userAccountControl")) or 0) & DISABLED)
    except (TypeError, ValueError):
        return False


def filetime(value) -> datetime | None:
    """AD's lastLogonTimestamp (100-ns steps since 1601, or a time ldap3 already read)."""
    v = _one(value)
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=n // 10)


def ou_of(dn: str) -> str:
    """Where an object sits, from its DN: "Computers / Virtual desktops" (OUs and containers, top first)."""
    parts = [p.split("=", 1)[1] for p in dn.split(",")[1:] if p.split("=", 1)[0].strip().upper() in ("OU", "CN")]
    return " / ".join(reversed(parts)) or "the domain's top"


def base_of(domain: str) -> str:
    return ",".join(f"DC={p}" for p in domain.split(".") if p)


def domain_of(dn: str) -> str:
    """The DNS domain of a base DN: DC=corp,DC=example -> corp.example."""
    return ".".join(p.split("=", 1)[1] for p in dn.split(",") if p.strip().upper().startswith("DC="))
