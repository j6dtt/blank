# Who else

A short guide to "Who else" under each issue on the Investigate page, the
"Other users of these apps" panel, and the "Main causes" list on the Slow
start-ups page.

## In one line

"Who else" lists PCs where Aternity **recorded the same failure**. A PC that
only has the same software or version, but never failed, is **not** listed.

## Under each issue

Every hang, crash or health event says it in the same shape:

```
This PC's failure (from Aternity)
  e.g. Outlook · Application hang
       in AddinX.dll 2.1 (inside outlook.exe)
        │
        ▼
Every PC's health events for Outlook,
or for the process if no app (DEVICE_HEALTH_RAW)
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

Example:

> Who else: 41 other PCs (38 users) of 900 PCs using Outlook, all on 2.1.
> This PC has 2.1. On other PCs since Nov 3.

- **Users** are counted too: on pooled virtual desktops one person moves
  between PCs, so PCs alone overstate it.
- **Of how many:** for an app, the PCs using it in the window
  (`APPLICATIONS_RAW`); for a background process, the PCs in
  `DEVICE_INVENTORY`.
- **Version** is shown, not matched. "All on 2.1" points at that version.
- **Since when:** other PCs' same failure in the 14 days before the window
  (for up to 5 failures, when 2 or more other PCs have it).
- **A hang with no details:** Aternity's app usage on every PC is read
  instead, so it is "PCs where this app hung", not "the same failure".
- **"Only this PC"** means no other PC had it in the time range.
- **One other PC** is told, but "what to do" stays about this PC: it takes
  two others to call it more than this PC's problem.

Click **Show who** under an issue for the list, **Hide who** to close it.

## "Other users of these apps"

Only when something was slow. It lists other users of the same apps who
were **slow at the same times**: that they use the same app, not that they
share this user's problem. They are grouped by the NetIM network point
(gateway) they sit behind and compared with the same hours on the same
days of the week before (working days with working days; activities are kept about 8 days). If most of them share one network point, it
is named. A virtual desktop's address is the desktop's own, so those users
are one group and never a network point.

## Start-ups page: "Main causes"

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
