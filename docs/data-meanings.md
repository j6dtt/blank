# What Aternity's data means: the register

Every meaning the tool gives to an Aternity value, with how sure we are.
The page shows conclusions built on these; a wrong meaning gives a wrong
conclusion even when the code is right.

**Status**
- **CONFIRMED**: Riverbed documentation or the Aternity admin said so.
- **OBSERVED**: seen in prod data (the date says when); not documented.
- **ASSUMED**: our reading; nothing has confirmed it.
- **UNKNOWN**: we do not know; the tool must not state it.

The Riverbed `$metadata` in `tests/fixtures/` gives column names and
types only: no units, no descriptions. So nothing here is CONFIRMED yet.

Each entry: what we take it to mean, the evidence, what on the page
rests on it, how to confirm it (admin question = `docs/aternity-admin-
questions.md`).

## Activities (`BUSINESS_ACTIVITIES_RAW`)

**M1. `SLA_STATUS` 'Minor' / 'Major' means the action was slow for the user.**
ASSUMED. Aternity's own verdict against each activity's limits; the
limits and what 'Undefined' means are not known.
Rests on it: every "slow action" count, the headline.
Confirm: admin questions 7 and 13.

**M2. `ACTIVITY_RESPONSE_TIME` is in seconds.**
OBSERVED (values such as 0.2 and 43.8, Nov 2026). Rests on it: every
"took X s". Confirm: admin question 13.

**M3. Client, network and backend time split the response time into PC,
network and server.**
ASSUMED from the names. Rests on it: "Aternity measured the slow time:
PC 82%, network 7%, server 11%". Confirm: admin question 13.

**M4. CPU, memory and disk queue on an activity row (`HRC_CPU_UTIL`, …)
were measured during that action.**
ASSUMED. Two actions with the same timestamp carry different values
(OBSERVED, Nov 2026): that fits a reading per action, but does not prove
it. The column name (`HRC_…`) is the device health source's too, which
could mean either. Rests on it: "a reading counts only for its own
action", "CPU 88.98% … none with a slow action". Confirm: admin question
11; `verify` (planned check).

**M5. `VIRTUALIZATION` 'true' means a virtual desktop (VMware Horizon),
pooled: one user moves between desktops.**
OBSERVED (612 of 1000 rows; one user on 2 desktops in 3 days, Nov 2026).
Rests on it: "virtual desktop", start-ups compared across the user's
desktops. Confirm: the desktop team.

**M6. `CLIENT_DEVICE_NAME` is the device the user sat at.**
ASSUMED from the name. Rests on it: "virtual desktop from WT…".
Confirm: admin question 1.

**M7. "N/A" means no value.**
OBSERVED. Rests on it: blanks everywhere.

**M8. Activities are kept about 8 days.**
OBSERVED (Nov 2026). Rests on it: every "usual" and "before" count.
Confirm: admin question 9.

## Start-ups (`WINDOWS_MACHINE_BOOTS`, `MACHINE_BOOT_COMPONENTS`)

**M9. `MEASUREMENT_DURATION_AVG` is in seconds.**
ASSUMED from values (1.9 to 426). Rests on it: "User Logon 37.4 s".
Confirm: admin question 7.

**M10. A start-up part belongs to the latest start-up row of its PC at or
before it, within an hour.**
ASSUMED (no key links them). Rests on it: what took a start-up's time.

**M11. A part's degradation is Aternity's own "longer than normal".**
ASSUMED from the name. Rests on it: "N s longer than normal".

## Health events and app crashes / hangs

**M12. `APPLICATION_HANG_TIME_TOTAL` has no known unit.**
UNKNOWN (values 3607 to 30089). The tool never shows it.
Confirm: admin question 7.

**M13. A module "unknown (Dll)" or version "0.0.0.0" means no module.**
ASSUMED. Rests on it: where an app failed.

**M14. 'Background Process Crash' is a process not tied to the user's
app.**
ASSUMED from the name. Rests on it: background crashes counted at any
slow action, "Other things".

**M15. 'Low Virtual Memory' with "Percent Remaining Memory: 100%".**
UNKNOWN. Confirm: admin question 7.

## Software change log (`INSTALLED_SOFTWARE_CHANGE_LOG`)

**M16. The change's time is when it happened.**
OBSERVED true on physical PCs (1 of 282 failures ran a version before its
logged time), late on virtual desktops (509 of 2,168; mostly under a
day), Nov 2026. Rests on it: "changed before it became slow", possible
causes. Confirm: admin question 8.

**M17. An "Uninstalled" and an "Installed" row of one software within a
few minutes are one update.**
ASSUMED (real updates seen 2 min apart, Nov 2026). Rests on it: "updated
1.26.08901 → 1.26.18801".

**M18. 5 or more "Uninstalled" rows with no install in one minute are not
real removals (an image refresh).**
OBSERVED: Microsoft 365 Apps among them while Outlook kept running
(Nov 2026). The cause is ASSUMED. Rests on it: "27 programs logged as
uninstalled … not changes to check". Confirm: admin question 8.

**M19. The same version removed then put back is the log, not a change.**
OBSERVED (Chrome, Notepad++ daily, Nov 2026); the cause is UNKNOWN.
Rests on it: "comes and goes in Aternity's log". Confirm: admin
question 4.

**M20. The change log shows changes, not what is installed.**
OBSERVED. A desktop with no change logged may still have the new version.
Rests on it: "none on <desktop>: they do not explain it alone" says what
was logged, nothing more. Confirm: admin question 10.

## Per-process data (`APPLICATION_RESOURCES_HOURLY`)

**M21. One row per process per hour; CPU is percent of the whole PC;
memory (`PRC_PHYSICAL_MEMORY_UTIL_*`) is bytes.**
ASSUMED from values (CPU up to 80; memory 241664 to 8.2e9), Nov 2026.
Rests on it: "explorer CPU up to 17.5%", MB figures. Confirm: admin
question 3.

## Shown as given, not interpreted

- `APPLICATION_UXI_AVG` ("Aternity UXI: avg 3.5"): shown for IT, no
  meaning drawn from it.

## Not meanings: our own rules

Choices the tool makes on top of the data (limits, the 5-min window,
"became slow", the headline order) are in `docs/headline-rules.md` and
`CLAUDE.md`. They are judgments, not facts about the data.
