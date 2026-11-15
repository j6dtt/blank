# Admin page roadmap

A page for whoever runs the app: who uses it and how. Kept apart
from `docs/roadmap.md` (user, 2026-11-15). Updated as we agree on
things; each decision is dated.

## Decided

- **Where**: `/admin` on the main app's port. No link from the
  main app: only those who know the URL, and a login for the
  admin (user, 2026-11-15).
- **Prod runs on Docker** for now (user, 2026-11-15), so the
  page is built for Docker compose first; `k8s/` kept in step.
- **Active users** are found by the caller's address, looked
  up in Aternity (the main app has no login). Most people use
  the app on virtual desktops (user, 2026-11-15).
- **No line on the main page** saying use is recorded (user,
  2026-11-15).
- **No check on prod first**: the page itself says when every
  visit comes from one address (a proxy in front of the app),
  so people cannot be told apart. Nothing is guessed.

## First version (built 2026-11-15, "Build it"; 16d699b)

How it works: the app counts each use as it happens and saves
the counts of a day in `data/usage/<date>.json`; `/admin` reads
them after a login. Built as listed below, with the open choices
as recommended (user: "Build it"). Found and fixed while
checking on dev:
- NetIM's parallel interface reads were counted as "other" (its
  pool's threads lost what the app was doing).
- Uses since the last write were lost on a restart (now written
  at shutdown too).
- The read at start (24 hours) is shown apart from the regular
  reads.

On prod (copied by the user, 2026-11-15): first "not known",
then the user's own name once a read had looked ("it works
now"): the app sees the desktops' own addresses on prod's
Docker. Bug found: the page said "not known" from the first
second, before the next read of Aternity had looked; now
"waiting for Aternity" until 2 hours after the visit. Also
fixed: rows with no name were counted as people (the heading
and the days strip). Not committed yet.

1. **Login.** One password, `ADMIN_PASSWORD` in `.env` (never
   in config.yaml). In-page form, a cookie for 8 h, Log out.
   5 wrong tries: locked 15 min. Each login, wrong password and
   lock logged in plain English with the caller's address. No
   new library (the bundle stays the same size).
2. **Active users.** Aternity's activity rows carry
   `DEVICE_IP_ADDRESS`, `USERNAME` and `DEVICE_NAME`; the Today
   reader reads them every 15 min (no extra load). The action
   from the caller's address nearest the visit, within the
   hour, gives the user and desktop (a pooled desktop: whoever
   was on it then).
   - Now (last 15 min): user, desktop, tab, last visit.
   - Per day: each person's first and last visit, tabs used,
     how many investigations and questions they ran.
   - A visit Aternity cannot place: "not known" + its address.
   - Every visit from one address: "people cannot be told
     apart" (a proxy hides the desktops).
3. **Use of the app.**
   - Pages opened per day and hour, by tab; busiest hours.
   - Investigations: by hand, by the Care list, by Ask
     anything; how long each took; how often each headline kind
     came (failing disk, crash, network, start-up…).
   - Start-ups runs, Software clicks, Today's Investigate ▸.
   - Ask anything: questions, look-ups per question, answer
     time, numbers flagged, look-ups that failed. Explain uses.
   - Care list: users listed, times a tile lit, investigations it ran,
     changes (add, remove, label, order), wrong PINs.
   - `/api/MSS/care/status`: polls per day (about 288 at one
     every 5 min) and the last one, so a stop is seen.
   - Load on Riverbed: Aternity and NetIM reads per hour (how
     many, rows, seconds, retries, failed or cut short), the
     source that costs most, Today reads longer than
     `refresh_minutes`.

Choices (taken as recommended with "Build it", 2026-11-15):
- Usernames and desktops on disk 30 days, then deleted.
- Login on the HTTPS port only: the password is readable on
  plain HTTP.
- Keep Ask anything's question text, 30 days, admin only.

Known limits of the first version:
- The lock after 5 wrong passwords is per address: behind one
  proxy address, one person's mistakes lock everyone out for 15
  minutes.
- Logins are in memory: a restart logs everyone out.
- An address's rows are kept from its first visit of the day:
  an hour already read for the last time before that is not read
  again. Someone whose last action was in that hour, with none
  since, is "not known" (example: a visit at 10:20, the last
  action at 09:50, the 09:00 hour read for the last time at
  10:15).
- Not counted: the Software reader's daily `$metadata` read.

## Later (ideas)

- **Data freshness**: how late Aternity sends rows, per hour
  (explains "Aternity sent it at 18:10"); the oldest day it
  still holds (about 8).
- **Connections over time**: each Aternity / NetIM / AI outage
  per day ("Aternity down 14:02 to 14:20"); the lights only
  show the state now.
- **AI services**: calls per service, seconds, failures,
  answers replaced by our own words.
- **App health**: version, uptime, restarts, unexpected errors
  (the last 20), running and queued jobs, size of each `data/`
  folder, HTTPS certificate expiry, config.yaml notes.
- **AD lookup of users and computers** (asked 2026-11-15; next
  step, the user: "Create a new image with AD users/computers
  lookup tools"): each username looked up in Active Directory
  (LDAP, a read-only service account) for name, title,
  department and office; each desktop's computer object for its
  description, OU, operating system and whether it is enabled;
  kept a day. On the admin page first ("Pat Lee, Finance" beside
  sample40); later the Care list's Add a user by name, its
  labels and accounts disabled in AD, and who else is affected
  grouped by department.
  - Image 4 (built 2026-11-15): ldap3 (LDAP),
    dnspython (finds the domain controllers from DNS SRV
    records), pycryptodome (MD4, for ldap3's NTLM login: this
    Python's OpenSSL 3 has none). Bundle 16 MB (limit 25).
    Image 3's libraries unchanged (pinned in `constraints.txt`).
  - Login, recommended: the service account over LDAPS (636), or
    StartTLS on 389. NTLM is there too, but ldap3's NTLM cannot
    sign, so a domain that requires LDAP signing refuses it.
    Kerberos is not in the image (C libraries and a keytab).
  - `verify --ad` built on dev (2026-11-15, the user: "build
    it"), tested against a real Samba DC (dev/compose.yaml
    `sample-ad`): the domain, its DCs (DNS SRV), 636 / 389 /
    StartTLS on up to 3 DCs, a DC's header with no login, the
    login (LDAPS, StartTLS, then NTLM; a password never over
    plain LDAP; a refused password stops: AD's lockout), then
    "N of 50" of Aternity's most active usernames and PCs of the
    last hour in AD, and how Aternity writes them. Counts only;
    `--user` / `--pc` for one. NTLM not proven on dev (Samba has
    no Microsoft NTLM bind for LDAP).
  - To find out on prod with it: the answers to the points
    above, first with no account (part 1 needs none), then with
    the service account.
  - Next, once prod's answers are in: the look-up on the admin
    page (name and department beside each person), kept a day.
  - Needs from the AD team: a read-only service account (its
    password in `.env`), the domain name or a DC address.
- **Tools for prod** (no paste there): run `verify` (or
  `--overview`, `--ai`) from the page; the app's log for the
  last hours and config.yaml as loaded, secrets filtered out.

## To find out on stage

- Whether something in front of the app hides the desktops'
  addresses (the page will say).
- On VPN: whether Aternity records the VPN address or the PC's
  own (test with your own PC).
- A server desktop shared by many users: one address, shown as
  "one of N users".
- A PC with no Aternity agent, or no action in the last hour:
  "not known".

## Dropped, and why

- **A second port** (reached by `kubectl port-forward` or an
  SSH tunnel): proposed first; the user chose the URL and a
  login instead.
- **Counting browsers** (a random ID kept in each browser):
  counts browsers, not people; the address lookup names them.
- **Windows sign-in (Kerberos)**: real names, but needs AD work
  (an SPN and a keytab) and a new library. It is the only way
  the app sees the Windows username itself (2026-11-15): kept
  in mind for a login on the main app later.
- **"Your name?" asked once**: people can type anything.
- **Which named users or PCs people looked up**: personal data;
  the page counts investigations, not whom (the question text
  is an open choice above).

## Discussion so far (2026-11-15)

1. User: "Need a backend admin page to view usage statistics,
   no link from the main app. Ideas of what are on the admin
   page?" Proposed: counts per day in `data/usage/`, a second
   port not exposed (no link only hides it: the main app has no
   login), sections: people using the app, load on Riverbed, AI
   services, app health, the other platform's polls.
2. User: "Prod is currently running on docker at the moment.
   To access admin page users have to know the URL, we can add
   login for admin. Revise proposals, more ideas." Proposed:
   the login above; 9 sections (first version: use of the app,
   Ask anything and Explain, Care list, load on Riverbed; later:
   data freshness, connections over time, AI services, app
   health, tools for prod).
3. User: "I want to have active users using the application.
   How do we reverse lookup since the main app has no login?"
   Proposed: the caller's address looked up in Aternity's
   activity; when it fails (a proxy, shared server desktops,
   VPN, no agent); other ways (Kerberos, a name asked once).
4. User: "I'm not connected to prod to check but the people
   use the app are on vdi mostly." Proposed: no prod check
   first, the page says when addresses cannot tell people
   apart; the first version above.
5. User: a roadmap kept up as we discuss; the admin page in a
   file of its own (this one).
6. User: "Remove use is recorded on main app. What are the
   benefits of integrateing AD users lookup for enrichment?"
   Decided: no line on the main page. Proposed: AD lookup
   (Later, above).
7. User: "can the application see the windows username when a
   user connects to the app?" Answer: not by itself; only with
   Windows sign-in (Kerberos, "Negotiate"): the browser on a
   domain-joined desktop sends the user's ticket without a
   prompt. Needs an SPN and keytab from the AD team, the app's
   DNS name trusted by the browsers (a GPO), a library.
8. User: "What's SPN?" Answered (the name the AD team registers
   for the app's address, so Windows can issue tickets for it).
9. User: "Build it". Built the first version above.
10. User: "Create a new image with AD users/computers lookup
    tools". Image 4 (ldap3, dnspython, pycryptodome).
11. User: "should we start on verify --ad?", then "build it".
    Built on dev (AD lookup, above).
