# Roadmap

What is being built, what is agreed, what waits on a check or a
decision, and the ideas not chosen yet. Updated as we agree on
things; each decision is dated. Made 2026-11-15.

Status words:
- **Now**: being built.
- **Agreed**: decided, not started.
- **Waiting on you**: a check on stage / prod, or a choice.
- **Idea**: proposed, not chosen.
- **Parked**: on hold, kept in step.

## Now

### Admin page (`/admin`)
Usage statistics and active users, behind a login, no link from
the main app. First version committed 16d699b (2026-11-15).
Its own roadmap: `docs/admin-roadmap.md` (decided, first
version, limits, later ideas, the discussion so far).

### SecureView (admin page)
The devices people sit at to work outside the office (THIN,
`secureview.match`): first and last heard per device per day,
Today and History, silent 30 / 60 / 90 days, by department from
AD. Built on dev 2026-11-17 ("build it"), 294bd37; on stage the
same day: the daily data names the devices, History back to
Aug 19. Then the admin page in two tabs, Statistics and
SecureView, and By department across the page (option B), both
built on dev 2026-11-17 ("build it"), bb76884. Then SEARCH and
ACTIONS, built on dev 2026-11-17 ("build it", with the AD name
search): SecureView's third sub-tab searches every day kept by a
username, device, desktop or a person's real name (AD); a third
admin tab, ACTIONS, lists every action of any user today, read
from Aternity on Show; pushed 8301765, on stage. Then every day of
people and times in Search (the user: "first heard and last heard
also for all dates"), built on dev 2026-11-17: Aternity's hourly
data, then its daily data with the people and desktops. See
`docs/admin-roadmap.md`.

## Waiting on you

- **Stage copy of Care list step 2** (built on dev 2026-11-17):
  `aternity.py`, `assess.py`, `care.py`, `ad.py`, `web.py`,
  `explain.py`, `static/index.html`, restart. To learn on stage:
  each Care user's Show more, "Start-ups today" (how many days of
  start-ups Aternity keeps, which types carry the username), slow
  start-ups a day; with `ad:` set, the names and departments on the
  tiles, Add by name.
- **Stage copy of the week under "Yesterday"** and **no red dots
  on Care tiles** (both built on dev 2026-11-17): `assess.py`,
  `care.py`, `explain.py`, `static/index.html`, with the step 2
  copy above. To look at on stage: the Today tab's assessment box,
  under Yesterday: three rows, last week dashed, gaps where a day
  was not read in full.
- Done (the user, 2026-11-17: "1-5 are all good, stage/prod are
  same version now"): the stage copy of SecureView's history of
  people and times (f905606); `verify --ai` on stage; the prod
  copy of a807a50; `verify --clients --except WT` (first run on
  stage, a quiet hour: 52 devices behind 52 virtual desktops, 8
  not starting with WT, ending THIN / THINE / THINVM;
  `LOCATION_ON_VPN` 'false' on every action; decided: SecureView).

## Agreed

- **A week of issues under "Yesterday"** (the user, 2026-11-17:
  "a lines chart under 'Yesterday, the day in short', showing
  issues counts by day from Mon-Sun"; three mocked, the user
  picked A; "build it"): built on dev 2026-11-17. PCs a day with
  a Critical event, a Major event and Major slow actions, Monday
  to Sunday; a row per count with its own scale; this week solid,
  today hollow (so far), last week dashed; no new Aternity read.
  Not chosen: B (a line per kind of event, this week), C (the
  last full week only).
- **No red dots on Care tiles** (the user, 2026-11-17: "makes no
  sense": a dot marked any saved issue of a day, a crash too, on
  a bar of Major actions counted up to now): removed on dev.

- **Care list step 2, built on dev 2026-11-17** ("build it A and
  B, all three"): slow start-ups as an issue (A and B: longer than
  every one of theirs of that type in the 30 days before, and than
  1 in 10 of everyone's today), and Active Directory on the tab
  (add a user by name, AD's name when no label, "disabled in AD").
  Alerts out of the app: not for now (the user, 2026-11-17). Stage
  copy, then a week on stage to judge how noisy it is: how many
  slow start-ups a day, and how long Aternity keeps start-ups (Show
  more says "N days with one, from …").

## Ideas

- **Assessment**: a weekly roll-up; sending it by email (the
  app sends nothing today); weekends (it runs every day now).
  Does Aternity mark an event resolved (`HEALTH_EVENT_STATUS`,
  never read on prod)? A verify line offered.
- **Active users and PCs**: PCs that only report to Aternity
  (`TIME_LAST_REPORTING`, needs a verify line first); a verify
  line listing PCs marked virtual / physical / not marked for
  the desktop team.
- **Data meanings register** (`docs/data-meanings.md`, M1-M22,
  paused 2026-11-09): `# meaning Mx` tags in src, a test keeping
  them in step, "Meaning checks" in verify.
- **Ask anything**: hangs per app and software changes as
  look-ups (measure their row counts with verify first).
- **Software tab**, to confirm with data: version spread,
  one-user installs, programs on few PCs.

## Parked

- **AD lookup of users and computers** (2026-11-15, the user:
  "next step in roadmap"): image 4 has the libraries (ldap3,
  dnspython, pycryptodome); `verify --ad` passed on prod
  with and without the account (2026-11-16, the user). Names on
  the admin page on prod (912aeab, 2026-11-16). The Care list
  (name search at Add, labels from AD, "disabled in AD" on the
  tiles) on hold (the user, 2026-11-16: "Let's hold AD
  integration for now"). See `docs/admin-roadmap.md`.
- **Kubernetes**: prod runs on Docker for now (user,
  2026-11-15); `k8s/uxcorrelate.yaml` is kept in step with
  `compose.yaml`. The way in switches between the ingress and
  a load balancer with the pod's own TLS (2026-11-16):
  `k8s/ingress.yaml`, `k8s/loadbalancer.yaml`, one apply + one
  delete. Not used until the users' real addresses reach the
  pod (the user, 2026-11-16: "I'm not using k8s til I figure
  out passing real client IPs"); the admin page is the check
  (each visitor named, or "every visit from one address").
  Also open: cpu / memory numbers, only one user could connect.

## Done (latest)

- Failing disk needs Aternity's category Hardware, not only its
  "(Disk)" / "(Drive)" label (built on dev 2026-11-16, from
  `verify --overview` on stage: Low Disk Space 8 PCs and
  Corrupted FS 2 PCs under "(Drive)" are System; they had led
  investigations and lit Care tiles all day). a807a50 (pushed);
  on stage 2026-11-16 (verify --overview: Low Disk Space and
  Corrupted FS "not a failing disk", HD Failure and S.M.A.R.T.
  "a failing disk"); prod copy next.

- 912aeab: AD names on the admin page; on prod 2026-11-16 (the
  user: the display name shows under "People using the app").
  With it the admin page fix of 5c9a735.

- c4435b0: image 4 and `verify --ad`; on prod 2026-11-16,
  all passed with and without the account.
- 13a023b: Care list judges each Major action by its own time
  of day; every tile lists its Major actions today.
  On stage (2026-11-16: its three files match there).
- 1c291e0: Care tile bars are Major actions per day from
  Aternity.
- 3d0f674: gap review: a read that failed or was cut short is
  said, never "nothing found".
- fa41a96: Care list "Arrange tiles".
