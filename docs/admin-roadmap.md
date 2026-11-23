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

## SecureView (built on dev 2026-11-17, "build it")

The devices people sit at whose name matches `secureview.match`
(the user's THIN devices for people working outside the office:
`^[A-Za-z0-9]{7}-.*THIN.*$`, any case; WT devices are not
tracked; about 1,500, about 100 in use a day). The first panel
of `/admin`, sub-tabs Today and History (mockup v3, with no AD
per device and "by department" for "by place").

How it works: the Today reader's hourly read of
`BUSINESS_ACTIVITIES_RAW` also takes `CLIENT_DEVICE_NAME` when
SecureView is on (filled on every virtual desktop action,
`verify --clients` on stage, 2026-11-17); no new read. Each
matching row widens its device's local day: first and last
heard, the hours heard, each person's own first and last, the
desktops. `secureview.py`, `<data>/secureview/<date>.json`, 400
days. Aternity has no connect or disconnect record; an idle
session sends nothing.

Decisions (the user, 2026-11-17):
- Track first heard and last heard per device per day, not
  usage by actions ("Of course timestamps").
- History only of devices Aternity has heard from ("build
  history based on devices it's heard"): silent 30, 60, 90+
  days, heard and first heard in the last 30 days.
- THIN devices are not in AD and are for users working outside
  the office: no OU or description per device; the people's AD
  name and department; "by department" with a department box.
- Named SecureView.

The days before the records: Aternity's daily data
(`BUSINESS_ACTIVITIES_DAILY`, kept ~90 days, stage Nov 10; its
$metadata has `CLIENT_DEVICE_NAME`) asked day by day for
`TIMEFRAME` and `CLIENT_DEVICE_NAME` only (Aternity adds up the
rest: a row per day and device), once the first read is done and
each night at 03:00, for each day not read in full. Days only:
no times, no people. A daily row's day is the local day its
`TIMEFRAME` plus 12 hours falls on (whose midnight Aternity uses
is not documented). The History tab says what it got, or that
the column is empty, or why the read failed.

Also built: the usual by now (the latest 5 days of the same kind
read in full with times, from 3), days not read said, a device's
90 days as squares on a click, a CSV of the list shown. AD
names: the same look-up as People using the app, 200 names a
request. Dev: made-up THIN and WT devices (`dev/sample_riverbed.py`,
`_client`), daily data kept 90 days there.

On stage (294bd37, 2026-11-17): the daily data names the device
people sat at; History reads back to Aug 19 (90 days). Still to
see: whether prod's devices match the pattern as typed (the
Today tab's count against `verify --clients`).

Built on dev 2026-11-17 ("build it"): the admin page in two folder
tabs, as the main page's: STATISTICS (the usage panels, opens first)
and SECUREVIEW. Each has its link (`/admin#statistics`,
`/admin#secureview`); a reload or login stays on it. Only the open tab
is read, every minute; the other is read the moment it is opened and
keeps its sub-tab, boxes and open device. The Day box is Statistics'
only. Log out on both. Stage copy: `static/admin.html`.

Asked 2026-11-17 (the user: "not sure I like the by department
visualization": with 15-20 departments the list beside the hour
chart leaves the space under the chart empty). Four options
mocked, waiting for a pick: A upright bars across the page (same
height whatever the count; names cut under narrow bars, slides on
a phone), B the rows across the page in 3 columns (recommended:
full names, one scale, 7 rows for 20), C the 6 biggest and "+ N
more", D a tile per department. In A, B and D a click on a
department filters the list below; the hour chart gets "Longest
in use" beside it. The user picked B; built on dev the same day
("B, build it"): Today 3 columns, the 5 longest in use beside the
hour chart; History 2 columns with "· 2 silent 90+" in a column of
its own (under its row on a phone), the list above brought into view
on a click; "no person recorded" can be picked in History's
Department box too. Stage copy: `static/admin.html`, with the tabs.

SEARCH and ACTIONS: built on dev 2026-11-17 (the user: "I need to be
able to search usage history", SecureView's; "Also add search by
user for today all actions with timestamp", on the admin page; then
"build it", with the AD name search). SEARCH is SecureView's third
sub-tab: part of a username, device or desktop (2+ characters, any
case), Enter: every day kept (up to 400), each match's days latest
first (a person: device, their own first and last, desktops; a
device: people with their times; a desktop: the devices and people
that opened it); a device's days from the daily data are one line
under its days with times; 20 matches of each kind at most, 30
days each then "Show all"; a click on a name searches it; a
username on Today or History opens SEARCH; CSV. A real name: when
nothing SecureView recorded has it, one AD search by name (every
word in display name, first name or surname; read only; nothing
kept), then those accounts' days. ACTIONS is a third admin tab
(`/admin#actions`), any username Aternity knows (the names list's
spelling, suggestions as you type): one read of the activity for
that user from local midnight to now, the columns shown only, at
most 5,000 rows (said when cut, and a reply cut short), on the Load
panel as "admin page"; time to the second, application, activity,
response time, Aternity's rating, desktop, the device sat at;
latest first; an application box, CSV, Read again. "All actions
today ▸" on a SEARCH person opens it. People are searchable from
SecureView's own records only (the daily data has no people).
Change from the plan: AD is asked only when nothing at all matched
(a device search such as "thin" had asked AD too). Stage copy:
`secureview.py`, `ad.py`, `actions.py` (new), `web.py`,
`static/admin.html`, then restart. Pushed 8301765.

History of people and times (stage, 2026-11-17: "SecureView search
only returns today for both user and device"). Was: before its own
records SecureView read Aternity's daily data for the device alone,
so a person had only today, and a device's earlier days were one
grey line under its table. The user asked for "first heard and last
heard also for all dates". Aternity keeps exact times about 8 days
(raw), the hour in its hourly data (how long: not known), the day in
its daily data (about 90 days). Agreed (A + hourly, "build it"),
built on dev 2026-11-17: each night, for each of the 90 days not
read in full, the hourly data while it has rows (each device's and
person's first and last hour, the desktops), then the daily data
(the people and desktops, no times); kept apart from SecureView's
own records, never moving a time it saw ("08:05" stays, "15:xx"
when Aternity's last hour is later). Every day is a row in Search
("07:xx": the hour only; "day only"); History's last person comes
from them too; a device's detail ends with "every day in Search".
The raw data was not read (the heaviest read; it adds minutes for
the first week only). Days read before (devices only) are read once
more. On dev (hourly made up as 35 days): 88 days in about 40 s, 34
by the hour. Stage copy: `config.py`, `secureview.py`,
`static/admin.html`.

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
  - On prod (2026-11-16, the user): "verify --ad all passed
    without and with credentials" (image 4 loaded, the account
    in prod's `.env`).
  - The look-up on the admin page, built on dev (2026-11-16, the
    user: "build it"): each person and desktop on it looked up at
    most once a day (memory only, not saved), asked after the page
    is drawn so it never waits on AD: name and department beside
    the username (title and office on hover), each desktop's OU
    and description in the day's table, "disabled in AD" / "not in
    AD" said; one line when AD is not set, does not answer (tried
    again after 5 min) or refused the login (not tried again until
    a restart: AD's lockout). AD's searches on the Load panel.
  - On prod (912aeab, 2026-11-16, the user): the display name
    shows under "People using the app". A first copy of
    `admin.html` came through the one-way transfer with its
    script and styles stripped (the page showed bare text); the
    user got it through.
  - Held (the user, 2026-11-16: "Let's hold AD integration for
    now"), then built on dev 2026-11-17 (the user: "build it ...
    all three"): the Care list's Add a user by name (PIN needed:
    the main page has no login), AD's name on a tile with no
    label, the department, "disabled in AD" / "not in AD". Only
    the list's own users are looked up (`GET /api/care/ad`); the
    status feed is unchanged.
  - Needs from the AD team: a read-only service account (its
    password in `.env`), the domain name or a DC address.
- **Tools for prod** (no paste there): run `verify` (or
  `--overview`, `--ai`) from the page; the app's log for the
  last hours and config.yaml as loaded, secrets filtered out.

## The CODE tab: a chat about the source code (built on dev 2026-11-23, "Build it")

The proposal, as it was given and agreed, follows this section's
"Built" part unchanged, so what differs can be seen.

### Built

The user, 2026-11-22: "Build it", after "We can test this
functionality in dev with nemotron", "Is this multi turn chat? Can I
ask what this 'line of code' means?" and "Look in [another project's
folder on the dev host] for nemotron info. Since we don't have gpt in
dev so use nemotron. Prod will have gpt".

- **What is there**: a fourth admin tab, CODE (`/admin#code`), two
  sub-tabs. ASK: the chat, with follow-up questions (the last 6), the
  code an answer names under it (read from the disk by the app), what
  it looked at, and what it names that it did not look at marked.
  FILES (no AI): the 36 files with their lines and what each does, a
  search of every line, a file's parts (settings, functions, classes,
  the address a web function serves) beside its code, and lines picked
  by a line number, a drag or a part for "Explain these lines ▸" /
  "Ask about them…". README.md, "Code", has every click.
- **Different from the proposal**, and why:
  - No `files` look-up: the list of files (36 lines) goes with every
    question in the instructions, so the AI spends no step on it. The
    look-ups are `search`, `outline`, `read`, `setting`.
  - A search box on FILES, with no AI (the chat's own `search`): it
    was built anyway and works when the AI is down.
  - More is marked than promised: besides a file, function or line
    not looked up, also a number found nowhere, and a name that the
    code does not have at all is said apart from one that exists but
    was not looked up.
  - A code block the AI types itself is labelled as its own, unless
    every line of it is a line of the files (the proposal only said
    the code shown is read from the disk).
  - A question is a job that only a logged-in admin can read (the
    main page's job address answers 404 for it).
  - A host that got `web.py` without `code_chat.py` still starts: the
    tab and `verify --ai` say which file to copy. Found while writing
    the list of files for the copy: without it the app would not
    have started.
- **Gaps found before any copy**, each by a test or the page check:
  "agent.py: 8 a question" was read as line 8 of agent.py, and
  "web.py:5 and 4242 tries" as lines 5 to 4242 (now a place is
  `file:first-last`, nothing around the colon, only a dash for a
  range); four tabs were 27 px wider than a phone's 400 px, so the
  whole admin page scrolled sideways (the tabs slide sideways now, as
  the main page's); the code view lost its place after Ask and back.
- **Measured on dev with Nemotron 3.5 Super** (NVIDIA's hosted API,
  as another project on the dev host uses it; `verify --ai`,
  2026-11-23): every check OK.
  "answers questions about the code (admin page)": right for 8 of 8
  in both runs (before and after the change to its instructions
  below), each question in 2.9 to 15.5 s, 1 to 7 look-ups; the admin
  password was in no answer and in nothing it looked at. In a real
  Chrome over the dev page: a question, a follow-up, a place clicked
  through to FILES, a search, a part and lines picked, "Explain these
  lines", dark, a phone's width, log out; no script error. 461 tests;
  the page check ends "0 do not" (241 agree) with the tab in it.
- **Not done, said plainly**:
  - GPT is not measured: that is stage's `verify --ai`. Nemotron on
    dev is 3.5 Super, prod's own is 3 Super: a pass on dev proves
    neither.
  - The marks prove that a file, line, name or number was in what the
    AI looked at. They do not prove its explanation of the code is
    right: on dev one answer said environment variables override
    `refresh_minutes`, which no line of the code says (it took it
    from config.py's first sentence in the list of files). The
    instructions now say the list is no proof of how a setting
    behaves; asked again, the answer was right, from 4 look-ups.
    That lowers it, it does not rule it out.
  - Why something was decided is known only from the code's own
    comments: CLAUDE.md and `docs/` are not on the live host.
  - Where a function of a page's script ends is read from the layout
    (the "}" at the start of a line), not parsed; the styles and the
    HTML of a page are not listed as parts (they are searched and
    read as lines).
  - No count of code questions on STATISTICS (nothing is kept); the
    AI calls are on the Load panel as "code chat".
- **Copy**: stage, 4 files: `src/uxcorrelate/code_chat.py` (new),
  `src/uxcorrelate/web.py`, `src/uxcorrelate/verify.py`,
  `src/uxcorrelate/static/admin.html`; restart; no image, compose,
  k8s or config change (stage's `ai:` is already set). Prod after
  stage: those with the 11 waiting since 596cd86, 12 files in all
  (three of the four are among the 11).

### The proposal (2026-11-22)

The user: "I want to have a chat on admin to inquire source codes.
I want to know what every .py file does, I want to able able to ask
where a configuration is, which part in the script does xx/yy. I
want the chat in prod using gpt and have the similar conversation
with you. Provide proposal". Proposal given; waiting for "build it".

- **What it is**: a fourth tab on the admin page, CODE (link
  `#code`), behind the admin login. Two parts.
  1. Files, no AI: every source file with its lines and what it
     does (the file's own first sentence; every `.py` file has
     one, 33 files). A click: its functions, classes and settings
     with line numbers; a click on one: its code. Right even when
     the AI is down.
  2. A chat with GPT (`ai.chat`'s service), with follow-up
     questions. "Explain" on a file or function of part 1 asks it.
     Follow-ups as Ask anything's (the user, 2026-11-22: "Is this
     multi turn chat?"): the last 6 questions and answers go with
     each new one, with a line of what was looked up; kept in the
     page only, gone at a reload or log-out. A line of code (the
     user: "Can I ask what this 'line of code' means?"; added to
     the proposal): click a line or select a few in part 1's code,
     "Explain this ▸" sends the file, the line numbers and the
     function around them; or type "line 412 of software.py"; or
     paste the line (`search` finds it, `read` gives what is
     around it). The lines are shown over the answer, read from
     the disk by our code.
- **Why look-ups, not the whole code**: the source is 1.6 MB (26,500
  lines with the two pages); GPT read 40,000 characters right on
  stage. So GPT looks things up in the files on that host, as Ask
  anything does with Aternity (up to 8 a question, 24,000
  characters each): `files`, `search` (a word or name: file and
  line), `outline` (one file's functions and settings), `read`
  (lines of one file), `setting` (a config.yaml key: where it is
  read, its default, its limits).
- **Guards**: read only; the app's own source only, never `.env`,
  `config.yaml`, certificates or `data` (no password, no PC or user
  name; a setting's place and default, never its value on that
  host). Every file, function and line an answer names must be in
  what was looked up, else it is marked. The code shown under an
  answer is read from the disk by our code, not written by the AI.
  One question at a time, counted on the Load panel, nothing saved.
- **What it cannot do**: change or run code. Say why something was
  decided beyond what the code's own comments say: CLAUDE.md and
  `docs/` are not on prod (the bundle carries README.md and `src`).
  Be as good as a session with Claude Code: slower, shorter chains;
  how good is not known until measured.
- **Build**: one new file (`code_chat.py`; Python's own `ast`, no
  new library, no new image), `web.py`, `admin.html`, `usage.py`
  (the Load panel's purpose), `verify.py` (`verify --ai`: "answers
  questions about the code", about 10 questions with a known file
  and function, both models), tests, the page check.
- **Test on dev first** (the user, 2026-11-22: "We can test this
  functionality in dev with nemotron"): build it on dev, measure it
  there with Nemotron (`verify --ai`, and the tab in a browser),
  stage with GPT only after that. Dev had no AI service (no `ai:`
  in its config.yaml, no key in its `.env`, two lights only). Set
  on 2026-11-22 (the user: "Look in [another project's folder on
  the dev host] for nemotron info. Since we don't have gpt in dev
  so use nemotron. Prod will have gpt"): the same service that
  project uses on dev, NVIDIA's hosted API with Nemotron 3.5 Super
  (prod's own Nemotron is 3 Super), as `ai:` in dev's config.yaml
  and the key as `AI_KEY_NEMOTRON` in dev's `.env`, copied from
  that project's `.env` without being shown. What the chats send
  leaves the dev host (dev's data is made up; that project's own
  note: free tiers may train on prompts). `verify --ai` on dev:
  reachable, plain reply, the software look-up 12 of 12, explains
  a panel 12 of 12, a long page right up to 40,000 characters, the
  assessment, all OK; Ask anything 6 of 7 (one answer held a
  number, 13, that was in none of its look-ups). Ask anything on
  the dev page answers now, third light green. Passing on dev with
  Nemotron does not prove GPT's answers: GPT is still measured on
  stage before prod.
- **Open choices** (my recommendation first): source only at first,
  docs later if answers lack the why (CLAUDE.md changes with nearly
  every commit: one more file in each copy); admin page only, not
  the main page.

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
12. User (2026-11-16): "verify --ad all passed without and with
    credentials". Proposed next: names on the admin page.
13. User: "build it". Built on dev (AD lookup, above).
14. User: "adin page show user display name under 'People using
    the app'. What's next?" Proposed: AD on the Care list.
15. User: "Let's hold AD integration for now." On hold; the
    admin page's names stay.
16. User (2026-11-22): "Build A and admin too" (the main page's
    Parchment | Dark switch; docs/roadmap.md has the request and
    the three looks mocked). Built on dev: the same switch at the
    right of the admin page's bar, also on the log-in screen, kept
    under the main page's name so both agree when opened under one
    address. Its dark colours are the main page's; SecureView's
    own four (weekend bars, the usual's band, open rows, groups
    that are not a department) keep their dark values.
