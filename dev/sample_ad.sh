#!/usr/bin/env bash
# A made-up Active Directory for the dev app (dev/compose.yaml, service sample-ad): a real
# Samba domain controller, so `uxcorrelate verify --ad` meets real DNS SRV records, LDAPS,
# StartTLS and logins (not NTLM: Samba has no Microsoft NTLM bind for LDAP). Not part of the
# product or the bundle.
#
# Domain ad.sample.test (NetBIOS SAMPLE; .test is reserved, RFC 2606). It holds the names of
# the made-up Aternity (dev/sample_riverbed.py), with gaps on purpose:
#   users     sample01..sample47 in OU=Staff (title, department, office); sample48 disabled;
#             sample49, sample50 not in AD
#   computers PC-001..PC-034 in Desktops / Physical, VD-01..VD-16 in Desktops / Virtual
#             desktops (description, operating system); PC-034 disabled; PC-035, PC-036
#             not in AD
#   the app's read-only account: AD_USERNAME / AD_PASSWORD from the repo's .env
# The domain's own administrator password is random and never needed.
set -euo pipefail
: "${AD_USERNAME:?put AD_USERNAME=... in the repo's .env}"
: "${AD_PASSWORD:?put AD_PASSWORD=... in the repo's .env}"

export REALM=AD.SAMPLE.TEST DOMAIN=SAMPLE DNS_FORWARDER=127.0.0.11
# listen on every interface: the image's own choice reads Docker's "eth0@ifNN" as a name, so
# Samba listened on 127.0.0.1 alone (no DNS, no LDAP from the app)
export BIND_NETWORK_INTERFACES=false
export ADMIN_PASS="Aa1-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
BASE="DC=ad,DC=sample,DC=test"
SAM=/usr/local/samba/private/sam.ldb

samba-domain-provision >/dev/null
samba -F &
SAMBA=$!

have() { samba-tool "$@" >/dev/null 2>&1; }
# a lockout policy, as a real domain has: Samba counts wrong passwords (badPwdCount) only with one
samba-tool domain passwordsettings set --account-lockout-threshold=5 --account-lockout-duration=15 \
  --reset-account-lockout-after=15 >/dev/null
TITLES=("Analyst" "Planner" "Engineer" "Coordinator" "Officer" "Technician")
DEPTS=("Operations" "Finance" "Logistics" "Planning" "Support")

ous=$(samba-tool ou list 2>/dev/null || true)
for ou in "OU=Staff" "OU=Desktops" "OU=Physical,OU=Desktops" "OU=Virtual desktops,OU=Desktops"; do
  grep -qxF "$ou" <<<"$ous" || samba-tool ou create "$ou" >/dev/null
done

for n in $(seq 1 48); do
  u=$(printf "sample%02d" "$n")
  have user show "$u" && continue
  samba-tool user create "$u" "Aa1-$(head -c 18 /dev/urandom | base64 | tr -d '/+=')" \
    --userou="OU=Staff" --given-name="Sample" --surname="User $(printf %02d "$n")" \
    --job-title="${TITLES[$((n % 6))]}" --department="${DEPTS[$((n % 5))]}" \
    --physical-delivery-office="Building $((n % 4 + 1))" >/dev/null
done
have user show sample48 && samba-tool user disable sample48 >/dev/null

if ! have user show "$AD_USERNAME"; then
  samba-tool user create "$AD_USERNAME" "$AD_PASSWORD" --description="UX Correlator, read only (dev)" >/dev/null
  samba-tool user setexpiry "$AD_USERNAME" --noexpiry >/dev/null
fi

ldif=$(mktemp)
for n in $(seq 1 34); do
  pc=$(printf "PC-%03d" "$n")
  have computer show "$pc" || samba-tool computer create "$pc" --computerou="OU=Physical,OU=Desktops" \
    --description="Desk $n, building $((n % 4 + 1))" >/dev/null
  uac=$([ "$n" = 34 ] && echo 4098 || echo 4096)  # in use: enabled (samba-tool makes a PC disabled)
  printf 'dn: CN=%s,OU=Physical,OU=Desktops,%s\nchangetype: modify\nreplace: operatingSystem\noperatingSystem: Windows 11 Enterprise\n-\nreplace: userAccountControl\nuserAccountControl: %s\n\n' "$pc" "$BASE" "$uac" >> "$ldif"
done
for n in $(seq 1 16); do
  pc=$(printf "VD-%02d" "$n")
  have computer show "$pc" || samba-tool computer create "$pc" --computerou="OU=Virtual desktops,OU=Desktops" \
    --description="Pooled virtual desktop" >/dev/null
  printf 'dn: CN=%s,OU=Virtual desktops,OU=Desktops,%s\nchangetype: modify\nreplace: operatingSystem\noperatingSystem: Windows 10 Enterprise multi-session\n-\nreplace: userAccountControl\nuserAccountControl: 4096\n\n' "$pc" "$BASE" >> "$ldif"
done
ldbmodify -H "$SAM" "$ldif" >/dev/null
rm -f "$ldif"
echo "sample AD ready: ad.sample.test, 47 users + 1 disabled, 49 computers + 1 disabled, account $AD_USERNAME"
wait "$SAMBA"
