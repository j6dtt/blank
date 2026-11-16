"""Active Directory, read only (ad.py, verify --ad). The directory is ldap3's own mock with
Microsoft's AD 2012 R2 schema (OFFLINE_AD_2012_R2, shipped with ldap3); DNS and ports are
stubbed. A real Samba DC on dev (dev/compose.yaml sample-ad) runs the same paths end to end."""

from datetime import timezone

import httpx
import pytest
from ldap3 import MOCK_SYNC, OFFLINE_AD_2012_R2, Connection, Server

from uxcorrelate import ad
from uxcorrelate.aternity import AternityClient
from uxcorrelate import cli
from uxcorrelate.config import Settings, load_settings
from uxcorrelate.verify import render_checks, verify_ad

BASE = "DC=corp,DC=example"
PASSWORD = "S3cret-not-printed"


def mock_ad():
    """A made-up directory: users, PCs, the service account."""
    conn = Connection(Server("dc1.corp.example", get_info=OFFLINE_AD_2012_R2), user=f"CN=svc,{BASE}",
                      password=PASSWORD, client_strategy=MOCK_SYNC)
    add = conn.strategy.add_entry
    add(f"CN=svc,{BASE}", {"objectClass": ["user"], "userPassword": PASSWORD, "sAMAccountName": "svc"})
    for sam, extra in [("alice", {"displayName": "Alice Archer", "title": "Planner", "department": "Finance",
                                  "physicalDeliveryOfficeName": "Building 2", "userAccountControl": 512}),
                       ("bob", {"userPrincipalName": "bob@corp.example", "userAccountControl": 512}),
                       ("carol", {"userAccountControl": 514}),  # disabled
                       ("axxb", {"userAccountControl": 512})]:
        add(f"CN={sam},OU=Staff,{BASE}", {"objectClass": ["top", "person", "user"], "objectCategory": "person",
                                          "sAMAccountName": sam, **extra})
    for cn, extra in [("PC-001", {"operatingSystem": "Windows 11 Enterprise", "description": "Desk 1",
                                  "lastLogonTimestamp": 134000000000000000, "userAccountControl": 4096}),
                      ("PC-002", {"dNSHostName": "pc-002.corp.example", "userAccountControl": 4096}),
                      ("VD-01", {"userAccountControl": 4098})]:  # disabled
        add(f"CN={cn},OU=Virtual desktops,OU=Desktops,{BASE}",
            {"objectClass": ["computer"], "objectCategory": "computer", "cn": cn, "sAMAccountName": f"{cn}$", **extra})
    conn.bind()
    return conn


class FakeDirectory(ad.Directory):
    """Logins and the DC header decided by the test; searches are ldap3's mock."""

    def __init__(self, conn=None, refuse=None, root=None, no_tls=(), no_anonymous=False):
        super().__init__(5)
        self.conn, self.refuse, self.root_dse, self.tries = conn, refuse, root, []
        self.no_tls, self.no_anonymous = no_tls, no_anonymous

    def open(self, host, how, user=None, password=None, ntlm=False):
        self.tries.append((how, user, ntlm))
        if how in self.no_tls:
            return None, "socket ssl wrapping error: [SSL: UNEXPECTED_EOF_WHILE_READING]", None
        if user is None:  # anonymous: refused (a result code) or the connection itself
            return (None, "refused: inappropriateAuthentication", 48) if self.no_anonymous else (self, "", None)
        if user is not None and how == "ldap" and not ntlm:
            return None, "not tried: a password over plain LDAP", None
        if self.refuse:
            return None, self.refusal(self.refuse), self.refuse.get("result")
        return self.conn, "", None

    def unbind(self):  # the StartTLS probe's connection is the directory itself
        pass

    def root(self, host, how):
        return (self.root_dse, "") if self.root_dse else ({}, "refused")


def settings(**ad_keys):
    s = Settings()
    s.aternity.base_url = "https://aternity.test/odata"
    for k, v in ad_keys.items():
        setattr(s.ad, k, v)
    return s


def aternity(rows):
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"value": rows})
    return AternityClient(settings().aternity, transport=httpx.MockTransport(handler))


@pytest.fixture
def stubs(monkeypatch):
    monkeypatch.setattr(ad, "host_domains", lambda path=ad.RESOLV_CONF: [])
    monkeypatch.setattr(ad, "srv_dcs", lambda d, dns, t: ([ad.Dc("dc1.corp.example"), ad.Dc("dc2.corp.example")],
                                                         f"_ldap._tcp.dc._msdcs.{d}", []))
    monkeypatch.setattr(ad, "reachable", lambda host, port, t: None)
    monkeypatch.setattr(ad, "address_of", lambda name, dns, t: None)


ROOT = {"defaultNamingContext": BASE, "dnsHostName": "dc1.corp.example", "domainFunctionality": "7"}
ROWS = [{"USERNAME": u, "DEVICE_NAME": p} for u, p in
        [("CORP\\alice", "PC-001"), ("bob@corp.example", "pc-002.corp.example"), ("carol", "VD-01"),
         ("zed", "PC-404"), ("CORP\\alice", "PC-001")]]


def text(checks):
    return render_checks(checks)


# -- the domain and its DCs ----------------------------------------------------
def test_host_domains_and_the_domains_above(tmp_path):
    conf = tmp_path / "resolv.conf"
    conf.write_text("nameserver 192.0.2.53\nsearch ops.corp.example. corp.example\ndomain ops.corp.example\n")
    assert ad.host_domains(str(conf)) == ["ops.corp.example", "corp.example"]
    assert ad.host_domains(str(tmp_path / "none")) == []
    assert ad.candidates("ops.corp.example") == ["ops.corp.example", "corp.example"]
    assert ad.candidates("corp") == ["corp"]


def test_srv_records_are_microsofts_then_plain_ldaps_in_their_order(monkeypatch):
    import dns.resolver

    class Rr:
        def __init__(self, target, priority, weight):
            self.target, self.priority, self.weight, self.port = target, priority, weight, 389

    asked = []

    def resolve(self, name, kind):
        asked.append((name, kind))
        if name.startswith("_ldap._tcp.dc._msdcs."):
            raise dns.resolver.NXDOMAIN()
        return [Rr("dc2.corp.example.", 10, 50), Rr("dc1.corp.example.", 0, 100), Rr("dc3.corp.example.", 10, 90)]

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", resolve)
    dcs, record, tried = ad.srv_dcs("corp.example", ["192.0.2.53"], 2)
    assert [d.host for d in dcs] == ["dc1.corp.example", "dc3.corp.example", "dc2.corp.example"]
    assert record == "_ldap._tcp.corp.example"
    assert tried == ["_ldap._tcp.dc._msdcs.corp.example: no such name"]
    assert asked[0] == ("_ldap._tcp.dc._msdcs.corp.example", "SRV")


def test_a_dns_server_this_host_cannot_name_is_said():
    dcs, _, tried = ad.srv_dcs("corp.example", ["no-such-host.invalid"], 1)
    assert dcs == [] and "none of ad.dns" in tried[0]


# -- the login -------------------------------------------------------------------
def test_refusals_in_plain_words():
    r = ad.Directory.refusal
    assert r({"result": 49, "message": "80090308: LdapErr: DSID-0C09044E, comment: AcceptSecurityContext error, "
                                       "data 775, v4563"}) == "login refused: account locked out (AD's code 775)"
    assert "the name or password is wrong" in r({"result": 49, "message": "data 52e, v4563"})
    assert "signed or encrypted" in r({"result": 8})
    assert "wants TLS" in r({"result": 13})


def test_login_tries_ldaps_then_starttls_then_ntlm_and_never_a_password_over_plain_ldap():
    d = FakeDirectory(refuse={"result": 53, "description": "unwillingToPerform"})
    conn, how, failed = d.login("dc1", "svc", PASSWORD, "corp.example", {636: None, 389: None, "starttls": None})
    assert conn is None
    assert d.tries == [("ldaps", "svc@corp.example", False), ("starttls", "svc@corp.example", False),
                       ("ldaps", "corp.example\\svc", True), ("ldap", "corp.example\\svc", True)]
    assert len(failed) == 4 and all("unwillingToPerform" in f for f in failed)
    # 636 closed, StartTLS refused: only NTLM on 389 is left
    d = FakeDirectory(refuse={"result": 53})
    d.login("dc1", "CORP\\svc", PASSWORD, "corp.example", {636: "refused", 389: None, "starttls": "refused"})
    assert d.tries == [("ldap", "CORP\\svc", True)]
    # a simple bind over plain LDAP is refused by open() itself
    assert ad.Directory(1).open("dc1", "ldap", "svc", PASSWORD)[1] == "not tried: a password over plain LDAP"


def test_a_wrong_password_stops_the_tries():
    """Each wrong password counts towards AD's lockout of the account: one try, not four."""
    d = FakeDirectory(refuse={"result": 49, "message": "data 52e"})
    conn, how, failed = d.login("dc1", "svc", "wrong", "corp.example", {636: None, 389: None, "starttls": None})
    assert conn is None and len(d.tries) == 1 and "the name or password is wrong" in failed[0]


# -- names -----------------------------------------------------------------------
def test_users_and_pcs_are_matched_as_aternity_writes_them():
    conn, d = mock_ad(), ad.Directory(5)
    found = ad.users(d, conn, BASE, ["CORP\\alice", "bob@corp.example", "Carol", "zed", "a*b"])
    assert found["CORP\\alice"]["displayName"] == "Alice Archer"
    assert found["bob@corp.example"] and found["Carol"] and ad.disabled(found["Carol"])
    assert found["zed"] is None
    assert found["a*b"] is None  # a name is a name, not a pattern (axxb is not it)
    pcs = ad.computers(d, conn, BASE, ["PC-001", "pc-002.corp.example", "VD-01", "PC-404"])
    assert pcs["PC-001"]["operatingSystem"] == "Windows 11 Enterprise"
    assert pcs["pc-002.corp.example"] and ad.disabled(pcs["VD-01"]) and pcs["PC-404"] is None
    assert ad.filetime(pcs["PC-001"]["lastLogonTimestamp"]).year == 2025
    assert ad.ou_of(pcs["PC-001"]["dn"]) == "Desktops / Virtual desktops"
    assert ad.similar(d, conn, BASE, "pc-0", computer=True) == ["PC-001", "PC-002"]
    assert len(d.search(conn, BASE, "(objectCategory=computer)", ("cn",), size=2)) == 2  # a size limit keeps what came


def test_filetime_reads_ads_100ns_steps():
    t = ad.filetime("134000000000000000")
    assert t.tzinfo == timezone.utc and t.isoformat().startswith("2025-08-18")
    assert ad.filetime("0") is None and ad.filetime(None) is None


# -- verify --ad -----------------------------------------------------------------
def test_verify_ad_counts_aternitys_names_in_ad_and_shows_none(stubs):
    s = settings(domain="corp.example", username="svc", password=PASSWORD)
    checks = verify_ad(s, aternity(ROWS), directory=FakeDirectory(mock_ad(), root=ROOT))
    out = text(checks)
    by = {c.item: c for c in checks}
    assert by["domain controllers"].ok and "2 found in DNS" in by["domain controllers"].message
    assert "LDAPS (636) works; 389 answers; StartTLS works" in by["dc1.corp.example"].message
    assert "DC=corp,DC=example, its level Windows Server 2016" in by["directory"].message
    assert by["login"].message == "Logged in as AD_USERNAME over LDAPS (636), simple bind."
    assert by["users"].ok is None
    assert by["users"].message.startswith("3 of 4 of Aternity's most active usernames are in AD; 1 is not; 1 disabled")
    assert "Aternity writes usernames as DOMAIN\\name: 1" in by["users"].rows
    assert by["users"].rows[-1].startswith("To see one: uxcorrelate verify --ad --user")
    assert by["computers"].message.startswith("3 of 4 of Aternity's most active PC names are in AD; 1 is not; 1 disabled")
    for name in ("alice", "bob", "carol", "zed", "PC-001", "PC-404", "VD-01", PASSWORD, "svc@"):
        assert name not in out  # counts only: names only when typed


def test_verify_ad_shows_a_typed_user_and_pc(stubs):
    s = settings(domain="corp.example", username="svc", password=PASSWORD)
    by = {c.item: c for c in verify_ad(s, None, user="CORP\\alice", pc="PC-001",
                                        directory=FakeDirectory(mock_ad(), root=ROOT))}
    assert by["user CORP\\alice"].message.startswith("Alice Archer · Planner · Finance · Building 2 · enabled")
    assert by["PC PC-001"].message.startswith("Windows 11 Enterprise · in Desktops / Virtual desktops · Desk 1 · "
                                             "enabled · last logon 2025-08-18")
    by = {c.item: c for c in verify_ad(s, None, user="ali", pc="PC-0", directory=FakeDirectory(mock_ad(), root=ROOT))}
    assert by["user ali"].ok is None and 'Names with "ali" in them: alice.' in by["user ali"].message
    assert 'Computers with "PC-0" in their name: PC-001, PC-002.' in by["PC PC-0"].message


def test_verify_ad_with_no_account_checks_what_needs_none(stubs):
    by = {c.item: c for c in verify_ad(settings(domain="corp.example"), None,
                                        directory=FakeDirectory(root=ROOT))}
    assert by["directory"].ok and by["login"].ok is None
    assert by["login"].message.startswith("Not set: AD_USERNAME and AD_PASSWORD in .env")
    assert "users" not in by


def test_verify_ad_says_a_refused_login_and_why(stubs):
    s = settings(domain="corp.example", username="svc", password="wrong")
    d = FakeDirectory(refuse={"result": 49, "message": "data 52e"}, root=ROOT)
    by = {c.item: c for c in verify_ad(s, None, directory=d)}
    assert by["login"].ok is False and "stopped at the refused password" in by["login"].message
    assert by["login"].rows == ["LDAPS (636), simple bind: login refused: the name or password is wrong (AD's code 52e)"]
    assert "Check AD_USERNAME and AD_PASSWORD" in by["login"].fix


def test_verify_ad_with_no_domain_controller_in_dns(monkeypatch):
    monkeypatch.setattr(ad, "host_domains", lambda path=ad.RESOLV_CONF: ["ops.corp.example"])
    monkeypatch.setattr(ad, "srv_dcs", lambda d, dns, t: ([], "", [f"_ldap._tcp.dc._msdcs.{d}: no such name"]))
    by = {c.item: c for c in verify_ad(settings(), None, directory=FakeDirectory())}
    assert "ops.corp.example (this host's DNS search domain)" in by["domain"].message
    assert by["domain controllers"].ok is False and "Set ad.dns" in by["domain controllers"].fix
    assert by["domain controllers"].rows == ["_ldap._tcp.dc._msdcs.ops.corp.example: no such name",
                                             "_ldap._tcp.dc._msdcs.corp.example: no such name"]


def test_verify_ad_finds_the_dcs_of_the_domain_above(monkeypatch):
    monkeypatch.setattr(ad, "host_domains", lambda path=ad.RESOLV_CONF: ["ops.corp.example"])
    monkeypatch.setattr(ad, "srv_dcs", lambda d, dns, t: ([ad.Dc("dc1.corp.example")], "x", [])
                        if d == "corp.example" else ([], "", []))
    monkeypatch.setattr(ad, "reachable", lambda host, port, t: None)
    checks = verify_ad(settings(), None, directory=FakeDirectory(root=ROOT))
    assert any(c.item == "domain" and c.message.startswith("The DCs are those of corp.example, found through this "
                                                            "host's DNS search list") and "domain: corp.example" in c.paste
               for c in checks)


def test_a_dc_with_no_certificate_is_not_ldaps_and_a_refused_anonymous_bind_is_tls_working(stubs):
    s = settings(domain="corp.example", username="svc", password=PASSWORD)
    d = FakeDirectory(mock_ad(), root=ROOT, no_tls=("ldaps", "starttls"))
    checks = verify_ad(s, None, user="CORP\\alice", directory=d)
    by = {c.item: c for c in checks}
    dc = by["dc1.corp.example"]
    assert dc.ok is None and dc.message.startswith("LDAPS (636) the port answers, its TLS does not (socket ssl")
    assert "StartTLS fails (socket ssl" in dc.message and "only an NTLM login can be tried" in dc.message
    assert "a DC with no certificate" in by["TLS"].message
    assert by["login"].message == "Logged in as AD_USERNAME over 389 with no TLS, NTLM."
    assert [t for t in d.tries if t[1]] == [("ldap", "corp.example\\svc", True)]  # no password tried without TLS but NTLM
    d = FakeDirectory(mock_ad(), root=ROOT, no_anonymous=True)
    by = {c.item: c for c in verify_ad(s, None, user="CORP\\alice", directory=d)}
    assert "LDAPS (636) works; 389 answers; StartTLS works" in by["dc1.corp.example"].message


def test_every_search_domain_is_tried_and_the_domain_comes_from_the_dc(monkeypatch):
    monkeypatch.setattr(ad, "host_domains", lambda path=ad.RESOLV_CONF: ["ns.svc.cluster.local", "ops.corp.example"])
    asked = []
    monkeypatch.setattr(ad, "srv_dcs", lambda d, dns, t: asked.append(d) or (
        ([ad.Dc("dc1.corp.example")], "x", []) if d == "corp.example" else ([], "", [f"{d}: no such name"])))
    monkeypatch.setattr(ad, "reachable", lambda host, port, t: None)
    checks = verify_ad(settings(), None, directory=FakeDirectory(root=ROOT))
    assert asked == ["ns.svc.cluster.local", "svc.cluster.local", "cluster.local", "ops.corp.example", "corp.example"]
    assert any(c.item == "domain" and c.paste == "ad:\n  domain: corp.example" for c in checks)
    # DCs named in ad.servers, no domain: the login's domain is the DC's own
    monkeypatch.setattr(ad, "host_domains", lambda path=ad.RESOLV_CONF: [])
    monkeypatch.setattr(ad, "address_of", lambda name, dns, t: None)
    d = FakeDirectory(mock_ad(), root=ROOT)
    verify_ad(settings(servers=["dc1.corp.example"], username="svc", password=PASSWORD), None, user="alice",
              directory=d)
    assert ("ldaps", "svc@corp.example", False) in d.tries
    assert ad.domain_of("DC=corp,DC=example") == "corp.example"


def test_verify_ad_when_no_port_answers(stubs, monkeypatch):
    monkeypatch.setattr(ad, "reachable", lambda host, port, t: "no answer in 5 s (a firewall?)")
    checks = verify_ad(settings(domain="corp.example", username="svc", password=PASSWORD), None,
                       directory=FakeDirectory(mock_ad(), root=ROOT))
    dc = [c for c in checks if c.item == "dc1.corp.example"][0]
    assert dc.ok is False and "a firewall between it and the DC?" in dc.fix
    assert checks[-1].item == "dc2.corp.example"  # nothing to log in to: it stops there


# -- settings and the command -------------------------------------------------------
def test_ad_settings_keep_the_account_in_env_only(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ad:\n  domain: corp.example.\n  servers: dc1.corp.example\n")
    monkeypatch.setenv("AD_USERNAME", " svc ")
    monkeypatch.setenv("AD_PASSWORD", PASSWORD)
    s = load_settings(str(cfg))
    assert (s.ad.domain, s.ad.servers, s.ad.username, s.ad.password) == ("corp.example", ["dc1.corp.example"], "svc",
                                                                         PASSWORD)
    cfg.write_text("ad:\n  password: oops\n")
    with pytest.raises(ValueError, match="AD_PASSWORD=... in .env"):
        load_settings(str(cfg))


def test_verify_ad_command_line(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_verify", lambda args: seen.append(args) or 0)
    assert cli.main(["verify", "--ad", "--user", "CORP\\alice", "--pc", "PC-001"]) == 0
    assert (seen[0].ad, seen[0].user, seen[0].pc) == (True, "CORP\\alice", "PC-001")


# -- the admin page's look-up (ad.Lookup) ---------------------------------------------
class CountingDirectory(FakeDirectory):
    searches = 0

    def search(self, conn, base, flt, attrs, size=0):
        CountingDirectory.searches += 1
        return super().search(conn, base, flt, attrs, size)


def lookup(**ad_keys):
    cfg = settings(**ad_keys).ad
    made = []

    def directory(c):
        made.append(CountingDirectory(mock_ad(), root=ROOT, **made_with))
        return made[-1]
    made_with: dict = {}
    return ad.Lookup(lambda: cfg, directory), made, made_with


def test_lookup_reads_each_name_once_a_day_in_memory(stubs, monkeypatch):
    from datetime import datetime, timedelta
    from uxcorrelate import http
    told = []
    monkeypatch.setattr(http, "METER", lambda *a: told.append(a))
    look, made, _ = lookup(domain="corp.example", username="svc", password=PASSWORD)
    now = datetime(2026, 11, 16, 15, tzinfo=timezone.utc)
    CountingDirectory.searches = 0
    got = look.look(["CORP\\alice", "zed", "carol"], ["PC-001", "PC-404"], now)
    assert got["state"] == "ok" and got["kept_h"] == 24
    assert got["users"]["CORP\\alice"] == {"found": True, "name": "Alice Archer", "title": "Planner",
                                           "department": "Finance", "office": "Building 2", "disabled": False}
    assert got["users"]["zed"] == {"found": False} and got["users"]["carol"]["disabled"] is True
    assert got["pcs"]["PC-001"]["ou"] == "Desktops / Virtual desktops" and got["pcs"]["PC-404"] == {"found": False}
    assert CountingDirectory.searches == 2  # one search for the users, one for the PCs
    assert [t[:3] for t in told] == [("Active Directory", "other", "login"), ("Active Directory", "other", "users"),
                                     ("Active Directory", "other", "computers")]
    assert told[1][4] == 2  # rows: the users found
    # asked again within the day: nothing read, the DC that answered is kept
    again = look.look(["CORP\\alice"], ["PC-001"], now + timedelta(hours=23))
    assert CountingDirectory.searches == 2 and again["users"]["CORP\\alice"]["found"]
    assert look.look(["CORP\\alice"], [], now + timedelta(hours=25))["users"]["CORP\\alice"]["found"]
    assert CountingDirectory.searches == 3 and look.dc[0].host == "dc1.corp.example"


def test_lookup_not_set_reads_nothing(stubs):
    look, made, _ = lookup(domain="corp.example")
    got = look.look(["alice"], ["PC-001"], datetime_now())
    assert got["state"] == "off" and "AD_USERNAME and AD_PASSWORD" in got["why"] and not made


def test_lookup_never_tries_a_refused_password_again(stubs):
    cfg = settings(domain="corp.example", username="svc", password="wrong").ad
    made = []

    def directory(c):
        made.append(FakeDirectory(refuse={"result": 49, "message": "data 52e"}, root=ROOT))
        return made[-1]
    look = ad.Lookup(lambda: cfg, directory)
    got = look.look(["alice"], [], datetime_now())
    assert got["state"] == "refused" and got["why"] == "the name or password is wrong (AD's code 52e)"
    logins = [t for t in made[0].tries if t[1]]
    assert len(logins) == 1  # one try, on the first DC only: the second would count against the account too
    assert look.look(["alice", "bob"], [], datetime_now())["state"] == "refused" and len(made) == 1


def test_lookup_when_ad_does_not_answer_waits_before_trying_again(stubs, monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr(ad, "srv_dcs", lambda d, dns, t: ([], "", [f"_ldap._tcp.dc._msdcs.{d}: no answer in 5 s"]))
    look, made, _ = lookup(domain="corp.example", username="svc", password=PASSWORD)
    now = datetime_now()
    got = look.look(["alice"], [], now)
    assert got["state"] == "down" and got["why"].startswith("no domain controller found in DNS") and got["users"] == {}
    look.look(["alice"], [], now + timedelta(minutes=4))
    assert len(made) == 1  # within RETRY_MIN: not tried
    monkeypatch.setattr(ad, "srv_dcs", lambda d, dns, t: ([ad.Dc("dc1.corp.example")], "x", []))
    got = look.look(["alice"], [], now + timedelta(minutes=6))
    assert got["state"] == "ok" and got["users"]["alice"]["found"] and len(made) == 2


def test_admin_ad_route_needs_the_login(stubs, monkeypatch):
    from fastapi.testclient import TestClient
    from uxcorrelate import web
    monkeypatch.setenv(web.ADMIN_ENV, "s3cret-test")
    monkeypatch.delenv("UXCORRELATE_DATA", raising=False)
    monkeypatch.delenv("AD_USERNAME", raising=False)
    app = web.create_app()
    https = TestClient(app, base_url="https://testserver")
    assert https.post("/admin/api/ad", json={"users": ["alice"]}).status_code == 401
    assert https.post("/admin/api/login", json={"password": "s3cret-test"}).status_code == 200
    got = https.post("/admin/api/ad", json={"users": ["alice"], "pcs": ["PC-001"]}).json()
    assert got["state"] == "off" and got["users"] == {}
    page = https.get("/admin").text
    assert 'fetch("admin/api/ad"' in page and "data-adu" in page and "data-adp" in page


def datetime_now():
    from datetime import datetime
    return datetime(2026, 11, 16, 15, tzinfo=timezone.utc)
