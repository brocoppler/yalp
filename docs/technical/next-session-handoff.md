# Next-session handoff — resume here

**Last updated:** 2026-07-15, end of an on-hardware evening session on the Pi 5.
**One-line status:** Milestone J (collision-stop) DONE — bench demo PASSED this
evening. First floor drive is the next task.

---

## Where we are

- **Milestone J — collision-stop reflex: DONE (2026-07-15, bench/wheels-up).**
  `DRIVE_GOAL` straight 10 m @ 0.3 through `RealReactiveBackend` (stock config).
  Hand approach gave a clean decreasing distance series; `SAFE_STOP` latched at
  0.2815 m (< 0.30 m threshold, ~50 ms / one 20 Hz tick). Motors zeroed within
  one tick, `BLOCKED` reported (reason `'obstacle'`), zero reverse commands,
  latch held for 15 s after obstacle cleared, clean `SIGINT` shutdown.
  Roadmap updated.

- **Phantom-stop grace fix (de48495) VERIFIED ON HARDWARE.** At 15 Hz stock
  config, 18/18 induced open-room echo timeouts coasted — zero phantom stops.
  The prior ~20-miss bursts did not recur (worst observed this session: 3
  consecutive misses). The fix is solid.

- **CONFIG VERDICT: run bone-stock.** Use the defaults — 15 Hz ultrasonic cap,
  150 ms / 2-miss grace. The old "poll at 5-6 Hz" advice is now **obsolete and
  harmful**: the 150 ms grace window is mathematically inert below ~6.7 Hz
  (re-pulse interval exceeds the window, so 0 misses are absorbed). If the poll
  cap ever needs changing, the env knob is `YALP_ULTRASONIC_MAX_POLL_HZ`;
  grace knobs are `YALP_ULTRASONIC_GRACE_MS` / `YALP_ULTRASONIC_GRACE_MAX_MISSES`.
  Grace construction-time warning fires automatically if the window goes inert.

## THE NEXT TASK — first floor drive

Off the stand, full reactive stack live, collision-stop underneath. Slow speed.
Hand-over-sensor = brake pedal. Battery pack switch = hardware cutoff.

1. **Power on izzy.** Pack switch ON, wheels DOWN (off the stand). Keep the
   stand nearby for the first few metres — you can hop back on it any time.
2. **Connect:** `ssh izzy` (host `izzy.local`, user `izzy`, key auth). The old
   static IP `192.168.86.191` is **dead** — DHCP lease changed; use `izzy.local`
   only.
3. **Early housekeeping (do once):** the `.venv` has root-owned files from an
   old `sudo pip` install. Fix before trying a pull or editable reinstall:
   `sudo chown -R izzy:izzy ~/yalp/.venv`. Harmless to skip today if not
   pulling (existing editable install already resolves imports to `~/yalp/src`
   — verified working).
4. **Optional pull:** izzy is at `67b312d` (contains `de48495`). No pull is
   needed for the floor drive. If you want this session's hardening changes
   (env knob, inert-grace warning, hardware-docs), push them first, then
   `git pull --ff-only` on izzy, then `.venv/bin/pip install -e . -q`.
5. **Run the floor drive** — same `DRIVE_GOAL` recipe as the bench demo:
   `{"kind":"straight","target":10,"speed":0.3}` through `RealReactiveBackend`,
   bone-stock config. Keep your hand 10–15 cm in front of the sensor as a
   brake pedal throughout.

**CRITICAL — aim-geometry blind spot (measured this session):**
The ultrasonic sensor is aimed slightly downward and locks onto strong
background echoes past low or curved obstacles. Two real obstacles were
**invisible** to it: a curved cat fountain at ~0.9 m actual distance read as
background 3.04 m; a solid obstacle at ~0.46 m read as hard background 1.14 m.
By contrast, it tracks a centred palm cleanly through the 0.30 m threshold, and
in open room the floor-graze echo is a steady ~2.6 m (safe). **Treat the
ultrasonic as a last-resort centred reflex, not obstacle perception.** For the
first floor drive, do NOT rely on it to catch furniture or walls — use the pack
switch as a hard stop and your hand as the brake. Details in `hardware.md`
field-findings section. Levers: (a) improve the aim/mount height; (b) consider
a raised sensor bracket before or after the first drive.

## Key facts for whoever resumes

- **izzy was powered OFF at end of this session.** No state to worry about.
- **Connection:** `ssh izzy` — host `izzy.local`, user `izzy`, ed25519 key
  auth. The old static IP `192.168.86.191` is dead; `izzy.local` only.
- **izzy's `~/yalp` is at `67b312d`** (contains `de48495`). No pull needed for
  the floor drive.
- **Housekeeping (early next session):** `sudo chown -R izzy:izzy ~/yalp/.venv`
  — fixes root-owned venv files; harmless to defer if not pulling or reinstalling.
- **`/tmp` artifacts on izzy** (`ultra_char.py`, `demo_drive.py`,
  characterisation and demo logs) are wiped on reboot — recreate if needed.
  Persistent telemetry lives at `~/.local/state/yalp/telemetry/telemetry.jsonl`.
- **Settled facts — do NOT re-diagnose:** header wiring correct; DRV8833 IN/IN
  driver fix confirmed; motor calibration saved (`~/.config/yalp/calibration.json`,
  both inverts `true`); camera is USB `/dev/video0`; `.env` on izzy holds the
  rotated `ANTHROPIC_API_KEY`. Motor safety ritual: pack switch is the motor
  master cutoff; keep Pin 1 / Pin 2 wires dressed apart. Cosmetic known warnings:
  `cv2.CascadeClassifier` fallback (FOLLOW-only concern); `gpiozero`
  `PWMSoftwareFallback` on Pi 5.

## The road after

First floor drive → `yalp see` (vision Q&A, key is ready on izzy) →
person-following (Gate H already measured GO at ~8.8× margin) → voice input
on the C270's built-in mic. All software from here; the body is built and healthy.
