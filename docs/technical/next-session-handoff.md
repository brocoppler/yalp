# Next-session handoff — resume here

**Last updated:** 2026-09-15 (first session after the summer: wiring regression
found and fixed, DRV8833 decay-mode asymmetry found and fixed, **visual heading
hold + closed-loop turns + trim learning landed and verified on the robot**).
**One-line status:** Izzy drives straight and turns to a commanded angle without
encoders — the camera is her heading sensor. Straight legs hold heading within
~±5° forward and reverse (was 14°–42° of veer); `yalp drive --turn 90` turns
+96° / −91° (old open-loop timer: 2–15°). Calibration on the Pi: `left_invert=false`,
`right_invert=true`, trims 1.0, learned `straight_bias_*` ≈ +0.01.
Road: decide the SAFE_STOP escape rule (below), then longer lanes / person-following.

---

## 2026-09-15 session — what happened, in order

1. **Motor pack was switched off** at first (no motion, exit 0 — the drive CLI's
   open-loop "complete" proves nothing; the sonar not moving did). Switched on.
2. **She spun left instead of driving straight.** The left TT motor's leads at
   H4/H5 are swapped relative to July. Fixed in the calibration file:
   `left_invert=false` (backup `calibration.json.bak-2026-09-15`).
3. **Still veered, and the veer flipped sign with duty** (+14° left at 0.45,
   −42° right at 0.60, ~100° left in reverse). Per-wheel camera measurement
   (`as-built-wiring.md` §3.6 table) showed the DRV8833 "mixed" PWM dialect put the
   two wheels in different decay modes once only one was inverted. **Fix:**
   `config.MOTOR_DECAY_MODE="slow"` — uniform slow decay on both channels/both
   directions (DIR pins are software-PWMed too). No re-wiring.
4. **Visual heading hold** (`yalp/reactive/visual_odometry.py` + `ReactiveTickCore`):
   frame-to-frame yaw from phase correlation (left/right halves averaged to
   cancel looming), P+D correction on the straight-drive duty split, learned
   feed-forward bias persisted to the calibration file after every straight drive
   (also on SAFE_STOP / preemption). Closed-loop rotate goals with a predictive
   stop lead. `yalp drive --turn DEG` added; the timeline prints `hdg=`.
   Config knobs: `HEADING_HOLD_*`, `ROTATE_*`, `TRIM_LEARNING_*`, `CAMERA_HFOV_DEG`.
5. **Camera warm-up** (`camera.WARMUP_MAX_SECONDS` 1.5 → 3 s): the C270 publishes
   black frames for ~1 s after open; the heading estimate is blind until then.
   Give the server ~4 s after its port comes up before the first drive.

**Open design decision — SAFE_STOP has no escape.** Once the reflex latches at
<0.30 m, the tick halts *every* goal, and the drive CLI pre-flight refuses
reverse and rotate too. On a small floor she strands herself after every forward
leg and after any pivot that sweeps the sonar across furniture (happened twice
this session). Recommendation: allow `rotate` goals and NEGATIVE straight drives
while the obstacle latch is active (neither moves the nose forward; the reflex
still halts any forward motion), and have the pre-flight pass them. This changes
software-spec.md §2.3 — operator's call, not made unilaterally.

**Measured this session (slow decay, single-wheel |yaw rate| deg/s, no trims):**
duty 0.45 → L fwd 104 / R fwd 66 / L rev 111 / R rev 76; duty 0.60 → 121 / 89 /
81 / 102. Left is ~20–35% stronger forward; the heading hold + learned bias absorb it.

---

## Where we are (as of 2026-07-17 — still accurate unless noted above)

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

- **Drivetrain tuned (2026-07-17 afternoon).** Motor stall confirmed at duty
  0.30 — zero displacement over a 5.4 s commanded drive (frame- and
  sonar-verified). Duty 0.45 reliably above stiction. Speed sag measured:
  0.155 m/s fresh → 0.114 m/s after ~28 runs (~26% across one session —
  recharge between long sessions). Pivots stiction-dominated at duty 0.45:
  commanded 30–60° delivers 2–15°. Reverse is slower than forward and
  pulls the nose right. Deadband code landed in commit `ebbd727` (see Key
  facts). Calibration written to `~/.config/yalp/calibration.json` (backup
  at `calibration.json.bak-pretune`): `max_speed_mps 0.29`,
  `right_trim 0.90`. Post-tune verification: commanded 1.0 m → 0.94 m
  sonar-true, veer ~7 cm/m, SAFE_STOP re-verified at served 0.28 m.

- **First `yalp see` on-robot run (2026-07-17 afternoon).** `yalp see` ran
  from `~/yalp` with the API key loaded via `load_dotenv` from `.env`; the
  C270 webcam captured a frame; the model returned an accurate scene
  description of the wall corner, baseboard, and floor Izzy faced —
  verified against the frame by an independent reviewer. Milestone C
  done-signal satisfied; PASSED 2026-07-17 in the roadmap.

---

## Defect backlog (minor — none blocking)

1. **Open-loop timed distance over-reports ~1.8×:** ~0.9 m actual on a
   1.6 m commanded goal. Fix: recalibrate the speed constant (now
   `max_speed_mps 0.29` in calibration), or add encoders. Side-effect:
   goal-timer-vs-reflex race on long approaches (the reflex wins cleanly
   but the timer fires early). Non-blocking.

2. **Veer** under way — tuned to `right_trim 0.90` this session; veer now
   ~7 cm/m. A fresh-battery confirmation run is recommended before declaring
   the trim final (battery sag shifts the asymmetry).

3. **Floor-graze mode ~0.6 m** — the sensor module's bottom edge leans
   forward a few degrees, putting the lower beam skirt on the hardwood ~0.6 m
   out. Permanent fix: shim the module's bottom edge up 1–2 degrees. Benign
   meanwhile: it can only stop her early, never late.

4. **Battery-sag awareness** — speed droops ~26% over ~28 runs in one
   session (0.155 → 0.114 m/s at duty 0.45). Recharge between long sessions.
   Future: recharge flag or per-session recal.

5. **Rotation model** — `turn_rate_dps` 120 over-predicts small turns
   5–10× below ~duty 0.5 (stiction). Reserving a `turn_duty_deadband`
   calibration field and `TURN_STICTION_DUTY` config constant for future
   tuning (commit `ebbd727`); full model deferred.

6. **Retinue-side (not this repo):** tasks that depend on archived task IDs
   stall. Known workaround: clear `depends_on` on the stalled task.

---

## THE NEXT TASK: richer `yalp see` session and the road to person-following

The first `yalp see` on-robot run is done — frame captured, accurate scene
description returned, independently verified (2026-07-17 afternoon). The
camera + LLM seam is live. What's next is deepening it.

**Suggested next session:**

1. Run `yalp see` with a free-text question (e.g. "what obstacles are near
   the floor?") and compare the answer with a live sonar reading.
2. Try `yalp see --speak` — verify the TTS path on Izzy end-to-end.
3. Multiple scenes / questions in one session — prove the interaction loop,
   not just the single-shot capture.

**After that, in order:**

- **Person-following (M)** — Gate H measured GO at ~8.8× margin; the
  detection path is already exercised on the laptop. Drive the bench loop
  on Izzy.
- **Voice** — push-to-talk STT (`yalp agent --listen`) on the C270 mic
  (same USB device as the camera).

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

- **Calibration state (2026-09-15):** `~/.config/yalp/calibration.json` has
  `left_invert=false, right_invert=true, left_trim=1.0, right_trim=1.0,
  max_speed_mps=0.29, turn_rate_dps=120` plus the LEARNED `straight_bias_fwd` /
  `straight_bias_rev` (≈ +0.01; rewritten by the reactive layer after straight
  drives). Backups: `calibration.json.bak-pretune` (July) and
  `calibration.json.bak-2026-09-15` (pre-session). The July line below is history:
  ~~`left_invert=true, right_invert=true, right_trim=0.90`~~ — `right_trim 0.90`
  was tuned for the old mixed decay dialect and is WRONG under slow decay.
  The `duty_deadband` key is NOT in the live calibration file (pre-deadband
  file: from_dict defaults it to 0.0, preserving the measured linear
  behavior — see below).

- **Deadband code landed (commit `ebbd727`):** `MotorCalibration` now has
  `duty_deadband` (field default 0.27) and `turn_duty_deadband` (default 0.0)
  fields; `config.py` adds `DRIVE_DUTY_DEADBAND` (0.27), `TURN_STICTION_DUTY`
  (0.5), `TURN_DUTY_DEADBAND` (0.0). Back-compat: `from_dict` defaults a
  MISSING `duty_deadband` to 0.0 (not 0.27), so the live calibration file
  reproduces the old linear model exactly. `yalp drive` default `--speed`
  raised from 0.3 → 0.45. Stall/rotation warnings added to CLI and backend.

- **Settled facts — do NOT re-diagnose:** DRV8833 IN/IN driver fix confirmed;
  camera `/dev/video0`; `.env` holds `ANTHROPIC_API_KEY` (mode 600, gitignored)
  — key loads via `load_dotenv` when running `yalp see` from `~/yalp`.
  `GpiodUltrasonicSensor` auto-selected when `python3-libgpiod` v2 is present
  (force with `YALP_ULTRASONIC_BACKEND=gpiod`; chip via `YALP_GPIOCHIP`).
  Milestones H, J, floor-drive, and C (`yalp see` first-light) are all DONE —
  do not re-run them to prove the stack.

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

Richer `yalp see` session (interactive Q&A, `--speak` path, multiple scenes)
→ person-following (M — Gate H GO at ~8.8× margin, laptop-proven detection
path) → voice on the C270 mic (`yalp agent --listen`) → fresh-battery
confirmation run for `right_trim 0.90` → encoder addition or speed-constant
recalibration (fix open-loop distance over-reporting) → rotation model tuning
(`turn_duty_deadband`, `TURN_STICTION_DUTY` hooks are in code) → sensor shim
(clean floor headroom telemetry).
