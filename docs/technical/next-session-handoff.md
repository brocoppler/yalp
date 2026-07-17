# Next-session handoff — resume here

**Last updated:** 2026-07-17, morning session (floor drive earned).
**One-line status:** First floor drive MILESTONE EARNED 2026-07-17 — three
frame-verified laps, true-range sonar throughout, SAFE_STOP at served 0.30 m
(reason `'obstacle'`), sticky latch, live operator-in-lane refusals. The body
and reflexes are done; next up is the mind: `yalp see` vision Q&A.

---

## Where we are

- **Floor-drive milestone: EARNED.** Four drives (A–D) across the session
  closed the rung:
  - **Drive A:** Timed-goal completion on a long lane — wall track confirmed
    true-range descent from 1.94 m to 1.08 m.
  - **Drive B:** Leftward-veer run (defect filed — see backlog item 2 below).
  - **Drive C:** Goal timer expired 2 cm above the SAFE_STOP threshold — no
    contact, clean exit.
  - **Drive D (earning run):** SAFE_STOP triggered at served 0.30 m, reason
    `"obstacle"`. Motors zeroed in one tick; retry refused by the sticky
    latch. This is the run that earned the milestone.

- **Sonar quality across all drives:** ~5,000 reads, 100% valid, zero misses.
  `GpiodUltrasonicSensor` (gpiod backend) confirmed in-stack — the kernel irq
  thread is visible in the process table. No gpiozero fallback, no inflation
  artifact.

- **Frame-verified motion gate is now standard practice:** one visible pivot
  burst in camera frames before trusting any motor run. Exit codes are never
  proof of motion — this gate is.

- **SIGINT shutdown verified graceful** at session end (motors zeroed, bounded
  joins, clean exit).

- **Immobility root cause (that morning):** the motor pack's red VM lead had
  been pulled out during charging. Reseated at breadboard J1 per
  `as-built-wiring.md` §3.3. The session then proceeded without further power
  issues.

- **Proven session playbook (now first-class tooling):**
  - `yalp hwtest --check motors --matrix` — 6-step per-channel triage
    (LEFT-FWD/REV, RIGHT-FWD/REV, PIVOT-LEFT/RIGHT).
  - `yalp hwtest --check ultrasonic --seconds N --hz H` — timed soak with
    valid%, miss%, min/median/max/stdev, longest-miss streak.
  - `yalp drive` — gated one-command drive (pre-flights safety, sends
    `DRIVE_GOAL`, polls live timeline, refuses genuine obstacles before
    sending intent).
  - Frame-verified motion gate before trusting any motor run.

---

## Defect backlog (minor — none blocking)

1. **Open-loop timed distance over-reports ~1.8×:** ~0.9 m actual on a
   1.6 m commanded goal. Fix: recalibrate the speed constant, or add
   encoders. Side-effect: goal-timer-vs-reflex race on long approaches (the
   reflex wins cleanly but the timer fires early). Non-blocking.

2. **Leftward veer** under way — motor trim calibration needed.

3. **Floor-graze mode ~0.6 m** — the sensor module's bottom edge leans
   forward a few degrees, putting the lower beam skirt on the hardwood ~0.6 m
   out. Permanent fix: shim the module's bottom edge up 1–2 degrees. Benign
   meanwhile: it can only stop her early, never late.

4. **Retinue-side (not this repo):** tasks that depend on archived task IDs
   stall. Known workaround: clear `depends_on` on the stalled task.

---

## THE NEXT TASK: `yalp see` — vision Q&A

The `ANTHROPIC_API_KEY` is already in izzy's `.env` (mode 600, gitignored).
The camera is proven — it diagnosed the sonar saga and has been the motion
gate all session.

**Suggested first session:**

1. Run `yalp see`.
2. Ask her what's in front of her.
3. Compare the answer with a live sonar reading.

That's the first camera + sonar + LLM moment.

**After that, in order:**

- **Person-following** — Gate H measured GO at ~8.8× margin. The detection
  path is already exercised.
- **Voice** — on the C270 mic (same USB device as the camera).

---

## Key facts for whoever resumes

- **Connection:** `ssh izzy` / `izzy.local`. Old static IP is dead — do not
  use it. Pack switch = motor master cutoff; sensor-only work is safe with it
  off.

- **Repo state:** izzy is at `468f457` or later on `origin/main`. Pull if the
  roadmap/handoff commits from this session get pushed:
  ```
  cd ~/yalp && git pull --ff-only && .venv/bin/pip install -e . -q
  ```

- **Robot parked:** nose ~30 cm from the wall at last SAFE_STOP, stack down,
  process table clean. Pack switch state is per operator.

- **Motor power path — do not re-diagnose:**
  `red = VM = breadboard J1`, `black = J3`, caps' + legs on the VM column.
  See `as-built-wiring.md` §3.3 before touching anything in J1.

- **Settled facts — do NOT re-diagnose:** `~/.config/yalp/calibration.json`
  has `left_invert=true, right_invert=true`; DRV8833 IN/IN driver fix
  confirmed; camera `/dev/video0`; `.env` holds `ANTHROPIC_API_KEY` (mode
  600, gitignored). `GpiodUltrasonicSensor` auto-selected when
  `python3-libgpiod` v2 is present (force with
  `YALP_ULTRASONIC_BACKEND=gpiod`; chip via `YALP_GPIOCHIP`). Milestones H,
  J, and floor-drive are all DONE — do not re-run them to prove the stack.

- **Jam signature (unchanged):** commanded pivot with zero scene change in
  camera frames = wheel obstruction or dead channel, not a software bug. Run
  the channel matrix, clear physical obstacles.

- **Cosmetic warnings expected (unchanged):** `cv2.CascadeClassifier` fallback,
  `gpiozero PWMSoftwareFallback`. Both harmless.

- **Safety ritual (unchanged):** wheels up on stand for motor tests; pack
  switch is the motor master cutoff; dress Pin 1 (3V3) and Pin 2 (5V) wires
  apart.

---

## The road after

`yalp see` (vision Q&A — camera + sonar + LLM first moment) → person-following
(Gate H GO at ~8.8× margin) → voice on the C270 mic → trim calibration
(fix leftward veer) → encoder addition or speed-constant recalibration (fix
open-loop distance over-reporting) → sensor shim (clean floor headroom
telemetry).
