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

- **Stage copy of the load counter fix** (built on dev 2026-11-18,
  the user's photo of prod: "AI service GPT-5.4 824 calls, 0 rows;
  816 failed", the light green): `http.py`, `ai.py`, `status.py`,
  `usage.py`, `today.py`, `software.py`, `care.py`,
  `static/admin.html`, restart. Then the admin page's Load panel:
  GPT-5.4's failed count stops growing (today's earlier calls stay
  counted), no "rows" for it. f3b8459 (pushed 2026-11-19). Works
  (the user's screenshots, 2026-11-20): connection lights 1,123 ->
  1,132 calls in 10 minutes, failed 626 both times, no rows. Left:
  the prod copy, if that panel was stage.
- **Stage copy: Software tab leaves PCs that are gone out** (built
  on dev 2026-11-20, "build it"). From `verify --inventory` on
  stage (a95d34c): the installed software list has 6,464 PCs,
  1,266 no longer in Aternity's device list, 3 of them in use in
  8 days. Now only PCs in the device list are counted; the first
  tile says how many were left out. Copy `software.py`,
  `software_chat.py`, `agent.py`, `web.py`, `verify.py`,
  `explain.py`, `static/index.html`, restart. First
  `verify --inventory`: its 4. says how long the extra read takes
  and whether Aternity takes 25 PCs in one read ("or", never used
  on live before). Then the Software tab: about 5,198 PCs,
  "1,266 more left out".
- To watch on prod with Care step 2: how many slow start-ups a day
  (noisy or not), how many days of start-ups Aternity keeps (Show
  more says "N days with one, from ..."), which types carry the
  username.

## Agreed

- Nothing open.

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

- dbcf407: the week under "Yesterday" (the user's pick A of three
  mocked: PCs a day with a Critical event, a Major event and Major
  slow actions, Monday to Sunday, this week and last; not chosen:
  B a line per kind of event, C the last full week only) and no
  red dots on Care tiles ("makes no sense"). 16eeed7: Care list
  step 2, slow start-ups as an issue (A and B) and Active
  Directory on the tab (add by name, AD's name, "disabled in
  AD"; with it the AD lookup is done: verify --ad, the admin
  page's names, the Care list). Both on stage and prod 2026-11-17 (the user: "All good
  both stage and prod").
- f905606, a807a50, SecureView history, `verify --ai`,
  `verify --clients --except WT`: on stage / prod 2026-11-17 (the
  user: "1-5 are all good, stage/prod are same version now").

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
