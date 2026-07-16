# Next-session handoff — resume here

**Last updated:** 2026-07-15, end of the evening on-hardware session on the Pi 5.
**One-line status:** Milestone J (collision-stop) DONE — bench demo PASSED this
evening on the Pi 5. Phantom-stop grace fix verified on hardware. Next: first
floor drive.

---

## Where we are

- **Milestone J — collision-stop reflex: DONE (2026-07-15 evening, bench/wheels-up).**
  Bench demo passed on the Pi 5. `DRIVE_GOAL` straight 10 m @ 0.3 through
  `RealReactiveBackend` (stock config); hand approach gave a clean decreasing
  distance series; `SAFE_STOP` latched at 0.2815 m (< 0.30 m threshold, ~50 ms /
  one 20 Hz tick). Motors zeroed within one tick, `BLOCKED` reported (reason
  `'obstacle'`), zero reverse commands, latch held 15 s after the obstacle
  cleared, clean `SIGINT` shutdown. Roadmap updated.

- **Phantom-stop grace fix (de48495) VERIFIED ON HARDWARE.** At 15 Hz stock
  config, 18/18 induced open-room echo timeouts coasted cleanly — zero phantom
  stops. The prior ~20-miss bursts did not recur (worst observed this session: 3
  consecutive misses). The fix is solid.

- **CONFIG VERDICT: run the reactive stack bone-stock.** Use the defaults —
  15 Hz ultrasonic cap, 150 ms / 2-miss grace. The old "poll at 5-6 Hz, not
  15 Hz" advice is now **obsolete and harmful**: the 150 ms grace window is
  mathematically inert below ~6.7 Hz poll rate (re-pulse interval exceeds the
  window, so 0 misses are absorbed). A new env knob `YALP_ULTRASONIC_MAX_POLL_HZ`
  exists if the cap ever needs changing; grace knobs remain
  `YALP_ULTRASONIC_GRACE_MS` / `YALP_ULTRASONIC_GRACE_MAX_MISSES`. A grace
  construction-time warning now fires automatically if the window goes inert.

- **Motor calibration is set on izzy:** `~/.config/yalp/calibration.json` has
  `left_invert=true, right_invert=true`. Do NOT re-run calibration unless motors
  move wrong.

## THE NEXT TASK — first floor drive

Off the stand, full reactive stack live (collision-stop underneath). Slow speed.
Hand-over-sensor = brake pedal. Battery pack switch = hardware cutoff.

**⚠️ CRITICAL — aim-geometry blind spot (measured this session):**

The slightly downward-tilted ultrasonic sensor locks onto strong background
echoes past low, curved, or off-axis obstacles. It **missed two real obstacles**
during characterisation:

- Curved cat fountain at ~0.9 m actual → read as background **3.04 m**
- Solid obstacle at ~0.46 m actual → read as hard background **1.14 m**

By contrast, it tracks a centred palm cleanly through the 0.30 m threshold, and
in open room the floor-graze echo is a steady ~2.6 m (safe). **Treat the
ultrasonic as a last-resort centred reflex, NOT obstacle perception.** For the
first floor drive, do NOT rely on it to catch furniture or walls — use the pack
switch as a hard stop and your hand as the brake. Details in `hardware.md`
field-findings section. Levers: (a) improve the aim/mount height; (b) consider a
raised/leveled sensor bracket before or after the first drive.

**Steps:**

1. **Power on izzy.** Pack switch ON; wheels UP for any motor checks first, then
   set her DOWN off the stand. Keep the stand nearby for the first few metres —
   you can hop back on it any time.
2. **Connect:** `ssh izzy` (host `izzy.local`, user `izzy`, key auth). The old
   static IP `192.168.86.191` is **dead** — DHCP lease changed; use `izzy.local`
   only.
3. **Early housekeeping (do once):** the `.venv` has root-owned files from an old
   `sudo pip` install — an editable reinstall currently refuses. Fix before a
   pull or reinstall: `sudo chown -R izzy:izzy ~/yalp/.venv`. Harmless to skip
   today if not pulling (the existing editable install already resolves imports
   to the live `~/yalp/src` tree — verified working).
4. **Optional pull:** izzy is at `67b312d` (contains `de48495`). No pull is
   needed for the floor drive. Only pull if you want this session's hardening
   changes (env knob, inert-grace warning, hardware-docs): push them first, then
   `git pull --ff-only` on izzy, then `.venv/bin/pip install -e . -q`.
5. **Set her down and run the floor drive** — same `DRIVE_GOAL` recipe as the
   bench demo: `{"kind":"straight","target":10,"speed":0.3}` through
   `RealReactiveBackend`, bone-stock config. Keep your hand 10–15 cm in front of
   the sensor as a brake pedal throughout (pack switch = hard cutoff). Watch the
   blind-spot geometry — don't approach her from the side or low; come centred
   from the front.
6. **After the drive:** consider leveling/raising the ultrasonic mount to reduce
   the background-echo bias and improve off-axis coverage before tackling
   person-following.

## Key facts for whoever resumes

- **izzy was powered OFF at end of this session.** No state to worry about.
- **Connection:** `ssh izzy` — host `izzy.local`, user `izzy`, ed25519 key auth.
  The old static IP `192.168.86.191` is **dead** (new DHCP lease); `izzy.local`
  only.
- **izzy's `~/yalp` is at `67b312d`** (contains `de48495`). No pull needed for
  the floor drive unless this session's hardening tasks are pushed and wanted.
- **Housekeeping (early next session):** `sudo chown -R izzy:izzy ~/yalp/.venv`
  — fixes root-owned venv files from an old sudo install; harmless to defer (the
  editable install resolves to the live `~/yalp/src` tree), but fix it before any
  install/upgrade.
- **`/tmp` artifacts on izzy** (`ultra_char.py`, `demo_drive.py`, characterisation
  and demo logs) are wiped on reboot — recreate if needed. Persistent telemetry
  lives at `~/.local/state/yalp/telemetry/telemetry.jsonl`.
- **`.env` on izzy** holds the rotated `ANTHROPIC_API_KEY` (gitignored, mode 600).
  `yalp see` / vision will work once that path is reached.
- **Settled facts — do NOT re-diagnose:** header wiring correct; DRV8833 IN/IN
  driver fix confirmed; motor calibration saved (`~/.config/yalp/calibration.json`,
  both inverts `true`); camera is USB `/dev/video0`; `.env` on izzy holds the
  rotated `ANTHROPIC_API_KEY`. Cosmetic known warnings: `cv2.CascadeClassifier`
  fallback (FOLLOW-only concern); `gpiozero PWMSoftwareFallback` on Pi 5 — both
  expected and harmless.
- **Safety ritual (unchanged):** wheels up on stand for motor tests; pack switch
  is the motor master cutoff; keep Pin 1 (3V3) and Pin 2 (5V) wires dressed apart.

## The road after

First floor drive → `yalp see` (vision Q&A, key is ready on izzy) →
person-following (Gate H already measured GO at ~8.8× margin) → voice input on
the C270's built-in mic. All software from here; the body is built and healthy.
