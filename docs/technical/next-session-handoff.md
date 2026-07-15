# Next-session handoff — resume here

**Last updated:** 2026-07-15, end of an on-hardware bring-up session.
**One-line status:** Milestone H (drives) DONE. Milestone J (collision-stop) reflex
proven but the Pi-5 ultrasonic phantom-stops; a debounce fix is merged and awaits
**on-hardware verification** — that verification is THE next task.

---

## Where we are

- **Milestone H — Hello Motors: DONE (2026-07-15).** Both wheels drive forward
  together and pivot both ways, through `GpiozeroMotorDriver` + saved calibration,
  on the replacement Pi 5. The DRV8833 IN/IN driver fix (commit `9c43242`) is
  confirmed on hardware: idle channel stays still, `stop()` coasts.
- **Motor calibration is set on izzy:** `~/.config/yalp/calibration.json` has
  `left_invert=true, right_invert=true` (both motors were mirror-mounted/reversed).
  The real backend auto-loads it. Do NOT re-run calibration unless motors move wrong.
- **Milestone J — collision-stop: reflex PROVEN, not yet cleanly demoed.** The
  safety override works correctly: unknown/timeout → STOP, obstacle → STOP, latches
  `SAFE_STOP`, reports `BLOCKED`, never reverses. BUT on the Pi 5 the HC-SR04 echo is
  timed in software (no pigpio on Pi 5) and throws frequent timeouts — long bursts
  (~20 in a row = several seconds) at open-room range. Every timeout biases to STOP,
  so she phantom-stops and can't drive smoothly yet.
- **Fix merged, lab-tested, pushed:** commit `de48495` — a bounded "coast last-known"
  grace in `GpiozeroUltrasonicSensor`: isolated timeouts coast on the last valid
  reading (bounded by `ULTRASONIC_GRACE_MS` ~150ms and `ULTRASONIC_GRACE_MAX_MISSES`
  ~2-3); sustained blindness still stops; a near obstacle is never coasted into
  "clear". **This has NOT been verified on real hardware.**

## THE NEXT TASK (do this first)

Verify the debounce fix on izzy, then finish milestone J, then first floor drive.

1. **Power on izzy** (SD card in the slot — the replacement Pi boots from `mmcblk0`).
   For motor tests: **pack switch ON, wheels UP** on a stand.
2. **Connect:** wait for `izzy.local`, then `ssh izzy` (key auth; also
   `192.168.86.191`). If the host key complains, `ssh-keygen -R izzy.local` (new Pi).
3. **Pull the fix:** `cd ~/yalp && git pull --ff-only` (must land `de48495` or later),
   then `.venv/bin/pip install -e . -q` if needed.
4. **Look at the real timeout pattern FIRST — don't assume.** Run a ~12s live
   ultrasonic read and watch how often it still reads UNKNOWN with the grace in
   place, both facing open space AND facing a wall ~1-2 m away. Decide empirically.
5. **Clean milestone-J demo:** point the robot at a surface **~1-2 m away** (strong
   echo = reliable reads; open floor is the twitchy case). Drive forward slowly via a
   `DRIVE_GOAL` `{"kind":"straight","target":10,"speed":0.3}` through
   `RealReactiveBackend`, poll ~5-6 Hz (NOT 15 Hz — too fast for the Pi5 sensor),
   then bring a hand in to cross 30 cm (`SAFE_STOP_THRESHOLD_M`). Expect: valid
   decreasing cm → `SAFE_STOP` / `BLOCKED` / no reverse.
6. **If clean:** milestone J is green — mark it DONE in `roadmap.md` (see the
   milestone-H entry for format) and proceed to the **first floor drive** (off the
   stand, full reactive stack, collision-stop live, hand = brake pedal).
7. **If still twitchy at range:** the next levers, in order — (a) lengthen the grace
   window / miss budget; (b) aim/mount the sensor better; (c) treat "no obstacle
   confirmed within range" more leniently than "blind" (a genuine design question,
   not a quick tweak). Let real data pick.

## Key facts for whoever resumes

- **Connection:** `ssh izzy` (host `izzy.local` / `192.168.86.191`, user `izzy`,
  ed25519 key auth). Camera is USB (`/dev/video0`), works regardless of GPIO header.
- **`.env` on izzy** already holds the rotated `ANTHROPIC_API_KEY` (gitignored, mode
  600). `yalp see` / vision will work; nothing else needs the key.
- **Poll the ultrasonic slower than 15 Hz.** At 15 Hz the Pi5 software timing chokes
  (overlapping pings → timeouts). ~5-6 Hz reads reliably against a near surface.
- **Safety ritual:** motor tests are wheels-up; the battery pack switch is the motor
  master cutoff ("batteries off" = switch off); keep header Pin 1 (3V3) and Pin 2 (5V)
  wires dressed apart. Custom test scripts were written to `/tmp` on izzy and are
  wiped on reboot — recreate them (recipes above; or use `yalp hwtest --check ...`).
- **Do NOT re-diagnose settled facts:** the header is fully wired and correct (motors
  + ultrasonic verified working this session); the driver bug is fixed; calibration is
  set. The ONLY open item is the ultrasonic timeout robustness above.

## The road after milestone J

First floor drive → then the fun opens up: `yalp see` (vision Q&A, key is ready) →
person-following (Gate H already measured GO at ~8.8× margin) → voice input on the
C270's built-in mic. All software from here; the body is built and healthy.
