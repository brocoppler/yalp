# Next-session handoff — resume here

**Last updated:** 2026-07-16, end of evening session.
**One-line status:** Victory lap correctly ABORTED — sonar boresight measured
~35-45° LEFT of camera/wheel axis, battery sag killed left-FORWARD and degraded
right-REVERSE by session end, safety geometry was inverted. Charge the pack,
fix the sonar bracket ~40° CW, verify with live data, then drive.

---

## Where we are

- **2026-07-16 supervised floor drive: DONE.** 1.2 m on hardwood via
  `DRIVE_GOAL` straight 1.5 @ 0.3 through `RealReactiveBackend`. Ran under a
  relaxed-grace env config (`YALP_ULTRASONIC_GRACE_MS=20000`,
  `MAX_MISSES=100000`) — supervised-only safety net, never a default. Completed
  on the goal timer; motors ran clean; zero reverse commands. The
  distance-based `SAFE_STOP` stayed armed the whole run but was never reached
  because the sonar never saw the door (reason: it was pointing at the wrong
  wall — see below).

- **Sonar boresight refined: ~35-45° LEFT of camera/wheel axis (not the
  earlier 90-140° CCW estimate).** Evening pivot-sweep triangulation pinned
  this more tightly. Falsification test: hanging a quilt on the door changed
  the door's acoustics but the dominant 174.4 cm reading did not move a
  millimeter — the beam was never on the door. Photos alone repeatedly failed
  to reveal this; only live-data tests did. Full field findings (including the
  visual cross-reference method) are in `hardware.md` 2026-07-16 subsection.

- **Victory lap correctly ABORTED (2026-07-16 evening):** autonomous attempt
  (drive at the quilt-covered door, expect `SAFE_STOP` at 0.30 m) was
  aborted before any drive — no sonar lock on the target, inverted safety
  geometry (distance threshold was unreachable given beam angle), and a
  failing drive channel. No contact; izzy parked clean ~1.2-1.4 m
  square-facing the door.

- **Actuation degradation at session end (prime suspect: battery sag after a
  full day):** left-motor-FORWARD dead, right-motor-REVERSE marginal-to-dead
  at duty 0.3; left-rotation channels (left-reverse + right-forward) still
  worked. Channels failed progressively in order of load. **Charge or swap
  the pack before any motor work next session**, then run a quick channel
  matrix (fwd/rev per wheel, pivot both ways) before trusting any maneuver.

- **Physical state at power-down:** izzy parked ~1.2-1.4 m square-facing the
  quilt-covered door; quilt still hung on the door; black fabric storage cube
  (sonar-invisible absorber) moved out of the drive lane; shoes + kettlebell
  near the left wall by the original lane — **clear them before pivoting**
  (they jammed right-pivots once already).

- **Environment visually confirmed:** the target door is real, closed. The
  quilt/towel stays on the glossy door for all sonar work (specular
  otherwise). Black woven-fabric storage cube is an ultrasound ABSORBER
  (invisible to sonar at any aim) — keep it out of drive lanes.

- **Code landed this session — 2 commits ahead of `origin/main`, not yet
  merged:**
  - `b1e1f34` — SIGINT shutdown fix: the real-stack previously survived
    SIGINT during timeout storms (needed SIGTERM); motors now zeroed first in
    teardown, bounded joins, regression tests added.
  - `fde1587` — Ultrasonic observability counters: state/telemetry now carry
    `total_reads / valid / raw_misses / coasted / unknown_served`. Eliminates
    the lower-bound-only field analysis problem.
  - Earlier (already on main): `YALP_ULTRASONIC_MAX_POLL_HZ` env knob and
    inert-grace construction warning.
  - **izzy is at `0ce8a26` (main).** Pull needed after these commits merge:
    `git pull --ff-only`, then `.venv/bin/pip install -e . -q`.

## THE NEXT TASK

**Both paths require a charged pack first. Run a channel matrix before
trusting any maneuver. Verify sonar aim with live data — never with photos.**

### Step 0 — mandatory before either path

1. **Charge or swap the battery pack.** Left-FORWARD and right-REVERSE were
   dead or marginal at session end. Nothing else until this is done.

2. **Channel matrix:** fwd/rev per wheel, pivot both ways at duty 0.3. Confirm
   all four channels alive before any maneuver. Note any that are still weak.

3. **Clear the corridor:** shoes + kettlebell by the left wall — move them;
   they jammed right-pivots once already. Storage cube already out of lane.

---

### Path A — RECOMMENDED (permanent fix)

**Rotate the sonar bracket ~40° CLOCKWISE** (viewed from above, pitch stays
level) so the transducers co-align with the camera.

- Verify BOTH axes: izzy ~1.2-1.4 m square to the quilt-covered door. Run
  `/tmp/ultra_char.py --hz 15 --seconds 8` (recreate from
  `GpiozeroMotorDriver` session artifacts if `/tmp` was wiped). Expect STEADY
  ~1.2-1.4 m AND the camera frame showing the door centered.
- If not steady, iterate yaw in ~10° steps with live sonar as feedback —
  photos are useless for this.
- **Do not drive until BOTH read true.**

**Victory lap:** stock config (NO relaxed grace),
`DRIVE_GOAL` straight 1.6 @ 0.3. Expect: valid decreasing distance track from
the door, distance-triggered `SAFE_STOP` at ≤ 0.30 m, `BLOCKED`, no reverse,
sticky latch. That run marks the floor-drive rung DONE — update `roadmap.md`
(mirror the milestone-J entry format).

---

### Path B — as-is compensation (no hardware change)

Place/rotate izzy ~40° RIGHT of facing the door so the skewed beam squares
onto the quilt. Verify sonar reads STEADY ~1.1-1.6 m. Drive the same goal
diagonally — closing speed along the sonar ray is ~cos(40°) × 0.15 m/s;
`SAFE_STOP` trips at 0.30 m ray range.

Path A is cleaner and makes future work sane. Path B is a fallback if
hardware adjustment isn't feasible this session.

## Key facts for whoever resumes

- **Connection:** `ssh izzy` (host `izzy.local`). Old static IP is dead. Pack
  switch = motor master cutoff; sensor-only work is safe with it off.

- **Sensor ops procedure (new, hard-won):** after ANY mount adjustment verify
  aim against a known frontal target before trusting any reading. Confident
  stable readings can be a wall behind the robot's shoulder.

- **izzy's `~/yalp` is at `0ce8a26` (main).** The two code commits from this
  session are not yet merged. Pull after merge: `git pull --ff-only`, then
  `.venv/bin/pip install -e . -q`.

- **`/tmp` artifacts on izzy** (`ultra_char.py`, drive scripts, logs) are wiped
  on reboot — recreate from repo/session artifacts if needed. Persistent
  telemetry lives at `~/.local/state/yalp/telemetry/telemetry.jsonl`.

- **Helper scripts for pivots/arcs** were staged in `izzy:/tmp` this session
  (wiped on reboot; trivial to recreate from `GpiozeroMotorDriver` — pivot
  bursts at duty 0.3 with `try/finally` stop).

- **Jam signature:** commanded pivot with zero scene change in camera frames =
  wheel obstruction or dead channel, not a software bug. Check the channel
  matrix and clear physical obstacles first.

- **Calibration and wiring — do NOT re-diagnose:** `~/.config/yalp/calibration.json`
  has `left_invert=true, right_invert=true`; DRV8833 IN/IN driver fix confirmed;
  camera `/dev/video0`; `.env` holds the rotated `ANTHROPIC_API_KEY` (mode 600,
  gitignored). `venv` ownership fixed this session (`pip install -e .` works).

- **Documented field findings (startup-blind latch semantics, timed-goal
  ~20% short calibration, stiction asymmetry forward vs backward/right-pivot):**
  see `hardware.md` 2026-07-16 subsection.

- **Settled facts unchanged:** wiring, driver fix, motor calibration, milestone H
  and J DONE (bench), grace fix verified at 15 Hz, cosmetic warnings expected
  (`cv2.CascadeClassifier` fallback, `gpiozero PWMSoftwareFallback`).

- **Safety ritual (unchanged):** wheels up on stand for motor tests; pack switch
  is the motor master cutoff; dress Pin 1 (3V3) and Pin 2 (5V) wires apart.

## The road after

Charge pack → channel matrix → sonar bracket fix (~40° CW) → victory lap
(floor collision-stop proven, floor-drive rung DONE in `roadmap.md`) →
`yalp see` (vision Q&A; the camera+vision loop already proved itself
diagnosing the sonar) → person-following → voice.
