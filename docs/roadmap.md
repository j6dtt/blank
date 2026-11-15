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

## Waiting on you

- **Admin page fix on prod**: "waiting for Aternity" instead
  of an early "not known"; unnamed addresses not counted as
  people (`usage.py`, `static/admin.html`).
- **Stage copy of 13a023b**: `care.py`, `explain.py`,
  `static/index.html`; restart; a tile with "1 rated Major"
  lists it under "Rated Major today".
- **`verify --overview` on stage**: the failing-disk rule waits
  for this evidence (stage showed Low Disk Space and Corrupted
  FS under Aternity's "(Drive)" label with HD Failure).
- **`verify --ai` on stage**: the Care list samples changed.

## Agreed

- **Care list step 2**: slow start-ups as an issue; alerts out
  of the app (needs a mail relay or chat webhook: not known
  yet). Then a week on stage to judge how noisy it is.

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
- **AD lookup** (2026-11-15, proposed): names, title,
  department and office for Aternity's usernames, on the admin
  page first; then the Care list (add by name, labels, accounts
  disabled in AD) and who else is affected by department. See
  `docs/admin-roadmap.md`.
- **Software tab**, to confirm with data: version spread,
  one-user installs, programs on few PCs.

## Parked

- **Kubernetes**: prod runs on Docker for now (user,
  2026-11-15); `k8s/uxcorrelate.yaml` is kept in step with
  `compose.yaml`. Open there: the load balancer's address in
  the log, cpu / memory numbers, only one user could connect.

## Done (latest)

- 13a023b: Care list judges each Major action by its own time
  of day; every tile lists its Major actions today.
- 1c291e0: Care tile bars are Major actions per day from
  Aternity.
- 3d0f674: gap review: a read that failed or was cut short is
  said, never "nothing found".
- fa41a96: Care list "Arrange tiles".
