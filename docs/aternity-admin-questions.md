# Questions for the Aternity admin

What UX Correlator could not get from our Aternity, found by
`uxcorrelate verify` on the live host (Oct 31 – Nov 1, 2026). Each item
says what we saw, why it matters, and what to ask. Most important first.

## 1. Remote display latency is empty for our VMware Horizon desktops

- **Seen:** `REMOTE_DISPLAY_LATENCY_RAW` (and `_HOURLY`, `_DAILY`) is not
  published. In `BUSINESS_ACTIVITIES_RAW`, `ACTIVITY_REMOTE_DISPLAY_LATENCY_AVG`
  has no value on any row, though 612 of 1000 rows are virtual desktops
  (`VIRTUALIZATION` = 'true', client type 'Remote Device'). The devices
  users sit at (`CLIENT_DEVICE_NAME`, mostly names starting "WT") are not
  Aternity devices: none of the 20 most used is in `DEVICE_INVENTORY`.
- **Why it matters:** this is the delay a Horizon user feels between their
  own device and the desktop. Without it, the network is not checked for
  virtual desktop users.
- **Ask:** What does Aternity need to measure remote display latency for
  Horizon (Blast / PCoIP): a setting, a licence, an agent version, an agent
  on the client device? Can it be turned on, and can the
  `REMOTE_DISPLAY_LATENCY_*` data sources be published to the REST API?

## 2. Network round-trip time is empty on every activity

- **Seen:** `NETWORK_RTT_AVG` in `BUSINESS_ACTIVITIES_RAW` has no value in
  any row of the last day (physical and virtual PCs).
- **Why it matters:** a slow connection from the PC is never seen.
- **Ask:** What fills `NETWORK_RTT_AVG`? Is it off on our agents, or not
  collected for our setup?

## 3. No top-processes data source

- **Seen:** `RAW_DATA_TOP_PROCESSES` is not published. Only
  `APPLICATION_RESOURCES_HOURLY` has process data: CPU and memory per
  process per hour, no disk IO columns. The tool now uses it (CPU as a
  percent, `PRC_PHYSICAL_MEMORY_UTIL_*` as bytes: values 241664 to 8.2e9).
- **Why it matters:** when a PC's CPU, memory or disk is high at a slow
  action, we can't say which process caused it, and hourly values blur a
  spike of a few minutes.
- **Ask:** Can `RAW_DATA_TOP_PROCESSES` be published (with
  `PRC_IO_READ_RATE` / `PRC_IO_WRITE_RATE`)? If not, are the IO columns of
  `APPLICATION_RESOURCES_HOURLY` available? Please confirm the units:
  `PRC_CPU_UTIL_AVG` / `_MAX` percent of the whole PC (live max 30)?
  `PRC_PHYSICAL_MEMORY_UTIL_*` bytes, despite "UTIL" in the name? Does a
  row's `TIMEFRAME` (seen at any minute, e.g. :04, :39) mark the start or
  the end of its hour?

## 4. Software comes and goes in the change log

- **Seen:** in `INSTALLED_SOFTWARE_CHANGE_LOG`, the same version of a
  software is "Uninstalled" one day and "Installed" the next, again and
  again, on the same PC (e.g. Chrome and Notepad++ around 09:00 daily),
  with no other PC doing it in that minute. Real updates look different
  (removal of the old version and install of the new one 2 min apart).
- **Why it matters:** these rows look like changes before a problem. The
  tool now marks them "comes and goes in Aternity's log" and never points
  at them, but a real removal of such software could hide among them.
- **Ask:** Why does the agent log these? (Per-user installs seen only when
  that user is signed in? An inventory scan that misses them sometimes?)
  Can it be fixed or filtered on the Aternity side?

## 5. Image build is empty; management client is on few devices

- **Seen:** `DEVICE_IMAGE_BUILD_NUMBER` is 'N/A' on every row (activities,
  boots, inventory). `SCCM_CLIENT_VERSION` is filled on 176 of 1000
  devices.
- **Why it matters:** a Horizon pool refresh or a new golden image can
  change many desktops at once; without the image build we can't tell when
  a desktop's image changed.
- **Ask:** Can the agent report the Horizon image (golden image / snapshot)
  in `DEVICE_IMAGE_BUILD_NUMBER` or a custom attribute? Which devices are
  managed by what (SCCM, Intune), and can that be a device attribute?

## 6. Logons without a boot have no breakdown

- **Seen:** `MACHINE_BOOT_COMPONENTS` has parts for boots (driver,
  service, group policy, boot phases) but a "User Logon" without a boot
  usually has none.
- **Why it matters:** on pooled Horizon desktops most start-ups are
  logons; we can say a logon was slow, not what took the time (profile,
  group policy, shell).
- **Ask:** Can Aternity record logon parts for every logon (profile load,
  group policy, shell start)? Is a newer agent or a setting needed?

## 7. Units and meanings we could not find documented

- `APPLICATION_HANG_TIME_TOTAL`: what unit (seen: 3607 to 30089)?
- `MEASUREMENT_DURATION_AVG` (boots, logons): seconds? (seen: 1.9 to 426)
- `SLA_STATUS` = 'Undefined' (26 of 200 rows): what does it mean, and
  should it count as slow?
- Start-up types 'Machine Boot' and 'Total Boot Duration': what is the
  difference?
- Health event 'Low Virtual Memory' with details "Percent Remaining
  Memory: 100%": how should it be read?
- `DEVICE_USED_REMOTELY` is not published: is it available?

## Not for the Aternity admin (NetIM)

- None of the subnets seen is in NetIM today: the virtual desktops' (87
  desktops in 16 /24s) and the physical PCs' (40 PCs in 11 /24s). Until
  they are added, the network of a user's PC is not checked.
  `uxcorrelate verify` lists them ("virtual desktops' own addresses",
  "physical PCs' addresses").
- NetIM alerts are off (`netim.alerts_path` empty): the active-alerts path
  is in NetIM's HELP > API Documentation.
