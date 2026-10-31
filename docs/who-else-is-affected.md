# "Who else is affected?"

A short guide to the section on the Investigate page, and to the
"Main causes" list on the Slow start-ups page. Both answer one question:
**which other users and PCs actually had the same problem?**

## In one line

It lists PCs where Aternity **recorded the same failure**. A PC that only
has the same software or version, but never failed, is **not** listed.

## Investigate page: an application failure

When the main finding is an app crash or hang:

```
This user's failure (from Aternity)
  e.g. Outlook · Hang / Application hang
       in AddinX.dll 2.1 (inside outlook.exe)
        │
        ▼
Every PC's health events for Outlook
in the same time range (DEVICE_HEALTH_RAW)
        │
        ▼
Keep an event only if ALL match:
  ✔ same kind     (category + name: hang ≠ crash)
  ✔ same program  (HEALTH_EVENT_COMPONENT)
  ✔ same module   (HEALTH_EVENT_SUB_COMPONENT)
        │
        ▼
One row per PC:
  PC · user · version · times · last seen
```

- **Version** is shown, not matched. It tells you whether the failing
  PCs share a version (e.g. "all on 2.1"), which points at that version.
- **"Only this PC"** means no other PC had that same failure in the time
  range. Look at this PC (see "Why").

Example sentence:

> 41 other PC(s) had the same failure (Outlook Hang / Application hang in
> AddinX.dll 2.1 (inside outlook.exe 16.0)), all on 2.1. This PC has 2.1.

Click **Show who** for the list, and **Hide who** to close it.

## Investigate page: no application failure

When the main finding is not an app failure (e.g. the network), the
section lists the other users of the same apps who were **slow at the same
times**. They are grouped by the NetIM network point (gateway) they sit
behind and compared with the same hours on the 3 previous days. If most
of them share one network point, that point is named.

## Slow start-ups page: "Main causes"

For the slowest start-ups:

```
The part Aternity measured as most
"longer than normal" (its degradation time)
  e.g. Group Policy (User Logon) +45 s
        │
        ▼
That part on every PC in the time range
(MACHINE_BOOT_COMPONENTS)
        │
        ▼
Keep it only where Aternity says it was
longer than normal (degradation > 0)
        │
        ▼
One row per PC:
  PC · user · location · times ·
  most longer than normal · last seen
```

The count by location shows whether it is one site (e.g. one branch's
domain controller or link) or everywhere (e.g. a policy change).

## What it does not do

- It does not list PCs that merely have the same software or version.
- It only looks inside the time range you searched.
- It shows up to 100 PCs, then "… and N more".
