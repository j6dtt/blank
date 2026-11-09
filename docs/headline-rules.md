# How the headline is written

The headline at the top of an investigation is two sentences:

1. **What the user felt**: the action, how long it took, on which PC,
   Aternity's status.
2. **One verdict**: why it was slow, or what is known about it.

Every other slow app and activity gets its own line in the reason
("Also slow: …"), with what is known about it.

Code: `answer._pick_headline` (which class), `answer.build_answer`
(the wording). Tests: `tests/test_headline.py` (one per class) and
`tests/test_real_cases.py` (cases seen on prod).

**The order of the classes is provisional.** It is a judgment, not a
Riverbed rule. Check it against past cases where IT found the real
cause before relying on it.

## No slow actions

The headline counts what the user hit:

- "1 hang, 2 app crashes and 1 Low Memory Pagefaults event, no slow actions"
- "No slow actions, hangs or app crashes" (only background crashes)
- "No problems found" / "No activity found"

## With slow actions: the first class that fits wins

| Class | When | Verdict (example) | Word |
|---|---|---|---|
| A | A problem within `bucket_minutes` of slow actions | "Likely: Microsoft Outlook crashed on VD-41 at 3 of them; 2 more issues (see What went wrong)" | Likely if at over half the slow actions, else Possibly |
| B | Many other users of the same apps slow at once, more than on other days | "Many users behind <point> were slow at the same time" | none |
| C | It became slow after changes on the PC | "Microsoft Outlook Preview Mail on VD-41: slow 2 of 6 times from Nov 6 (4.4 s, usually 0.2 s); normal on each of the 3 days before. 3 changes logged there on Nov 5: possibly one of them" | Possibly |
| D | It became slow and no change to check was logged | "… No change to check was logged there before it (on a virtual desktop the log can run late)" | none |
| E | Right after a start-up, and slower then on every PC of its kind | "Microsoft Teams Launch took 43.8 s on VD-14 (Minor), 3 min after a start-up. On virtual desktops 93% are slow then, 62% later" | none |
| F | Aternity marked most runs slow on every day the user ran it | "… usually 45.0 s. Aternity marked most runs slow on each of the 7 days user1 used it: not new" | none |
| G | Other users as slow as usual at this time | "Other Microsoft Outlook users were as slow as usual at this time" | none |
| H | Nothing found | "No cause found", or "No cause found in what could be read (1 source not read: see Warnings)" | none |

Details:

- **A, tie order:** most slow actions first. On a tie: a crash or hang,
  then a health event (blue screen, low memory, background crash), a
  start-up part, the network (NetIM), a PC reading (CPU, memory, disk
  queue, round trip), and last a nearly full disk.
  **Exception:** when the top problem is only a PC reading and E holds,
  E is the headline. One reading weighs less than a count over many PCs.
  The reading is then said as "Also with it".
- **C:** one change is named ("Possibly: Print Agent updated on Oct 17").
  Several are counted, by day or span. Their names stay in "What
  changed on <PC>". A batch of 5+ "Uninstalled" rows in one minute is
  never counted.
- **C, D only for a real slowdown:** slow on at least 2 runs, and on
  most runs since the first slow one, on a PC (`normal.py` `persistent`,
  `MIN_SLOW`). Otherwise it is occasional: never the headline, no step
  to check changes, an "Also slow" line with both counts ("Microsoft
  Outlook Preview Mail 3.6 s on VD-41 and 5.2 s on VD-14: slow 2 of 22
  times from Nov 6, usually 0.2 s (before: 0 of 32 slow)"). If the
  slowest action is occasional and nothing else fits (G, H), the
  headline says so: "…, slow 2 of 22 times from Nov 6, usually 0.2 s:
  occasional. No cause found".
- **C, D: what was counted, never "slow since".** The runs of that
  activity on that PC from the first slow one ("slow 2 of 6 times from
  Nov 6"): runs in between may have been fast. Then what was normal
  before: every day ("normal on each of the 3 days before") or the
  last normal day. The PC is named once; later it is "there".
- **C, D on several PCs:** each PC it was slow on with its own count
  and time ("slow on 2 desktops from Nov 6: VD-41 1 of 11 times
  (5.2 s), VD-14 1 of 3 times (3.6 s)"). Changes are read on each, up
  to when it first became slow. If one of them had no change: "3
  changes on VD-41 on Nov 5, none on VD-14: they do not explain it
  alone", with no "possibly" (level unsure).
- **C, D, F subject:** the slow app and activity with the most slow
  actions, then the slowest. That can beat the slowest action overall.
- **Thin data is said:** the reason gives "The usual time is from N
  runs in the last D days".
- **PCs by name:** "Other things on VD-41", "on VD-41 and VD-14", "on the
  3 PCs"; never "these PCs".

## Weak assumptions, written down (not changed until a case shows them)

- **A rests on time alone:** a problem within `bucket_minutes` of slow
  actions. A crash that hits hundreds of other PCs counts as strongly
  as one only this PC has.
- **A PC reading counts only for its own action:** depends on what an
  activity row's CPU is (admin question 11).
- **F ("not new") is a majority per day:** with about 8 days kept,
  few days carry it.
- **The class order is a judgment**, not a Riverbed rule.

## What cannot be a verdict

Aternity does not record these, so no headline can rest on them:
per-minute CPU per program (hourly only), the network between a thin
client and its virtual desktop, and the hang time's unit.
